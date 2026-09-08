"""Every mutating REST route is gated, or on a list that says who decided not to.

SCOPE, stated first because the obvious reading is wider than the truth: this
covers ``APIRoute`` entries on the core-api app. It does NOT cover the MCP
surface. ``app.py`` does ``app.mount("/mcp", ...)``, which the walk below sees
as a ``Mount`` with no ``.routes`` and a plain Starlette ``Route`` — neither an
``APIRoute`` — and ``/mcp`` is absent from ``app.openapi()``, so the
self-check cannot see it either. Mutating MCP tools guard themselves with
``_check_write_scope`` instead — seven of them, not the three an earlier
revision of this paragraph named: ``caura_doc``, ``caura_evolve``,
``caura_insights``, ``caura_keystones_set``, ``caura_manage``, ``caura_tune``
and ``caura_write``. That surface now has its own inventory, in
``tests/test_mcp_authz_gate_inventory.py``, which enumerates it from the tool
registry so the count above cannot silently go stale again. A plain Starlette
``Route`` added with a mutating method would still be invisible here.

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

So: enumerate the surface and make the omissions explicit. Three invariants,
one mechanism.

The third is not about mutation. ``enforce_self_agent`` (#1364/#1365) asks
whether an agent credential may act as the agent a REQUEST NAMED, and a read
can leak on that axis as readily as a write: ``GET /stm/notes?agent_id=<peer>``
and ``filter_agent_id`` on ``POST /recall`` were both disclosure, not tamper.
So that invariant is scoped by the route's INTERFACE — does it take a
caller-supplied agent identity — rather than by verb, and it reads
``route.dependant`` rather than the handler body. See ``_identity_params``.

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
import functools
import inspect
import re
import textwrap
import typing
from collections.abc import Callable
from typing import NamedTuple

from core_api.app import app
from tests._legacy_contracts import LEGACY_KEYSTONES_ROUTE

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

# The SELF plane is the third axis, and its exemptions are the widest of the
# three because both other gates settle it by construction: a route that has
# already refused agent credentials outright (``enforce_not_agent_credential``)
# or admits only the admin key (``enforce_admin``) cannot be reached by a
# caller with an agent identity, so "may it act as the agent it named" has no
# subject. Four routes rest on this and carry no allowlist line:
# ``DELETE /agents/{id}``, ``PATCH /agents/{id}/fleet``,
# ``PATCH /agents/{id}/trust``, ``GET /admin/memories``.
SELF_GATE_EXEMPT = frozenset({"enforce_admin", "enforce_not_agent_credential"})

# Module-level names a settings object is bound to, for the flag scan in
# ``_classify``. Both spellings are live and neither is the house style:
# ``memories``, ``interview`` and ``keystones`` import it as ``app_settings``,
# ``stm`` and ``health`` as plain ``settings``. A third alias would be read as
# "this handler consults no flags", so the scan is only as wide as this set.
SETTINGS_OBJECTS = frozenset({"settings", "app_settings"})

WRITE_GATE = "enforce_read_only"
PLANE_GATE = "enforce_not_agent_credential"
SELF_GATE = "enforce_self_agent"
# The precedence half of the same plane. Not a gate — it refuses nothing —
# but it settles the same question, so ``_classify`` records it alongside
# the ``enforce_*`` names and ``_unguarded_for_self`` accepts either.
SELF_BINDER = "effective_agent_id"

# Parameter names that carry a caller's ASSERTION about which agent it is. The
# set is a judgement and the membership is the whole content of the invariant,
# so the names left out are recorded below rather than merely absent.
SELF_ID_PARAMS = frozenset(
    {"agent_id", "x_agent_id", "filter_agent_id", "caller_agent_id"}
)

# Agent-shaped parameter names deliberately NOT treated as identity assertions.
# ``test_the_excluded_identity_names_are_still_live`` fails if one stops
# appearing, so a rename cannot turn an exclusion into silence.
SELF_ID_PARAMS_EXCLUDED: dict[str, str] = {
    "target_agent_id": (
        "redistribute's DESTINATION — a payload naming where rows go, not a "
        "claim about who the caller is. That route also takes ``agent_id`` and "
        "IS gated on it"
    ),
    "agents": "fleet/heartbeat's node roster: a report about agents, not a claim to be one",
    "agent": "install-skill's runtime selector (claude-code | codex | both)",
}
# ``written_by`` is the nearest miss and is deliberately not here: it is an
# AUTHOR filter, and ``GET /memories`` documents it as distinct from the
# visibility identity precisely so a caller can ask for a peer's authored rows
# without claiming that peer's identity. It contains no "agent" substring, so
# it never reaches the scan; the note is for the reader who goes looking.

# THE SUBSTITUTE RULE, and the story of why it is a call and not a shape.
#
# The first draft credited any handler containing ``auth.agent_id or <param>``
# — the precedence idiom, where the authenticated identity wins. Four routes
# were exempted by it, and one of the four should not have been.
#
# ``delete_memory`` decides authorization with ``caller_agent_id =
# auth.agent_id`` (``routes/memories.py``, plain attribute, no fallback). The
# line that matched the shape is the NEXT one, ``attribution_agent_id =
# auth.agent_id or agent_id``, whose own comment reads "Authorization must not
# trust this value; the audit log must not discard it". So the exemption was
# granted by the audit-log line. Measured consequence: rewrite the real
# principal to ``agent_id or auth.agent_id`` — the escalation — and the route
# still counted as bound, because the attribution line satisfies the shape on
# its own. A false negative in the one check that is supposed to catch exactly
# that. An ``any()`` over a function body cannot tell the deciding expression
# from a bystander.
#
# So the rule was deleted, and the fix named in its place has now been made:
# ``AuthContext.effective_agent_id(requested)`` is that expression behind a
# name, and a NAMED CALL IS DECIDABLE WHERE A SHAPE IS NOT. ``delete_memory``
# is the proof that the new rule does not over-credit — its audit line still
# spells itself out, the handler calls nothing, and it keeps the allowlist
# entry below saying it binds more strictly than precedence, not less.
#
# ``tests/test_memory_byid_authz.py`` records the 2026-09-03 ruling that this
# very expression, used as a principal, WAS the bug on that route.
#
# The claim the rule makes is narrower than "this route is safe", and worth
# stating: the handler consults the effective-identity helper. It does not
# prove the result is what the route then authorizes with. What it buys over
# the shape is that a reviewer reading a call to a method whose docstring says
# "for the visibility or authorization identity ONLY" can see a misuse, which
# a bare ``or`` gave nobody a reason to look at. The one case that would
# reinstate the false negative — pointing the helper at an audit value — is
# pinned by ``test_the_audit_attribution_is_not_bound_by_the_helper``.
#
# WHY THIS IS WEAKER THAN THE MCP SIBLING'S RULE, since a reader comparing the
# two files will ask. ``test_mcp_authz_gate_inventory._rebound_params`` credits
# a binding only when EVERY load of the parameter sits inside it, which is a
# property of the whole body rather than of one line. That is strictly
# stronger, and it does not port: measured on this surface it reports 2 of the
# 4 routes as unbound that are not. ``GET /memories`` reads the raw ``agent_id``
# again at ``author_filter`` and ``GET /memories/stats`` twice more, because
# here one parameter is deliberately BOTH the visibility identity and the
# author filter — which is the divergence this file's own preamble opens with.
# On MCP each name means one thing, so the stricter rule is free there and
# costs false positives here.
#
# ``resolve_write_agent`` is still not a substitute, though it looks like the
# obvious candidate. It enforces the BROKER ownership boundary (degrade an
# install's write that names another install's agent) and documents that
# "non-broker callers pass straight through". Binding a write to the credential
# is a different mechanism — ``bind_write_identity_to_auth`` — which
# ``config.py`` defaults to False and describes as shipping dark.

# Imported callables worth naming in the report. Not an exemption and not
# consulted by the invariant: purely a filter so the failure output shows the
# identity-relevant calls instead of every ``Depends`` and ``Query``.
IDENTITY_BINDERS = frozenset(
    {
        "resolve_write_agent",
        "resolve_caller_and_gate",
        "broker_owned_agent_id",
        "enforce_delete",
        "enforce_fleet_write",
    }
)

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
    # Reviewing is privileged INSIDE a tenant, not super-admin-only: enforce_admin
    # would put it out of reach of the people who run the tenant. But plain tenant
    # scope would let any agent dismiss the contradictions it caused, and a
    # dismissal is the only record that detection was wrong — so the resolve route
    # gates on require_trust(min_level=2), the keystone-author bar.
    "conflicts": "trust-plane: human review of detector output, gated by require_trust",
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
    # The five ``skills-inbox`` actions were here as KNOWN GAPs — guarded on
    # the admin axis by ``_require_inbox_admin``, which admits an org admin
    # whose key may be read-only. Closed: each handler now calls
    # ``enforce_read_only`` itself, so they need no entry at all, and
    # ``test_allowlists_have_no_unnecessary_entries`` is what said so rather
    # than someone remembering. Their PLANE entries below stay — only the
    # write axis moved.
}

# A reason starting with this is NOT a justification — it is an unfixed gap
# recorded in the open, and the count below may only shrink. Borrowed from
# ``tenant_scope_gate``, which carries a ``blind-spot ... <- backlog`` category
# for the same purpose: the alternative is a list where "excused" and "not yet
# fixed" look identical, and then neither gets read.
KNOWN_GAP_PREFIX = "KNOWN GAP:"
# The ceiling itself lives on ``_Axis.gap_ceiling``, one per invariant, so
# headroom cannot be fungible between them. There is deliberately no module
# constant here: a single number would have to be the sum, and a reader
# lowering it after a fix could not say which axis it belonged to.

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

# The SELF plane. Every route accepting a ``SELF_ID_PARAMS`` name either calls
# ``enforce_self_agent``, is exempt by construction (``SELF_GATE_EXEMPT``),
# binds by ``auth.agent_id`` precedence, or has a line here.
#
# The lines fall into four kinds, and the count per kind is the interesting
# part: what an ``agent_id`` MEANS is not constant across this surface, and
# every past bug on this axis was a route where two meanings shared one
# parameter (``GET /memories/stats``'s knob was both author filter and
# visibility identity; ``/recall``'s ``filter_agent_id`` was both).
SELF_GATE_ALLOWLIST: dict[str, str] = {
    # STRICTER than precedence, and the reason it is worth its own line. The
    # authorization principal here is ``auth.agent_id`` alone — the query param
    # is never promoted — and ``enforce_delete`` then gates that principal at
    # trust >= 3. The ``auth.agent_id or agent_id`` in the same handler is the
    # AUDIT attribution and is documented as not for authorization.
    #
    # Nothing HERE would notice if that changed: no static check in this file
    # can tell which of two same-shaped expressions governs, which is why the
    # rule that tried was removed. What holds the line is behavioural —
    # ``test_memory_byid_authz.test_rest_delete_byid_tenant_key_not_trust_gated``
    # is documented as the test that fails if the param becomes a principal
    # again, and it is where a reader should go, not this line.
    "DELETE /api/v1/memories/{memory_id}": (
        "principal is auth.agent_id alone, gated by enforce_delete at "
        "trust >= 3; the query param reaches only the audit row"
    ),
    # FILTERS, not identity assertions. The parameter narrows which rows the
    # call touches; the caller's own identity is established elsewhere.
    "DELETE /api/v1/memories": (
        "filter: narrows the delete; the caller's own trust is what is gated, "
        "by enforce_delete(auth.agent_id) at trust >= 3"
    ),
    "GET /api/v1/keystones": "filter: narrows a fleet listing, no visibility identity attached",
    # The literal path is hoisted to ``tests/_legacy_contracts.py`` rather
    # than written here: an allowlist key must equal the path the app
    # serves, so the legacy spelling is mandatory, and a marker on this
    # line would be displaced the first time ``ruff format`` wrapped it.
    LEGACY_KEYSTONES_ROUTE: "filter: the permanent legacy alias of the line above",
    "GET /api/v1/reports/agent-activity": (
        "filter: narrows a digest on a surface that is cross-agent by design — "
        "GET /reports builds a per_agent breakdown of the tenant"
    ),
    # READS of the agent row itself. Tenant-readable by design, and gating one
    # spelling would leave the identical payload one hop away: both call
    # sc.get_agent and return the same AgentOut under the same enforce_tenant.
    # Whether the row should be tenant-readable at all is a real question and a
    # wider one than this invariant — raised in #1364, not settled there.
    "GET /api/v1/agents/{agent_id}": "tenant-readable agent row; the /tune twin serves the same payload",
    "GET /api/v1/agents/{agent_id}/tune": "tenant-readable agent row; same payload as GET /agents/{agent_id}",
    # BOUND by an imported resolver rather than by the local precedence shape.
    # resolve_caller_and_gate applies the same rule one layer down —
    # ``auth.agent_id or body_agent_id`` — so a named peer is discarded, not
    # refused. It LOGS the mismatch instead of raising, which is why it is not
    # a caller of the gate; see the note in AuthContext.enforce_self_agent.
    "POST /api/v1/evolve/report": "bound by resolve_caller_and_gate: the verified identity wins",
    "POST /api/v1/insights/generate": "bound by resolve_caller_and_gate: the verified identity wins",
    # INERT. Both reach ``ingest_preview``, which never reads
    # ``request.agent_id`` — its first use is on the commit path, which is the
    # entry below. "Persists nothing" is also true but argues the write axis,
    # and this invariant opens by saying it is not about mutation; the
    # parameter being unread is the fact that settles a read too.
    "POST /api/v1/ingest/file": "inert: ingest_preview never reads the agent_id it is handed",
    "POST /api/v1/ingest/preview": "inert: ingest_preview never reads the agent_id it is handed",
    # NODE-plane in intent — the interviewer runs under the install's
    # credential and reports on the worker node it watches, so ``agent_id``
    # names the SUBJECT and ``enforce_self_agent`` would refuse the case the
    # route exists for (``routes/interview.py`` declares it required and its own
    # comment calls it "the WORKER agent the window belongs to").
    #
    # Recorded as a gap anyway, because by this file's taxonomy that is what it
    # is: ``interview_service`` persists memories with
    # ``agent_id=<caller-named>``, the same property the two ``POST /memories``
    # lines below record. Narrower in blast radius — visibility is forced to
    # ``scope_team``, so nothing lands in a peer's private scope — and the
    # ``metadata.written_by`` the service comment offers as the mitigation is
    # the constant string ``"interviewer"``, not the submitting credential, so
    # no row identifies who sent it.
    "POST /api/v1/interview/submit": (
        "KNOWN GAP: node-plane by intent, but persists memories attributed to "
        "a caller-named agent; scope_team caps the blast radius"
    ),
    # KNOWN GAPs. Write attribution is caller-named on the agent plane today:
    # an agent credential may write a memory attributed to a peer. This is
    # acknowledged and staged, not unnoticed — ``config.py`` carries
    # ``bind_write_identity_to_auth`` ("Phase 2 (spoof hardening), ships dark",
    # default False), which is exactly this fix, held until the reserved-`main`
    # credentials are re-identified. resolve_write_agent does NOT close it: it
    # enforces the broker/install ownership boundary and passes non-broker
    # callers straight through.
    "POST /api/v1/memories": (
        "KNOWN GAP: attribution is caller-named; binding is behind "
        "bind_write_identity_to_auth, which ships dark"
    ),
    "POST /api/v1/memories/bulk": (
        "KNOWN GAP: attribution is caller-named; binding is behind "
        "bind_write_identity_to_auth, which ships dark"
    ),
    # Named without the flag, deliberately: this handler never reads it, and
    # ``test_allowlist_reasons_that_name_a_mechanism_are_corroborated`` is what
    # said so. The two entries above DO read it, which is the difference — this
    # path would still be caller-named with Phase 2 fully enabled.
    "POST /api/v1/ingest/commit": (
        "KNOWN GAP: attribution is caller-named; broker_owned_agent_id gates "
        "install ownership only, and nothing here binds the write to the "
        "calling credential"
    ),
}


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------


def _normalise(path: str) -> str:
    """``{doc_id:path}`` in a route is ``{doc_id}`` in the schema."""
    return re.sub(r"\{([^{}:]+):[^{}]+\}", r"{\1}", path)


def _resolve_operations() -> list[tuple[str, str, object]]:
    """Flatten the live app to ``(verb, path, route)``.

    The ROUTE rather than the endpoint, because the self-plane invariant needs
    ``route.dependant`` — the request interface — while the other two need
    ``route.endpoint``, which is one attribute away.

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
                        found.append((verb, prefix + route.path, route))
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
# Request-interface inspection (the self plane)
# ---------------------------------------------------------------------------


