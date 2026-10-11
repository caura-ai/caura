"""M-61, documents index: semantic document search must reach the caller's own
documents when other tenants' documents are nearer.

``ix_documents_embedding_hnsw`` is one index across every tenant, like the
memories and entities indexes. Without ``hnsw.iterative_scan`` an HNSW scan
hands back one ``ef_search`` batch and the tenant filter runs on that batch
alone, so a tenant whose documents sit behind nearer documents of other tenants
got no results.

Same setup as the memories test: 60 documents of another tenant nearer the probe
than the caller's own, and the search planned through the HNSW index (seq scan
and sort off, a 10-row ``ef_search`` batch) in its transaction.
"""

import contextlib
from uuid import uuid4

import pytest
from sqlalchemy import text

import core_storage_api.services.postgres_service as ps
from common.models import Document
from tests import test_m61_filtered_ann_lookups_reach_sparse_tenants as m61
from tests.conftest import new_tenant_id

_COLLECTION = "notes"


async def _seed_documents(tenant_id: str, embeddings: list[list[float]]) -> list[str]:
    doc_ids = [f"m61-doc-{i}" for i in range(len(embeddings))]
    async with ps.get_session() as session:
        session.add_all(
            Document(
                id=uuid4(),
                tenant_id=tenant_id,
                fleet_id=None,
                collection=_COLLECTION,
                doc_id=doc_id,
                data={"name": doc_id},
                embedding=embedding,
            )
            for doc_id, embedding in zip(doc_ids, embeddings, strict=True)
        )
    return doc_ids


def _plan_through_hnsw(monkeypatch: pytest.MonkeyPatch) -> None:
    """The search's read session, planned as a large table plans it."""
    gucs = [
        text("SET LOCAL enable_seqscan = off"),
        text("SET LOCAL enable_sort = off"),
        text(f"SET LOCAL hnsw.ef_search = {m61._EF_SEARCH}"),
    ]
    real_get_read_session = ps.get_read_session

    @contextlib.asynccontextmanager
    async def through_hnsw():
        async with real_get_read_session() as session:
            for guc in gucs:
                await session.execute(guc)
            yield session

    monkeypatch.setattr(ps, "get_read_session", through_hnsw)


@pytest.fixture(autouse=True)
def _real_probe(monkeypatch: pytest.MonkeyPatch):
    """Each test starts unprobed, so the real DB answers (CI: pgvector 0.8+)."""
    monkeypatch.setattr(ps, "_pgvector_version", None)


async def test_search_finds_the_document_behind_other_tenants(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = m61._Space(tenant_id)
    await _seed_documents(new_tenant_id(), space.others())
    [own] = await _seed_documents(tenant_id, space.own(1))
    _plan_through_hnsw(monkeypatch)

    hits = await ps.PostgresService().document_search(
        tenant_id=tenant_id,
        query_embedding=space.probe,
        collection=_COLLECTION,
        top_k=5,
    )

    assert [doc.doc_id for doc, _ in hits] == [own]
