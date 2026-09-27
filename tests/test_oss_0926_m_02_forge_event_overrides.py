"""oss-0926-m-02 — a ``dry_run=True`` forge-distill event must not run for real.

``LifecycleForgeDistillRequest`` declares five per-run override fields. The
consumer reads none of them: ``forge_distill_op`` passed only ``org_id``,
``fleet_id`` and ``run_label`` to the adapter, and the tick resolves every
bound it applies from ``org_settings.skills_factory.forge.*``.

Four of the five fail SAFE when ignored — the run falls through to the
tenant's configured bounds, which an operator already chose. Those are
documented as inert at their declaration and pinned inert here.

``dry_run`` fails OPEN, which is what separates it. Its declared meaning was
"produce candidates with ``status=candidate`` only; do not run the
staged-promotion auto-gates", and ``run_forge_cron_tick`` runs
``promote_pending_candidates`` unconditionally after mining — so an ignored
dry run wrote real candidates AND promoted them, and with
``skills_factory.sentinel.auto_promote_clean`` enabled promotion can carry a
skill past ``staged`` to ``active``. The one flag whose entire purpose is to
prevent side effects produced the largest side effect available.

WHY REFUSED RATHER THAN HONOURED. Skipping the promotion half is easy; that
is not the cost. A dry run that finishes normally writes a ``success`` row
for action ``forge-distill``, and the shared runner's 23h dedup gate reads
that row as "already done" and skips the tenant's next REAL tick. An honest
implementation therefore has to change a dedup contract that five other
lifecycle actions share. Refusing is the smaller and safer statement: the
event is recorded as a terminal failure and nothing runs.

WHY IT WAS MISSABLE, recorded because the mechanism matters more than the
field. Two comments used to say the knobs were unconsumed — the adapter
protocol's ("extra run-knobs ... are intentionally NOT plumbed through the
adapter for the Phase 0 stub") and ``forge_distill_op``'s. #311 deleted both
while wiring the real tick, truncating the first mid-clause. Nothing was
wrong with the code that commit shipped; what it removed was the only
description of what the code did not do.
"""

from __future__ import annotations

from functools import partial

import pytest

from common.events.base import Event, PermanentOpError
from common.events.lifecycle_forge_request import LifecycleForgeDistillRequest
from common.events.lifecycle_handlers import _run_action, register_pipeline_consumers
from common.events.topics import Topics


class _FakeAdapter:
    """Records what reached the Forge tick. ``forge_distill`` standing in
    for the real adapter is the whole assertion surface: the run happening
    at all is the failure being tested for.
    """

    def __init__(self, *, has_recent_success: bool = False) -> None:
        self.forge_calls: list[dict] = []
        self.audit_calls: list[tuple[int, str, dict | None, str | None]] = []
        self.has_recent_success_value = has_recent_success

    async def forge_distill(
        self, *, org_id: str, fleet_id: str | None, run_label: str
    ) -> int:
        self.forge_calls.append(
            {"org_id": org_id, "fleet_id": fleet_id, "run_label": run_label}
        )
        return 3

    async def has_recent_lifecycle_success(
        self, *, org_id: str, action: str, since_hours: int
    ) -> bool:
        return self.has_recent_success_value

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        org_id: str,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
        claim_token: str | None = None,
    ) -> dict:
        self.audit_calls.append((audit_id, status, stats, error_message))
        return {}


def _forge_event(**overrides) -> Event:
    payload = LifecycleForgeDistillRequest(
        audit_id=4242,
        org_id="tenant-x",
        triggered_by="manual:operator",
        fleet_id="fleet-1",
        run_label="forge-dry-run-tenant-x-20260926T1200",
        **overrides,
    ).model_dump(mode="json")
    return Event(event_type=Topics.Lifecycle.FORGE_DISTILL_REQUESTED, payload=payload)


def _real_consumer(adapter: _FakeAdapter):
    """Capture the REAL ``forge_distill_op`` closure off the registration.

    Rebuilding an equivalent op in the test — which is how the neighbouring
    lifecycle handler tests bind their actions — would exercise the test's
    own copy of the guard and pass with the guard absent from the shipped
    closure, so this registers the production consumers and pulls the
    subscribed handler back out.
    """
    captured: list = []

    class _RecordingBus:
        def subscribe(self, topic, handler):
            if topic == Topics.Lifecycle.FORGE_DISTILL_REQUESTED:
                captured.append(handler)

    import common.events.lifecycle_handlers as lh

    original = lh.get_event_bus
    lh.get_event_bus = lambda: _RecordingBus()  # type: ignore[assignment]
    try:
        register_pipeline_consumers(adapter)
    finally:
        lh.get_event_bus = original  # type: ignore[assignment]

    assert captured, "the forge-distill consumer was never registered"
    return captured[0]


