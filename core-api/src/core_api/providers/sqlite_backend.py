"""SQLite storage backend for MemClaw.

A lightweight, file-based implementation of the ``StorageBackend`` protocol
suitable for local development, on-premise single-node deployments, and
testing.  Embeddings are stored as packed float32 BLOBs, and cosine
similarity is computed in Python after filtering candidates via SQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import struct
import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from core_api.protocols import SearchFilters

logger = logging.getLogger(__name__)

# Columns that callers may update via the ``update()`` method.
_UPDATABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "content",
        "embedding",
        "weight",
        "title",
        "content_hash",
        "source_uri",
        "run_id",
        "metadata",
        "status",
        "visibility",
        "memory_type",
        "agent_id",
        "fleet_id",
        "deleted_at",
        "ts_valid_start",
        "ts_valid_end",
        "recall_count",
        "last_recalled_at",
        "subject_entity_id",
        "predicate",
        "object_value",
        "supersedes_id",
        "last_dedup_checked_at",
    }
)

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    fleet_id TEXT,
    agent_id TEXT,
    memory_type TEXT DEFAULT 'fact',
    content TEXT NOT NULL,
    embedding BLOB,
    weight REAL DEFAULT 0.5,
    title TEXT,
    content_hash TEXT,
    source_uri TEXT,
    run_id TEXT,
    metadata TEXT,
    status TEXT DEFAULT 'active',
    visibility TEXT DEFAULT 'scope_team',
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    ts_valid_start TEXT,
    ts_valid_end TEXT,
    recall_count INTEGER DEFAULT 0,
    last_recalled_at TEXT,
    subject_entity_id TEXT,
    predicate TEXT,
    object_value TEXT,
    supersedes_id TEXT,
    last_dedup_checked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_tenant
    ON memories (tenant_id);
CREATE INDEX IF NOT EXISTS idx_memories_tenant_status
    ON memories (tenant_id, status, deleted_at);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    fleet_id TEXT,
    entity_type TEXT,
    canonical_name TEXT,
    attributes TEXT,
    name_embedding BLOB
);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    from_entity_id TEXT,
    relation_type TEXT,
    to_entity_id TEXT,
    weight REAL DEFAULT 1.0,
    evidence_memory_id TEXT
);

CREATE TABLE IF NOT EXISTS memory_entity_links (
    memory_id TEXT,
    entity_id TEXT,
    role TEXT,
    PRIMARY KEY (memory_id, entity_id)
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pack_embedding(emb: list[float]) -> bytes:
    """Pack a float list into a compact binary BLOB (float32)."""
    return struct.pack(f"{len(emb)}f", *emb)


def _unpack_embedding(blob: bytes) -> list[float]:
    """Unpack a binary BLOB back into a list of floats."""
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity between two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _row_to_dict(
    row: aiosqlite.Row,
    *,
    decode_embedding: bool = False,
) -> dict[str, Any]:
    """Convert an ``aiosqlite.Row`` to a plain dict.

    Optionally unpacks the ``embedding`` BLOB and deserialises the
    ``metadata`` JSON string.
    """
    d = dict(row)
    # Deserialise metadata JSON
    if d.get("metadata") is not None:
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            pass
    # Optionally unpack embedding
    if decode_embedding and d.get("embedding") is not None:
        d["embedding"] = _unpack_embedding(d["embedding"])
    elif not decode_embedding:
        # Drop raw blob from output when not decoding
        d.pop("embedding", None)
    return d


# ---------------------------------------------------------------------------
# SqliteBackend
# ---------------------------------------------------------------------------


class SqliteBackend:
    """File-based storage backend using SQLite (via ``aiosqlite``).

    Satisfies the ``StorageBackend`` protocol through structural subtyping.
    """

    def __init__(self, db_path: str = "~/.memclaw/memclaw.db") -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            self._db_path = os.path.expanduser(db_path)
            parent = os.path.dirname(self._db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._db: aiosqlite.Connection | None = None
        self._init_lock: asyncio.Lock | None = None

    # -- connection management ----------------------------------------------

    async def _get_db(self) -> aiosqlite.Connection:
        """Return the lazily-initialised database connection.

        Uses a double-checked lock so only the first caller pays the
        initialisation cost; concurrent callers wait on the lock.
        """
        if self._db is not None:
            return self._db
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._db is None:
                conn = await aiosqlite.connect(self._db_path)
                try:
                    conn.row_factory = aiosqlite.Row
                    await conn.execute("PRAGMA journal_mode=WAL")
                    await self._ensure_schema(conn)
                except Exception:
                    await conn.close()
                    raise
                self._db = conn
                logger.info("SQLite backend initialised: %s", self._db_path)
        return self._db

    @staticmethod
    async def _ensure_schema(db: aiosqlite.Connection) -> None:
        """Create tables if they do not already exist."""
        await db.executescript(_SCHEMA_SQL)
        await db.commit()

    # -- StorageBackend protocol --------------------------------------------

    async def store(
        self,
        tenant_id: str,
        content: str,
        embedding: list[float] | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Insert a new memory and return its UUID string."""
        db = await self._get_db()
        memory_id = str(uuid.uuid4())
        created_at = datetime.now(UTC).isoformat()
        emb_blob = _pack_embedding(embedding) if embedding else None
        meta_json = json.dumps(metadata) if metadata else None

        await db.execute(
            """
            INSERT INTO memories (id, tenant_id, content, embedding, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, tenant_id, content, emb_blob, meta_json, created_at),
        )
        await db.commit()
        return memory_id

    async def get(
        self,
        tenant_id: str,
        memory_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve a single memory by ID (tenant-scoped, excludes deleted)."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM memories WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
            (memory_id, tenant_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_dict(row, decode_embedding=True)

    async def update(
        self,
        tenant_id: str,
        memory_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Update only the specified fields on a memory."""
        if not fields:
            return
        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"Unknown or non-updatable fields: {unknown}")
        db = await self._get_db()

        # Pre-process special fields
        processed = dict(fields)
        if "embedding" in processed and processed["embedding"] is not None:
            processed["embedding"] = _pack_embedding(processed["embedding"])
        if "metadata" in processed and processed["metadata"] is not None:
            processed["metadata"] = json.dumps(processed["metadata"])

        set_clause = ", ".join(f"{k} = ?" for k in processed)
        values = list(processed.values()) + [memory_id, tenant_id]
        await db.execute(
            f"UPDATE memories SET {set_clause} WHERE id = ? AND tenant_id = ?",
            values,
        )
        await db.commit()

    async def delete(
        self,
        tenant_id: str,
        memory_id: str,
    ) -> bool:
        """Soft-delete a memory by setting ``deleted_at``."""
        db = await self._get_db()
        now = datetime.now(UTC).isoformat()
        cursor = await db.execute(
            "UPDATE memories SET deleted_at = ? WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
            (now, memory_id, tenant_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def search(
        self,
        query_embedding: list[float],
        query_text: str,
        filters: SearchFilters,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Hybrid vector + keyword search with Python-side cosine ranking."""
        db = await self._get_db()

        # Build WHERE clause
        conditions = ["tenant_id = ?", "deleted_at IS NULL"]
        params: list[Any] = [filters.tenant_id]

        if filters.fleet_ids:
            placeholders = ", ".join("?" for _ in filters.fleet_ids)
            conditions.append(f"fleet_id IN ({placeholders})")
            params.extend(filters.fleet_ids)

        if filters.agent_id is not None:
            conditions.append("agent_id = ?")
            params.append(filters.agent_id)

        if filters.memory_type is not None:
            conditions.append("memory_type = ?")
            params.append(filters.memory_type)

        if filters.status is not None:
            conditions.append("status = ?")
            params.append(filters.status)

        # Text keyword filter (case-insensitive, OR across words)
        query_words = query_text.strip().split() if query_text else []
        if query_words:
            or_clauses = " OR ".join("LOWER(content) LIKE ?" for _ in query_words)
            conditions.append(f"({or_clauses})")
            params.extend(f"%{w.lower()}%" for w in query_words)

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM memories WHERE {where} LIMIT ?"
        params.append(limit * 20)

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

        # Score and rank
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            d = _row_to_dict(row, decode_embedding=True)

            # Cosine similarity component
            cosine_sim = 0.0
            if query_embedding and d.get("embedding"):
                cosine_sim = _cosine_similarity(query_embedding, d["embedding"])

            # Text match ratio component
            text_ratio = 0.0
            if query_words:
                content_lower = (d.get("content") or "").lower()
                matched = sum(1 for w in query_words if w.lower() in content_lower)
                text_ratio = matched / len(query_words)

            similarity = 0.7 * cosine_sim + 0.3 * text_ratio
            d["similarity"] = similarity
            # Drop raw embedding from search results
            d.pop("embedding", None)
            scored.append((similarity, d))

        # Sort descending by score, take top `limit`
        scored.sort(key=lambda t: t[0], reverse=True)
        return [d for _, d in scored[:limit]]

    async def graph_traverse(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        hops: int = 1,
    ) -> list[dict[str, Any]]:
        """BFS through the relations table, returning connected memories."""
        db = await self._get_db()

        # BFS to collect entity IDs within `hops` edges
        visited: set[str] = {entity_id}
        frontier: set[str] = {entity_id}

        for _ in range(hops):
            if not frontier:
                break
            placeholders = ", ".join("?" for _ in frontier)
            cursor = await db.execute(
                f"""
                SELECT from_entity_id, to_entity_id FROM relations
                WHERE tenant_id = ?
                  AND (from_entity_id IN ({placeholders}) OR to_entity_id IN ({placeholders}))
                """,
                [tenant_id, *frontier, *frontier],
            )
            rows = await cursor.fetchall()
            next_frontier: set[str] = set()
            for r in rows:
                for eid in (r["from_entity_id"], r["to_entity_id"]):
                    if eid not in visited:
                        visited.add(eid)
                        next_frontier.add(eid)
            frontier = next_frontier

        if not visited:
            return []

        # Find memories linked to any visited entity
        placeholders = ", ".join("?" for _ in visited)
        cursor = await db.execute(
            f"""
            SELECT m.* FROM memories m
            JOIN memory_entity_links mel ON m.id = mel.memory_id
            WHERE mel.entity_id IN ({placeholders})
              AND m.tenant_id = ?
              AND m.deleted_at IS NULL
            """,
            [*visited, tenant_id],
        )
        rows = await cursor.fetchall()
        return [_row_to_dict(row, decode_embedding=False) for row in rows]
