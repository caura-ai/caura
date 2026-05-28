"""PostgreSQL service -- all database queries for core tables.

Single point of DB access for the OSS core-storage-api.  Every query that
was previously spread across eight repository classes now lives here, grouped
by domain: memories, entities, agents, documents, fleet, audit, reports, tasks.

Session management uses a module-level ``async_sessionmaker`` backed by the
shared engine from ``core_storage_api.database.init.get_engine``.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, delete, false, func, literal_column, or_, select, text, tuple_
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only

from common.constants import (
    CONTRADICTION_CANDIDATE_MAX,
    CONTRADICTION_SIMILARITY_THRESHOLD,
    DEFAULT_RELATION_TYPE_WEIGHT,
    ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    GRAPH_MAX_HOPS,
    RECALL_BOOST_SCALE,
    RELATION_TYPE_WEIGHTS,
    SEMANTIC_DEDUP_CANDIDATE_LIMIT,
    SEMANTIC_DEDUP_THRESHOLD,
    TYPE_DECAY_DAYS,
)
from common.events.lifecycle_purge_request import MEMORY_RETENTION_MAX_DAYS
from common.models import (
    Agent,
    AuditLog,
    BackgroundTaskLog,
    CrystallizationReport,
    DedupReview,
    Document,
    Entity,
    FleetCommand,
    FleetNode,
    IdempotencyResponse,
    LifecycleAudit,
    Memory,
    MemoryEntityLink,
    Relation,
)
from core_storage_api.observability import db_measure

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session factories (singletons)
# ---------------------------------------------------------------------------
#
# Two factories bound to two engines: writer (primary) and reader
# (replica when ``read_database_url`` is set, else the primary). The
# factories are lazy so tests can swap the underlying engines before
# the first request without touching service-module state.

_session_factory: async_sessionmaker[AsyncSession] | None = None
_read_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        from core_storage_api.database.init import get_engine

        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


def _get_read_session_factory() -> async_sessionmaker[AsyncSession]:
    global _read_session_factory
    if _read_session_factory is None:
        from core_storage_api.database.init import get_read_engine

        _read_session_factory = async_sessionmaker(
            get_read_engine(),
            expire_on_commit=False,
        )
    return _read_session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Transactional writer session; commits on success, rolls back on error."""
    factory = _get_session_factory()
    async with factory() as session:
        async with session.begin():
            yield session


@asynccontextmanager
async def get_read_session() -> AsyncIterator[AsyncSession]:
    """Reader session — no explicit transaction wrapper since these are
    query-only paths. Routes to the replica engine when
    ``settings.read_database_url`` is set; otherwise shares the primary
    pool (OSS standalone — unchanged behavior).

    Do NOT use for read-your-writes flows inside a single request; use
    :func:`get_session` for those so the read sees the same transaction
    scope as the write.
    """
    factory = _get_read_session_factory()
    async with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope_sql(
    tenant_id: str,
    fleet_id: str | None,
    table: str = "m",
) -> tuple[str, dict]:
    """Build a WHERE clause fragment for tenant + optional fleet scoping."""
    clause = f"{table}.tenant_id = :tenant_id"
    params: dict = {"tenant_id": tenant_id}
    if fleet_id is not None:
        clause += f" AND {table}.fleet_id = :fleet_id"
        params["fleet_id"] = fleet_id
    return clause, params


def _relation_weight(relation_type: str, row_weight: float) -> float:
    """Compute effective weight for a relation edge."""
    type_w = RELATION_TYPE_WEIGHTS.get(
        relation_type.lower(),
        DEFAULT_RELATION_TYPE_WEIGHT,
    )
    return type_w * row_weight


# Cached at import time so the per-row column-name filter on the bulk
# write hot path (100 items x ~25 columns) doesn't pay the cost of
# walking ``Memory.__table__.columns`` and ``__mapper__.column_attrs``
# on every call. Both sets are static for the lifetime of the process.
_MEMORY_VALID_FIELDS = frozenset(
    {c.key for c in Memory.__table__.columns} | {a.key for a in Memory.__mapper__.column_attrs}
)


# ── Org hard-delete purge (CAURA-689) ──
#
# Tables wiped when an organization is permanently deleted. Ordered
# children-before-parents so the per-table DELETEs never trip a foreign
# key. Tables WITHOUT their own ``tenant_id`` column (``memory_entity_links``)
# are not listed — they're removed by the ON DELETE CASCADE from
# ``memories`` / ``entities``. ``relations`` and ``fleet_commands`` are
# listed explicitly (ahead of their parents) so their per-table counts are
# reported rather than hidden inside a cascade.
_PURGE_TENANT_TABLES: tuple[str, ...] = (
    "relations",
    "fleet_commands",
    "memories",
    "entities",
    "agents",
    "fleet_nodes",
    "audit_log",
    "documents",
    "analysis_reports",
    "dedup_reviews",
    "background_task_log",
    "idempotency_responses",
)
# OSS keys these by ``org_id``, which equals the tenant id in the
# single-key-per-tenant OSS model (CAURA-654). ``lifecycle_audit`` is
# deliberately NOT purged: it's the operational audit trail (including
# the hard-delete-org row itself), so wiping it would erase the record
# of the very deletion being performed.
_PURGE_ORG_KEYED_TABLES: tuple[str, ...] = (
    "organization_settings",
    "organization_settings_audit",
)


# ═══════════════════════════════════════════════════════════════════════════
# PostgresService
# ═══════════════════════════════════════════════════════════════════════════