def _body_models(annotation) -> list[type]:
    """Pydantic models reachable from a body annotation, through ``Optional``,
    ``Annotated`` and unions."""
    if annotation is None:
        return []
    found = [annotation] if hasattr(annotation, "model_fields") else []
    for arg in typing.get_args(annotation) or ():
        found.extend(_body_models(arg))
    return found


def _request_params(route) -> frozenset[str]:
    """Every parameter name this route accepts, from any source.

    Two attribute choices decide how much of the surface this sees, and both
    of the plausible-looking alternatives fail QUIETLY — they return a smaller
    set, never an error, and the invariant then passes over what is left.
    Counted on this tree:

        route.dependant + field_info.annotation   34 routes   (this)
        route.dependant, field.type_ only         24 routes
        inspect.signature(endpoint)               30 routes

    ``inspect.signature`` loses the four handlers whose module carries
    ``from __future__ import annotations`` — ``POST /stm/promote``,
    ``POST /evolve/report``, ``POST /insights/generate``,
    ``POST /interview/submit`` — because their body annotation is the STRING
    ``"PromoteRequest"``, which has no ``model_fields``.

    ``field.type_`` is ``None`` on the FastAPI/pydantic build here, so reading
    only it loses every body-borne identity: ten routes, including
    ``POST /recall`` and ``POST /search``, which are the two the gate was
    written for. Both spellings are tried, and
    ``test_the_identity_scan_sees_body_borne_ids`` is what stops a FastAPI
    upgrade from shrinking this invariant in silence.
    """
    found: set[str] = set()
    seen: set[int] = set()

    def visit(dependant) -> None:
        if dependant is None or id(dependant) in seen:
            return
        seen.add(id(dependant))
        for group in ("path_params", "query_params", "header_params", "cookie_params"):
            for field in getattr(dependant, group, None) or ():
                found.add(field.name)
        for field in getattr(dependant, "body_params", None) or ():
            found.add(field.name)
            annotation = getattr(field, "type_", None)
            if not hasattr(annotation, "model_fields"):
                annotation = getattr(
                    getattr(field, "field_info", None), "annotation", None
                )
            for model in _body_models(annotation):
                found.update(model.model_fields)
        for sub_dependant in getattr(dependant, "dependencies", None) or ():
            visit(sub_dependant)

    visit(getattr(route, "dependant", None))
    return frozenset(found)


