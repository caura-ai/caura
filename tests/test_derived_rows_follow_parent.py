"""Rows derived from a memory follow it: on delete, on expiry, in provenance.

Auto-chunk and atomic-fact children carry the parent's own text
(``metadata.parent_memory_id``). Two gaps let that text outlive the parent:

* ``soft_delete_memory`` (REST ``DELETE /memories/{id}`` and the MCP delete op)
  deleted only the named row, so the document stayed recallable through its
  children. Governance remediation already cascades a drop; a user's delete
  now does too.
* ``fan_out_atomic_facts`` dropped the parent's ``expires_at``, ``run_id`` and
  ``source_uri``, so the children outlived the parent's TTL and lost its
  provenance. The auto-chunk children always carried all three.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api.services import memory_service
from core_api.services.memory_enrichment import AtomicFact
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid


@pytest.mark.unit
async def test_fanout_children_inherit_expiry_run_and_source():
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(side_effect=lambda _p: {"id": str(uuid.uuid4())})

    async def _no_live(*_a, **_k):
        return set()

    async def _embeds(texts, *_a, **_k):
        return [[0.0] * 4 for _ in texts]

    with (
        patch.object(memory_service, "_live_duplicate_hashes", new=_no_live),
        patch.object(memory_service, "_embed_children_or_degrade", new=_embeds),
    ):
        await memory_service.fan_out_atomic_facts(
            sc,
            atomic_facts=[
                AtomicFact(content="alpha fact"),
                AtomicFact(content="beta fact"),
            ],
            memory_id=str(uuid.uuid4()),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            parent_metadata={},
            parent_visibility="scope_team",
            parent_weight=0.5,
            parent_ts_start=None,
            tenant_config=SimpleNamespace(atomic_fact_fanout_enabled=True),
            parent_expires_at="2030-01-01T00:00:00+00:00",
            parent_run_id="run-7",
            parent_source_uri="https://example.test/doc",
        )

    payloads = [call.args[0] for call in sc.create_memory.await_args_list]
    assert len(payloads) == 2
    for p in payloads:
        assert p["expires_at"] == "2030-01-01T00:00:00+00:00"
        assert p["run_id"] == "run-7"
        assert p["source_uri"] == "https://example.test/doc"


@pytest.mark.integration
async def test_deleting_a_parent_deletes_its_children(client, tenant_id, sc):
    """End to end over the real storage: parent + two children, one DELETE."""
    _, headers = get_test_auth(tenant_id)
    agent = f"derived-{_uid()}"
    parent = await sc.create_memory(
        {
            "tenant_id": tenant_id,
            "agent_id": agent,
            "memory_type": "fact",
            "content": f"parent document {_uid()}",
            "metadata_": {"auto_chunked": True},
            "status": "active",
            "visibility": "scope_team",
        }
    )
    children = [
        await sc.create_memory(
            {
                "tenant_id": tenant_id,
                "agent_id": agent,
                "memory_type": "fact",
                "content": f"child slice {i} {_uid()}",
                "metadata_": {
                    "parent_memory_id": str(parent["id"]),
                    "source": "auto_chunk",
                },
                "status": "active",
                "visibility": "scope_team",
            }
        )
        for i in range(2)
    ]
    unrelated = await sc.create_memory(
        {
            "tenant_id": tenant_id,
            "agent_id": agent,
            "memory_type": "fact",
            "content": f"unrelated {_uid()}",
            "status": "active",
            "visibility": "scope_team",
        }
    )

    resp = await client.delete(
        f"/api/v1/memories/{parent['id']}?tenant_id={tenant_id}", headers=headers
    )
    assert resp.status_code == 204, resp.text

    for child in children:
        assert await sc.get_memory(str(child["id"]), tenant_id, read=False) is None, (
            "child survived"
        )
    assert await sc.get_memory(str(unrelated["id"]), tenant_id, read=False) is not None
