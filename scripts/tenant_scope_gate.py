#!/usr/bin/env python3
"""Fail a PR that adds a storage route or SQL-layer method with no tenant scope.

Two advisories this month were the same defect wearing different clothes.
GHSA-xw4x-jwf5-8m9h validated the tenant a request *named* and never checked it
against the row the request *addressed*. GHSA-wgvw-28pq-jc36's exploit primitive
was smaller still: ``POST /memories/bulk-get`` makes the ``tenant_id`` filter
optional, so omitting it returns any row by id across every tenant.

Neither is an accident of one endpoint. The tenancy invariant — *every statement
that touches a tenant-scoped table is bound to a tenant* — is held up across the
storage surface by convention alone. Nothing reads it, so nothing notices when
it lapses, and a lapse is not a syntax error, not a type error and not a test
failure. It is a row belonging to somebody else.

This makes the invariant checkable. It does NOT attempt to prove that any
particular statement is correctly scoped: that is a semantic question, a gate
that guesses at semantics produces false positives, and a security gate with
false positives gets switched off. What it checks instead are three things that
are exactly decidable:

1. **Completeness.** Every route and every public service method is enumerated
   from the code itself. Nothing is skipped because someone forgot to list it.
2. **Justification.** Anything that does not take a binding tenant scope must
   appear in the allowlist with a reason someone wrote and a reviewer read.
3. **Direction.** The allowlist may shrink. It may not grow. A new unscoped
   path fails the build (``--base``).

The semantics stay a human claim, as they must, but the claim is forced into the
diff where review can see it, and the population it has to cover can only get
smaller.

**Why an allowlist of exceptions rather than a manifest of everything.** A file
holding all ~380 entries churns on every route added, which trains reviewers to
skim it, and a line that is skimmed is not reviewed. Listing only the exceptions
means adding a properly scoped route changes nothing here, and adding an
unscoped one is a line in the diff. Completeness does not suffer: the
enumeration runs against live code on every invocation, so an entry matching
nothing is reported as stale and an unscoped path that is not listed fails. Same
reasoning as ``scripts/mypy.ini``'s per-module exemptions — named entries, never
globs.

**Why routes are classified by AST and methods by signature.** The service
methods are decidable exactly: ``inspect.signature`` says whether a binding
scope parameter is there and whether it has a default. Routes are not. Storage
handlers take ``request: Request`` and parse the body themselves, so the tenant
never appears in a signature and the only evidence is what the function body
does with a dict. The AST pass reads that, and it is a heuristic: it recognises
``_require(body, "tenant_id")`` and ``body["tenant_id"]`` as required and
``body.get("tenant_id")`` as optional, and it cannot see an inline
``if not isinstance(...): raise`` guard. Those blind spots cost one honest
allowlist line each, which is the right price — a wrong "this is scoped" is
invisible, while a wrong "this is not" is a line a reviewer deletes.

Usage::

    python3 scripts/tenant_scope_gate.py                       # check
    python3 scripts/tenant_scope_gate.py --base origin/main     # check + ratchet
    python3 scripts/tenant_scope_gate.py --write                # reseed allowlist
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "core-storage-api" / "tenant_scope_allowlist.json"
ROUTERS_DIR = REPO_ROOT / "core-storage-api" / "src" / "core_storage_api" / "routers"

# Resolve imports from the repo layout rather than from the caller's
# environment. ``common`` is a plain directory at the repo root and is on no
# installed path even in CI, where the services are installed editable — so
# without this the gate runs only under a PYTHONPATH somebody remembered to
# set, and a gate that needs an incantation is one that gets invoked wrong and
# skipped. Prepended so a checkout always wins over an installed copy.
for _path in (REPO_ROOT, REPO_ROOT / "core-storage-api" / "src", REPO_ROOT / "core-api" / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Parameter names that BIND a statement to a tenant. ``org_id`` is here because
# organizations are the tenant boundary for the settings and lifecycle-audit
# tables, which key on it instead of ``tenant_id``.
#
# SINGULAR ONLY. ``tenant_ids`` (plural) was here and has been removed: a
# parameter that takes a LIST of tenants cannot, by its shape, be the thing that
# confines a statement to the caller's own — it says which tenants to span, and
# something upstream has to have earned that span. The gate cannot see whether
# the list was derived from a verified caller-tenant relationship or taken
# verbatim from the body, and it does not get the benefit of the doubt: crediting
# it is a false REQUIRED, the direction this gate must never be wrong in.
#
# Measured rather than assumed, at the time this changed: exactly one signature
# in PostgresService takes ``tenant_ids`` — ``tenant_usage_query`` — reached only
# by ``POST /tenant-usage/query``, which passes ``body.tenant_ids`` straight from
# the request to the query, with no binding tenant anywhere in the call. So the
# only instance is the unsafe one, and it was passing as fully scoped. It is now
# carried explicitly under ``grant-in-lieu-of-tenant``.
BINDING_SCOPE = frozenset({"tenant_id", "org_id"})

# ``readable_tenant_ids`` is deliberately NOT in the set above. It is the
# opposite of a scope: a caller-supplied grant that WIDENS a read across sibling
# tenants. Every call site reads
#
#     if readable_tenant_ids:  Model.tenant_id.in_(readable_tenant_ids)
#     else:                    Model.tenant_id == tenant_id
#
# so omitting it narrows to the home tenant — fail-closed, which is only true
# because a binding ``tenant_id`` is always there to fall back to. Counting it
# as a scope would let a method satisfy this gate while having no binding tenant
# at all, and in that method omitting the grant WOULD be unscoped. That is what
# ``_widening_grant_findings`` exists to prevent.
WIDENING_GRANT = "readable_tenant_ids"

# A sentinel-typed parameter is a required, explicitly-spelled opt-out rather
# than a default-unsafe one, so it does not count as "defaulted" below.
SENTINEL_TYPE = "Unscoped"

# The guards in ``routers/_validation.py`` that raise on a missing or malformed
# key. A call to one of these IS the fail-closed contract, so it is what makes a
# route REQUIRED.
#
# Enumerated, not prefix-matched. ``startswith("_require")`` reads as a naming
# convention and behaves as a promise about code nobody has written yet: a
# future ``_require_if_present`` or ``_require_optional`` — helpers whose names
# say plainly that they do NOT raise — would be trusted as proof of scoping and
# would drop the route off this list and out of the ratchet. That is the one
# direction the gate must never be wrong in, and a convention is not the place
# to hold it. ``test_every_fail_closed_guard_exists_and_raises`` checks each
# name here is really defined and really raises, so this set cannot rot into a
# claim about functions that no longer match it.
FAIL_CLOSED_GUARDS = frozenset({"_require", "_require_dict", "_require_number"})

# Dict reads that pull a key out of the parsed body. ``pop`` counts: a handler
# that must keep the scope out of a column update reads it that way, and it is
# no less a read of the request for removing the key afterwards.
DICT_READS = frozenset({"get", "pop"})

# How much of a binding a verdict represents, so "weaker than" is a comparison
# rather than a rule spelled out at each site. REQUIRED binds every call;
# OPTIONAL binds the calls that pass the scope; NONE cannot bind at all.
VERDICT_STRENGTH = {"NONE": 0, "OPTIONAL": 1, "REQUIRED": 2}

# Categories that describe work still to do rather than a settled decision.
# Flagged in the summary so the number a reader takes away is the one that
# should be falling, not the total.
BACKLOG_CATEGORIES = frozenset(
    {"id-addressed-write", "id-addressed-read", "blind-spot", "grant-in-lieu-of-tenant"}
)

# A tracked follow-up in an entry's note, e.g. "Tracked in #1086.". Required on
# the mutating backlog so each of those entries has somewhere the work is owned
# and closed, rather than only a category describing it.
ISSUE_REF = re.compile(r"#\d+")

# The subset of the backlog where the consequence of being unscoped is a write.
# Called out separately in the summary because "43 unscoped paths" and "21 of
# them mutate rows addressed by a bare UUID" are different facts, and a flat
# count let the second hide inside the first.
DESTRUCTIVE_CATEGORIES = frozenset({"id-addressed-write"})

# Categories whose entries must name a tracked issue. NOT the same set as
# ``DESTRUCTIVE_CATEGORIES``, and the difference is the point: "does this
# mutate" and "does this need an owner" are different questions, and answering
# the second with the first left ``grant-in-lieu-of-tenant`` unowned.
#
# That category is a live cross-tenant READ — the caller supplies the tenant
# list and storage authenticates nothing, so it names its own scope. Nothing is
# destroyed, so it does not belong under a name that means "mutates"; it is
# every bit as much a hole that needs someone accountable for closing it, so it
# belongs here. ``id-addressed-read`` is deliberately absent: at 22 entries it
# is the bulk backlog to grind down, not a set of individually-owned holes, and
# requiring an issue per entry would make 22 issues nobody reads.
TRACKED_CATEGORIES = DESTRUCTIVE_CATEGORIES | frozenset({"grant-in-lieu-of-tenant"})

# ---------------------------------------------------------------------------
# The categories an exception can claim.
#
# WHY A CLOSED SET RATHER THAN FREE PROSE PER ENTRY. There are ~150 exceptions
# on the day this lands. Asking for a bespoke sentence on each produces ~150
# sentences nobody reads, and ``legacy_name_ratchet.py`` already learned this
# the expensive way: it carries two markers instead of one specifically because
# "a reviewer facing eleven undifferentiated entries stops reading". At 150 the
# argument is not close. Six categories get argued over once, properly, and then
# each entry's claim is a single word a reviewer can check against the code.
#
# These are not equally benign, and the point of separating them is that they
# are not. Four describe paths that are correct as they stand. Two describe a
# backlog: ID_ADDRESSED is the shape both advisories had, and BLIND_SPOT is this
# gate failing to see a guard that is really there. Those two are what the
# ratchet exists to drive down.
# ---------------------------------------------------------------------------
CATEGORIES: dict[str, str] = {
    "no-tenant-data": (
        "Touches no tenant-scoped table at all — liveness probes, the pg_locks "
        "snapshot, idempotency keys. There is no tenant to bind to."
    ),
    "cross-tenant-by-design": (
        "Legitimately spans tenants and would be wrong if it did not: the "
        "capability-usage flush (one batch carries many tenants' counters, "
        "migration 023), the tenant-discovery lists that drive the lifecycle "
        "fanout, and global operational counts."
    ),
    "opaque-body-write": (
        "An insert/upsert whose tenant travels as a field inside the payload "
        "dict rather than as a parameter, so no signature can carry it. The "
        "binding is real but lives in the row being written; the route above is "
        "responsible for having validated it."
    ),
    "admin-unscoped": (
        "A deliberate operator-facing path that runs across tenants, reached "
        "only through an admin-gated route upstream."
    ),
    "grant-in-lieu-of-tenant": (
        "Scoped only by a caller-supplied list of tenants — a "
        "``readable_tenant_ids`` grant, or a ``tenant_ids`` filter — with no "
        "binding tenant behind it. ALSO BACKLOG. Storage authenticates "
        "nothing and takes the list verbatim from the request body, so "
        "accepting it in place of a home tenant is strictly weaker than "
        "requiring one: the caller names its own scope. Where sibling routes "
        "DO require a binding tenant, the difference is an inconsistency to "
        "resolve rather than a design. Added as its own category because "
        "filing such a route under ``cross-tenant-by-design`` said the "
        "opposite of what its note said."
    ),
    "id-addressed-write": (
        "Addressed by a primary key the caller is assumed to already hold, with "
        "no tenant predicate, AND it mutates the row. THIS IS THE TOP OF THE "
        "BACKLOG. GHSA-wgvw-28pq-jc36 was the read form of this shape; these are "
        "the same defect where the consequence is a write, so knowing a UUID is "
        "enough to change or destroy another tenant's row, not merely to read "
        "it. Fix these before the read half. Shrink; never add."
    ),
    "id-addressed-read": (
        "Addressed by a primary key the caller is assumed to already hold, with "
        "no tenant predicate, and it only reads. THIS IS THE BACKLOG. It is the "
        "shape of GHSA-wgvw-28pq-jc36 — knowing a UUID is not the same as being "
        "entitled to the row behind it — and every entry here is one an attacker "
        "who can reach storage directly can use. Shrink; never add."
    ),
    "blind-spot": (
        "The path IS scoped, but by an idiom the AST pass cannot read — an "
        "inline ``if not isinstance(...): raise`` rather than the shared "
        "``_require`` guard. The entry records that a human checked it. Prefer "
        "rewriting the guard to use ``_require`` and deleting the entry."
    ),
}


class Entry:
    """One thing that must be tenant-scoped, and what the code actually says."""

    def __init__(self, kind: str, key: str, verdict: str, detail: str) -> None:
        self.kind = kind  # "route" | "method" | "grant"
        self.key = key
        self.verdict = verdict  # "REQUIRED" | "OPTIONAL" | "NONE"
        self.detail = detail

    @property
    def ident(self) -> str:
        return f"{self.kind}:{self.key}"


# ---------------------------------------------------------------------------
# Enumeration — the SQL layer
# ---------------------------------------------------------------------------


def _public_methods(cls: type) -> list[tuple[str, Any]]:
    """Every public callable on ``cls``, whatever kind of method it is.

    ``inspect.isfunction`` is the obvious predicate and the wrong one: a
    ``@classmethod`` reached through the class is a BOUND METHOD, so
    ``isfunction`` is False and it would be skipped — not reported, not
    allowlisted, not ratcheted, just absent. That is the one way this gate can
    fail that nobody would notice, since a method it never enumerates is a
    method it never objects to. ``isroutine`` covers functions, bound
    classmethods and staticmethods alike.
    """
    return [
        (name, fn)
        for name, fn in inspect.getmembers(cls, predicate=inspect.isroutine)
        if not name.startswith("_")
    ]


def enumerate_methods() -> list[Entry]:
    """Every public ``PostgresService`` method, classified by its signature.

    Exact, not heuristic: the question is whether a parameter with a binding
    name exists and whether it carries a default, and ``inspect.signature``
    answers both. A defaulted binding scope is reported as OPTIONAL rather than
    REQUIRED because the default is what a caller gets by forgetting the
    argument — that is the ``bulk-get`` defect at the layer below the route.
    """
    from core_storage_api.services.postgres_service import PostgresService

    entries: list[Entry] = []
    for name, fn in _public_methods(PostgresService):
        # Every public function reachable on the class, wherever it was defined.
        # An earlier version skipped anything whose ``__qualname__`` did not start
        # with ``PostgresService``, meaning to exclude inherited helpers. That is
        # the wrong direction for this gate: it also excluded anything arriving
        # from a mixin or attached at runtime, so a method could carry unscoped
        # SQL and never be enumerated — invisible, and therefore never objected
        # to. The class has no bases today, so including everything costs nothing
        # now and fails closed if one is ever added.
        params = inspect.signature(fn).parameters
        binding = [p for p in params.values() if p.name in BINDING_SCOPE]
        if not binding:
            entries.append(Entry("method", name, "NONE", "no binding tenant parameter"))
            continue
        defaulted = [
            p
            for p in binding
            if p.default is not inspect.Parameter.empty
            and SENTINEL_TYPE not in str(p.annotation)
        ]
        # ANY defaulted binding scope makes the method OPTIONAL, even when
        # another binding parameter is mandatory. That is deliberate, and it is
        # deliberately NOT what ``_classify_handler`` does for routes, where one
        # required key outvotes the optional ones. The two are reading different
        # evidence and it does not combine the same way:
        #
        #   * A route is REQUIRED because a guard RAISES on the missing key.
        #     That is positive proof no request reaches the body without it, so
        #     one such guard settles the route however many optional reads sit
        #     beside it.
        #   * A method is "mandatory" only because a parameter has no default.
        #     That proves a caller passes SOMETHING, not that the value is used
        #     as a predicate. ``settings`` and ``lifecycle_audit`` key on
        #     ``org_id`` rather than ``tenant_id``, so a signature like
        #     ``(tenant_id: str, org_id: str | None = None)`` can filter on the
        #     defaulted one and carry the mandatory one for logging. Letting the
        #     mandatory parameter outvote the defaulted one would pass that
        #     silently, and it is the bulk-get defect exactly: the caller who
        #     forgets the argument gets the unscoped query.
        #
        # So the asymmetry is the conservative direction on both sides. Zero
        # methods have this mixed shape today; the rule is here for the first
        # one that does. ``test_a_mandatory_binding_param_does_not_outvote_a
        # _defaulted_one`` pins it.
        if defaulted:
            names = ", ".join(sorted(p.name for p in defaulted))
            entries.append(
                Entry("method", name, "OPTIONAL", f"{names} is defaulted; omitting it drops the scope")
            )
        else:
            entries.append(Entry("method", name, "REQUIRED", ""))
    return sorted(entries, key=lambda e: e.key)


def _widening_grant_findings() -> list[Entry]:
    """A method taking ``readable_tenant_ids`` must also take a binding scope.

    This is the invariant that makes the widening grant safe to omit, and it is
    the one worth gating — not "the grant must be passed", which would mandate
    the dangerous direction. With a binding ``tenant_id`` present, the ``else``
    branch narrows to one tenant and forgetting the grant costs a caller some
    rows. Without one, that branch has nothing to narrow to, and forgetting the
    grant would be the unscoped read the advisory was about.

    Holds for all eleven call sites today. It is listed as its own check so that
    a twelfth arriving without a binding tenant fails loudly rather than being
    quietly counted as scoped.
    """
    from core_storage_api.services.postgres_service import PostgresService

    findings: list[Entry] = []
    for name, fn in _public_methods(PostgresService):
        params = inspect.signature(fn).parameters
        if WIDENING_GRANT not in params:
            continue
        if not any(p in BINDING_SCOPE for p in params):
            findings.append(
                Entry(
                    "grant",
                    name,
                    "NONE",
                    f"takes {WIDENING_GRANT} with no binding tenant to fall back to",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Enumeration — the routes
# ---------------------------------------------------------------------------


def enumerate_routes() -> list[Entry]:
    """Every storage operation, classified by what its handler does with the body.

    The route list comes from the live application rather than from the source,
    so a router registered in a loop or behind a conditional is still counted.
    The walk is checked against ``app.openapi()`` and raises if the two disagree
    — a FastAPI internals change then breaks the build instead of silently
    shrinking the surface this gate believes it covers, which is the failure a
    security gate must not have.
    """
    from core_storage_api.app import app

    live = _resolve_operations(app)
    classified = _classify_handlers()

    entries: list[Entry] = []
    for method, path, endpoint in live:
        key = f"{method} {path}"
        lookup = (endpoint.__module__.rsplit(".", 1)[-1], endpoint.__name__)
        verdict = classified.get(lookup)
        if verdict is None:
            # A live route whose handler is not a plain decorated function in
            # routers/ — generated, wrapped, or registered from elsewhere. It
            # cannot be read statically, so it does not get the benefit of the
            # doubt; it needs an explicit entry saying who checked it.
            entries.append(
                Entry("route", key, "NONE", f"handler {lookup[0]}.{lookup[1]} is not statically classifiable")
            )
            continue
        entries.append(Entry("route", key, verdict[0], verdict[1]))
    return sorted(entries, key=lambda e: e.key)


def _resolve_operations(app: Any) -> list[tuple[str, str, Any]]:
    """Flatten the app's route tree to ``(method, path, endpoint)``.

    FastAPI wraps an included router in a private ``_IncludedRouter`` that
    carries the mounted prefix, so the tree has to be walked rather than read
    off ``app.routes``. The self-check below is why using a private attribute is
    acceptable here: if it ever stops working, the counts stop matching and this
    raises.
    """
    from fastapi.routing import APIRoute

    def walk(routes: list[Any], prefix: str) -> list[tuple[str, str, Any]]:
        found: list[tuple[str, str, Any]] = []
        for route in routes:
            if isinstance(route, APIRoute):
                for verb in sorted(route.methods or []):
                    if verb in ("HEAD", "OPTIONS"):
                        continue
                    found.append((verb, prefix + route.path, route.endpoint))
                continue
            context = getattr(route, "include_context", None)
            original = getattr(route, "original_router", None)
            if context is not None and original is not None:
                found.extend(walk(original.routes, prefix + (context.prefix or "")))
            elif getattr(route, "routes", None):
                found.extend(walk(route.routes, prefix))
        return found

    operations = walk(app.routes, "")

    # ``{doc_id:path}`` in a route's own path is ``{doc_id}`` in the schema;
    # drop the converter before comparing so a path converter is not read as a
    # missing route.
    def normalise(path: str) -> str:
        return re.sub(r"\{([^{}:]+):[^{}]+\}", r"{\1}", path)

    walked = {(verb, normalise(path)) for verb, path, _ in operations}
    schema = app.openapi()["paths"]
    documented = {
        (verb.upper(), path)
        for path, verbs in schema.items()
        for verb in verbs
        if verb.lower() in ("get", "post", "put", "patch", "delete")
    }
    if walked != documented:
        raise RuntimeError(
            "route walk disagrees with the OpenAPI schema — this gate would be "
            "checking a smaller surface than the app actually serves.\n"
            f"  missing from the walk: {sorted(documented - walked)}\n"
            f"  seen only in the walk: {sorted(walked - documented)}\n"
            "Fix _resolve_operations before trusting a green run."
        )
    return operations


def _classify_handlers() -> dict[tuple[str, str], tuple[str, str]]:
    """Read every router module and say what each handler requires of the body.

    Keyed by ``(module, function)`` so the result can be joined onto the live
    route list — the live list is authoritative about which routes exist, this
    is authoritative about what their source says.
    """
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for source in sorted(ROUTERS_DIR.glob("*.py")):
        if source.name.startswith("_"):
            continue
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _is_route_handler(node):
                continue
            out[(source.stem, node.name)] = _classify_handler(node)
    return out


def _is_route_handler(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and isinstance(dec.func.value, ast.Name)
        and dec.func.value.id == "router"
        for dec in node.decorator_list
    )


def _classify_handler(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, str]:
    """REQUIRED / OPTIONAL / NONE for one handler, with the evidence."""
    args = node.args
    positional = args.posonlyargs + args.args
    # "Has a default expression", syntactically — NOT "is optional to FastAPI".
    # ``tenant_id: str = Query(...)`` is a REQUIRED query parameter spelled with
    # a default, and this counts it as defaulted, so such a route would be
    # reported OPTIONAL and need an allowlist line it does not deserve.
    #
    # Left that way on purpose. Reading the default expression to decide it is
    # really ``Query(...)`` and not ``Query(default=None)`` would be a fourth
    # inference layer whose only effect is to move routes toward REQUIRED, and
    # every wrong answer it gave would be a route that silently left the list.
    # Over-listing costs a line someone deletes; under-listing is the advisory.
    #
    # Measured, so the cost is known rather than assumed: the storage routers
    # have four such defaults today — one ``limit`` and three
    # ``readable_tenant_ids: ... = Query(default=None)``, which is the widening
    # grant and genuinely optional. None is a binding scope, so this costs
    # nothing at present and stays correct-but-noisy if the style spreads.
    defaulted: set[str] = set()
    for param, _default in zip(positional[len(positional) - len(args.defaults) :], args.defaults):
        defaulted.add(param.arg)
    for param, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            defaulted.add(param.arg)

    required: set[str] = set()
    optional: set[str] = set()

    # Parameters are request-derived by construction: FastAPI fills them from
    # the path or query string. Kept so a bare ``tenant_id`` in a guard test can
    # be told apart from a local that merely shares the name.
    scope_params = {p.arg for p in positional + args.kwonlyargs if p.arg in BINDING_SCOPE}

    for param in positional + args.kwonlyargs:
        if param.arg in BINDING_SCOPE:
            (optional if param.arg in defaulted else required).add(param.arg)

    # Only reads OF THE REQUEST count. Matching every dict in the function was
    # wrong in the one direction this gate cannot afford: a handler that merely
    # writes ``response["tenant_id"] = ...`` scored REQUIRED and vanished from
    # the allowlist, so a genuinely unscoped route would have been reported as
    # scoped. A wrong "not scoped" costs one line someone deletes; a wrong
    # "scoped" is invisible. Hence both restrictions below — the receiver must
    # be request-derived, and a subscript must be a read.
    request_derived = _request_derived_names(node)

    # Local names bound from a tenant key, so an inline guard that tests the
    # variable rather than the subscript is still recognised.

    def receiver_is_request(value: ast.expr) -> bool:
        return isinstance(value, ast.Name) and value.id in request_derived

    for sub in ast.walk(node):
        # ``_require(body, "tenant_id")`` — the shared fail-closed guard.
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id in FAIL_CLOSED_GUARDS
            and sub.args
            and receiver_is_request(sub.args[0])
        ):
            for arg in sub.args[1:]:
                if isinstance(arg, ast.Constant) and arg.value in BINDING_SCOPE:
                    required.add(arg.value)
        # ``body["tenant_id"]`` — absent raises, so the scope is required.
        # ``ast.Load`` only: ``payload["tenant_id"] = x`` is a write, and says
        # nothing about what the request had to supply.
        if (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.ctx, ast.Load)
            and isinstance(sub.slice, ast.Constant)
            and sub.slice.value in BINDING_SCOPE
            and receiver_is_request(sub.value)
        ):
            required.add(sub.slice.value)
        # ``body.get("tenant_id")`` — absent yields None, so it is omittable
        # unless a guard below rejects that None. ``pop`` too: ``update_memory``
        # reads the scope out with ``body.pop("tenant_id", None)`` so the key
        # does not then reach the column update, and that is the same read.
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in DICT_READS
            and receiver_is_request(sub.func.value)
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and sub.args[0].value in BINDING_SCOPE
        ):
            optional.add(sub.args[0].value)
    # Every binding of every name, in source order, with the scope key it reads
    # or None. Not a flat name->key map: a name can be bound more than once in a
    # function, and Python has no block scope, so a map keyed only by name lets
    # a LATER ``value = body.get("tenant_id")`` retroactively credit an EARLIER
    # ``if not value: raise`` that was checking something else entirely. Keeping
    # the line numbers lets each guard resolve against the binding actually in
    # effect where it stands.
    bindings: list[tuple[int, str, str | None]] = []
    for target, bound in _assignments(node):
        if isinstance(target, ast.Name):
            bindings.append((target.lineno, target.id, _tenant_key_read(bound, request_derived)))
    bindings.sort()

    # An inline ``if <tenant is missing/wrong>: raise`` is the same fail-closed
    # contract as ``_require``, written out. Several routes predate the shared
    # helper and validate this way; reading only ``_require`` would report two
    # dozen correctly-scoped routes as exceptions, and an allowlist mostly made
    # of this gate's own blind spots is one nobody can read for the entries that
    # matter. Recognising the idiom is not a softening — it is the difference
    # between measuring the code and measuring the classifier.
    for sub in ast.walk(node):
        if not isinstance(sub, ast.If):
            continue
        # The raise must be a statement of the branch itself, not merely
        # somewhere beneath it. ``ast.walk`` also finds one nested under a
        # second condition — ``if tenant_id: if something_else: raise`` — where
        # a missing tenant does NOT reach the raise, and crediting that as a
        # guard hides a genuinely unscoped route. Unconditional-given-the-test
        # is the weakest claim that is still a claim.
        if not any(isinstance(stmt, ast.Raise) for stmt in sub.body):
            continue
        in_effect = _bindings_in_effect(bindings, sub.lineno)
        for key in _keys_rejected_when_missing(sub.test, in_effect, scope_params, request_derived):
            required.add(key)
            optional.discard(key)

    if required:
        return "REQUIRED", ""
    if optional:
        names = ", ".join(sorted(optional))
        return "OPTIONAL", f"reads {names} with .get() and never rejects a missing one"
    return "NONE", "no binding tenant read from the request"


def _bindings_in_effect(
    bindings: list[tuple[int, str, str | None]], line: int
) -> dict[str, str]:
    """The scope key each name holds at ``line``, last write before it wins.

    A name bound to a tenant read LATER in the function says nothing about what
    it held at an earlier guard, and Python has no block scope to make that
    obvious. Resolving positionally is what stops
    ``if not value: raise`` … ``value = body.get("tenant_id")`` from reading as
    a tenant guard — a false REQUIRED, which is the invisible direction.

    Ignores branching: a name bound in one arm of an ``if`` is treated as bound
    from that line on. That errs toward crediting, so it is not a substitute for
    the checks above — but the alternative is a dataflow analysis, and every
    real handler here binds its scope once, straight-line, before using it.
    """
    in_effect: dict[str, str] = {}
    for lineno, name, key in bindings:
        if lineno >= line:
            break
        if key is None:
            in_effect.pop(name, None)  # rebound to something that is not a scope
        else:
            in_effect[name] = key
    return in_effect


def _keys_rejected_when_missing(
    test: ast.expr,
    aliases: dict[str, str],
    scope_params: set[str],
    request_derived: set[str],
) -> set[str]:
    """Scope keys whose ABSENCE makes ``test`` true — i.e. reaches the raise.

    The question a guard has to answer is not "does the tenant appear in this
    condition" but "does a request without one end up at the raise". Those come
    apart on polarity: ``if not tenant_id: raise`` is a guard, and
    ``if tenant_id and flag: raise`` is the opposite — it fires only when the
    tenant IS present, and crediting it marks an unscoped route REQUIRED.

    Compoundness is not the discriminator, though, and rejecting every
    ``BoolOp`` would be wrong here: ``if not isinstance(tenant_id, str) or not
    tenant_id: raise`` is the dominant real idiom in these routers, at eight
    sites. Polarity handles both, because the two operators differ in exactly
    the way that matters — ``or`` is true if any branch is, so one negative
    check on the tenant suffices; ``and`` needs all of them, so a positive
    tenant operand makes the whole test false precisely when the tenant is
    gone. Hence: recurse through ``or``, refuse ``and``.
    """
    if isinstance(test, ast.BoolOp):
        parts = [_keys_rejected_when_missing(v, aliases, scope_params, request_derived) for v in test.values]
        if isinstance(test.op, ast.Or):
            # True if any branch is, so one negative check on the tenant carries
            # the whole test.
            return set().union(*parts) if parts else set()
        # ``and`` needs every branch. It is still a guard when EVERY branch is
        # itself a negative check on a BINDING scope key — ``if not tenant_id
        # and not org_id: raise`` means "at least one binding scope must be
        # present", which is fail-closed against having none. It is NOT a guard
        # when any branch is something else, because that branch can be false
        # while the tenant is missing and the raise is never reached.
        #
        # ``readable_tenant_ids`` is NOT such a key — it is the widening grant,
        # deliberately outside BINDING_SCOPE — so ``if not tenant_id and not
        # readable_tenant_ids: raise`` yields an empty set for its second
        # branch, fails ``all(parts)``, and is NOT credited. That is the
        # intended reading and not an oversight: the grant is supplied verbatim
        # by an unauthenticated caller, so "a grant was passed" is strictly
        # weaker than "a tenant was bound", and treating the pair as
        # interchangeable here would let the weaker one satisfy the gate. The
        # live consequence is POST /memories/quality-metrics, which guards in
        # exactly this shape and is recorded OPTIONAL under
        # ``grant-in-lieu-of-tenant`` rather than passing as REQUIRED.
        return set().union(*parts) if parts and all(parts) else set()

    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = test.operand
        if isinstance(inner, ast.BoolOp):
            # Push the negation inward instead of looking through it. Handing
            # ``not (tenant_id or other_flag)`` straight to the mention scan
            # credits it, but that test is true only when BOTH are falsy — a
            # request with no tenant and a truthy flag sails past the raise. De
            # Morgan turns it into the ``and`` case, which already refuses a
            # branch that is not itself a negative scope check.
            flipped = ast.BoolOp(
                op=ast.And() if isinstance(inner.op, ast.Or) else ast.Or(),
                values=[ast.UnaryOp(op=ast.Not(), operand=v) for v in inner.values],
            )
            return _keys_rejected_when_missing(flipped, aliases, scope_params, request_derived)
        # ``not tenant_id``, ``not isinstance(tenant_id, str)``, ``not body.get(...)``
        return _scope_keys_mentioned(inner, aliases, scope_params, request_derived)

    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        operator, right = test.ops[0], test.comparators[0]
        # ``tenant_id is None`` / ``== None`` / ``== ""`` — true when absent.
        if isinstance(operator, (ast.Is, ast.Eq)) and _is_falsy_literal(right):
            return _scope_keys_mentioned(test.left, aliases, scope_params, request_derived)
        # ``node.tenant_id != body.get("tenant_id")`` — the pair check that
        # closed GHSA-xw4x-jwf5-8m9h. Absent reads as None, which differs from
        # the row's real tenant, so the raise is reached. Only when the other
        # side is not itself falsy, or the comparison would be None != None.
        if isinstance(operator, (ast.NotEq, ast.IsNot)) and not (
            _is_falsy_literal(right) or _is_falsy_literal(test.left)
        ):
            return _scope_keys_mentioned(
                test.left, aliases, scope_params, request_derived
            ) | _scope_keys_mentioned(right, aliases, scope_params, request_derived)
        return set()

    # A bare ``if tenant_id:`` raises when the tenant is PRESENT. Not a guard.
    return set()


def _is_falsy_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and (node.value is None or node.value == "")


def _scope_keys_mentioned(
    node: ast.expr,
    aliases: dict[str, str],
    scope_params: set[str],
    request_derived: set[str],
) -> set[str]:
    """Scope keys this expression refers to, by a route the request can reach.

    A name counts only when it is a handler parameter (FastAPI fills those from
    the path or query) or an alias bound from the request body. A local that
    merely happens to be spelled ``tenant_id`` — assigned from a config default,
    say — refers to nothing the caller sent, and taking it as evidence marks an
    unscoped route REQUIRED.

    A literal counts only as the key of a read off a request-derived object, via
    ``_tenant_key_read``. Matching the bare string anywhere in the subtree meant
    ``if not config["tenant_id"]: raise`` proved the ROUTE was scoped, when it
    proves something about a config file. The main classification loop already
    anchors its reads this way; this is the same rule inside a guard test.
    """
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            if sub.id in aliases:
                found.add(aliases[sub.id])
            elif sub.id in scope_params:
                found.add(sub.id)
            continue
        key = _tenant_key_read(sub, request_derived) if isinstance(sub, ast.expr) else None
        if key:
            found.add(key)
    return found


def _assignments(node: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    """Every single-target binding in ``node``, as ``(target, value)``.

    Both statement forms, because the routers overwhelmingly use the annotated
    one: ``body: dict = await request.json()`` is an ``AnnAssign``, not an
    ``Assign``, and reading only ``Assign`` sees 8 of the 107 body bindings in
    this tree. Missing them made every handler that reads its tenant off an
    annotated ``body`` look unscoped.
    """
    out: list[tuple[ast.expr, ast.expr]] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
            out.append((sub.targets[0], sub.value))
        elif isinstance(sub, ast.AnnAssign) and sub.value is not None:
            out.append((sub.target, sub.value))
    return out


def _request_derived_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Local names holding the parsed request body, or something taken from it.

    Seeded from ``body = await request.json()`` and grown transitively, because
    a handler may validate a nested object rather than the body itself —
    ``event = body.get("event")`` then ``_require(event, "tenant_id")`` is a
    real idiom here, and the tenant it demands is still one the caller had to
    send. Iterated to a fixed point so the order of assignments does not matter.
    """
    derived: set[str] = set()
    assignments: list[tuple[str, ast.expr]] = []
    for sub in _assignments(node):
        target, bound = sub
        if not isinstance(target, ast.Name):
            continue
        value = bound
        if isinstance(value, ast.Await):
            value = value.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "json"
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "request"
        ):
            derived.add(target.id)
        else:
            assignments.append((target.id, bound))

    changed = True
    while changed:
        changed = False
        for name, value in assignments:
            if name in derived:
                continue
            base = None
            if isinstance(value, ast.Subscript):
                base = value.value
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr in DICT_READS
            ):
                base = value.func.value
            if isinstance(base, ast.Name) and base.id in derived:
                derived.add(name)
                changed = True
    return derived


