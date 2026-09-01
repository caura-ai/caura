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
false positives gets switched off. What it checks instead are four things that
are exactly decidable:

1. **Completeness.** Every route and every public service method is enumerated
   from the code itself. Nothing is skipped because someone forgot to list it.
2. **Justification.** Anything that does not take a binding tenant scope must
   appear in the allowlist with a reason someone wrote and a reviewer read.
3. **Direction.** The allowlist may shrink. It may not grow. A new unscoped
   path fails the build (``--base``).
4. **Immutability of what the predicate filters on.** A statement bound to
   ``WHERE tenant_id = :tenant`` is only as good as the caller's inability to
   rewrite ``tenant_id`` — or the primary key — in the same request. Any method
   building an ``UPDATE ... SET`` from a caller-controlled dict must filter the
   keys through a named set, and that set must exclude the model's identity,
   scope and database-maintained columns.

Checks 1–3 were the whole gate for its first month, and three primary-key
rewrites got through them: #1081, #1118 and #1121. Two of the three were
correctly tenant-bound, so nothing in 1–3 had any objection to make — the
statement satisfied its predicate and then moved the row out from under it.
Check 4 is that gap, and it is why "bound to a tenant" is reported separately
from "cannot be unbound by the same request".

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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "core-storage-api" / "tenant_scope_allowlist.json"
ROUTERS_DIR = REPO_ROOT / "core-storage-api" / "src" / "core_storage_api" / "routers"

INSTALL_HINT = 'uv pip install -e "core-storage-api/[dev]"'

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

# --- Identity-column writability: the write-side half of the same invariant ---
#
# A tenant predicate is worth exactly as much as the immutability of the column
# it filters on. Three methods proved that in a week, and two of the three were
# ALREADY passing the binding-scope checks above:
#
#   entity_update      (#1081/#1119)  ``if hasattr(entity, key): setattr(...)``
#   memory_update      (#1118/#1122)  ``if hasattr(Memory, key)`` in a comprehension
#   fleet_upsert_node  (#1121/#1129)  ``if k not in ("tenant_id", "node_name")``
#
# Each built an ``UPDATE ... SET`` from a caller-controlled dict, and each
# admitted ``id``. The statement became ``SET id = <caller's choice> WHERE
# id = :id AND tenant_id = :tenant``: predicate satisfied, primary key moved.
# ``memory_update`` and ``fleet_upsert_node`` were correctly tenant-bound the
# whole time, so nothing above had anything to say — the defect lives one layer
# below where this gate was looking.
#
# ``IDENTITY_WRITE_GUARDS`` pairs each such method with the model it writes and
# the constant that filters the caller's keys. ``admits`` records the polarity:
# True for ``key in CONST`` (the constant lists what may be written), False for
# ``key not in CONST`` (it lists what may not).
IDENTITY_WRITE_GUARDS: dict[str, tuple[str, str, bool]] = {
    "entity_update": ("Entity", "_ENTITY_UPDATABLE_FIELDS", True),
    "memory_update": ("Memory", "_MEMORY_UPDATABLE_FIELDS", True),
    "fleet_upsert_node": ("FleetNode", "_FLEET_NODE_IMMUTABLE_FIELDS", False),
}

# Column types a caller may never write because the database maintains them.
# ``search_vector`` is the live case: its trigger fires ``BEFORE INSERT OR
# UPDATE OF content, title``, so a patch naming only the vector does not fire it
# and the caller's value persists — the row leaves keyword recall with no
# content change to explain it (#1122). Matched on type rather than name, so a
# second tsvector column is covered the day it is added.
DERIVED_COLUMN_TYPES = frozenset({"TSVECTOR"})

# What a ``.values(**…)`` chain has to hang off before it can move an existing
# row. ``pg_insert(...).values(**data)`` is deliberately absent: naming the id of
# a row you are creating collides rather than overwrites. Enumerated rather than
# prefix-matched, for the reason ``FAIL_CLOSED_GUARDS`` gives — a set that
# quietly grows to fit a new name is a set that stops meaning anything.
UPDATE_STATEMENT_ROOTS = frozenset({"sql_update", "update"})

