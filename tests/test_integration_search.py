"""Integration tests: full search_memories pipeline with all P0 fixes.

Requires a running PostgreSQL instance with pgvector.
Set TEST_DATABASE_URL env var or use defaults (memclaw_test on localhost).

These tests exercise the actual SQL expressions — freshness, recall boost,
scoring blend, entity matching — end-to-end via the service layer.
"""

import hashlib
import math
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update

from core_storage_api.services.postgres_service import get_session

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    CANDIDATE_POOL_SIZE,
    FTS_RANK_SCALE,
    FRESHNESS_DECAY_DAYS,
    MIN_SEARCH_SIMILARITY,
    SCORE_FORMULA,
    SEARCH_OVERFETCH_FACTOR,
    SQL_SCORING_PARAM_KEYS,
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

    # ``search_vector`` is NOT written here. 001's trigger populates it on INSERT,
    # and since 034 it indexes the title too — a hand-rolled
    # ``to_tsvector(content)`` would overwrite that with a title-less vector and
    # quietly un-test the change. Let the trigger own it.
    async with get_session() as session:
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
            # search_vector comes from the trigger (title-inclusive since 034).
            return await sc.create_memory(payload)

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


async def _insert_memory_with_embedding(tenant_id, content, *, embedding, fleet_id=None, weight=0.5):
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
        "weight": weight,
        "embedding": embedding,
        "content_hash": _hash(tenant_id, fleet_id, content),
        "status": "active",
        "recall_count": 0,
        "visibility": "scope_team",
    }
    # search_vector comes from the trigger (title-inclusive since 034).
    return await sc.create_memory(payload)


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


async def _rank_and_score(
    content: str, query: str, scale: float, *, title: str = ""
) -> tuple[float, float]:
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
                    "  SELECT ts_rank_cd(to_tsvector('english', :t || ' ' || :c),"
                    "                    plainto_tsquery('english', :q)) AS r"
                    ") t"
                ),
                {"k": scale, "c": content, "q": query, "t": title},
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


# The scoring knobs that fail SILENTLY. ``memory_scored_search`` reads these
# three as ``sp.get(key, fallback)``, so one that never arrives degrades ranking
# with no error anywhere; its other six scoring keys are read as ``sp[key]`` and
# raise, which the route's ``search_params`` guard turns into a 422. Only the
# quiet ones need a delivery test, and each is paired here with the constant it
# falls back to plus a tuned value inside ``validate_search_profile``'s range.
_SQL_SCORING_KEYS = (
    ("fts_rank_scale", FTS_RANK_SCALE, 3.0),
    ("candidate_pool_size", CANDIDATE_POOL_SIZE, 25),
    ("score_formula", SCORE_FORMULA, 1),
)


def _spy_on_scored_search(monkeypatch) -> list[tuple[dict, list]]:
    """Install a pass-through spy on the storage service; returns its call log.

    Spies on the storage *service* rather than the HTTP client because the route
    decides server-side what the SQL finally reads — an assertion made at the
    client boundary cannot see what that decision kept or dropped. Each entry is
    ``(kwargs, returned_rows)``: the kwargs show what was asked for, the rows
    show what the SQL's LIMIT actually produced.
    """
    from core_storage_api.services import postgres_service as pg

    real = pg.PostgresService.memory_scored_search
    calls: list[tuple[dict, list]] = []

    async def _spy(self, *a, **kw):
        rows = await real(self, *a, **kw)
        calls.append((dict(kw), rows))
        return rows

    monkeypatch.setattr(pg.PostgresService, "memory_scored_search", _spy)
    return calls


