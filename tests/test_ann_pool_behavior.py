"""ANN candidate pool (``ann_pool_size``) — end-to-end behaviour against Postgres.

The statement-shape pins live in core-storage-api/tests/test_ann_pool_statement.py;
these run the real thing. The invariants:

* pool ⊇ corpus  ⇒ results are EXACTLY the full-scan results (the pool only
  restricts admission; scoring, dedup and ordering are untouched code paths);
* the ANN arm admits the nearest-by-cosine row even when every side arm is
  too small to catch it;
* the CAURA-594/679 contract survives pool mode: an FTS-matching row with a
  NULL embedding stays discoverable (fts arm);
* entity-boosted ids are admitted regardless of cosine rank (boosted arm) —
  and visibility filters still apply to them;
* agent-scoped rows stay invisible to other callers in pool mode (every arm
  carries the full row-filter set);
* pgvector < 0.8 behaves byte-identically to knob-off (probe fallback);
* the ANN arm's statement is HNSW-servable: with seq scan and sort disabled,
  EXPLAIN plans it through ``ix_memories_embedding_hnsw``.

Requires a database like the rest of this tree (CI always; locally set
TEST_DATABASE_URL against a schema created by alembic — the search_vector
trigger and the HNSW index come from migrations 001/012).
"""

import contextlib
import hashlib
import json
import uuid

import pytest
from sqlalchemy import func, select, text

import core_storage_api.services.postgres_service as ps
from common.embedding import fake_embedding
from core_api.clients.storage_client import get_storage_client
from tests.test_scored_search_materialized_plan import _ExplainJSON

_SP = {
    "fts_weight": 0.3,
    "freshness_floor": 0.7,
    "freshness_decay_days": 90,
    "recall_boost_cap": 1.1,
    "recall_decay_window_days": 14,
    "similarity_blend": 0.85,
    "fts_rank_scale": 6.0,
}


async def _insert(
    tenant_id: str,
    content: str,
    *,
    embedding="auto",
    visibility="scope_team",
    agent_id="test-agent",
):
    sc = get_storage_client()
    payload: dict = {
        "tenant_id": tenant_id,
        "fleet_id": None,
        "agent_id": agent_id,
        "memory_type": "fact",
        "content": content,
        "weight": 0.5,
        "embedding": fake_embedding(content) if embedding == "auto" else embedding,
        "content_hash": hashlib.sha256(f"{tenant_id}:{content}".encode()).hexdigest(),
        "status": "active",
        "visibility": visibility,
    }
    return await sc.create_memory(payload)


async def _search(
    tenant_id: str, *, embedding, query: str, ann_pool: int, top_k: int = 10, **kw
):
    sp = dict(_SP)
    if ann_pool:
        sp["ann_pool_size"] = ann_pool
    rows = await ps.PostgresService().memory_scored_search(
        tenant_id=tenant_id,
        embedding=embedding,
        query=query,
        search_params=sp,
        top_k=top_k,
        **kw,
    )
    return [str(r.Memory.id) for r in rows]


@pytest.fixture(autouse=True)
def _real_probe(monkeypatch):
    """Each test starts unprobed, so the real DB answers (CI: pgvector 0.8+)."""
    monkeypatch.setattr(ps, "_pgvector_version", None)
    yield


async def test_pool_wider_than_corpus_is_exact(tenant_id) -> None:
    """pool ⊇ corpus ⇒ identical ids in identical order vs the full scan."""
    tenant = tenant_id
    target = await _insert(tenant, "the quarterly irrigation schedule was revised")
    for i in range(11):
        await _insert(tenant, f"unrelated filler memory number {i} about topic {i}")

    probe = fake_embedding("the quarterly irrigation schedule was revised")
    baseline = await _search(
        tenant, embedding=probe, query="irrigation schedule", ann_pool=0
    )
    pooled = await _search(
        tenant, embedding=probe, query="irrigation schedule", ann_pool=1000
    )

    assert baseline, "baseline search returned nothing — seeding broke"
    assert pooled == baseline
    assert pooled[0] == str(target["id"])


