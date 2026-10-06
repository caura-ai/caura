import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.constants import VECTOR_DIM
from common.models.base import Base

#: Row-state predicate for "this live memory still has background work coming".
#: Each disjunct is a durable marker the async write path sets and clears on the
#: row itself, so it survives process restarts (no in-memory counter):
#:
#: * ``embedding IS NULL`` -- the vector has not landed. core-worker's embed
#:   PATCH (or the backfill sweep) fills it.
#: * ``enrichment_pending = true`` -- set by the fast single-write path and by
#:   the deferred bulk path; the worker's enrich PATCH writes ``false`` into
#:   both homes. ``_system`` wins over the legacy top-level key, as it does in
#:   ``extract_system_metadata``.
#: * ``atomic_facts`` is set and not JSON ``null`` -- the worker persisted
#:   facts that core-api's ENRICHED consumer has not fanned out into child rows
#:   yet; the consumer overwrites the key with JSON ``null`` once it has
#:   (``->>`` yields SQL NULL for that).
#:
#: Only ``->`` / ``->>`` are used: the migrated ``metadata`` column is ``json``
#: (migration 001) while ``create_all`` builds it ``jsonb``, and these operators
#: are the ones both types share.
#:
#: Shared verbatim by ``ix_memories_pending_work`` below and by the storage
#: query that counts pending work, so the planner can match the partial index.
PENDING_EMBEDDING_SQL = "embedding IS NULL"
PENDING_ENRICHMENT_SQL = (
    "COALESCE(metadata -> '_system' ->> 'enrichment_pending', "
    "metadata ->> 'enrichment_pending') = 'true'"
)
PENDING_FANOUT_SQL = "(metadata ->> 'atomic_facts') IS NOT NULL"
PENDING_WORK_SQL = (
    f"({PENDING_EMBEDDING_SQL} OR {PENDING_ENRICHMENT_SQL} OR {PENDING_FANOUT_SQL})"
)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(VECTOR_DIM))
    weight: Mapped[float] = mapped_column(Float, server_default=text("0.5"))
    source_uri: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(Text)
    # ``json``, not JSONB: migration 001 creates it that way (CAURA-595), and
    # ``test_models_match_the_migrated_schema`` holds the model to the schema.
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    title: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    # Provenance for ``embedding``: the ``content_hash`` of the text the
    # vector was actually computed from. Without it a vector left over
    # from earlier content is byte-identical to a correct one, so a
    # mis-embedded row is undetectable — ``embedding IS NOT NULL`` says
    # only that *something* was embedded, never *what*.
    #
    # Staleness is then expressible:
    #     embedding IS NOT NULL
    #     AND embedded_content_hash IS NOT NULL      -- provenance known
    #     AND embedded_content_hash IS DISTINCT FROM content_hash
    #
    # NULL means "provenance unknown", NOT "stale": every row written
    # before migration 037 has no recorded hash, and calling those stale
    # would report the entire historical corpus as damaged.
    #
    # Which is why the second line is load-bearing rather than redundant:
    # ``NULL IS DISTINCT FROM <hash>`` is TRUE, so dropping it silently
    # folds every unknown row into the stale count.
    embedded_content_hash: Mapped[str | None] = mapped_column(Text)
    # Per-attempt idempotency token (CAURA-602). Server-derived from
    # ``X-Bulk-Attempt-Id + ":" + index`` on the bulk path; NULL for
    # single-write and pre-rollout rows. The partial unique index
    # ``ix_memories_attempt_unique`` (migration 007) enforces uniqueness
    # only when this is non-NULL, so legacy paths are unaffected.
    client_request_id: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_vector = mapped_column(TSVECTOR)

    # RDF triple representation
    subject_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="SET NULL"),
    )
    predicate: Mapped[str | None] = mapped_column(Text)
    object_value: Mapped[str | None] = mapped_column(Text)

    # Temporal validity windows
    ts_valid_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ts_valid_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Status lifecycle
    status: Mapped[str] = mapped_column(
        Text, server_default=text("'active'"), nullable=False
    )

    # Visibility scope
    visibility: Mapped[str] = mapped_column(
        Text,
        server_default=text("'scope_team'"),
        nullable=False,
    )

    # Recall tracking (incremented on agent-facing retrievals only)
    recall_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Crystallizer dedup tracking
    last_dedup_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # 09/02 M-55. When ``status`` last changed. NULL on every row written
    # before this column existed, and on any row whose status has never been
    # touched — readers must COALESCE to ``created_at``.
    #
    # Exists because a contradiction is a status FLIP on an existing row, and
    # that event had no timestamp anywhere. ``outcome_contradiction_signals``
    # therefore windowed on ``created_at``, so a memory written weeks ago and
    # contradicted today fell outside the current scan window and its failure
    # evidence was dropped — silently, since "no rows" and "no contradictions"
    # are the same answer. The source extractor's docstring always described
    # windowing on the transition time and anticipated exactly this column.
    #
    # Distinct from supersession, which needs no such field: there the event IS
    # the creation of the superseding memory, so ``new_mem.created_at`` is
    # already the right timestamp.
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Contradiction tracking
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="SET NULL"),
    )

    # Unified contradiction model (A55) — see benchmark/A55-schema-design.md.
    # ``confidence``: confidence in this memory's CLAIM (extraction + assertion),
    # NULL = unknown/legacy. Guards invariant 5 — weak evidence must not delete
    # strong. ``is_inferred``: True when the system materialised this memory by
    # inference (not directly stated), so it never silently overrides an explicit
    # fact; lineage lives in ``memory_derivations``. ``scope``: structured validity
    # qualifiers (role/task/location); two memories conflict only if scopes overlap.
    confidence: Mapped[float | None] = mapped_column(Float)
    is_inferred: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    scope: Mapped[dict | None] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )

    __table_args__ = (
        Index("ix_memories_tenant_type", "tenant_id", "memory_type"),
        Index("ix_memories_tenant_agent", "tenant_id", "agent_id"),
        Index("ix_memories_content_hash", "tenant_id", "content_hash"),
        # Backs per-attempt bulk-write idempotency (CAURA-602). Created
        # ``CONCURRENTLY`` in migration 007 with the same predicates;
        # declared here so SQLAlchemy reflection / Alembic autogen
        # round-trips match the live schema. ``COALESCE(fleet_id, '')``
        # makes fleetless rows participate in the unique constraint —
        # PostgreSQL treats NULLs as distinct by default, so without
        # this two retries with ``fleet_id IS NULL`` would both insert.
        Index(
            "ix_memories_attempt_unique",
            "tenant_id",
            func.coalesce(text("fleet_id"), ""),
            "client_request_id",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL AND client_request_id IS NOT NULL"
            ),
        ),
        # Enforces the dedup contract the write path advertises: one LIVE row
        # per (tenant, fleet, agent, content_hash). Created ``CONCURRENTLY`` in
        # migration 040 with the same key and predicate; declared here so
        # reflection / autogen round-trip against the live schema, and so the
        # suites that build a schema from this metadata rather than from the
        # migration chain (``tests/conftest.py`` uses
        # ``Base.metadata.create_all``) exercise the constraint too.
        #
        # ``ix_memories_content_hash`` above is NOT a substitute: it is
        # non-unique and keyed on ``(tenant_id, content_hash)`` only — it makes
        # the lookup fast, it never made it correct.
        #
        # ``agent_id`` is in the key because two agents recording identical
        # content are two independent observations, which is the same scope
        # ``memory_find_by_content_hash`` dedups on. ``COALESCE(fleet_id, '')``
        # for the reason 007 needs it: PostgreSQL treats NULLs as distinct, so
        # without it fleetless rows would escape the constraint entirely.
        Index(
            "uq_memories_live_content_hash",
            "tenant_id",
            func.coalesce(text("fleet_id"), ""),
            "agent_id",
            "content_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND content_hash IS NOT NULL"),
        ),
        Index("ix_memories_valid_range", "ts_valid_start", "ts_valid_end"),
        Index("ix_memories_subject_entity", "subject_entity_id"),
        # For DELETEs, not reads — grep will find no query using it. This is the
        # referencing side of a SET NULL self-FK, which PostgreSQL enforces once
        # per deleted parent row. Partial because the RI check only ever looks
        # for a non-NULL match, and almost every row here is NULL (232 kB -> 16 kB).
        # Created CONCURRENTLY in migration 035, which carries the measurements;
        # declared here so reflection / autogen round-trip against the live schema.
        Index(
            "ix_memories_supersedes_id",
            "supersedes_id",
            postgresql_where=text("supersedes_id IS NOT NULL"),
        ),
        Index("ix_memories_recall_count", "recall_count"),
        Index("ix_memories_tenant_fleet", "tenant_id", "fleet_id"),
        # Backs the cursor-paginated list path (``list_by_filters`` +
        # the ``caura_list`` MCP tool) which orders by
        # ``(created_at DESC, id DESC)`` under ``tenant_id = ?`` and
        # ``deleted_at IS NULL``. Partial WHERE keeps the index small
        # since soft-deleted rows are never read on the hot path.
        # Bare-column references (``created_at.desc()`` not
        # ``text("created_at DESC")``) so Alembic autogen can reflect
        # and compare the index — matches ``analysis_report.py:38``.
        Index(
            "ix_memories_tenant_created_active",
            "tenant_id",
            created_at.desc(),
            id.desc(),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Backs the CAURA-656 purge-fanout discovery query
        # (``list_tenants_with_purgeable_memories``). Every other
        # partial index on this table is keyed on
        # ``deleted_at IS NULL`` (active path); without this
        # complement the soft-deleted-side discovery falls back to a
        # full scan.
        Index(
            "ix_memories_purgeable",
            "tenant_id",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
        # Backs the ``pending`` / ``settled`` block of ``GET /memories/stats``
        # (lme-0929-m-03). Partial on ``PENDING_WORK_SQL``, so it holds only
        # rows with background work outstanding: empty for a settled store,
        # and the count costs O(pending rows) instead of a tenant-wide scan.
        # Created CONCURRENTLY in migration 053 with the same predicate.
        Index(
            "ix_memories_pending_work",
            "tenant_id",
            postgresql_where=text(f"deleted_at IS NULL AND {PENDING_WORK_SQL}"),
        ),
    )


# Backs the derived-row lookup every memory delete runs (B25: M-52, M-53).
# Auto-chunk and atomic-fact children link to their parent only through
# ``metadata.parent_memory_id``; partial on live rows that have one, so it holds
# derived rows only. Created CONCURRENTLY in migration 058 with the same key and
# predicate. Declared after the class rather than in ``__table_args__`` because
# its key is a JSON operator on ``Memory.metadata_``, which has to exist first.
Index(
    "ix_memories_parent_memory_id",
    Memory.tenant_id,
    Memory.metadata_["parent_memory_id"].astext,
    postgresql_where=text(
        "deleted_at IS NULL AND (metadata ->> 'parent_memory_id') IS NOT NULL"
    ),
)


# Backs the ingest doc-hash lookup every preview runs (L-193): live ingest rows,
# keyed on the document's content hash. Created CONCURRENTLY in migration 059
# with the same key and predicate. Declared after the class rather than in
# ``__table_args__`` because its key is a JSON operator on ``Memory.metadata_``,
# which has to exist first.
Index(
    "ix_memories_ingest_doc_hash",
    Memory.tenant_id,
    Memory.metadata_["doc_hash"].astext,
    postgresql_where=text("deleted_at IS NULL AND (metadata ->> 'source') = 'ingest'"),
)


# Backs session rollback and a session's held writes (g2.9): the broker stamps
# each memory it writes with ``metadata.session_id``. Partial on live rows that
# have one. Created CONCURRENTLY in migration 062 with the same key and
# predicate; declared after the class for the reason the two above give.
Index(
    "ix_memories_session",
    Memory.tenant_id,
    Memory.metadata_["session_id"].astext,
    postgresql_where=text("deleted_at IS NULL AND (metadata ->> 'session_id') IS NOT NULL"),
)


# Backs the review queue of held memories and its count (g2.9). Held rows only,
# so the queue costs what it holds, not what the tenant holds. Created
# CONCURRENTLY in migration 062 with the same key and predicate.
Index(
    "ix_memories_held",
    Memory.tenant_id,
    Memory.created_at,
    Memory.id,
    postgresql_where=text("deleted_at IS NULL AND status = 'quarantined'"),
)