# The statements that create rather than move a row. Named so that a chain
# resolving to neither set is treated as an update: an unrecognised builder is
# an unknown, and this check's stated bias is to report an unknown rather than
# assume it is the harmless one.
INSERT_STATEMENT_ROOTS = frozenset({"pg_insert", "insert"})

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
# argument is not close. Eight categories get argued over once, properly, and
# then each entry's claim is a single word a reviewer can check against the code.
#
# These are not equally benign, and the point of separating them is that they
# are not. Four describe paths that are correct as they stand. Four are a
# backlog, and ``BACKLOG_CATEGORIES`` above is the authority on which: the
# ``id-addressed-*`` pair is the shape both advisories had, BLIND_SPOT is this
# gate failing to see a guard that is really there, and GRANT_IN_LIEU is a
# caller naming its own scope. Those four are what the ratchet drives down.
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
        "binding is real but lives in the row being written, which settles "
        "which tenant OWNS the new row. WHAT IT DOES NOT SETTLE is whether "
        "that tenant is the right one. Since #1066 the only credential this "
        "service accepts is a shared secret carrying no tenant identity: it "
        "authenticates THAT a caller is internal, never WHICH tenant it "
        "speaks for. A foreign ``tenant_id`` in the body is written "
        "faithfully and nothing here can tell. The accepted control is that "
        "the caller was correct — say that plainly rather than deferring to "
        "an unnamed route. (This replaces ``the route above is responsible "
        "for having validated it``, which named no route and, for a caller "
        "reaching this service directly, referred to nothing.) A write that "
        "ALSO references other rows by bare id raises a separate question "
        "this category does not answer: that id needs its own tenant check, "
        "as ``POST /fleet/commands`` and ``POST /memories/conflicts`` do."
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
        "entitled to the row behind it — and every entry here is one that any "
        "caller reaching this service can use against any tenant. Until #1066 "
        "that meant anyone who could reach the port; it now means any holder of "
        "the shared secret. That is a narrower set, not a smaller consequence: "
        "the secret says nothing about which tenant its holder speaks for, so "
        "the reach of a single entry is unchanged. Shrink; never add."
    ),
    "blind-spot": (
        "The path IS scoped, but by an idiom the AST pass cannot read — an "
        "inline ``if not isinstance(...): raise`` rather than the shared "
        "``_require`` guard. The entry records that a human checked it. Prefer "
        "rewriting the guard to use ``_require`` and deleting the entry. "
        "THIS IS A BACKLOG CATEGORY, and filing here counts against the "
        "backlog exactly as the ``id-addressed-*`` pair does — it is not a "
        "tidier home for a path that is merely awkward to read. In "
        "particular, a ``_require`` this pass misses because it sits one "
        "frame down in a HELPER does not belong here: the pass reads each "
        "handler's own AST, so hoisting that call into the handler scores the "
        "route REQUIRED and the entry deletes outright rather than moving "
        "(#1199, which did that for six ``insights/*`` routes)."
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
# Identity-column writability
# ---------------------------------------------------------------------------


def _literal_keys(node: ast.expr | None) -> bool:
    """Whether ``node`` is a collection of constant strings written in the source.

    A loop over ``("fleet_id", "trust_level", …)`` cannot reach a column the
    author did not type, so it is safe however the caller's dict is shaped —
    ``agent_add`` is the live example. A loop over ``data.items()`` can reach any
    key the caller sends, and that is the shape this check looks for.
    """
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys)
    return False


def _walk_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Walk ``node``'s subtree without descending into a nested function scope.

    A closure defined inside a method has its own locals, so an assignment to
    ``values`` in there says nothing about the ``values`` the method passes to
    ``.values(**…)``. Walking through it would let an inner name decide whether
    an outer statement is judged safe — in either direction.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _walk_scope(child)