async def test_ann_arm_admits_nearest_row_when_side_arms_miss_it(
    tenant_id, monkeypatch
) -> None:
    """Shrink the side arms so only the ANN arm can admit the target."""
    monkeypatch.setattr(ps, "_ANN_POOL_SIDE_ARM_LIMIT", 2)
    tenant = tenant_id
    # Target is seeded FIRST → oldest → outside a recency arm of 2.
    target = await _insert(tenant, "orca migration patterns in the norwegian fjords")
    for i in range(8):
        await _insert(tenant, f"newer filler about accounting quarter {i}")

    probe = fake_embedding("orca migration patterns in the norwegian fjords")
    # Query text matches nothing lexically — the fts arm cannot rescue it.
    ids = await _search(
        tenant, embedding=probe, query="zzznomatchzzz", ann_pool=3, top_k=3
    )
    assert ids and ids[0] == str(target["id"])


async def test_null_embedding_fts_row_survives_pool_mode(
    tenant_id, monkeypatch
) -> None:
    """CAURA-594/679: deferred-embed rows stay discoverable via the fts arm."""
    monkeypatch.setattr(ps, "_ANN_POOL_SIDE_ARM_LIMIT", 2)
    tenant = tenant_id
    pending = await _insert(tenant, "xylospectral calibration notes", embedding=None)
    for i in range(6):
        await _insert(tenant, f"embedded filler row {i}")

    probe = fake_embedding("something else entirely")
    ids = await _search(
        tenant, embedding=probe, query="xylospectral calibration", ann_pool=3
    )
    assert str(pending["id"]) in ids


async def test_boosted_id_admitted_despite_distant_embedding(
    tenant_id, monkeypatch
) -> None:
    """Entity-expansion ids reach the pool verbatim (boosted arm)."""
    monkeypatch.setattr(ps, "_ANN_POOL_SIDE_ARM_LIMIT", 2)
    tenant = tenant_id
    target = await _insert(
        tenant, "entity linked but lexically and semantically distant"
    )
    for i in range(8):
        await _insert(tenant, f"popular nearby filler {i}")

    probe = fake_embedding("popular nearby filler 0")
    tid = uuid.UUID(str(target["id"]))
    ids = await _search(
        tenant,
        embedding=probe,
        query="zzznomatchzzz",
        ann_pool=2,
        boosted_memory_ids={tid},
        memory_boost_factor={tid: 1.3},
    )
    assert str(target["id"]) in ids


async def test_agent_scoped_rows_stay_invisible_in_pool_mode(tenant_id) -> None:
    """Every pool arm carries the visibility filters — no widening via the pool."""
    tenant = tenant_id
    secret = await _insert(
        tenant,
        "agent private note about the launch",
        visibility="scope_agent",
        agent_id="agent-a",
    )
    await _insert(tenant, "team visible note about the launch")

    probe = fake_embedding("agent private note about the launch")
    for pool in (0, 1000):
        ids = await _search(
            tenant,
            embedding=probe,
            query="launch note",
            ann_pool=pool,
            caller_agent_id="agent-b",
        )
        assert str(secret["id"]) not in ids, f"scope_agent row leaked (ann_pool={pool})"


async def test_old_pgvector_behaves_identically_to_knob_off(
    tenant_id, monkeypatch
) -> None:
    tenant = tenant_id
    await _insert(tenant, "fallback parity check row one")
    await _insert(tenant, "fallback parity check row two")

    probe = fake_embedding("fallback parity check row one")
    baseline = await _search(
        tenant, embedding=probe, query="fallback parity", ann_pool=0
    )

    monkeypatch.setattr(ps, "_pgvector_version", (0, 7, 4))
    fallback = await _search(
        tenant, embedding=probe, query="fallback parity", ann_pool=500
    )
    assert fallback == baseline