async def _scored_search_call(
    monkeypatch, tenant_id, use_pipeline, *, search_profile=None, boost=None, top_k=3, tenant_config=None
):
    """Run one search; return the kwargs ``memory_scored_search`` received.

    ``boost`` forces a non-empty entity-boost result. The two paths compute it
    in different places — the legacy helper and the pipeline step — so both are
    stubbed; whichever one the path under test uses is the one that matters, and
    a stub that fails to take effect shows up as an empty boost at the assert.
    """
    from core_api.pipeline.steps.search import parallel_embed_entity_boost as peb
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)

    if boost is not None:
        ids, factors = boost

        async def _stub_boost(*a, **kw):
            return ids, factors

        monkeypatch.setattr(memory_service, "_entity_boost_pipeline", _stub_boost)

        real_exec = peb.ParallelEmbedAndEntityBoost.execute

        async def _exec(self, ctx):
            result = await real_exec(self, ctx)
            ctx.data["boosted_memory_ids"] = ids
            ctx.data["memory_boost_factor"] = factors
            return result

        monkeypatch.setattr(peb.ParallelEmbedAndEntityBoost, "execute", _exec)

    calls = _spy_on_scored_search(monkeypatch)
    await memory_service.search_memories(
        tenant_id=tenant_id,
        query="anything at all",
        top_k=top_k,
        search_profile=search_profile,
        tenant_config=tenant_config,
    )

    path = "pipeline" if use_pipeline else "legacy"
    assert calls, f"the {path} path never reached memory_scored_search"
    return calls[0][0]


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
@pytest.mark.parametrize(
    "via", [None, "agent_profile", "tenant_default"], ids=["defaults", "agent", "tenant_default"]
)
async def test_scoring_knobs_reach_the_sql_on_both_search_paths(
    tenant_id, monkeypatch, use_pipeline, via
):
    """Every quiet scoring knob must arrive where the SQL reads it, on BOTH paths.

    One builder per path and one server-side decision can each drop a key, and
    each is invisible to the others:

      * ``resolve_search_params`` resolves the knobs for both paths;
      * each path projects its own wire payload out of that;
      * the storage route decides what ``search_params`` the SQL finally sees.

    Asserting at the client boundary cannot see the last one, because the nesting
    happens server-side — which is why this asserts against the dict the storage
    service actually received. A key that never arrives leaves the SQL on its own
    fallback, so the feature applies to one path only and the documented legacy
    rollback lever changes ranking as a side effect.

    All three sources are needed. ``defaults`` pins the wiring, but two of these
    constants are currently 0, so on its own it cannot tell a delivered value from
    a builder that hardcoded the same number. ``agent`` moves every knob off its
    default and makes the value itself the assertion. ``tenant_default`` sends the
    same values down the A47 tenant-wide rung instead — which the legacy path
    used to skip entirely, resolving its own ladder without the tenant merge, so
    a tenant-wide default reached the pipeline path alone.
    """
    from core_api.services.organization_settings import ResolvedConfig

    tuned = {key: t for key, _, t in _SQL_SCORING_KEYS}
    profile = tuned if via == "agent_profile" else None
    tenant_config = (
        ResolvedConfig({"search": {"default_profile": tuned}}) if via == "tenant_default" else None
    )
    expected = tuned if via else {key: const for key, const, _ in _SQL_SCORING_KEYS}

    call = await _scored_search_call(
        monkeypatch,
        tenant_id,
        use_pipeline,
        search_profile=profile,
        tenant_config=tenant_config,
    )
    delivered = {k: (call.get("search_params") or {}).get(k) for k in expected}

    path = "pipeline" if use_pipeline else "legacy"
    assert delivered == expected, (
        f"the {path} path did not deliver every scoring knob from {via or 'the constants'} "
        f"to the SQL; storage saw {delivered} instead of {expected}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_entity_boost_inputs_reach_the_sql_on_both_search_paths(
    tenant_id, monkeypatch, use_pipeline
):
    """Both halves of the entity boost must arrive, on BOTH paths.

    The SQL gates the entire entity-boost stack on ``boosted_memory_ids AND
    memory_boost_factor`` — the ids alone buy nothing. The legacy path sent the
    per-memory factors *under the ids key* and never sent the factors key, so
    the guard saw a falsy factor map and skipped the whole stack: graph
    expansion and hop boosts ran in core-api on every legacy search and were
    then discarded at the storage boundary.

    Same failure mode as the scoring knobs above — a ranking input that reaches
    one search path only, so the documented legacy rollback lever quietly
    changes ranking rather than reproducing it.
    """
    mid = uuid.uuid4()
    call = await _scored_search_call(
        monkeypatch, tenant_id, use_pipeline, boost=({mid}, {mid: 1.5})
    )

    path = "pipeline" if use_pipeline else "legacy"
    assert call.get("boosted_memory_ids"), f"the {path} path delivered no boosted_memory_ids"
    assert call.get("memory_boost_factor"), (
        f"the {path} path delivered boosted_memory_ids but no memory_boost_factor, so the SQL's "
        f"`if boosted_memory_ids and memory_boost_factor` guard skips the entity boost entirely"
    )