class PostgresService:
    """Single point of DB access for all core tables.

    Every public method acquires its own session via ``get_session()``.
    Callers never need to manage sessions or transactions.
    """

    # ══════════════════════════════════════════════════════════════════════
    #  MEMORIES
    # ══════════════════════════════════════════════════════════════════════

    # ------------------------------------------------------------------
    # A) Core CRUD
    # ------------------------------------------------------------------

    async def memory_get_by_id(self, memory_id: UUID) -> Memory | None:
        # Wrap the full session block so db_ms includes connection-pool
        # wait time — a saturated pool shows up as slow "DB" here, which
        # is exactly how we want to see it in Cloud Logging.
        with db_measure():
            async with get_read_session() as session:
                return await session.get(Memory, memory_id)

    async def memory_get_by_id_for_tenant(
        self,
        memory_id: UUID,
        tenant_id: str,
    ) -> Memory | None:
        with db_measure():
            async with get_read_session() as session:
                memory = await session.get(Memory, memory_id)
                if memory is None or memory.tenant_id != tenant_id or memory.deleted_at is not None:
                    return None
                return memory

    @staticmethod
    def _filter_fields(model_cls, data: dict) -> dict:
        valid = {c.key for c in model_cls.__table__.columns}
        return {k: v for k, v in data.items() if k in valid}

    @staticmethod
    def _filter_memory_fields(data: dict) -> dict:
        return {k: v for k, v in data.items() if k in _MEMORY_VALID_FIELDS}

    async def memory_add(self, data: dict) -> Memory:
        async with get_session() as session:
            memory = Memory(**self._filter_memory_fields(data))
            session.add(memory)
            await session.flush()
            return memory

    async def memory_add_all(self, items: list[dict]) -> list[dict]:
        """Insert with per-attempt idempotency (CAURA-602).

        Every item must carry a non-empty ``client_request_id``. Callers
        on the bulk-write path derive it from the
        ``X-Bulk-Attempt-Id`` header (``f"{attempt_id}:{index}"``);
        server-internal callers (auto-chunk, atomic-facts) generate a
        UUID per item. Partial unique
        ``ix_memories_attempt_unique`` makes a retry of the same logical
        attempt deterministic: rows already committed by a prior call
        are detected via ``ON CONFLICT DO NOTHING`` and returned with
        ``was_inserted=False`` and the canonical row id, instead of
        being silently re-inserted or vanishing because the response
        was lost mid-flight.

        Returns one entry per input item, **in input order**:

            ``{client_request_id, id, was_inserted}``

        ``id`` is ``None`` only in the pathological case where the
        item was neither inserted nor found on a follow-up read — e.g.
        a concurrent soft-delete between INSERT and SELECT, or the
        unresolved row drifted out of scope. The caller surfaces those
        as per-item errors.
        """
        if not items:
            return []

        for d in items:
            if not d.get("client_request_id"):
                # Required at this layer so the partial-unique guarantee
                # holds for every row. Routing the rejection here, rather
                # than at the FastAPI route, also catches in-process
                # callers (auto-chunk via ``sc.create_memories``) that
                # forgot to mint an id.
                raise ValueError("memory_add_all: every item must carry client_request_id")

        # All callers send a single-tenant, single-fleet batch (the
        # bulk endpoint is tenant-and-fleet-scoped on the way in). Pin
        # the post-conflict re-query to BOTH dimensions so an attacker
        # who learned a foreign ``client_request_id`` can't read
        # across tenants OR fleets by sneaking it into an items list,
        # and so the re-query window matches the unique index's scope
        # ``(tenant_id, COALESCE(fleet_id, ''), client_request_id)``
        # exactly. Validating up-front keeps the invariant explicit
        # instead of relying on the upstream schema.
        tenant_id = items[0]["tenant_id"]
        fleet_id = items[0].get("fleet_id")
        if any(d.get("tenant_id") != tenant_id for d in items):
            raise ValueError("memory_add_all: all items must share the same tenant_id")
        if any(d.get("fleet_id") != fleet_id for d in items):
            raise ValueError("memory_add_all: all items must share the same fleet_id")

        async with get_session() as session:
            rows = [self._filter_memory_fields(d) for d in items]
            # The conflict target must mirror ``ix_memories_attempt_unique``
            # *expression-for-expression* — the planner only treats the
            # ON CONFLICT and the partial-unique index as matched if every
            # element is byte-equivalent, including the ``COALESCE`` over
            # nullable ``fleet_id``. Stripping the COALESCE here would
            # silently fall back to "no inferred constraint" and double-
            # insert on retry for fleetless attempts.
            stmt = (
                pg_insert(Memory)
                .values(rows)
                .on_conflict_do_nothing(
                    # ``text("COALESCE(fleet_id, '')")`` matches migration
                    # 007's CREATE INDEX SQL character-for-character.
                    # ``func.coalesce(Memory.fleet_id, "")`` works today
                    # via Postgres conflict-inference normalisation
                    # (which strips table qualifiers), but pinning the
                    # raw text removes the dependency on that
                    # normalisation behaviour across SQLAlchemy + asyncpg
                    # versions. A future renderer change that emits
                    # ``coalesce(memories.fleet_id, '')`` *would* still
                    # match today, but if the planner ever returns "no
                    # unique constraint matches the ON CONFLICT
                    # specification" the silent-create class re-emerges
                    # as a 500-on-every-retry. The model-level Index in
                    # ``common/models/memory.py`` uses the same text()
                    # form for the same reason.
                    index_elements=[
                        Memory.tenant_id,
                        text("COALESCE(fleet_id, '')"),
                        Memory.client_request_id,
                    ],
                    index_where=text("deleted_at IS NULL AND client_request_id IS NOT NULL"),
                )
                .returning(Memory.id, Memory.client_request_id)
            )
            inserted: dict[str, UUID] = {
                row.client_request_id: row.id for row in (await session.execute(stmt)).all()
            }

            # Items the conflict swallowed already exist — committed by a
            # prior attempt with the same ``X-Bulk-Attempt-Id``. Re-read
            # their canonical ids in the same session so the caller can
            # surface ``duplicate_attempt`` instead of dropping them.
            unresolved = [d["client_request_id"] for d in items if d["client_request_id"] not in inserted]
            existing: dict[str, UUID] = {}
            if unresolved:
                # Chunk the IN-list to keep the parameter count well below
                # asyncpg's 32k bind-arg ceiling. 500 mirrors the bulk
                # batch ceiling so a single-batch retry is one query;
                # larger calls (auto-chunk) split cleanly.
                fleet_predicate = (
                    Memory.fleet_id == fleet_id if fleet_id is not None else Memory.fleet_id.is_(None)
                )
                for chunk_start in range(0, len(unresolved), 500):
                    chunk = unresolved[chunk_start : chunk_start + 500]
                    result = await session.execute(
                        select(Memory.id, Memory.client_request_id).where(
                            Memory.tenant_id == tenant_id,
                            fleet_predicate,
                            Memory.client_request_id.in_(chunk),
                            Memory.deleted_at.is_(None),
                        )
                    )
                    for row in result.all():
                        existing[row.client_request_id] = row.id

        out: list[dict] = []
        for d in items:
            crid = d["client_request_id"]
            if crid in inserted:
                out.append({"client_request_id": crid, "id": str(inserted[crid]), "was_inserted": True})
            elif crid in existing:
                out.append({"client_request_id": crid, "id": str(existing[crid]), "was_inserted": False})
            else:
                # Soft-delete or schema-skew edge case: the row was neither
                # newly inserted nor visible on the follow-up read. ``id``
                # is None so the core-api layer surfaces this as a
                # per-item error rather than fabricating an id.
                out.append({"client_request_id": crid, "id": None, "was_inserted": False})
        return out

    async def memory_soft_delete(self, memory_id: UUID) -> None:
        async with get_session() as session:
            memory = await session.get(Memory, memory_id)
            if memory is not None:
                memory.deleted_at = datetime.now(UTC)
                memory.status = "deleted"

    async def memory_update(self, memory_id: UUID, patch: dict) -> bool:
        """Apply arbitrary field updates to a memory.

        Two patch shapes are supported in the same request:

        * Plain ORM column keys (``memory_type``, ``weight``, ``status``,
          ``ts_valid_*``, …) — applied as a single ``UPDATE ... SET``.
        * The synthetic key ``metadata_patch`` — a dict that is merged
          into the existing ``metadata`` JSONB column atomically with
          ``COALESCE(metadata, '{}'::jsonb) || :patch``. Used by the
          async-enrich worker (CAURA-595) to add ``summary`` / ``tags`` /
          ``contains_pii`` / ``pii_types`` / ``retrieval_hint`` /
          ``llm_ms`` without clobbering keys an earlier write set.

        Other top-level keys whose names don't match a ``Memory`` column
        are silently dropped — callers validate upstream.

        Returns ``True`` when the row exists and is live (the patch was
        applied or was a no-op due to all-unknown keys); ``False`` when
        the row is absent or soft-deleted, which the route turns into
        404. Both UPDATE branches run inside a single
        ``SELECT ... FOR UPDATE`` snapshot so a concurrent
        ``memory_soft_delete`` can't commit between them and leave the
        row in a torn state (status updated, metadata not, or vice
        versa) — the bare per-statement ``deleted_at IS NULL`` guard
        wasn't enough on its own under READ COMMITTED.
        """
        metadata_patch = patch.get("metadata_patch") if isinstance(patch, dict) else None
        # Map JSON keys to model columns. ``metadata_patch`` is handled
        # via a separate JSONB-merge statement below; skip it here so it
        # doesn't accidentally land as a column write.
        values: dict = {
            key: val for key, val in patch.items() if key != "metadata_patch" and hasattr(Memory, key)
        }
        async with get_session() as session:
            # Existence check FIRST — runs even on empty / all-unknown-
            # keys patches so a PATCH on an absent or soft-deleted row
            # consistently returns 404 regardless of body shape. Pre-
            # this-change the no-op paths (empty body, all-unknown
            # keys) short-circuited with ``return True`` and the route
            # answered 200, so a deleted row could absorb a "successful"
            # no-op PATCH that depended only on whether the body
            # carried recognised columns.
            #
            # ``SELECT ... FOR UPDATE`` locks the row so the two UPDATE
            # branches below run inside one snapshot: a concurrent soft
            # DELETE blocks until this transaction commits or rolls
            # back, eliminating the column-set-but-not-metadata torn
            # state under READ COMMITTED.
            #
            # ``id, deleted_at`` come back as a tuple so "row absent"
            # (None tuple) and "row exists, deleted_at IS NULL" (live)
            # are distinguishable — ``scalar_one_or_none`` on
            # ``deleted_at`` alone would collapse both into None.
            row = (
                await session.execute(
                    select(Memory.id, Memory.deleted_at).where(Memory.id == memory_id).with_for_update()
                )
            ).first()
            if row is None:
                return False  # row truly absent — caller → 404
            if row.deleted_at is not None:
                return False  # soft-deleted — caller → 404, no UPDATE runs

            # No-op patches on a live row are valid: existence check
            # already passed, so report success without burning UPDATEs.
            # Worth the SELECT roundtrip cost (rate-limited PATCH route
            # bounds it) for the consistent 404-on-absent contract.
            if not patch or (not values and not metadata_patch):
                return True

            # ``deleted_at IS NULL`` predicate stays on each UPDATE
            # even though the FOR UPDATE lock above already gates this
            # path; it's belt-and-suspenders against a future change
            # that splits the lock and the UPDATEs into separate
            # sessions.
            if values:
                await session.execute(
                    sql_update(Memory)
                    .where(Memory.id == memory_id, Memory.deleted_at.is_(None))
                    .values(**values)
                )
            if metadata_patch:
                # Single-statement JSONB merge — concurrent merges are
                # last-writer-wins per key but never corrupt the doc.
                # ``::jsonb`` cast on the bind keeps the parameter typed
                # so empty dicts merge cleanly instead of failing the
                # ``||`` operator.
                #
                # ``metadata::jsonb`` cast on the column handles the
                # CAURA-595 production drift case: the ORM declares the
                # column as ``JSONB`` (common/models/memory.py) but
                # legacy Postgres tables created before the JSONB
                # migration store it as ``json`` (lowercase). Without
                # the explicit cast, ``COALESCE(metadata, '{}'::jsonb)``
                # raises ``CannotCoerceError: COALESCE could not
                # convert type jsonb to json`` on those installations.
                # The cast is a no-op when the column is already
                # ``jsonb`` and a one-time conversion when it isn't —
                # cheap either way relative to the network round-trip.
                #
                # ``deleted_at IS NULL`` guard mirrors the column-set
                # branch above so a PATCH never resurrects a deleted
                # row via the metadata-merge path either.
                await session.execute(
                    text(
                        "UPDATE memories "
                        "SET metadata = COALESCE(metadata::jsonb, '{}'::jsonb) || (:patch)::jsonb "
                        "WHERE id = :id AND deleted_at IS NULL"
                    ).bindparams(patch=json.dumps(metadata_patch), id=memory_id),
                )
        return True

    async def memory_update_status(
        self,
        memory_id: UUID,
        status: str,
        *,
        supersedes_id: UUID | None = None,
        unset_supersedes: bool = False,
        expected_supersedes_id: UUID | None = None,
    ) -> bool:
        """Update a memory's ``status`` and optionally (re)set ``supersedes_id``.

        Args:
            memory_id: Target row.
            status: New status value.
            supersedes_id: If provided, set ``supersedes_id`` to this UUID.
                Ignored when ``unset_supersedes`` is True.
            unset_supersedes: If True, clear ``supersedes_id`` to NULL.
                Takes precedence over ``supersedes_id``.
            expected_supersedes_id: Optional CAS gate — only update if the
                row's current ``supersedes_id`` matches this value. Used by
                the contradiction-retraction path so a concurrent writer
                that already cleared / changed the pointer doesn't get
                clobbered.

        Returns:
            True if the row was updated, False if the ``expected_supersedes_id``
            CAS check failed (or the row id doesn't exist). Existing callers
            ignore the return value — adding it is backward-compatible.
        """
        values: dict[str, Any] = {"status": status}
        if unset_supersedes:
            values["supersedes_id"] = None
        elif supersedes_id is not None:
            values["supersedes_id"] = supersedes_id

        async with get_session() as session:
            stmt = sql_update(Memory).where(Memory.id == memory_id)
            if expected_supersedes_id is not None:
                stmt = stmt.where(Memory.supersedes_id == expected_supersedes_id)
            stmt = stmt.values(**values)
            result = await session.execute(stmt)
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def memory_update_embedding(
        self,
        memory_id: UUID,
        embedding: list[float],
        metadata: dict | None = None,
    ) -> None:
        async with get_session() as session:
            values: dict = {"embedding": embedding}
            if metadata is not None:
                values["metadata_"] = metadata
            await session.execute(sql_update(Memory).where(Memory.id == memory_id).values(**values))

    # ------------------------------------------------------------------
    # B) Content hash / dedup
    # ------------------------------------------------------------------

    async def memory_find_by_content_hash(
        self,
        tenant_id: str,
        content_hash: str,
        fleet_id: str | None = None,
        agent_id: str | None = None,
    ) -> Memory | None:
        # ``agent_id`` scopes the dedup match: two different agents writing
        # identical content in the same fleet should both succeed (they are
        # independent observations). Omitted → legacy tenant+fleet+content
        # scope, which silently collides cross-agent.
        async with get_session() as session:
            stmt = select(Memory).where(
                Memory.tenant_id == tenant_id,
                Memory.content_hash == content_hash,
                Memory.deleted_at.is_(None),
            )
            if fleet_id:
                stmt = stmt.where(Memory.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Memory.fleet_id.is_(None))
            if agent_id is not None:
                stmt = stmt.where(Memory.agent_id == agent_id)
            return (await session.execute(stmt)).scalar_one_or_none()

    async def memory_find_duplicate_hash(
        self,
        tenant_id: str,
        content_hash: str,
        fleet_id: str | None = None,
        exclude_id: UUID | None = None,
        agent_id: str | None = None,
    ) -> UUID | None:
        async with get_session() as session:
            stmt = select(Memory.id).where(
                Memory.tenant_id == tenant_id,
                Memory.content_hash == content_hash,
                Memory.deleted_at.is_(None),
            )
            if fleet_id:
                stmt = stmt.where(Memory.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Memory.fleet_id.is_(None))
            if agent_id is not None:
                stmt = stmt.where(Memory.agent_id == agent_id)
            if exclude_id is not None:
                stmt = stmt.where(Memory.id != exclude_id)
            return (await session.execute(stmt)).scalar_one_or_none()

    async def memory_find_embedding_by_content_hash(
        self,
        tenant_id: str,
        content_hash: str,
    ) -> list[float] | None:
        async with get_session() as session:
            stmt = (
                select(Memory.embedding)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.content_hash == content_hash,
                    Memory.embedding.isnot(None),
                    Memory.deleted_at.is_(None),
                )
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def memory_find_semantic_duplicate(
        self,
        tenant_id: str,
        fleet_id: str | None,
        embedding: list[float],
        exclude_id: UUID | None = None,
        visibility: str | None = None,
        min_similarity: float | None = None,
    ) -> tuple[Memory, float] | None:
        """Find the closest memory above ``min_similarity``.

        ``min_similarity`` (A1 #16): cosine-similarity cutoff applied
        in SQL. Defaults to ``SEMANTIC_DEDUP_THRESHOLD`` (0.95) for
        back-compat with single-tier callers; A1 #16's tier-dispatching
        pipeline step passes ``SEMANTIC_DEDUP_JUDGE_THRESHOLD`` (0.85)
        so candidates in the judge band become visible.

        Returns ``(memory, similarity)`` or ``None``. The similarity
        field is what callers use to decide auto-reject vs judge-dispatch
        vs accept (see ``check_semantic_duplicate.py``).
        """
        threshold = min_similarity if min_similarity is not None else SEMANTIC_DEDUP_THRESHOLD
        async with get_session() as session:
            distance = Memory.embedding.cosine_distance(embedding)
            similarity = (1.0 - distance).label("similarity")

            stmt = (
                select(Memory, similarity)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.deleted_at.is_(None),
                    Memory.status.in_(("active", "confirmed", "pending")),
                    Memory.embedding.is_not(None),
                )
                .where((1.0 - distance) >= threshold)
                .order_by(distance)
                .limit(SEMANTIC_DEDUP_CANDIDATE_LIMIT)
            )

            if fleet_id:
                stmt = stmt.where(Memory.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Memory.fleet_id.is_(None))
            if visibility:
                stmt = stmt.where(Memory.visibility == visibility)
            if exclude_id is not None:
                stmt = stmt.where(Memory.id != exclude_id)

            result = await session.execute(stmt)
            row = result.first()
            if row is None:
                return None
            return row.Memory, float(row.similarity)

    # ------------------------------------------------------------------
    # A1 #18 — Dedup review queue
    # ------------------------------------------------------------------

    async def dedup_review_enqueue(self, payload: dict) -> DedupReview:
        """Insert a new ``dedup_reviews`` row in ``pending`` status.

        Caller-supplied fields:
          - tenant_id, fleet_id, agent_id (scoping)
          - new_memory_id (may be NULL — rejected writes never persist)
          - candidate_memory_id (the matched memory)
          - new_content, candidate_content (snapshots — preserved even
            if either memory is later deleted)
          - similarity, judge_verdict, judge_confidence
          - decision_band (one of ``DEDUP_REVIEW_BANDS``)
        """
        from common.models.dedup_review import DEDUP_REVIEW_BANDS

        band = payload.get("decision_band")
        if band not in DEDUP_REVIEW_BANDS:
            raise ValueError(f"unknown decision_band: {band!r}")

        async with get_session() as session:
            row = DedupReview(
                tenant_id=payload["tenant_id"],
                fleet_id=payload.get("fleet_id"),
                agent_id=payload["agent_id"],
                new_memory_id=UUID(payload["new_memory_id"]) if payload.get("new_memory_id") else None,
                candidate_memory_id=UUID(payload["candidate_memory_id"]),
                new_content=payload["new_content"],
                candidate_content=payload["candidate_content"],
                similarity=float(payload["similarity"]),
                judge_verdict=payload.get("judge_verdict"),
                judge_confidence=(
                    float(payload["judge_confidence"])
                    if payload.get("judge_confidence") is not None
                    else None
                ),
                decision_band=band,
            )
            session.add(row)
            await session.flush()
            return row

    async def dedup_review_list(
        self,
        tenant_id: str,
        status: str = "pending",
        limit: int = 50,
    ) -> list[DedupReview]:
        """Return reviews for ``tenant_id`` filtered by ``status``,
        newest-first. Default ``status='pending'`` keeps the busy-queue
        case (decided rows piled up) from drowning the caller."""
        async with get_session() as session:
            stmt = (
                select(DedupReview)
                .where(
                    DedupReview.tenant_id == tenant_id,
                    DedupReview.status == status,
                )
                .order_by(DedupReview.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def dedup_review_decide(
        self, review_id: UUID, status: str, decided_by: str | None = None
    ) -> DedupReview | None:
        """Transition a review from ``pending`` to one of the terminal
        statuses (``confirmed_duplicate`` / ``override_not_duplicate``
        / ``dismissed``). Returns the updated row, or None if the row
        doesn't exist. Raises ``ValueError`` for unknown statuses."""
        from common.models.dedup_review import DEDUP_REVIEW_STATUSES

        if status not in DEDUP_REVIEW_STATUSES or status == "pending":
            raise ValueError(f"invalid terminal status: {status!r}")

        async with get_session() as session:
            row = await session.get(DedupReview, review_id)
            if row is None:
                return None
            row.status = status
            row.decided_by = decided_by
            row.decided_at = datetime.now(UTC)
            await session.flush()
            return row

    async def memory_bulk_find_by_content_hashes(
        self,
        tenant_id: str,
        hashes: list[str],
        fleet_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, dict]:
        """Map ``content_hash → {id, client_request_id}`` for existing rows.

        ``client_request_id`` is included so the upstream bulk-write
        path can distinguish the two duplicate states (CAURA-602):
        a content match whose stored ``client_request_id`` equals the
        current request's per-item id is the caller's *own* retry
        (``duplicate_attempt``); any other match is a different
        attempt's content (``duplicate_content``). NULL on legacy rows
        written before the column existed.

        ``agent_id`` scopes the dedup lookup so cross-agent writes of
        identical content no longer collide (Stage 5 / friction §2.8).
        """
        async with get_session() as session:
            stmt = select(Memory.content_hash, Memory.id, Memory.client_request_id).where(
                Memory.tenant_id == tenant_id,
                Memory.content_hash.in_(hashes),
                Memory.deleted_at.is_(None),
            )
            if fleet_id:
                stmt = stmt.where(Memory.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Memory.fleet_id.is_(None))
            if agent_id is not None:
                stmt = stmt.where(Memory.agent_id == agent_id)
            rows = (await session.execute(stmt)).all()
            return {row[0]: {"id": row[1], "client_request_id": row[2]} for row in rows}

    # ------------------------------------------------------------------
    # C) Scored search (CTE-based)
    # ------------------------------------------------------------------

    async def memory_scored_search(
        self,
        tenant_id: str,
        embedding: list[float],
        query: str,
        *,
        fleet_ids: list[str] | None = None,
        caller_agent_id: str | None = None,
        filter_agent_id: str | None = None,
        memory_type_filter: str | None = None,
        status_filter: str | None = None,
        valid_at: datetime | None = None,
        boosted_memory_ids: set[UUID] | None = None,
        memory_boost_factor: dict[UUID, float] | None = None,
        search_params: dict,
        temporal_window: timedelta | None = None,
        recall_boost_enabled: bool = True,
        top_k: int = 10,
        date_range_start: str | None = None,
        date_range_end: str | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[SimpleNamespace]:
        """Execute the full CTE-based scored search with entity-link JOIN.

        Returns a list of SimpleNamespace objects with attributes:
        Memory, score, similarity, vec_sim, entity_links.
        """
        boosted_memory_ids = boosted_memory_ids or set()
        memory_boost_factor = memory_boost_factor or {}
        sp = search_params

        _fts_weight = sp["fts_weight"]
        _freshness_floor = sp["freshness_floor"]
        _freshness_decay_days = sp["freshness_decay_days"]
        _recall_boost_cap = sp["recall_boost_cap"]
        _recall_decay_window_days = sp["recall_decay_window_days"]
        _similarity_blend = sp["similarity_blend"]
        _top_k = sp.get("top_k", top_k)

        # -- Scoring expressions --
        # CAURA-594: pgvector's `<=>` is strict — NULL in → NULL out. A
        # bare `1 - cosine_distance` would therefore propagate NULL up
        # through the similarity blend into `score`, and PostgreSQL's
        # default `ORDER BY score DESC` sorts NULLS FIRST — putting
        # every unembedded row at the TOP of results. The CASE forces
        # a numeric value (0.0) and also short-circuits so
        # cosine_distance isn't even evaluated for NULL rows.
        # `has_embedding` below is the authoritative NULL-vs-orthogonal
        # signal for callers — `vec_sim == 0.0` is ambiguous with a
        # genuinely orthogonal embedding.
        vec_sim = case(
            (
                Memory.embedding.is_not(None),
                1.0 - Memory.embedding.cosine_distance(embedding),
            ),
            else_=0.0,
        ).label("vec_sim")
        has_embedding = Memory.embedding.is_not(None).label("has_embedding")

        ts_query = func.plainto_tsquery("english", query)
        raw_fts = func.ts_rank_cd(Memory.search_vector, ts_query)
        fts_score = (raw_fts / (1.0 + raw_fts)).label("fts_score")

        # CAURA-679: NULL-embedding rows fall back to `fts_score` alone
        # rather than the `(1 - w) * 0 + w * fts_score` haircut that
        # the unconditional blend would apply. The haircut multiplies
        # the FTS signal by `fts_weight` (≤1), so an FTS-matching but
        # unembedded row could rank below noise-floor-cosine embedded
        # rows (vec_sim ~0.15-0.20 from high-dim sphere clustering),
        # which then drop it past the `LIMIT top_k * overfetch_factor`
        # cutoff. This protects the FTS-fallback contract for both the
        # CAURA-594 deferred-embed window and any case where the embed
        # worker fails permanently — the row stays discoverable rather
        # than silently undiscoverable.
        similarity = case(
            (
                Memory.embedding.is_not(None),
                (1.0 - _fts_weight) * vec_sim + _fts_weight * fts_score,
            ),
            else_=fts_score,
        ).label("similarity")

        anchor = func.greatest(
            Memory.created_at,
            func.coalesce(Memory.ts_valid_start, Memory.created_at),
        )
        age_days = func.extract("epoch", func.now() - anchor) / 86400.0

        type_decay = case(
            *[(Memory.memory_type == mt, float(days)) for mt, days in TYPE_DECAY_DAYS.items()],
            else_=float(_freshness_decay_days),
        ).label("type_decay_days")

        freshness = case(
            (
                and_(
                    Memory.ts_valid_end.is_not(None),
                    Memory.ts_valid_end < func.now(),
                ),
                _freshness_floor,
            ),
            (Memory.ts_valid_end.is_not(None), 1.0),
            (
                age_days < type_decay,
                1.0 - (age_days / type_decay) * (1.0 - _freshness_floor),
            ),
            else_=_freshness_floor,
        ).label("freshness")

        if recall_boost_enabled:
            days_since_recall = (
                func.extract(
                    "epoch",
                    func.now() - func.coalesce(Memory.last_recalled_at, Memory.created_at),
                )
                / 86400.0
            )
            recency_factor = func.greatest(0.0, 1.0 - days_since_recall / _recall_decay_window_days)
            recall_boost_expr = (
                1.0
                + (_recall_boost_cap - 1.0)
                * recency_factor
                * Memory.recall_count
                / (Memory.recall_count + RECALL_BOOST_SCALE)
            ).label("recall_boost")
        else:
            recall_boost_expr = literal_column("1.0").label("recall_boost")

        base_score = (_similarity_blend * similarity + (1.0 - _similarity_blend) * Memory.weight).label(
            "base_score"
        )

        if temporal_window is not None:
            cutoff = func.now() - temporal_window
            temporal_boost = case(
                (Memory.created_at >= cutoff, 1.3),
                else_=1.0,
            ).label("temporal_boost")
        else:
            temporal_boost = literal_column("1.0").label("temporal_boost")

        # Soft date-range boost: multiplies the score for memories whose
        # anchor date falls inside the query-extracted window.  Pairs with
        # tighter padding in ``_extract_temporal_date_range`` — replaces
        # the old hard WHERE filter so semantically strong out-of-range
        # memories remain retrievable.
        if date_range_start and date_range_end:
            from datetime import date as date_type

            from sqlalchemy import Date, cast, literal

            from core_storage_api.config import settings as _storage_settings

            temporal_anchor = func.coalesce(
                cast(Memory.ts_valid_start, Date),
                cast(Memory.created_at, Date),
            )
            _start_dt = date_type.fromisoformat(date_range_start)
            _end_dt = date_type.fromisoformat(date_range_end)
            date_range_boost = case(
                (
                    and_(
                        temporal_anchor >= cast(literal(_start_dt), Date),
                        temporal_anchor <= cast(literal(_end_dt), Date),
                    ),
                    _storage_settings.date_range_boost_factor,
                ),
                else_=1.0,
            ).label("date_range_boost")
        else:
            date_range_boost = literal_column("1.0").label("date_range_boost")

        status_penalty = case(
            (Memory.status.in_(("outdated", "conflicted")), 0.5),
            else_=1.0,
        ).label("status_penalty")

        # Soft currency factor: memories whose ts_valid_end is in the past
        # relative to valid_at are down-weighted instead of excluded.
        # Pairs with the removal of the `ts_valid_end >= valid_at` WHERE
        # clause below — one bad enrichment date no longer blanks a memory.
        if valid_at is not None:
            from core_storage_api.config import settings as _storage_settings_cf

            currency_factor = case(
                (
                    and_(
                        Memory.ts_valid_end.is_not(None),
                        Memory.ts_valid_end < valid_at,
                    ),
                    _storage_settings_cf.expired_currency_factor,
                ),
                else_=1.0,
            ).label("currency_factor")
        else:
            currency_factor = literal_column("1.0").label("currency_factor")

        if boosted_memory_ids and memory_boost_factor:
            boost_tiers: dict[float, list[UUID]] = {}
            for mid, factor in memory_boost_factor.items():
                boost_tiers.setdefault(factor, []).append(mid)
            whens = [
                (Memory.id.in_(mids), factor) for factor, mids in sorted(boost_tiers.items(), reverse=True)
            ]
            entity_boost = case(*whens, else_=1.0).label("entity_boost")
            score = (
                base_score
                * freshness
                * entity_boost
                * recall_boost_expr
                * temporal_boost
                * date_range_boost
                * currency_factor
                * status_penalty
            ).label("score")
        else:
            score = (
                base_score
                * freshness
                * recall_boost_expr
                * temporal_boost
                * date_range_boost
                * currency_factor
                * status_penalty
            ).label("score")

        # -- Build scored CTE --
        # CAURA-594: NULL-embedding rows are admitted only if they also
        # match the FTS query — otherwise they'd rank on `Memory.weight *
        # freshness * ...` alone and could fill top_k slots with rows
        # that have no relationship to the query during a large backfill
        # window. `search_vector @@ ts_query` is GIN-indexed, so the
        # extra predicate is free for rows that already had to scan
        # the tenant/fleet slice.
        # Other paths (find_semantic_duplicate, find_similar_candidates,
        # find_neighbors_by_embedding, compute_health_stats) keep their
        # NULL guards — vector-pure operations where a NULL operand has
        # no comparable semantics.
        # `plainto_tsquery('english', '')` (and any whitespace-only or
        # stop-word-only input it normalises down to empty) returns the
        # empty `tsquery`, which `@@`-matches every non-NULL `tsvector`
        # — that would silently re-admit every NULL-embedding row when
        # callers pass `query=""` or `query="   "` (e.g. entity-only /
        # vector-only search modes), bringing back the displacement-by-
        # weight bug. Gate on the Python-side query string so the
        # operator is only emitted when there's actual text to match.
        _fts_guard = Memory.search_vector.op("@@")(ts_query) if query and query.strip() else false()
        scored_stmt = (
            select(
                Memory.id.label("mem_id"),
                score,
                similarity,
                vec_sim,
                has_embedding,
                status_penalty,
            )
            # Multi-tenant read predicate: when ``readable_tenant_ids``
            # is provided (cross-tenant agent key), reads widen across
            # the full set; otherwise we stay single-tenant for the
            # common case. Result rows still carry ``Memory.tenant_id``
            # so the caller can attribute each row to its source tenant.
            .where(
                Memory.tenant_id.in_(readable_tenant_ids)
                if readable_tenant_ids
                else Memory.tenant_id == tenant_id
            )
            .where(Memory.deleted_at.is_(None))
            .where(
                or_(
                    Memory.embedding.is_not(None),
                    _fts_guard,
                )
            )
        )

        if fleet_ids:
            scored_stmt = scored_stmt.where(
                or_(
                    Memory.fleet_id.in_(fleet_ids),
                    Memory.fleet_id.is_(None),
                    Memory.visibility == "scope_org",
                )
            )

        if caller_agent_id:
            visibility_filter = or_(
                Memory.visibility == "scope_org",
                Memory.visibility == "scope_team",
                and_(
                    Memory.visibility == "scope_agent",
                    Memory.agent_id == caller_agent_id,
                ),
            )
            scored_stmt = scored_stmt.where(visibility_filter)
        else:
            scored_stmt = scored_stmt.where(Memory.visibility != "scope_agent")

        if filter_agent_id:
            scored_stmt = scored_stmt.where(Memory.agent_id == filter_agent_id)
        if memory_type_filter:
            scored_stmt = scored_stmt.where(Memory.memory_type == memory_type_filter)
        if status_filter:
            scored_stmt = scored_stmt.where(Memory.status == status_filter)
        else:
            # Exclude superseded memories from default search results. The
            # contradiction detector marks the older row ``outdated`` (RDF
            # path) or ``conflicted`` (semantic path) and points the newer
            # one at it via ``supersedes_id``. Surfacing both would dilute
            # ranking with stale claims agents shouldn't act on. Callers
            # that need to inspect superseded rows pass an explicit
            # ``status_filter`` to override.
            scored_stmt = scored_stmt.where(Memory.status.notin_(("outdated", "conflicted")))
        if valid_at:
            from datetime import date as _date_type

            from sqlalchemy import Date as _Date
            from sqlalchemy import cast as _cast
            from sqlalchemy import literal as _literal

            # Hard filter on the START side, compared at DAY granularity.
            # Future-dated memories can't answer past questions — but strict
            # timestamp comparison also excludes same-day memories written a
            # few hours after the query was asked, which is too aggressive
            # for workflows where the question + its evidence share a day.
            # We cast both sides to DATE so same-day-later memories pass.
            _valid_at_date = (
                valid_at.date() if hasattr(valid_at, "date") else _date_type.fromisoformat(str(valid_at)[:10])
            )
            scored_stmt = scored_stmt.where(
                or_(
                    Memory.ts_valid_start.is_(None),
                    _cast(Memory.ts_valid_start, _Date) <= _cast(_literal(_valid_at_date), _Date),
                ),
            )
            # NOTE: the END side (`ts_valid_end >= valid_at`) is NO LONGER
            # a hard filter.  A past ts_valid_end now triggers the soft
            # ``currency_factor`` above (default 0.5x) — so an over-eager
            # enrichment date can't silently hide a semantically strong
            # memory from historical-question queries.

        # NOTE: date_range_start/end no longer produces a hard WHERE filter;
        # the multiplier ``date_range_boost`` above handles it softly.

        scored_stmt = scored_stmt.order_by(score.desc(), Memory.created_at.desc()).limit(_top_k)

        scored_cte = scored_stmt.cte("scored")

        # -- Outer query: JOIN Memory + LEFT JOIN entity links --
        stmt = (
            select(
                Memory,
                scored_cte.c.score,
                scored_cte.c.similarity,
                scored_cte.c.vec_sim,
                scored_cte.c.has_embedding,
                scored_cte.c.status_penalty,
                MemoryEntityLink.entity_id,
                MemoryEntityLink.role,
            )
            .join(scored_cte, Memory.id == scored_cte.c.mem_id)
            .outerjoin(MemoryEntityLink, Memory.id == MemoryEntityLink.memory_id)
            .order_by(scored_cte.c.score.desc(), Memory.created_at.desc())
        )

        # db_ms captures only pool wait + SQL round-trip; materialise rows
        # inside the session (they hold lazy-load handles), then drop the
        # Python-side grouping outside the measured block so OrderedDict
        # work doesn't inflate the DB timing signal.
        with db_measure():
            async with get_read_session() as session:
                result = await session.execute(stmt)
                rows = result.all()

        grouped: OrderedDict[UUID, SimpleNamespace] = OrderedDict()
        for row in rows:
            mid = row.Memory.id
            if mid not in grouped:
                grouped[mid] = SimpleNamespace(
                    Memory=row.Memory,
                    score=row.score,
                    similarity=row.similarity,
                    vec_sim=row.vec_sim,
                    has_embedding=row.has_embedding,
                    status_penalty=row.status_penalty,
                    entity_links=[],
                )
            if row.entity_id is not None:
                grouped[mid].entity_links.append({"entity_id": row.entity_id, "role": row.role})
        return list(grouped.values())

    # ------------------------------------------------------------------
    # C-2) Load specific memories by ID (ENTITY_LOOKUP short-circuit)
    # ------------------------------------------------------------------

    async def memory_load_by_ids(
        self,
        memory_ids: list[UUID],
        tenant_id: str,
        *,
        fleet_ids: list[str] | None = None,
        caller_agent_id: str | None = None,
        filter_agent_id: str | None = None,
        memory_type_filter: str | None = None,
        status_filter: str | None = None,
        valid_at: datetime | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[Memory]:
        """Load memories by ID with visibility/fleet/agent filters applied.

        Used by the ENTITY_LOOKUP short-circuit in ``ClassifyQuery._collect_memories``,
        which already chose the specific memory IDs based on entity graph
        expansion and just needs them loaded — no vector cosine, no FTS,
        no freshness scoring.

        No server-side ``top_k`` LIMIT: the caller has already capped the
        ID set at ``GRAPH_MAX_BOOSTED_MEMORIES`` (=50) and will apply the
        user-facing ``top_k`` AFTER sorting by hop-distance boost. A SQL
        LIMIT here would return an arbitrary subset (no ORDER BY in this
        query) and silently discard high-boost rows before the sort.

        CAURA-687: the short-circuit previously POSTed to ``/memories/
        scored-search`` with a ``memory_ids`` key + ``entity_lookup: True``
        flag the route never read, so storage hard-indexed ``body["embedding"]``
        and 500'd. The broad except at classify_query.py:123 swallowed it,
        and the path silently fell through to keyword/semantic.

        Filter semantics MUST match ``memory_scored_search`` exactly so the
        short-circuit and the scored-search fallthrough surface identical
        rows when given the same filter args. Any drift is a cross-tenant
        leak risk — keep these two WHERE-clause blocks in sync.
        """
        if not memory_ids:
            return []
        async with get_read_session() as session:
            stmt = select(Memory).where(
                Memory.id.in_(memory_ids),
                Memory.tenant_id.in_(readable_tenant_ids)
                if readable_tenant_ids
                else Memory.tenant_id == tenant_id,
                Memory.deleted_at.is_(None),
            )
            if fleet_ids:
                stmt = stmt.where(
                    or_(
                        Memory.fleet_id.in_(fleet_ids),
                        Memory.fleet_id.is_(None),
                        Memory.visibility == "scope_org",
                    )
                )
            if caller_agent_id:
                stmt = stmt.where(
                    or_(
                        Memory.visibility == "scope_org",
                        Memory.visibility == "scope_team",
                        and_(
                            Memory.visibility == "scope_agent",
                            Memory.agent_id == caller_agent_id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(Memory.visibility != "scope_agent")
            if filter_agent_id:
                stmt = stmt.where(Memory.agent_id == filter_agent_id)
            if memory_type_filter:
                stmt = stmt.where(Memory.memory_type == memory_type_filter)
            if status_filter:
                stmt = stmt.where(Memory.status == status_filter)
            else:
                # Mirrors scored_search: exclude superseded memories from
                # default results. Callers wanting them pass status_filter.
                stmt = stmt.where(Memory.status.notin_(("outdated", "conflicted")))
            if valid_at:
                from datetime import date as _date_type

                from sqlalchemy import Date as _Date
                from sqlalchemy import cast as _cast
                from sqlalchemy import literal as _literal

                # DATE-cast comparison matches scored_search semantics
                # (same-day-later memories pass). End side is intentionally
                # NOT a hard filter — see scored_search currency_factor.
                _valid_at_date = (
                    valid_at.date()
                    if hasattr(valid_at, "date")
                    else _date_type.fromisoformat(str(valid_at)[:10])
                )
                stmt = stmt.where(
                    or_(
                        Memory.ts_valid_start.is_(None),
                        _cast(Memory.ts_valid_start, _Date) <= _cast(_literal(_valid_at_date), _Date),
                    ),
                )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # D-0) Supersedes chain: find successor memories
    # ------------------------------------------------------------------

    async def memory_find_successors(
        self,
        supersedes_ids: list[UUID],
        tenant_id: str,
        *,
        fleet_ids: list[str] | None = None,
        caller_agent_id: str | None = None,
        filter_agent_id: str | None = None,
        memory_type_filter: str | None = None,
        valid_at: datetime | None = None,
    ) -> list[Memory]:
        """Find active/confirmed memories that supersede the given memory IDs."""
        async with get_session() as session:
            stmt = select(Memory).where(
                Memory.tenant_id == tenant_id,
                Memory.supersedes_id.in_(supersedes_ids),
                Memory.status.in_(("active", "confirmed")),
                Memory.deleted_at.is_(None),
            )
            if fleet_ids:
                stmt = stmt.where(
                    or_(
                        Memory.fleet_id.in_(fleet_ids),
                        Memory.fleet_id.is_(None),
                        Memory.visibility == "scope_org",
                    )
                )
            if caller_agent_id:
                stmt = stmt.where(
                    or_(
                        Memory.visibility == "scope_org",
                        Memory.visibility == "scope_team",
                        and_(
                            Memory.visibility == "scope_agent",
                            Memory.agent_id == caller_agent_id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(Memory.visibility != "scope_agent")
            if filter_agent_id:
                stmt = stmt.where(Memory.agent_id == filter_agent_id)
            if memory_type_filter:
                stmt = stmt.where(Memory.memory_type == memory_type_filter)
            if valid_at:
                stmt = stmt.where(
                    or_(
                        Memory.ts_valid_start.is_(None),
                        Memory.ts_valid_start <= valid_at,
                    ),
                ).where(
                    or_(
                        Memory.ts_valid_end.is_(None),
                        Memory.ts_valid_end >= valid_at,
                    ),
                )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # D) Contradiction detection
    # ------------------------------------------------------------------

    async def memory_find_entity_overlap_candidates(
        self,
        memory_id: UUID,
        tenant_id: str,
        fleet_id: str | None = None,
        visibility: str = "scope_team",
        limit: int = CONTRADICTION_CANDIDATE_MAX,
        include_supersedes: bool = False,
    ) -> list[Memory]:
        """Find active memories sharing entities with the given memory by entity name.

        Joins through Entity.canonical_name to find overlap. When fleet_id is
        provided, candidates are scoped to the same fleet. ``visibility``
        scopes candidates to the writer's visibility tier so a scope_org
        write can't be linked into a scope_team chain (and vice versa).

        ``include_supersedes`` (A4 #11): when True, also return the
        ``conflicted`` row that ``memory_id``'s chain points at — i.e.
        the candidate Path A retracted FOR this memory. Path C uses
        this to re-judge Path A's verdict (A4 #13).

        Filter direction: a row M is included via this branch iff
        ``M.status == 'conflicted'`` AND
        ``memory_id``'s ``supersedes_id`` field equals ``M.id``. This
        matches Path A's chain shape — the *newer/active* row carries
        ``supersedes_id`` pointing back at the *older/conflicted* row;
        the conflicted row itself has ``supersedes_id=NULL``.

        The original A4 #11 (PR #185) used ``Memory.supersedes_id ==
        memory_id`` instead, which was structurally inverted — it
        looked for "conflicted rows pointing at me" but Path A leaves
        the conflicted row's ``supersedes_id`` as NULL. That filter
        matched zero production rows. See
        ``flow-debug-contradiction-chain-shape`` memory for the
        investigation that surfaced it.

        Default ``False`` preserves the back-compat behaviour for every
        existing caller.
        """
        async with get_session() as session:
            # Subquery: canonical names of entities linked to the target memory
            new_mel = MemoryEntityLink.__table__.alias("new_mel")
            new_ent = Entity.__table__.alias("new_ent")
            new_entity_names = (
                select(func.lower(new_ent.c.canonical_name))
                .select_from(new_mel.join(new_ent, new_mel.c.entity_id == new_ent.c.id))
                .where(new_mel.c.memory_id == memory_id)
                .subquery()
            )

            # Find other memories whose entities share canonical names
            other_mel = MemoryEntityLink.__table__.alias("other_mel")
            other_ent = Entity.__table__.alias("other_ent")

            if include_supersedes:
                # Subquery: the supersedes_id field on the query target
                # memory itself. If non-NULL, it points at the row Path A
                # marked conflicted on this memory's behalf.
                target_supersedes = (
                    select(Memory.supersedes_id).where(Memory.id == memory_id).scalar_subquery()
                )
                status_filter = or_(
                    Memory.status.in_(("active", "confirmed", "pending")),
                    and_(
                        Memory.status == "conflicted",
                        Memory.id == target_supersedes,
                    ),
                )
            else:
                status_filter = Memory.status.in_(("active", "confirmed", "pending"))

            stmt = (
                select(
                    Memory,
                    func.count(func.distinct(other_ent.c.canonical_name)).label("shared"),
                )
                .select_from(
                    Memory.__table__.join(other_mel, other_mel.c.memory_id == Memory.id).join(
                        other_ent, other_mel.c.entity_id == other_ent.c.id
                    )
                )
                .where(
                    func.lower(other_ent.c.canonical_name).in_(select(new_entity_names)),
                    Memory.tenant_id == tenant_id,
                    Memory.id != memory_id,
                    Memory.deleted_at.is_(None),
                    status_filter,
                    Memory.visibility == visibility,
                    *([Memory.fleet_id == fleet_id] if fleet_id else []),
                )
                .group_by(Memory.id)
                .order_by(func.count(func.distinct(other_ent.c.canonical_name)).desc())
                .limit(limit)
            )

            result = await session.execute(stmt)
            return [row.Memory for row in result.all()]

    async def memory_find_rdf_conflicts(
        self,
        tenant_id: str,
        subject_entity_id: UUID,
        predicate: str,
        object_value: str,
        memory_id: UUID,
        fleet_id: str | None = None,
    ) -> list[Memory]:
        async with get_session() as session:
            stmt = select(Memory).where(
                Memory.tenant_id == tenant_id,
                Memory.deleted_at.is_(None),
                Memory.status.in_(("active", "confirmed", "pending")),
                Memory.subject_entity_id == subject_entity_id,
                func.lower(Memory.predicate) == predicate.lower(),
                Memory.object_value != object_value,
                Memory.id != memory_id,
            )
            if fleet_id:
                stmt = stmt.where(Memory.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Memory.fleet_id.is_(None))

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def memory_find_similar_candidates(
        self,
        tenant_id: str,
        fleet_id: str | None,
        embedding: list[float],
        memory_id: UUID,
        visibility: str = "scope_team",
        threshold: float = CONTRADICTION_SIMILARITY_THRESHOLD,
        limit: int = CONTRADICTION_CANDIDATE_MAX,
    ) -> list[Memory]:
        async with get_session() as session:
            distance = Memory.embedding.cosine_distance(embedding)
            similarity = (1.0 - distance).label("similarity")

            stmt = (
                select(Memory, similarity)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.deleted_at.is_(None),
                    Memory.status.in_(("active", "confirmed", "pending")),
                    Memory.embedding.is_not(None),
                    Memory.id != memory_id,
                )
                .where((1.0 - distance) >= threshold)
                .order_by(distance)
                .limit(limit)
            )

            if fleet_id:
                stmt = stmt.where(Memory.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Memory.fleet_id.is_(None))

            stmt = stmt.where(Memory.visibility == visibility)

            result = await session.execute(stmt)
            return [row.Memory for row in result.all()]

    # ------------------------------------------------------------------
    # E) Lifecycle batch
    # ------------------------------------------------------------------

    async def memory_archive_expired(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        batch_size: int = 500,
    ) -> int:
        async with get_session() as session:
            params: dict = {"tenant_id": tenant_id, "batch_size": batch_size}
            fleet_clause = ""
            if fleet_id:
                fleet_clause = "AND fleet_id = :fleet_id"
                params["fleet_id"] = fleet_id

            result = await session.execute(
                text(f"""
                UPDATE memories SET status = 'outdated'
                WHERE id IN (
                    SELECT id FROM memories
                    WHERE tenant_id = :tenant_id
                      {fleet_clause}
                      AND ts_valid_end < NOW()
                      AND status = 'active'
                      AND deleted_at IS NULL
                    LIMIT :batch_size
                )
                RETURNING id
            """),
                params,
            )
            return len(result.all())

    async def memory_archive_stale(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        stale_days: int = 90,
        max_weight: float = 0.3,
        batch_size: int = 500,
    ) -> int:
        async with get_session() as session:
            params: dict = {
                "tenant_id": tenant_id,
                "stale_days": stale_days,
                "max_weight": max_weight,
                "batch_size": batch_size,
            }
            fleet_clause = ""
            if fleet_id:
                fleet_clause = "AND fleet_id = :fleet_id"
                params["fleet_id"] = fleet_id

            result = await session.execute(
                text(f"""
                UPDATE memories SET status = 'archived'
                WHERE id IN (
                    SELECT id FROM memories
                    WHERE tenant_id = :tenant_id
                      {fleet_clause}
                      AND created_at < NOW() - INTERVAL '1 day' * :stale_days
                      AND recall_count = 0
                      AND weight < :max_weight
                      AND status = 'active'
                      AND deleted_at IS NULL
                    LIMIT :batch_size
                )
                RETURNING id
            """),
                params,
            )
            return len(result.all())

    async def memory_purge_soft_deleted(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        retention_days: int = MEMORY_RETENTION_MAX_DAYS,
        batch_size: int = 500,
    ) -> int:
        """Hard-delete soft-deleted memories whose ``deleted_at`` is older
        than ``retention_days``. Soft-deletes (CAURA-656) keeps rows
        addressable for a grace period before they're physically removed,
        so a misclick or buggy client can be undone for ``retention_days``
        days. After that window the row is gone for good — including its
        embedding, entity links, and idempotency response cache lines
        (cascaded by their FKs to ``memories.id``).
        """
        async with get_session() as session:
            params: dict = {
                "tenant_id": tenant_id,
                "retention_days": retention_days,
                "batch_size": batch_size,
            }
            fleet_clause = ""
            if fleet_id:
                fleet_clause = "AND fleet_id = :fleet_id"
                params["fleet_id"] = fleet_id

            result = await session.execute(
                text(f"""
                DELETE FROM memories
                WHERE id IN (
                    SELECT id FROM memories
                    WHERE tenant_id = :tenant_id
                      {fleet_clause}
                      AND deleted_at IS NOT NULL
                      AND deleted_at < NOW() - INTERVAL '1 day' * :retention_days
                    LIMIT :batch_size
                )
                RETURNING id
            """),
                params,
            )
            return len(result.all())

    async def purge_tenant_data(self, tenant_id: str) -> dict[str, int]:
        """Permanently delete EVERY row scoped to ``tenant_id`` across the
        OSS schema — the hard side of organization deletion (CAURA-689).

        Runs in one transaction, children-before-parents, so a foreign key
        never blocks a delete and a mid-purge failure rolls back cleanly.
        Tables without their own ``tenant_id`` (``memory_entity_links``)
        are removed by the CASCADE from ``memories`` / ``entities``;
        ``organization_settings*`` are keyed by ``org_id`` (== tenant id in
        OSS). ``lifecycle_audit`` is intentionally retained as the audit
        trail. Returns per-table deleted counts.

        Idempotent: re-running on an already-purged tenant deletes nothing
        and returns zeros, so a retried org hard-delete is safe.

        The enterprise ``enterprise.*`` rows (org, tenants, keys, …) are
        purged separately by platform-storage-api; this owns the OSS
        ``public.*`` schema only.
        """
        counts: dict[str, int] = {}
        async with get_session() as session:
            # One DELETE per table in the declared children-before-parents
            # order (so a foreign key never blocks a delete) and one rowcount
            # per table (the per-table breakdown is a reported feature). The
            # two groups differ only in the scoping column: most tables carry
            # ``tenant_id``; ``organization_settings*`` carry ``org_id``.
            for tables, column in (
                (_PURGE_TENANT_TABLES, "tenant_id"),
                (_PURGE_ORG_KEYED_TABLES, "org_id"),
            ):
                for table_name in tables:
                    # Schema-qualify (public.) so an irreversible hard-delete
                    # can't be redirected by a non-default search_path.
                    result = await session.execute(
                        text(f"DELETE FROM public.{table_name} WHERE {column} = :tid"),
                        {"tid": tenant_id},
                    )
                    counts[table_name] = result.rowcount  # type: ignore[attr-defined]
        return counts

    async def count_tenant_data(self, tenant_id: str) -> dict[str, int]:
        """Per-table row count for a ``tenant_id`` across the OSS schema
        (CAURA-696). Mirrors ``purge_tenant_data``'s table set + column
        layout so the preview is an accurate forecast of what the
        purge would delete — adding a new table to the purge list
        means adding it here too (one source of truth would be nice
        but the two ops have different read/write modes, so we
        accept the parallel constants and call out the invariant in
        comments on both).

        Cheap, read-only: one ``SELECT count(*)`` per table, all
        keyed off the same indexed ``tenant_id`` / ``org_id``
        columns. Reports zeros for tables with no rows so the caller
        gets the full breakdown without a follow-up round-trip.
        """
        counts: dict[str, int] = {}
        async with get_read_session() as session:
            for tables, column in (
                (_PURGE_TENANT_TABLES, "tenant_id"),
                (_PURGE_ORG_KEYED_TABLES, "org_id"),
            ):
                for table_name in tables:
                    # Schema-qualify (``public.``) for the same reason
                    # ``purge_tenant_data`` does — a non-default
                    # ``search_path`` could otherwise target the wrong
                    # rows / mask drift between preview and purge.
                    result = await session.execute(
                        text(f"SELECT count(*) FROM public.{table_name} WHERE {column} = :tid"),
                        {"tid": tenant_id},
                    )
                    counts[table_name] = int(result.scalar() or 0)
        return counts

    async def set_tenant_suppression(
        self,
        tenant_id: str,
        *,
        action: str,
        updated_by: str | None = None,
    ) -> dict:
        """Upsert one row in ``public.tenant_suppression`` (CAURA-694).

        ``action='suppress'`` sets ``suppressed_at = now()``;
        ``action='restore'`` clears it. Returns the resulting row so the
        consumer can log the post-state without an extra round-trip.

        Idempotent: a duplicate ``suppress`` keeps the original
        ``suppressed_at`` (we DO NOT overwrite — the first suppression
        time is the meaningful one) but still bumps ``updated_at`` /
        ``updated_by``. A duplicate ``restore`` is a no-op-shaped
        update that still leaves the row in the ``live`` state.
        """
        if action not in {"suppress", "restore"}:
            raise ValueError(f"unknown suppression action: {action!r}")
        async with get_session() as session:
            if action == "suppress":
                # ON CONFLICT: keep the original suppressed_at (first one
                # wins) so we record WHEN the suppression actually began,
                # not the latest re-publish of the same decision.
                result = await session.execute(
                    text("""
                        INSERT INTO public.tenant_suppression
                            (tenant_id, suppressed_at, updated_at, updated_by)
                        VALUES (:tid, now(), now(), :who)
                        ON CONFLICT (tenant_id) DO UPDATE
                          SET suppressed_at = COALESCE(
                                public.tenant_suppression.suppressed_at,
                                EXCLUDED.suppressed_at
                              ),
                              updated_at = now(),
                              updated_by = EXCLUDED.updated_by
                        RETURNING tenant_id, suppressed_at, updated_at, updated_by
                    """),
                    {"tid": tenant_id, "who": updated_by},
                )
            else:  # restore
                result = await session.execute(
                    text("""
                        INSERT INTO public.tenant_suppression
                            (tenant_id, suppressed_at, updated_at, updated_by)
                        VALUES (:tid, NULL, now(), :who)
                        ON CONFLICT (tenant_id) DO UPDATE
                          SET suppressed_at = NULL,
                              updated_at = now(),
                              updated_by = EXCLUDED.updated_by
                        RETURNING tenant_id, suppressed_at, updated_at, updated_by
                    """),
                    {"tid": tenant_id, "who": updated_by},
                )
            row = result.mappings().one()
            return dict(row)

    async def is_tenant_suppressed(self, tenant_id: str) -> bool:
        """Boundary-guard primitive used by core-api auth (CAURA-694).

        Returns ``True`` iff a row exists with ``suppressed_at IS NOT
        NULL``. A missing row (never touched by the lifecycle) is the
        same as "live" — that's the standalone-OSS and pre-CAURA-694
        deployment shape. Hot path: one indexed PK lookup per
        authenticated request.
        """
        async with get_read_session() as session:
            result = await session.execute(
                text(
                    "SELECT 1 FROM public.tenant_suppression "
                    "WHERE tenant_id = :tid AND suppressed_at IS NOT NULL"
                ),
                {"tid": tenant_id},
            )
            return result.first() is not None

    async def memory_count_active(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> int:
        async with get_read_session() as session:
            stmt = (
                select(func.count())
                .select_from(Memory)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.status == "active",
                    Memory.deleted_at.is_(None),
                )
            )
            if fleet_id:
                stmt = stmt.where(Memory.fleet_id == fleet_id)
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def memory_count_all(self) -> int:
        # Exclude soft-deleted rows so the public counter matches the live,
        # queryable footprint — same predicate every other count in this
        # file uses. Without the filter, tombstoned tenant clean-ups inflate
        # the number by an order of magnitude on busy environments.
        async with get_read_session() as session:
            result = await session.scalar(
                select(func.count()).select_from(Memory).where(Memory.deleted_at.is_(None))
            )
            return result or 0

    async def memory_distinct_agent_count(self) -> int:
        """Count distinct agent identities across all memories in all tenants.

        Powers the public Agents counter — reflects actual agent *activity*
        (wrote at least one memory) rather than provisioned API keys. Agents
        whose memories have all been soft-deleted are excluded so the count
        tracks live activity, not historical churn.
        """
        # Pure read — route via the read pool (matches the sibling
        # ``memory_distinct_tenant_count`` below). The write pool was
        # the original choice when ``get_read_session`` didn't exist;
        # leaving public-stats COUNT(DISTINCT) calls on the write
        # pool wastes a write connection on every landing-page hit.
        async with get_read_session() as session:
            result = await session.scalar(
                select(func.count(func.distinct(Memory.agent_id))).where(
                    Memory.agent_id.isnot(None),
                    Memory.deleted_at.is_(None),
                )
            )
            return result or 0

    async def memory_distinct_tenant_count(self) -> int:
        """Count distinct tenants that own at least one live memory.

        Powers the public Tenants counter so it reflects real activity
        instead of the previous hardcoded ``1`` returned by ``/api/v1/stats``.
        Mirrors ``memory_distinct_agent_count`` — soft-deleted rows excluded
        so a tenant whose memories were all tombstoned no longer inflates
        the count.
        """
        async with get_read_session() as session:
            result = await session.scalar(
                select(func.count(func.distinct(Memory.tenant_id))).where(
                    Memory.deleted_at.is_(None),
                )
            )
            return result or 0

    # ------------------------------------------------------------------
    # F) Crystallizer hygiene
    # ------------------------------------------------------------------

    async def memory_list_null_embedding_rows(
        self,
        *,
        limit: int,
        after: UUID | None = None,
        tenant_id: str | None = None,
    ) -> tuple[list[tuple[UUID, str]], int]:
        """Page through memories whose ``embedding IS NULL``.

        Returns ``(rows, total_remaining)``. Each row carries only the
        identifiers the caller needs to address a follow-up fetch
        (``id, tenant_id``). The raw ``content`` and ``content_hash``
        are NOT included — the worker uses ``GET /memories/{id}`` per
        row to retrieve them. This keeps the listing endpoint's
        response small + deterministic and avoids leaking full memory
        content via what is essentially an unauthenticated id scan
        (the storage API has no auth middleware; see Spec I in
        ``local_emb_res/specs/``). Cursor-style on ``id`` for stable
        resumability under the consumer's concurrent writes flipping
        rows from NULL to non-NULL.

        ``deleted_at`` rows are excluded — re-embedding them is wasted
        work since they're already filtered out of every read path.

        ``total_remaining`` is an exact ``COUNT(*)`` of *all* rows still
        matching the embedding-NULL + deleted-at + (optional) tenant
        filter — i.e. it uses ``base_filters``, not ``page_filters``. The
        cursor (``after``) is paging-only state; including it in the
        count would shrink the reported total as the backfill walks
        forward, which is the wrong number for the caller to drive
        progress UI / completion logging on. Acceptable on tables up to
        a few million rows; revisit with ``pg_class.reltuples`` if it
        shows up in operator profiles.
        """
        async with get_session() as session:
            base_filters = [
                Memory.embedding.is_(None),
                Memory.deleted_at.is_(None),
            ]
            if tenant_id is not None:
                base_filters.append(Memory.tenant_id == tenant_id)

            page_filters = list(base_filters)
            if after is not None:
                page_filters.append(Memory.id > after)

            stmt = (
                select(
                    Memory.id,
                    Memory.tenant_id,
                )
                .where(*page_filters)
                .order_by(Memory.id)
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()

            total_remaining = (
                await session.execute(select(func.count()).select_from(Memory).where(*base_filters))
            ).scalar_one()

            return (
                [(r[0], r[1]) for r in rows],
                int(total_remaining),
            )

    async def memory_find_missing_embeddings(
        self,
        tenant_id: str,
        fleet_id: str | None,
        batch_size: int = 100,
    ) -> list[tuple]:
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)
            result = await session.execute(
                text(f"""
                SELECT m.id, m.content
                FROM memories m
                WHERE {scope}
                  AND m.embedding IS NULL
                  AND m.deleted_at IS NULL
                LIMIT :batch_size
            """),
                {**params, "batch_size": batch_size},
            )
            return result.all()  # type: ignore[return-value]

    async def memory_find_near_duplicate_candidates(
        self,
        tenant_id: str,
        fleet_id: str | None,
        batch_size: int,
        offset: int = 0,
    ) -> list[tuple]:
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)
            result = await session.execute(
                text(f"""
                SELECT m.id, m.embedding
                FROM memories m
                WHERE {scope}
                  AND m.embedding IS NOT NULL
                  AND m.deleted_at IS NULL
                  AND m.last_dedup_checked_at IS NULL
                ORDER BY m.created_at DESC
                LIMIT :batch_size OFFSET :batch_offset
            """),
                {**params, "batch_size": batch_size, "batch_offset": offset},
            )
            return result.all()  # type: ignore[return-value]

    async def memory_find_neighbors_by_embedding(
        self,
        tenant_id: str,
        fleet_id: str | None,
        query_embedding: Any,
        exclude_id: UUID,
        threshold: float,
        limit: int,
    ) -> list[tuple]:
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id, table="n")
            result = await session.execute(
                text(f"""
                SELECT n.id,
                       1 - (n.embedding <=> :query_emb) AS similarity
                FROM memories n
                WHERE {scope}
                  AND n.embedding IS NOT NULL
                  AND n.deleted_at IS NULL
                  AND n.id != :self_id
                  AND 1 - (n.embedding <=> :query_emb) >= :threshold
                ORDER BY n.embedding <=> :query_emb
                LIMIT :k
            """),
                {
                    **params,
                    "query_emb": str(query_embedding),
                    "self_id": exclude_id,
                    "threshold": threshold,
                    "k": limit,
                },
            )
            return result.all()  # type: ignore[return-value]

    async def memory_mark_dedup_checked(
        self,
        memory_ids: list[UUID],
    ) -> None:
        if not memory_ids:
            return
        async with get_session() as session:
            await session.execute(
                sql_update(Memory).where(Memory.id.in_(memory_ids)).values(last_dedup_checked_at=func.now())
            )

    async def memory_find_expired_still_active(
        self,
        tenant_id: str,
        fleet_id: str | None,
    ) -> list[tuple]:
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)
            result = await session.execute(
                text(f"""
                SELECT m.id
                FROM memories m
                WHERE {scope}
                  AND m.ts_valid_end < NOW()
                  AND m.status = 'active'
                  AND m.deleted_at IS NULL
                LIMIT 100
            """),
                params,
            )
            return result.all()  # type: ignore[return-value]

    async def memory_find_stale_count(
        self,
        tenant_id: str,
        fleet_id: str | None,
        stale_days: int,
        max_weight: float,
    ) -> list[tuple]:
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)
            params["stale_days"] = stale_days
            params["max_weight"] = max_weight
            result = await session.execute(
                text(f"""
                SELECT m.id
                FROM memories m
                WHERE {scope}
                  AND m.created_at < NOW() - INTERVAL '1 day' * :stale_days
                  AND m.recall_count = 0
                  AND m.weight < :max_weight
                  AND m.deleted_at IS NULL
                LIMIT 100
            """),
                params,
            )
            return result.all()  # type: ignore[return-value]

    async def memory_find_short_content(
        self,
        tenant_id: str,
        fleet_id: str | None,
        min_chars: int,
    ) -> list[tuple]:
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)
            params["min_chars"] = min_chars
            result = await session.execute(
                text(f"""
                SELECT m.id
                FROM memories m
                WHERE {scope}
                  AND LENGTH(m.content) < :min_chars
                  AND m.deleted_at IS NULL
                LIMIT 100
            """),
                params,
            )
            return result.all()  # type: ignore[return-value]

    async def memory_compute_health_stats(
        self,
        tenant_id: str,
        fleet_id: str | None,
    ) -> dict:
        async with get_read_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)

            r = await session.execute(
                text(f"""
                SELECT COUNT(*) FROM memories m WHERE {scope} AND m.deleted_at IS NULL
            """),
                params,
            )
            total = r.scalar() or 0

            r = await session.execute(
                text(f"""
                SELECT COUNT(*) FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL AND m.embedding IS NOT NULL
            """),
                params,
            )
            with_embedding = r.scalar() or 0
            embedding_pct = round(with_embedding / total * 100, 1) if total > 0 else 0.0

            r = await session.execute(
                text(f"""
                SELECT m.memory_type, COUNT(*) AS cnt
                FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL
                GROUP BY m.memory_type
                ORDER BY cnt DESC
            """),
                params,
            )
            type_dist = {row[0]: row[1] for row in r.all()}

            r = await session.execute(
                text(f"""
                SELECT m.status, COUNT(*) AS cnt
                FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL
                GROUP BY m.status
                ORDER BY cnt DESC
            """),
                params,
            )
            status_dist = {row[0]: row[1] for row in r.all()}

            r = await session.execute(
                text(f"""
                SELECT AVG(m.weight),
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.weight),
                       PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY m.weight)
                FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL
            """),
                params,
            )
            wrow = r.one()
            weight_stats = {
                "avg": round(float(wrow[0]), 3) if wrow[0] is not None else None,
                "p50": round(float(wrow[1]), 3) if wrow[1] is not None else None,
                "p90": round(float(wrow[2]), 3) if wrow[2] is not None else None,
            }

            r = await session.execute(
                text(f"""
                SELECT COUNT(*) FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL AND m.status IN ('outdated', 'conflicted')
            """),
                params,
            )
            contradiction_count = r.scalar() or 0

            r = await session.execute(
                text(f"""
                SELECT COUNT(*) FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL AND m.metadata->>'contains_pii' = 'true'
            """),
                params,
            )
            pii_count = r.scalar() or 0

            r = await session.execute(
                text(f"""
                SELECT AVG(m.recall_count) FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL
            """),
                params,
            )
            avg_recall = r.scalar()
            avg_recall = round(float(avg_recall), 2) if avg_recall is not None else 0.0

            # ``total`` is duplicated under both keys for compatibility:
            # ``total_memories`` was the original field; ``total`` matches the
            # core-api stats response shape so callers that hit this endpoint
            # directly (or via storage_client.get_memory_stats fallback) don't
            # need to know about the rename.
            return {
                "total": total,
                "total_memories": total,
                "embedding_coverage_pct": embedding_pct,
                "type_distribution": type_dist,
                "status_distribution": status_dist,
                "weight_stats": weight_stats,
                "contradiction_count": contradiction_count,
                "pii_count": pii_count,
                "avg_recall_count": avg_recall,
            }

    async def memory_compute_usage_stats(
        self,
        tenant_id: str,
        fleet_id: str | None,
    ) -> dict:
        async with get_read_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)

            r = await session.execute(
                text(f"""
                SELECT m.id, m.title, m.recall_count
                FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL
                ORDER BY m.recall_count DESC
                LIMIT 10
            """),
                params,
            )
            most_recalled = [{"id": str(row[0]), "title": row[1], "recall_count": row[2]} for row in r.all()]

            r = await session.execute(
                text(f"""
                SELECT m.id, m.title, m.recall_count
                FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL AND m.status = 'active'
                ORDER BY m.recall_count ASC
                LIMIT 10
            """),
                params,
            )
            least_recalled = [{"id": str(row[0]), "title": row[1], "recall_count": row[2]} for row in r.all()]

            r = await session.execute(
                text(f"""
                SELECT m.fleet_id, COUNT(*) AS cnt
                FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL
                GROUP BY m.fleet_id
                ORDER BY cnt DESC
            """),
                params,
            )
            fleet_activity = [{"fleet_id": row[0], "memory_count": row[1]} for row in r.all()]

            return {
                "most_recalled": most_recalled,
                "least_recalled": least_recalled,
                "fleet_activity": fleet_activity,
            }

    async def memory_list_recent(
        self,
        tenant_id: str,
        fleet_id: str | None,
        *,
        limit: int = 20,
    ) -> list[Memory]:
        async with get_read_session() as session:
            stmt = (
                select(Memory)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.deleted_at.is_(None),
                    Memory.status == "active",
                )
                .order_by(Memory.created_at.desc())
                .limit(limit)
            )
            if fleet_id is not None:
                stmt = stmt.where(Memory.fleet_id == fleet_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # G) Recall tracking
    # ------------------------------------------------------------------

    async def memory_increment_recall(self, memory_ids: list[UUID]) -> None:
        if not memory_ids:
            return
        async with get_session() as session:
            await session.execute(
                sql_update(Memory)
                .where(Memory.id.in_(memory_ids))
                .values(
                    recall_count=Memory.recall_count + 1,
                    last_recalled_at=func.now(),
                )
            )

    # ------------------------------------------------------------------
    # H) Entity links (memory side)
    # ------------------------------------------------------------------

    async def memory_add_entity_links(
        self,
        memory_id: UUID,
        links: list[dict],
    ) -> None:
        # Bulk-insert with ``ON CONFLICT (memory_id, entity_id) DO NOTHING``
        # so two concurrent writes targeting the same ``(memory_id,
        # entity_id)`` pair don't serialise on ``Lock/transactionid``
        # (CAURA-686). Each ``link`` dict carries ``entity_id`` (UUID) and
        # ``role`` (str).
        if not links:
            return
        rows = [
            {"memory_id": memory_id, "entity_id": link["entity_id"], "role": link["role"]} for link in links
        ]
        async with get_session() as session:
            stmt = (
                pg_insert(MemoryEntityLink)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["memory_id", "entity_id"])
            )
            await session.execute(stmt)

    async def memory_get_entity_links_for_memories(
        self,
        memory_ids: list[UUID],
    ) -> dict[UUID, list[dict]]:
        if not memory_ids:
            return {}
        async with get_session() as session:
            result = await session.execute(
                select(MemoryEntityLink).where(MemoryEntityLink.memory_id.in_(memory_ids))
            )
            links_by_memory: dict[UUID, list[dict]] = {}
            for link in result.scalars().all():
                links_by_memory.setdefault(link.memory_id, []).append(
                    {"entity_id": link.entity_id, "role": link.role}
                )
            return links_by_memory

    async def memory_get_memories_by_ids(
        self,
        memory_ids: list[UUID],
    ) -> dict[UUID, Memory]:
        """Fetch multiple memories by ID, returned as {id: Memory}."""
        if not memory_ids:
            return {}
        async with get_session() as session:
            stmt = select(Memory).where(
                Memory.id.in_(memory_ids),
                Memory.deleted_at.is_(None),
            )
            result = await session.execute(stmt)
            return {m.id: m for m in result.scalars().all()}

    # ══════════════════════════════════════════════════════════════════════
    #  ENTITIES
    # ══════════════════════════════════════════════════════════════════════

    # ------------------------------------------------------------------
    # Entity CRUD
    # ------------------------------------------------------------------

    async def entity_get_by_id(self, entity_id: UUID) -> Entity | None:
        async with get_session() as session:
            return await session.get(Entity, entity_id)

    async def entity_find_exact(
        self,
        tenant_id: str,
        entity_type: str,
        canonical_name: str,
        fleet_id: str | None = None,
    ) -> Entity | None:
        """Phase 1 entity resolution: exact match on tenant + fleet + type + name."""
        async with get_session() as session:
            stmt = select(Entity).where(
                Entity.tenant_id == tenant_id,
                Entity.entity_type == entity_type,
                Entity.canonical_name == canonical_name,
            )
            # ``is not None`` rather than truthy so an empty-string
            # ``fleet_id`` matches an empty-string column value instead
            # of silently routing to the IS NULL branch.
            if fleet_id is not None:
                stmt = stmt.where(Entity.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Entity.fleet_id.is_(None))

            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def entity_find_by_embedding_similarity(
        self,
        tenant_id: str,
        entity_type: str,
        name_embedding: list[float],
        fleet_id: str | None = None,
        limit: int = ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    ) -> list[tuple[Entity, float]]:
        """Phase 2 entity resolution: embedding cosine similarity.

        Returns list of (Entity, similarity_score) ordered by distance.
        """
        async with get_session() as session:
            distance = Entity.name_embedding.cosine_distance(name_embedding)
            similarity = (1.0 - distance).label("similarity")
            stmt = (
                select(Entity, similarity)
                .where(
                    Entity.tenant_id == tenant_id,
                    Entity.entity_type == entity_type,
                    Entity.name_embedding.isnot(None),
                )
                .order_by(distance)
                .limit(limit)
            )
            # ``is not None`` rather than truthy so an empty-string
            # ``fleet_id`` matches an empty-string column value instead
            # of silently routing to the IS NULL branch.
            if fleet_id is not None:
                stmt = stmt.where(Entity.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Entity.fleet_id.is_(None))

            result = await session.execute(stmt)
            return list(result.all())  # type: ignore[arg-type]

    async def entity_bulk_resolve(
        self,
        tenant_id: str,
        items: list[dict],
        threshold: float,
        candidate_limit: int = ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    ) -> list[dict | None]:
        """Bulk version of the two-phase resolution in entity_service.upsert_entity.

        Replicates the same precedence — Phase 1 exact match by
        ``(tenant_id, fleet_id, canonical_name, entity_type)``; Phase 2
        embedding cosine similarity (top-N by distance, first ≥ threshold
        wins) — but in one round-trip and one DB connection. Phase 1 is
        a single batched SELECT keyed by row tuple; Phase 2 issues one
        similarity SELECT per unresolved item with a non-null embedding,
        all sharing the same session.

        Each input item: ``{"input_idx": int, "fleet_id": str|None,
        "canonical_name": str, "entity_type": str, "name_embedding":
        list[float]|None}``. Returns a list aligned to input order where
        each element is either ``None`` (no match) or
        ``{"entity_id", "canonical_name", "attributes", "matched_by",
        "similarity"}`` — ``matched_by`` ∈ {"exact", "similarity"}.

        Threshold is required, not defaulted — the resolution rule lives
        in the core-api layer; the storage service is the executor.
        """
        if not items:
            return []

        out: list[dict | None] = [None] * len(items)

        async with get_read_session() as session:
            # Phase 1: one batched SELECT keyed by row tuple. We can't
            # use SQL VALUES/JOIN here because (canonical_name, entity_type,
            # fleet_id) includes a nullable column, so build an OR-of-ANDs
            # of the input tuples. Single round-trip, single plan.

            # Group items by their (canonical_name, entity_type, fleet_id)
            # so a duplicate tuple in the input batch only triggers one
            # comparison; map back to input idxs at the end.
            tuple_to_idxs: dict[tuple[str, str, str | None], list[int]] = {}
            for it in items:
                key = (
                    it["canonical_name"],
                    it["entity_type"],
                    it.get("fleet_id"),
                )
                tuple_to_idxs.setdefault(key, []).append(it["input_idx"])

            # SQLAlchemy ``tuple_(...).in_(...)`` doesn't honor NULL
            # equality, so split into the with-fleet and no-fleet halves.
            with_fleet = [k for k in tuple_to_idxs if k[2] is not None]
            no_fleet = [k for k in tuple_to_idxs if k[2] is None]

            exact_rows: list[Entity] = []
            if with_fleet:
                stmt = select(Entity).where(
                    Entity.tenant_id == tenant_id,
                    tuple_(Entity.canonical_name, Entity.entity_type, Entity.fleet_id).in_(with_fleet),
                )
                exact_rows.extend((await session.execute(stmt)).scalars().all())
            if no_fleet:
                stmt = select(Entity).where(
                    Entity.tenant_id == tenant_id,
                    Entity.fleet_id.is_(None),
                    tuple_(Entity.canonical_name, Entity.entity_type).in_([(k[0], k[1]) for k in no_fleet]),
                )
                exact_rows.extend((await session.execute(stmt)).scalars().all())

            matched_idxs: set[int] = set()
            for row in exact_rows:
                key = (row.canonical_name, row.entity_type, row.fleet_id)
                for idx in tuple_to_idxs.get(key, []):
                    out[idx] = {
                        "entity_id": str(row.id),
                        "canonical_name": row.canonical_name,
                        "attributes": row.attributes or {},
                        "matched_by": "exact",
                        "similarity": 1.0,
                    }
                    matched_idxs.add(idx)

            # Phase 2: per-unmatched-item similarity SELECT, all in this
            # session. N queries one HTTP — the win is HTTP-roundtrip
            # elimination, not query count. Items without a name_embedding
            # skip Phase 2 (mirrors ``entity_service.upsert_entity`` line 46).
            for it in items:
                idx = it["input_idx"]
                if idx in matched_idxs:
                    continue
                emb = it.get("name_embedding")
                if emb is None:
                    continue
                distance = Entity.name_embedding.cosine_distance(emb)
                sim_col = (1.0 - distance).label("similarity")
                stmt = (
                    select(Entity, sim_col)
                    .where(
                        Entity.tenant_id == tenant_id,
                        Entity.entity_type == it["entity_type"],
                        Entity.name_embedding.isnot(None),
                    )
                    .order_by(distance)
                    .limit(candidate_limit)
                )
                # Mirror Phase 1's None-vs-value semantics so an empty-
                # string ``fleet_id`` doesn't silently route to IS NULL.
                if it.get("fleet_id") is not None:
                    stmt = stmt.where(Entity.fleet_id == it["fleet_id"])
                else:
                    stmt = stmt.where(Entity.fleet_id.is_(None))

                rows = (await session.execute(stmt)).all()
                for entity, sim in rows:
                    if float(sim) >= threshold:
                        out[idx] = {
                            "entity_id": str(entity.id),
                            "canonical_name": entity.canonical_name,
                            "attributes": entity.attributes or {},
                            "matched_by": "similarity",
                            "similarity": float(sim),
                        }
                        break

        return out

    async def entity_bulk_upsert(self, items: list[dict]) -> list[dict]:
        """Apply many entity create / update operations in one round-trip.

        Each input item:
          - ``input_idx``: int (preserved in response)
          - ``action``: "create" | "update"
          - ``entity_id``: UUID (required when action="update")
          - ``tenant_id``, ``fleet_id``, ``entity_type``, ``canonical_name``,
            ``attributes`` (dict), ``name_embedding`` (list[float] | None)

        Returns aligned list: ``{"input_idx", "entity_id", "action"}``.
        ``action`` in the response reflects what actually happened:
        ``"created"`` (INSERT succeeded), ``"updated"`` (UPDATE matched),
        or ``"merged"`` (INSERT race lost → ON CONFLICT DO UPDATE picked
        up the prior row; same outcome as today's IntegrityError recovery
        in ``entity_add``).

        Caller pre-computed the merged attributes from ``bulk_resolve_entities``
        output — server side does not re-merge. Concurrent writers between
        resolve and upsert have the same lost-update window as today's
        serial path (find_exact → update_entity); see crystallizer
        cluster-locking notes for the full race story.
        """
        if not items:
            return []

        # Partition by action; updates and creates each use a per-item
        # session (for FK-error isolation — a constraint error on item
        # N must not roll back items 0..N-1). Per-row sessions for both
        # paths cost connection pool checkouts but stay within one HTTP
        # — same big win.
        results: list[dict | None] = [None] * len(items)

        updates = [it for it in items if it["action"] == "update"]
        creates = [it for it in items if it["action"] == "create"]

        # Per-item sessions so a constraint error on item N doesn't roll
        # back items 0..N-1. The HTTP-roundtrip win is what matters; per-
        # item session checkout cost is negligible.
        for item in updates:
            eid = item["entity_id"]
            if not isinstance(eid, UUID):
                eid = UUID(eid)
            values: dict[str, Any] = {
                "entity_type": item["entity_type"],
                "canonical_name": item["canonical_name"],
                "attributes": item["attributes"],
            }
            if item.get("name_embedding") is not None:
                values["name_embedding"] = item["name_embedding"]
            async with get_session() as session:
                # ``tenant_id`` in the WHERE so a cross-tenant ``entity_id``
                # (caller bug or hostile input) is treated as "missing"
                # rather than silently updating someone else's row.
                upd = await session.execute(
                    sql_update(Entity)
                    .where(Entity.id == eid, Entity.tenant_id == item["tenant_id"])
                    .values(**values)
                )
                # rowcount==0 means the entity_id no longer exists, was
                # deleted, or belongs to a different tenant. All three
                # surface as ``missing`` so the caller can disambiguate
                # from ``updated``.
                results[item["input_idx"]] = {
                    "input_idx": item["input_idx"],
                    "entity_id": str(eid),
                    "action": "missing" if (upd.rowcount or 0) == 0 else "updated",  # type: ignore[attr-defined]
                }

        for item in creates:
            # The natural-key unique index is functional (``lower(canonical_name)``,
            # ``COALESCE(fleet_id, '')``), which SQLAlchemy's ON CONFLICT helpers
            # can't target via the column list, so we use the read-then-write
            # shape with TOCTOU recovery. The recovery folds SELECT + UPDATE
            # into one writer session to guarantee read-your-writes against
            # the row we just collided with.

            # Step 1: was it already there before we tried? Determines the
            # response ``action`` field even when we win the insert race.
            existed_before = await self.entity_find_exact(
                tenant_id=item["tenant_id"],
                entity_type=item["entity_type"],
                canonical_name=item["canonical_name"],
                fleet_id=item.get("fleet_id"),
            )

            merge_values: dict[str, Any] = {"attributes": item["attributes"]}
            if item.get("name_embedding") is not None:
                merge_values["name_embedding"] = item["name_embedding"]

            if existed_before is not None:
                # Pre-existing → apply caller's merged attributes. If the
                # row got deleted between our SELECT and UPDATE (a narrow
                # but real window), ``entity_update`` returns None — surface
                # as "missing" rather than reporting a "merged" that didn't
                # actually happen.
                updated = await self.entity_update(existed_before.id, merge_values)
                results[item["input_idx"]] = {
                    "input_idx": item["input_idx"],
                    "entity_id": str(existed_before.id),
                    "action": "missing" if updated is None else "merged",
                }
                continue

            # Step 2: insert; on IntegrityError another writer raced us
            # between Step 1 and now. Recover by re-SELECT + UPDATE in
            # a single writer session — guarantees read-your-writes against
            # the row we just collided with, and avoids the prior
            # two-session window where the racing writer could DELETE
            # between our SELECT and our UPDATE.
            payload: dict[str, Any] = {
                "tenant_id": item["tenant_id"],
                "fleet_id": item.get("fleet_id"),
                "entity_type": item["entity_type"],
                "canonical_name": item["canonical_name"],
                "attributes": item["attributes"],
            }
            if item.get("name_embedding") is not None:
                payload["name_embedding"] = item["name_embedding"]

            try:
                async with get_session() as session:
                    new_entity = Entity(**payload)
                    session.add(new_entity)
                    await session.flush()
                    new_id = new_entity.id
                results[item["input_idx"]] = {
                    "input_idx": item["input_idx"],
                    "entity_id": str(new_id),
                    "action": "created",
                }
            except IntegrityError:
                # TOCTOU recovery — SELECT + UPDATE in one writer session.
                logger.info(
                    "Entity bulk-upsert race: '%s' created concurrently, re-selecting",
                    item["canonical_name"],
                )
                async with get_session() as session:
                    sel = select(Entity).where(
                        Entity.tenant_id == item["tenant_id"],
                        Entity.entity_type == item["entity_type"],
                        func.lower(Entity.canonical_name) == item["canonical_name"].lower(),
                    )
                    if item.get("fleet_id") is not None:
                        sel = sel.where(Entity.fleet_id == item["fleet_id"])
                    else:
                        sel = sel.where(Entity.fleet_id.is_(None))
                    racy_existing = (await session.execute(sel)).scalar_one_or_none()

                    if racy_existing is None:
                        # The row that conflicted with us was deleted in
                        # the microseconds between our IntegrityError and
                        # the recovery SELECT. Surface as "missing"; the
                        # caller can retry the whole flow if they care.
                        results[item["input_idx"]] = {
                            "input_idx": item["input_idx"],
                            "entity_id": None,
                            "action": "missing",
                        }
                    else:
                        # Defence-in-depth ``tenant_id`` guard on the
                        # recovery UPDATE — the SELECT above already
                        # filters by tenant, but pinning the UPDATE
                        # WHERE too keeps the invariant local to the
                        # write statement (a future refactor of the
                        # SELECT can't accidentally let a cross-tenant
                        # row slip through).
                        upd = await session.execute(
                            sql_update(Entity)
                            .where(
                                Entity.id == racy_existing.id,
                                Entity.tenant_id == item["tenant_id"],
                            )
                            .values(**merge_values)
                        )
                        # rowcount==0 here means the row was deleted
                        # between our SELECT and UPDATE inside the SAME
                        # session — vanishingly unlikely but report
                        # consistently as "missing".
                        results[item["input_idx"]] = {
                            "input_idx": item["input_idx"],
                            "entity_id": str(racy_existing.id),
                            "action": "missing" if (upd.rowcount or 0) == 0 else "merged",  # type: ignore[attr-defined]
                        }

        # All slots filled (we partitioned over all items); filter for mypy.
        return [r for r in results if r is not None]

    async def entity_list(
        self,
        tenant_id: str,
        *,
        fleet_id: str | None = None,
        entity_type: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Entity]:
        async with get_session() as session:
            stmt = select(Entity).where(Entity.tenant_id == tenant_id).offset(offset).limit(limit)
            if fleet_id:
                stmt = stmt.where(Entity.fleet_id == fleet_id)
            if entity_type:
                stmt = stmt.where(Entity.entity_type == entity_type)
            if search:
                stmt = stmt.where(Entity.canonical_name.ilike(f"%{search}%"))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def entity_add(self, data: dict) -> Entity:
        """Create new entity — handle race with concurrent extraction tasks.

        The uq_entities_tenant_type_name_fleet unique index rejects
        duplicates at INSERT time; on conflict we re-SELECT and merge.
        """
        from sqlalchemy.exc import IntegrityError

        async with get_session() as session:
            entity = Entity(**data)
            session.add(entity)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "Entity dedup race: '%s' already exists, re-selecting",
                    data.get("canonical_name"),
                )
                # Re-SELECT the entity that won the race
                result = await session.execute(
                    select(Entity).where(
                        Entity.tenant_id == data["tenant_id"],
                        Entity.entity_type == data["entity_type"],
                        func.lower(Entity.canonical_name) == data["canonical_name"].lower(),
                        Entity.fleet_id == data.get("fleet_id")
                        if data.get("fleet_id")
                        else Entity.fleet_id.is_(None),
                    )
                )
                entity = result.scalar_one_or_none()
                if entity is None:
                    raise ValueError(
                        f"Entity '{data.get('canonical_name')}' conflict but re-select returned nothing"
                    )
            return entity

    async def entity_update(self, entity_id: UUID, data: dict) -> Entity | None:
        """Update an existing entity by ID with the given fields."""
        async with get_session() as session:
            entity = await session.get(Entity, entity_id)
            if entity is None:
                return None
            for key, value in data.items():
                if hasattr(entity, key):
                    setattr(entity, key, value)
            await session.flush()
            return entity

    # ------------------------------------------------------------------
    # Entity FTS
    # ------------------------------------------------------------------

    async def entity_fts_search(
        self,
        tokens: list[str],
        tenant_id: str,
        fleet_ids: list[str] | None = None,
    ) -> list[UUID]:
        """Full-text search against the entity tsvector index.

        A7: ORs tokens (any-match) instead of ANDing (all-match). The
        AND default (``plainto_tsquery('english', " ".join(tokens))``)
        meant a query like ``Helios telescope`` AND'd → matched only
        entities containing BOTH terms, hiding ``Helios Robotics``
        from the entity-lookup short-circuit. Switching to OR matches
        any token; downstream graph expansion + memory linking +
        ``GRAPH_MAX_EXPANDED_ENTITIES`` cap are the precision filter.

        Empty token list → empty result (defensive: prior behaviour
        passed empty string to plainto_tsquery, which returned the
        empty tsquery and matched zero rows; explicit guard is
        clearer).
        """
        if not tokens:
            return []
        async with get_session() as session:
            # OR across tokens via one plainto_tsquery per token. Each
            # term passes through PG's ``english`` config (stem +
            # stopword), so we don't have to escape — plainto_tsquery
            # is the safe variant. Empty / all-stopword tokens degrade
            # to ``''::tsquery`` and contribute False to the OR, which
            # is correct.
            per_token = [Entity.search_vector.op("@@")(func.plainto_tsquery("english", t)) for t in tokens]
            stmt = select(Entity.id).where(
                Entity.tenant_id == tenant_id,
                or_(*per_token),
            )
            if fleet_ids:
                stmt = stmt.where(or_(Entity.fleet_id.in_(fleet_ids), Entity.fleet_id.is_(None)))
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    async def relation_find(
        self,
        tenant_id: str,
        from_entity_id: UUID,
        relation_type: str,
        to_entity_id: UUID,
        fleet_id: str | None = None,
    ) -> Relation | None:
        async with get_session() as session:
            stmt = select(Relation).where(
                Relation.tenant_id == tenant_id,
                Relation.from_entity_id == from_entity_id,
                Relation.relation_type == relation_type,
                Relation.to_entity_id == to_entity_id,
            )
            if fleet_id:
                stmt = stmt.where(Relation.fleet_id == fleet_id)
            else:
                stmt = stmt.where(Relation.fleet_id.is_(None))

            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def relation_add(self, data: dict) -> Relation:
        """Idempotent UPSERT keyed on the natural key
        ``(tenant_id, from_entity_id, relation_type, to_entity_id)``.

        Pre-fix this was a plain INSERT and silently drove the
        ``IntegrityError: duplicate key value violates unique constraint
        "uq_relations_natural_key"`` cluster that surfaced in
        ``loadtest-1777212094`` as 5xx storms in the entity-extraction
        path → cascaded into bulk_write 500s → driving the
        ``silent-create-bulk`` HIGH finding (rows committed but caller
        saw an error and retried). The caller in ``entity_service.py``
        already named itself ``upsert_relation`` and commented "Storage
        API handles upsert (create-or-update) internally" — the comment
        was aspirational; this method now actually delivers it.

        On conflict, refresh ``weight`` (latest write wins) and
        ``evidence_memory_id`` (latest non-NULL write wins; a caller
        that omits/NULLs the field does NOT wipe an existing evidence
        link). ``fleet_id`` is **first-writer-wins**: it is not part of
        the unique constraint and is intentionally NOT touched by the
        UPDATE clause. The returned ``Relation``'s ``fleet_id`` may
        therefore differ from ``data["fleet_id"]`` if the row was
        originally created by a different fleet — callers that surface
        the response to clients should treat the returned fleet_id as
        authoritative. The row id is preserved across upserts so
        callers reading by id still find the relation.
        """
        async with get_session() as session:
            insert_stmt = pg_insert(Relation).values(**data)
            upsert_stmt = insert_stmt.on_conflict_do_update(
                constraint="uq_relations_natural_key",
                set_={
                    "weight": insert_stmt.excluded.weight,
                    # COALESCE so a caller that omits ``evidence_memory_id``
                    # (or passes ``None``) does NOT wipe an existing evidence
                    # link — common in the entity-extraction path where a
                    # follow-up memory mentioning the same entities arrives
                    # without a fresh evidence pointer. Latest non-NULL wins.
                    "evidence_memory_id": func.coalesce(
                        insert_stmt.excluded.evidence_memory_id,
                        Relation.evidence_memory_id,
                    ),
                },
            )
            await session.execute(upsert_stmt)

            # Re-fetch through the session so the caller gets a fully
            # ORM-tracked ``Relation`` (matching the legacy ``session.add``
            # path's contract). ``RETURNING`` on a ``pg_insert + on_conflict``
            # statement yields a ``Row`` rather than a tracked instance,
            # which downstream serialisers (``orm_to_dict``) expect to be
            # an ORM object — re-querying keeps the contract.
            #
            # The four-column unique constraint guarantees at most one row per
            # natural key regardless of ``fleet_id``, so filtering on fleet_id
            # would crash with ``NoResultFound`` whenever the stored row's
            # fleet_id differs from the incoming call's (the upsert SET clause
            # intentionally does not touch fleet_id — first-writer wins on it).
            select_stmt = select(Relation).where(
                Relation.tenant_id == data["tenant_id"],
                Relation.from_entity_id == data["from_entity_id"],
                Relation.relation_type == data["relation_type"],
                Relation.to_entity_id == data["to_entity_id"],
            )
            result = await session.execute(select_stmt)
            return result.scalar_one()

    async def relation_list(
        self,
        tenant_id: str,
        *,
        fleet_id: str | None = None,
        include_null_fleet: bool = False,
    ) -> list[Relation]:
        async with get_session() as session:
            stmt = select(Relation).where(Relation.tenant_id == tenant_id)
            if fleet_id:
                if include_null_fleet:
                    stmt = stmt.where(or_(Relation.fleet_id == fleet_id, Relation.fleet_id.is_(None)))
                else:
                    stmt = stmt.where(Relation.fleet_id == fleet_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def relation_get_outgoing(
        self,
        entity_id: UUID,
        tenant_id: str,
    ) -> list[tuple[Relation, Entity]]:
        """Return outgoing relations with their target entities."""
        async with get_session() as session:
            stmt = (
                select(Relation, Entity)
                .join(Entity, Entity.id == Relation.to_entity_id)
                .where(
                    Relation.from_entity_id == entity_id,
                    Relation.tenant_id == tenant_id,
                )
            )
            result = await session.execute(stmt)
            return list(result.all())  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Graph expansion
    # ------------------------------------------------------------------

    async def entity_expand_graph(
        self,
        seed_entity_ids: list[UUID],
        tenant_id: str,
        fleet_id: str | None,
        max_hops: int = GRAPH_MAX_HOPS,
        use_union: bool = False,
    ) -> dict[UUID, tuple[int, float]]:
        """Traverse relations from seed entities up to max_hops.

        Returns {entity_id: (min_hop_distance, relation_weight)} for all
        reachable entities (including seeds at hop 0, weight 1.0).
        """
        async with get_session() as session:
            entity_hops: dict[UUID, tuple[int, float]] = dict.fromkeys(seed_entity_ids, (0, 1.0))
            frontier = set(seed_entity_ids)

            for hop in range(1, max_hops + 1):
                if not frontier:
                    break
                fwd = select(
                    Relation.to_entity_id,
                    Relation.relation_type,
                    Relation.weight,
                ).where(
                    Relation.tenant_id == tenant_id,
                    Relation.from_entity_id.in_(frontier),
                )
                rev = select(
                    Relation.from_entity_id,
                    Relation.relation_type,
                    Relation.weight,
                ).where(
                    Relation.tenant_id == tenant_id,
                    Relation.to_entity_id.in_(frontier),
                )
                if fleet_id:
                    fwd = fwd.where(or_(Relation.fleet_id == fleet_id, Relation.fleet_id.is_(None)))
                    rev = rev.where(or_(Relation.fleet_id == fleet_id, Relation.fleet_id.is_(None)))

                if use_union:
                    combined = fwd.union_all(rev)
                    result = await session.execute(combined)
                    all_rows = result.all()
                else:
                    fwd_result = await session.execute(fwd)
                    rev_result = await session.execute(rev)
                    all_rows = (*fwd_result.all(), *rev_result.all())

                neighbor_weights: dict[UUID, float] = {}
                for eid, rel_type, row_w in all_rows:
                    w = _relation_weight(rel_type, row_w)
                    if eid not in neighbor_weights or w > neighbor_weights[eid]:
                        neighbor_weights[eid] = w

                for eid, w in neighbor_weights.items():
                    if eid not in entity_hops:
                        entity_hops[eid] = (hop, w)
                frontier = neighbor_weights.keys() - {eid for eid in entity_hops if entity_hops[eid][0] < hop}

            return entity_hops

    async def entity_get_full_graph(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> tuple[list[Entity], list[Relation]]:
        """Return all entities and relations for a tenant (optionally filtered by fleet).

        Skips the heavy ``name_embedding`` (pgvector) and ``search_vector`` (TSVECTOR)
        columns — the graph view doesn't need them, and loading + serialising them
        dominates the response time for tenants with many entities.
        """
        async with get_session() as session:
            entity_stmt = (
                select(Entity)
                .options(
                    load_only(
                        Entity.id,
                        Entity.tenant_id,
                        Entity.fleet_id,
                        Entity.entity_type,
                        Entity.canonical_name,
                        Entity.attributes,
                    )
                )
                .where(Entity.tenant_id == tenant_id)
            )
            if fleet_id:
                entity_stmt = entity_stmt.where(or_(Entity.fleet_id == fleet_id, Entity.fleet_id.is_(None)))
            entities_result = await session.execute(entity_stmt)
            entities = list(entities_result.scalars().all())

            relation_stmt = select(Relation).where(Relation.tenant_id == tenant_id)
            if fleet_id:
                relation_stmt = relation_stmt.where(
                    or_(Relation.fleet_id == fleet_id, Relation.fleet_id.is_(None))
                )
            relations_result = await session.execute(relation_stmt)
            relations = list(relations_result.scalars().all())

            return entities, relations

    # ------------------------------------------------------------------
    # Memory-entity links (entity side)
    # ------------------------------------------------------------------

    async def entity_count_memories_per_entity(
        self,
        entity_ids: list[UUID],
    ) -> dict[UUID, int]:
        """Return {entity_id: count} for the given entity IDs."""
        if not entity_ids:
            return {}
        async with get_session() as session:
            result = await session.execute(
                select(MemoryEntityLink.entity_id, func.count())
                .where(MemoryEntityLink.entity_id.in_(entity_ids))
                .group_by(MemoryEntityLink.entity_id)
            )
            return dict(result.all())  # type: ignore[arg-type]

    async def entity_get_linked_memories(
        self,
        entity_id: UUID,
        tenant_id: str,
    ) -> list[tuple]:
        """Return (MemoryEntityLink, Memory) rows for an entity, excluding deleted memories."""
        async with get_session() as session:
            stmt = (
                select(MemoryEntityLink, Memory)
                .join(Memory, Memory.id == MemoryEntityLink.memory_id)
                .where(
                    MemoryEntityLink.entity_id == entity_id,
                    Memory.deleted_at.is_(None),
                    Memory.tenant_id == tenant_id,
                )
            )
            result = await session.execute(stmt)
            return list(result.all())  # type: ignore[arg-type]

    async def entity_get_entity_links_for_memories(
        self,
        memory_ids: list[UUID],
    ) -> list[MemoryEntityLink]:
        """Return all MemoryEntityLink rows for the given memory IDs."""
        if not memory_ids:
            return []
        async with get_session() as session:
            result = await session.execute(
                select(MemoryEntityLink).where(MemoryEntityLink.memory_id.in_(memory_ids))
            )
            return list(result.scalars().all())

    async def entity_get_memory_ids_by_entity_ids(
        self,
        entity_ids: list[UUID],
    ) -> list[tuple[UUID, UUID, str]]:
        """Return (memory_id, entity_id, role) tuples for the given entity IDs."""
        if not entity_ids:
            return []
        async with get_session() as session:
            stmt = select(
                MemoryEntityLink.memory_id,
                MemoryEntityLink.entity_id,
                MemoryEntityLink.role,
            ).where(MemoryEntityLink.entity_id.in_(entity_ids))
            result = await session.execute(stmt)
            return list(result.all())  # type: ignore[arg-type]

    async def entity_find_entity_link(
        self,
        memory_id: UUID,
        entity_id: UUID,
    ) -> MemoryEntityLink | None:
        async with get_session() as session:
            result = await session.execute(
                select(MemoryEntityLink).where(
                    MemoryEntityLink.memory_id == memory_id,
                    MemoryEntityLink.entity_id == entity_id,
                )
            )
            return result.scalar_one_or_none()

    async def entity_add_entity_link(self, data: dict) -> MemoryEntityLink:
        async with get_session() as session:
            link = MemoryEntityLink(**data)
            session.add(link)
            await session.flush()
            return link

    async def entity_bulk_upsert_links(self, items: list[dict]) -> list[dict]:
        """Idempotently create many memory→entity links in one statement.

        Each input item: ``{"input_idx", "memory_id", "entity_id", "role"}``.
        Returns aligned list with ``{"input_idx", "memory_id", "entity_id",
        "role", "created": bool}``. ``created=False`` means a row with the
        same composite PK ``(memory_id, entity_id)`` already existed;
        its ``role`` is preserved (mirrors today's ``find_entity_link``
        → ``create_entity_link`` flow which skips on a hit).

        Cap enforced at the router level.
        """
        if not items:
            return []

        # Composite PK is (memory_id, entity_id); ``role`` is not part of
        # the unique key. INSERT ... ON CONFLICT DO UPDATE with the
        # no-op SET (``role = memory_entity_links.role``) is the standard
        # trick that lets RETURNING fire on both branches so we can
        # detect insert-vs-existed via the ``xmax`` system column.
        #
        # Per-item sessions: an FK violation (memory_id or entity_id
        # pointing at a deleted/nonexistent row) on item N would
        # otherwise roll back items 0..N-1 in a shared transaction.
        # Keyed by ``input_idx`` rather than (mid, eid) so a caller
        # accidentally sending the same pair twice doesn't lose the
        # second slot's result to map overwrite.
        idx_to_result: dict[int, dict[str, Any]] = {}

        for it in items:
            mid = it["memory_id"]
            eid = it["entity_id"]
            if not isinstance(mid, UUID):
                mid = UUID(mid)
            if not isinstance(eid, UUID):
                eid = UUID(eid)
            ins_stmt = (
                pg_insert(MemoryEntityLink)
                .values(memory_id=mid, entity_id=eid, role=it["role"])
                .on_conflict_do_update(
                    index_elements=[
                        MemoryEntityLink.memory_id,
                        MemoryEntityLink.entity_id,
                    ],
                    set_={"role": MemoryEntityLink.role},
                )
                .returning(
                    MemoryEntityLink.memory_id,
                    MemoryEntityLink.entity_id,
                    MemoryEntityLink.role,
                    literal_column("xmax"),
                )
            )
            try:
                async with get_session() as session:
                    row = (await session.execute(ins_stmt)).one()
                # xmax=0 ⇒ INSERT inserted; non-zero ⇒ existing row hit
                # by the DO UPDATE no-op.
                idx_to_result[it["input_idx"]] = {
                    "input_idx": it["input_idx"],
                    "memory_id": str(mid),
                    "entity_id": str(eid),
                    "role": row[2],
                    "created": int(row[3]) == 0,
                }
            except IntegrityError:
                # FK violation on memory_id or entity_id — report per-row
                # so the caller can continue processing the other links
                # rather than losing the whole batch.
                logger.warning(
                    "Entity link bulk-upsert FK violation: memory_id=%s entity_id=%s",
                    mid,
                    eid,
                )
                idx_to_result[it["input_idx"]] = {
                    "input_idx": it["input_idx"],
                    "memory_id": str(mid),
                    "entity_id": str(eid),
                    "role": it["role"],
                    "created": False,
                    "error": "fk_violation",
                }

        return [idx_to_result[it["input_idx"]] for it in items]

    async def entity_delete_entity_links(
        self,
        links_data: list[dict],
    ) -> None:
        """Delete entity links by (memory_id, entity_id) pairs."""
        async with get_session() as session:
            for ld in links_data:
                link = await session.execute(
                    select(MemoryEntityLink).where(
                        MemoryEntityLink.memory_id == ld["memory_id"],
                        MemoryEntityLink.entity_id == ld["entity_id"],
                    )
                )
                obj = link.scalar_one_or_none()
                if obj is not None:
                    await session.delete(obj)

    # ------------------------------------------------------------------
    # Crystallizer helpers (entity)
    # ------------------------------------------------------------------

    async def entity_find_orphaned(
        self,
        tenant_id: str,
        fleet_id: str | None,
        limit: int = 100,
    ) -> list[tuple]:
        """Entities with zero memory_entity_links. Returns (id, canonical_name) tuples."""
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id, table="e")
            result = await session.execute(
                text(f"""
                SELECT e.id, e.canonical_name
                FROM entities e
                LEFT JOIN memory_entity_links mel ON mel.entity_id = e.id
                WHERE {scope}
                  AND mel.entity_id IS NULL
                LIMIT :lim
            """),
                {**params, "lim": limit},
            )
            return list(result.all())  # type: ignore[arg-type]

    async def entity_find_broken_links(
        self,
        tenant_id: str,
        fleet_id: str | None,
        limit: int = 100,
    ) -> list[tuple]:
        """Entity links pointing to soft-deleted memories. Returns (memory_id, entity_id) tuples."""
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)
            result = await session.execute(
                text(f"""
                SELECT mel.memory_id, mel.entity_id
                FROM memory_entity_links mel
                JOIN memories m ON m.id = mel.memory_id
                WHERE {scope}
                  AND m.deleted_at IS NOT NULL
                LIMIT :lim
            """),
                {**params, "lim": limit},
            )
            return list(result.all())  # type: ignore[arg-type]

    # ══════════════════════════════════════════════════════════════════════
    #  AGENTS
    # ══════════════════════════════════════════════════════════════════════

    async def agent_get_by_id(
        self,
        agent_id: str,
        tenant_id: str,
    ) -> Agent | None:
        async with get_session() as session:
            result = await session.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.agent_id == agent_id,
                )
            )
            return result.scalar_one_or_none()

    async def agent_list_by_tenant(
        self,
        tenant_id: str,
    ) -> list[Agent]:
        async with get_session() as session:
            result = await session.execute(
                select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.created_at.desc())
            )
            return list(result.scalars().all())

    async def agent_add(self, data: dict) -> Agent:
        """Create new agent — handle race with concurrent registrations.

        Uses ``INSERT ... ON CONFLICT (tenant_id, agent_id) DO NOTHING
        RETURNING ...`` paired with a same-session re-SELECT for the
        conflicted case. The same shape ``memory_add_all`` uses for
        per-attempt idempotency (caura-memclaw#23): it avoids the
        ``flush() → IntegrityError → rollback() → re-SELECT`` pattern's
        mid-session rollback, which is brittle (the rollback aborts any
        other pending writes in the same session) and forced
        ``test_concurrent_same_key_returns_same_id`` to pre-create the
        agent row to dodge the failure mode.
        """
        async with get_session() as session:
            stmt = (
                pg_insert(Agent)
                .values(**data)
                .on_conflict_do_nothing(index_elements=["tenant_id", "agent_id"])
                .returning(Agent.id)
            )
            inserted_id = (await session.execute(stmt)).scalar_one_or_none()

            if inserted_id is not None:
                # New row — fetch the full ORM object for the return.
                # ``scalar_one_or_none`` (not ``scalar_one``) defends
                # against a concurrent delete between INSERT RETURNING
                # and the re-SELECT: ``scalar_one`` would raise
                # ``NoResultFound`` and surface as a 500 with no
                # actionable detail. We just-INSERTED this row in the
                # same session so the realistic race window is
                # vanishingly small, but cheap to be loud about it.
                result = await session.execute(select(Agent).where(Agent.id == inserted_id))
                agent = result.scalar_one_or_none()
                if agent is None:
                    raise ValueError(
                        f"Agent row {inserted_id} vanished after INSERT — concurrent delete during agent_add"
                    )
                return agent

            # Conflict: another caller (or a prior attempt) already
            # created the row. Re-SELECT and apply any new fields the
            # caller supplied (e.g. ``fleet_id`` backfill from a write
            # that learned the fleet after the agent existed, or the
            # heartbeat-refreshed ``display_name`` / first-contact
            # ``install_id`` introduced by the agent identity split).
            #
            # ``.with_for_update()`` on the re-SELECT serialises against
            # an in-progress concurrent ``agent_delete`` so we either
            # see the live row or wait until that delete commits — if
            # it does commit before we read, we still raise the
            # ``ValueError`` below (the row genuinely vanished), but
            # the lock removes the gap where the row was visible during
            # ``ON CONFLICT`` and gone here.
            logger.info(
                "Agent dedup race: '%s/%s' already exists, re-selecting",
                data.get("tenant_id"),
                data.get("agent_id"),
            )
            result = await session.execute(
                select(Agent)
                .where(
                    Agent.tenant_id == data["tenant_id"],
                    Agent.agent_id == data["agent_id"],
                )
                .with_for_update()
            )
            agent = result.scalar_one_or_none()
            if agent is None:
                # Conflict happened but the row vanished — concurrent
                # delete or schema drift. Surface as a clean ValueError
                # so the caller sees the inconsistent state rather than
                # an opaque ``None`` returned from a "create" call.
                raise ValueError(f"Agent '{data.get('agent_id')}' conflict but re-select returned nothing")
            # Track whether any field actually changed so we don't
            # bump ``updated_at`` (or burn an UPDATE roundtrip) when
            # the caller's data has nothing to backfill — e.g. a
            # plain idempotent re-register that just wants the
            # existing row back.
            changed = False
            for key in ("fleet_id", "trust_level", "display_name", "install_id"):
                if key in data and data[key] is not None and getattr(agent, key) != data[key]:
                    if key == "install_id" and getattr(agent, key) is not None:
                        # ``install_id`` is the per-OpenClaw-install opaque
                        # identity that disambiguates the default
                        # ``agent_id="main"`` across fleet machines. Once
                        # persisted it must be stable for the agent row's
                        # lifetime: backfill when previously NULL but never
                        # overwrite. ``agent_service.get_or_create_agent``
                        # already enforces this at the application layer;
                        # the guard is duplicated here so any future direct
                        # caller of ``agent_add`` (REST endpoint, admin
                        # tool) can't silently rewrite a stable identity.
                        # ``display_name`` and ``fleet_id`` intentionally
                        # overwrite on change (rename / reassignment).
                        continue
                    setattr(agent, key, data[key])
                    changed = True
            if changed:
                agent.updated_at = datetime.now(UTC)
                await session.flush()
            return agent

    async def agent_delete(self, agent_id: str, tenant_id: str) -> None:
        async with get_session() as session:
            result = await session.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.agent_id == agent_id,
                )
            )
            agent = result.scalar_one_or_none()
            if agent is not None:
                await session.delete(agent)

    async def agent_update_trust_level(
        self,
        agent_id: str,
        tenant_id: str,
        trust_level: int,
        fleet_id: str | None = None,
    ) -> None:
        async with get_session() as session:
            result = await session.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.agent_id == agent_id,
                )
            )
            agent = result.scalar_one_or_none()
            if agent is not None:
                agent.trust_level = trust_level
                if fleet_id is not None:
                    agent.fleet_id = fleet_id
                agent.updated_at = datetime.now(UTC)
                await session.flush()

    async def agent_update_fleet(
        self,
        agent_id: str,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        async with get_session() as session:
            result = await session.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.agent_id == agent_id,
                )
            )
            agent = result.scalar_one_or_none()
            if agent is not None:
                agent.fleet_id = fleet_id

    async def agent_update_search_profile(
        self,
        agent_id_pk: object,
        search_profile: dict,
    ) -> None:
        """Update an agent's search_profile by primary key (Agent.id)."""
        async with get_session() as session:
            await session.execute(
                sql_update(Agent).where(Agent.id == agent_id_pk).values(search_profile=search_profile)
            )

    async def agent_reset_search_profile(
        self,
        agent_id_pk: object,
    ) -> None:
        """Clear an agent's search_profile by primary key (Agent.id)."""
        async with get_session() as session:
            await session.execute(
                sql_update(Agent).where(Agent.id == agent_id_pk).values(search_profile=None)
            )

    async def agent_backfill_from_memories(self) -> int:
        """Create agent rows for (tenant_id, agent_id) pairs in memories
        that don't have an agent row yet."""
        async with get_session() as session:
            result = await session.execute(
                text("""
                INSERT INTO agents (tenant_id, agent_id, fleet_id, trust_level)
                SELECT DISTINCT ON (m.tenant_id, m.agent_id)
                       m.tenant_id, m.agent_id,
                       m.fleet_id,
                       1
                FROM memories m
                WHERE m.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agents a
                      WHERE a.tenant_id = m.tenant_id AND a.agent_id = m.agent_id
                  )
                ORDER BY m.tenant_id, m.agent_id, m.created_at ASC
                ON CONFLICT (tenant_id, agent_id) DO NOTHING
            """)
            )
            await session.flush()
            return result.rowcount  # type: ignore[attr-defined]

    # ══════════════════════════════════════════════════════════════════════
    #  DOCUMENTS
    # ══════════════════════════════════════════════════════════════════════

    async def document_upsert(
        self,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
        data: dict,
        fleet_id: str | None = None,
        system: bool = False,
    ) -> Document:
        """INSERT ... ON CONFLICT DO UPDATE. Returns the upserted Document.

        Collections whose name starts with ``_`` are system-managed
        (e.g. ``_keystones``); writes to them must pass ``system=True``.
        Public ``/documents`` endpoint never sets the flag, so callers
        that accidentally target a system collection get a clear
        ``ValueError`` instead of polluting governance state.
        """
        if collection.startswith("_") and not system:
            raise ValueError(f"Collection '{collection}' is system-managed; use the dedicated endpoint.")
        async with get_session() as session:
            stmt = (
                pg_insert(Document)
                .values(
                    tenant_id=tenant_id,
                    fleet_id=fleet_id,
                    collection=collection,
                    doc_id=doc_id,
                    data=data,
                )
                .on_conflict_do_update(
                    constraint="uq_documents_tenant_collection_doc",
                    set_={
                        "data": data,
                        "fleet_id": fleet_id,
                        "updated_at": datetime.now(UTC),
                    },
                )
                .returning(Document)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def document_upsert_returning_xmax(
        self,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
        data: dict,
        fleet_id: str | None = None,
    ) -> tuple:
        """Upsert and return (id, created_at, updated_at, xmax) for MCP callers."""
        async with get_session() as session:
            stmt = (
                pg_insert(Document)
                .values(
                    tenant_id=tenant_id,
                    fleet_id=fleet_id,
                    collection=collection,
                    doc_id=doc_id,
                    data=data,
                )
                .on_conflict_do_update(
                    constraint="uq_documents_tenant_collection_doc",
                    set_={"data": data, "fleet_id": fleet_id, "updated_at": text("now()")},
                )
                .returning(Document.id, Document.created_at, Document.updated_at, text("xmax"))
            )
            result = await session.execute(stmt)
            return result.one()  # type: ignore[return-value]

    async def document_get_by_doc_id(
        self,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
    ) -> Document | None:
        async with get_session() as session:
            stmt = select(Document).where(
                Document.tenant_id == tenant_id,
                Document.collection == collection,
                Document.doc_id == doc_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def document_query(
        self,
        *,
        tenant_id: str,
        collection: str,
        fleet_id: str | None = None,
        where: dict | None = None,
        order_by: str | None = None,
        order: str = "asc",
        limit: int = 20,
        offset: int = 0,
    ) -> list[Document]:
        """Query documents with optional JSONB field-equality filters."""
        async with get_session() as session:
            stmt = select(Document).where(
                Document.tenant_id == tenant_id,
                Document.collection == collection,
            )
            if fleet_id:
                stmt = stmt.where(Document.fleet_id == fleet_id)

            for key, value in (where or {}).items():
                if isinstance(value, bool):
                    stmt = stmt.where(Document.data[key].as_boolean() == value)
                elif isinstance(value, (int, float)):
                    stmt = stmt.where(Document.data[key].as_float() == value)
                else:
                    stmt = stmt.where(Document.data[key].astext == str(value))

            if order_by:
                col = Document.data[order_by].astext
                stmt = stmt.order_by(col.desc() if order == "desc" else col.asc())
            else:
                stmt = stmt.order_by(Document.updated_at.desc())

            stmt = stmt.offset(offset).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def document_list_by_collection(
        self,
        *,
        tenant_id: str,
        collection: str,
        fleet_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        async with get_session() as session:
            stmt = select(Document).where(
                Document.tenant_id == tenant_id,
                Document.collection == collection,
            )
            if fleet_id:
                stmt = stmt.where(Document.fleet_id == fleet_id)
            stmt = stmt.order_by(Document.updated_at.desc()).offset(offset).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def document_delete_by_doc_id(
        self,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
        system: bool = False,
    ) -> UUID | None:
        """Delete by (tenant_id, collection, doc_id). Returns the deleted id or None.

        Mirrors the ``system`` guard on ``document_upsert`` — deletes against
        system-managed collections (``_``-prefixed) require ``system=True``.
        """
        if collection.startswith("_") and not system:
            raise ValueError(f"Collection '{collection}' is system-managed; use the dedicated endpoint.")
        async with get_session() as session:
            stmt = (
                delete(Document)
                .where(
                    Document.tenant_id == tenant_id,
                    Document.collection == collection,
                    Document.doc_id == doc_id,
                )
                .returning(Document.id)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    # ══════════════════════════════════════════════════════════════════════
    #  FLEET
    # ══════════════════════════════════════════════════════════════════════

    # -- Fleet stats --

    async def fleet_agent_stats(
        self,
        tenant_id: str,
        fleet_id: str | None,
    ) -> dict:
        """Per-agent memory stats + fleet summary for the Fleet UI."""
        async with get_session() as session:
            scope, params = _scope_sql(tenant_id, fleet_id)
            # Per-agent stats from memories
            result = await session.execute(
                text(f"""
                SELECT m.agent_id,
                       COUNT(m.id)              AS total_memories,
                       MAX(m.created_at)        AS last_write_at,
                       COALESCE(SUM(m.recall_count), 0) AS total_recalls,
                       MAX(m.last_recalled_at)  AS last_recall_at
                FROM memories m
                WHERE {scope} AND m.deleted_at IS NULL
                GROUP BY m.agent_id
                ORDER BY COUNT(m.id) DESC
            """),
                params,
            )
            agent_rows = result.all()

            trust_result = await session.execute(
                text("SELECT agent_id, trust_level FROM agents WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            trust_by_id: dict[str, int] = {row.agent_id: row.trust_level for row in trust_result.all()}

            now = datetime.now(UTC)
            day_ago = now - timedelta(days=1)
            week_ago = now - timedelta(days=7)

            active_24h: set[str] = set()
            agents: list[dict] = []
            for r in agent_rows:
                last_write = r.last_write_at.replace(tzinfo=UTC) if r.last_write_at else None
                last_recall = r.last_recall_at.replace(tzinfo=UTC) if r.last_recall_at else None
                if last_write and last_write > day_ago:
                    active_24h.add(r.agent_id)
                agents.append(
                    {
                        "agent_id": r.agent_id,
                        "trust_level": trust_by_id.get(r.agent_id, 1),
                        "total_memories": r.total_memories,
                        "last_write_at": last_write.isoformat() if last_write else None,
                        "total_recalls": int(r.total_recalls),
                        "last_recall_at": last_recall.isoformat() if last_recall else None,
                        "active_24h": r.agent_id in active_24h,
                        "stale": last_write is not None and last_write < week_ago,
                    }
                )

            # Memory totals + status breakdown (single query, one table scan).
            # ``deleted_at IS NULL`` keeps live rows separate from soft-deleted
            # rows; the soft-deleted count is computed on its own below.
            result_totals = await session.execute(
                text(f"""
                SELECT
                    COUNT(*) FILTER (WHERE m.deleted_at IS NULL)                                      AS total_memories,
                    COUNT(*) FILTER (WHERE m.deleted_at IS NULL AND m.status = 'conflicted')          AS conflicted_memories,
                    COUNT(*) FILTER (WHERE m.deleted_at IS NULL AND m.status = 'outdated')            AS outdated_memories,
                    COUNT(*) FILTER (WHERE m.deleted_at IS NOT NULL)                                  AS deleted_memories,
                    COUNT(*) FILTER (WHERE m.deleted_at IS NULL AND m.created_at > :day_ago)          AS memories_24h,
                    COUNT(*) FILTER (WHERE m.deleted_at IS NULL AND m.last_recalled_at > :day_ago)    AS recalled_memories_24h
                FROM memories m
                WHERE {scope}
            """),
                {**params, "day_ago": day_ago},
            )
            totals = result_totals.one()
            memories_24h = int(totals.memories_24h or 0)

            # Agents from fleet nodes (may have agents with no memories)
            node_scope, node_params = _scope_sql(tenant_id, fleet_id, table="fn")
            result_nodes = await session.execute(
                text(f"""
                SELECT fn.agents_json FROM fleet_nodes fn
                WHERE {node_scope} AND fn.node_name NOT LIKE '\\_fleet\\_%'
            """),
                node_params,
            )
            known_agent_ids = {a["agent_id"] for a in agents}
            for (agents_json,) in result_nodes.all():
                if not agents_json or not isinstance(agents_json, list):
                    continue
                for a in agents_json:
                    aid = a.get("agentId") or a.get("name") if isinstance(a, dict) else str(a)
                    if aid and aid not in known_agent_ids:
                        known_agent_ids.add(aid)
                        agents.append(
                            {
                                "agent_id": aid,
                                "trust_level": trust_by_id.get(aid, 1),
                                "total_memories": 0,
                                "last_write_at": None,
                                "total_recalls": 0,
                                "last_recall_at": None,
                                "active_24h": False,
                                "stale": False,
                            }
                        )

            return {
                "agents": agents,
                "fleet_summary": {
                    "total_agents": len(agents),
                    "active_agents_24h": len(active_24h),
                    "memories_24h": memories_24h,
                    "stale_agents": sum(1 for a in agents if a["stale"]),
                    "total_memories": int(totals.total_memories or 0),
                    "conflicted_memories": int(totals.conflicted_memories or 0),
                    "outdated_memories": int(totals.outdated_memories or 0),
                    "deleted_memories": int(totals.deleted_memories or 0),
                    "recalled_memories_24h": int(totals.recalled_memories_24h or 0),
                },
            }

    # -- Fleet CRUD --

    async def fleet_exists(
        self,
        *,
        tenant_id: str,
        fleet_id: str,
    ) -> bool:
        async with get_session() as session:
            result = await session.execute(
                select(FleetNode.id)
                .where(
                    FleetNode.tenant_id == tenant_id,
                    FleetNode.fleet_id == fleet_id,
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def fleet_list(
        self,
        *,
        tenant_id: str,
    ) -> Sequence[Any]:
        """Return rows of (fleet_id, node_count, last_heartbeat)."""
        async with get_session() as session:
            result = await session.execute(
                select(
                    FleetNode.fleet_id,
                    func.sum(
                        case(
                            (~FleetNode.node_name.startswith("_fleet_"), 1),
                            else_=0,
                        )
                    ).label("node_count"),
                    func.max(FleetNode.last_heartbeat).label("last_heartbeat"),
                )
                .where(
                    FleetNode.tenant_id == tenant_id,
                    FleetNode.fleet_id.isnot(None),
                )
                .group_by(FleetNode.fleet_id)
                .order_by(FleetNode.fleet_id)
            )
            return result.all()

    async def fleet_delete(
        self,
        *,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """Delete all nodes (and their commands) for a fleet."""
        async with get_session() as session:
            # Get node IDs first
            result = await session.execute(
                select(FleetNode.id).where(
                    FleetNode.tenant_id == tenant_id,
                    FleetNode.fleet_id == fleet_id,
                )
            )
            node_ids = list(result.scalars().all())

            if node_ids:
                await session.execute(
                    FleetCommand.__table__.delete().where(FleetCommand.node_id.in_(node_ids))
                )

            await session.execute(
                FleetNode.__table__.delete().where(
                    FleetNode.tenant_id == tenant_id,
                    FleetNode.fleet_id == fleet_id,
                )
            )

    # -- Nodes --

    async def fleet_upsert_node(
        self,
        *,
        values: dict[str, Any],
    ) -> UUID:
        async with get_session() as session:
            stmt = pg_insert(FleetNode.__table__).values(**values)
            stmt = stmt.on_conflict_do_update(  # type: ignore[assignment]
                constraint="uq_fleet_nodes_tenant_node",
                set_={k: v for k, v in values.items() if k not in ("tenant_id", "node_name")},
            ).returning(FleetNode.__table__.c.id)
            result = await session.execute(stmt)
            await session.flush()
            return result.scalar_one()

    async def fleet_add_node(self, data: dict) -> FleetNode:
        async with get_session() as session:
            node = FleetNode(**data)
            session.add(node)
            await session.flush()
            return node

    async def fleet_get_node_id(
        self,
        *,
        tenant_id: str,
        node_name: str,
    ) -> UUID:
        async with get_session() as session:
            result = await session.execute(
                select(FleetNode.id).where(
                    FleetNode.tenant_id == tenant_id,
                    FleetNode.node_name == node_name,
                )
            )
            return result.scalar_one()

    async def fleet_get_node_by_id(
        self,
        *,
        node_id: UUID,
    ) -> FleetNode | None:
        async with get_session() as session:
            return await session.get(FleetNode, node_id)

    async def fleet_list_nodes(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> Sequence[FleetNode]:
        async with get_session() as session:
            query = select(FleetNode).where(FleetNode.tenant_id == tenant_id)
            if fleet_id:
                query = query.where(FleetNode.fleet_id == fleet_id)
            result = await session.execute(query.order_by(FleetNode.last_heartbeat.desc()))
            return result.scalars().all()

    async def fleet_count_nodes(
        self,
        *,
        tenant_id: str,
        fleet_id: str,
    ) -> int:
        async with get_session() as session:
            result = await session.execute(
                select(func.count(FleetNode.id)).where(
                    FleetNode.tenant_id == tenant_id,
                    FleetNode.fleet_id == fleet_id,
                )
            )
            return result.scalar() or 0

    async def fleet_get_node_ids_for_fleet(
        self,
        *,
        tenant_id: str,
        fleet_id: str,
    ) -> list[UUID]:
        async with get_session() as session:
            result = await session.execute(
                select(FleetNode.id).where(
                    FleetNode.tenant_id == tenant_id,
                    FleetNode.fleet_id == fleet_id,
                )
            )
            return list(result.scalars().all())

    # -- Commands --

    async def fleet_get_command_by_id(
        self,
        *,
        command_id: UUID,
    ) -> FleetCommand | None:
        async with get_session() as session:
            return await session.get(FleetCommand, command_id)

    async def fleet_get_pending_commands(
        self,
        *,
        node_id: UUID,
    ) -> Sequence[FleetCommand]:
        async with get_session() as session:
            result = await session.execute(
                select(FleetCommand)
                .where(
                    FleetCommand.node_id == node_id,
                    FleetCommand.status == "pending",
                )
                .order_by(FleetCommand.created_at)
            )
            return result.scalars().all()

    async def fleet_ack_commands(
        self,
        *,
        command_ids: list[UUID],
        now: datetime,
    ) -> None:
        if not command_ids:
            return
        async with get_session() as session:
            await session.execute(
                sql_update(FleetCommand)
                .where(FleetCommand.id.in_(command_ids))
                .values(status="acked", acked_at=now)
            )

    async def fleet_add_command(self, data: dict) -> FleetCommand:
        async with get_session() as session:
            command = FleetCommand(**self._filter_fields(FleetCommand, data))
            session.add(command)
            await session.flush()
            return command

    async def fleet_list_commands(
        self,
        *,
        tenant_id: str,
        node_id: UUID | None = None,
        limit: int = 50,
    ) -> Sequence[FleetCommand]:
        async with get_session() as session:
            stmt = (
                select(FleetCommand)
                .where(FleetCommand.tenant_id == tenant_id)
                .order_by(FleetCommand.created_at.desc())
                .limit(limit)
            )
            if node_id:
                stmt = stmt.where(FleetCommand.node_id == node_id)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def fleet_delete_commands_for_nodes(
        self,
        *,
        node_ids: list[UUID],
    ) -> None:
        if not node_ids:
            return
        async with get_session() as session:
            await session.execute(FleetCommand.__table__.delete().where(FleetCommand.node_id.in_(node_ids)))

    # ══════════════════════════════════════════════════════════════════════
    #  AUDIT
    # ══════════════════════════════════════════════════════════════════════

    async def audit_add(
        self,
        *,
        tenant_id: str,
        agent_id: str | None = None,
        action: str,
        resource_type: str,
        resource_id: UUID | None = None,
        detail: dict | None = None,
    ) -> None:
        async with get_session() as session:
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    detail=detail,
                )
            )

    async def audit_add_batch(self, events: list[dict]) -> None:
        """Persist N audit events in one transaction with one INSERT
        statement (CAURA-628).

        ``session.add_all`` issues a single multi-row INSERT to
        Postgres, vs ``audit_add`` which opens one transaction +
        acquires the table write lock once per event. Reducing N
        per-event lock acquisitions to one batched acquisition is the
        whole point of the CAURA-628 refactor; the per-event legacy
        path is preserved for the synchronous-fallback case in
        core-api's ``log_action``.

        Empty ``events`` short-circuits without touching the
        database — saves a no-op session open under the audit
        flusher's interval-driven empty ticks.
        """
        if not events:
            return
        async with get_session() as session:
            session.add_all(
                AuditLog(
                    tenant_id=event["tenant_id"],
                    agent_id=event.get("agent_id"),
                    action=event["action"],
                    resource_type=event["resource_type"],
                    resource_id=event.get("resource_id"),
                    detail=event.get("detail"),
                )
                for event in events
            )

    async def audit_list_by_tenant(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[AuditLog]:
        async with get_session() as session:
            q = (
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
            if since:
                q = q.where(AuditLog.created_at > since)
            result = await session.execute(q)
            return list(result.scalars().all())

    # ══════════════════════════════════════════════════════════════════════
    #  LIFECYCLE AUDIT (CAURA-655)
    # ══════════════════════════════════════════════════════════════════════

    async def lifecycle_audit_create(
        self,
        *,
        org_id: str,
        action: str,
        triggered_by: str,
    ) -> int:
        """Insert a ``status='pending'`` row and return its id.

        Called by the core-api fanout endpoint just before publishing
        each per-org Pub/Sub message — the id rides along in the
        envelope so the consumer can finalise the same row on
        completion.
        """
        async with get_session() as session:
            row = LifecycleAudit(
                org_id=org_id,
                action=action,
                triggered_by=triggered_by,
            )
            session.add(row)
            await session.flush()
            return row.id

    async def lifecycle_audit_finalize(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> bool | None:
        """Set terminal-or-progress state on the row.

        Tri-state return distinguishes the two ``rowcount==0`` cases:
        * ``True``  — row updated.
        * ``None``  — row exists but is already at ``status='success'``
          (the sticky-success gate skipped the UPDATE). A no-op, NOT
          an error — typically a Pub/Sub redelivery of an already-
          acked successful message.
        * ``False`` — row not found at all (pruned, or a buggy
          publisher invented an id).

        ``finished_at`` is only stamped on terminal status values so the
        ``in_progress`` transition leaves the row addressable for a
        later success/failure update.
        """
        values: dict = {"status": status}
        if status in ("success", "failure"):
            values["finished_at"] = func.now()
        elif status == "in_progress":
            # Pub/Sub redelivery after a prior ``failure`` re-enters this
            # method with status="in_progress"; without the explicit NULL
            # the row would carry the previous attempt's ``finished_at``,
            # and any query using ``finished_at IS NOT NULL`` to find
            # completed rows would misclassify the retrying row.
            values["finished_at"] = None
        if stats is not None:
            values["stats"] = stats
        if error_message is not None:
            values["error_message"] = error_message
        if status == "success":
            # A redelivery that recovered from a prior ``failure`` must
            # not leave the failure's ``error_message`` lingering on a
            # now-successful row.
            values["error_message"] = None
        async with get_session() as session:
            # ``success`` is sticky — once a row reaches it, no
            # subsequent transition (Pub/Sub redelivery of an already-
            # acked-but-late-acked message) can downgrade or overwrite
            # it. Without this, a redelivery would re-enter as
            # ``in_progress`` → re-run the (idempotent) archive
            # primitive that returns 0 → clobber the original
            # ``stats.archived`` count with 0. Failure recovery
            # (failure → in_progress → success) still works because
            # ``failure`` is NOT gated.
            stmt = (
                sql_update(LifecycleAudit)
                .where(LifecycleAudit.id == audit_id)
                .where(LifecycleAudit.status != "success")
                .values(**values)
            )
            result = await session.execute(stmt)
            if result.rowcount > 0:  # type: ignore[attr-defined]
                return True
            # rowcount==0 has two causes — disambiguate so the router
            # can return 200 for the no-op (already-success) path
            # instead of a misleading 404 that would surface as a
            # spurious "audit row not found" warning on every Pub/Sub
            # redelivery of an acked-but-late-acked successful message.
            exists = await session.scalar(select(LifecycleAudit.id).where(LifecycleAudit.id == audit_id))
            return None if exists else False

    async def lifecycle_audit_has_recent_success(
        self,
        *,
        org_id: str,
        action: str,
        since_hours: int,
    ) -> bool:
        """CAURA-657 dedup gate: did this org+action succeed within the
        last ``since_hours``? Used by the pipeline-op consumers to
        skip a redundant run when the cron tick double-fires (deploy
        + immediate redeploy, manual re-trigger after a recent
        successful run, etc.).

        Filters on ``finished_at`` rather than ``started_at`` so an
        in-progress row from the current attempt — pre-published by
        the fanout endpoint just moments ago — is naturally excluded
        (its ``finished_at`` is still NULL).
        """
        async with get_read_session() as session:
            row = await session.execute(
                select(LifecycleAudit.id)
                .where(LifecycleAudit.org_id == org_id)
                .where(LifecycleAudit.action == action)
                .where(LifecycleAudit.status == "success")
                .where(LifecycleAudit.finished_at > func.now() - timedelta(hours=since_hours))
                .limit(1)
            )
            return row.scalar_one_or_none() is not None

    # ══════════════════════════════════════════════════════════════════════
    #  REPORTS (CrystallizationReport)
    # ══════════════════════════════════════════════════════════════════════

    async def report_get_by_id(
        self,
        report_id: UUID,
    ) -> CrystallizationReport | None:
        async with get_session() as session:
            return await session.get(CrystallizationReport, report_id)

    async def report_find_running(
        self,
        tenant_id: str,
        fleet_id: str | None,
    ) -> UUID | None:
        async with get_session() as session:
            result = await session.execute(
                select(CrystallizationReport.id).where(
                    CrystallizationReport.tenant_id == tenant_id,
                    CrystallizationReport.fleet_id == fleet_id
                    if fleet_id
                    else CrystallizationReport.fleet_id.is_(None),
                    CrystallizationReport.status == "running",
                )
            )
            return result.scalar_one_or_none()

    async def report_add(self, data: dict) -> CrystallizationReport:
        async with get_session() as session:
            report = CrystallizationReport(**self._filter_fields(CrystallizationReport, data))
            session.add(report)
            await session.flush()
            return report

    async def report_update_completed(
        self,
        report_id: UUID,
        *,
        status: str,
        completed_at: datetime,
        duration_ms: int,
        summary: dict,
        hygiene: dict,
        health: dict,
        usage_data: dict,
        issues: list,
        crystallization: dict,
    ) -> None:
        async with get_session() as session:
            await session.execute(
                sql_update(CrystallizationReport)
                .where(CrystallizationReport.id == report_id)
                .values(
                    status=status,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    summary=summary,
                    hygiene=hygiene,
                    health=health,
                    usage_data=usage_data,
                    issues=issues,
                    crystallization=crystallization,
                )
            )

    async def report_list_by_tenant(
        self,
        tenant_id: str,
        limit: int = 10,
        offset: int = 0,
    ) -> list[CrystallizationReport]:
        async with get_session() as session:
            result = await session.execute(
                select(CrystallizationReport)
                .where(CrystallizationReport.tenant_id == tenant_id)
                .order_by(CrystallizationReport.started_at.desc())
                .offset(offset)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def report_get_latest_completed(
        self,
        tenant_id: str,
    ) -> CrystallizationReport | None:
        async with get_session() as session:
            result = await session.execute(
                select(CrystallizationReport)
                .where(
                    CrystallizationReport.tenant_id == tenant_id,
                    CrystallizationReport.status == "completed",
                )
                .order_by(CrystallizationReport.started_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    # ══════════════════════════════════════════════════════════════════════
    #  TASKS (BackgroundTaskLog)
    # ══════════════════════════════════════════════════════════════════════

    async def task_add_failure(
        self,
        *,
        task_name: str,
        memory_id: UUID | None = None,
        tenant_id: str,
        error_message: str,
        error_traceback: str,
    ) -> None:
        async with get_session() as session:
            session.add(
                BackgroundTaskLog(
                    task_name=task_name,
                    memory_id=memory_id,
                    tenant_id=tenant_id,
                    status="failed",
                    error_message=error_message[:1000],
                    error_traceback=error_traceback,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.flush()

    # ══════════════════════════════════════════════════════════════════════
    # Idempotency inbox
    # ══════════════════════════════════════════════════════════════════════

    async def idempotency_get(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> IdempotencyResponse | None:
        """Return the stored idempotency row if live, else None.

        Expired rows are treated as absent so callers transparently
        re-run the request after TTL. A separate cleanup job prunes
        them from storage.
        """
        async with get_session() as session:
            stmt = select(IdempotencyResponse).where(
                IdempotencyResponse.tenant_id == tenant_id,
                IdempotencyResponse.idempotency_key == idempotency_key,
                # Match ``func.now()`` used by ``idempotency_claim``'s
                # ON CONFLICT WHERE so the two paths agree on what
                # "expired" means even when the app and DB clocks drift.
                IdempotencyResponse.expires_at > func.now(),
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def idempotency_claim(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_hash: str,
        expires_at: datetime,
    ) -> IdempotencyResponse | None:
        """Atomically claim ``(tenant_id, idempotency_key)`` for a new
        request. Returns the freshly inserted (or reclaimed) row if the
        caller won the race, ``None`` if a *live* row already existed.

        Expired rows are reclaimed in place: the conflict triggers an
        UPDATE only when ``expires_at`` is in the past. Without that
        clause an expired-but-not-yet-pruned row would block fresh
        claims indefinitely — ``idempotency_get`` filters expired rows
        out, so the middleware would loop forever between "no cache"
        and "claim conflicts" until the cleanup job pruned the row.

        The claim row carries ``is_pending=True`` and an empty
        ``response_body``. :meth:`idempotency_record` flips
        ``is_pending`` to False once the handler completes.
        """
        async with get_session() as session:
            stmt = (
                pg_insert(IdempotencyResponse)
                .values(
                    tenant_id=tenant_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response_body={},
                    status_code=0,
                    expires_at=expires_at,
                    is_pending=True,
                )
                .on_conflict_do_update(
                    constraint="pk_idempotency_responses",
                    set_={
                        "request_hash": request_hash,
                        "response_body": {},
                        "status_code": 0,
                        "expires_at": expires_at,
                        "is_pending": True,
                    },
                    # ``func.now()`` evaluates against the DB clock, not the
                    # app clock — avoids a row being treated as still-live
                    # when the app and DB clocks have drifted.
                    where=IdempotencyResponse.expires_at <= func.now(),
                )
                .returning(IdempotencyResponse)
            )
            # ``ON CONFLICT DO UPDATE WHERE`` either inserts a fresh row,
            # reclaims an expired row, or returns nothing — the WHERE
            # blocks the UPDATE on a live row, so RETURNING yields no
            # rows. ``scalar_one_or_none`` cleanly maps that to None
            # (caller must poll for the original request's completion).
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def idempotency_record(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_hash: str,
        response_body: dict,
        status_code: int,
        expires_at: datetime,
    ) -> IdempotencyResponse:
        """Persist the handler's response and clear ``is_pending``.

        Optimistic about the prior :meth:`idempotency_claim` having
        inserted the row: this is an UPDATE-by-(tenant, key, hash) first,
        scoped to the still-pending claim. If no row exists (claim was
        skipped, e.g. legacy callers calling ``upsert`` directly), falls
        back to INSERT ON CONFLICT DO NOTHING and returns whichever row
        wins. The ``request_hash`` filter ensures a late-arriving record
        for an *expired* claim doesn't clobber a fresh claim that
        reclaimed the slot — its UPDATE will match no rows and the
        fallback INSERT will conflict against the new claim, returning
        the new row's state.
        """
        async with get_session() as session:
            update_stmt = (
                sql_update(IdempotencyResponse)
                .where(
                    IdempotencyResponse.tenant_id == tenant_id,
                    IdempotencyResponse.idempotency_key == idempotency_key,
                    IdempotencyResponse.request_hash == request_hash,
                    IdempotencyResponse.is_pending.is_(True),
                )
                .values(
                    response_body=response_body,
                    status_code=status_code,
                    expires_at=expires_at,
                    is_pending=False,
                )
                .returning(IdempotencyResponse)
            )
            result = await session.execute(update_stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                return row

            insert_stmt = (
                pg_insert(IdempotencyResponse)
                .values(
                    tenant_id=tenant_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response_body=response_body,
                    status_code=status_code,
                    expires_at=expires_at,
                    is_pending=False,
                )
                .on_conflict_do_nothing(constraint="pk_idempotency_responses")
                .returning(IdempotencyResponse)
            )
            result = await session.execute(insert_stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                return row

            existing = await session.execute(
                select(IdempotencyResponse).where(
                    IdempotencyResponse.tenant_id == tenant_id,
                    IdempotencyResponse.idempotency_key == idempotency_key,
                )
            )
            row = existing.scalar_one_or_none()
            if row is None:
                # The cleanup job pruned the conflicting row between our
                # INSERT (which got DO NOTHING because the row existed)
                # and this SELECT. Vanishingly rare; raising explicitly
                # gives a 500 with an actionable log line instead of a
                # cryptic NoResultFound traceback.
                raise RuntimeError(
                    f"idempotency row ({tenant_id!r}, {idempotency_key!r}) "
                    "vanished between INSERT conflict and SELECT — "
                    "likely pruned by the cleanup job mid-request"
                )
            if row.is_pending:
                # Expiry-reclaim race: our (slow) handler's pending row
                # expired, a fresh request reclaimed the slot with a
                # different hash, and now WE try to record a response
                # against a slot that's no longer ours. The UPDATE
                # missed (hash mismatch in the WHERE), the INSERT
                # conflicted, and the SELECT returned the new claim's
                # pending row (status_code=0, is_pending=True). Returning
                # that to the caller would silently cache zero-state
                # data; raise loudly so the middleware's outer
                # try/except degrades to no-cache instead.
                raise RuntimeError(
                    f"idempotency row ({tenant_id!r}, {idempotency_key!r}) "
                    "is still pending under a different claim — the original "
                    "pending TTL likely elapsed and the slot was reclaimed"
                )
            if row.request_hash != request_hash:
                # Same expiry-reclaim shape as above but the new claim
                # already finished. The SELECT has no ``request_hash``
                # filter (it can't — the new claim's hash is unknown
                # ahead of time), so silently returning would cache the
                # OTHER request's response under the original caller's
                # hash. Raise so the caller sees a clear failure rather
                # than a wrong-data replay.
                raise RuntimeError(
                    f"idempotency row ({tenant_id!r}, {idempotency_key!r}) "
                    "holds a different request hash — slot reclaimed by a "
                    "fresh request that completed before our record() arrived"
                )
            return row

    async def idempotency_upsert(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_hash: str,
        response_body: dict,
        status_code: int,
        expires_at: datetime,
    ) -> IdempotencyResponse:
        """Backwards-compatible alias for :meth:`idempotency_record`.

        Existing callers that do single-shot upsert without a prior
        claim continue to work. New code should use the
        ``claim`` + ``record`` pair to close the concurrent-handler
        race window.
        """
        return await self.idempotency_record(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_body=response_body,
            status_code=status_code,
            expires_at=expires_at,
        )