def _identity_params(route) -> frozenset[str]:
    """The ``SELF_ID_PARAMS`` this route actually accepts."""
    return frozenset(_request_params(route) & SELF_ID_PARAMS)


# ---------------------------------------------------------------------------
# Handler classification
# ---------------------------------------------------------------------------


@functools.cache
def _parse(fn) -> ast.AST | None:
    """Cached because the fifteen tests here re-derive the same rows.

    Every test calls a row builder, each builder walks the whole app, and
    ``_classify`` parses each handler plus two levels of helpers — so one file
    run did ~7,100 ``getsource`` + ``ast.parse`` pairs over a few hundred
    distinct functions. Measured: the cache takes the in-test work from 1.83s
    to 0.55s.

    Safe because a function's source cannot change mid-run and nothing mutates
    the returned tree — every reader is an ``ast.walk``. Keyed on the function
    object, which is what ``route.endpoint`` holds from registration, so it is
    also stable across the monkeypatching the wider suite does (which targets
    services and settings, never a registered handler).
    """
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


@functools.cache
def _classify(
    fn, _depth: int = 0
) -> tuple[
    frozenset[str], frozenset[str], frozenset[str], frozenset[str], frozenset[str]
]:
    """``(enforce_* names, guard helpers, imported calls, settings flags, binders)``.

    Cached on the same grounds as ``_parse``, and for a larger win: fifteen
    tests each rebuild the same rows, so this re-walked already-parsed trees
    about eight times per run — 825k ``ast.walk`` visits, 1.3s of a 3.8s
    profiled run. The arguments are its full signature, the four returned sets
    are frozen, and nothing mutates a registered handler mid-run.

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

    - IMPORTED helpers are still not FOLLOWED (the ``__module__`` filter
      below), so ``enforce_delete``, ``enforce_fleet_write``,
      ``enforce_fleet_read_many``, ``resolve_caller_and_gate`` and
      ``update_memory`` contribute no gates. All 14 routes reaching them also
      call ``enforce_read_only`` directly, so nothing is missed today — and the
      limitation fails SAFE: a route whose only guard is imported reports no
      gates and fails the invariant. Their NAMES are now recorded in
      ``imported_calls``, which is a strictly weaker claim: "this handler calls
      ``resolve_write_agent``" is a fact, "that covers the self plane" is a
      judgement, and the judgement stays in the allowlist where
      ``test_allowlist_reasons_that_name_a_mechanism_are_corroborated`` checks
      the fact under it. Deliberately NOT gate attribution: following an
      imported service function would credit a route with a gate that runs
      three layers down, which is exactly the attribution this file avoids.
    - ``ast.walk`` ignores control flow and ordering, so a gate inside an
      ``if``, after an early ``return``, or in a nested ``def`` all count.
      No route relies on that for either invariant gate today (three
      ``memories`` routes do it for ``enforce_usage_limits``). Ordering is
      asserted behaviourally in ``tests/test_route_authz_gaps.py`` instead,
      which is where a "gate ran after the write" mutant is caught.

    ``flags`` records ``settings.<name>`` / ``app_settings.<name>`` reads, on
    the same terms as ``imported_calls``: a behaviour held behind a feature
    flag is a fact about the handler, and an allowlist line naming the flag
    should be falsifiable. It says nothing about the flag's VALUE —
    ``bind_write_identity_to_auth`` defaults to False, and a reader who needs
    that has to open ``config.py``.
    """
    tree = _parse(fn)
    if tree is None:
        return frozenset(), frozenset(), frozenset(), frozenset(), frozenset()
    gates: set[str] = set()
    helpers: set[str] = set()
    binders: set[str] = set()
    imported: set[str] = set()
    flags: set[str] = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in SETTINGS_OBJECTS
    }
    module = inspect.getmodule(fn)
    module_globals = getattr(module, "__dict__", {})
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith("enforce_"):
                gates.add(node.func.attr)
            elif node.func.attr == SELF_BINDER:
                # Its OWN channel, deliberately. Folding it into ``gates`` was
                # the smaller edit and quietly falsified three things: the
                # failure report prints that set under the label "enforce_*
                # called", the helper-promotion below reads a non-empty
                # ``sub_gates`` as evidence that a helper GUARDS, and
                # ``test_allowlist_reasons_that_name_a_mechanism_are_corroborated``
                # then accepts that helper as corroboration. A binder refuses
                # nothing; it must not be able to stand in for something that
                # does.
                binders.add(node.func.attr)
        elif isinstance(node.func, ast.Name) and _depth < 2:
            if node.func.id == fn.__name__:
                continue  # decorator returns fn unchanged; do not self-credit
            target = module_globals.get(node.func.id)
            if not inspect.isfunction(target):
                continue
            if getattr(target, "__module__", None) != fn.__module__:
                # An imported callable: record the NAME and do not follow it.
                imported.add(node.func.id)
                continue
            sub_tree = _parse(target)
            if sub_tree is None:
                continue
            sub_gates, sub_helpers, sub_imported, sub_flags, sub_binders = _classify(
                target, _depth + 1
            )
            # Imported names merge unconditionally while gates merge only from a
            # helper that guards. The asymmetry is deliberate: ``helpers``
            # answers "is this a guard", which needs evidence, and
            # ``imported_calls`` answers "what does the reachable code call",
            # which does not. ``POST /memories`` reaches ``resolve_write_agent``
            # through ``_write_memory_inner``, which neither refuses nor adds a
            # gate of its own.
            imported |= sub_imported
            flags |= sub_flags
            # Binders propagate like imports and flags — unconditionally, and
            # WITHOUT promoting the helper to ``helpers``. A helper that only
            # computes an identity is not a guard.
            binders |= sub_binders
            if sub_gates or _refuses(sub_tree):
                helpers.add(node.func.id)
                gates |= sub_gates
                helpers |= sub_helpers
    return (
        frozenset(gates),
        frozenset(helpers),
        frozenset(imported),
        frozenset(flags),
        frozenset(binders),
    )