def _enclosing_loops(fn: ast.FunctionDef | ast.AsyncFunctionDef, target: ast.AST) -> list[ast.AST]:
    """The loops that actually contain ``target``, innermost first.

    Ancestry, not line numbers. Comparing ``node.lineno >= loop.lineno`` counts
    every loop that merely STARTS earlier, so an unrelated sibling loop over
    written-out names — ``for flag in ("a", "b")`` — would vouch for a later
    ``setattr`` over ``data.items()`` and the site would go unreported. That is
    a false negative in the one direction this check exists to cover.

    The ascent stops at a function boundary, because a loop only vouches for
    names it actually binds. A nested scope rebinds them::

        for key in ("a", "b"):
            f = lambda key, value: setattr(row, key, value)
            f(caller_key, caller_value)

    Lexically the loop encloses that ``setattr``, and the key is spelled
    ``key`` — but it is the lambda's parameter, filled by the caller. Matching
    on the name alone let the loop clear a write it has nothing to do with.
    A ``def`` in that position is caught anyway, since every function is
    scanned as its own scope; a ``Lambda`` is not a ``FunctionDef`` and never
    gets that second reading, so the boundary is what covers it.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(fn):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    loops: list[ast.AST] = []
    current = parents.get(target)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            break
        if isinstance(current, (ast.For, ast.AsyncFor)):
            loops.append(current)
        current = parents.get(current)
    return loops


def _bound_names(target: ast.expr) -> set[str]:
    """The names a ``for`` target binds, including through tuple unpacking."""
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _key_is_bound_to_literal_names(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call
) -> bool:
    """Whether the key handed to ``setattr`` provably comes from written-out names.

    Provenance, not proximity. Asking whether ANY enclosing loop iterates literal
    names lets an outer loop vouch for an inner one::

        for group in ("core", "extra"):        # literal, and irrelevant
            for key, value in data[group].items():
                setattr(row, key, value)       # key comes from HERE

    so the innermost binding is the only one that answers the question. A key
    that no enclosing loop binds — a bare variable from somewhere else — is not
    provably safe either, and falls through to being reported.
    """
    if len(call.args) < 2:
        return False
    key = call.args[1]
    if isinstance(key, ast.Constant):
        return True
    if not isinstance(key, ast.Name):
        return False
    for loop in _enclosing_loops(fn, call):
        target = getattr(loop, "target", None)
        if target is not None and key.id in _bound_names(target):
            return _literal_keys(getattr(loop, "iter", None))
    return False


def _local_assignments(fn: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> list[ast.expr]:
    """Every value assigned to ``name`` in ``fn``'s own scope, in source order."""
    found: list[ast.expr] = []
    for node in _walk_scope(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value
        ):
            found.append(node.value)
    return found


def _statement_roots(
    expr: ast.expr,
    fn: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
    seen: frozenset[str] = frozenset(),
) -> set[str]:
    """Every constructor a ``.values(...)`` chain could hang off.

    A set rather than one answer because a name can be assigned more than once,
    and ``stmt = sql_update(M)`` followed by ``stmt = stmt.values(**data)``
    makes the LAST assignment self-referential — resolving only that one answers
    ``stmt`` and the update disappears. Considering every assignment finds the
    ``sql_update`` that the chain actually started from.
    """
    current: ast.AST = expr
    while True:
        if isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Attribute):
            current = current.value
        elif isinstance(current, ast.Name):
            if fn is None or current.id in seen:
                return {current.id}
            origins = _local_assignments(fn, current.id)
            if not origins:
                return {current.id}
            roots: set[str] = set()
            for origin in origins:
                roots |= _statement_roots(origin, fn, seen | {current.id})
            return roots or {current.id}
        else:
            return set()


def _is_update_statement(
    expr: ast.expr, fn: ast.FunctionDef | ast.AsyncFunctionDef | None = None
) -> bool:
    """Whether a ``.values(...)`` chain hangs off something that moves an existing row.

    An INSERT is exempt for the reason given on ``_caller_keyed_update_sites``.
    Anything that resolves to neither an insert nor an update builder is treated
    as an update: it is a shape this pass does not recognise, and reporting an
    unknown costs one registry line a reviewer can delete, while assuming it is
    an insert costs a silent miss.
    """
    roots = _statement_roots(expr, fn)
    if roots & UPDATE_STATEMENT_ROOTS:
        return True
    return not (roots & INSERT_STATEMENT_ROOTS)


