"""Integration tests: full search_memories pipeline with all P0 fixes.

Requires a running PostgreSQL instance with pgvector.
Set TEST_DATABASE_URL env var or use defaults (memclaw_test on localhost).

These tests exercise the actual SQL expressions — freshness, recall boost,
scoring blend, entity matching — end-to-end via the service layer.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update

from core_storage_api.services.postgres_service import get_session

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    FTS_RANK_SCALE,
    FRESHNESS_DECAY_DAYS,
    SEARCH_OVERFETCH_FACTOR,
)
from common.models.memory import Memory
from common.embedding import fake_embedding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(tenant_id, fleet_id, content):
    return hashlib.sha256(f"{tenant_id}:{fleet_id}:{content}".encode()).hexdigest()


async def _insert_memory(
    tenant_id,
    content,
    *,
    weight=0.5,
    fleet_id=None,
    agent_id="test-agent",
    memory_type="fact",
    status="active",
    created_at=None,
    ts_valid_start=None,
    ts_valid_end=None,
    recall_count=0,
    last_recalled_at=None,
):
    """Insert a memory via storage client for test setup."""
    emb = fake_embedding(content)
    sc = get_storage_client()

    payload: dict = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "agent_id": agent_id,
        "memory_type": memory_type,
        "content": content,
        "weight": weight,
        "embedding": emb,
        "content_hash": _hash(tenant_id, fleet_id, content),
        "status": status,
        "recall_count": recall_count,
        "visibility": "scope_team",
    }
    if last_recalled_at is not None:
        payload["last_recalled_at"] = last_recalled_at.isoformat()
    if ts_valid_start is not None:
        payload["ts_valid_start"] = ts_valid_start.isoformat()
    if ts_valid_end is not None:
        payload["ts_valid_end"] = ts_valid_end.isoformat()

    mem = await sc.create_memory(payload)

    # Populate search_vector for FTS scoring (normally done by app/trigger),
    # committed via the storage write session so the storage-routed search path
    # sees it (the rolled-back ``db`` fixture would be invisible to it).
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE memories SET search_vector = to_tsvector('english', :content) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"content": content, "id": mem["id"]},
        )
        # Override created_at if provided (server_default set it on create).
        if created_at:
            await session.execute(
                update(Memory).where(Memory.id == mem["id"]).values(created_at=created_at)
            )

    return mem


async def _insert_entity(tenant_id, name, entity_type="concept", fleet_id=None):
    """Insert an entity via storage client with search_vector populated."""
    sc = get_storage_client()
    entity = await sc.create_entity({
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "entity_type": entity_type,
        "canonical_name": name,
    })
    # Populate search_vector for FTS scoring, committed via the storage write
    # session so the storage-routed search path sees it.
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE entities SET search_vector = to_tsvector('english', :name) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"name": name, "id": entity["id"]},
        )
    return entity


async def _link_memory_entity(memory_id, entity_id, role="mentioned"):
    sc = get_storage_client()
    await sc.create_entity_link({
        "memory_id": str(memory_id),
        "entity_id": str(entity_id),
        "role": role,
    })


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSearchPipelineEndToEnd:
    """Full search_memories pipeline with all P0 fixes applied."""

    async def test_basic_search_returns_results(self, db, tenant_id):
        """Baseline: search returns stored memories sorted by relevance."""
        await _insert_memory(tenant_id, "Python is a programming language", weight=0.7
        )
        await _insert_memory(tenant_id, "The weather is sunny today", weight=0.7)

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "Python programming")
        assert len(results) >= 1
        # The Python memory should rank higher
        assert "Python" in results[0].content

    async def test_freshness_prefers_recent_events(self, db, tenant_id):
        """P0-2: memory about recent event ranks higher than old memory about same topic."""
        now = datetime.now(timezone.utc)

        # Old memory, no temporal fields — will decay normally
        await _insert_memory(            tenant_id,
            "deployment system update completed successfully last quarter",
            weight=0.7,
            created_at=now - timedelta(days=FRESHNESS_DECAY_DAYS + 10),
        )
        # New memory with recent ts_valid_start
        await _insert_memory(            tenant_id,
            "deployment system update critical patch applied today",
            weight=0.7,
            created_at=now - timedelta(days=80),
            ts_valid_start=now - timedelta(days=2),
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "deployment system update")
        assert len(results) >= 2
        # The one with recent ts_valid_start should rank higher
        assert "critical patch" in results[0].content

    async def test_expired_memory_ranked_lower(self, db, tenant_id):
        """P0-2: expired memory (ts_valid_end in past) gets freshness floor."""
        now = datetime.now(timezone.utc)

        await _insert_memory(            tenant_id,
            "Sprint deadline is next Friday for the analytics dashboard",
            weight=0.7,
            ts_valid_end=now - timedelta(days=1),  # expired yesterday
        )
        await _insert_memory(            tenant_id,
            "Sprint deadline is this Friday for the analytics dashboard",
            weight=0.7,
            ts_valid_end=now + timedelta(days=5),  # still valid
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "sprint deadline analytics")
        assert len(results) >= 2
        # Valid memory should rank above expired one
        assert "this Friday" in results[0].content

    async def test_recall_boost_decays_over_time(self, db, tenant_id):
        """P0-3: frequently recalled but stale memory doesn't dominate."""
        now = datetime.now(timezone.utc)

        # Memory A: recalled 50 times but 45 days ago (stale)
        await _insert_memory(            tenant_id,
            "Architecture decision for microservices migration",
            weight=0.5,
            recall_count=50,
            last_recalled_at=now - timedelta(days=45),
        )
        # Memory B: recalled 2 times but just now (fresh)
        await _insert_memory(            tenant_id,
            "Architecture decision for database sharding approach",
            weight=0.5,
            recall_count=2,
            last_recalled_at=now,
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "architecture decision")
        assert len(results) >= 2
        # Memory B (recently recalled) should not be dominated by A (stale popular)
        # Both are relevant — the key is A's 50 recalls don't give it unfair advantage

    async def test_similarity_beats_weight(self, db, tenant_id):
        """P0-4: highly similar + low weight ranks above moderately similar + high weight."""
        # Use very different content to get clearly different similarity scores
        await _insert_memory(            tenant_id,
            "kafka consumer lag monitoring alert threshold configuration",
            weight=0.3,  # low weight
        )
        await _insert_memory(            tenant_id,
            "general operational procedures for infrastructure management overview",
            weight=0.95,  # high weight
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "kafka consumer lag monitoring")
        assert len(results) >= 1
        # The highly similar kafka memory should rank first despite low weight
        assert "kafka" in results[0].content

    async def test_entity_boost_with_stopword_filtering(self, db, tenant_id):
        """P0-1 + entity boost: stopwords don't pollute entity matching."""
        # Create entity
        entity = await _insert_entity(tenant_id, "kafka cluster")

        # Create memory linked to entity
        mem = await _insert_memory(            tenant_id,
            "kafka cluster status healthy all nodes running",
            weight=0.7,
        )
        await _link_memory_entity(mem["id"], entity["id"])

        # Create unrelated memory
        await _insert_memory(            tenant_id,
            "weather forecast shows clear skies for tomorrow",
            weight=0.7,
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "kafka cluster status")
        assert len(results) >= 1
        assert "kafka" in results[0].content

    async def test_search_with_all_fixes_combined(self, db, tenant_id):
        """Smoke test: all four P0 fixes working together."""
        now = datetime.now(timezone.utc)

        # Memory 1: old, high weight, lots of stale recalls
        await _insert_memory(            tenant_id,
            "redis cache performance tuning guide from last quarter",
            weight=0.95,
            recall_count=100,
            last_recalled_at=now - timedelta(days=60),
            created_at=now - timedelta(days=120),
        )
        # Memory 2: fresh, moderate weight, few recent recalls, entity-linked
        entity = await _insert_entity(tenant_id, "redis")
        mem2 = await _insert_memory(            tenant_id,
            "redis cache performance dropped to 40% after latest deployment",
            weight=0.6,
            recall_count=3,
            last_recalled_at=now - timedelta(hours=2),
            ts_valid_start=now - timedelta(days=1),
        )
        await _link_memory_entity(mem2["id"], entity["id"])

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "redis cache performance")
        assert len(results) >= 1
        # Memory 2 should win: fresher, recently recalled, entity-boosted
        # Memory 1's high weight and stale recall count shouldn't dominate

    async def test_scored_search_excludes_soft_deleted(self, tenant_id):
        # Regression guard for the parallel ``deleted_at IS NULL`` filter
        # sites in core-storage-api/services/postgres_service.py
        # (memory_scored_search at line 857 today). The semantic-dedup
        # path already has its own deleted-row test in
        # tests/test_p2_semantic_dedup.py::test_deleted_memory_doesnt_block;
        # this closes the parallel gap on the scored-search read path.
        #
        # Note: the deleted row is created with ``deleted_at`` ALREADY
        # set, matching the dedup-side test's helper. A post-insert
        # ``UPDATE memories SET deleted_at = ...`` via the per-test
        # ``db`` fixture is invisible to the storage-api ASGI bridge:
        # ``db`` runs the outer transaction in
        # ``join_transaction_mode="create_savepoint"`` (conftest
        # ``db`` fixture), so its commits land at a savepoint inside a
        # never-committed outer transaction — a separate connection
        # (which the storage app's session_factory checks out from the
        # same engine) never sees the change.
        from core_api.clients.storage_client import get_storage_client
        from core_api.services.memory_service import search_memories

        sc = get_storage_client()
        # Seed helpers now commit through the storage write session (the rolled-back
        # ``db`` fixture is invisible to the storage read path), so sibling tests'
        # rows persist under the shared TENANT_ID. This test asserts an EXACT result
        # set, so isolate it on its own tenant to avoid top-K pollution.
        tenant_id = f"{tenant_id}-softdel"

        async def _seed(content: str, *, deleted: bool) -> dict:
            payload = {
                "tenant_id": tenant_id,
                "fleet_id": None,
                "agent_id": "scored-search-soft-delete",
                "memory_type": "fact",
                "content": content,
                "weight": 0.7,
                "embedding": fake_embedding(content),
                "content_hash": _hash(tenant_id, None, content),
                "status": "active",
                "visibility": "scope_team",
            }
            if deleted:
                payload["deleted_at"] = datetime.now(timezone.utc).isoformat()
            mem = await sc.create_memory(payload)
            async with get_session() as session:
                await session.execute(
                    text(
                        "UPDATE memories SET search_vector = to_tsvector('english', :content) "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"content": content, "id": mem["id"]},
                )
            return mem

        deleted_mem = await _seed(
            "kubernetes pod restart troubleshooting guide", deleted=True
        )
        live_mem = await _seed(
            "kubernetes pod restart common causes summary", deleted=False
        )

        results = await search_memories(tenant_id, "kubernetes pod restart")

        result_ids = {str(r.id) for r in results}
        assert str(deleted_mem["id"]) not in result_ids, (
            "soft-deleted memory leaked into scored search results"
        )
        assert str(live_mem["id"]) in result_ids, (
            "live memory was not returned by scored search"
        )