def _tenant_key_read(value: ast.expr, request_derived: set[str]) -> str | None:
    """The tenant key an expression reads off the request, if it reads one.

    Covers ``body.get("tenant_id")`` and ``body["tenant_id"]`` — the two ways a
    handler pulls a scope out of a parsed body before validating it. Anchored to
    ``request_derived`` for the same reason as the caller: a tenant key read off
    some other dict is not a claim about what the request supplied.
    """
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr in DICT_READS
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id in request_derived
        and value.args
        and isinstance(value.args[0], ast.Constant)
        and value.args[0].value in BINDING_SCOPE
    ):
        return str(value.args[0].value)
    if (
        isinstance(value, ast.Subscript)
        and isinstance(value.value, ast.Name)
        and value.value.id in request_derived
        and isinstance(value.slice, ast.Constant)
        and value.slice.value in BINDING_SCOPE
    ):
        return str(value.slice.value)
    return None


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def exceptions(entries: list[Entry]) -> list[Entry]:
    return [e for e in entries if e.verdict != "REQUIRED"]


def allowlisted_categories(entries: list[Entry], allowlist: dict[str, dict[str, str]]) -> list[str]:
    """The category each live exception currently claims, for the summary line."""
    return [(allowlist.get(e.ident, {}).get("category") or "unclassified") for e in entries]