def _all_literal_keys(fn: ast.FunctionDef | ast.AsyncFunctionDef, expr: ast.expr | None) -> bool:
    """Whether ``expr``'s keys are written out in the source on *every* path to it.

    A bare name is resolved through ``_local_assignments``, and all of them have
    to be literal-keyed for the name to clear. Not a reaching-definitions pass:
    this deliberately ignores which assignment reaches the call, because the
    cheap alternative — take the last one — is wrong in the direction that
    matters::

        values = {k: v for k, v in patch.items()}   # caller-keyed
        await session.execute(sql_update(M).values(**values))
        values = {"status": "done"}                 # unrelated, later

    The dict that reaches the UPDATE is the first one. Answering with the last
    reports "literal" and the site disappears — a silent miss, where the failure
    mode of being conservative is one registry line a reviewer deletes. Same
    rule as ``_statement_roots``, for the same reason: any origin can sink the
    verdict. Nested scopes are excluded — see ``_walk_scope``.
    """
    if not isinstance(expr, ast.Name):
        return _literal_keys(expr)
    origins = _local_assignments(fn, expr.id)
    # A name with no local assignment is a parameter or a global: unreadable
    # here, so it does not clear.
    return bool(origins) and all(_literal_keys(origin) for origin in origins)


def _caller_keyed_update_sites(source: str) -> dict[str, str]:
    """Methods whose UPDATE keys come from a caller-controlled dict.

    Three shapes, one per defect actually found:

    * ``setattr(row, key, …)`` under a loop that is not over literal names.
    * ``set_=`` given anything but a dict written out in the source — the
      ``ON CONFLICT DO UPDATE`` half of an upsert.
    * ``.values(**X)`` where ``X`` is built by a comprehension, which is how a
      caller's dict gets filtered into an ``UPDATE ... SET``.

    INSERT is deliberately not a site. ``pg_insert(...).values(**data)`` lets a
    caller name the id of a row *they are creating*; colliding with an existing
    key raises rather than overwriting, so no existing row moves. The defect is
    an existing row's identity changing under it, which only the update paths
    above can do. That cut is what keeps ``relation_add``, ``agent_add`` and
    ``agent_activity_digest_upsert`` off this list.

    Heuristic, and biased the way the route classifier is: it would rather name
    a method that turns out to be safe — one registry line, which a reviewer can
    read and delete — than miss one that is not, which is invisible.
    """
    tree = ast.parse(source)
    sites: dict[str, str] = {}
    # Private helpers are scanned too. Skipping them left a hole big enough to
    # drive the whole defect through: move the ``setattr`` loop into
    # ``_apply_patch`` and call it from a public method, and neither one is a
    # site — the public method's own body is clean and the helper is invisible.
    # A private helper that qualifies is reported under its own name, which is
    # also where the guard belongs, since that is where the keys are applied.
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and not _key_is_bound_to_literal_names(fn, node)
            ):
                sites.setdefault(fn.name, "setattr over caller-supplied keys")
            for kw in node.keywords:
                if kw.arg == "set_" and not _all_literal_keys(fn, kw.value):
                    sites.setdefault(fn.name, "ON CONFLICT set_ from caller-supplied keys")
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "values"
                and _is_update_statement(node.func.value, fn)
            ):
                # Both ways of handing ``values()`` a whole dict. ``.values(**d)``
                # puts it in ``keywords`` with ``arg=None``; ``.values(d)`` puts
                # it in ``args``, and that form is if anything the more likely —
                # it is what you must write when a key is not a valid Python
                # identifier. Reading only ``keywords`` saw the spread and never
                # the argument, so the whole defect with two characters removed
                # went through.
                #
                # A NAMED keyword is deliberately not included: ``arg`` being a
                # real column name is the author typing it out, which is the
                # opposite of a caller-keyed dict, and is what keeps
                # ``report_update_completed`` and every other explicit SET off
                # this list.
                spread = [kw.value for kw in node.keywords if kw.arg is None]
                if any(not _all_literal_keys(fn, v) for v in [*node.args, *spread]):
                    sites.setdefault(fn.name, "UPDATE values(…) from caller-supplied keys")
    return sites


def _names_referenced(source: str) -> dict[str, set[str]]:
    """Every name each method mentions, so a registration can be checked.

    Without this the registry is a claim about code rather than a reading of it:
    ``IDENTITY_WRITE_GUARDS`` could name the right constant while the method had
    gone back to ``hasattr``, and both other halves would still pass — the
    runtime half validates the constant, not its use.

    Attribute access counts as mentioning the name: ``models.Entity`` is the
    same registration as ``Entity``. Reading only ``ast.Name`` meant a change to
    qualified imports would report every guard in the file as stale — a red gate
    fixed by reverting an import style, which is the wrong lesson. This makes
    the check weaker in the direction that cannot block a correct tree, which is
    the right way round for a question as loose as "is this name spoken here".
    """
    tree = ast.parse(source)
    out: dict[str, set[str]] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        out[fn.name] = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)
        }
    return out