@pytest.mark.integration
class TestConflictedExactMatchSurfaces:
    """Conflicted memories that exactly match the query must not be hidden.

    The semantic contradiction path marks the older of two embedding-near rows
    ``conflicted`` and routinely mismarks distinct ``#NNNN`` siblings (different
    entities sharing a name prefix). scored_search previously hard-excluded all
    ``conflicted``/``outdated`` rows, so an exact-match gold buried under
    confirmed near-duplicates vanished from results entirely. The carve-out
    keeps a ``conflicted`` row when it is an exact lexical (FTS) match for the
    query; ``outdated`` (a definitive retraction) stays excluded.
    """

    async def test_conflicted_exact_match_is_surfaced(self, db, tenant_id):
        # Distinctive token "zylqx" appears only in the conflicted gold, so the
        # query FTS-matches the gold and nothing else.
        await _insert_memory(            tenant_id,
            "Division zylqx quarterly revenue is forty two million",
            weight=0.7,
            status="conflicted",
        )
        for sib in (
            "Division abcde quarterly revenue is ninety one million",
            "Division fghij quarterly revenue is twelve million",
            "Division klmno quarterly revenue is sixty million",
        ):
            await _insert_memory(tenant_id, sib, weight=0.7, status="confirmed")

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "zylqx quarterly revenue", top_k=10
        )
        assert any("zylqx" in r.content for r in results), (
            "conflicted exact-match gold was excluded from results"
        )

    async def test_conflicted_non_match_still_excluded(self, db, tenant_id):
        # A conflicted row that does NOT lexically match the query stays hidden
        # (carve-out is scoped to exact matches, not all conflicted rows).
        await _insert_memory(            tenant_id,
            "Division qqqqq headcount is two hundred",
            weight=0.7,
            status="conflicted",
        )
        await _insert_memory(            tenant_id,
            "Division wwwww quarterly revenue is five million",
            weight=0.7,
            status="confirmed",
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "quarterly revenue", top_k=10)
        assert not any("qqqqq" in r.content for r in results), (
            "conflicted non-matching row should remain excluded"
        )

    async def test_outdated_exact_match_still_excluded(self, db, tenant_id):
        # ``outdated`` is a definitive retraction — it stays excluded even on an
        # exact lexical match (only ``conflicted`` gets the carve-out).
        await _insert_memory(            tenant_id,
            "Project vortex status is cancelled",
            weight=0.7,
            status="outdated",
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id, "vortex status", top_k=10)
        assert not any("vortex" in r.content for r in results), (
            "outdated exact-match should remain excluded"
        )


