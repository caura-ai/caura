"""lme-0929-m-03 (SIDE-56) — ``GET /memories/stats`` says whether a store is settled.

A LongMemEval store ingested via ``/memories/bulk`` kept changing for days: in a
deferred deployment the embedding, the LLM enrichment and the atomic-fact
fan-out all land after the write returns, and contradiction marks follow them.
A benchmark measured the store hours after ingest and again two weeks later,
saw different results, and nothing could have told it the store was not done.

These drive the real path end to end: a deferred bulk ingest, the stats route,
then the same storage PATCHes core-worker sends, until ``settled`` flips. The
per-marker and plan checks against the migrated (``json``) schema live in
``core-storage-api/tests/test_lme_0929_m03_pending_work.py``.
"""

from __future__ import annotations

import contextlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings as core_settings
from core_api.openapi_responses import MemoryStatsResponse
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
from core_api.services import memory_service as ms
from tests.conftest import get_test_auth, new_tenant_id

pytestmark = pytest.mark.integration

_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)
_VEC = [0.1] * 1024
_WORKER_ENRICH_CLEAR = {
    "metadata_patch": {
        "enrichment_pending": False,
        "_system": {"enrichment_pending": False},
    }
}


@contextlib.contextmanager
def _enrichment_on():
    """Enable enrichment for the tenant on top of the REAL resolved config.

    A fresh test tenant resolves ``enrichment_enabled`` False and CI resolves the
    provider to ``"none"``; both gate the deferred publish, so both are forced
    (same shape as ``test_c25_caller_owned_metadata_all_paths``).
    """
    from core_api.services import organization_settings

    class _On:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        @property
        def enrichment_enabled(self):
            return True

        @property
        def enrichment_provider(self):
            return "fake"

    real_resolve = organization_settings.resolve_config

    async def _resolve(tenant_id):
        return _On(await real_resolve(tenant_id))

    with patch.object(organization_settings, "resolve_config", new=_resolve):
        yield


async def _bulk(tenant: str, agent: str, n: int) -> list:
    resp = await ms.create_memories_bulk(
        BulkMemoryCreate(
            tenant_id=tenant,
            fleet_id="m03-fleet",
            agent_id=agent,
            items=[
                BulkMemoryItem(
                    content=f"m03 {agent} item {i} {uuid.uuid4()}." + _PADDING
                )
                for i in range(n)
            ],
        ),
        bulk_attempt_id=uuid.uuid4().hex,
    )
    assert [r.status for r in resp.results] == ["created"] * n, resp.results
    return [r.id for r in resp.results]


async def _stats(client, tenant: str, **params) -> dict:
    _, headers = get_test_auth(tenant)
    resp = await client.get(
        "/api/v1/memories/stats",
        params={"tenant_id": tenant, **params},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_deferred_bulk_ingest_reports_pending_until_the_worker_lands(
    client, monkeypatch
):
    monkeypatch.setattr(core_settings, "deployment_mode", "deferred")
    tenant = new_tenant_id()
    publish_spy = AsyncMock(return_value=None)
    with (
        _enrichment_on(),
        patch.object(ms, "publish_memory_enrich_request", new=publish_spy),
        # Hold the vector back, as a deferred deployment does until core-worker
        # runs; in-process the fallback re-embed would otherwise race the reads.
        patch.object(ms, "_reembed_memories_bulk", new=AsyncMock(return_value=None)),
    ):
        ids = await _bulk(tenant, "m03-agent", 2)
        assert publish_spy.call_count == 2  # the enrich requests the marker stands for

        stats = await _stats(client, tenant)
        assert stats["pending"] == {"embedding": 2, "enrichment": 2, "fanout": 0}
        assert stats["settled"] is False

        sc = get_storage_client()
        # core-worker's enrich PATCH, with one row also persisting atomic facts
        # for core-api's fan-out consumer.
        await sc.update_memory(str(ids[0]), tenant, _WORKER_ENRICH_CLEAR)
        await sc.update_memory(
            str(ids[1]),
            tenant,
            {
                "metadata_patch": {
                    **_WORKER_ENRICH_CLEAR["metadata_patch"],
                    "atomic_facts": [{"content": "x"}],
                }
            },
        )
        stats = await _stats(client, tenant)
        assert stats["pending"] == {"embedding": 2, "enrichment": 0, "fanout": 1}
        assert stats["settled"] is False

        # The fan-out consumer's clear, then core-worker's embed PATCH.
        await sc.update_memory(
            str(ids[1]), tenant, {"metadata_patch": {"atomic_facts": None}}
        )
        for mid in ids:
            await sc.update_memory(str(mid), tenant, {"embedding": _VEC})
        stats = await _stats(client, tenant)
        assert stats["pending"] == {"embedding": 0, "enrichment": 0, "fanout": 0}
        assert stats["settled"] is True


async def test_inline_deployment_bulk_ingest_is_settled_immediately(client):
    """No marker is set for work nobody will do: inline embedding, enrichment
    off for the tenant — a marker here would never clear and ``settled`` would
    never flip."""
    tenant = new_tenant_id()
    await _bulk(tenant, "m03-agent", 2)
    stats = await _stats(client, tenant)
    assert stats["pending"] == {"embedding": 0, "enrichment": 0, "fanout": 0}
    assert stats["settled"] is True


async def test_scoping_by_agent_id(client, monkeypatch):
    monkeypatch.setattr(core_settings, "deployment_mode", "deferred")
    tenant = new_tenant_id()
    with (
        _enrichment_on(),
        patch.object(
            ms, "publish_memory_enrich_request", new=AsyncMock(return_value=None)
        ),
        patch.object(ms, "_reembed_memories_bulk", new=AsyncMock(return_value=None)),
    ):
        busy = await _bulk(tenant, "m03-busy", 1)
        done = await _bulk(tenant, "m03-done", 1)
    sc = get_storage_client()
    await sc.update_memory(
        str(done[0]), tenant, {**_WORKER_ENRICH_CLEAR, "embedding": _VEC}
    )
    assert busy

    assert (await _stats(client, tenant, agent_id="m03-done"))["settled"] is True
    busy_stats = await _stats(client, tenant, agent_id="m03-busy")
    assert busy_stats["pending"] == {"embedding": 1, "enrichment": 1, "fanout": 0}
    assert busy_stats["settled"] is False
    assert (await _stats(client, tenant))["settled"] is False


async def test_the_response_is_an_additive_extension(client):
    """Every key a pre-change client read is still there with the same type; the
    new block only adds. Validated against the documented response model."""
    tenant = new_tenant_id()
    await _bulk(tenant, "m03-agent", 1)
    stats = await _stats(client, tenant)
    assert {"total", "by_type", "by_agent", "by_status"} <= set(stats)
    assert stats["total"] == 1
    assert stats["by_agent"] == {"m03-agent": 1}
    model = MemoryStatsResponse.model_validate(stats)
    assert model.settled is True
    assert model.pending is not None