def _service_tree() -> ast.Module:
    """The parsed source of the one module check 4 reads."""
    import core_storage_api.services.postgres_service as service

    return ast.parse(Path(inspect.getfile(service)).read_text())


def _scan_assumption_findings(cls: type, tree: ast.Module) -> list[str]:
    """The two things check 4's single-file, bare-name scan takes for granted.

    Checks 1-3 enumerate the class through ``inspect.getmembers`` so a method
    arriving from a mixin is never silently skipped — see ``_public_methods``.
    Check 4 parses the text of one module instead, and keys everything by bare
    function name. Both are true of the tree today and neither is guaranteed,
    and each would fail SILENTLY: a mixin's ``setattr`` loop would simply not be
    read, and two same-named functions would have one supply the verdict while
    the other supplies the names it is checked against, since ``sites`` keeps
    the first and ``_names_referenced`` the last.

    Reported rather than handled. Walking every module that defines a reachable
    method, or keying by a qualified name, are both real answers — but the first
    is speculative machinery for a case that cannot arise yet, and the second
    would push a class path into ``IDENTITY_WRITE_GUARDS``, which is written by
    hand and would go stale. Failing here puts the decision in front of whoever
    creates the situation, with a concrete second module or collision to look at.
    """
    errors: list[str] = []

    bases = [base.__name__ for base in cls.__mro__[1:] if base is not object]
    if bases:
        errors.append(
            f"{cls.__name__} now has a base class ({', '.join(bases)}), and check 4 only "
            f"reads {cls.__name__}'s own module. A caller-keyed UPDATE inherited from one "
            "would not be scanned, while checks 1-3 would still enumerate it. Scan every "
            "module that defines a reachable method, or state here why the base cannot "
            "carry one."
        )

    seen: dict[str, int] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        seen[fn.name] = seen.get(fn.name, 0) + 1
    for name, count in sorted(seen.items()):
        if count > 1:
            errors.append(
                f"the scanned module defines {count} functions named {name!r}, and check 4 "
                "keys sites, references and IDENTITY_WRITE_GUARDS by bare name. One of them "
                "would decide the verdict and the other would supply the names it is checked "
                "against. Rename one."
            )
    return errors


def _protected_columns(model: Any) -> dict[str, str]:
    """Columns on ``model`` a caller must never write, and why.

    All three reasons are read off the model, so a schema change moves them
    without anybody remembering to update this file.
    """
    protected: dict[str, str] = {}
    for column in model.__table__.primary_key.columns:
        protected[column.key] = "primary key"
    for column in model.__table__.columns:
        if column.key in BINDING_SCOPE:
            protected[column.key] = "tenant scope"
        elif type(column.type).__name__.upper() in DERIVED_COLUMN_TYPES:
            protected[column.key] = "database-maintained"
    return protected


