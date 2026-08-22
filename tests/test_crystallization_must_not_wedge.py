"""Crystallization must never leave its report row stuck 'running' (OSS #817).

``run_crystallization`` short-circuits on whatever ``find_running_report``
returns, so a report row that never reaches a terminal status disables
crystallization for that tenant — permanently, for every trigger, until someone
edits the row by hand.

The escape route was an uncaught ``HTTPException``. ``create_memory`` raises 409
for a duplicate, and the crystallizer's per-fact handler caught only
``(SQLAlchemyError, ValueError, GoogleAPIError)`` while the outer handler caught
only ``(SQLAlchemyError, httpx.HTTPError, ValueError, RuntimeError)``. An
``HTTPException`` is in neither, so it propagated past both and aborted the run
BEFORE ``update_report``.

And that 409 is expected by construction, not incidental: a crystallized fact is
a near-verbatim merge of cluster members that are >=0.95 similar to one another
and still ACTIVE at create time — sources are archived only after the create
loop. The fake-LLM path returns a member's content verbatim, so the exact
content-hash gate fires too, without semantic dedup even being enabled.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core_api.services.crystallizer_service import _run_crystallization, run_crystallization

pytestmark = [pytest.mark.unit]


def _pair(a: str, b: str) -> dict:
    """Near-duplicate pair in the shape ``_build_clusters`` reads (``id1``/``id2``)."""
    return {"id1": a, "id2": b}


def _memory_row(mid) -> dict:
    return {
        "id": str(mid),
        "content": f"content for {mid}",
        "memory_type": "fact",
        "status": "active",
    }


async def _stub_resolve_config(_tenant_id):
    return SimpleNamespace()


def _hygiene_with_one_cluster() -> tuple[dict, list[dict]]:
    a, b, c = uuid4(), uuid4(), uuid4()
    pairs = [_pair(str(a), str(b)), _pair(str(b), str(c)), _pair(str(a), str(c))]
    return {"near_duplicates": {"pairs": pairs}}, [
        _memory_row(a),
        _memory_row(b),
        _memory_row(c),
    ]


def _storage_mock(memories: list[dict]) -> AsyncMock:
    sc = AsyncMock()
    sc.bulk_get_memories = AsyncMock(return_value=memories)
    sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    return sc


# ---------------------------------------------------------------------------
# The per-fact 409 must be counted, not fatal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    ["Duplicate memory exists: abc", "Near-duplicate memory exists: abc"],
    ids=["exact-gate", "semantic-gate"],
)
async def test_a_duplicate_fact_is_counted_and_the_run_continues(detail) -> None:
    """Both dedup gates raise 409. Neither may end the run."""
    hygiene, memories = _hygiene_with_one_cluster()
    sc = _storage_mock(memories)

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch("core_api.services.organization_settings.resolve_config", _stub_resolve_config),
        patch(
            "core_api.services.crystallizer_service._crystallize_cluster",
            AsyncMock(return_value=[{"content": "crystallized", "memory_type": "fact", "weight": 0.8}]),
        ),
        patch(
            "core_api.services.memory_service.create_memory",
            AsyncMock(side_effect=HTTPException(status_code=409, detail=detail)),
        ),
    ):
        result = await _run_crystallization(tenant_id="t1", fleet_id=None, hygiene=hygiene)

    assert result["duplicate_facts"] == 1, (
        "a 409 was not counted, so a run that crystallized nothing is "
        "indistinguishable from one that found nothing"
    )
    assert result["new_memories"] == 0
    assert result["failed_facts"] == 0, "a duplicate is not a failure"


@pytest.mark.asyncio
async def test_a_non_409_rejection_is_counted_separately() -> None:
    """Counted apart from duplicates: one means "already stored", the other means
    the fact was lost, and an operator reading the report needs them distinct."""
    hygiene, memories = _hygiene_with_one_cluster()
    sc = _storage_mock(memories)

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch("core_api.services.organization_settings.resolve_config", _stub_resolve_config),
        patch(
            "core_api.services.crystallizer_service._crystallize_cluster",
            AsyncMock(return_value=[{"content": "crystallized", "memory_type": "fact", "weight": 0.8}]),
        ),
        patch(
            "core_api.services.memory_service.create_memory",
            AsyncMock(side_effect=HTTPException(status_code=422, detail="too short")),
        ),
    ):
        result = await _run_crystallization(tenant_id="t1", fleet_id=None, hygiene=hygiene)

    assert result["failed_facts"] == 1
    assert result["duplicate_facts"] == 0


@pytest.mark.asyncio
async def test_a_duplicate_does_not_stop_the_facts_after_it() -> None:
    """Per-fact isolation. The old handler let the first 409 abandon the rest of
    the cluster along with the whole run."""
    hygiene, memories = _hygiene_with_one_cluster()
    sc = _storage_mock(memories)
    facts = [
        {"content": "first", "memory_type": "fact", "weight": 0.8},
        {"content": "second", "memory_type": "fact", "weight": 0.8},
    ]
    calls: list[str] = []

    async def _create(payload):
        calls.append(payload.content)
        if payload.content == "first":
            raise HTTPException(status_code=409, detail="Duplicate memory exists: abc")
        return type("_MemOut", (), {"id": uuid4()})()

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch("core_api.services.organization_settings.resolve_config", _stub_resolve_config),
        patch(
            "core_api.services.crystallizer_service._crystallize_cluster",
            AsyncMock(return_value=facts),
        ),
        patch("core_api.services.memory_service.create_memory", AsyncMock(side_effect=_create)),
    ):
        result = await _run_crystallization(tenant_id="t1", fleet_id=None, hygiene=hygiene)

    assert calls == ["first", "second"], f"the 409 aborted the cluster: {calls}"
    assert result["duplicate_facts"] == 1
    assert result["new_memories"] == 1


# ---------------------------------------------------------------------------
# The report must never be left 'running'
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _driving(sc: AsyncMock):
    """Patches for driving ``run_crystallization`` end to end.

    ``_compute_health`` / ``_compute_usage`` are stubbed because against a bare
    mock they raise ``TypeError``, which their own handlers do not catch — so the
    guard would fire on that instead of on whatever the test is actually testing.
    Worth noting rather than hiding: those two handlers are as narrow as the ones
    this PR widens, and a TypeError there wedged the report exactly the same way
    before the guard existed. The guard is what makes it survivable.

    The hygiene checks are deliberately NOT stubbed — they fail against the mock
    and are caught by their own handler, which is the realistic shape and keeps
    the tests honest about what the guard is and is not responsible for.
    """
    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch("core_api.services.organization_settings.resolve_config", _stub_resolve_config),
        patch(
            "core_api.services.crystallizer_service._compute_health",
            AsyncMock(return_value={}),
        ),
        patch(
            "core_api.services.crystallizer_service._compute_usage",
            AsyncMock(return_value={}),
        ),
        # Stubbed so the ONLY exception in play is the one a test injects.
        # Unstubbed it raises TypeError against the mock, which its handler does
        # not catch either — real, but it would mask whatever the test injected.
        patch(
            "core_api.services.crystallizer_service._run_crystallization",
            AsyncMock(return_value={"enabled": True, "new_memories": 0}),
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_an_exception_marks_the_report_failed_instead_of_leaving_it_running() -> None:
    """The wedge itself. Anything escaping between ``create_report`` and
    ``update_report`` used to leave the row 'running' forever, and every later run
    short-circuited on it — so crystallization stayed off for the tenant.
    """
    report_id = str(uuid4())
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": report_id})
    sc.update_report = AsyncMock()

    boom = HTTPException(status_code=409, detail="Duplicate memory exists: abc")

    with (
        _driving(sc),
        # Raise from the issues step, which sits outside every inner handler's
        # tuple — the same shape as the original escape.
        patch(
            "core_api.services.crystallizer_service._generate_issues",
            MagicMock(side_effect=boom),
        ),
    ):
        with pytest.raises(HTTPException):
            await run_crystallization(tenant_id="t1", fleet_id=None)

    assert sc.update_report.await_count == 1, (
        "the report was never updated, so the row is still 'running' and "
        "crystallization is now permanently disabled for this tenant"
    )
    written = sc.update_report.await_args_list[0].args[1]
    assert written["status"] == "failed", f"expected status=failed, got {written!r}"
    assert written.get("completed_at"), "a terminal row must carry completed_at"


@pytest.mark.asyncio
async def test_the_original_exception_is_re_raised_unchanged() -> None:
    """The guard reports, it does not swallow. A caller must still see the fault,
    and the nightly trigger must still record a failure."""
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    sc.update_report = AsyncMock()
    # TypeError: outside every inner handler's tuple, so it reaches the guard.
    sentinel = TypeError("original cause")

    with (
        _driving(sc),
        patch(
            "core_api.services.crystallizer_service._generate_issues",
            MagicMock(side_effect=sentinel),
        ),
    ):
        with pytest.raises(TypeError) as caught:
            await run_crystallization(tenant_id="t1", fleet_id=None)

    assert caught.value is sentinel


@pytest.mark.asyncio
async def test_a_failure_to_mark_failed_does_not_mask_the_original() -> None:
    """Best-effort marking. If the marking write also fails there is nothing left
    to try, and its exception must not replace the one that actually broke the
    run — that would send an operator after the wrong system."""
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    sc.update_report = AsyncMock(side_effect=RuntimeError("storage down too"))
    sentinel = TypeError("original cause")

    with (
        _driving(sc),
        patch(
            "core_api.services.crystallizer_service._generate_issues",
            MagicMock(side_effect=sentinel),
        ),
    ):
        with pytest.raises(TypeError) as caught:
            await run_crystallization(tenant_id="t1", fleet_id=None)

    assert caught.value is sentinel, "the marking failure masked the real cause"


@pytest.mark.asyncio
async def test_a_cancelled_marking_write_does_not_mask_the_original() -> None:
    """Review round 2: the best-effort write caught only ``Exception``, so a
    ``CancelledError`` from the write itself escaped and replaced the exception
    being handled — the exact opposite of the guarantee.

    Reachable during a shutdown that cancelled the run to begin with: the second
    cancellation lands while the handler is already inside the marking write.
    """
    import asyncio

    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    sc.update_report = AsyncMock(side_effect=asyncio.CancelledError())
    sentinel = TypeError("original cause")

    with (
        _driving(sc),
        patch(
            "core_api.services.crystallizer_service._generate_issues",
            MagicMock(side_effect=sentinel),
        ),
    ):
        with pytest.raises(TypeError) as caught:
            await run_crystallization(tenant_id="t1", fleet_id=None)

    assert caught.value is sentinel, (
        "a CancelledError from the marking write replaced the real cause"
    )


@pytest.mark.asyncio
async def test_cancellation_also_marks_the_report_failed() -> None:
    """``BaseException``, not ``Exception``: a CancelledError wedges the row just
    as thoroughly, and it is MORE likely on the scheduled path, where the task can
    be torn down mid-run."""
    import asyncio

    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    sc.update_report = AsyncMock()

    with (
        _driving(sc),
        patch(
            "core_api.services.crystallizer_service._generate_issues",
            MagicMock(side_effect=asyncio.CancelledError()),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_crystallization(tenant_id="t1", fleet_id=None)

    assert sc.update_report.await_count == 1, "a cancelled run left the row 'running'"
    assert sc.update_report.await_args_list[0].args[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_a_clean_run_still_completes_normally() -> None:
    """The guard must not change the happy path."""
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    sc.update_report = AsyncMock()

    with (
        _driving(sc),
        # Stubbed only here: the hygiene checks run against a mock, so a
        # non-awaited stub value reaches ``_generate_issues`` and TypeErrors on a
        # comparison. That is a mock artefact, not the behaviour under test —
        # this test is about the guard leaving the happy path alone.
        patch("core_api.services.crystallizer_service._generate_issues", MagicMock(return_value=[])),
    ):
        await run_crystallization(tenant_id="t1", fleet_id=None, auto_crystallize=False)

    assert sc.update_report.await_count == 1
    assert sc.update_report.await_args_list[0].args[1]["status"] in {"completed", "failed"}
    # 'failed' is legitimate here — the hygiene checks run against an AsyncMock
    # storage client, so they may all report errors. What matters is that the row
    # reached a TERMINAL status rather than staying 'running'.


@pytest.mark.asyncio
async def test_a_running_report_still_short_circuits() -> None:
    """The lock is still a lock. Fixing the wedge must not turn the short-circuit
    off, or two runs could overlap on every trigger."""
    existing = str(uuid4())
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value={"id": existing})
    sc.create_report = AsyncMock()

    with _driving(sc):
        returned = await run_crystallization(tenant_id="t1", fleet_id=None)

    assert returned == existing
    sc.create_report.assert_not_awaited()


# ---------------------------------------------------------------------------
# The run must not be awaited by the request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_publishes_instead_of_running_inline() -> None:
    """``POST /crystallize`` answers ``status="running"``, which was false while
    the run was awaited — and once the run stopped aborting on the first duplicate
    it also stopped fitting in a request.

    PUBLISHED rather than scheduled as an asyncio task: a fire-and-forget task
    assumes the process keeps scheduling it after the response is flushed, which a
    runtime that allocates CPU per request does not guarantee. A starved task
    leaves the report 'running' — this bug again, by another route.
    """
    from core_api.services import crystallizer_service

    report_id = str(uuid4())
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": report_id})
    publish = AsyncMock()
    execute = AsyncMock()

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(
            crystallizer_service, "publish_crystallize_on_demand_request", new=publish
        ),
        patch.object(crystallizer_service, "_execute_crystallization", new=execute),
    ):
        returned = await crystallizer_service.start_crystallization("t1", "f1")

    assert returned == report_id
    execute.assert_not_awaited(), "the run happened inline instead of being queued"
    publish.assert_awaited_once_with(
        tenant_id="t1",
        report_id=report_id,
        fleet_id="f1",
        auto_crystallize=True,
    )


@pytest.mark.asyncio
async def test_the_reserved_report_is_executed_not_re_reserved() -> None:
    """The consumer side. It must run the row the API already reserved — reserving
    a second would leave the caller polling an id nothing ever finishes, which is
    the wedge reintroduced by the fix for it."""
    from core_api.services import crystallizer_service

    report_id = str(uuid4())
    sc = AsyncMock()
    sc.find_running_report = AsyncMock()
    sc.create_report = AsyncMock()
    execute = AsyncMock()

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(crystallizer_service, "_execute_crystallization", new=execute),
    ):
        await crystallizer_service.execute_reserved_report(
            report_id=report_id, tenant_id="t1", fleet_id="f1", auto_crystallize=False
        )

    sc.find_running_report.assert_not_awaited()
    sc.create_report.assert_not_awaited(), "the consumer reserved a second report"
    execute.assert_awaited_once()
    assert execute.await_args.args[1] == report_id


@pytest.mark.asyncio
async def test_start_does_not_schedule_a_second_run_when_one_is_in_flight() -> None:
    """The lock still holds on the scheduling path. Without this a caller could
    start N overlapping runs by posting N times."""
    from core_api.services import crystallizer_service

    existing = str(uuid4())
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value={"id": existing})
    sc.create_report = AsyncMock()
    execute = AsyncMock()

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(crystallizer_service, "_execute_crystallization", new=execute),
    ):
        returned = await crystallizer_service.start_crystallization("t1", None)

    assert returned == existing
    sc.create_report.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_inline_entry_point_still_awaits() -> None:
    """``run_crystallization`` keeps awaiting: the nightly lifecycle trigger has no
    request budget and wants the finished result. Splitting the entry points must
    not quietly make that one fire-and-forget too."""
    from core_api.services import crystallizer_service

    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    execute = AsyncMock()

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(crystallizer_service, "_execute_crystallization", new=execute),
    ):
        await crystallizer_service.run_crystallization("t1", None)

    execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# Round 2: the consumer must survive redelivery and malformed payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_a_redelivered_message_does_not_rerun_a_finished_report(status) -> None:
    """Pub/Sub is at-least-once and ``EventBus.subscribe`` says handlers must be
    idempotent. Redelivery is LIKELY here, not hypothetical: the run is the
    multi-minute one that made moving off the request necessary, so it can outlive
    the subscription's ack deadline and be redelivered mid-flight.

    Re-running would repeat every LLM call and overwrite a report that had already
    finished.
    """
    from core_api.services import crystallizer_service

    report_id = str(uuid4())
    sc = AsyncMock()
    sc.get_report = AsyncMock(return_value={"id": report_id, "status": status})
    execute = AsyncMock()

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(crystallizer_service, "_execute_crystallization", new=execute),
    ):
        await crystallizer_service.execute_reserved_report(
            report_id=report_id, tenant_id="t1", fleet_id=None, auto_crystallize=True
        )

    execute.assert_not_awaited(), f"a {status} report was re-run on redelivery"


@pytest.mark.asyncio
async def test_a_still_running_report_is_executed() -> None:
    """The other side: a reserved row that has NOT finished must actually run, or
    the idempotency check would turn every first delivery into a no-op."""
    from core_api.services import crystallizer_service

    report_id = str(uuid4())
    sc = AsyncMock()
    sc.get_report = AsyncMock(return_value={"id": report_id, "status": "running"})
    execute = AsyncMock()

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(crystallizer_service, "_execute_crystallization", new=execute),
    ):
        await crystallizer_service.execute_reserved_report(
            report_id=report_id, tenant_id="t1", fleet_id=None, auto_crystallize=True
        )

    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_vanished_report_is_dropped_not_recreated() -> None:
    """If the row is gone there is nothing to finish. Reserving a replacement
    would hand back an id the caller never asked for."""
    from core_api.services import crystallizer_service

    sc = AsyncMock()
    sc.get_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock()
    execute = AsyncMock()

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(crystallizer_service, "_execute_crystallization", new=execute),
    ):
        await crystallizer_service.execute_reserved_report(
            report_id=str(uuid4()), tenant_id="t1", fleet_id=None, auto_crystallize=True
        )

    execute.assert_not_awaited()
    sc.create_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_malformed_payload_is_dropped_not_redelivered_forever() -> None:
    """``_run_action`` drops on ``ValidationError`` because an unhandled exception
    nacks the delivery and redelivers forever / DLQs (audit M16). Bypassing that
    shared wrapper cost this handler the guard, so it has its own.

    The realistic trigger is a rolling deploy where publisher and consumer briefly
    disagree on the payload schema.
    """
    from common.events import lifecycle_handlers
    from common.events.base import Event
    from common.events.topics import Topics

    captured: list[str] = []
    adapter = MagicMock()
    adapter.crystallize_reserved_report = AsyncMock()

    class _RecordingBus:
        def subscribe(self, topic, handler):
            if topic == Topics.Lifecycle.CRYSTALLIZE_ON_DEMAND_REQUESTED:
                captured.append(handler)

    with patch.object(lifecycle_handlers, "get_event_bus", lambda: _RecordingBus()):
        lifecycle_handlers.register_pipeline_consumers(adapter)

    assert captured, "the on-demand consumer was never registered"
    handler = captured[0]

    # ``report_id`` missing — the shape a version-skewed publisher would send.
    await handler(
        Event(
            event_type=Topics.Lifecycle.CRYSTALLIZE_ON_DEMAND_REQUESTED,
            tenant_id="t1",
            payload={"tenant_id": "t1"},
        )
    )

    adapter.crystallize_reserved_report.assert_not_awaited(), (
        "a malformed payload reached the adapter instead of being dropped"
    )


@pytest.mark.asyncio
async def test_the_report_shape_is_the_same_whether_auto_curate_ran() -> None:
    """``duplicate_facts``/``failed_facts`` must be present either way, so a
    consumer reading them need not know which branch produced the row."""
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    sc.update_report = AsyncMock()

    with (
        _driving(sc),
        patch("core_api.services.crystallizer_service._generate_issues", MagicMock(return_value=[])),
    ):
        await run_crystallization(tenant_id="t1", fleet_id=None, auto_crystallize=False)

    written = sc.update_report.await_args_list[0].args[1]
    assert "duplicate_facts" in written["crystallization"]
    assert "failed_facts" in written["crystallization"]


# ---------------------------------------------------------------------------
# Round 3: reserving without queueing, and reading your own write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_publish_failure_fails_the_reserved_report() -> None:
    """The row is reserved BEFORE the publish. If the publish fails, nothing will
    ever execute that row — and leaving it 'running' blocks every later trigger
    for this tenant until the staleness cutoff expires.

    An hour of "crystallization does nothing", bought by one transient publish
    error: this bug's own shape, reached through the fix for it.
    """
    from core_api.services import crystallizer_service

    report_id = str(uuid4())
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": report_id})
    sc.update_report = AsyncMock()
    boom = RuntimeError("pubsub unreachable")

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(
            crystallizer_service,
            "publish_crystallize_on_demand_request",
            new=AsyncMock(side_effect=boom),
        ),
    ):
        with pytest.raises(RuntimeError) as caught:
            await crystallizer_service.start_crystallization("t1", None)

    assert caught.value is boom, "the caller must still see the publish failure"
    assert sc.update_report.await_count == 1, (
        "the reserved report was left 'running' with nothing to execute it"
    )
    written = sc.update_report.await_args_list[0].args[1]
    assert written["status"] == "failed"


@pytest.mark.asyncio
async def test_a_publish_failure_is_not_masked_by_the_marking_write() -> None:
    """Same best-effort rule as the run's own guard: the recovery write must not
    replace the error the caller needs to act on."""
    from core_api.services import crystallizer_service

    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    sc.update_report = AsyncMock(side_effect=RuntimeError("storage down too"))
    boom = RuntimeError("pubsub unreachable")

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(
            crystallizer_service,
            "publish_crystallize_on_demand_request",
            new=AsyncMock(side_effect=boom),
        ),
    ):
        with pytest.raises(RuntimeError) as caught:
            await crystallizer_service.start_crystallization("t1", None)

    assert caught.value is boom, "the marking failure masked the publish failure"


@pytest.mark.asyncio
async def test_the_idempotency_check_reads_the_writer_not_the_replica() -> None:
    """A read-your-write across a delivery boundary. The status being checked was
    written by the PREVIOUS delivery of this same message, so a lagging replica
    can answer 'running' for a report that already finished — and the whole
    multi-minute run happens again, which is the duplication the check exists to
    prevent. Same class as H-02 (#812).
    """
    from core_api.services import crystallizer_service

    report_id = str(uuid4())
    sc = AsyncMock()
    sc.get_report = AsyncMock(return_value={"id": report_id, "status": "running"})

    with (
        patch("core_api.services.crystallizer_service.get_storage_client", return_value=sc),
        patch.object(crystallizer_service, "_execute_crystallization", new=AsyncMock()),
    ):
        await crystallizer_service.execute_reserved_report(
            report_id=report_id, tenant_id="t1", fleet_id=None, auto_crystallize=True
        )

    assert sc.get_report.await_args.kwargs.get("read") is False, (
        "the idempotency check read the replica, so lag can re-run a finished report"
    )