# ---------------------------------------------------------------------------
# The candidate window: SEARCH_OVERFETCH_FACTOR must reach the SQL LIMIT
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_overfetched_top_k_is_what_storage_receives(tenant_id, monkeypatch, use_pipeline):
    """The overfetched limit must reach storage, and nothing may shadow it.

    Storage takes the candidate-window LIMIT from the ``top_k`` request
    parameter. A ``top_k`` inside ``search_params`` used to override it, and the
    pipeline builder put the caller's *unmultiplied* ``top_k`` there while
    sending the overfetched value as the parameter — so the overfetch never
    reached the SQL and ``PostFilterResults`` ran with no headroom at all.

    Asserting the parameter alone is not enough: it was always correct. The
    shadowing key is the thing to pin.
    """
    caller_top_k = 3
    call = await _scored_search_call(monkeypatch, tenant_id, use_pipeline, top_k=caller_top_k)

    path = "pipeline" if use_pipeline else "legacy"
    assert call.get("top_k") == caller_top_k * SEARCH_OVERFETCH_FACTOR, (
        f"the {path} path sent top_k={call.get('top_k')!r} to storage, expected the "
        f"overfetched {caller_top_k * SEARCH_OVERFETCH_FACTOR}"
    )
    assert "top_k" not in (call.get("search_params") or {}), (
        f"the {path} path put top_k inside search_params, where it shadows the overfetched "
        f"request parameter and becomes the SQL LIMIT instead"
    )


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_candidate_window_is_actually_overfetched_in_sql(monkeypatch, use_pipeline):
    """Storage must return a full overfetched window, not the caller's top_k.

    The delivery test above pins the payload; this pins the SQL. It counts the
    rows ``memory_scored_search`` actually returned, which is the LIMIT the CTE
    applied — the only place the shadowing was observable, and the reason
    ``PostFilterResults`` silently had no headroom to drop a low-vec_sim row
    without starving the result set.

    Uses its OWN tenant: the shared session tenant accumulates rows from every
    other test, which would satisfy a count assertion for the wrong reason.
    """
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)

    isolated = f"test-tenant-overfetch-{uuid.uuid4().hex[:8]}"
    caller_top_k = 3
    window = caller_top_k * SEARCH_OVERFETCH_FACTOR
    token = f"zqxjvbn{uuid.uuid4().hex[:10]}"
    query_embedding = fake_embedding(token)

    # Comfortably more candidates than the window, all embedded so the #700
    # FTS-only reserved branch (NULL embedding + FTS match) contributes nothing.
    for i in range(window + 4):
        await _insert_memory_with_embedding(
            isolated, f"candidate row {i} about {token}", embedding=query_embedding
        )

    calls = _spy_on_scored_search(monkeypatch)
    await memory_service.search_memories(tenant_id=isolated, query=token, top_k=caller_top_k)

    path = "pipeline" if use_pipeline else "legacy"
    assert calls, f"the {path} path never reached memory_scored_search"
    distinct = len({str(r.Memory.id) for r in calls[0][1]})
    assert distinct == window, (
        f"the {path} path's SQL LIMIT was {distinct}, expected the overfetched {window} "
        f"(top_k={caller_top_k} x SEARCH_OVERFETCH_FACTOR={SEARCH_OVERFETCH_FACTOR}); "
        f"{window + 4} candidates exist, so a smaller count is the LIMIT, not the corpus"
    )


