"""M-61: a filtered nearest-neighbour lookup must reach the caller's own rows
when other tenants' rows are nearer.

``ix_memories_embedding_hnsw`` is one index across every tenant. Without
``hnsw.iterative_scan``, an HNSW scan hands back one ``ef_search`` batch (40
rows by default) and the tenant filter runs on that batch alone. A tenant whose
rows sit behind 40 nearer rows of other tenants got nothing back, and no error:
semantic dedup admitted a paraphrase it should refuse, contradiction detection
judged an empty candidate set, and the crystallizer's sweep missed the pair.

Each test puts 60 rows of another tenant nearer the probe than the caller's own
rows, then runs the lookup planned through the HNSW index, the plan a large
table gets: seq scan and sort are off for the lookup's transaction. The batch is
10 rows there, not 40: HNSW search is approximate, and over 60 rows this tightly
packed a 40-row batch sometimes reached the caller's row anyway. The fix does
not depend on ``ef_search``.
"""

import contextlib
import math
import random
import uuid

import pytest
from sqlalchemy import text

import core_storage_api.services.postgres_service as ps
from common.constants import VECTOR_DIM
from core_api.clients.storage_client import get_storage_client
from tests.conftest import new_tenant_id

# Six ef_search batches, all nearer than the caller's rows.
_OTHER_ROWS = 60
_EF_SEARCH = 10
_OTHER_OFFSET = 0.01
_OWN_OFFSET = 0.1


class _Space:
    """A probe direction unique to one test, and points around it.

    ``near(i, a)`` moves the probe by ``a`` along offset ``i``: a basis vector
    with its probe component removed, so it is orthogonal to the probe and
    almost orthogonal to every other offset. Its cosine to the probe is
    1 / sqrt(1 + a**2), and two points at offset 0.1 are 0.990 apart, both
    further than either is from a point at offset 0.01.
    """

    def __init__(self, seed: str) -> None:
        rng = random.Random(seed)
        self.probe = _unit([rng.gauss(0.0, 1.0) for _ in range(VECTOR_DIM)])
        self._dims = rng.sample(range(VECTOR_DIM), _OTHER_ROWS + 2)

    def near(self, i: int, a: float) -> list[float]:
        d = self._dims[i]
        offset = [-self.probe[d] * p for p in self.probe]
        offset[d] += 1.0
        offset = _unit(offset)
        return _unit([p + a * o for p, o in zip(self.probe, offset, strict=True)])

    def others(self) -> list[list[float]]:
        return [self.near(i, _OTHER_OFFSET) for i in range(_OTHER_ROWS)]

    def own(self, n: int) -> list[list[float]]:
        return [self.near(_OTHER_ROWS + j, _OWN_OFFSET) for j in range(n)]


def _unit(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


async def _seed(tenant_id: str, embeddings: list[list[float]]) -> list[str]:
    created = await get_storage_client().create_memories(
        [
            {
                "tenant_id": tenant_id,
                "fleet_id": None,
                "agent_id": "test-agent",
                "memory_type": "fact",
                "content": f"m61 row {i}",
                "embedding": embedding,
                "weight": 0.5,
                "content_hash": f"m61-{tenant_id}-{i}",
                "status": "active",
                "visibility": "scope_team",
                "client_request_id": str(uuid.uuid4()),
            }
            for i, embedding in enumerate(embeddings)
        ]
    )
    ids = [row["id"] for row in created]
    assert all(ids), "the bulk insert did not return an id for every row"
    return ids


def _plan_through_hnsw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the lookups as a large table plans them: through the HNSW index."""
    real_get_session = ps.get_session

    @contextlib.asynccontextmanager
    async def through_hnsw():
        async with real_get_session() as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            await session.execute(text("SET LOCAL enable_sort = off"))
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {_EF_SEARCH}"))
            yield session

    monkeypatch.setattr(ps, "get_session", through_hnsw)


@pytest.fixture(autouse=True)
def _real_probe(monkeypatch: pytest.MonkeyPatch):
    """Each test starts unprobed, so the real DB answers (CI: pgvector 0.8+)."""
    monkeypatch.setattr(ps, "_pgvector_version", None)


async def test_semantic_dedup_finds_the_duplicate_behind_other_tenants(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = _Space(tenant_id)
    await _seed(new_tenant_id(), space.others())
    [own] = await _seed(tenant_id, space.own(1))
    _plan_through_hnsw(monkeypatch)

    hit = await ps.PostgresService().memory_find_semantic_duplicate(
        tenant_id=tenant_id, fleet_id=None, embedding=space.probe
    )

    assert hit is not None, "the caller's own near-duplicate was not found"
    memory, similarity = hit
    assert str(memory.id) == own
    assert similarity == pytest.approx(1 / math.sqrt(1 + _OWN_OFFSET**2), abs=1e-3)


async def test_contradiction_candidates_reach_past_other_tenants(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = _Space(tenant_id)
    await _seed(new_tenant_id(), space.others())
    [own] = await _seed(tenant_id, space.own(1))
    _plan_through_hnsw(monkeypatch)

    found = await ps.PostgresService().memory_find_similar_candidates(
        tenant_id=tenant_id,
        fleet_id=None,
        embedding=space.probe,
        memory_id=uuid.uuid4(),
    )

    assert [str(m.id) for m in found] == [own]


async def test_crystallizer_sweep_pairs_rows_behind_other_tenants(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = _Space(tenant_id)
    await _seed(new_tenant_id(), space.others())
    a, b = await _seed(tenant_id, space.own(2))
    _plan_through_hnsw(monkeypatch)

    rows = await ps.PostgresService().memory_find_near_duplicate_pairs(
        tenant_id=tenant_id, fleet_id=None, batch_size=10
    )

    pairs = {(str(r.candidate_id), str(r.neighbor_id)) for r in rows if r.neighbor_id}
    assert pairs == {(a, b), (b, a)}