def _row(verb: str, path: str, route) -> dict:
    endpoint = route.endpoint
    gates, helpers, imported, flags, binders = _classify(endpoint)
    return {
        "key": f"{verb} {path}",
        "router": endpoint.__module__.rsplit(".", 1)[-1],
        "handler": endpoint.__name__,
        "gates": gates,
        "helpers": helpers,
        "imported_calls": imported,
        "settings_flags": flags,
        "binders": binders,
        "identity_params": _identity_params(route),
    }


def _mutating_routes(include_test_only: bool = False) -> list[dict]:
    rows = []
    for verb, path, route in _resolve_operations():
        if verb not in MUTATING_VERBS:
            continue
        row = _row(verb, path, route)
        if row["router"] in TEST_ONLY_ROUTERS and not include_test_only:
            continue
        rows.append(row)
    return rows


def _self_plane_routes() -> list[dict]:
    """Every route, ANY verb, that accepts a caller-supplied agent identity.

    No ``include_test_only`` knob, unlike ``_mutating_routes``: measured, no
    ``testing`` route takes a ``SELF_ID_PARAMS`` name, so the parameter would
    have had one value at every call site.

    ``_identity_params`` is checked BEFORE ``_row``, which is the cheap order —
    the scan costs 0.16ms over all 115 routes and classifying them costs 132ms,
    and 81 of the 115 are discarded.
    """
    rows = []
    for verb, path, route in _resolve_operations():
        if not _identity_params(route):
            continue
        row = _row(verb, path, route)
        if row["router"] in TEST_ONLY_ROUTERS:
            continue
        rows.append(row)
    return rows


