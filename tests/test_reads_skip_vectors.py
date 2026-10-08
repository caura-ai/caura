"""core-api's half of the reads that stop sending vectors (audit 2026-10-01, B33).

Storage's bulk-get leaves each row's vectors out unless asked (L-189). The bulk
re-embed is the one caller that reads a fetched row's embedding: a row whose
vector landed while the batch was embedding keeps it, and contradiction
detection runs on that vector. So it asks.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from core_api.clients.storage_client import CoreStorageClient
from core_api.constants import VECTOR_DIM
from tests.conftest import close_scheduled_coro

TENANT_ID = "t-reads-skip-vectors"
LANDED = [0.2] * VECTOR_DIM


async def test_bulk_get_asks_for_the_embedding_only_when_told():
    sc = CoreStorageClient()
    sent: list[dict] = []

    async def fake_post(path, data=None, *, read=False, idempotent=False):
        sent.append(data)
        return [{"id": i} for i in data["ids"]]

    sc._post = fake_post  # type: ignore[method-assign]
    ids = [str(uuid.uuid4())]

    await sc.bulk_get_memories(ids, tenant_id=TENANT_ID)
    await sc.bulk_get_memories(ids, tenant_id=TENANT_ID, with_embedding=True)

    assert [body["with_embedding"] for body in sent] == [False, True]


async def test_the_bulk_re_embed_keeps_a_vector_that_landed_first():
    from core_api.services import memory_service

    async def bulk_get(ids, tenant_id, *, read=True, with_embedding=False):
        # As storage answers it: the embedding only when asked.
        landed = {"embedding": LANDED} if with_embedding else {}
        return [{"id": i, "fleet_id": "f1", "deleted_at": None, **landed} for i in ids]

    async def embed_batch(texts, _cfg, **_kwargs):
        return [[0.1] * VECTOR_DIM for _ in texts]

    sc = MagicMock()
    sc.bulk_get_memories = AsyncMock(side_effect=bulk_get)
    sc.update_embedding = AsyncMock()
    detect = MagicMock()

    with (
        patch.object(memory_service, "get_embeddings_batch", new=embed_batch),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch.object(memory_service, "track_task", side_effect=close_scheduled_coro),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=close_scheduled_coro),
        ),
        patch(
            "core_api.services.contradiction.run_contradiction_detection",
            new=detect,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memories_bulk(
            [(uuid.uuid4(), "a body the worker embedded first")], TENANT_ID, "f1"
        )

    sc.update_embedding.assert_not_awaited()
    assert detect.call_args.kwargs["embedding"] == LANDED