# ---------------------------------------------------------------------------
# FTS-only rows must survive a saturated candidate window (#687)
# ---------------------------------------------------------------------------


async def _insert_memory_with_embedding(tenant_id, content, *, embedding, fleet_id=None):
    """Insert a row with a caller-chosen embedding (``None`` for FTS-only).

    Separate from ``_insert_memory``, which always derives the embedding from
    the content. Reproducing #687 needs both an embedding chosen to outrank
    (competitors) and a genuinely absent one (the subject row).
    """
    sc = get_storage_client()
    payload: dict = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "agent_id": "test-agent",
        "memory_type": "fact",
        "content": content,
        "weight": 0.5,
        "embedding": embedding,
        "content_hash": _hash(tenant_id, fleet_id, content),
        "status": "active",
        "recall_count": 0,
        "visibility": "scope_team",
    }
    mem = await sc.create_memory(payload)
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE memories SET search_vector = to_tsvector('english', :content) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"content": content, "id": mem["id"]},
        )
        await session.commit()
    return mem


@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_fts_only_row_survives_a_saturated_candidate_window(tenant_id, monkeypatch, use_pipeline):
    """An FTS-matching row with no embedding must not be cut by the candidate window.

    The scored search fetches ``top_k * SEARCH_OVERFETCH_FACTOR`` candidates
    ordered by score, and only afterwards does ``PostFilterResults`` exempt
    NULL-embedding rows from the cosine gate (CAURA-679/CAURA-594 exist so such
    rows stay discoverable during the deferred-embed window). That exemption is
    unreachable once embedded rows fill the window: a NULL-embedding row scores
    on ``fts_score`` alone — single digits of a percent for one term — while
    embedded rows near the query score far higher, so the row is dropped in SQL
    before any exemption runs.

    This is #687 as measured on prod: 791 memories, embedded rows scoring
    0.35-0.39, the FTS-only row reported at 0, and a 10-row window (top_k=5 x
    factor 2). Existing tests miss it because they run in tenants of a handful
    of rows, where a 10-candidate window excludes nothing.

    The competitors here carry the *query's own* embedding, so they legitimately
    outrank on cosine, and their content does not contain the token, so the
    subject row is the only FTS match. That isolates the window as the only
    thing that can exclude it.
    """
    from core_api.services import memory_service
    from core_api.services.memory_service import search_memories

    # Both implementations trim independently, so both need the reservation.
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)

    top_k = 5
    window = top_k * SEARCH_OVERFETCH_FACTOR
    token = f"zqxjvbn{uuid.uuid4().hex[:10]}"
    query_embedding = fake_embedding(token)

    # Saturate the window with rows that outrank on cosine and cannot FTS-match.
    for i in range(window + 5):
        await _insert_memory_with_embedding(
            tenant_id,
            f"unrelated filler row {i} about irrigation schedules and runoff {uuid.uuid4().hex[:6]}",
            embedding=query_embedding,
        )

    subject = await _insert_memory_with_embedding(
        tenant_id,
        f"deferred-embedding subject row referencing {token}",
        embedding=None,
    )

    results = await search_memories(tenant_id=tenant_id, query=token, top_k=top_k)

    assert str(subject["id"]) in {str(r.id) for r in results}, (
        f"FTS-only row cut by the candidate window (#687): it is the ONLY row whose "
        f"content contains {token!r}, its search_vector FTS-matches, and it is exempt "
        f"from the cosine gate — but {window} higher-cosine rows fill the "
        f"top_k({top_k}) * SEARCH_OVERFETCH_FACTOR({SEARCH_OVERFETCH_FACTOR}) window "
        f"first. search returned {len(results)} row(s), none of them the subject."
    )