def _report(row: dict) -> str:
    lines = [
        f"    {row['key']}",
        f"        handler: {row['router']}.{row['handler']}",
        f"        enforce_* called: {sorted(row['gates']) or 'NONE'}",
        f"        identity binders: {sorted(row['binders']) or 'none'}",
        f"        refusing helpers: {sorted(row['helpers']) or 'none'}",
    ]
    if row["identity_params"]:
        binders = sorted(row["imported_calls"] & IDENTITY_BINDERS)
        lines.append(f"        identity params: {sorted(row['identity_params'])}")
        lines.append(f"        identity-binding calls: {binders or 'none'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------


def _unguarded_for_write(row: dict) -> bool:
    """Would this route fail the write invariant without an allowlist entry?"""
    return WRITE_GATE not in row["gates"] and not (row["gates"] & WRITE_GATE_EXEMPT)


def _unguarded_for_plane(row: dict) -> bool:
    """Would this route fail the plane invariant without an allowlist entry?"""
    return (
        row["router"] in ADMIN_PLANE_ROUTERS
        and PLANE_GATE not in row["gates"]
        and not (row["gates"] & PLANE_GATE_EXEMPT)
    )


def _unguarded_for_self(row: dict) -> bool:
    """Would this route fail the self-plane invariant without an entry?"""
    return (
        bool(row["identity_params"])
        and SELF_GATE not in row["gates"]
        and SELF_BINDER not in row["binders"]
        and not (row["gates"] & SELF_GATE_EXEMPT)
    )


class _Axis(NamedTuple):
    """One invariant, and everything the hygiene checks need to police it.

    Sharing the predicate with the invariant test is the original point: an
    entry is "needed" exactly when the invariant would flag the route, by one
    definition rather than two that can drift. ``rows`` and ``gap_ceiling``
    joined it for the same reason — the self plane arrived needing a different
    route surface (every verb, not just mutating ones) and its own ratchet, and
    both started life as separate structures keyed by ``name``. A fourth axis
    would then have inherited the staleness, unnecessary-entry and
    corroboration checks automatically and silently had NO gap ceiling, which
    is the "sat outside both sets with nothing failing" failure this file
    records against ``keystones``.
    """

    name: str
    allowlist: dict[str, str]
    needs_entry: Callable[[dict], bool]
    rows: Callable[[], list[dict]]
    gap_ceiling: int


# Ceilings are PER AXIS, not one number across all three, so headroom cannot be
# fungible: closing a self-plane gap must not silently license a new write-gate
# one. The self plane starts at 4 because the axis is newer than the gaps on it
# — write attribution is caller-named today and ``config.py`` already carries
# the staged fix — and the ceiling is what stops a fifth joining quietly.
_ALLOWLISTS = (
    _Axis(
        "WRITE_GATE_ALLOWLIST",
        WRITE_GATE_ALLOWLIST,
        _unguarded_for_write,
        _mutating_routes,
        0,
    ),
    _Axis(
        "PLANE_GATE_ALLOWLIST",
        PLANE_GATE_ALLOWLIST,
        _unguarded_for_plane,
        _mutating_routes,
        0,
    ),
    _Axis(
        "SELF_GATE_ALLOWLIST",
        SELF_GATE_ALLOWLIST,
        _unguarded_for_self,
        _self_plane_routes,
        4,
    ),
)


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
        if _unguarded_for_write(row) and row["key"] not in WRITE_GATE_ALLOWLIST
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
        if _unguarded_for_plane(row) and row["key"] not in PLANE_GATE_ALLOWLIST
    ]
    assert not offenders, (
        f"{len(offenders)} admin-plane mutating route(s) neither call "
        f"{PLANE_GATE}, nor are admin-only, nor are listed in "
        f"PLANE_GATE_ALLOWLIST:\n"
        + "\n".join(_report(r) for r in offenders)
        + f"\n\nAdd the gate, or add a line to PLANE_GATE_ALLOWLIST in {__file__} "
        "saying which plane this route belongs to."
    )