# ── the refusal ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dry_run_event_does_not_perform_a_real_run():
    """The row's headline, in executable form."""
    adapter = _FakeAdapter()
    handler = _real_consumer(adapter)

    await handler(_forge_event(dry_run=True))

    assert adapter.forge_calls == [], (
        "a dry_run=True event reached the Forge tick. The tick has no dry-run "
        "mode and promotes after mining, so this run wrote real candidates and "
        "promoted them — the exact side effects the flag exists to prevent."
    )


@pytest.mark.asyncio
async def test_the_refusal_is_terminal_and_not_redelivered():
    """A refusal that nacks would redeliver forever and DLQ.

    ``PermanentOpError`` is the established idiom for "a retry cannot help":
    the runner writes the ``failure`` row with ``stats={"terminal": True}``
    and acks. Pinned because a later edit swapping it for a plain raise would
    still block the run — this test is what would notice the queue damage.
    """
    adapter = _FakeAdapter()
    handler = _real_consumer(adapter)

    # Does not raise: the runner acks a permanent failure.
    await handler(_forge_event(dry_run=True))

    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "failure"]
    audit_id, _status, stats, error_message = adapter.audit_calls[-1]
    assert audit_id == 4242
    assert stats == {"terminal": True}, (
        "the refusal must be marked terminal in the durable record, or it is "
        "indistinguishable from a transient failure awaiting retry"
    )
    assert error_message and "dry_run" in error_message


@pytest.mark.asyncio
async def test_the_op_raises_permanent_op_error_and_not_some_other_error():
    """The exception TYPE, which the runner's ack/nack choice turns on.

    ``_run_action`` acks a ``PermanentOpError`` and therefore swallows it, so
    the type is invisible from outside — except on the one branch where the
    failure row could not be written (``recorded=False``), which falls through
    to a re-raise. An adapter that fails that write exposes the real
    exception, so the assertion is on the op's own contract rather than on a
    reconstruction of it.
    """

    class _UnrecordableAdapter(_FakeAdapter):
        async def update_lifecycle_audit_row(self, audit_id, **kwargs) -> dict:
            if kwargs.get("status") == "failure":
                raise RuntimeError("storage down")
            return {}

    adapter = _UnrecordableAdapter()
    handler = _real_consumer(adapter)

    with pytest.raises(PermanentOpError, match="dry_run"):
        await handler(_forge_event(dry_run=True))
    assert adapter.forge_calls == []


# ── the normal path is untouched ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_production_event_still_runs():
    """``dry_run`` defaults False, which is every event published today —
    ``resolve_publisher_kwargs`` sets only ``run_label``. The guard must cost
    the cron path nothing.
    """
    adapter = _FakeAdapter()
    handler = _real_consumer(adapter)

    await handler(_forge_event())

    assert adapter.forge_calls == [
        {
            "org_id": "tenant-x",
            "fleet_id": "fleet-1",
            "run_label": "forge-dry-run-tenant-x-20260926T1200",
        }
    ]
    assert [c[1] for c in adapter.audit_calls] == ["in_progress", "success"]
    assert adapter.audit_calls[-1][2] == {"candidates_produced": 3}


@pytest.mark.asyncio
async def test_dry_run_defaults_to_false_so_the_guard_is_opt_in():
    """A payload with no ``dry_run`` key at all — the rolling-deploy shape,
    since ``extra="ignore"`` means an older publisher's message simply omits
    it — must not trip the refusal.
    """
    adapter = _FakeAdapter()
    handler = _real_consumer(adapter)

    await handler(
        Event(
            event_type=Topics.Lifecycle.FORGE_DISTILL_REQUESTED,
            payload={
                "audit_id": 7,
                "org_id": "tenant-x",
                "triggered_by": "core-operations",
                "run_label": "forge-cron-tenant-x-20260926T0600",
            },
        )
    )

    assert len(adapter.forge_calls) == 1


# ── the four inert fields ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_value_overrides_are_inert_and_that_is_deliberate():
    """Pins the (b) half of this row: the four value knobs are NOT honoured.

    Asserting the current behaviour rather than a fix, on purpose. If someone
    later threads them through, this test fails and sends them to the
    declaration comment that currently tells readers the fields do nothing —
    so the comment cannot silently become false again the way it did in #311.
    """
    adapter = _FakeAdapter()
    handler = _real_consumer(adapter)

    await handler(
        _forge_event(
            freshness_window_days=999,
            min_cluster_size=1,
            min_distinct_agents=1,
            max_writes_per_run=10_000,
        )
    )

    assert adapter.forge_calls == [
        {
            "org_id": "tenant-x",
            "fleet_id": "fleet-1",
            "run_label": "forge-dry-run-tenant-x-20260926T1200",
        }
    ], (
        "the adapter signature took an override. If that is intended, update "
        "LifecycleForgeDistillRequest's declaration — it currently tells "
        "readers these four fields are inert."
    )


