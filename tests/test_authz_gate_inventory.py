"""Every mutating REST route is gated, or on a list that says who decided not to.

SCOPE, stated first because the obvious reading is wider than the truth: this
covers ``APIRoute`` entries on the core-api app. It does NOT cover the MCP
surface. ``app.py`` does ``app.mount("/mcp", ...)``, which the walk below sees
as a ``Mount`` with no ``.routes`` and a plain Starlette ``Route`` — neither an
``APIRoute`` — and ``/mcp`` is absent from ``app.openapi()``, so the
self-check cannot see it either. Every mutating MCP tool (``caura_write``,
``caura_manage``, ``caura_keystones_set``) guards itself with
``_check_write_scope`` instead; that surface wants its own inventory and does
not have one. A plain Starlette ``Route`` added with a mutating method would be
invisible here for the same reason.

This class of gap has been fixed one route at a time, repeatedly:

    2026-06-11  S2                 command_result, redistribute, STM writes
    2026-08-14  H-12 / H-13 / H-15 agents/trust, fleet/commands, settings
    (later)     H-04               fleet/heartbeat
    (later)     M-25               DELETE /fleet/{fleet_id}
    (later)     #1335              POST /fleet/commands, the PLANE gate
    (later)     #1337              PATCH /agents/{id}/tune, the WRITE gate

Every one is the same shape: a mutating route that skipped a gate its own
neighbours already applied. ``POST /fleet/commands`` is the case that argues
for this file — the 2026-08-14 pass opened that handler, added two gates, and
walked away without the third, so the SECOND visit to one function is what
found it. Manual sweeps have not converged, and prose in a route docstring is
not readable by anything.

So: enumerate the surface and make the omissions explicit. Two invariants, one
mechanism.

WHAT THIS IS NOT. It does not decide whether a gate is the RIGHT one, and it
cannot: ``skills_inbox`` guards with a module-local ``_require_inbox_admin``
that raises 403 on its own, and no static check can prove that equivalent to
``enforce_read_only``. So the classifier reports FACTS — which ``enforce_*``
calls a handler makes, and which module-local guard helpers it calls — and the
allowlists carry the JUDGMENTS, one line each, with a name attached. Same split
as ``core-storage-api/tenant_scope_allowlist.json``.

WHY A TEST AND NOT A SCRIPT. ``scripts/tenant_scope_gate.py`` needs its own CI
step and degrades to a report when the environment cannot import the app. This
needs neither: it runs in the suite that already imports ``core_api.app``, so
it cannot silently stop running.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap

from core_api.app import app

MUTATING_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Gates strong enough to make each invariant moot — and the two sets are NOT
# the same, which is worth spelling out because collapsing them is the obvious
# mistake.
#
# ``enforce_admin`` admits only the super-admin key, and ``auth.py`` builds
# that context as ``AuthContext(tenant_id=None, is_admin=True)`` — no
# capabilities, no demo flag — so the one caller that clears it is a caller
# ``enforce_read_only`` would also pass. Note it is NOT that
# ``enforce_read_only`` short-circuits on ``is_admin``: it does not, it tests
# only ``is_demo`` and ``capabilities``. The exemption rests on how the admin
# context is constructed, not on a bypass inside the gate.
WRITE_GATE_EXEMPT = frozenset({"enforce_admin"})

# ``enforce_org_admin`` is in NEITHER set, and the reasoning is worth keeping
# because it is not obvious in either direction.
#
# Not write-gate-exempt: ``org_role`` (``auth.py``, Path 4) and ``capabilities``
# arrive as independent gateway headers, so an org admin holding a
# capabilities={'read'} key clears ``enforce_org_admin`` while
# ``enforce_read_only`` refuses it.
#
# Not plane-exempt either, which is the part I first got wrong. The tempting
# argument is that agent credentials carry no org membership — but that
# statement lives in ``auth.py``'s PATH 4 branch comment, which says "Read on
# THIS branch only", and Path 2 (standalone) returns
# ``AuthContext(tenant_id=..., org_role="admin", agent_id=agent_id)``: an agent
# credential that IS an org admin. So the exemption would hold on the gateway
# and not in standalone. No route calls ``enforce_org_admin`` today, so leaving
# it unexempted costs nothing and the first route to use it gets looked at.
PLANE_GATE_EXEMPT = frozenset({"enforce_admin"})

WRITE_GATE = "enforce_read_only"
PLANE_GATE = "enforce_not_agent_credential"

# Routers whose mutating surface is admin-plane: operations on the fleet, on
# agent identity, on tenant settings, on the org itself. This is where
# ``enforce_not_agent_credential`` is the house rule, and where M-25 and #1335
# both landed.
#
# Deliberately NOT app-wide, and NOT because a long allowlist is inherently
# bad — ``tenant_scope_gate`` ships a 63-entry one. The reason is that an
# app-wide plane invariant would encode no rule: an agent credential writing
# memories, documents and entities IS the product, so ~50 of the ~57 mutating
# routes would carry a line saying exactly that, and a list where almost every
# entry is the same non-fact teaches a reader nothing. The plane rule is a
# property of these routers, not of mutation in general.
#
# Every router is classified, here or in the set below, and
# ``test_every_router_is_classified`` fails on a router that is neither — so a
# new router cannot become exempt by default, which is how ``keystones``
# escaped an earlier draft of this file.
ADMIN_PLANE_ROUTERS = frozenset(
    {"agents", "fleet", "settings", "org_deletion", "lifecycle", "skills_inbox"}
)

# Routers with mutating routes that are deliberately NOT admin-plane. The
# reason matters as much as the membership: this set is what stops the plane
# invariant from silently narrowing, so each line has to say what plane the
# router is on instead.
NON_ADMIN_PLANE_ROUTERS: dict[str, str] = {
    "memories": "agent-plane: an agent writing its own memories is the product",
    "stm": "agent-plane: short-term notes are written by agents",
    "entities": "agent-plane: derived from agent-written memories",
    "documents": "agent-plane: caura_doc is an agent-facing surface",
    "evolve": "agent-plane: agents report their own outcomes",
    "insights": "agent-plane: generated for the calling agent",
    "reports": "agent-plane for the digest trigger; the admin run is enforce_admin",
    "crystallizer": "agent-plane trigger; the all-tenants sweep is enforce_admin",
    "interview": "agent-plane submit; the scheduler run is enforce_admin",
    "plugin": "bootstrap: unauthenticated install-script rendering, no auth context",
    # TRUST-plane, not admin-plane, and the distinction is the whole design.
    # ``_enforce_author_trust`` lets an agent author its OWN keystone at
    # trust >= 1 and anything wider at trust >= 2, so refusing agent
    # credentials outright would remove the intended capability rather than
    # close a hole. Named explicitly because an earlier draft omitted it from
    # both sets and nothing failed.
    "keystones": "trust-plane: agents author their own rules, gated by require_trust",
}

# Routers that exist ONLY under an explicit env flag, and so are not part of
# the served surface either invariant is about. ``testing`` is registered from
# ``app.py`` inside ``if os.getenv("TESTING") == "1"``, which is exactly the
# condition this suite runs under — set by ``tests/conftest.py`` before any
# test module imports the app, NOT by CI's env block — so these routes are visible HERE and absent
# in production, and the inventory would otherwise be environment-dependent.
#
# Excluded by RULE rather than by allowlist line, because a per-route list
# would churn every time a test endpoint is added, for no reading value. The
# rule is not taken on trust: ``test_test_only_routes_check_their_flag`` below
# requires every one of these routes to ALSO call the flag check in its own
# body, so the exclusion is earned per route rather than assumed per router.
TEST_ONLY_ROUTERS = frozenset({"testing"})
TEST_ONLY_FLAG_GUARD = "_require_testing_mode"

# ---------------------------------------------------------------------------
# The allowlists. One line per route, and the reason is the point of the line.
# A stale entry FAILS (see test_allowlists_have_no_stale_entries) so these
# cannot quietly outlive the routes they excuse.
# ---------------------------------------------------------------------------

WRITE_GATE_ALLOWLIST: dict[str, str] = {
    # POST-shaped READS. The verb is POST because the query travels in a body,
    # not because anything is written; each is gated by
    # ``enforce_readable_tenant``, which is the read-side scope check.
    "POST /api/v1/search": "read: POST-bodied query, gated by enforce_readable_tenant",
    "POST /api/v1/recall": "read: POST-bodied query, gated by enforce_readable_tenant",
    "POST /api/v1/documents/query": "read: POST-bodied query, gated by enforce_readable_tenant",
    "POST /api/v1/documents/search": "read: POST-bodied query, gated by enforce_readable_tenant",
    "POST /api/v1/skills/installable": "read: POST-bodied query, gated by enforce_readable_tenant",
    # Unauthenticated bootstrap. These take no ``auth`` parameter at all — they
    # render an install script for a machine that does not have a credential
    # yet, which is the whole point. Nothing tenant-scoped is written.
    "POST /api/v1/install-plugin": "bootstrap: no auth context by design, renders a script",
    "POST /api/install-plugin": "bootstrap: no auth context by design, renders a script",
    # KNOWN GAP, not a justification — see test_known_gaps_do_not_grow.
    #
    # These five call ``_require_inbox_admin``, which raises 403 unless
    # ``is_admin`` or ``org_role == "admin"``. The first draft of this file
    # called that "strictly narrower than the write gate". It is not, for the
    # same reason ``enforce_org_admin`` is excluded from WRITE_GATE_EXEMPT
    # above: ``org_role`` and ``capabilities`` are independent gateway headers,
    # so an ORG ADMIN holding a capabilities={'read'} key clears
    # ``_require_inbox_admin`` and then meets no write gate — and can approve,
    # reject, quarantine, defer or edit a skill. Writing the reason down is what
    # exposed it; it is recorded here rather than fixed because five behaviour
    # changes each want their own over-refusal test, which is a security PR of
    # its own and not this one.
    "POST /api/v1/skills-inbox/{slug:path}/approve": "KNOWN GAP: _require_inbox_admin admits a read-only org admin",
    "POST /api/v1/skills-inbox/{slug:path}/reject": "KNOWN GAP: _require_inbox_admin admits a read-only org admin",
    "POST /api/v1/skills-inbox/{slug:path}/quarantine": "KNOWN GAP: _require_inbox_admin admits a read-only org admin",
    "POST /api/v1/skills-inbox/{slug:path}/defer": "KNOWN GAP: _require_inbox_admin admits a read-only org admin",
    "POST /api/v1/skills-inbox/{slug:path}/edit": "KNOWN GAP: _require_inbox_admin admits a read-only org admin",
}

# A reason starting with this is NOT a justification — it is an unfixed gap
# recorded in the open, and the count below may only shrink. Borrowed from
# ``tenant_scope_gate``, which carries a ``blind-spot ... <- backlog`` category
# for the same purpose: the alternative is a list where "excused" and "not yet
# fixed" look identical, and then neither gets read.
KNOWN_GAP_PREFIX = "KNOWN GAP:"
KNOWN_GAP_CEILING = 5

PLANE_GATE_ALLOWLIST: dict[str, str] = {
    # NODE-plane, not admin-plane. The plugin holds whatever credential the
    # install was given, and both of these are how a node stays live and
    # reports back (``plugin/src/heartbeat.ts`` posts them). Refusing an
    # agent-scoped credential here would take fleets offline, which is worse
    # than the gap it would close. Note the over-refusal guards in
    # ``tests/test_route_authz_gaps.py`` cover read-only and demo credentials
    # on these routes, NOT agent-scoped ones — so this line is a judgement
    # about the deployment, not a restatement of something already tested.
    "POST /api/v1/fleet/heartbeat": "node-plane: the plugin's own check-in",
    "POST /api/v1/fleet/commands/{command_id}/result": "node-plane: the plugin's own ack",
    # SELF-plane. An agent tuning its OWN search profile is the documented
    # behaviour behind MCP ``caura_tune``, and this route is what the plugin's
    # tool PATCHes. The route enforces the narrower rule itself: an agent
    # credential may write only ``agent_id == auth.agent_id``.
    "PATCH /api/v1/agents/{agent_id}/tune": "self-plane: agent may tune only itself",
    # DELIBERATE, and the one entry here that is a judgement rather than a
    # category. ``POST /fleet`` creates a fleet by inserting a sentinel node
    # row — but a heartbeat naming a fresh ``fleet_id`` creates one implicitly,
    # and the heartbeat must stay open to agent credentials (see above). Gating
    # only the explicit route would move no capability, so it is left open on
    # purpose rather than for lack of noticing. Raised in #1335.
    "POST /api/v1/fleet": "implicitly reachable via heartbeat, which is node-plane",
    # ``_require_inbox_admin`` refuses an agent credential ON THE GATEWAY PATH,
    # where the auth service emits no ``X-Org-Role`` for an agent key, so
    # ``org_role`` is None and the helper raises. NOT in standalone: Path 2
    # builds ``org_role="admin"`` alongside ``agent_id``, so there an agent
    # credential clears it. That is the same deployment caveat the plane gate
    # itself carries — standalone shares one key and ``X-Agent-ID`` is
    # caller-supplied, so there is no agent/tenant boundary there to protect.
    "POST /api/v1/skills-inbox/{slug:path}/approve": "admin-only via _require_inbox_admin",
    "POST /api/v1/skills-inbox/{slug:path}/reject": "admin-only via _require_inbox_admin",
    "POST /api/v1/skills-inbox/{slug:path}/quarantine": "admin-only via _require_inbox_admin",
    "POST /api/v1/skills-inbox/{slug:path}/defer": "admin-only via _require_inbox_admin",
    "POST /api/v1/skills-inbox/{slug:path}/edit": "admin-only via _require_inbox_admin",
}


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------


def _normalise(path: str) -> str:
    """``{doc_id:path}`` in a route is ``{doc_id}`` in the schema."""
    return re.sub(r"\{([^{}:]+):[^{}]+\}", r"{\1}", path)


def _resolve_operations() -> list[tuple[str, str, object]]:
    """Flatten the live app to ``(verb, path, endpoint)``.

    The walk has to be the source, not ``app.openapi()``: the schema carries no
    endpoint function, and this check is about what the handler does. FastAPI
    0.137 mounts ``include_router(prefix=...)`` as an opaque ``_IncludedRouter``
    (``path=None``, no public ``.routes``), so the tree is walked through
    ``include_context``/``original_router`` — the same private pair
    ``scripts/tenant_scope_gate._resolve_operations`` uses, for the same reason.

    The self-check below is what makes leaning on a private attribute safe, and
    it is ONE-DIRECTIONAL on purpose. Every DOCUMENTED operation must appear in
    the walk: a walk that silently stops recursing would otherwise leave this
    file checking a handful of routes and passing, which is the exact failure
    ``app.py`` records against the 0.137 upgrade. The reverse is legitimate —
    core-api registers routes with ``include_in_schema=False`` (the permanent
    legacy keystones alias, a trailing-slash ``/skills-inbox/``) that the
    walk sees and the schema does not, and those are still real surface that
    SHOULD be checked. (``tenant_scope_gate`` compares both directions because
    it walks ``core_storage_api.app``, which has no hidden routes; copying that
    check here fails on the aliases.)
    """
    from fastapi.routing import APIRoute

    def walk(routes, prefix: str) -> list[tuple[str, str, object]]:
        found: list[tuple[str, str, object]] = []
        for route in routes:
            if isinstance(route, APIRoute):
                for verb in sorted(route.methods or []):
                    if verb not in ("HEAD", "OPTIONS"):
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
    walked = {(verb, _normalise(path)) for verb, path, _ in operations}
    documented = {
        (verb.upper(), path)
        for path, verbs in app.openapi()["paths"].items()
        for verb in verbs
        if verb.lower() in ("get", "post", "put", "patch", "delete")
    }
    undiscovered = documented - walked
    assert not undiscovered, (
        "the route walk missed operations the app documents, so this gate would "
        "be checking a smaller surface than is served:\n  "
        + "\n  ".join(f"{v} {p}" for v, p in sorted(undiscovered))
        + "\nFix _resolve_operations before trusting a green run."
    )
    return operations


# ---------------------------------------------------------------------------
# Handler classification
# ---------------------------------------------------------------------------


def _parse(fn) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return None


def _refuses(tree: ast.AST) -> bool:
    """Does this function raise an ``HTTPException`` with a 401/403 status?"""
    return any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and any(
            kw.arg == "status_code"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value in (401, 403)
            for kw in node.exc.keywords
        )
        for node in ast.walk(tree)
    )


def _classify(fn, _depth: int = 0) -> tuple[frozenset[str], frozenset[str]]:
    """``(enforce_* names, module-local guard helpers)`` reachable from ``fn``.

    Two levels of module-local indirection are resolved. What that buys is
    narrower than it first appears, and worth stating precisely: all five
    ``skills_inbox`` actions guard through ``_require_inbox_admin(auth)`` and
    call no ``enforce_*`` at all, so ``gates`` stays empty either way and their
    offender status is unchanged by the resolution — the allowlist is what
    excuses them. What the resolution actually produces is the ``helpers`` set,
    and that is what ``test_allowlist_reasons_that_name_a_mechanism_are_corroborated``
    checks a reason against. Without it, a line reading "admin-only via
    ``_require_inbox_admin``" would be unfalsifiable prose.

    A helper is recorded when it calls an ``enforce_*`` itself or raises
    401/403 directly. Note what that does NOT prove: ``_refuses`` accepts any
    401/403, so a feature-flag check (``_require_skills_factory_enabled``) and
    a "you must name a tenant" check (``_require_tenant``) both land in
    ``helpers`` alongside real authz. Hence "refusing helpers" in the report,
    not "guards".

    Two known blind spots, both measured rather than assumed:

    - IMPORTED helpers are skipped (the ``__module__`` filter below), so
      ``enforce_delete``, ``enforce_fleet_write``, ``enforce_fleet_read_many``,
      ``resolve_caller_and_gate`` and ``update_memory`` are not followed. All
      14 routes reaching them also call ``enforce_read_only`` directly, so
      nothing is missed today — and the limitation fails SAFE: a route whose
      only guard is imported reports no gates and fails the invariant.
    - ``ast.walk`` ignores control flow and ordering, so a gate inside an
      ``if``, after an early ``return``, or in a nested ``def`` all count.
      No route relies on that for either invariant gate today (three
      ``memories`` routes do it for ``enforce_usage_limits``). Ordering is
      asserted behaviourally in ``tests/test_route_authz_gaps.py`` instead,
      which is where a "gate ran after the write" mutant is caught.
    """
    tree = _parse(fn)
    if tree is None:
        return frozenset(), frozenset()
    gates: set[str] = set()
    helpers: set[str] = set()
    module = inspect.getmodule(fn)
    module_globals = getattr(module, "__dict__", {})
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr.startswith(
            "enforce_"
        ):
            gates.add(node.func.attr)
        elif isinstance(node.func, ast.Name) and _depth < 2:
            if node.func.id == fn.__name__:
                continue  # decorator returns fn unchanged; do not self-credit
            target = module_globals.get(node.func.id)
            if not inspect.isfunction(target):
                continue
            if getattr(target, "__module__", None) != fn.__module__:
                continue  # only module-local helpers; imports are someone else's surface
            sub_tree = _parse(target)
            if sub_tree is None:
                continue
            sub_gates, sub_helpers = _classify(target, _depth + 1)
            if sub_gates or _refuses(sub_tree):
                helpers.add(node.func.id)
                gates |= sub_gates
                helpers |= sub_helpers
    return frozenset(gates), frozenset(helpers)


def _mutating_routes(include_test_only: bool = False) -> list[dict]:
    rows = []
    for verb, path, endpoint in _resolve_operations():
        if verb not in MUTATING_VERBS:
            continue
        router = endpoint.__module__.rsplit(".", 1)[-1]
        if router in TEST_ONLY_ROUTERS and not include_test_only:
            continue
        gates, helpers = _classify(endpoint)
        rows.append(
            {
                "key": f"{verb} {path}",
                "router": router,
                "handler": endpoint.__name__,
                "gates": gates,
                "helpers": helpers,
            }
        )
    return rows


def _report(row: dict) -> str:
    return (
        f"    {row['key']}\n"
        f"        handler: {row['router']}.{row['handler']}\n"
        f"        enforce_* called: {sorted(row['gates']) or 'NONE'}\n"
        f"        refusing helpers: {sorted(row['helpers']) or 'none'}"
    )


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------


def test_every_mutating_route_has_a_write_gate() -> None:
    """A route that mutates must refuse a credential that may not write.

    ``enforce_read_only`` is the gate: demo sandbox, and any credential minted
    with capabilities that omit ``write``. This is the invariant #1337 restored
    on ``PATCH /agents/{id}/tune``, which was the only mutating route in
    ``routes/agents.py`` without it.
    """
    offenders = [
        row
        for row in _mutating_routes()
        if WRITE_GATE not in row["gates"]
        and not (row["gates"] & WRITE_GATE_EXEMPT)
        and row["key"] not in WRITE_GATE_ALLOWLIST
    ]
    assert not offenders, (
        f"{len(offenders)} mutating route(s) neither call {WRITE_GATE}, nor are "
        f"admin-only, nor are listed in WRITE_GATE_ALLOWLIST:\n"
        + "\n".join(_report(r) for r in offenders)
        + f"\n\nAdd the gate, or add a line to WRITE_GATE_ALLOWLIST in {__file__} "
        "saying why this route is not a write."
    )


def test_admin_plane_mutating_routes_refuse_agent_credentials() -> None:
    """On the admin plane, an agent-scoped credential must not be the caller.

    The trust ladder is self-defeating otherwise: an agent that cannot raise
    its own ``trust_level`` must not be able to reach the same outcome by
    another route. M-25 (``DELETE /fleet/{fleet_id}``) and #1335
    (``POST /fleet/commands``) were both this, found by hand, a month apart.
    """
    offenders = [
        row
        for row in _mutating_routes()
        if row["router"] in ADMIN_PLANE_ROUTERS
        and PLANE_GATE not in row["gates"]
        and not (row["gates"] & PLANE_GATE_EXEMPT)
        and row["key"] not in PLANE_GATE_ALLOWLIST
    ]
    assert not offenders, (
        f"{len(offenders)} admin-plane mutating route(s) neither call "
        f"{PLANE_GATE}, nor are admin-only, nor are listed in "
        f"PLANE_GATE_ALLOWLIST:\n"
        + "\n".join(_report(r) for r in offenders)
        + f"\n\nAdd the gate, or add a line to PLANE_GATE_ALLOWLIST in {__file__} "
        "saying which plane this route belongs to."
    )


def test_allowlists_have_no_stale_entries() -> None:
    """An entry naming a route that no longer exists has to fail.

    Without this the lists rot into a place where a real gap can hide behind a
    line that once meant something — the failure mode
    ``test_api_read_scope_params`` guards its own opt-out table against.
    """
    live = {row["key"] for row in _mutating_routes()}
    stale = {
        name: sorted(set(allowlist) - live)
        for name, allowlist in (
            ("WRITE_GATE_ALLOWLIST", WRITE_GATE_ALLOWLIST),
            ("PLANE_GATE_ALLOWLIST", PLANE_GATE_ALLOWLIST),
        )
        if set(allowlist) - live
    }
    assert not stale, (
        "allowlist entries name routes that are no longer mutating routes on "
        f"this app — renamed, removed, or their verb changed:\n{stale}\n"
        "Delete the entries; do not update them to match a route you have not "
        "re-examined."
    )


def test_test_only_routes_check_their_flag() -> None:
    """The rule that excludes ``TEST_ONLY_ROUTERS`` has to be earned per route.

    These routes are skipped by both invariants above on the grounds that they
    do not exist in production. That is true of the ROUTER — ``app.py``
    registers it inside ``if os.getenv("TESTING") == "1"`` — and this suite
    runs with that flag set, so the exclusion cannot be observed from here in
    the negative. What CAN be checked is the second lock: each handler calling
    the flag guard itself, so a route that ever escaped the conditional
    registration would still refuse.

    A test endpoint added without it fails here rather than slipping silently
    into an exempt router.
    """
    ungated = [
        row
        for row in _mutating_routes(include_test_only=True)
        if row["router"] in TEST_ONLY_ROUTERS
        and TEST_ONLY_FLAG_GUARD not in row["helpers"]
    ]
    assert not ungated, (
        f"test-only mutating route(s) do not call {TEST_ONLY_FLAG_GUARD}, so "
        "they rely entirely on conditional registration:\n"
        + "\n".join(_report(r) for r in ungated)
    )


def test_the_test_only_exclusion_is_not_silently_empty() -> None:
    """Guards the guard above: the exclusion must actually be excluding.

    If ``TESTING`` stopped being set for this suite, or the router were
    renamed, ``TEST_ONLY_ROUTERS`` would match nothing — and
    ``test_test_only_routes_check_their_flag`` would pass vacuously over an
    empty list while the two invariants quietly stopped skipping anything. Both
    failure modes are silent, so assert the set is non-empty on purpose.
    """
    test_only = [
        row
        for row in _mutating_routes(include_test_only=True)
        if row["router"] in TEST_ONLY_ROUTERS
    ]
    assert test_only, (
        "TEST_ONLY_ROUTERS matched no mutating route. Either TESTING=1 is no "
        f"longer set for this suite (so {sorted(TEST_ONLY_ROUTERS)} never "
        "registers and the exclusion is dead code), or the router was renamed "
        "and the exclusion now silently covers nothing."
    )


def test_allowlist_reasons_that_name_a_mechanism_are_corroborated() -> None:
    """A reason that names code must be checkable, and is checked here.

    Most of these entries carry a category ("read", "node-plane"), which is a
    judgement and cannot be verified statically. But several name the actual
    mechanism they rely on — ``_require_inbox_admin``,
    ``enforce_readable_tenant`` — and a named mechanism is a claim about code,
    not a category. Unverified, those lines decay exactly the way a stale
    comment does: the helper gets renamed or the call removed, and the line
    still reads as though someone had checked.

    This also closes a real hole. Drop the helper-indirection resolution from
    ``_classify`` and the two invariants above still pass, because the routes
    whose classification changed are the ones the allowlists excuse BY KEY — so
    the allowlist masks a blinded classifier. Requiring corroboration means a
    classifier that stops seeing ``_require_inbox_admin`` fails here.
    """
    rows = {row["key"]: row for row in _mutating_routes()}
    broken: dict[str, str] = {}
    for name, allowlist in (
        ("WRITE_GATE_ALLOWLIST", WRITE_GATE_ALLOWLIST),
        ("PLANE_GATE_ALLOWLIST", PLANE_GATE_ALLOWLIST),
    ):
        for key, reason in allowlist.items():
            row = rows.get(key)
            if row is None:
                continue  # staleness is test_allowlists_have_no_stale_entries' job
            observed = row["gates"] | row["helpers"]
            for claimed in re.findall(r"\b(_\w+|enforce_\w+)\b", reason):
                if claimed not in observed:
                    broken[f"{name}[{key}]"] = (
                        f"reason names {claimed!r}, which the handler does not "
                        f"call; observed {sorted(observed) or 'nothing'}"
                    )
    assert not broken, (
        "allowlist reasons name mechanisms the handler does not use:\n"
        + "\n".join(f"    {k}: {v}" for k, v in sorted(broken.items()))
        + "\nEither the reason is stale, or _classify stopped seeing the call."
    )


def test_known_gaps_do_not_grow() -> None:
    """A ratchet, so ``KNOWN GAP`` cannot become a place to put things.

    Numeric ceiling rather than a fixed set, matching
    ``test_c33_openapi_completeness``: the point is a number that only goes
    down. Fixing a gap means deleting its line and lowering the ceiling; a new
    route must not borrow the headroom a fix created.
    """
    gaps = sorted(
        f"{key}  ({reason})"
        for allowlist in (WRITE_GATE_ALLOWLIST, PLANE_GATE_ALLOWLIST)
        for key, reason in allowlist.items()
        if reason.startswith(KNOWN_GAP_PREFIX)
    )
    assert len(gaps) <= KNOWN_GAP_CEILING, (
        f"{len(gaps)} known gaps, ceiling is {KNOWN_GAP_CEILING}:\n    "
        + "\n    ".join(gaps)
        + "\n\nAdd the gate rather than raising the ceiling. The ceiling exists "
        "to be lowered."
    )
    assert len(gaps) == KNOWN_GAP_CEILING, (
        f"{len(gaps)} known gaps but the ceiling is still {KNOWN_GAP_CEILING} — "
        "a gap was fixed without lowering it, so the ratchet has slack a new "
        f"gap could take up silently. Set KNOWN_GAP_CEILING = {len(gaps)}."
    )


def test_every_router_is_classified() -> None:
    """No router may be exempt from the plane invariant by default.

    ``ADMIN_PLANE_ROUTERS`` is the scope of the plane check, so a router that
    is in neither it nor ``NON_ADMIN_PLANE_ROUTERS`` is not "out of scope" —
    it is unexamined, and indistinguishable from a decision. That is not
    hypothetical: ``keystones`` has four mutating routes and no plane gate, and
    it sat outside both sets in an earlier draft with nothing failing. It turns
    out to be correct (trust-plane), which is exactly why the omission was easy
    to miss — a wrong classification announces itself, a missing one does not.
    """
    live = {row["router"] for row in _mutating_routes(include_test_only=True)}
    classified = ADMIN_PLANE_ROUTERS | set(NON_ADMIN_PLANE_ROUTERS) | TEST_ONLY_ROUTERS
    unclassified = sorted(live - classified)
    assert not unclassified, (
        f"router(s) with mutating routes are in no classification set: "
        f"{unclassified}\nAdd each to ADMIN_PLANE_ROUTERS, or to "
        "NON_ADMIN_PLANE_ROUTERS with the plane it is actually on."
    )
    overlap = sorted(ADMIN_PLANE_ROUTERS & set(NON_ADMIN_PLANE_ROUTERS))
    assert not overlap, f"router(s) classified both admin-plane and not: {overlap}"


def test_the_plane_routers_all_exist() -> None:
    """``ADMIN_PLANE_ROUTERS`` names modules; a typo would silently narrow it.

    A misspelled router name matches nothing, and the plane invariant would
    then pass by checking fewer routes than it claims to.
    """
    live_routers = {row["router"] for row in _mutating_routes()}
    missing = sorted(ADMIN_PLANE_ROUTERS - live_routers)
    assert not missing, (
        f"ADMIN_PLANE_ROUTERS names modules with no mutating routes: {missing}. "
        f"Live routers with mutating routes: {sorted(live_routers)}"
    )
