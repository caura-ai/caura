"""ax-0917-h-01 / h-02 — is a request-scoped deadline ARMED, or only claimed?

``per_tenant_storage_slot`` queues UNBOUNDED and justifies that in its own
docstring: *"an outer request budget already caps total wall time"*. That is a
claim about the roster of CALLERS, and it was false on the transport agents use
most — MCP — for months, because the justification lived in a comment and
comments are not checked. #1726 armed a budget there; this file is what keeps
the claim true for the next surface someone adds.

The same shape recurred ten times across the 09/17-09/24 audit: a sentinel
check, a phantom CAS, a dead token knob, a stale worker warning, guard comments
— each asserting a property the system did not have. The durable fix named in
#1726 was *"a check that a request-scoped deadline is armed, not a comment
saying one is."* This is that check.

WHAT IT ENFORCES, in two halves that fail for different reasons:

* **Behaviour.** Each surface in ``ARMED`` is driven through its real entry
  point — real route, real middleware order, real pipeline, real bulkhead — and
  the recorder is read AT the unbounded acquire (``_deadline is not None``,
  still in the future). Then the same surface is driven again with the
  semaphore made un-acquirable, and the deadline must actually CANCEL that wait
  and name it. A check that only read the docstring would be the very defect
  being fixed; a check that only read the ContextVar could pass on a recorder
  armed with ``own_deadline(None)``, which is exactly the state MCP shipped in.
* **Structure.** Every HTTP path in the OpenAPI schema and every ASGI mount is
  run THROUGH ``RequestTimeoutMiddleware`` itself, and any one the middleware
  does not arm for must be named in the roster below. Adding a route to
  ``_TIMEOUT_OPT_OUT_PATHS`` or mounting a new transport is therefore what
  breaks the build — not a count someone bumps. (``test_c27_strict_fleet_scoping``
  asserted a call-site COUNT, which pins an incompleteness instead of catching
  it; that became ax-0917-m-19, and #1701 gave it a by-name enumeration
  alongside the count. This file enumerates by name from the start.)

WHAT IT CANNOT ENFORCE, stated rather than implied: a caller that is neither an
HTTP route nor an ASGI mount is invisible to the structural half, because there
is no wiring here to walk, and a background caller has no request budget to
assert anyway. Two such callers are live today:

* the audit-queue flusher (``_flush_one_tenant`` in ``app.py``) reaches this
  same bulkhead from a background loop. It has no request recorder, so it
  arms its own ``asyncio.timeout`` over the acquire
  (``audit_flush_slot_timeout_seconds``, oss-0927-m-04) and is gated by NAME
  in ``BACKGROUND`` below — driven against the saturated semaphore like the
  request surfaces, minus the recorder read it has no recorder for. What is
  still not walked is the discovery of a THIRD background caller: that one
  gets in by being added here, not by being found;
* ``core_worker.per_tenant_concurrency`` is a SEPARATE bulkhead in a separate
  service, justified by the Pub/Sub redelivery budget rather than a request
  budget, and deliberately out of scope.

No provider calls and no LLM calls: the interview surface's tenant is on the
``fake`` provider, so its fallback chain's ``fake_fn`` stands in a report, and
every stalled hop is a patched semaphore.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import get_test_auth, new_tenant_id, uid

# No module-level ``asyncio`` mark: ``asyncio_mode = auto`` already covers the
# async tests, and the two roster tests at the end are synchronous — a blanket
# mark makes pytest warn on those.

# Low enough to keep the suite fast, high enough that ordinary scheduling
# jitter cannot make a hop that never yields look like a timeout. Same value
# the two sibling attribution files use, for the same reason.
_BUDGET_S = 0.4

# How long a surface may take before "the deadline cancelled the wait" stops
# being the explanation. Twenty times the budget: far enough above scheduler
# noise to never flake, far enough below a real hang to still fail one.
_TERMINATION_CEILING_S = _BUDGET_S * 20

# Far past the budget: a stall that could finish on its own would make every
# assertion here a race.
_STALL_S = 30.0


# ---------------------------------------------------------------------------
# The roster. Adding a surface means adding an entry AND a driver; the
# structural tests below refuse to pass while either is missing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Surface:
    """One entry point that can reach a deadline-dependent unbounded wait.

    ``budget_setting`` is the ``Settings`` attribute holding the budget, which
    the driver lowers — so a knob nothing reads fails here rather than shipping
    as decoration. ``path`` is the ASGI path the blanket middleware must NOT
    arm for (``None`` for the blanket surface itself, which IS the middleware).
    """

    name: str
    path: str | None
    budget_setting: str | None
    expected_phase: str | None
    why: str


# Surfaces that must arm a request-scoped deadline before the unbounded wait.
ARMED: tuple[Surface, ...] = (
    Surface(
        name="rest.blanket",
        path=None,
        budget_setting="request_timeout_seconds",
        expected_phase="slot_acquire.storage_search",
        why=(
            "RequestTimeoutMiddleware is the blanket budget for every route "
            "that does not opt out; /api/v1/search is driven as its witness "
            "because it reaches the storage_search bulkhead."
        ),
    ),
    Surface(
        name="rest.memories_bulk",
        path="/api/v1/memories/bulk",
        budget_setting="bulk_request_timeout_seconds",
        expected_phase="slot_acquire.storage_write",
        why=(
            "Opts out of the blanket middleware (CAURA-602 needs a longer "
            "budget than the single-write hot path) and arms its own via "
            "request_phase.own_deadline."
        ),
    ),
    Surface(
        name="rest.interview_submit",
        path="/api/v1/interview/submit",
        budget_setting="interview_request_timeout_seconds",
        expected_phase="slot_acquire.storage_write",
        why=(
            "Opts out of the blanket middleware (the synchronous map-reduce "
            "interview measured ~63s) and arms its own via "
            "request_phase.own_deadline."
        ),
    ),
    Surface(
        name="mcp.tools_call",
        path="/mcp",
        budget_setting="mcp_request_timeout_seconds",
        expected_phase="slot_acquire.storage_search",
        why=(
            "The mount is skipped by the middleware (is_mcp_path) because it "
            "serves long-lived streaming responses; oss-0924-h-02 gave the "
            "per-dispatch call_tool frame its own asyncio.timeout instead. "
            "This is the surface whose absent budget made the bulkhead "
            "docstring a defect."
        ),
    ),
)

# Surfaces the blanket middleware does not arm and that are NOT required to arm
# anything. A recorded decision with its reason, re-checked on every run — not
# an omission, and not a free pass either: the reason itself is what
# ``test_the_recorded_exclusion_still_holds`` drives.
UNARMED: tuple[Surface, ...] = (
    Surface(
        name="rest.admin_org_purge_data",
        path="/api/v1/admin/org/purge-data",
        budget_setting=None,
        expected_phase=None,
        why=(
            "Assessed in #1715 and excluded: it reaches NO per_tenant_storage_slot "
            "acquire at all (it calls purge_tenant_data on the storage client "
            "directly), so there is no unbounded wait here for a request budget "
            "to cap. It is bounded by the storage client's own httpx timeouts, "
            "and it is a terminal admin batch driven by the daily sweep rather "
            "than a caller waiting on a queue — cancelling it mid-flight is "
            "pointless, since each tenant's purge is one idempotent transaction."
        ),
    ),
)

ROSTER: tuple[Surface, ...] = ARMED + UNARMED

# Paths the blanket middleware is ALLOWED not to arm for. Derived from the
# roster rather than restated, so the two can never drift apart.
_DECLARED_BYPASSES = frozenset(s.path for s in ROSTER if s.path is not None)


# ---------------------------------------------------------------------------
# Observation points
# ---------------------------------------------------------------------------


class _NeverAcquires:
    """A semaphore nobody ever gets into — the saturated queue, at its limit.

    Stands in for the storage bulkhead's semaphore so the ONE wait the code
    calls unbounded is the wait under test. ``release`` is never reached: a
    cancellation during ``acquire`` unwinds from inside ``__aenter__``, before
    ``per_tenant_storage_slot`` enters the ``try`` that would release.
    """

    def locked(self) -> bool:
        return True

    async def acquire(self) -> bool:
        await asyncio.sleep(_STALL_S)
        return True

    def release(self) -> None:  # pragma: no cover - never reached
        raise AssertionError("a slot that was never acquired must not be released")


@dataclass
class _Acquire:
    """What was armed at ONE ``per_tenant_storage_slot`` acquire."""

    scope: str
    armed: bool
    deadline: float | None
    remaining: float | None


def _watch_acquires(monkeypatch) -> list[_Acquire]:
    """Record the recorder bound at every per-tenant slot acquire.

    Wraps ``_get_semaphore``, which ``per_tenant_storage_slot`` calls in the
    caller's own frame immediately before the unbounded ``acquire()`` — so this
    reads the context the wait will actually run in, not a context reconstructed
    beside it. The real function is still called, so the real bulkhead runs.
    """
    import core_api.middleware.per_tenant_concurrency as ptc
    from core_api import request_phase

    seen: list[_Acquire] = []
    real = ptc._get_semaphore

    def _spy(scope, tenant_id):
        recorder = request_phase.current()
        deadline = None if recorder is None else recorder._deadline
        seen.append(
            _Acquire(
                scope=scope,
                armed=deadline is not None,
                deadline=deadline,
                remaining=None if deadline is None else deadline - time.monotonic(),
            )
        )
        return real(scope, tenant_id)

    monkeypatch.setattr(ptc, "_get_semaphore", _spy)
    return seen


def _saturate_storage_slots(monkeypatch) -> None:
    """Make every storage-scoped acquire block forever; leave the rest alone.

    Only the ``storage_*`` scopes: the route-entry ``write`` / ``search`` /
    ``embed`` slots fast-fail at ``per_tenant_acquire_timeout_seconds`` and are
    not the unbounded wait this file is about.
    """
    import core_api.middleware.per_tenant_concurrency as ptc

    real = ptc._get_semaphore

    def _pick(scope, tenant_id):
        if scope.startswith("storage_"):
            return _NeverAcquires()
        return real(scope, tenant_id)

    monkeypatch.setattr(ptc, "_get_semaphore", _pick)


# ---------------------------------------------------------------------------
# Drivers — one per roster entry, keyed by name
# ---------------------------------------------------------------------------


def _lower_middleware_budget(monkeypatch) -> None:
    """Lower the BLANKET budget on the live middleware instance.

    ``RequestTimeoutMiddleware`` captures ``request_timeout_seconds`` at
    ``add_middleware`` time, so patching the settings object does nothing once
    the stack is built. Reaching the instance is what lets the production stack
    be exercised at test speed; the three other surfaces read their budget from
    settings at call time and are lowered the way an operator would.
    """
    from core_api.app import app
    from core_api.middleware.request_timeout import RequestTimeoutMiddleware

    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None:
        if isinstance(node, RequestTimeoutMiddleware):
            monkeypatch.setattr(node, "timeout_seconds", _BUDGET_S)
            return
        node = getattr(node, "app", None)
    raise AssertionError(
        "RequestTimeoutMiddleware is not in the app's middleware stack — the "
        "blanket budget every non-opted-out route relies on is gone"
    )


def _lower_setting(monkeypatch, attr: str) -> None:
    from core_api import config as cfg

    assert hasattr(cfg.settings, attr), (
        f"{attr!r} is not a Settings field — the roster names a budget knob "
        "that does not exist, which is the dead-knob defect this file exists "
        "to prevent"
    )
    monkeypatch.setattr(cfg.settings, attr, _BUDGET_S)


async def _drive_rest_blanket(ctx) -> dict | None:
    _lower_middleware_budget(ctx.monkeypatch)
    # A minted tenant, not the shared ``default``: every surface here leaves
    # rows behind (a recall event, a memory), and ``default`` is the one tenant
    # the end-of-run sweep does NOT reclaim. Tests that measure it — the
    # embedding-coverage admin ones — read a total across the whole suite.
    tenant_id, headers = get_test_auth(new_tenant_id())
    resp = await ctx.client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": "who runs fleet ops"},
        headers=headers,
    )
    if resp.status_code != 504:
        return None
    return resp.json()["error"]["details"]


async def _drive_memories_bulk(ctx) -> dict | None:
    from core_api import config as cfg

    _lower_setting(ctx.monkeypatch, "bulk_request_timeout_seconds")
    # Above the route budget so the ROUTE deadline is what fires; the storage
    # phase cap is a different deadline with a different owner.
    ctx.monkeypatch.setattr(cfg.settings, "storage_bulk_timeout_seconds", 30.0)
    # Minted, for the reason given on the blanket driver: this one commits a
    # row, and on ``default`` it would never be swept.
    tenant_id, headers = get_test_auth(new_tenant_id())
    resp = await ctx.client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"guard-{uid()}",
            "items": [{"content": f"guard-content-{uid()}"}],
        },
        headers={**headers, "X-Bulk-Attempt-Id": f"guard-{uid()}"},
    )
    if resp.status_code != 504:
        return None
    return resp.json()["error"]["details"]


async def _drive_interview_submit(ctx) -> dict | None:
    from core_api import config as cfg

    ctx.monkeypatch.setattr(cfg.settings, "interview_async_submit", False)
    ctx.monkeypatch.setattr(cfg.settings, "storage_bulk_timeout_seconds", 30.0)
    tenant_id, headers = get_test_auth(new_tenant_id())
    # ``fake`` named explicitly: the suite's provider is ``none`` in CI, and with
    # no LLM the window now fails before it reaches the bulk write (M-50).
    enable = await ctx.client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"interviewer": {"enabled": True}, "enrichment": {"provider": "fake"}},
        headers=headers,
    )
    assert enable.status_code == 200, enable.text
    # The route refuses a node_id that is not a node of the tenant.
    node = await ctx.client.post(
        "/api/v1/fleet/heartbeat",
        json={"tenant_id": tenant_id, "node_name": f"node-{uid()}"},
        headers=headers,
    )
    assert node.status_code == 200, node.text
    # Lowered AFTER the settings write and the heartbeat, which are themselves
    # requests on this app.
    _lower_setting(ctx.monkeypatch, "interview_request_timeout_seconds")
    base = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
    resp = await ctx.client.post(
        "/api/v1/interview/submit",
        json={
            "tenant_id": tenant_id,
            "node_id": node.json()["node_id"],
            "agent_id": f"agent-{uid()}",
            "command_id": "cmd-1",
            "cursor_from": 0,
            "cursor_to": 10,
            "events": [
                {
                    "seq": i,
                    "ts": (base + timedelta(minutes=i)).isoformat(),
                    "session_id": "sess-1",
                    "role": "assistant",
                    "kind": "message",
                    "content": f"Worked on step {i}: refactored the ingest pipeline.",
                }
                for i in range(3)
            ],
        },
        headers=headers,
    )
    if resp.status_code != 504:
        return None
    # ``coded_detail`` keeps ``detail`` a plain sentence for deployed clients
    # and puts the structure alongside it, so the attribution is read from the
    # same place on all four surfaces.
    return resp.json()["error"]["details"]


async def _drive_mcp_tools_call(ctx) -> dict | None:
    """Drive ``tools/call`` — the single dispatch point a JSON-RPC call runs.

    ``caura_recall`` is the tool oss-0924-h-02 is about: it reaches the
    storage_search bulkhead through ``search_memories``, and nothing is stubbed
    between the two, so the acquire under observation is the production one.
    """
    import json

    from core_api import mcp_server
    from tests._mcp_test_helpers import as_text, is_error_envelope

    _lower_setting(ctx.monkeypatch, "mcp_request_timeout_seconds")
    try:
        result = await mcp_server.mcp.call_tool(
            "caura_recall", {"query": "who runs fleet ops"}
        )
    except TimeoutError:
        # A ``TimeoutError`` reaching the caller means the dispatch frame's
        # clock fired but the recorder could not confirm the deadline was
        # ours, so the envelope was never built. Reported as "no timeout" so
        # the caller's own message explains it rather than a bare traceback.
        return None
    if not is_error_envelope(result):
        return None
    payload = json.loads(as_text(result))
    return payload.get("error", {}).get("details")


async def _drive_admin_org_purge_data(ctx) -> dict | None:
    _tenant_id, headers = get_test_auth()  # admin key; the body names the target
    resp = await ctx.client.post(
        "/api/v1/admin/org/purge-data",
        # A tenant that has never existed: the purge is a no-op over empty
        # tables, which is all this surface needs to be to be observed.
        json={"tenant_ids": [str(uuid.uuid4())]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return None


_DRIVERS = {
    "rest.blanket": _drive_rest_blanket,
    "rest.memories_bulk": _drive_memories_bulk,
    "rest.interview_submit": _drive_interview_submit,
    "mcp.tools_call": _drive_mcp_tools_call,
    "rest.admin_org_purge_data": _drive_admin_org_purge_data,
}


@dataclass
class _Ctx:
    client: object
    monkeypatch: pytest.MonkeyPatch


@pytest.fixture
def ctx(client, mcp_env, monkeypatch):
    """Everything a driver may need, so one parametrised test covers all of them.

    ``mcp_env`` bypasses MCP auth, trust and metering only; it leaves
    ``search_memories`` and the storage routing alone, which is why the MCP
    driver reaches the real bulkhead. It is harmless for the REST drivers
    because it patches nothing outside ``core_api.mcp_server``.
    """
    return _Ctx(client=client, monkeypatch=monkeypatch)


# ---------------------------------------------------------------------------
# 1 — behaviour: the deadline is armed where the unbounded wait happens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ARMED, ids=lambda s: s.name)
async def test_a_deadline_is_armed_at_the_unbounded_wait(surface, ctx):
    """Read the recorder AT the acquire, on the real path, per surface.

    Not "a recorder is bound": ``own_deadline(None)`` binds one with no clock,
    and that is precisely the state the MCP transport shipped in between #1715
    and #1726 — instrumented, attributed, and capped by nothing. The assertion
    is that ``_deadline`` holds a number and that number is still ahead of us
    when the wait is entered.
    """
    seen = _watch_acquires(ctx.monkeypatch)
    await _DRIVERS[surface.name](ctx)

    storage = [a for a in seen if a.scope.startswith("storage_")]
    assert storage, (
        f"{surface.name} reached no per_tenant_storage_slot acquire, so this "
        "test proved nothing about it. Either the surface no longer reaches "
        "the unbounded wait — in which case move it to UNARMED with that as "
        f"the recorded reason — or its driver stopped exercising it. {surface.why}"
    )
    for acquire in storage:
        assert acquire.armed, (
            f"{surface.name} entered the unbounded {acquire.scope} queue with "
            "NO request-scoped deadline armed. per_tenant_storage_slot queues "
            "unboundedly and names the request budget as its only cap; on this "
            f"surface that cap does not exist. Arm one ({surface.budget_setting}), "
            "or move this surface to UNARMED and say what bounds it instead."
        )
        assert acquire.remaining is not None and acquire.remaining > 0, (
            f"{surface.name} armed a deadline that had already passed before "
            "the wait began — a recorder, not a budget"
        )


# ---------------------------------------------------------------------------
# 2 — behaviour: the armed deadline actually CANCELS that wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ARMED, ids=lambda s: s.name)
async def test_the_armed_deadline_cancels_the_unbounded_wait(surface, ctx):
    """Saturate the bulkhead and prove the surface still ends, and says where.

    The half that cannot pass vacuously. An armed ContextVar is a claim; a
    queue that never drains and a surface that returns anyway at the budget is
    the property the bulkhead docstring asserts. The attribution is checked too,
    because a surface that ended for some OTHER reason at roughly the right
    time would otherwise read as a pass.
    """
    _saturate_storage_slots(ctx.monkeypatch)

    started = time.monotonic()
    try:
        # The ceiling is enforced HERE as well as asserted below: without it a
        # surface whose deadline has gone missing hangs for the length of the
        # stall instead of failing, and a slow red is a red people stop running.
        details = await asyncio.wait_for(
            _DRIVERS[surface.name](ctx), _TERMINATION_CEILING_S
        )
    except TimeoutError:
        details = None
    elapsed = time.monotonic() - started

    assert details is not None, (
        f"{surface.name} did not report a budget timeout in {elapsed:.2f}s "
        f"against a {_BUDGET_S}s budget, with a queue that never drains. "
        "Either it no longer reaches the unbounded wait, or that wait is no "
        "longer capped by this surface's deadline — which is what "
        "per_tenant_storage_slot's unbounded acquire is relying on."
    )
    assert details.get("budget_seconds") == _BUDGET_S, (
        f"{surface.name} did not time out at the budget this test lowered via "
        f"{surface.budget_setting!r}. A knob the surface does not read is a "
        "knob an operator cannot move either."
    )
    assert details.get("phase") == surface.expected_phase, (
        f"{surface.name} blew its budget in {details.get('phase')!r}, not in "
        f"{surface.expected_phase!r} — the saturated queue was not the wait "
        "that ate it, so this run did not exercise the hazard"
    )


# ---------------------------------------------------------------------------
# 3 — the recorded exclusion, re-derived rather than trusted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", UNARMED, ids=lambda s: s.name)
async def test_the_recorded_exclusion_still_holds(surface, ctx):
    """An exclusion is a claim too, and claims are what this file checks.

    ``/admin/org/purge-data`` is excluded because it reaches no unbounded
    acquire — not because someone decided it was fine. If it ever routes
    through ``per_tenant_storage_slot``, the reason recorded on its roster
    entry stops being true and this fails, naming it.
    """
    seen = _watch_acquires(ctx.monkeypatch)
    await _DRIVERS[surface.name](ctx)

    storage = [a.scope for a in seen if a.scope.startswith("storage_")]
    assert not storage, (
        f"{surface.name} now reaches the unbounded {storage} queue, which the "
        "reason it was excluded says it does not. Give it a deadline and move "
        f"it to ARMED, or record a new reason. Old reason: {surface.why}"
    )


# ---------------------------------------------------------------------------
# 4 — structure: nothing slips past the blanket budget undeclared
# ---------------------------------------------------------------------------


async def _middleware_arms_for(path: str) -> bool:
    """Run the REAL middleware over ``path`` and report whether it armed.

    Driven rather than re-implemented on purpose: a test that re-stated
    ``is_mcp_path(...) or _is_opted_out(...)`` would agree with the middleware
    by construction and would keep agreeing after the middleware changed. This
    asks the object itself.
    """
    from core_api import request_phase
    from core_api.middleware.request_timeout import RequestTimeoutMiddleware

    armed: list[bool] = []

    async def _inner(scope, receive, send):
        recorder = request_phase.current()
        armed.append(recorder is not None and recorder._deadline is not None)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _receive():  # pragma: no cover - the stub never reads a body
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(_message):
        return None

    middleware = RequestTimeoutMiddleware(_inner, timeout_seconds=30.0)
    await middleware(
        {"type": "http", "path": path, "method": "GET", "headers": []},
        _receive,
        _send,
    )
    assert armed, f"the middleware never reached its inner app for {path!r}"
    return armed[0]


async def test_no_http_route_slips_past_the_blanket_budget_undeclared():
    """Every documented path, run through the middleware, one at a time.

    This is the enumeration that makes adding an opt-out route the thing that
    breaks the build. Paths are driven as the schema spells them, templates and
    all, which is sound because the bypass predicate matches the literal path:
    a template can no more equal an opt-out entry than a real request to a
    different route can.
    """
    from core_api.app import app

    unarmed = set()
    for path in app.openapi().get("paths", {}):
        if not await _middleware_arms_for(path):
            unarmed.add(path)

    assert unarmed == _DECLARED_BYPASSES & unarmed, (
        "these routes are not covered by the blanket request budget and are "
        f"not in the roster: {sorted(unarmed - _DECLARED_BYPASSES)}. A route "
        "that opts out must arm its own deadline (request_phase.own_deadline) "
        "and be added to ARMED, or be added to UNARMED with what bounds it "
        "instead — per_tenant_storage_slot queues unboundedly and names this "
        "budget as its only cap."
    )
    missing = _DECLARED_BYPASSES - unarmed - {"/mcp"}
    assert not missing, (
        f"the roster says {sorted(missing)} bypass the blanket budget, but the "
        "middleware arms for them. The entry is stale: a surface that IS "
        "covered by the blanket budget does not need its own."
    )


async def test_no_asgi_mount_slips_past_the_blanket_budget_undeclared():
    """The MCP case, as a rule rather than as one remembered exception.

    MCP is not an OpenAPI path — it is a mounted ASGI app, which is exactly why
    the middleware's ``is_mcp_path`` skip went unexamined for months. A second
    mount added the same way would be invisible to the test above; it is not
    invisible to this one.
    """
    from starlette.routing import Mount

    from core_api.app import app

    mounts = [r for r in app.routes if isinstance(r, Mount)]
    assert mounts, "no ASGI mounts found — this test would pass vacuously"

    unarmed = {m.path for m in mounts if not await _middleware_arms_for(m.path)}
    assert unarmed <= _DECLARED_BYPASSES, (
        f"these ASGI mounts bypass the blanket request budget undeclared: "
        f"{sorted(unarmed - _DECLARED_BYPASSES)}. A transport the middleware "
        "skips must enforce its own deadline per dispatch — the way "
        "_InstrumentedMCPServer.call_tool does (oss-0924-h-02) — and be added "
        "to ARMED here."
    )


# ---------------------------------------------------------------------------
# 5 — the roster itself cannot rot quietly
# ---------------------------------------------------------------------------


def test_every_roster_entry_is_actually_driven():
    """A roster entry with no driver would be a comment again."""
    assert {s.name for s in ROSTER} == set(_DRIVERS), (
        "roster and drivers disagree: "
        f"undriven={sorted({s.name for s in ROSTER} - set(_DRIVERS))} "
        f"orphaned={sorted(set(_DRIVERS) - {s.name for s in ROSTER})}"
    )
    assert len({s.name for s in ROSTER}) == len(ROSTER), "duplicate surface name"
    for surface in ARMED:
        assert surface.budget_setting and surface.expected_phase, (
            f"{surface.name} claims to arm a deadline but names no budget "
            "setting or no phase to check it at"
        )
    for surface in UNARMED:
        assert surface.budget_setting is None, (
            f"{surface.name} is recorded as unarmed but names a budget setting"
        )
        assert len(surface.why) > 80, (
            f"{surface.name} is an exclusion with no reason recorded; an "
            "exclusion without one is an omission"
        )


def test_the_opt_out_list_and_the_roster_name_the_same_routes():
    """The middleware's own opt-out set, checked against the roster by NAME.

    The behavioural tests above would not notice a route added to
    ``_TIMEOUT_OPT_OUT_PATHS`` that happens never to reach the bulkhead today —
    and "today" is how the MCP gap survived. This pins the set itself.
    """
    from core_api.middleware.request_timeout import _TIMEOUT_OPT_OUT_PATHS

    rest_bypasses = _DECLARED_BYPASSES - {"/mcp"}
    assert rest_bypasses == set(_TIMEOUT_OPT_OUT_PATHS), (
        "the timeout opt-out set and this roster disagree: "
        f"undeclared={sorted(set(_TIMEOUT_OPT_OUT_PATHS) - rest_bypasses)} "
        f"stale={sorted(rest_bypasses - set(_TIMEOUT_OPT_OUT_PATHS))}"
    )


# ---------------------------------------------------------------------------
# 6 — background callers: no request, so their OWN budget is what is checked
# ---------------------------------------------------------------------------

# Callers of the unbounded acquire that no request budget can reach. Separate
# from ``ROSTER`` because nothing here has an ASGI path or a request recorder;
# what they must prove instead is that their own budget cancels the wait.
BACKGROUND: tuple[Surface, ...] = (
    Surface(
        name="background.audit_flusher",
        path=None,
        budget_setting="audit_flush_slot_timeout_seconds",
        # No request recorder on a background loop, so no phase to read.
        expected_phase=None,
        why=(
            "AuditEventQueue's flusher gathers _flush_one_tenant over every "
            "tenant in a chunk. Uncapped, one tenant with saturated "
            "storage_write slots held the whole flush cycle until the queue "
            "filled and dropped every tenant's events (oss-0927-m-04)."
        ),
    ),
)


class _RecordingStorage:
    """Stands in for the storage client; records what reached the POST."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.posted: list[list[dict]] = []

    async def create_audit_logs_bulk(self, events: list[dict]) -> dict:
        await asyncio.sleep(self.delay_s)
        self.posted.append(events)
        return {"inserted": len(events)}


