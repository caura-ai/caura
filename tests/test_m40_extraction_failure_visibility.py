"""09/02 M-40 — an extraction failure was a log line and nothing else.

``tracked_task`` writes a ``BackgroundTaskLog`` row ONLY when the coroutine it
wraps raises. ``process_entity_extraction`` catches everything, logs
"(non-fatal)", and returns normally — so the wrapper saw a success. The result:
the table an operator inspects stayed empty, the memory permanently kept no
entities, and nothing retried it or knew to.

The handler now records the failure itself. That keeps the non-raising contract
the module documents and several tests pin, while making the failure visible
where failures are supposed to be visible.

Not hypothetical: a storage 500 on ``POST /entities/relations`` produced exactly
this during an A40 wet test, and presented as "A40 does not work" — because
nothing anywhere said a task had failed.
"""

import inspect
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.services import entity_extraction_worker as w
from core_api.services import task_tracker

pytestmark = pytest.mark.unit


# ── the shared recorder ───────────────────────────────────────────────────


def test_the_recorder_is_shared_with_tracked_task():
    """One row shape for both writers. If the handler hand-rolled its own
    ``add_task_failure`` payload, the two would drift and an operator would be
    reading two different things from one table."""
    src = inspect.getsource(task_tracker.tracked_task)
    assert "record_task_failure(" in src
    assert "add_task_failure" not in src, "persistence should live in the helper"


@pytest.mark.asyncio
async def test_the_recorder_never_raises():
    """A failure to record a failure must not become the failure — it runs
    inside an ``except`` on a fire-and-forget path."""
    with patch.object(
        task_tracker, "get_storage_client", side_effect=RuntimeError("storage down")
    ):
        await task_tracker.record_task_failure(
            "t", uuid4(), "tenant", RuntimeError("boom")
        )


@pytest.mark.asyncio
async def test_the_recorder_writes_the_expected_row():
    sc = AsyncMock()
    mid = uuid4()
    with patch.object(task_tracker, "get_storage_client", return_value=sc):
        await task_tracker.record_task_failure(
            "entity_extraction", mid, "t1", ValueError("nope")
        )

    sc.add_task_failure.assert_awaited_once()
    row = sc.add_task_failure.await_args.args[0]
    assert row["task_name"] == "entity_extraction"
    assert row["memory_id"] == str(mid)
    assert row["tenant_id"] == "t1"
    assert "nope" in row["error_message"]


# ── the worker records, and still does not raise ──────────────────────────


def test_the_handler_records_the_failure():
    src = inspect.getsource(w.process_entity_extraction)
    assert "record_task_failure(" in src


def test_the_handler_records_after_the_h02_purge():
    """Order is load-bearing. The purge keeps a memory dropped mid-extraction
    from retaining the graph rows this run wrote; recording first would be
    harmless, but recording must not be able to short-circuit it, so the two
    are asserted in sequence."""
    src = inspect.getsource(w.process_entity_extraction)
    tail = src[src.rindex("except Exception as exc:") :]
    assert "record_task_failure(" in tail


@pytest.mark.asyncio
@patch(
    "core_api.services.entity_extraction_worker.record_task_failure",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_a_failing_extraction_is_recorded_and_does_not_raise(
    mock_resolve, mock_extract, mock_sc_factory, _log, mock_record
):
    """The contract in one test: the caller still sees no exception, AND the
    failure reaches the log table. Before this change only the first was true."""
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.auto_entity_linking_enabled = False
    cfg.entity_blocklist = frozenset()
    cfg.entity_extraction_provider = "openai"
    cfg.entity_extraction_model = "gpt-4o-mini"
    mock_resolve.return_value = cfg
    mock_extract.side_effect = RuntimeError("extraction provider is down")

    with patch("core_api.tasks.track_task", side_effect=lambda c, *a, **k: c.close()):
        # does not raise — the established fire-and-forget contract
        await w.process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    mock_record.assert_awaited_once()
    assert mock_record.await_args.args[0] == "entity_extraction"


@pytest.mark.asyncio
@patch(
    "core_api.services.entity_extraction_worker.record_task_failure",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_a_clean_extraction_records_nothing(
    mock_resolve, mock_extract, mock_sc_factory, _log, mock_record
):
    """Success must stay free. ``tracked_task``'s whole reason for writing only
    on failure is to keep this table from growing unboundedly, and a handler
    that recorded on every run would defeat it."""
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.auto_entity_linking_enabled = False
    cfg.entity_blocklist = frozenset()
    cfg.entity_extraction_provider = "openai"
    cfg.entity_extraction_model = "gpt-4o-mini"
    mock_resolve.return_value = cfg
    graph = MagicMock()
    graph.entities = []
    graph.relations = []
    mock_extract.return_value = graph

    with patch("core_api.tasks.track_task", side_effect=lambda c, *a, **k: c.close()):
        await w.process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="nothing extractable",
            memory_type="episodic",
        )

    mock_record.assert_not_awaited()