def _identity_writability_findings() -> list[str]:
    """No registered write filter admits an identity column, and none is missing.

    Three failure modes, and they are genuinely different. The AST half answers
    "is there a filter at all", which ``entity_update`` and ``memory_update``
    failed. The reference check answers "does the method still use the filter it
    is registered for", without which the registry is a claim nobody reads
    against the code. The runtime half answers "does the filter actually exclude
    the identity columns", which ``fleet_upsert_node`` failed while carrying a
    filter that looked deliberate.

    ``_scan_assumption_findings`` guards the ground all three stand on — that
    the methods are one class in one file, addressable by bare name.
    """
    import core_storage_api.services.postgres_service as service

    from common import models

    errors: list[str] = []
    source = Path(inspect.getfile(service)).read_text()
    errors.extend(_scan_assumption_findings(service.PostgresService, ast.parse(source)))
    sites = _caller_keyed_update_sites(source)
    referenced = _names_referenced(source)

    for method in sorted(set(sites) - set(IDENTITY_WRITE_GUARDS)):
        errors.append(
            f"method:{method} builds an UPDATE from caller-supplied keys ({sites[method]}) "
            "with no registered identity guard. A tenant predicate does not protect a "
            "column the same request can rewrite: filter the keys through a frozenset that "
            "excludes the model's primary key and tenant scope, then register it in "
            "IDENTITY_WRITE_GUARDS in scripts/tenant_scope_gate.py."
        )

    for method, (model_name, const_name, admits) in sorted(IDENTITY_WRITE_GUARDS.items()):
        if method not in sites:
            errors.append(
                f"method:{method} is registered in IDENTITY_WRITE_GUARDS but no longer builds "
                "an UPDATE from caller-supplied keys. Delete the entry — a registration "
                "matching nothing reads as coverage nobody has."
            )
            continue
        if const_name not in referenced.get(method, set()):
            errors.append(
                f"method:{method} is registered as guarded by {const_name} but does not "
                "mention it. The registration is a claim about code that no longer reads "
                "that way — point the filter back at the constant, or delete the entry and "
                "let the unregistered-site check speak."
            )
            continue
        # The model half of the same claim. A constant can be genuinely used and
        # the entry still name the wrong table, and then the column check below
        # runs against columns the method never writes: registering
        # ``memory_update`` against ``Entity`` stops ``Memory.search_vector``
        # being checked at all, because Entity has no TSVECTOR to find.
        # By reference, not by reading the model off the statement — the
        # ``setattr`` shape has no statement to read, and that is the shape the
        # first of these defects had.
        if model_name not in referenced.get(method, set()):
            errors.append(
                f"method:{method} is registered against {model_name} but does not mention "
                f"{model_name}. The entry would validate {const_name} against a table this "
                "method does not write, so the columns actually at risk go unchecked."
            )
            continue
        model = getattr(models, model_name, None)
        constant = getattr(service, const_name, None)
        if model is None or constant is None:
            errors.append(
                f"method:{method} names {model_name}/{const_name}, which no longer both "
                "exist. A guard that cannot be resolved is not a guard."
            )
            continue
        columns = {c.key for c in model.__table__.columns}
        written = set(constant) if admits else columns - set(constant)
        for column, reason in sorted(_protected_columns(model).items()):
            if column in written:
                errors.append(
                    f"method:{method} may write {model_name}.{column} ({reason}) — "
                    f"{const_name} admits it. A caller who satisfies the tenant predicate can "
                    "then move the row out from under it."
                )
    return errors


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


def _category(row: dict[str, str]) -> str:
    """A printable category for a row, including a malformed unclassified one."""
    return str(row.get("category") or "").strip() or "unclassified"


def allowlisted_categories(entries: list[Entry], allowlist: dict[str, dict[str, str]]) -> list[str]:
    """The category each live exception currently claims, for the summary line."""
    return [_category(allowlist.get(e.ident, {})) for e in entries]


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


def _category_counts(rows: dict[str, dict[str, str]]) -> dict[str, int]:
    """Count allowlist rows by their hand-written category."""
    counts: dict[str, int] = {}
    for row in rows.values():
        category = _category(row)
        counts[category] = counts.get(category, 0) + 1
    return counts


def _report_allowlist_comparison(
    base: str,
    baseline: dict[str, dict[str, str]],
    listed_now: dict[str, dict[str, str]],
    live_exceptions: dict[str, Entry],
    all_entries: dict[str, Entry],
) -> None:
    """Report written allowlist movement; use live code only to explain removals."""
    baseline_ids = set(baseline)
    listed_ids = set(listed_now)
    added = sorted(listed_ids - baseline_ids)
    removed = baseline_ids - listed_ids
    fixed = sorted(
        ident
        for ident in removed
        if ident in all_entries and all_entries[ident].verdict == "REQUIRED"
    )
    gone = sorted(ident for ident in removed if ident not in all_entries)
    still_unscoped = sorted(ident for ident in removed if ident in live_exceptions)
    recategorised = [
        (ident, _category(baseline[ident]), _category(listed_now[ident]))
        for ident in sorted(baseline_ids & listed_ids)
        if _category(baseline[ident]) != _category(listed_now[ident])
    ]

    print(f"Allowlist comparison against {base}:")
    print(f"  added ({len(added)})" + (":" if added else ""))
    for ident in added:
        print(f"      + {ident} [{_category(listed_now.get(ident, {}))}]")
    print(f"  removed — fixed ({len(fixed)})" + (":" if fixed else ""))
    for ident in fixed:
        print(f"      - {ident} [{_category(baseline[ident])}]")
    print(f"  removed — gone ({len(gone)})" + (":" if gone else ""))
    for ident in gone:
        print(f"      - {ident} [{_category(baseline[ident])}]")
    print(
        f"  removed — still unscoped ({len(still_unscoped)})"
        + (":" if still_unscoped else "")
    )
    for ident in still_unscoped:
        print(f"      ! {ident} [{_category(baseline[ident])}]")
    print(f"  recategorised ({len(recategorised)})" + (":" if recategorised else ""))
    for ident, before, after in recategorised:
        print(f"      ~ {ident} [{before} -> {after}]")

    before_counts = _category_counts(baseline)
    after_counts = _category_counts(listed_now)
    print("  categories (before -> after):")
    for category in sorted(set(before_counts) | set(after_counts)):
        print(
            f"      {category}: {before_counts.get(category, 0)}"
            f" -> {after_counts.get(category, 0)}"
        )