async def test_the_audit_flusher_budget_cancels_the_unbounded_wait(monkeypatch):
    """A saturated tenant must fail its own slice at the budget, not park.

    ``_flush_audit_batch`` gathers this per tenant, so a slice that parks here
    parks the whole flush cycle with it. Run as a task and bounded by the
    ceiling from outside, so a missing budget reads as "still running", which
    cannot be mistaken for the budget's own ``TimeoutError``.
    """
    import core_api.app as app_module

    (surface,) = BACKGROUND
    _saturate_storage_slots(monkeypatch)
    _lower_setting(monkeypatch, surface.budget_setting)
    storage = _RecordingStorage()
    monkeypatch.setattr(app_module, "get_storage_client", lambda: storage)

    started = time.monotonic()
    task = asyncio.create_task(
        app_module._flush_one_tenant(new_tenant_id(), [{"action": "x"}])
    )
    done, _ = await asyncio.wait({task}, timeout=_TERMINATION_CEILING_S)
    elapsed = time.monotonic() - started
    if not done:
        task.cancel()
    assert done, (
        f"{surface.name} was still waiting on a storage_write slot after "
        f"{elapsed:.2f}s against a {_BUDGET_S}s {surface.budget_setting}. Its "
        f"acquire is uncapped again. {surface.why}"
    )
    assert isinstance(task.exception(), TimeoutError), task.exception()
    assert elapsed >= _BUDGET_S * 0.9, (
        f"{surface.name} gave up after {elapsed:.2f}s, before the {_BUDGET_S}s "
        "budget — something other than the budget ended the wait"
    )
    assert storage.posted == [], "a slot that was never acquired reached storage"