def test_routes_taking_an_agent_identity_gate_the_self_plane() -> None:
    """A caller must not act as an agent it merely named.

    The third axis, and the one that is not about mutation:
    ``GET /stm/notes?agent_id=<peer>`` returned a peer's private notes and
    ``filter_agent_id`` on ``POST /recall`` returned a peer's private rows.
    Both reads, both found and fixed one route at a time — the 2026-06-11
    audit closed the STM DELETE twin and left the read open (``routes/stm.py``
    still carries the note), and H-06 was the ``/recall`` half. #1364 is what
    gave the rule one name to look for.

    A route satisfies it three ways: call ``enforce_self_agent`` or
    ``auth.effective_agent_id``; refuse agent credentials outright, so the
    question has no subject (``SELF_GATE_EXEMPT``); or carry a line in
    ``SELF_GATE_ALLOWLIST`` saying what the parameter means instead. The
    binder counts because it is a NAMED call — the note above
    ``SELF_ID_PARAMS`` records the shape-matching version that was tried
    first and what it let through.
    """
    offenders = [
        row
        for row in _self_plane_routes()
        if _unguarded_for_self(row) and row["key"] not in SELF_GATE_ALLOWLIST
    ]
    assert not offenders, (
        f"{len(offenders)} route(s) accept a caller-supplied agent identity, "
        f"do not call {SELF_GATE}, are not admin-only or "
        "agent-credential-free, and are not listed in SELF_GATE_ALLOWLIST:\n"
        + "\n".join(_report(r) for r in offenders)
        + f"\n\nAdd the gate, or add a line to SELF_GATE_ALLOWLIST in {__file__} "
        "saying what this route's agent_id means if not 'act as this agent'."
    )


