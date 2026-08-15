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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.constants import VECTOR_DIM
from common.models.base import Base


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
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
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
    )
