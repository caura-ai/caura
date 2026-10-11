"""Lost entity extractions are found and re-run (``services.extraction_rerun``).

``background_task_log`` records every extraction that did not end in the model's
graph: ``entity_extraction`` rows a run that raised (``failed``) or that a
shutdown stopped (``cancelled``), and ``entity_extraction_degraded`` rows a run
that settled for the regex heuristic. The table had writers and no reader, so
those memories stayed without the model's graph. These tests pin the reader:
storage lists a tenant's open rows and moves them on exactly once, the hourly
sweep re-runs the oldest across tenants a few at a time with a cap per memory,
and an operator can re-run one memory by hand.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from core_api.services import extraction_rerun as er
from tests.conftest import new_tenant_id

_SINCE = datetime.now(UTC) - timedelta(days=1)


async def _failure(
    sc,
    tenant: str,
    memory_id: str | None,
    task: str = "entity_extraction",
    status: str = "failed",
):
    await sc.add_task_failure(
        {
            "task_name": task,
            "memory_id": memory_id,
            "tenant_id": tenant,
            "error_message": f"{task} for the test",
            "error_traceback": "",
            "status": status,
        }
    )


async def _open(sc, tenant: str, **kw) -> list[dict]:
    return await sc.list_open_task_failures(
        tenant, er.LOST_EXTRACTION_TASKS, since=_SINCE, limit=100, **kw
    )


# ── Storage: the read half, and moving a row on ──


async def test_a_tenants_open_extraction_rows_are_listed_oldest_first(sc):
    tenant, other = new_tenant_id(), new_tenant_id()
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    await _failure(sc, tenant, first, "entity_extraction_degraded")
    await _failure(sc, tenant, second, "entity_extraction", "cancelled")
    await _failure(sc, tenant, str(uuid.uuid4()), "enrich_stranded")  # another task
    await _failure(sc, tenant, None)  # names no memory
    await _failure(sc, other, str(uuid.uuid4()))  # another tenant

    rows = await _open(sc, tenant)

    assert [(r["memory_id"], r["task_name"], r["status"]) for r in rows] == [
        (first, "entity_extraction_degraded", "failed"),
        (second, "entity_extraction", "cancelled"),
    ]
    assert [r["memory_id"] for r in await _open(sc, tenant, memory_id=second)] == [
        second
    ]


async def test_rows_older_than_the_cutoff_are_not_listed(sc):
    tenant = new_tenant_id()
    await _failure(sc, tenant, str(uuid.uuid4()))

    rows = await sc.list_open_task_failures(
        tenant,
        er.LOST_EXTRACTION_TASKS,
        since=datetime.now(UTC) + timedelta(minutes=1),
        limit=100,
    )

    assert rows == []


async def test_a_handled_row_leaves_the_list_and_cannot_be_moved_again(sc):
    tenant, other = new_tenant_id(), new_tenant_id()
    await _failure(sc, tenant, str(uuid.uuid4()))
    [row] = await _open(sc, tenant)

    assert (
        await sc.mark_task_failures_handled(other, [row["id"]], er.RERUN) == 0
    )  # not its row
    assert await sc.mark_task_failures_handled(tenant, [row["id"]], er.RERUN) == 1
    assert (
        await sc.mark_task_failures_handled(tenant, [row["id"]], er.SKIPPED) == 0
    )  # already handled
    assert await _open(sc, tenant) == []


async def test_a_memory_re_run_enough_times_is_not_listed_again(sc):
    """A re-run that keeps failing writes a fresh row each time; the cap stops
    the sweep picking that memory every hour until its rows age out."""
    tenant, memory = new_tenant_id(), str(uuid.uuid4())
    for _ in range(er.RERUN_MAX_PER_MEMORY):
        await _failure(sc, tenant, memory)
        [row] = await _open(sc, tenant)
        await sc.mark_task_failures_handled(tenant, [row["id"]], er.RERUN)
    await _failure(sc, tenant, memory)

    assert len(await _open(sc, tenant)) == 1
    assert await _open(sc, tenant, max_reruns_per_memory=er.RERUN_MAX_PER_MEMORY) == []


async def test_rows_marked_together_count_as_one_re_run(sc):
    """One re-run marks every open row the memory has (a degraded run and a
    cancelled one, say). That is one re-run of the cap, not one per row."""
    tenant, memory = new_tenant_id(), str(uuid.uuid4())
    for _ in range(3):
        await _failure(sc, tenant, memory, "entity_extraction_degraded")
    rows = await _open(sc, tenant)
    assert (
        await sc.mark_task_failures_handled(tenant, [r["id"] for r in rows], er.RERUN)
        == 3
    )
    await _failure(sc, tenant, memory, "entity_extraction", "cancelled")

    assert len(await _open(sc, tenant, max_reruns_per_memory=2)) == 1
    assert await _open(sc, tenant, max_reruns_per_memory=1) == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param(
            "/tasks/failures",
            {
                "task_name": "t",
                "tenant_id": "x",
                "error_message": "m",
                "status": "rerun",
            },
            id="new-row-as-handled",
        ),
        pytest.param(
            "/tasks/failures/handled",
            {"tenant_id": "x", "ids": [str(uuid.uuid4())], "status": "failed"},
            id="reopen",
        ),
        pytest.param(
            "/tasks/failures/handled",
            {"tenant_id": "x", "ids": [], "status": "rerun"},
            id="no-ids",
        ),
        pytest.param(
            "/tasks/failures/handled",
            {"tenant_id": "x", "ids": ["nope"], "status": "rerun"},
            id="bad-id",
        ),
        pytest.param(
            "/tasks/failures/handled",
            {"ids": [str(uuid.uuid4())], "status": "rerun"},
            id="no-tenant",
        ),
    ],
)
async def test_storage_refuses_a_status_or_ids_it_does_not_know(
    storage_http, path, body
):
    resp = await storage_http.post(f"/api/v1/storage{path}", json=body)

    assert resp.status_code == 422, resp.text


async def test_the_list_needs_a_tenant(storage_http):
    resp = await storage_http.get(
        "/api/v1/storage/tasks/failures",
        params={"task_name": "entity_extraction", "since": _SINCE.isoformat()},
    )

    assert resp.status_code == 422, resp.text


# ── The sweep, end to end on real storage ──


async def _memory(sc, tenant: str, content: str) -> str:
    row = await sc.create_memory(
        {
            "tenant_id": tenant,
            "agent_id": "rerun-agent",
            "memory_type": "fact",
            "content": content,
            "status": "active",
            "visibility": "scope_team",
        }
    )
    return str(row["id"])


async def test_the_sweep_re_runs_a_lost_extraction_and_marks_its_rows(sc, monkeypatch):
    tenant, other = new_tenant_id(), new_tenant_id()
    await sc.update_org_settings(tenant, {"entity_extraction": {"enabled": True}})
    memory = await _memory(sc, tenant, "Anna Bergstrom joined Acme Corp.")
    gone = await _memory(sc, tenant, "A memory deleted since its extraction failed.")
    assert await sc.soft_delete_memory(gone, tenant)
    await _failure(sc, tenant, memory, "entity_extraction_degraded")
    await _failure(sc, tenant, memory, "entity_extraction", "cancelled")
    await _failure(sc, tenant, str(uuid.uuid4()))  # a memory that never existed
    await _failure(sc, tenant, str(uuid.uuid4()), "enrich_stranded")
    await _failure(sc, other, str(uuid.uuid4()))

    scheduled: list = []
    extract = AsyncMock()
    reset = AsyncMock(wraps=sc.reset_entity_artifacts)
    monkeypatch.setattr(sc, "list_active_tenants", AsyncMock(return_value=[tenant]))
    monkeypatch.setattr(sc, "reset_entity_artifacts", reset)
    monkeypatch.setattr(er, "process_entity_extraction", extract)
    monkeypatch.setattr(er, "track_task", lambda coro: scheduled.append(coro))

    counts = await er.rerun_lost_extractions()
    for coro in scheduled:
        await coro

    assert counts == {
        "tenants": 1,
        "unreadable_tenants": 0,
        "memories": 2,
        "scheduled": 1,
        "skipped": 1,
        "failed_memories": 0,
    }
    reset.assert_awaited_once_with(tenant, memory)
    extract.assert_awaited_once_with(
        UUID(memory),
        tenant,
        None,
        "rerun-agent",
        "Anna Bergstrom joined Acme Corp.",
        "fact",
    )
    assert (
        await _open(sc, tenant) == []
    )  # both of the memory's rows, and the missing one's
    enrich = await sc.list_open_task_failures(
        tenant, ["enrich_stranded"], since=_SINCE, limit=10
    )
    assert len(enrich) == 1  # not an extraction row
    assert len(await _open(sc, other)) == 1  # not a tenant this sweep read

    assert (await er.rerun_lost_extractions())["memories"] == 0


async def _scheduled_re_run(
    sc, monkeypatch
) -> tuple[str, str, list, AsyncMock, AsyncMock]:
    """One lost extraction, swept and scheduled; its re-run not yet run."""
    tenant = new_tenant_id()
    await sc.update_org_settings(tenant, {"entity_extraction": {"enabled": True}})
    memory = await _memory(sc, tenant, "Anna Bergstrom joined Acme Corp.")
    await _failure(sc, tenant, memory, "entity_extraction_degraded")
    scheduled: list = []
    extract = AsyncMock()
    reset = AsyncMock(wraps=sc.reset_entity_artifacts)
    monkeypatch.setattr(sc, "list_active_tenants", AsyncMock(return_value=[tenant]))
    monkeypatch.setattr(sc, "reset_entity_artifacts", reset)
    monkeypatch.setattr(er, "process_entity_extraction", extract)
    monkeypatch.setattr(er, "track_task", lambda coro: scheduled.append(coro))
    assert (await er.rerun_lost_extractions())["scheduled"] == 1
    return tenant, memory, scheduled, reset, extract


async def test_a_re_run_extracts_the_text_the_memory_holds_when_its_turn_comes(
    sc, monkeypatch
):
    """A re-run can wait behind others for minutes. An edit meanwhile runs its
    own extraction, and the worker drops a result for text the row no longer
    holds: resetting and extracting the old text would leave no graph."""
    tenant, memory, scheduled, reset, extract = await _scheduled_re_run(sc, monkeypatch)
    edited = "Anna Bergstrom left Acme Corp. for Globex."
    await sc.update_memory(memory, tenant, {"content": edited})

    for coro in scheduled:
        await coro

    reset.assert_awaited_once_with(tenant, memory)
    extract.assert_awaited_once_with(
        UUID(memory), tenant, None, "rerun-agent", edited, "fact"
    )


async def test_a_memory_deleted_while_its_re_run_waits_is_left_alone(sc, monkeypatch):
    tenant, memory, scheduled, reset, extract = await _scheduled_re_run(sc, monkeypatch)
    assert await sc.soft_delete_memory(memory, tenant)

    for coro in scheduled:
        await coro

    reset.assert_not_awaited()
    extract.assert_not_awaited()


# ── The sweep's choices, in isolation ──


def _row(memory_id: str, minutes_ago: int, task: str = "entity_extraction") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "task_name": task,
        "memory_id": memory_id,
        "status": "failed",
        "created_at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
    }


def _storage(
    rows_by_tenant: dict, *, live: set[str], marked: int | None = None
) -> MagicMock:
    sc = MagicMock()
    sc.list_active_tenants = AsyncMock(return_value=list(rows_by_tenant))

    async def _list(tenant_id, task_names, **_kw):
        rows = rows_by_tenant[tenant_id]
        if isinstance(rows, BaseException):
            raise rows
        return rows

    sc.list_open_task_failures = AsyncMock(side_effect=_list)
    sc.get_memory = AsyncMock(
        side_effect=lambda memory_id, tenant_id, **_kw: (
            {"id": memory_id, "deleted_at": None, "content": "text"}
            if memory_id in live
            else None
        )
    )
    sc.mark_task_failures_handled = AsyncMock(
        side_effect=lambda tenant_id, ids, status: (
            len(ids) if marked is None else marked
        )
    )
    return sc


async def _sweep(sc, *, enabled: bool = True) -> tuple[dict, list]:
    scheduled: list = []
    cfg = SimpleNamespace(entity_extraction_enabled=enabled)
    with (
        patch.object(er, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=cfg),
        ),
        patch.object(
            er,
            "_schedule",
            side_effect=lambda memory_id, tenant: scheduled.append((tenant, memory_id)),
        ),
    ):
        counts = await er.rerun_lost_extractions()
    return counts, scheduled


@pytest.mark.unit
async def test_the_oldest_lost_extractions_go_first_across_tenants_up_to_the_cap(
    monkeypatch,
):
    monkeypatch.setattr(er, "RERUN_MAX_MEMORIES", 2)
    sc = _storage(
        {
            "t1": [_row("m-new", 5), _row("m-old", 300)],
            "t2": [_row("m-mid", 60), _row("m-mid", 10)],
        },
        live={"m-new", "m-old", "m-mid"},
    )

    counts, scheduled = await _sweep(sc)

    assert scheduled == [("t1", "m-old"), ("t2", "m-mid")]
    assert counts["memories"] == 2
    # One mark per memory, carrying all of that memory's rows.
    marks = {
        call.args[0]: call.args[1]
        for call in sc.mark_task_failures_handled.await_args_list
    }
    assert len(marks["t2"]) == 2


@pytest.mark.unit
async def test_the_sweep_asks_for_a_weeks_rows_and_leaves_out_spent_memories():
    sc = _storage({"t1": []}, live=set())

    await _sweep(sc)

    kwargs = sc.list_open_task_failures.await_args.kwargs
    assert kwargs["max_reruns_per_memory"] == er.RERUN_MAX_PER_MEMORY
    assert abs((datetime.now(UTC) - kwargs["since"]) - er.RERUN_LOOKBACK) < timedelta(
        minutes=1
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("live", "enabled"),
    [(set(), True), ({"m1"}, False)],
    ids=["gone-or-held", "extraction-off"],
)
async def test_a_memory_with_nothing_to_re_run_is_marked_skipped(live, enabled):
    sc = _storage({"t1": [_row("m1", 30)]}, live=live)

    counts, scheduled = await _sweep(sc, enabled=enabled)

    assert scheduled == []
    assert (counts["scheduled"], counts["skipped"]) == (0, 1)
    assert sc.mark_task_failures_handled.await_args.args[2] == er.SKIPPED


@pytest.mark.unit
async def test_a_memory_another_sweep_already_claimed_is_not_run_twice():
    sc = _storage({"t1": [_row("m1", 30)]}, live={"m1"}, marked=0)

    counts, scheduled = await _sweep(sc)

    assert scheduled == []
    assert counts["scheduled"] == 0


@pytest.mark.unit
async def test_a_tenant_that_cannot_be_read_does_not_stop_the_others():
    sc = _storage(
        {"t1": RuntimeError("storage 503"), "t2": [_row("m2", 30)]}, live={"m2"}
    )

    counts, scheduled = await _sweep(sc)

    assert counts["unreadable_tenants"] == 1
    assert scheduled == [("t2", "m2")]


@pytest.mark.unit
async def test_a_memory_that_errors_does_not_stop_the_sweep_and_keeps_its_rows():
    good = _row("m-good", 30)
    sc = _storage({"t1": [_row("m-bad", 300), good]}, live={"m-good"})
    read = sc.get_memory.side_effect

    def _get(memory_id, tenant_id, **kw):
        if memory_id == "m-bad":  # the oldest, so the sweep meets it first
            raise RuntimeError("storage 503")
        return read(memory_id, tenant_id, **kw)

    sc.get_memory.side_effect = _get

    counts, scheduled = await _sweep(sc)

    assert scheduled == [("t1", "m-good")]
    assert (counts["memories"], counts["scheduled"], counts["failed_memories"]) == (
        2,
        1,
        1,
    )
    # m-bad's rows were not marked, so a later sweep tries it again.
    assert [c.args[1] for c in sc.mark_task_failures_handled.await_args_list] == [
        [good["id"]]
    ]


@pytest.mark.unit
async def test_a_re_run_resets_the_memorys_extraction_before_running_it():
    """Reset first, so a re-run replaces a partial or heuristic graph."""
    calls: list[str] = []
    memory = {
        "id": str(uuid.uuid4()),
        "fleet_id": "f1",
        "agent_id": "a1",
        "content": "text",
        "memory_type": "fact",
        "deleted_at": None,
    }
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value=memory)
    sc.reset_entity_artifacts = AsyncMock(side_effect=lambda *a: calls.append("reset"))
    extract = AsyncMock(side_effect=lambda *a: calls.append("extract"))

    with (
        patch.object(er, "get_storage_client", return_value=sc),
        patch.object(er, "process_entity_extraction", extract),
    ):
        await er._rerun(memory["id"], "t1")

    assert calls == ["reset", "extract"]
    extract.assert_awaited_once_with(
        UUID(memory["id"]), "t1", "f1", "a1", "text", "fact"
    )


# ── The routes ──


def _route(path: str, method: str = "POST"):
    from starlette.routing import Match

    from core_api.routes import lifecycle

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "root_path": "",
    }
    return [r for r in lifecycle.router.routes if r.matches(scope)[0] == Match.FULL]


@pytest.mark.unit
def test_both_routes_resolve_to_their_handlers():
    from core_api.routes import lifecycle

    [sweep] = _route("/admin/entity-extraction/rerun-lost")
    assert sweep.endpoint is lifecycle.rerun_lost_entity_extractions
    [one] = _route(f"/admin/memories/{uuid.uuid4()}/re-extract")
    assert one.endpoint is lifecycle.re_extract_memory
    assert one.status_code == 202


@pytest.mark.unit
async def test_both_routes_are_admin_only():
    from core_api.routes import lifecycle

    auth = MagicMock()
    auth.enforce_admin.side_effect = HTTPException(status_code=403, detail="admin only")
    with pytest.raises(HTTPException) as sweep:
        await lifecycle.rerun_lost_entity_extractions(auth=auth)
    with pytest.raises(HTTPException) as one:
        await lifecycle.re_extract_memory(uuid.uuid4(), tenant_id="t1", auth=auth)

    assert sweep.value.status_code == one.value.status_code == 403


@pytest.mark.unit
@pytest.mark.parametrize(
    ("memory", "enabled", "status"),
    [(None, True, 404), ({"id": "m"}, False, 409)],
    ids=["gone-or-held", "extraction-off"],
)
async def test_re_extracting_one_memory_refuses_what_it_cannot_run(
    memory, enabled, status
):
    from core_api.routes import lifecycle

    with (
        patch.object(er, "live_memory", AsyncMock(return_value=memory)),
        patch.object(
            lifecycle,
            "resolve_config",
            AsyncMock(return_value=SimpleNamespace(entity_extraction_enabled=enabled)),
        ),
        patch.object(er, "schedule_rerun", AsyncMock()) as schedule,
    ):
        with pytest.raises(HTTPException) as caught:
            await lifecycle.re_extract_memory(
                uuid.uuid4(), tenant_id="t1", auth=MagicMock()
            )

    assert caught.value.status_code == status
    schedule.assert_not_awaited()


async def test_re_extracting_one_memory_marks_its_rows_and_schedules_it(
    sc, monkeypatch
):
    from core_api.routes import lifecycle

    tenant = new_tenant_id()
    await sc.update_org_settings(tenant, {"entity_extraction": {"enabled": True}})
    memory = await _memory(sc, tenant, "Anna Bergstrom joined Acme Corp.")
    await _failure(sc, tenant, memory, "entity_extraction_degraded")
    scheduled: list = []
    monkeypatch.setattr(er, "_schedule", lambda m, t: scheduled.append((t, m)))

    body = await lifecycle.re_extract_memory(
        UUID(memory), tenant_id=tenant, auth=MagicMock()
    )

    assert body == {"memory_id": memory, "scheduled": True, "rows_marked": 1}
    assert scheduled == [(tenant, memory)]
    assert await _open(sc, tenant) == []