def test_the_audit_attribution_is_not_bound_by_the_helper() -> None:
    """``delete_memory`` must keep spelling its audit attribution out.

    That handler holds both values at once: ``caller_agent_id = auth.agent_id``
    is what it authorizes with, and ``attribution_agent_id = auth.agent_id or
    agent_id`` is what the audit row records, under a comment reading
    "Authorization must not trust this value". The second is why the
    shape-matching rule was a false negative, and it is the one line on this
    surface that must NOT become ``auth.effective_agent_id``.

    Without this, ``AuthContext.effective_agent_id``'s docstring asserts that
    property and nothing checks it. Worse, the check that WOULD eventually fire
    is ``test_allowlists_have_no_unnecessary_entries``, whose message says
    "Delete the entries" — the exact wrong move at the exact moment the false
    negative returns. This fires first and says the right thing.
    """
    key = "DELETE /api/v1/memories/{memory_id}"
    rows = {row["key"]: row for row in _self_plane_routes()}
    row = rows.get(key)
    assert row is not None, (
        f"{key} is no longer a self-plane route, so this test guards nothing. "
        "Re-point it or delete it — do not leave it passing vacuously."
    )
    assert SELF_BINDER not in row["binders"], (
        f"{key} now calls {SELF_BINDER}, which the self-plane rule reads as "
        "'the identity is bound'. That handler authorizes with auth.agent_id "
        "alone; the only same-shaped expression in it is the AUDIT "
        "attribution, whose own comment says authorization must not trust "
        "it.\n\n"
        "If the AUDIT line was converted: revert it. Binding the audit value "
        "makes the route look bound without changing what it authorizes with, "
        "which is exactly the 2026-09-03 false negative returning.\n"
        "If the PRINCIPAL genuinely changed to precedence: that is a real "
        "authorization change, and tests/test_memory_byid_authz.py is where it "
        "has to be argued, not here.\n\n"
        "Do NOT resolve this by deleting the SELF_GATE_ALLOWLIST entry."
    )


def test_the_identity_scan_sees_body_borne_ids() -> None:
    """Guards the scan itself, which failed silently on its first draft.

    ``_request_params`` documents which attributes it reads and what each
    alternative loses. This pins three of the ten routes that the plausible
    wrong one drops: the ones that ARE gated, so a regression on them is a real
    hole rather than a missing allowlist line, and two of the three are what
    ``enforce_self_agent`` was written for.

    A FastAPI upgrade that moves the annotation again fails HERE, naming what
    broke, instead of quietly narrowing the invariant to the query string.
    """
    rows = {row["key"]: row for row in _self_plane_routes()}
    expected = {
        "POST /api/v1/recall": {"filter_agent_id", "caller_agent_id"},
        "POST /api/v1/search": {"filter_agent_id", "caller_agent_id"},
        "POST /api/v1/stm/promote": {"agent_id"},
    }
    missing = {
        key: (
            "route not in the self-plane set at all"
            if key not in rows
            else f"found {sorted(rows[key]['identity_params'])}, want {sorted(want)}"
        )
        for key, want in expected.items()
        if key not in rows or not want <= rows[key]["identity_params"]
    }
    assert not missing, (
        "the identity scan stopped seeing body-borne agent ids:\n"
        + "\n".join(f"    {k}: {v}" for k, v in sorted(missing.items()))
        + "\nFix _request_params before trusting a green run — the self-plane "
        "invariant checks whatever this finds, and finding less passes."
    )


def test_the_excluded_identity_names_are_still_live() -> None:
    """``SELF_ID_PARAMS_EXCLUDED`` documents names that are NOT assertions.

    A name that stops appearing on the surface makes its line a decision about
    nothing, and the next reader takes it as evidence the question was asked
    recently. Same failure the allowlist staleness checks exist for, one level
    down: these exclusions are why routes are absent from the scan entirely,
    so nothing else would notice.
    """
    live: set[str] = set()
    for _verb, _path, route in _resolve_operations():
        live |= {n for n in _request_params(route) if "agent" in n.lower()}
    dead = sorted(set(SELF_ID_PARAMS_EXCLUDED) - live)
    assert not dead, (
        f"SELF_ID_PARAMS_EXCLUDED names parameters no route takes: {dead}\n"
        f"Agent-shaped names actually on the surface: {sorted(live)}\n"
        "Delete the entries, or correct them to the name that replaced them."
    )
    unclassified = sorted(live - SELF_ID_PARAMS - set(SELF_ID_PARAMS_EXCLUDED))
    assert not unclassified, (
        f"agent-shaped parameter name(s) in neither set: {unclassified}\n"
        "Add each to SELF_ID_PARAMS if it is a claim about who the caller is, "
        "or to SELF_ID_PARAMS_EXCLUDED with what it means instead. A new name "
        "must not join the surface unexamined."
    )


