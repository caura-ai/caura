"""Squashed initial schema — all OSS tables.

Revision ID: 001
Revises:
Create Date: 2026-03-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── memories ──
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text()),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768)),
        sa.Column("weight", sa.Float(), server_default=sa.text("0.5")),
        sa.Column("source_uri", sa.Text()),
        sa.Column("run_id", sa.Text()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("title", sa.Text()),
        sa.Column("content_hash", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("search_vector", sa.Text()),  # TSVECTOR — raw DDL below
        sa.Column(
            "subject_entity_id", sa.Uuid(), sa.ForeignKey("entities.id", ondelete="SET NULL", use_alter=True)
        ),
        sa.Column("predicate", sa.Text()),
        sa.Column("object_value", sa.Text()),
        sa.Column("ts_valid_start", sa.DateTime(timezone=True)),
        sa.Column("ts_valid_end", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("visibility", sa.Text(), server_default=sa.text("'scope_team'"), nullable=False),
        sa.Column("recall_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_recalled_at", sa.DateTime(timezone=True)),
        sa.Column("last_dedup_checked_at", sa.DateTime(timezone=True)),
        sa.Column("supersedes_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="SET NULL")),
    )
    op.create_index("ix_memories_tenant_id", "memories", ["tenant_id"])
    op.create_index("ix_memories_tenant_type", "memories", ["tenant_id", "memory_type"])
    op.create_index("ix_memories_tenant_agent", "memories", ["tenant_id", "agent_id"])
    op.create_index("ix_memories_content_hash", "memories", ["tenant_id", "content_hash"])
    op.create_index("ix_memories_status", "memories", ["status"])
    op.create_index("ix_memories_visibility", "memories", ["visibility"])
    op.create_index("ix_memories_valid_range", "memories", ["ts_valid_start", "ts_valid_end"])
    op.create_index("ix_memories_subject_entity", "memories", ["subject_entity_id"])
    op.create_index("ix_memories_recall_count", "memories", ["recall_count"])
    op.create_index("ix_memories_tenant_fleet", "memories", ["tenant_id", "fleet_id"])

    # Convert search_vector to tsvector, add GIN index + auto-update trigger
    op.execute("ALTER TABLE memories ALTER COLUMN search_vector TYPE tsvector USING search_vector::tsvector")
    op.execute("CREATE INDEX ix_memories_search_vector ON memories USING GIN (search_vector)")
    op.execute("""
        CREATE OR REPLACE FUNCTION memories_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector := to_tsvector('english', coalesce(NEW.content, ''));
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER memories_search_vector_trigger
        BEFORE INSERT OR UPDATE OF content ON memories
        FOR EACH ROW EXECUTE FUNCTION memories_search_vector_update();
    """)

    # HNSW index for vector similarity search
    op.execute("""
        CREATE INDEX ix_memories_embedding_hnsw ON memories
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # ── entities ──
    op.create_table(
        "entities",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text()),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("attributes", sa.JSON()),
        sa.Column("name_embedding", Vector(768)),
        sa.Column("search_vector", sa.Text()),  # TSVECTOR
    )
    op.create_index("ix_entities_tenant_id", "entities", ["tenant_id"])
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_entities_tenant_type_name_fleet
        ON entities (
            tenant_id,
            entity_type,
            lower(canonical_name),
            COALESCE(fleet_id, '')
        )
        """
    )

    # Convert entities search_vector to tsvector + GIN index
    op.execute("ALTER TABLE entities ALTER COLUMN search_vector TYPE tsvector USING search_vector::tsvector")
    op.execute("CREATE INDEX ix_entities_search_vector ON entities USING GIN (search_vector)")
    op.execute("""
        CREATE OR REPLACE FUNCTION entities_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector := to_tsvector('english', coalesce(NEW.canonical_name, ''));
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER entities_search_vector_trigger
        BEFORE INSERT OR UPDATE OF canonical_name ON entities
        FOR EACH ROW EXECUTE FUNCTION entities_search_vector_update();
    """)

    # ── memory_entity_links ──
    op.create_table(
        "memory_entity_links",
        sa.Column("memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False),
    )

    # ── relations ──
    op.create_table(
        "relations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text()),
        sa.Column(
            "from_entity_id", sa.Uuid(), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("relation_type", sa.Text(), nullable=False),
        sa.Column(
            "to_entity_id", sa.Uuid(), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("weight", sa.Float(), server_default=sa.text("1.0")),
        sa.Column("evidence_memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="SET NULL")),
    )
    op.create_index("ix_relations_tenant_id", "relations", ["tenant_id"])
    op.create_index("ix_relations_from", "relations", ["from_entity_id"])
    op.create_index("ix_relations_to", "relations", ["to_entity_id"])
    op.create_unique_constraint(
        "uq_relations_natural_key",
        "relations",
        ["tenant_id", "from_entity_id", "relation_type", "to_entity_id"],
    )

    # ── agents ──
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text()),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("trust_level", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("search_profile", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("uq_agents_tenant_agent", "agents", ["tenant_id", "agent_id"], unique=True)

    # ── audit_log ──
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Uuid()),
        sa.Column("detail", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])

    # ── analysis_reports ──
    op.create_table(
        "analysis_reports",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text()),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("summary", sa.JSON(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("hygiene", sa.JSON(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("health", sa.JSON(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("usage_data", sa.JSON(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("issues", sa.JSON(), server_default=sa.text("'[]'::jsonb")),
        sa.Column("crystallization", sa.JSON(), server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_analysis_reports_tenant_id", "analysis_reports", ["tenant_id"])
    op.create_index("ix_analysis_reports_status", "analysis_reports", ["status"])

    # ── fleet_nodes ──
    op.create_table(
        "fleet_nodes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text()),
        sa.Column("node_name", sa.Text(), nullable=False),
        sa.Column("hostname", sa.Text()),
        sa.Column("ip", sa.Text()),
        sa.Column("openclaw_version", sa.Text()),
        sa.Column("plugin_version", sa.Text()),
        sa.Column("plugin_hash", sa.Text()),
        sa.Column("os_info", sa.Text()),
        sa.Column("agents_json", sa.JSON()),
        sa.Column("tools_json", sa.JSON()),
        sa.Column("channels_json", sa.JSON()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "node_name", name="uq_fleet_nodes_tenant_node"),
    )
    op.create_index("ix_fleet_nodes_tenant_id", "fleet_nodes", ["tenant_id"])

    # ── fleet_commands ──
    op.create_table(
        "fleet_commands",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Uuid(), sa.ForeignKey("fleet_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("status", sa.Text(), server_default="pending"),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("acked_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_fleet_commands_tenant_id", "fleet_commands", ["tenant_id"])

    # ── documents ──
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text()),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "collection", "doc_id", name="uq_documents_tenant_collection_doc"),
    )
    op.create_index("ix_documents_tenant_collection", "documents", ["tenant_id", "collection"])

    # ── background_task_log ──
    op.create_table(
        "background_task_log",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("memory_id", sa.Uuid()),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'failed'")),
        sa.Column("error_message", sa.Text()),
        sa.Column("error_traceback", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_bg_task_log_tenant_status", "background_task_log", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_table("background_task_log")
    op.drop_table("documents")
    op.drop_table("fleet_commands")
    op.drop_table("fleet_nodes")
    op.drop_table("analysis_reports")
    op.drop_table("audit_log")
    op.drop_table("agents")
    op.drop_constraint("uq_relations_natural_key", "relations", type_="unique")
    op.drop_table("relations")
    op.drop_table("memory_entity_links")
    op.drop_index("uq_entities_tenant_type_name_fleet", table_name="entities")
    op.drop_table("entities")
    op.drop_table("memories")
    op.execute("DROP EXTENSION IF EXISTS vector")