def ratchet(
    base: str, allowlist_path: Path, all_entries: dict[str, Entry]
) -> list[str]:
    """Fail if the allowlist grew, or if anything on it lost scope, against ``base``.

    Compared against the base tree rather than a stored count, for the reason
    ``legacy_name_ratchet.py`` gives: a stored number goes stale, churns on every
    move, and has to be regenerated by the person the gate is pointed at. On a
    ``pull_request`` the checkout is already the merge commit, so the comparison
    is exactly this PR's contribution.

    ``all_entries`` is the full live enumeration, not just its exceptions. The
    required entries are what let the report distinguish a fixed identifier
    from one that disappeared.
    """
    live_exceptions = {
        ident: entry
        for ident, entry in all_entries.items()
        if entry.verdict != "REQUIRED"
    }

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
        print(
            f"Allowlist comparison against {base}: no allowlist at base; nothing to compare."
        )
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
    listed_now = load_allowlist(allowlist_path)
    baseline_ids = set(baseline)
    live_exception_ids = set(live_exceptions)
    shared = baseline_ids & live_exception_ids
    errors: list[str] = []

    added = sorted(live_exception_ids - baseline_ids)
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
    for ident in sorted(shared):
        was = str(baseline[ident].get("verdict") or "")
        now = live_exceptions[ident].verdict
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
    relabelled = []
    for ident in sorted(shared):
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

    _report_allowlist_comparison(
        base, baseline, listed_now, live_exceptions, all_entries
    )
    return errors


# ---------------------------------------------------------------------------
# Running without an installed tree
#
# The gate needed a full dev environment to say anything at all: no ``pgvector``
# and it exited 2 having printed one line. That is not a small inconvenience.
# The rebranding series published the ratchet's numbers in twelve revisions and
# this gate's in almost none, and an instrument that only runs in a full dev
# environment is an instrument that goes unread.
#
# The two halves are not equally dependent, and measuring which is which is the
# whole of this. ``_classify_handlers`` parses ``routers/*.py`` with ``ast`` and
# imports nothing: 187 handlers classified on a bare interpreter. The service
# methods, the widening grants and the identity-writability check all reach for
# ``PostgresService`` through ``inspect``, which means importing
# ``core_storage_api`` and everything under it. So does the ROUTE LIST, which
# comes from the live app on purpose — a router registered in a loop is still
# counted, and the walk is cross-checked against ``app.openapi()``.
#
# WHAT THIS DELIBERATELY DOES NOT DO. It does not reconstruct route paths from
# decorators to replace the live walk. That would be a second enumeration of
# the same surface, weaker than the one it stands in for, and its failure mode
# is a route it did not find — a false negative, the one direction this gate
# must never be wrong in. Without paths there are no ``route:<VERB> <path>``
# idents, so the degraded run cannot join the allowlist and CANNOT enforce it.
#
# Which is why this is a REPORT and not a gate, says so in those words, and
# still exits 2. A version that ran everywhere by checking less and exited 0
# would be worse than one that refuses: it would read as a pass.
# ---------------------------------------------------------------------------