@pytest.mark.unit
def test_the_declaration_says_the_fields_are_not_consumed():
    """A source check, which this suite normally avoids — justified here
    because the comment IS the fix for the four inert fields, and an unpinned
    comment is exactly what rotted: #311 deleted the two that said so.
    """
    from pathlib import Path

    import common.events.lifecycle_forge_request as mod

    src = Path(mod.__file__).read_text()
    assert "NOT CONSUMED" in src
    assert "REFUSED" in src
    for field in (
        "freshness_window_days",
        "min_cluster_size",
        "min_distinct_agents",
        "max_writes_per_run",
    ):
        assert field in src


@pytest.mark.unit
def test_max_clusters_per_run_is_still_absent_from_the_event():
    """Settled deliberately, not overlooked.

    Post-#1687 it is the knob that actually bounds a run's spend, so adding it
    beside ``max_writes_per_run`` is the obvious move. It stays off while the
    overrides are unconsumed: a sixth declared-and-unread field is a sixth
    instance of this row. The control remains reachable per tenant through
    ``org_settings.skills_factory.forge.max_clusters_per_run``, which
    ``cron_handler._resolve_forge_config`` does read.
    """
    assert "max_clusters_per_run" not in LifecycleForgeDistillRequest.model_fields

    from core_api.services.organization_settings import DEFAULT_SETTINGS

    assert "max_clusters_per_run" in DEFAULT_SETTINGS["skills_factory"]["forge"], (
        "the event may omit the knob only while the settings path still "
        "carries it; otherwise the control is missing everywhere"
    )


@pytest.mark.unit
def test_nothing_on_the_cron_path_publishes_an_override():
    """The reason this row is medium and not high: the trap is armed but
    nothing has stepped on it. ``resolve_publisher_kwargs`` supplies
    ``run_label`` alone, and the manual route reads only ``org_id``,
    ``fleet_id`` and ``retention_days`` off the body.
    """
    import inspect

    from core_api.routes import lifecycle as route_mod
    from core_api.services.lifecycle_audit import resolve_publisher_kwargs

    resolver_src = inspect.getsource(resolve_publisher_kwargs)
    assert "dry_run" not in resolver_src
    body_keys = set(
        __import__("re").findall(
            r'body\.get\("([^"]+)"\)', inspect.getsource(route_mod)
        )
    )
    assert body_keys == {"org_id", "fleet_id", "retention_days"}, (
        f"the manual lifecycle route grew a body key: {body_keys}. If it can "
        "now set dry_run, the refusal above becomes reachable from an "
        "operator curl and this row's severity changes."
    )


@pytest.mark.asyncio
async def test_a_dry_run_inside_the_dedup_window_is_skipped_not_run():
    """Documented consequence of guarding inside the op rather than before
    the runner: the dedup gate short-circuits first. Both paths are
    side-effect-free, which is the property that matters, so the gate is left
    alone — but the behaviour is pinned so it is a choice and not a surprise.
    """
    adapter = _FakeAdapter(has_recent_success=True)
    handler = partial(
        _run_action,
        adapter=adapter,
        payload_cls=LifecycleForgeDistillRequest,
        run_op=_real_op(adapter),
        stats_key="candidates_produced",
        action="forge-distill",
        dedup_window_hours=23,
    )

    await handler(_forge_event(dry_run=True))

    assert adapter.forge_calls == []
    assert adapter.audit_calls[-1][1] == "success"
    assert adapter.audit_calls[-1][2] == {"skipped": True, "reason": "recent_success"}


def _real_op(adapter: _FakeAdapter):
    """The production ``forge_distill_op`` closure, unbound from its runner."""
    captured: list = []

    class _RecordingBus:
        def subscribe(self, topic, handler):
            if topic == Topics.Lifecycle.FORGE_DISTILL_REQUESTED:
                captured.append(handler)

    import common.events.lifecycle_handlers as lh

    original = lh.get_event_bus
    lh.get_event_bus = lambda: _RecordingBus()  # type: ignore[assignment]
    try:
        register_pipeline_consumers(adapter)
    finally:
        lh.get_event_bus = original  # type: ignore[assignment]

    # ``partial`` keyword, reached through the registered handler's own
    # binding so the op under test is the shipped one.
    return captured[0].keywords["run_op"]