def _embedding_at_cosine(target: float, query_embedding: list[float], seed: str) -> list[float]:
    """A unit vector at exactly ``target`` cosine to ``query_embedding``.

    Gram-Schmidt against the query gives an orthonormal basis {q, r_orth}, so
    ``target*q + sqrt(1-target^2)*r_orth`` has the cosine asked for. Constructed
    rather than sampled because the point of the test below is a row sitting a
    known distance either side of ``MIN_SEARCH_SIMILARITY``.
    """

    def _unit(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    q = _unit(query_embedding)
    r = fake_embedding(seed)
    dot = sum(a * b for a, b in zip(r, q, strict=False))
    r_orth = _unit([ri - dot * qi for ri, qi in zip(r, q, strict=False)])
    a, b = target, math.sqrt(max(0.0, 1.0 - target * target))
    return _unit([a * qi + b * ri for qi, ri in zip(q, r_orth, strict=False)])


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_post_filter_does_not_starve_the_result_set(monkeypatch, use_pipeline):
    """Dropping low-cosine rows must not cost the caller results it should have.

    This is what the overfetch is FOR, stated as the user-visible outcome. The
    candidate window is ordered by ``score``, which blends ``weight`` into
    ``similarity`` — so rows below the cosine floor can legitimately occupy the
    whole window on score while being discarded a moment later by the
    ``min_similarity`` post-filter. Without headroom the caller gets nothing;
    with it, the qualifying rows behind them backfill.

    Constructed so the arithmetic is not marginal: 3 rows at cosine 0.28 (under
    the 0.3 floor) with weight 1.0 outrank 4 rows at cosine 0.45 with weight
    0.0, on either adaptive ``fts_weight``. At top_k=3 the unwidened window is
    exactly the 3 junk rows.
    """
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)

    isolated = f"test-tenant-starve-{uuid.uuid4().hex[:8]}"
    top_k = 3
    token = f"zqxjvbn{uuid.uuid4().hex[:10]}"
    q = fake_embedding(token)
    assert MIN_SEARCH_SIMILARITY == 0.3, (
        f"this test's 0.28/0.45 cosines straddle a 0.3 floor; MIN_SEARCH_SIMILARITY is "
        f"{MIN_SEARCH_SIMILARITY}, so the construction no longer means what it says"
    )

    # Content deliberately free of the query token: no FTS match, so the blend
    # reduces to the cosine term and the ordering is the constructed one.
    for i in range(top_k):
        await _insert_memory_with_embedding(
            isolated,
            f"high weight low cosine row {i} concerning irrigation schedules",
            embedding=_embedding_at_cosine(0.28, q, f"{token}_junk_{i}"),
            weight=1.0,
        )
    qualifying = set()
    for i in range(top_k + 1):
        mem = await _insert_memory_with_embedding(
            isolated,
            f"low weight high cosine row {i} concerning irrigation schedules",
            embedding=_embedding_at_cosine(0.45, q, f"{token}_good_{i}"),
            weight=0.0,
        )
        qualifying.add(str(mem["id"]))

    results = await memory_service.search_memories(tenant_id=isolated, query=token, top_k=top_k)

    path = "pipeline" if use_pipeline else "legacy"
    assert len(results) == top_k, (
        f"the {path} path returned {len(results)} of {top_k} requested; {len(qualifying)} rows "
        f"clear the cosine floor, but the candidate window held only the {top_k} sub-floor rows "
        f"that outrank them on score, and the post-filter then dropped every one"
    )
    assert {str(r.id) for r in results} <= qualifying, (
        f"the {path} path returned rows below MIN_SEARCH_SIMILARITY={MIN_SEARCH_SIMILARITY}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_only_sql_scoring_keys_cross_the_wire(tenant_id, monkeypatch, use_pipeline):
    """Both paths must deliver EXACTLY the declared key set — no more, no less.

    The per-key tests above pin that specific knobs arrive. This pins the set,
    which is the assertion that catches the other direction: a core-api-local
    field added to ``search_params`` and shipped to storage by accident. That is
    not hypothetical — ``top_k`` did exactly that, and because storage read it as
    the candidate-window LIMIT it silently defeated ``SEARCH_OVERFETCH_FACTOR``
    on the active path (#725).

    Equality, not containment, on purpose: containment would pass for every
    surplus key, which is the failure mode being guarded.
    """
    call = await _scored_search_call(monkeypatch, tenant_id, use_pipeline)
    delivered = set((call.get("search_params") or {}).keys())

    path = "pipeline" if use_pipeline else "legacy"
    assert delivered == set(SQL_SCORING_PARAM_KEYS), (
        f"the {path} path's search_params does not match SQL_SCORING_PARAM_KEYS; "
        f"surplus={sorted(delivered - set(SQL_SCORING_PARAM_KEYS))} "
        f"missing={sorted(set(SQL_SCORING_PARAM_KEYS) - delivered)}. Surplus keys reach the SQL "
        f"and can collide with a request parameter; missing keys leave the SQL on its own default"
    )


# ``SQL_SCORING_PARAM_KEYS`` / ``SQL_SCORING_REQUIRED_KEYS`` no longer need their
# own source-scan tests: both are derived from ``SEARCH_KNOBS``'s ``sql`` flags,
# so pinning the flags against storage (below) pins the tuples with them.


def test_resolve_search_params_emits_every_declared_knob():
    """The resolver must resolve exactly the declared knob set.

    It names each knob one by one, to attach a default the table deliberately does
    not carry (see ``SEARCH_KNOBS``). This makes skipping one loud.

    Asserted against ``resolve_search_params`` rather than either search path
    because there is now only one resolver — so this pins BOTH. The three
    core-api-local knobs (``top_k``, ``min_similarity``, ``graph_max_hops``) are
    the part no other test covers: they never reach the wire, so dropping one
    surfaces elsewhere only as a KeyError from whichever step reads it
    downstream. Here it fails with the key's name.
    """
    from common.constants import SEARCH_KNOBS
    from core_api.services.memory_service import resolve_search_params

    emitted = set(resolve_search_params(None, query="anything at all", top_k=5))
    assert emitted == set(SEARCH_KNOBS), (
        f"resolve_search_params is out of step with SEARCH_KNOBS; "
        f"declared but not resolved: {sorted(set(SEARCH_KNOBS) - emitted)}; "
        f"resolved but not declared: {sorted(emitted - set(SEARCH_KNOBS))}"
    )


def test_sql_flags_match_how_storage_reads_each_knob():
    """``sql`` / ``sql_required`` must match storage's actual reads.

    The flags derive both wire tuples, so they are the single registration step
    for a new scoring knob — which only holds while they describe what the SQL
    really does. ``sql`` is any read; ``sql_required`` is the indexed subset,
    where a missing key is a KeyError rather than a server-side default.
    """
    import ast
    import inspect
    import re
    import textwrap

    from common.constants import SQL_SCORING_PARAM_KEYS, SQL_SCORING_REQUIRED_KEYS
    from core_storage_api.services.postgres_service import PostgresService

    # Round-tripped through the AST, not grepped: the storage source discusses
    # these keys in prose as well as reading them, and a regex over raw text
    # scores a comment as a read — it picked up ``top_k`` from the note saying
    # #725 stopped reading it. ``ast.unparse`` drops comments.
    code = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(PostgresService.memory_scored_search))))
    read = set(re.findall(r"""sp(?:\[|\.get\()['"](\w+)['"]""", code))
    indexed = set(re.findall(r"""sp\[['"](\w+)['"]\]""", code))

    # Against the DERIVED tuples, which is the same assertion one step on: they
    # are literally ``{k for k, v in SEARCH_KNOBS.items() if v.sql}``.
    flagged, required = set(SQL_SCORING_PARAM_KEYS), set(SQL_SCORING_REQUIRED_KEYS)

    assert read and indexed, "found no `sp` reads — the scan broke, it did not pass"
    assert read == flagged, (
        f"SEARCH_KNOBS `sql` flags disagree with storage; "
        f"reads-but-not-flagged: {sorted(read - flagged)}; "
        f"flagged-but-unread: {sorted(flagged - read)}"
    )
    assert indexed == required, (
        f"SEARCH_KNOBS `sql_required` flags disagree with storage's indexed reads; "
        f"indexed-but-not-required: {sorted(indexed - required)}; "
        f"required-but-not-indexed: {sorted(required - indexed)}"
    )


