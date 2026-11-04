"""A memory's text does not outlive its deletion in the stores beside it.

* Memory audit rows record no title and no edited field's text: the audit
  log is hash-chained, so a row cannot be redacted when its memory is
  deleted later, and it must not hold the text in the first place. They
  record which text fields changed instead, as edits already do for content.
* Deleting or dropping an ingested fact clears its batch's summary (and the
  summary's embedding) from the batch's parent document: the summary quotes
  the batch's facts, and any tenant credential can read the document. The
  rest of the parent record stays.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from common.constants import VECTOR_DIM
from core_api.clients.storage_client import get_storage_client
from core_api.services import audit_service
from core_storage_api.services.postgres_service import get_session

_SECRET = "Dana's biopsy came back benign"
_SUMMARY = f"{_SECRET}. Follow-up in March."


async def test_memory_audit_rows_record_no_text(monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(audit_service, "get_audit_queue", lambda: None)
    monkeypatch.setattr(audit_service, "_post_audit_sync", post)

    await audit_service.log_action(
        tenant_id="t1",
        action="create",
        resource_type="memory",
        resource_id=str(uuid4()),
        detail={"memory_type": "fact", "title": _SECRET, "content_length": 40},
    )
    await audit_service.log_action(
        tenant_id="t1",
        action="update",
        resource_type="memory",
        resource_id=str(uuid4()),
        detail={
            "changes": {
                "title": {"old": _SECRET, "new": "Results"},
                "object_value": {"old": _SECRET, "new": "pending"},
                "source_uri": {"old": None, "new": f"mailto:dana?subject={_SECRET}"},
                "predicate": {"old": "has_result", "new": _SECRET},
                "weight": {"old": 0.5, "new": 0.9},
            },
            "content_changed": False,
        },
    )

    create, update = (call.args[0]["detail"] for call in post.await_args_list)
    assert "title" not in create
    assert create["memory_type"] == "fact"
    assert update["changes"] == {"weight": {"old": 0.5, "new": 0.9}}
    assert update["text_changed"] == [
        "object_value",
        "predicate",
        "source_uri",
        "title",
    ]
    assert _SECRET not in json.dumps(create) + json.dumps(update)


def _tenant() -> str:
    return f"test-tenant-copies-{uuid4().hex[:8]}"


async def _fact(tenant_id: str, run_id: str, *, agent_id: str) -> str:
    """A committed ingested fact of batch ``run_id``."""
    memory_id = str(uuid4())
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO memories
                    (id, tenant_id, agent_id, content, memory_type, run_id, metadata)
                VALUES
                    (CAST(:id AS uuid), :tenant_id, :agent_id, :content, 'fact', :run_id,
                     CAST('{"source": "ingest"}' AS jsonb))
                """
            ),
            {
                "id": memory_id,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "content": _SUMMARY,
                "run_id": run_id,
            },
        )
    return memory_id


async def _batch_document(tenant_id: str, run_id: str) -> None:
    """The batch's parent record, as ``ingest_commit`` writes it."""
    await get_storage_client().upsert_document_xmax(
        {
            "tenant_id": tenant_id,
            "fleet_id": None,
            "agent_id": "agent-a",
            "collection": "ingest-sources",
            "doc_id": run_id,
            "data": {
                "summary": _SUMMARY,
                "memory_count": 2,
                "source_uri": "text-input",
            },
            "embedding": [0.1] * VECTOR_DIM,
        }
    )


async def _batch_document_state(tenant_id: str, run_id: str) -> tuple[dict, bool]:
    """The parent record's ``data``, and whether it has no embedding."""
    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT data, embedding IS NULL AS unembedded FROM documents "
                    "WHERE tenant_id = :tenant_id AND collection = 'ingest-sources' "
                    "AND doc_id = :doc_id"
                ),
                {"tenant_id": tenant_id, "doc_id": run_id},
            )
        ).one()
    data = json.loads(row.data) if isinstance(row.data, str) else row.data
    return data, row.unembedded


_DELETES = {
    "single": lambda sc, tenant, memory_id: sc.soft_delete_memory(memory_id, tenant),
    "by_ids": lambda sc, tenant, memory_id: sc.soft_delete_by_ids(tenant, [memory_id]),
    "by_filter": lambda sc, tenant, _memory_id: sc.soft_delete_by_filter(
        {"tenant_id": tenant, "agent_id": "agent-a"}
    ),
    "policy_drop": lambda sc, tenant, memory_id: sc.drop_memory(memory_id, tenant),
}


@pytest.mark.parametrize("path", sorted(_DELETES))
async def test_deleting_a_fact_clears_its_batch_summary(path):
    tenant, batch, other_batch = _tenant(), str(uuid4()), str(uuid4())
    deleted = await _fact(tenant, batch, agent_id="agent-a")
    await _fact(tenant, batch, agent_id="agent-b")
    await _fact(tenant, other_batch, agent_id="agent-b")
    for run_id in (batch, other_batch):
        await _batch_document(tenant, run_id)

    assert await _DELETES[path](get_storage_client(), tenant, deleted)

    data, unembedded = await _batch_document_state(tenant, batch)
    assert "summary" not in data
    assert unembedded
    assert data["memory_count"] == 2
    data, unembedded = await _batch_document_state(tenant, other_batch)
    assert data["summary"] == _SUMMARY
    assert not unembedded