async def test_the_audit_flusher_budget_is_disarmed_once_the_slot_is_held(
    monkeypatch,
):
    """The budget caps the WAIT, not the write.

    Cancelling a POST already in flight would drop a slice storage may have
    committed and count it lost. The POST here outlasts the budget on purpose,
    through the real (unsaturated) bulkhead, and must still land.
    """
    import core_api.app as app_module

    (surface,) = BACKGROUND
    _lower_setting(monkeypatch, surface.budget_setting)
    storage = _RecordingStorage(delay_s=_BUDGET_S * 2)
    monkeypatch.setattr(app_module, "get_storage_client", lambda: storage)

    events = [{"action": "x"}]
    await asyncio.wait_for(
        app_module._flush_one_tenant(new_tenant_id(), events),
        _TERMINATION_CEILING_S,
    )
    assert storage.posted == [events]


def test_the_audit_flusher_reaches_the_bulkhead_only_through_the_gated_helper():
    """Pin the flusher's one acquire to the function the tests above drive.

    The lifespan builds the flusher's closures; a slot acquire inlined there
    again would be a background caller these tests never exercise.
    """
    import inspect

    import core_api.app as app_module

    lifespan_src = inspect.getsource(app_module.lifespan)
    assert "per_tenant_storage_slot" not in lifespan_src, (
        "the lifespan acquires a per_tenant_storage_slot directly again; route "
        "it through _flush_one_tenant (budgeted) or add it to BACKGROUND"
    )
    assert "_flush_one_tenant(" in lifespan_src, (
        "the audit flusher no longer calls _flush_one_tenant, so BACKGROUND "
        "drives a helper nothing uses; re-point the roster at the real caller"
    )
    helper_src = inspect.getsource(app_module._flush_one_tenant)
    assert BACKGROUND[0].budget_setting in helper_src


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_a_non_positive_audit_flusher_budget_refuses_to_boot(value):
    """``asyncio.timeout(0)`` would drop every slice on every flush."""
    from core_api.config import Settings

    with pytest.raises(ValueError, match="audit_flush_slot_timeout_seconds"):
        Settings(audit_flush_slot_timeout_seconds=value)