class AllowlistError(Exception):
    """The allowlist file cannot be read as a set of exceptions."""


def _index_by_id(rows: list[dict[str, str]], source: str) -> dict[str, dict[str, str]]:
    """Key the exceptions by id, refusing to let two rows share one.

    A dict comprehension keeps whichever duplicate comes last, which is a
    silent, order-dependent choice between two rows a reviewer believes are
    both present. It also bypasses the relabel guard in ``ratchet``: append a
    second copy of a mutating entry carrying a milder category and the lookup
    returns the milder one, so the row still reads as unchanged while the
    summary's MUTATES count falls.

    ``--write`` cannot produce a duplicate — it renders from live entries keyed
    by ident — so reaching this means a hand-edit or a merge that concatenated
    two versions of the array, which is exactly when a quiet choice is worst.
    """
    by_id: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    for row in rows:
        ident = row["id"]
        if ident in by_id:
            duplicates.append(ident)
        by_id[ident] = row
    if duplicates:
        raise AllowlistError(
            f"{source} lists {', '.join(sorted(set(duplicates)))} more than once. "
            "Two rows for one path means the gate silently picks whichever comes "
            "last — delete the duplicate and keep the row a reviewer agreed to."
        )
    return by_id


def load_allowlist(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return _index_by_id(raw["exceptions"], path.name)


def check_category_doc(path: Path) -> list[str]:
    """The file's own ``_categories`` block must match ``CATEGORIES`` exactly.

    ``--write`` generates that block from ``CATEGORIES``, but nothing read it
    back, so the two could drift: adding a category to the script and hand-
    editing one entry to claim it left the file documenting six categories
    while an entry used a seventh. ``check`` never saw it, because
    ``load_allowlist`` keeps only ``exceptions``.

    That is this gate's own failure mode — a checked-in artefact drifting from
    the source it is generated from is the thing the rest of the file exists to
    catch — so it is checked here rather than left to a reader to notice.
    Equality, not "every used category is present": it also catches a
    description edited in the JSON instead of the script, and a category
    dropped from the script but still documented. Any drift is repaired the
    same way, by regenerating.
    """
    if not path.exists():
        return []
    documented = json.loads(path.read_text()).get("_categories", {})
    if documented == CATEGORIES:
        return []
    missing = sorted(set(CATEGORIES) - set(documented))
    extra = sorted(set(documented) - set(CATEGORIES))
    reworded = sorted(k for k in set(documented) & set(CATEGORIES) if documented[k] != CATEGORIES[k])
    drift = ", ".join(
        part
        for part in (
            f"missing {', '.join(missing)}" if missing else "",
            f"unknown {', '.join(extra)}" if extra else "",
            f"reworded {', '.join(reworded)}" if reworded else "",
        )
        if part
    )
    return [
        f"{path.name} documents categories that do not match the script ({drift}). "
        "The '_categories' block is generated from CATEGORIES in "
        "scripts/tenant_scope_gate.py; re-run "
        "`python3 scripts/tenant_scope_gate.py --write` so the file explains "
        "every category its own entries claim."
    ]


def render_allowlist(entries: list[Entry], previous: dict[str, dict[str, str]]) -> str:
    """Serialize the allowlist, carrying every category that was already claimed.

    Regeneration must never silently drop a category: the category is the
    reviewed artefact, and the enumeration around it is the disposable part.
    """
    payload = {
        "_comment": (
            "Storage paths that take no binding tenant scope. Generated by "
            "scripts/tenant_scope_gate.py --write; the 'category' and 'note' fields "
            "are written by hand and preserved across regeneration. This list may "
            "shrink and may not grow. Delete an entry once its path takes a tenant "
            "scope. Category meanings are defined in the script — 'id-addressed-write' "
            "and 'id-addressed-read' are the backlog, not an approval, and the write "
            "half is the one to clear first."
        ),
        "_categories": CATEGORIES,
        "exceptions": [_row(e, previous.get(e.ident, {})) for e in sorted(exceptions(entries), key=lambda e: e.ident)],
    }
    return json.dumps(payload, indent=2) + "\n"


def _row(entry: Entry, prior: dict[str, str]) -> dict[str, str]:
    """One allowlist row. ``note`` is omitted when empty rather than emitted blank.

    Most entries are fully described by their category; carrying an empty string
    on all of them adds a line each to a file people have to read, and makes the
    rows that DO carry a note harder to spot rather than easier.
    """
    row = {
        "id": entry.ident,
        "verdict": entry.verdict,
        "evidence": entry.detail,
        "category": prior.get("category", ""),
    }
    note = (prior.get("note") or "").strip()
    if note:
        row["note"] = note
    return row


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check(entries: list[Entry], grants: list[Entry], allowlist: dict[str, dict[str, str]]) -> list[str]:
    errors: list[str] = []
    current = {e.ident: e for e in exceptions(entries)}

    unlisted = sorted(set(current) - set(allowlist))
    for ident in unlisted:
        errors.append(
            f"{ident} takes no binding tenant scope and is not in the allowlist "
            f"({current[ident].detail}).\n"
            "    Give it a tenant scope, or account for it: run "
            "`python3 scripts/tenant_scope_gate.py --write`, then set the new entry's "
            f"'category' to one of: {', '.join(CATEGORIES)}."
        )

    stale = sorted(set(allowlist) - set(current))
    for ident in stale:
        errors.append(
            f"{ident} is in the allowlist but no longer needs to be — it is either "
            "gone or now scoped. Delete the entry (--write does it for you)."
        )

    for ident in sorted(set(current) & set(allowlist)):
        recorded = (allowlist[ident].get("verdict") or "").strip()
        if recorded and recorded != current[ident].verdict:
            # The id alone is too coarse to hold an exception in place. A path
            # already on the list can weaken without leaving it — OPTIONAL means
            # a caller that passes the scope still gets scoped, NONE means there
            # is no way to scope it at all — and the identifier is identical
            # either way. Comparing the recorded verdict to the live one makes
            # that visible; ``_weakened`` below covers the case where the author
            # re-ran ``--write`` and the record moved with the code.
            errors.append(
                f"{ident} was recorded as {recorded} and is now {current[ident].verdict} "
                f"({current[ident].detail}). Re-run "
                "`python3 scripts/tenant_scope_gate.py --write` and have a reviewer "
                "confirm the new evidence before landing it."
            )
        category = (allowlist[ident].get("category") or "").strip()
        if not category:
            errors.append(
                f"{ident} is allowlisted with no category. An exception nobody can "
                f"classify is one nobody reviewed; pick one of: {', '.join(CATEGORIES)}."
            )
        elif category in TRACKED_CATEGORIES and not ISSUE_REF.search(
            allowlist[ident].get("note") or ""
        ):
            # A category says what the debt is; the issue is where paying it
            # gets argued and closed, and a note that loses its reference turns
            # a tracked item back into a line in a JSON file nobody is
            # accountable for.
            errors.append(
                f"{ident} is {category} with no tracked issue in its note. Every "
                f"entry in {', '.join(sorted(TRACKED_CATEGORIES))} carries a "
                "`Tracked in #N` reference — file one and add it, or fix the path "
                "and delete the entry."
            )
        elif category not in CATEGORIES:
            errors.append(
                f"{ident} claims unknown category {category!r}. Valid categories are "
                f"{', '.join(CATEGORIES)}. Add a new one to CATEGORIES in "
                "scripts/tenant_scope_gate.py only with a reviewer's agreement — the "
                "closed set is what keeps the list readable."
            )

    for grant in grants:
        errors.append(
            f"{grant.key} {grant.detail}. A widening grant is only safe to omit "
            "when a binding tenant scope is there to narrow to; add one."
        )

    return errors


def _ref_exists(ref: str) -> bool:
    """Whether ``ref`` resolves to a commit in this checkout."""
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        ).returncode
        == 0
    )