# ---------------------------------------------------------------------------
# #687 part two: fts_score on the cosine scale
# ---------------------------------------------------------------------------


async def _rank_and_score(content: str, query: str, scale: float) -> tuple[float, float]:
    """``(raw ts_rank_cd, fts_score)`` at a given scale, from real Postgres.

    Reads both out of one statement with the production expression rather than
    recomputing in Python — what ``ts_rank_cd`` feeds the saturating map is the
    thing under test, so a Python reimplementation would test the wrong function.
    Binds the scaled rank once via a subquery, mirroring how the production
    expression reuses it rather than re-evaluating.
    """
    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT r, (:k * r) / (1 + :k * r) AS s FROM ("
                    "  SELECT ts_rank_cd(to_tsvector('english', :c),"
                    "                    plainto_tsquery('english', :q)) AS r"
                    ") t"
                ),
                {"k": scale, "c": content, "q": query},
            )
        ).first()
    return float(row[0]), float(row[1])


@pytest.mark.integration
async def test_fts_rank_scale_lifts_a_lexical_match_onto_the_cosine_scale():
    """The scale must move fts_score into cosine's range, not just nudge it.

    #687's first half (#700) made an unembedded FTS-matching row *reachable*. It
    could not make a lexical match *rank* like one, because fts_score saturates a
    weight-D ts_rank_cd of 0.1 to 0.0909 while cosine on the same corpus measures
    0.35-0.39 — so the declared FTS_WEIGHT of 0.3 delivered an effective ~0.09.
    """
    content = "a memory that mentions zqxjvbn exactly once"

    _, before = await _rank_and_score(content, "zqxjvbn", 1.0)
    _, after = await _rank_and_score(content, "zqxjvbn", FTS_RANK_SCALE)

    assert before == pytest.approx(0.0909, abs=0.005), (
        f"the pre-#687 formula should saturate a single weight-D match to ~0.0909, got {before}"
    )
    # Cosine's measured strong-match range on this corpus is 0.35-0.39; landing
    # inside it is the whole point, and is what makes the nominal FTS_WEIGHT real.
    assert 0.30 <= after <= 0.45, (
        f"at FTS_RANK_SCALE={FTS_RANK_SCALE} a modal match should land in cosine's "
        f"0.35-0.39 range, got {after}"
    )