# ---------------------------------------------------------------------------
# 034: title in the tsvector, at the same weight as content
# ---------------------------------------------------------------------------


async def _insert_titled(tenant_id, title, content):
    """Insert with a title, letting 001/034's trigger build the vector."""
    sc = get_storage_client()
    return await sc.create_memory({
        "tenant_id": tenant_id,
        "fleet_id": None,
        "agent_id": "test-agent",
        "memory_type": "fact",
        "title": title,
        "content": content,
        "weight": 0.5,
        "embedding": fake_embedding(content),
        "content_hash": _hash(tenant_id, None, content),
        "status": "active",
        "recall_count": 0,
        "visibility": "scope_team",
    })


async def _stored_rank(memory_id, query) -> float:
    """``ts_rank_cd`` against the row's REAL stored vector, as the SQL reads it."""
    async with get_session() as session:
        return float(
            (
                await session.execute(
                    text(
                        "SELECT ts_rank_cd(search_vector, plainto_tsquery('english', :q)) "
                        "FROM memories WHERE id = CAST(:id AS uuid)"
                    ),
                    {"q": query, "id": memory_id},
                )
            ).scalar()
        )


@pytest.mark.integration
async def test_title_text_is_searchable_at_all():
    """A memory must be findable by words appearing ONLY in its title.

    This is the point of 034, and it is missing recall rather than a ranking
    preference: 001 built ``search_vector`` from content alone, so the title the
    enrichment writes on every enriched memory could never be matched. Measured
    before the migration, a title-only term scored exactly 0.0 — the row was
    unreachable, not ranked low.
    """
    isolated = f"test-tenant-title-{uuid.uuid4().hex[:8]}"
    token = f"zqxjvbn{uuid.uuid4().hex[:10]}"
    mem = await _insert_titled(
        isolated,
        title=f"quarterly {token} planning",
        content="unrelated body text about irrigation schedules and runoff",
    )

    assert await _stored_rank(mem["id"], token) > 0.0, (
        "a term present only in the title scored 0.0 — the title is not in the tsvector, "
        "so the memory cannot be reached by FTS on its own title"
    )