def _path_in_ref(ref: str, relative: str) -> bool:
    """Whether ``relative`` exists in ``ref``'s tree, by exit code."""
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{ref}:{relative}"],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        ).returncode
        == 0
    )


def ratchet(base: str, allowlist_path: Path, current: dict[str, Entry]) -> list[str]:
    """Fail if the allowlist grew, or if anything on it lost scope, against ``base``.

    Compared against the base tree rather than a stored count, for the reason
    ``legacy_name_ratchet.py`` gives: a stored number goes stale, churns on every
    move, and has to be regenerated by the person the gate is pointed at. On a
    ``pull_request`` the checkout is already the merge commit, so the comparison
    is exactly this PR's contribution.
    """
    # Two failures look identical to ``git show`` and must not be treated
    # alike. "The base has no allowlist yet" is the commit that introduces the
    # file and is fine. "The base does not resolve" — the ``git fetch`` step
    # before this one failed, or ``--base`` was mistyped — means the comparison
    # never happened, and returning "nothing grew" from that is how the only
    # mechanism holding the list flat gets skipped on a green build.
    if not _ref_exists(base):
        return [
            (
                f"the ratchet base {base!r} does not resolve, so the allowlist was never "
                "compared against anything.\n"
                "    This is not 'nothing grew' — it is 'nobody looked'. Check that the\n"
                "    `git fetch --no-tags --depth=1 origin main` step before this one ran."
            )
        ]

    relative = allowlist_path.relative_to(REPO_ROOT).as_posix()

    # ``git cat-file -e`` answers "is this path in that tree" with an exit
    # code. The alternative is reading ``git show``'s stderr for "does not
    # exist", which is English, and which git is free to reword — the same
    # unstable-signal problem ``_ref_exists`` above exists to avoid, so it
    # would be odd to reintroduce it three lines later. Asking a question that
    # has a numeric answer removes the ambiguity rather than pinning ``LC_ALL``
    # to work around it.
    if not _path_in_ref(base, relative):
        # This is the commit that introduces the allowlist.
        return []

    shown = subprocess.run(
        ["git", "show", f"{base}:{relative}"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if shown.returncode != 0:
        # The ref resolves and the path is in it, so a failure here is
        # something else entirely — a corrupt object, an unreadable store — and
        # must not read as "nothing grew".
        return [
            (
                f"could not read {relative} at {base}: {shown.stderr.strip()}\n"
                "    The allowlist was not compared against anything."
            )
        ]
    blob = shown.stdout

    # Same duplicate refusal as the working copy. The base is normally safe by
    # induction — this check runs on every PR, so a duplicate cannot reach main
    # once it is in place — but that induction has no base case for the commits
    # already on main, and a duplicate there would silently pick the baseline
    # row every comparison below is made against.
    baseline = _index_by_id(json.loads(blob)["exceptions"], f"{relative} at {base}")
    errors: list[str] = []

    added = sorted(set(current) - set(baseline))
    if added:
        errors.append(
            "the tenant-scope allowlist grew against " + base + ":\n"
            + "".join(f"      + {ident}\n" for ident in added)
            + "    A new storage path without a binding tenant scope is the shape both\n"
            "    advisories had. Scope it rather than listing it. If it genuinely\n"
            "    cannot be scoped, that is a reviewer's call, not this gate's — say so\n"
            "    in the PR and a maintainer can override."
        )

    # Counting rows is not enough on its own. An author who weakens a path
    # already on the list and then re-runs ``--write`` moves the record along
    # with the code: the id set is unchanged, so a size comparison sees nothing,
    # and ``check`` is satisfied because the file now agrees with the tree. The
    # base tree is the only copy that still remembers what the path used to be.
    weakened = []
    for ident in sorted(set(current) & set(baseline)):
        was = str(baseline[ident].get("verdict") or "")
        now = current[ident].verdict
        # A baseline row with no recorded verdict, or one this version does not
        # recognise, supports no comparison — say nothing rather than guess a
        # direction. Guessing "strongest" here reported every such row as a
        # regression; guessing "weakest" would have hidden real ones.
        if was not in VERDICT_STRENGTH or now not in VERDICT_STRENGTH:
            continue
        if VERDICT_STRENGTH[now] < VERDICT_STRENGTH[was]:
            weakened.append(f"{ident}: {was} -> {now}")
    if weakened:
        errors.append(
            "an allowlisted path lost tenant scope against " + base + ":\n"
            + "".join(f"      ~ {line}\n" for line in weakened)
            + "    Staying on the list is not permission to get worse: OPTIONAL still\n"
            "    scopes a caller that passes the tenant, NONE cannot be scoped at all."
        )

    # Splitting the id-addressed backlog by blast radius created a way to make
    # the number fall without fixing anything: relabel a mutating path as a read
    # one. The row stays, the verdict is unchanged, and the summary's MUTATES
    # count drops — a forcing function nobody has to satisfy. Leaving the
    # destructive category is therefore only legitimate when the path leaves the
    # allowlist entirely, which the id comparison above already permits.
    listed_now = load_allowlist(allowlist_path)
    relabelled = []
    for ident in sorted(set(current) & set(baseline)):
        was = str(baseline[ident].get("category") or "")
        now = str(listed_now.get(ident, {}).get("category") or "")
        if was in DESTRUCTIVE_CATEGORIES and now and now not in DESTRUCTIVE_CATEGORIES:
            relabelled.append(f"{ident}: {was} -> {now}")
    if relabelled:
        errors.append(
            "an unscoped mutating path was relabelled as something milder against " + base + ":\n"
            + "".join(f"      ~ {line}\n" for line in relabelled)
            + "    A path leaves the mutating backlog by getting a tenant scope and\n"
            "    leaving the allowlist, not by being refiled. If the original\n"
            "    classification was simply wrong, say so in the PR — that is a\n"
            "    reviewer's call, not a regeneration's."
        )

    if not errors:
        removed = sorted(set(baseline) - set(current))
        if removed:
            print(f"Allowlist shrank by {len(removed)}: {', '.join(removed)}")
    return errors




def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="git ref to ratchet against, e.g. origin/main")
    parser.add_argument("--write", action="store_true", help="reseed the allowlist")
    args = parser.parse_args()

    try:
        methods = enumerate_methods()
        routes = enumerate_routes()
        grants = _widening_grant_findings()
    except ImportError as exc:
        print(f"error: cannot import core-storage-api ({exc}).", file=sys.stderr)
        print("Install it first: uv pip install -e core-storage-api/[dev]", file=sys.stderr)
        return 2

    entries = methods + routes
    try:
        previous = load_allowlist(ALLOWLIST_PATH)
    except AllowlistError as exc:
        # A malformed allowlist is the author's to fix, not a crash to read a
        # traceback out of — and it must not reach the ratchet, which would
        # otherwise compare against whichever duplicate won.
        print(f"::error::{_as_annotation(str(exc))}" if _in_github_actions() else f"error: {exc}")
        return 1

    if args.write:
        ALLOWLIST_PATH.write_text(render_allowlist(entries, previous))
        listed = len(exceptions(entries))
        print(f"wrote {ALLOWLIST_PATH.relative_to(REPO_ROOT)} — {listed} exceptions")
        missing = [
            e.ident
            for e in exceptions(entries)
            if not (previous.get(e.ident, {}).get("category") or "").strip()
        ]
        if missing:
            print(f"{len(missing)} entries still need a 'category':")
            for ident in missing:
                print(f"  {ident}")
        return 0

    errors = check(entries, grants, previous)
    errors.extend(check_category_doc(ALLOWLIST_PATH))
    if args.base:
        try:
            errors.extend(ratchet(args.base, ALLOWLIST_PATH, {e.ident: e for e in exceptions(entries)}))
        except AllowlistError as exc:
            # A duplicate in the BASE copy. The comparison cannot be trusted, so
            # this is an error rather than a silent "nothing grew" — the same
            # reasoning as an unresolvable base ref.
            errors.append(str(exc))

    scoped = sum(1 for e in entries if e.verdict == "REQUIRED")
    print(
        f"tenant-scope gate: {len(routes)} routes + {len(methods)} service methods, "
        f"{scoped} bound to a tenant, {len(exceptions(entries))} allowlisted."
    )
    counts: dict[str, int] = {}
    for entry in allowlisted_categories(exceptions(entries), previous):
        counts[entry] = counts.get(entry, 0) + 1
    for category in sorted(counts):
        if category in DESTRUCTIVE_CATEGORIES:
            marker = "  <- backlog, MUTATES"
        elif category in BACKLOG_CATEGORIES:
            marker = "  <- backlog"
        else:
            marker = ""
        print(f"  {counts[category]:>3}  {category}{marker}")
    if errors:
        print()
        for err in errors:
            print(f"::error::{_as_annotation(err)}" if _in_github_actions() else f"error: {err}")
        return 1
    return 0


def _as_annotation(message: str) -> str:
    """Percent-encode a message so a workflow command carries all of it.

    ``::error::`` is line-based: a raw newline ends the command, so only the
    first line becomes the annotation and the rest falls out into plain log
    text. Every multi-line error here puts the diagnosis on line one and the
    REMEDY on the continuation lines — "run ``--write``, then set the new
    entry's category", "check that the git fetch step ran" — so the truncation
    drops exactly the half that tells someone what to do.

    ``%`` goes first: encoding it after the newlines would rewrite the ``%``
    of a ``%0A`` this function had just produced, and the annotation would
    render the escape rather than the line break.
    """
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _in_github_actions() -> bool:
    import os

    return bool(os.environ.get("GITHUB_ACTIONS"))


if __name__ == "__main__":
    sys.exit(main())