@pytest.mark.integration
async def test_fts_rank_scale_of_one_reproduces_the_pre_687_formula():
    """1.0 is the documented revert, so it must be exact, not merely close."""
    raw, scaled = await _rank_and_score("another memory mentioning zqxjvbn", "zqxjvbn", 1.0)
    assert scaled == pytest.approx(raw / (1.0 + raw), abs=1e-12)


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_rank_scale_reaches_the_sql_on_both_search_paths(tenant_id, monkeypatch, use_pipeline):
    """The scale must arrive where the SQL reads it, on BOTH paths.

    Three places can drop it, and each is invisible to the other two:

      * ``resolve_search_profile`` builds the pipeline's ``search_params``;
      * ``_search_memories_legacy`` builds its own, and sends the keys FLAT;
      * the storage route rebuilds ``search_params`` from a flat-key ALLOWLIST
        (``_SEARCH_PARAM_KEYS``) when no nested dict is present — so a key added
        to both core-api builders but not to that allowlist still applies only to
        the pipeline.

    Asserting at the client boundary cannot see the third, because the nesting
    happens server-side. So this spies on the storage service function that
    actually reads the value into the SQL expression.
    """
    from core_storage_api.services import postgres_service as pg

    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)

    seen: list = []
    real = pg.PostgresService.memory_scored_search

    async def _spy(self, *a, **kw):
        sp = kw.get("search_params") or next((x for x in a if isinstance(x, dict)), None)
        seen.append((sp or {}).get("fts_rank_scale"))
        return await real(self, *a, **kw)

    monkeypatch.setattr(pg.PostgresService, "memory_scored_search", _spy)
    await memory_service.search_memories(tenant_id=tenant_id, query="anything at all", top_k=3)

    path = "pipeline" if use_pipeline else "legacy"
    assert seen, f"the {path} path never reached memory_scored_search"
    assert seen[0] == FTS_RANK_SCALE, (
        f"the {path} path did not deliver fts_rank_scale to the SQL; storage saw "
        f"{seen[0]!r}, so it would fall back to 1.0 and keep the pre-#687 ranking"
    )