# Named in the skip notice, so what is missing is a list a reader can check
# rather than "some checks". Each is a check the full run performs and this one
# cannot, with the reason it cannot.
SKIPPED_WITHOUT_IMPORTS: tuple[tuple[str, str], ...] = (
    ("service-method signatures", "inspect.signature on PostgresService"),
    ("widening-grant findings", "inspect.signature on PostgresService"),
    ("identity-writability findings", "SQLAlchemy model column types"),
    ("the live route list and its OpenAPI cross-check", "core_storage_api.app"),
    ("the allowlist comparison", "needs route idents, which need the route list"),
    ("the --base ratchet", "needs the full entry set"),
)


def report_degraded(exc: ImportError, allowlist_path: Path) -> int:
    """Print everything decidable without imports, then refuse to call it a pass.

    Ordered diagnosis-first: what could not be imported, then what DID run,
    then — last, where it is read rather than scrolled past — the itemised list
    of what did not. The skip notice is the part that keeps this honest, so it
    is not a footnote.
    """
    print("tenant-scope gate: DEGRADED RUN — a REPORT, not a gate.")
    print(f"  cannot import core-storage-api ({exc}).")
    print(f"  Install it for a full run: {INSTALL_HINT}")
    print()

    handlers = _classify_handlers()
    counts: dict[str, int] = {}
    for verdict, _ in handlers.values():
        counts[verdict] = counts.get(verdict, 0) + 1
    print(
        f"AST pass over {ROUTERS_DIR.relative_to(REPO_ROOT)}: "
        f"{len(handlers)} route handlers classified, "
        f"{counts.get('REQUIRED', 0)} requiring a binding tenant."
    )

    # Grouped by module. 33 undifferentiated lines is the wall this file's own
    # CATEGORIES comment argues against, and the module is the axis a reader
    # navigates by — it is the file they would open.
    flagged: dict[str, list[str]] = {}
    for (module, function), (verdict, detail) in sorted(handlers.items()):
        if verdict == "REQUIRED":
            continue
        flagged.setdefault(module, []).append(f"{function}  [{verdict}] {detail}")
    if flagged:
        total = sum(len(v) for v in flagged.values())
        print(
            f"  {total} handler(s) the AST pass reads as taking no binding tenant. "
            "Whether each is a known exception is exactly what this run cannot "
            "tell you — see the skip notice."
        )
        for module in sorted(flagged):
            print(f"    {module}.py")
            for line in flagged[module]:
                print(f"      {line}")
    print()

    # Two real checks that need no imports. Reported as pass/fail rather than
    # folded into the skip list, because they genuinely ran.
    errors: list[str] = []
    try:
        allowlist = load_allowlist(allowlist_path)
    except AllowlistError as exc_allow:
        errors.append(str(exc_allow))
    else:
        print(
            f"Allowlist integrity: {allowlist_path.name} parses, "
            f"{len(allowlist)} entries, no duplicate ids."
        )
    errors.extend(check_category_doc(allowlist_path))
    if not errors:
        print(f"Category documentation: {allowlist_path.name} matches CATEGORIES.")
    print()

    print("SKIPPED — these checks did not run, and this report does not cover them:")
    for what, why in SKIPPED_WITHOUT_IMPORTS:
        print(f"  - {what} ({why})")
    print()
    print(
        "--write and --base are refused in a degraded run: reseeding the "
        "allowlist or ratcheting it from a partial enumeration would delete "
        "entries whose paths were never enumerated."
    )

    if errors:
        print()
        for err in errors:
            print(f"::error::{_as_annotation(err)}" if _in_github_actions() else f"error: {err}")

    # 2, not 0, and not 1. The run was INCOMPLETE, which is what 2 has always
    # meant here, and it stays 2 whether or not the import-free checks found
    # something: nothing that reads an exit code should be able to mistake this
    # for a full green gate.
    return 2


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
        # Report what the AST pass can decide instead of exiting having printed
        # nothing — see the section header above for what that is and what it
        # is not. Still exit 2, and never reach --write or --base from here.
        return report_degraded(exc, ALLOWLIST_PATH)

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
    # Deliberately not allowlisted. This invariant has no exceptions today, and
    # one with none does not need a file to hold them: the first would be a
    # decision worth arguing in review, not a line ``--write`` adds for you.
    errors.extend(_identity_writability_findings())
    if args.base:
        try:
            errors.extend(
                ratchet(args.base, ALLOWLIST_PATH, {e.ident: e for e in entries})
            )
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
    if not args.base:
        print("Allowlist comparison: no base given; nothing to compare.")
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