async def test_ann_arm_is_hnsw_servable(tenant_id, monkeypatch) -> None:
    """With seq scan and sort disabled, the pooled statement plans through the
    HNSW index — proving the arm's shape is index-servable (the planner's free
    choice at real scale is covered by the rig measurements in the plan doc;
    at test-corpus sizes it legitimately prefers a seq scan)."""
    tenant = tenant_id
    for i in range(20):
        await _insert(tenant, f"plan shape corpus row {i}")

    captured: list = []

    class _Stop(Exception):
        pass

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            captured.append(stmt)
            if len(captured) >= 3:
                raise _Stop
            return None

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    real_get_read_session = ps.get_read_session
    monkeypatch.setattr(ps, "_pgvector_version", (0, 8, 1))
    monkeypatch.setattr(ps, "get_read_session", _fake_session)
    sp = dict(_SP)
    sp["ann_pool_size"] = 10
    with contextlib.suppress(_Stop):
        await ps.PostgresService().memory_scored_search(
            tenant_id=tenant,
            embedding=fake_embedding("plan shape corpus row 0"),
            query="plan shape corpus",
            search_params=sp,
            top_k=5,
        )
    stmt = captured[-1]
    monkeypatch.setattr(ps, "get_read_session", real_get_read_session)

    async with real_get_read_session() as session:
        for guc in (
            "SELECT set_config('hnsw.ef_search', '100', true)",
            "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)",
            "SELECT set_config('enable_seqscan', 'off', true)",
            "SELECT set_config('enable_sort', 'off', true)",
        ):
            await session.execute(text(guc))
        result = await session.execute(_ExplainJSON(stmt))
        raw = result.scalar()
    plan_doc = json.loads(raw) if isinstance(raw, str) else raw
    assert "ix_memories_embedding_hnsw" in json.dumps(plan_doc), (
        "the ANN arm did not plan through the HNSW index even with seq scan "
        "and sort disabled — the arm's ORDER BY is no longer index-servable"
    )


async def test_read_session_preserves_set_local_across_executes() -> None:
    """The session property the ann-mode GUC pinning depends on.

    ``memory_scored_search`` issues two ``set_config(..., is_local=true)``
    executes and THEN the pooled statement on the same ``get_read_session``
    session, relying on SQLAlchemy's autobegin holding one transaction across
    all three. ``_build_engine`` sets no isolation override today; if the
    reader engine is ever flipped to AUTOCOMMIT (per-statement transactions),
    SET LOCAL evaporates between executes and the ANN pool silently loses its
    scan guarantees. This fails loudly instead: the value set in one execute
    must be visible to the next on the same session.
    """
    async with ps.get_read_session() as session:
        await session.execute(select(func.set_config("hnsw.ef_search", "123", True)))
        observed = (await session.execute(text("SHOW hnsw.ef_search"))).scalar()
    assert observed == "123", (
        f"SET LOCAL did not survive to the next execute (saw {observed!r}) — "
        "the reader session is no longer one transaction per checkout"
    )


async def test_real_search_path_has_gucs_live_at_statement_time(
    tenant_id, monkeypatch
) -> None:
    """The ACTUAL ann-mode code path, observed mid-flight.

    Unlike test_ann_arm_is_hnsw_servable (which re-applies GUCs by hand to
    prove the arm is index-servable), this exercises memory_scored_search
    itself: the real session, the real set_config calls, the real statement —
    and asserts the GUC values are live on that session at the moment the
    pooled statement executes. If the set_config effects did not persist to
    the main statement (per-statement transactions, a session swap, a future
    refactor reordering the block), this is the test that fails.
    """
    observed: dict = {}
    real_get_read_session = ps.get_read_session

    @contextlib.asynccontextmanager
    async def _instrumented():
        async with real_get_read_session() as session:

            class _Spy:
                async def execute(self, stmt, *args, **kwargs):
                    if "candidate_pool" in str(stmt):
                        observed["ef_search"] = (
                            await session.execute(text("SHOW hnsw.ef_search"))
                        ).scalar()
                        observed["iterative_scan"] = (
                            await session.execute(text("SHOW hnsw.iterative_scan"))
                        ).scalar()
                    return await session.execute(stmt, *args, **kwargs)

            yield _Spy()

    monkeypatch.setattr(ps, "get_read_session", _instrumented)
    sp = dict(_SP)
    sp["ann_pool_size"] = 200
    rows = await ps.PostgresService().memory_scored_search(
        tenant_id=tenant_id,
        embedding=fake_embedding("guc liveness probe"),
        query="guc liveness probe",
        search_params=sp,
        top_k=5,
    )
    assert rows == []  # empty tenant; the assertion is the GUC observation
    assert observed.get("ef_search") == "200", (
        f"hnsw.ef_search not live at statement time: {observed!r}"
    )
    assert observed.get("iterative_scan") == "relaxed_order", (
        f"hnsw.iterative_scan not live at statement time: {observed!r}"
    )
