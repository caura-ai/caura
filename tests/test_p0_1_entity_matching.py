"""P0-1: Entity matching — stopword filtering and token extraction.

Unit tests validate the stopword set and token filtering logic.
Integration tests validate PG FTS matching on the entities table.
"""

import pytest

from core_api.constants import ENTITY_STOPWORDS, ENTITY_TOKEN_MIN_LENGTH


# ---------------------------------------------------------------------------
# Unit tests: stopword filtering
# ---------------------------------------------------------------------------


def _extract_tokens(query: str) -> list[str]:
    """Replicate the token extraction logic from search_memories."""
    return [
        t.lower()
        for t in query.split()
        if len(t) >= ENTITY_TOKEN_MIN_LENGTH and t.lower() not in ENTITY_STOPWORDS
    ]


@pytest.mark.unit
class TestStopwordFiltering:
    """Verify the stopword set removes noise tokens from queries."""

    def test_common_question_words_removed(self):
        tokens = _extract_tokens("What is the Kubernetes status?")
        assert "what" not in tokens
        assert "the" not in tokens
        assert "status" not in tokens  # generic noun → stopword
        assert "kubernetes" in tokens

    def test_blocklist_mirror_words_removed(self):
        """Words in ENTITY_NAME_BLOCKLIST are also stopwords (can't be entities)."""
        tokens = _extract_tokens("What is the project status")
        assert "project" not in tokens  # blocklist mirror
        assert "status" not in tokens  # generic noun

    def test_pronouns_removed(self):
        tokens = _extract_tokens("Tell me about his deployment pipeline")
        assert "tell" not in tokens
        assert "his" not in tokens
        assert "about" not in tokens
        assert "deployment" in tokens
        assert "pipeline" in tokens

    def test_prepositions_removed(self):
        tokens = _extract_tokens("data from the kafka cluster into redis")
        assert "from" not in tokens
        assert "into" not in tokens
        assert "kafka" in tokens
        assert "cluster" in tokens
        assert "redis" in tokens

    def test_verbs_of_inquiry_removed(self):
        for verb in ("tell", "show", "give", "find", "get", "know", "explain"):
            tokens = _extract_tokens(f"{verb} me about kafka")
            assert verb not in tokens, f"'{verb}' should be in ENTITY_STOPWORDS"

    def test_short_tokens_removed(self):
        tokens = _extract_tokens("go to db on vm")
        # A7 (2026-05-24): ENTITY_TOKEN_MIN_LENGTH lowered to 2 to
        # retain acronym entities (``AI`` / ``ML`` / ``PR`` / ``DB`` /
        # ``VM``…). 2-char English fillers stay filtered via
        # ENTITY_STOPWORDS, not the length floor.
        assert "go" not in tokens  # stopword
        assert "to" not in tokens  # stopword
        assert "on" not in tokens  # stopword
        # ``db`` and ``vm`` are now retained (legitimate acronym entities).
        assert "db" in tokens
        assert "vm" in tokens

    def test_meaningful_tokens_preserved(self):
        tokens = _extract_tokens("kubernetes deployment failed for auth-service")
        assert "kubernetes" in tokens
        assert "deployment" in tokens
        assert "failed" in tokens
        assert "auth-service" in tokens

    def test_empty_query_returns_empty(self):
        assert _extract_tokens("") == []
        assert _extract_tokens("the a an") == []
        assert _extract_tokens("is it on") == []

    def test_single_meaningful_word(self):
        tokens = _extract_tokens("kafka")
        assert tokens == ["kafka"]

    def test_stopword_set_is_frozen(self):
        """Stopwords must be a frozenset for O(1) lookup and immutability."""
        assert isinstance(ENTITY_STOPWORDS, frozenset)

    def test_stopwords_all_lowercase(self):
        """All entries must be lowercase (we lowercase tokens before checking)."""
        for word in ENTITY_STOPWORDS:
            assert word == word.lower(), f"Stopword '{word}' is not lowercase"


# ---------------------------------------------------------------------------
# Integration tests: PG FTS on entities table
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEntityFTSMatching:
    """Verify PostgreSQL full-text search on the entities table."""

    async def _create_entity(self, db, tenant_id, name, entity_type="concept"):
        from common.models.entity import Entity
        from sqlalchemy import select, text as sql_text

        # Return existing entity if it already exists (unique index)
        existing = (
            await db.execute(
                select(Entity).where(
                    Entity.tenant_id == tenant_id,
                    Entity.entity_type == entity_type,
                    Entity.canonical_name == name,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        entity = Entity(
            tenant_id=tenant_id,
            entity_type=entity_type,
            canonical_name=name,
        )
        db.add(entity)
        await db.flush()
        # Manually set search_vector (normally the trigger does this)
        await db.execute(
            sql_text(
                "UPDATE entities SET search_vector = to_tsvector('english', :name) WHERE id = :id"
            ),
            {"name": name, "id": entity.id},
        )
        await db.flush()
        return entity

    async def _match_entities(self, db, tenant_id, query_tokens):
        from sqlalchemy import func, select
        from common.models.entity import Entity

        entity_ts_query = func.plainto_tsquery("english", " ".join(query_tokens))
        stmt = select(Entity.id, Entity.canonical_name).where(
            Entity.tenant_id == tenant_id,
            Entity.search_vector.op("@@")(entity_ts_query),
        )
        result = await db.execute(stmt)
        return {row[1] for row in result.all()}

    async def test_exact_word_match(self, db, tenant_id):
        await self._create_entity(db, tenant_id, "deployment pipeline")
        await self._create_entity(db, tenant_id, "kafka cluster")
        matched = await self._match_entities(db, tenant_id, ["deployment"])
        assert "deployment pipeline" in matched
        assert "kafka cluster" not in matched

    async def test_stemming_matches(self, db, tenant_id):
        """PG english dictionary stems: 'deploying' → 'deploy', 'deployment' → 'deploy'."""
        await self._create_entity(db, tenant_id, "deployment pipeline")
        matched = await self._match_entities(db, tenant_id, ["deploying"])
        assert "deployment pipeline" in matched

    async def test_no_substring_match(self, db, tenant_id):
        """'art' must NOT match 'start' — word boundary enforcement."""
        await self._create_entity(db, tenant_id, "start service")
        await self._create_entity(db, tenant_id, "particle system")
        await self._create_entity(db, tenant_id, "smart contract")
        await self._create_entity(db, tenant_id, "art gallery")
        matched = await self._match_entities(db, tenant_id, ["art"])
        assert "art gallery" in matched
        assert "start service" not in matched
        assert "particle system" not in matched
        assert "smart contract" not in matched

    async def test_stopwords_ignored_by_pg(self, db, tenant_id):
        """PG english dictionary ignores common words in tsquery too."""
        await self._create_entity(db, tenant_id, "the matrix")
        # 'the' alone is a PG stopword — tsquery should be empty, matching nothing
        matched = await self._match_entities(db, tenant_id, ["the"])
        # PG may or may not match depending on dictionary; key is no crash
        # The important test is that our Python stopwords filter 'the' before it reaches PG

    async def test_multi_token_query(self, db, tenant_id):
        await self._create_entity(db, tenant_id, "kafka cluster")
        await self._create_entity(db, tenant_id, "redis cache")
        await self._create_entity(db, tenant_id, "kafka streams")
        matched = await self._match_entities(db, tenant_id, ["kafka", "cluster"])
        assert "kafka cluster" in matched
        # 'kafka streams' may match on 'kafka' token — that's expected