def test_the_self_plane_scope_is_not_silently_empty() -> None:
    """Guards the guard: the invariant must be checking a real surface.

    ``_self_plane_routes`` filters on ``identity_params``, so a scan that
    returned nothing would make ``test_routes_taking_an_agent_identity_gate_
    the_self_plane`` pass over an empty list. Asserted loosely — the point is
    "many routes", not a number to update whenever one is added.

    Only the COUNT is asserted. A companion check that ``>= 10`` of them still
    call the gate was removed after measuring what it caught: blind
    ``_classify`` to ``enforce_self_agent`` and 11 of the 12 gated routes turn
    into offenders of the invariant above, which fails with the full report,
    because nothing consults the allowlist for a route that has no entry in it.
    A second magic number guarding a mutant that already fails two tests up.
    """
    rows = _self_plane_routes()
    assert len(rows) >= 25, (
        f"only {len(rows)} routes take a caller-supplied agent identity; there "
        "were 34 when this was written, so the scan has probably stopped "
        "seeing a whole parameter source."
    )


def test_allowlists_have_no_unnecessary_entries() -> None:
    """An entry excusing a route that now passes on its own is dead.

    ``test_allowlists_have_no_stale_entries`` only catches an entry naming a
    route that no longer exists. It does not catch the commoner case: the gap
    gets FIXED and the line stays, still reading as though someone decided the
    route did not need the gate. That is worse than untidy — a future reader
    takes the line at face value, and a genuinely unguarded route can later be
    added under the same key and be excused by a reason written about
    something else.

    This was not hypothetical for one commit: closing the five ``skills_inbox``
    write gaps left their ``KNOWN GAP`` entries excusing five routes that had
    just been gated, and every check in this file still passed. Hence this one.
    """
    unnecessary: dict[str, str] = {}
    for axis in _ALLOWLISTS:
        rows = {row["key"]: row for row in axis.rows()}
        for key, reason in axis.allowlist.items():
            row = rows.get(key)
            if row is None:
                continue  # test_allowlists_have_no_stale_entries owns this
            if not axis.needs_entry(row):
                unnecessary[f"{axis.name}[{key}]"] = (
                    f"route now satisfies the invariant on its own "
                    f"(gates: {sorted(row['gates'])}) — reason still says {reason!r}"
                )
    assert not unnecessary, (
        "allowlist entries excuse routes that no longer need excusing:\n"
        + "\n".join(f"    {k}: {v}" for k, v in sorted(unnecessary.items()))
        + "\n\nDelete the entries. If one was a KNOWN GAP, lower that axis's "
        "gap_ceiling in _ALLOWLISTS to match."
    )


def test_allowlists_have_no_stale_entries() -> None:
    """An entry naming a route that no longer exists has to fail.

    Without this the lists rot into a place where a real gap can hide behind a
    line that once meant something — the failure mode
    ``test_api_read_scope_params`` guards its own opt-out table against.
    """
    stale = {}
    for axis in _ALLOWLISTS:
        live = {row["key"] for row in axis.rows()}
        if set(axis.allowlist) - live:
            stale[axis.name] = sorted(set(axis.allowlist) - live)
    assert not stale, (
        "allowlist entries name routes this app no longer serves on that "
        f"axis — renamed, removed, or their verb changed:\n{stale}\n"
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


# Which words in a reason are read as claims about code. Extended past the
# original ``_private``/``enforce_*`` pair for the self plane, whose mechanisms
# are ordinary imported names (``resolve_caller_and_gate``,
# ``broker_owned_agent_id``). A name outside this pattern is not checked, so
# keeping it in step with the vocabulary the reasons use is what stops a line
# from being unfalsifiable prose — and ``bind_write_identity_to_auth`` is
# deliberately in it, so the KNOWN GAP lines naming the dark flag are checked
# against the handler that reads it.
_MECHANISM_NAMES = r"\b(_\w+|enforce_\w+|resolve_\w+|broker_\w+|bind_\w+)\b"


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
    broken: dict[str, str] = {}
    for axis in _ALLOWLISTS:
        rows = {row["key"]: row for row in axis.rows()}
        for key, reason in axis.allowlist.items():
            row = rows.get(key)
            if row is None:
                continue  # staleness is test_allowlists_have_no_stale_entries' job
            observed = (
                row["gates"]
                | row["helpers"]
                | row["imported_calls"]
                | row["settings_flags"]
                | row["binders"]
            )
            for claimed in re.findall(_MECHANISM_NAMES, reason):
                if claimed not in observed:
                    broken[f"{axis.name}[{key}]"] = (
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

    One ceiling PER AXIS, carried on ``_Axis``. A single shared number would
    make headroom fungible across invariants — closing a self-plane gap would
    license a new write-gate one without anything failing.
    """
    for axis in _ALLOWLISTS:
        gaps = sorted(
            f"{key}  ({reason})"
            for key, reason in axis.allowlist.items()
            if reason.startswith(KNOWN_GAP_PREFIX)
        )
        assert len(gaps) <= axis.gap_ceiling, (
            f"{axis.name}: {len(gaps)} known gaps, ceiling is "
            f"{axis.gap_ceiling}:\n    " + "\n    ".join(gaps) + "\n\nAdd the gate "
            "rather than raising the ceiling. The ceiling exists to be lowered."
        )
        assert len(gaps) == axis.gap_ceiling, (
            f"{axis.name}: {len(gaps)} known gaps but the ceiling is still "
            f"{axis.gap_ceiling} — a gap was fixed without lowering it, so the "
            "ratchet has slack a new gap could take up silently. Set this "
            f"axis's gap_ceiling to {len(gaps)}."
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