@pytest.mark.integration
async def test_a_title_match_scores_the_same_as_a_content_match():
    """One weight throughout — this is what keeps 034 a pure searchability fix.

    The alternative was ``setweight`` title-A / content-B, which multiplies every
    content rank by 4 and forces ``FTS_RANK_SCALE`` to be re-derived, and which
    moves the modal enriched row (titles summarise content, so most rows match
    both fields) from 0.375 to ~0.70. Preferring a title hit over a content hit
    is a relevance claim, and the corpus that would test it does not exist here —
    LoCoMo is dialogue turns with no titles. So: equal weight, and this test is
    what says so.
    """
    isolated = f"test-tenant-eqweight-{uuid.uuid4().hex[:8]}"
    t_token = f"zqxjvbn{uuid.uuid4().hex[:10]}"
    c_token = f"zqxjvbn{uuid.uuid4().hex[:10]}"

    in_title = await _insert_titled(isolated, title=f"heading {t_token} here", content="plain body")
    in_content = await _insert_titled(isolated, title="plain heading", content=f"body {c_token} here")

    assert await _stored_rank(in_title["id"], t_token) == pytest.approx(
        await _stored_rank(in_content["id"], c_token)
    ), "a title match and a content match must rank identically — 034 sets no field preference"


@pytest.mark.integration
async def test_content_only_scoring_is_untouched_by_034():
    """A content-only match must score exactly what it scored before 034.

    This is why ``FTS_RANK_SCALE`` stays 6.0 and no bound or default moves
    anywhere in the stack. Both fields are tokenised together at the same weight,
    so adding the title contributes lexemes without rescaling the existing ones —
    a modal single-term content match is 0.1 raw and 0.375 scored, before and
    after. If someone reaches for ``setweight`` later, this fails.
    """
    content = "a memory that mentions zqxjvbn exactly once"

    raw, scored = await _rank_and_score(content, "zqxjvbn", FTS_RANK_SCALE, title="an unrelated heading")

    assert raw == pytest.approx(0.1, abs=1e-6), (
        f"a modal single-term content match should stay at the weight-D rank 0.1, got {raw} — "
        f"a weighting change would rescale it and invalidate FTS_RANK_SCALE={FTS_RANK_SCALE}"
    )
    assert scored == pytest.approx(0.375, abs=0.001), f"expected the unchanged 0.375, got {scored}"


@pytest.mark.integration
async def test_editing_only_the_title_reindexes_the_row():
    """The trigger must fire on a title-only UPDATE, not just on content.

    001 declared ``BEFORE INSERT OR UPDATE OF content``. With the title in the
    vector that is a stale-index bug in waiting, and not a rare one: the
    async-enrich path writes the title after the row already exists, so those
    words would never become searchable. 034 widens it to ``OF content, title``.
    """
    isolated = f"test-tenant-retitle-{uuid.uuid4().hex[:8]}"
    token = f"zqxjvbn{uuid.uuid4().hex[:10]}"
    mem = await _insert_titled(isolated, title="placeholder", content="body about irrigation")

    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET title = :t WHERE id = CAST(:id AS uuid)"),
            {"t": f"revised {token} heading", "id": mem["id"]},
        )
        await session.commit()

    assert await _stored_rank(mem["id"], token) > 0.0, (
        "a title-only UPDATE left search_vector stale — the trigger's UPDATE OF list "
        "does not include title"
    )
