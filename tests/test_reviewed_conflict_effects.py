"""A committed reviewer verdict is authoritative for effects and notifications."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core_api.auth import AuthContext
from core_api.routes import conflicts
from core_api.schemas import ConflictResolveRequest
from core_api.services.contradiction_detector import verdict_applied

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("detector,reviewer", [(False, True), (True, False)])
async def test_reviewed_effect_overrides_detector_effect_without_losing_provenance(
    detector, reviewer
):
    record = {
        "review_status": "resolved",
        "metadata_": {
            "effect_applied": detector,
            "review_effect_applied": reviewer,
        },
    }
    assert verdict_applied(record) is reviewer
    assert record["metadata_"]["effect_applied"] is detector


async def test_route_announces_the_action_storage_actually_committed(monkeypatch):
    row = {
        "id": str(uuid4()),
        "tenant_id": "tenant-a",
        "new_memory_id": str(uuid4()),
        "old_memory_id": str(uuid4()),
        "relationship": "exact_value",
        "action": "supersede",
        "review_status": "resolved",
        "resolution_action": "supersede",
    }
    sc = MagicMock(resolve_memory_conflict=AsyncMock(return_value=row))
    log, announce = AsyncMock(), MagicMock()
    monkeypatch.setattr(conflicts, "get_storage_client", lambda: sc)
    monkeypatch.setattr(conflicts, "_reviewer", AsyncMock(return_value="reviewer"))
    monkeypatch.setattr(conflicts, "log_action", log)
    monkeypatch.setattr(conflicts, "on_conflict_resolved", announce)
    await conflicts.resolve_conflict(
        row["id"],
        ConflictResolveRequest(
            tenant_id="tenant-a",
            review_status="resolved",
        ),
        auth=AuthContext(tenant_id="tenant-a"),
    )
    assert log.await_args.kwargs["detail"]["resolution_action"] == "supersede"
    assert announce.call_args.kwargs["resolution_action"] == "supersede"


async def test_an_unprivileged_reviewer_cannot_start_the_storage_transition(
    monkeypatch,
):
    from fastapi import HTTPException

    sc = MagicMock(resolve_memory_conflict=AsyncMock())
    monkeypatch.setattr(conflicts, "get_storage_client", lambda: sc)
    monkeypatch.setattr(
        conflicts, "_require_trust", AsyncMock(return_value=(1, False, "insufficient"))
    )
    with pytest.raises(HTTPException) as caught:
        await conflicts.resolve_conflict(
            str(uuid4()),
            ConflictResolveRequest(
                tenant_id="tenant-a",
                review_status="resolved",
                resolution_action="supersede",
            ),
            auth=AuthContext(tenant_id="tenant-a", agent_id="writer"),
        )
    assert caught.value.status_code == 403
    sc.resolve_memory_conflict.assert_not_awaited()


@pytest.mark.parametrize(
    "action", ["replace", "supersede", "mark_disputed", "no_op", None]
)
async def test_correction_notices_use_the_committed_direction_and_effect(
    monkeypatch, action
):
    from core_api.clients import storage_client
    from core_api.services import memory_corrections

    first, second = str(uuid4()), str(uuid4())
    rows = {
        first: {"id": first, "content": "older proposition"},
        second: {"id": second, "content": "newer proposition"},
    }
    sc = MagicMock(get_memory=AsyncMock(side_effect=lambda mid, *_a, **_kw: rows[mid]))
    announce = MagicMock()
    monkeypatch.setattr(storage_client, "get_storage_client", lambda: sc)
    monkeypatch.setattr(memory_corrections, "on_memory_corrected", announce)
    await memory_corrections._conflict_corrected(
        "tenant-a",
        {
            "id": str(uuid4()),
            "new_memory_id": first,
            "old_memory_id": second,
            "metadata_": {
                "review_loser_id": first,
                "review_winner_id": second,
                "review_effect_applied": action != "no_op",
            },
        },
        "resolved",
        action,
        "reviewer",
    )
    event = announce.call_args.kwargs
    assert event["memory"]["id"] == first
    assert event["detail"]["related_memory_id"] == second
    assert event["new_summary"] == (
        "newer proposition" if action in {"replace", "supersede"} else None
    )
