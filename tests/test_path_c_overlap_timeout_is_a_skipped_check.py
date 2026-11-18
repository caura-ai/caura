"""A timed-out entity-overlap fetch is a skipped check, not a Path C error.

Storage caps ``/memories/entity-overlap-candidates`` with a statement timeout
and answers 504 when it fires, which it does by design while the read pool is
loaded (2026-11-18: the uncapped query took 80% of the pool's execution time).
Path C used to report that through its catch-all: an ERROR with a traceback per
memory. It now logs a WARNING and otherwise ends as any failed run does: the
memory is recorded as unchecked and the lock is released for a later trigger.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core_api.services import contradiction_detector as cd
from core_api.services import task_tracker

pytestmark = pytest.mark.asyncio

TENANT = f"test-overlap-timeout-{uuid.uuid4().hex[:8]}"
_URL = "http://storage/api/v1/storage/memories/entity-overlap-candidates"


def _gateway_timeout() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", _URL)
    response = httpx.Response(
        504, request=request, json={"detail": "statement timeout"}
    )
    return httpx.HTTPStatusError(
        "504 Gateway Timeout", request=request, response=response
    )


def _read_timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("timed out", request=httpx.Request("POST", _URL))


@pytest.mark.parametrize(
    "make_error", [_gateway_timeout, _read_timeout], ids=["storage-504", "read-timeout"]
)
async def test_a_timed_out_overlap_fetch_is_recorded_without_an_error(
    caplog, make_error
) -> None:
    memory = {
        "id": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "fleet_id": "f1",
        "content": "Alice lives in Boston",
        "status": "active",
        "visibility": "scope_team",
        "deleted_at": None,
    }
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value=memory)
    sc.find_entity_overlap_candidates = AsyncMock(side_effect=make_error())
    log_sc = MagicMock()
    log_sc.add_task_failure = AsyncMock()
    release = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(patch.object(cd, "get_storage_client", lambda: sc))
        stack.enter_context(
            patch.object(cd, "_acquire_entity_lock", AsyncMock(return_value=True))
        )
        stack.enter_context(patch.object(cd, "_release_lock", release))
        stack.enter_context(
            patch.object(
                cd, "_attempt_entity_retraction", AsyncMock(return_value=False)
            )
        )
        stack.enter_context(
            patch.object(cd, "_retract_unlinked_verdicts", AsyncMock(return_value=0))
        )
        stack.enter_context(
            patch.object(
                cd,
                "_rdf_conflict_pass",
                AsyncMock(return_value=cd._RdfPassResult([], [], None, False, [])),
            )
        )
        stack.enter_context(
            patch(
                "core_api.services.organization_settings.resolve_config",
                new=AsyncMock(return_value=SimpleNamespace()),
            )
        )
        stack.enter_context(
            patch.object(task_tracker, "get_storage_client", return_value=log_sc)
        )
        with caplog.at_level(logging.INFO, logger=cd.logger.name):
            await cd.detect_contradictions_by_entities_async(
                uuid.UUID(memory["id"]), TENANT, "f1"
            )

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]
    assert any(
        r.levelno == logging.WARNING and "candidates_timeout" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
    # Still a missed check: recorded as unchecked, and the lock is released so
    # a later trigger can run it.
    release.assert_awaited_once()
    log_sc.add_task_failure.assert_awaited_once()
    row = log_sc.add_task_failure.await_args.args[0]
    assert row["task_name"] == cd._CONTRADICTION_STRANDED_TASK
    assert row["memory_id"] == memory["id"]
    assert row["error_message"].startswith("entity detection failed")
