"""PostgreSQL service -- all database queries for core tables.

Single point of DB access for the OSS core-storage-api.  Every query that
was previously spread across eight repository classes now lives here, grouped
by domain: memories, entities, agents, documents, fleet, audit, reports, tasks.

Session management uses a module-level ``async_sessionmaker`` backed by the
shared engine from ``core_storage_api.database.init.get_engine``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import (
    and_,
    bindparam,
    case,
    delete,
    distinct,
    false,
    func,
    literal_column,
    or_,
    select,
    text,
    tuple_,
)
from sqlalchemy import update as sql_update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only

from common.constants import (
    CONTRADICTION_CANDIDATE_MAX,
    CONTRADICTION_SIMILARITY_THRESHOLD,
    DEFAULT_RELATION_TYPE_WEIGHT,
    ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    GRAPH_MAX_EXPANDED_ENTITIES,
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
    AuditChainHead,
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
from common.models.capability_usage import CapabilityUsage
from common.models.organization_settings import OrganizationSettings, OrganizationSettingsAudit
from common.models.recall_log import RecallCandidate, RecallEvent
from common.organization_settings_merge import deep_merge, diff_settings
from core_storage_api.observability import db_measure
from core_storage_api.schemas import MEMORY_LIST_FIELDS, orm_to_dict
from core_storage_api.services.audit_chain import (
    GENESIS_PREV_HASH,
    assert_pii_safe,
    canonical_created_at,
    canonical_event,
    compute_event_hash,
)

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


def _as_json_str(value: Any) -> str:
    """Normalise a JSONB bind to a JSON string for asyncpg's ``::jsonb`` cast.

    The session-trace upsert binds ``memory_ids`` / ``entity_ids`` /
    ``signals_summary`` as ``:param::jsonb``; asyncpg expects a JSON string,
    not a python list/dict. Callers may hand either (the HTTP body arrives as
    a python object after JSON-decode), so accept both and emit a string.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _coerce_dt(value: Any) -> Any:
    """Parse an ISO-8601 string to ``datetime``; pass ``datetime`` through.

    Trace timestamps cross the wire as ISO strings (JSON has no datetime),
    so the upsert path coerces them back before binding.
    """
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _relation_weight(relation_type: str, row_weight: float) -> float:
    """Compute effective weight for a relation edge."""
    type_w = RELATION_TYPE_WEIGHTS.get(
        relation_type.lower(),
        DEFAULT_RELATION_TYPE_WEIGHT,
    )
    return type_w * row_weight


# ---------------------------------------------------------------------------
# Union-Find helpers (entity-resolution clustering — Fix 2 Ph6)
# ---------------------------------------------------------------------------
#
# Ported byte-for-byte from
# ``core_api/pipeline/steps/entity_linking/resolve_entities.py`` so the
# duplicate-entity clustering keeps identical semantics now that the merge runs
# storage-side. core-storage-api must not import from ``core_api``.


def _entity_uf_find(parent: dict[UUID, UUID], x: UUID) -> UUID:
    while parent[x] != x:
        parent[x] = parent[parent[x]]  # path compression
        x = parent[x]
    return x


def _entity_uf_union(parent: dict[UUID, UUID], rank: dict[UUID, int], a: UUID, b: UUID) -> None:
    ra, rb = _entity_uf_find(parent, a), _entity_uf_find(parent, b)
    if ra == rb:
        return
    if rank[ra] < rank[rb]:
        ra, rb = rb, ra
    parent[rb] = ra
    if rank[ra] == rank[rb]:
        rank[ra] += 1


# Cached at import time so the per-row column-name filter on the bulk
# write hot path (100 items x ~25 columns) doesn't pay the cost of
# walking ``Memory.__table__.columns`` and ``__mapper__.column_attrs``
# on every call. Both sets are static for the lifetime of the process.
_MEMORY_VALID_FIELDS = frozenset(
    {c.key for c in Memory.__table__.columns} | {a.key for a in Memory.__mapper__.column_attrs}
)

# Columns the admin memory-list endpoint may sort by. Allowlisted so an
# unexpected ``sort`` value falls back to created_at instead of raising
# AttributeError (500) at ``getattr(Memory, sort)`` — the endpoint is callable
# independently of core-api's route-level regex guard.
_ADMIN_LIST_SORTABLE = frozenset(
    {
        "created_at",
        "weight",
        "memory_type",
        "agent_id",
        "status",
        "recall_count",
        "fleet_id",
        "tenant_id",
        "expires_at",
        "deleted_at",
    }
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

# ── Fleet-scoped hard-purge (test-tenant hygiene) ──
#
# The subset of ``_PURGE_TENANT_TABLES`` that carries its own ``fleet_id``
# column, so a single fleet's footprint can be permanently removed from a
# SHARED tenant without touching the rest of the tenant. Used by the
# OpenClaw fleet-tester to clean up its run-scoped ``nightly-<run_id>-fleet-NN``
# fleets at teardown (the dev tenant otherwise accumulates run data that
# confounds isolation/trust tests). Ordered children-before-parents so the
# per-table DELETEs never trip a foreign key.
#
# ``fleet_commands`` is intentionally NOT in this tuple — it has no
# ``fleet_id`` of its own (it's keyed by ``node_id``); ``purge_fleet_data``
# deletes it explicitly by the fleet's node ids before the nodes go, so its
# count is reported rather than hidden inside the ``fleet_nodes`` ON DELETE
# CASCADE. ``memory_entity_links`` (no ``fleet_id``) rides the CASCADE from
# ``memories`` / ``entities``. Tenant-wide tables (``audit_log``,
# ``background_task_log``, ``idempotency_responses``, ``organization_settings*``)
# are excluded — they're tenant config or the retained audit trail, not
# fleet-scoped run data.
_PURGE_FLEET_TABLES: tuple[str, ...] = (
    "relations",
    "memories",
    "entities",
    "agents",
    "fleet_nodes",
    "documents",
    "analysis_reports",
    "dedup_reviews",
)


def _verify_audit_chain_rows(
    tenant_id: str, rows: list[AuditLog], head: AuditChainHead | None, limit: int
) -> dict:
    """Walk pre-fetched chain rows and verify integrity (pure CPU, no I/O).

    Split out from :meth:`PostgresService.audit_verify_chain` so the SHA-256 /
    JSON-canonicalization loop — up to ``limit`` (≤ 500k) rows — can run via
    ``asyncio.to_thread`` instead of blocking the event loop. Operates only on
    already-loaded ORM attributes, so it's safe off the event loop.
    """
    expected_prev = GENESIS_PREV_HASH
    expected_seq = 1
    for row in rows:
        reason: str | None = None
        if row.seq != expected_seq:
            reason = "seq_gap"
        elif row.prev_hash != expected_prev:
            reason = "prev_hash_mismatch"
        else:
            canon = canonical_event(
                tenant_id=row.tenant_id,
                seq=row.seq,
                agent_id=row.agent_id,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                detail=row.detail,
                created_at_iso=canonical_created_at(row.created_at),
            )
            if compute_event_hash(canon, row.prev_hash) != row.event_hash:
                reason = "event_hash_mismatch"
        if reason is not None:
            return {
                "tenant_id": tenant_id,
                "valid": False,
                "verified_count": expected_seq - 1,
                "first_broken": {
                    "seq": row.seq,
                    "id": str(row.id),
                    "reason": reason,
                    "created_at": row.created_at.astimezone(UTC).isoformat(),
                },
            }
        expected_prev = row.event_hash
        expected_seq += 1

    truncated = len(rows) >= limit
    head_seq, head_hash = (head.last_seq, head.last_hash) if head is not None else (0, GENESIS_PREV_HASH)
    last_seq, last_hash = (rows[-1].seq, rows[-1].event_hash) if rows else (0, GENESIS_PREV_HASH)
    if not truncated and (head_seq != last_seq or head_hash != last_hash):
        return {
            "tenant_id": tenant_id,
            "valid": False,
            "verified_count": len(rows),
            "first_broken": {
                "reason": "tail_truncated",
                "head_seq": head_seq,
                "chain_seq": last_seq,
            },
        }
    return {
        "tenant_id": tenant_id,
        "valid": True,
        "verified_count": len(rows),
        "head_seq": last_seq,
        "truncated": truncated,
    }


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

    async def memory_update(self, memory_id: UUID, tenant_id: str, patch: dict) -> bool:
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

        Every statement is scoped to ``tenant_id`` (the row's home
        tenant): a ``memory_id`` owned by a different tenant matches no
        row, so the existence check returns ``False`` (→ 404) and neither
        UPDATE can touch a foreign tenant's row.

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
                    select(Memory.id, Memory.deleted_at)
                    .where(Memory.id == memory_id, Memory.tenant_id == tenant_id)
                    .with_for_update()
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
                    .where(
                        Memory.id == memory_id,
                        Memory.tenant_id == tenant_id,
                        Memory.deleted_at.is_(None),
                    )
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
                        "WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
                    ).bindparams(patch=json.dumps(metadata_patch), id=memory_id, tenant_id=tenant_id),
                )
        return True

    async def memory_update_status(
        self,
        memory_id: UUID,
        status: str,
        *,
        tenant_id: str,
        supersedes_id: UUID | None = None,
        unset_supersedes: bool = False,
        expected_supersedes_id: UUID | None = None,
    ) -> bool:
        """Update a memory's ``status`` and optionally (re)set ``supersedes_id``.

        Args:
            memory_id: Target row.
            status: New status value.
            tenant_id: Home tenant of the target memory. REQUIRED — the WHERE
                clause is scoped to ``Memory.tenant_id == tenant_id`` so a
                caller in tenant B can never flip the status of tenant A's
                memory by id (cross-tenant write guard).
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
            CAS check failed, the tenant didn't match, or the row id doesn't
            exist. Existing callers ignore the return value — adding it is
            backward-compatible.
        """
        values: dict[str, Any] = {"status": status}
        if unset_supersedes:
            values["supersedes_id"] = None
        elif supersedes_id is not None:
            values["supersedes_id"] = supersedes_id

        async with get_session() as session:
            stmt = sql_update(Memory).where(
                Memory.id == memory_id,
                Memory.tenant_id == tenant_id,
            )
            if expected_supersedes_id is not None:
                stmt = stmt.where(Memory.supersedes_id == expected_supersedes_id)
            stmt = stmt.values(**values)
            result = await session.execute(stmt)
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def memory_update_embedding(
        self,
        memory_id: UUID,
        tenant_id: str,
        embedding: list[float],
        metadata: dict | None = None,
    ) -> bool:
        # ``tenant_id`` scopes the write to the row's home tenant: a
        # memory_id from another tenant matches no row, so a stale or
        # spoofed worker payload can never overwrite a foreign embedding.
        # Returns whether a row matched so the route can surface a 404 on
        # a no-op (mirrors memory_update / memory_update_status) instead of
        # a silent 200 that lets callers over-count successful writes.
        async with get_session() as session:
            values: dict = {"embedding": embedding}
            if metadata is not None:
                values["metadata_"] = metadata
            result = await session.execute(
                sql_update(Memory)
                .where(Memory.id == memory_id, Memory.tenant_id == tenant_id)
                .values(**values)
            )
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

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

        # Status demotion. ``outdated`` is always demoted (a definitively
        # superseded fact). ``conflicted`` is demoted EXCEPT when the row is an
        # exact lexical match for the query: a conflicted memory carries a
        # competing claim, not a retraction, and when it is the exact thing the
        # caller asked for it must not be buried beneath unrelated near-duplicate
        # siblings (different entities that merely share a name prefix). The
        # competing successor is still surfaced via load_and_serialize's
        # supersedes-chain injection, so the caller sees both sides.
        #
        # Why an FTS match is the right gate: at corpus scale a conflicted row's
        # blended relevance (vec+fts) sits only marginally above its near-dup
        # siblings, so the 0.5 multiplier reliably sinks it below them even when
        # it is the single best match — the exact-match signal is what
        # distinguishes "the row the caller wants" from "a sibling about a
        # different entity". Empty/stopword-only queries degrade ts_query to the
        # empty tsquery (matches nothing here), so the gate is inert for
        # vector-only / entity-only callers and conflicted stays demoted.
        _exact_lexical_match = Memory.search_vector.op("@@")(ts_query) if query and query.strip() else false()
        status_penalty = case(
            (Memory.status == "outdated", 0.5),
            (and_(Memory.status == "conflicted", ~_exact_lexical_match), 0.5),
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
            #
            # Carve-out: a ``conflicted`` row that is an EXACT lexical match
            # for the query is kept. ``conflicted`` (unlike ``outdated``) means
            # "a competing claim exists", not "definitively retracted" — and the
            # semantic contradiction path mismarks near-duplicate-but-distinct
            # entities (e.g. ``Wayne #0000`` vs ``Wayne #0704``), so a blanket
            # exclusion silently drops the very row the caller named. The
            # exact-match gate scopes the carve-out to rows the caller clearly
            # asked for; status_penalty above keeps a surfaced exact-match
            # conflicted row un-demoted, and load_and_serialize still injects its
            # supersedes successor so both sides are visible. ``outdated`` stays
            # fully excluded.
            scored_stmt = scored_stmt.where(
                or_(
                    Memory.status.notin_(("outdated", "conflicted")),
                    and_(Memory.status == "conflicted", _exact_lexical_match),
                )
            )
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
                # ENTITY_LOOKUP path: these memory IDs are already scoped to the
                # entities the caller's query resolved to — every row here is
                # "about" the named entity, the entity-graph analogue of the
                # scored path's exact-lexical-match carve-out. So we keep
                # ``conflicted`` rows (a competing claim about the very entity
                # asked for, which the semantic contradiction path routinely
                # mismarks across distinct ``#NNNN`` siblings) and exclude only
                # ``outdated`` (definitively retracted). load_and_serialize still
                # injects the supersedes successor so both sides remain visible.
                stmt = stmt.where(Memory.status != "outdated")
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

    async def purge_fleet_data(self, tenant_id: str, fleet_id: str) -> dict[str, int]:
        """Permanently delete every row scoped to ``(tenant_id, fleet_id)``
        across the fleet-scoped OSS tables — the per-fleet analogue of
        ``purge_tenant_data`` for test-tenant hygiene (run-scoped fleet
        cleanup). Returns per-table deleted counts.

        Runs in one transaction, children-before-parents, so a foreign key
        never blocks a delete and a mid-purge failure rolls back cleanly.
        ``fleet_commands`` has no ``fleet_id`` of its own, so it's deleted by
        the fleet's ``node_id``s first (and counted) before the nodes go;
        ``memory_entity_links`` rides the ON DELETE CASCADE from ``memories`` /
        ``entities``. ``audit_log`` and the tenant-wide settings tables are
        intentionally untouched.

        Idempotent: re-running on an already-purged fleet deletes nothing and
        returns zeros, so a retried teardown is safe.
        """
        counts: dict[str, int] = {}
        async with get_session() as session:
            # ``fleet_commands`` is keyed by ``node_id`` (FK to fleet_nodes),
            # not ``fleet_id`` — resolve the fleet's node ids and delete its
            # commands first so the count is explicit rather than hidden in
            # the fleet_nodes CASCADE below.
            node_ids = list(
                (
                    await session.execute(
                        select(FleetNode.id).where(
                            FleetNode.tenant_id == tenant_id,
                            FleetNode.fleet_id == fleet_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if node_ids:
                cmd_result = await session.execute(
                    FleetCommand.__table__.delete().where(FleetCommand.node_id.in_(node_ids))
                )
                counts["fleet_commands"] = cmd_result.rowcount  # type: ignore[attr-defined]
            else:
                counts["fleet_commands"] = 0

            for table_name in _PURGE_FLEET_TABLES:
                # Schema-qualify (``public.``) so an irreversible hard-delete
                # can't be redirected by a non-default search_path — same
                # defence as ``purge_tenant_data``.
                result = await session.execute(
                    text(f"DELETE FROM public.{table_name} WHERE tenant_id = :tid AND fleet_id = :fid"),
                    {"tid": tenant_id, "fid": fleet_id},
                )
                counts[table_name] = result.rowcount  # type: ignore[attr-defined]
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

    async def memory_entity_coverage_count(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> int:
        """Count distinct memories that have at least one entity link.

        Ports the crystallizer ``_compute_health`` cross-table COUNT; the
        coverage pct is computed caller-side against total memories. The query
        is a fully static string (no f-string interpolation) — fleet stays
        optional via ``CAST(:fleet_id AS text) IS NULL``. The CAST is required
        so asyncpg can type the bound NULL; a bare ``:fleet_id IS NULL`` raises
        "could not determine data type of parameter" (CAURA-595 cast gotcha).
        """
        params: dict = {"tenant_id": tenant_id, "fleet_id": fleet_id}
        async with get_read_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT COUNT(DISTINCT mel.memory_id)
                    FROM memory_entity_links mel
                    JOIN memories m ON m.id = mel.memory_id
                    WHERE m.tenant_id = :tenant_id
                      AND (
                          CAST(:fleet_id AS text) IS NULL
                          OR m.fleet_id = CAST(:fleet_id AS text)
                      )
                      AND m.deleted_at IS NULL
                    """
                ),
                params,
            )
            return result.scalar() or 0

    async def memory_audit_usage_stats(self, tenant_id: str) -> dict:
        """Agent-activity + peak-hours from ``audit_log`` (crystallizer usage).

        Ports the two ``_compute_usage`` audit_log queries. The
        ``search_write_ratio`` query is intentionally NOT ported — its
        ``usage_counters`` table does not exist in the OSS schema.

        Tweaks vs the source query, all scoping to memory-attributed activity
        without changing real totals: ``agent_id IS NOT NULL`` drops the
        meaningless null-agent group, the ``searches`` FILTER is scoped to
        ``resource_type = 'memory'`` for symmetry with ``writes``, and
        ``peak_hours`` is likewise scoped to ``resource_type = 'memory'`` so
        non-memory events (entity extraction, etc.) can't distort the top hours.
        """
        async with get_read_session() as session:
            agents = await session.execute(
                text(
                    """
                    SELECT a.agent_id,
                           COUNT(*) FILTER (
                               WHERE a.action = 'create' AND a.resource_type = 'memory'
                           ) AS writes,
                           COUNT(*) FILTER (
                               WHERE a.action = 'search' AND a.resource_type = 'memory'
                           ) AS searches
                    FROM audit_log a
                    WHERE a.tenant_id = :tenant_id AND a.agent_id IS NOT NULL
                    GROUP BY a.agent_id
                    ORDER BY writes DESC
                    LIMIT 20
                    """
                ),
                {"tenant_id": tenant_id},
            )
            agent_activity = [
                {"agent_id": row[0], "writes": row[1], "searches": row[2]} for row in agents.all()
            ]
            hours = await session.execute(
                text(
                    """
                    SELECT EXTRACT(hour FROM a.created_at)::int AS hr, COUNT(*) AS cnt
                    FROM audit_log a
                    WHERE a.tenant_id = :tenant_id AND a.resource_type = 'memory'
                    GROUP BY hr
                    ORDER BY cnt DESC
                    LIMIT 3
                    """
                ),
                {"tenant_id": tenant_id},
            )
            peak_hours = [{"hour": row[0], "count": row[1]} for row in hours.all()]
        return {"agent_activity": agent_activity, "peak_hours": peak_hours}

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
        tenant_id: str,
    ) -> None:
        if not memory_ids:
            return
        # ``tenant_id`` bounds the bulk stamp to the caller's tenant: any
        # id in the list that belongs to another tenant is silently
        # skipped rather than having its dedup-checked timestamp moved.
        async with get_session() as session:
            await session.execute(
                sql_update(Memory)
                .where(Memory.id.in_(memory_ids), Memory.tenant_id == tenant_id)
                .values(last_dedup_checked_at=func.now())
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

    async def memory_increment_recall(self, memory_ids: list[UUID]) -> int:
        """Bump recall_count/last_recalled_at by id; returns rows actually updated."""
        if not memory_ids:
            return 0
        async with get_session() as session:
            result = await session.execute(
                sql_update(Memory)
                .where(Memory.id.in_(memory_ids))
                .values(
                    recall_count=Memory.recall_count + 1,
                    last_recalled_at=func.now(),
                )
            )
            # Real rowcount, not the input count — stale/deleted ids that match
            # no row must not inflate the reported "updated" total.
            return result.rowcount or 0  # type: ignore[attr-defined]

    async def recall_log_write(self, event: dict, candidates: list[dict]) -> str:
        """Persist one ``recall_event`` + N ``recall_candidate`` rows, ONE txn.

        Ports the fire-and-forget write that used to live in core-api's
        ``log_recall_event._persist`` (a direct ``async_session`` insert that
        regressed the storage-boundary rule). The ``event`` dict carries the
        row's own ``tenant_id`` (``recall_event`` is tenant-scoped by that
        column); each candidate is stamped with the freshly-assigned
        ``recall_event_id`` so the two inserts share one transaction. Returns
        the new ``recall_event.id`` as a string.

        ``event``/``candidates`` keys must be valid model columns — the router
        validates ``tenant_id``/``source`` presence up front; any unexpected
        key would raise a clean ``TypeError`` from the model constructor here.
        """
        async with get_session() as session:
            ev = RecallEvent(**event)
            session.add(ev)
            # flush assigns ev.id (server_default gen_random_uuid()) before the
            # candidate rows reference it — both still inside the single
            # ``get_session`` transaction that commits on context exit.
            await session.flush()
            for c in candidates:
                session.add(RecallCandidate(recall_event_id=ev.id, **c))
            return str(ev.id)

    # ------------------------------------------------------------------
    # G2) Doc-hash idempotency (ingest write-path gate)
    # ------------------------------------------------------------------

    async def find_prior_ingest_by_doc_hash(self, tenant_id: str, doc_hash: str) -> list[Memory]:
        """Return memories from the most-recent prior ingest of identical content.

        Ports ``ingest_service._find_prior_ingest_by_doc_hash`` verbatim: a
        non-deleted, tenant-scoped row whose metadata carries the same
        ``doc_hash`` and was tagged ``source="ingest"``. When several runs
        match, only the memories of the newest ``run_id`` are returned.

        Runs on ``get_session`` (the WRITER), NOT ``get_read_session``: this is
        a write-path idempotency gate — replica lag would miss a just-committed
        prior ingest and re-ingest the same document. ``metadata_->>'key'`` text
        extraction matches the source's ``.astext`` filter.
        """
        async with get_session() as session:
            stmt = (
                select(Memory)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.metadata_["doc_hash"].astext == doc_hash,
                    Memory.metadata_["source"].astext == "ingest",
                    Memory.deleted_at.is_(None),
                )
                .order_by(Memory.created_at.desc())
            )
            result = await session.execute(stmt)
            rows: list[Memory] = list(result.scalars().all())
        if not rows:
            return []
        # Newest run wins (top-level ``run_id`` column is the single source of
        # truth for batch identity). Guard the NULL case: ``r.run_id == None`` is
        # truthy in Python, so an anonymous (run_id IS NULL) newest row would
        # otherwise collapse EVERY null-run_id ingest across runs into one
        # result — return just the single newest row instead.
        newest_run_id = rows[0].run_id
        if newest_run_id is None:
            return [rows[0]]
        return [r for r in rows if r.run_id == newest_run_id]

    # ------------------------------------------------------------------
    # G3) Capability-usage analytics flush (cross-tenant, RLS-free)
    # ------------------------------------------------------------------

    async def capability_usage_insert(self, rows: list[dict]) -> int:
        """Bulk-append adoption-counter rows to ``capability_usage``, ONE txn.

        Ports core-api's ``capability_usage._default_flush``. This table is
        intentionally CROSS-TENANT / RLS-free (migration 023): one flush batch
        carries many tenants' counters, so NO per-tenant scoping is applied —
        each row carries its own ``tenant_id`` grouping dimension. Append-only
        (no unique constraint, no upsert); consumers SUM at query time. Returns
        the number of rows inserted.
        """
        if not rows:
            return 0
        async with get_session() as session:
            session.add_all([CapabilityUsage(**r) for r in rows])
        return len(rows)

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

    # ------------------------------------------------------------------
    # B) Fix 2 Phase 2 — fleet/admin discovery + detail + bulk mutations
    # ------------------------------------------------------------------

    async def memory_fleet_distribution(
        self,
        tenant_id: str | None,
        *,
        exclude_scope_agent: bool,
    ) -> list[dict]:
        """Distinct ``fleet_id`` with memory + distinct-agent counts, desc.

        Serves both the tenant-facing ``/fleets`` (``exclude_scope_agent``
        True — no caller identity to legitimately see ``scope_agent`` rows,
        so they're excluded the same way ``list_by_filters`` does) and the
        admin ``/admin/fleets`` (``exclude_scope_agent`` False, cross-tenant
        when ``tenant_id`` is None). Read-only (reader replica).
        """
        filters = [Memory.deleted_at.is_(None), Memory.fleet_id.isnot(None)]
        if exclude_scope_agent:
            # ``Memory.visibility`` is NOT NULL with a server default, so the
            # three-valued-logic NULL pitfall doesn't apply.
            filters.append(Memory.visibility != "scope_agent")
        if tenant_id is not None:
            filters.append(Memory.tenant_id == tenant_id)
        async with get_read_session() as session:
            rows = (
                await session.execute(
                    select(
                        Memory.fleet_id,
                        func.count(),
                        func.count(func.distinct(Memory.agent_id)),
                    )
                    .where(*filters)
                    .group_by(Memory.fleet_id)
                    .order_by(func.count().desc())
                )
            ).all()
        return [{"fleet_id": r[0], "memory_count": r[1], "agent_count": r[2]} for r in rows]

    async def memory_get_detail(
        self,
        memory_id: UUID,
        tenant_id: str,
    ) -> dict | None:
        """Bundle a single memory's full row + entity links + embedding stats.

        The raw pgvector NEVER crosses the wire: embedding min/max/mean/
        non_zero/dimensions and a first-20 preview are computed here and the
        embedding column is stripped from the returned row dict. The memory
        row and its entity-link outerjoin are fetched in two queries in one
        session (no per-link N+1). Returns None when the row is absent, soft-
        deleted, or belongs to another tenant. Read-only (reader replica).
        """
        async with get_read_session() as session:
            memory = await session.get(Memory, memory_id)
            if memory is None or memory.tenant_id != tenant_id or memory.deleted_at is not None:
                return None

            link_rows = (
                await session.execute(
                    select(MemoryEntityLink, Entity)
                    .outerjoin(Entity, MemoryEntityLink.entity_id == Entity.id)
                    .where(MemoryEntityLink.memory_id == memory_id)
                )
            ).all()
            entity_links: list[dict] = []
            for link, entity in link_rows:
                entry: dict = {"entity_id": str(link.entity_id), "role": link.role}
                if entity is not None:
                    entry["entity_type"] = entity.entity_type
                    entry["canonical_name"] = entity.canonical_name
                    entry["attributes"] = entity.attributes
                entity_links.append(entry)

            embedding_preview: list[float] | None = None
            embedding_stats: dict | None = None
            if memory.embedding is not None:
                vec = [float(v) for v in memory.embedding]
                if vec:
                    embedding_preview = vec[:20]
                    embedding_stats = {
                        "dimensions": len(vec),
                        "min": round(min(vec), 6),
                        "max": round(max(vec), 6),
                        "mean": round(sum(vec) / len(vec), 6),
                        "non_zero": sum(1 for v in vec if abs(v) > 1e-8),
                    }

            # MEMORY_LIST_FIELDS excludes embedding + search_vector: the client
            # consumes the server-computed preview/stats, never the raw vector,
            # so it's neither serialised nor shipped.
            row = orm_to_dict(memory, MEMORY_LIST_FIELDS)
        return {
            "memory": row,
            "entity_links": entity_links,
            "embedding_preview": embedding_preview,
            "embedding_stats": embedding_stats,
        }

    async def memory_contradiction_rows(
        self,
        memory_id: UUID,
        tenant_id: str,
    ) -> dict | None:
        """Bundle the 3 contradiction reads in one round-trip.

        Returns ``{memory, supersessors[], older|null}`` as flat row dicts;
        the upstream core-api keeps the ``_reason_for`` / direction /
        ``detection_status`` shaping. The cross-tenant ``older`` guard
        (a corrupted ``supersedes_id`` pointing at another tenant's row)
        is enforced here so the leak never crosses the wire. Returns None
        when the target memory is absent/soft-deleted/wrong tenant.
        Read-only (reader replica).
        """
        async with get_read_session() as session:
            memory = await session.get(Memory, memory_id)
            if memory is None or memory.tenant_id != tenant_id or memory.deleted_at is not None:
                return None

            supersessors = (
                (
                    await session.execute(
                        select(Memory)
                        .where(
                            Memory.supersedes_id == memory_id,
                            Memory.tenant_id == tenant_id,
                            Memory.deleted_at.is_(None),
                        )
                        .order_by(Memory.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )

            older = None
            if memory.supersedes_id:
                older_row = await session.get(Memory, memory.supersedes_id)
                # ``session.get`` is a bare PK lookup — guard against a
                # corrupted cross-tenant ``supersedes_id`` leaking another
                # tenant's content.
                if older_row is not None and older_row.tenant_id == tenant_id:
                    older = older_row

            return {
                # MEMORY_LIST_FIELDS: core-api's contradiction shaping never
                # reads the embedding/search_vector, so don't ship them.
                "memory": orm_to_dict(memory, MEMORY_LIST_FIELDS),
                "supersessors": [orm_to_dict(m, MEMORY_LIST_FIELDS) for m in supersessors],
                "older": orm_to_dict(older, MEMORY_LIST_FIELDS) if older is not None else None,
            }

    async def memory_soft_delete_by_filter(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None = None,
        agent_id: str | None = None,
        memory_type: str | None = None,
        status: str | None = None,
        exclude_ids: list[UUID] | None = None,
        metadata_filter: dict[str, str] | None = None,
    ) -> int:
        """Soft-delete every matching live memory for a tenant; returns count.

        The JSONB ``metadata->>'key' = 'value'`` predicates are built with
        SQLAlchemy bound params (``Memory.metadata_[key].astext == bindparam(...)``)
        — never string interpolation. Transactional (writer session).
        """
        stmt = sql_update(Memory).where(
            Memory.tenant_id == tenant_id,
            Memory.deleted_at.is_(None),
        )
        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        if agent_id:
            stmt = stmt.where(Memory.agent_id == agent_id)
        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        if status:
            stmt = stmt.where(Memory.status == status)
        if exclude_ids:
            stmt = stmt.where(Memory.id.notin_(exclude_ids))
        if metadata_filter:
            for i, (key, value) in enumerate(metadata_filter.items()):
                # Distinct bindparam name per pair so multiple predicates
                # don't collide; the KEY indexes the JSONB column (a SQL
                # expression, not a bound value) while the VALUE is bound.
                param: Any = bindparam(f"meta_val_{i}", value)
                stmt = stmt.where(Memory.metadata_[str(key)].astext == param)
        stmt = stmt.values(deleted_at=datetime.now(UTC), status="deleted")
        async with get_session() as session:
            result = await session.execute(stmt)
            return result.rowcount or 0  # type: ignore[attr-defined]

    async def memory_soft_delete_by_ids(
        self,
        tenant_id: str,
        ids: list[UUID],
    ) -> int:
        """Soft-delete live memories by id (tenant-scoped); returns count.

        Transactional (writer session). The 1-1000 cap stays in core-api.
        """
        if not ids:
            return 0
        async with get_session() as session:
            result = await session.execute(
                sql_update(Memory)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.id.in_(ids),
                    Memory.deleted_at.is_(None),
                )
                .values(deleted_at=datetime.now(UTC), status="deleted")
            )
            return result.rowcount or 0  # type: ignore[attr-defined]

    async def memory_soft_delete_by_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        metadata_source: str = "ingest",
    ) -> int:
        """Soft-delete live memories tagged with ``run_id`` AND
        ``metadata.source = metadata_source`` (belt-and-braces so non-ingest
        memories sharing a run_id aren't touched); returns count.
        Transactional (writer session).
        """
        async with get_session() as session:
            result = await session.execute(
                sql_update(Memory)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.deleted_at.is_(None),
                    Memory.run_id == run_id,
                    Memory.metadata_["source"].astext == metadata_source,
                )
                .values(deleted_at=datetime.now(UTC), status="deleted")
            )
            return result.rowcount or 0  # type: ignore[attr-defined]

    async def memory_redistribute(
        self,
        *,
        tenant_id: str,
        memory_ids: list[UUID],
        target_agent_id: str,
    ) -> dict:
        """Bulk-reassign memories to ``target_agent_id`` in ONE transaction.

        Locks the matching live rows ``FOR UPDATE``, loops computing
        moved/promoted/skipped/from_agents, sets ``agent_id`` and auto-promotes
        ``scope_agent`` → ``scope_team`` to prevent data loss, and computes
        ``not_found`` for ids that didn't match (deleted, wrong tenant, or
        non-existent). Trust gates + the agent_id==auth precedence check stay
        in core-api BEFORE the call. Transactional (writer session).
        """
        async with get_session() as session:
            memories = (
                (
                    await session.execute(
                        select(Memory)
                        .where(
                            Memory.id.in_(memory_ids),
                            Memory.tenant_id == tenant_id,
                            Memory.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )

            found_ids = {mem.id for mem in memories}
            not_found = [str(mid) for mid in memory_ids if mid not in found_ids]

            moved = 0
            promoted = 0
            skipped = 0
            from_agents: set[str] = set()

            for mem in memories:
                if mem.agent_id == target_agent_id:
                    skipped += 1
                    continue
                from_agents.add(mem.agent_id)
                mem.agent_id = target_agent_id
                if mem.visibility == "scope_agent":
                    mem.visibility = "scope_team"
                    promoted += 1
                moved += 1

        return {
            "moved": moved,
            "promoted": promoted,
            "skipped": skipped,
            "from_agents": sorted(from_agents),
            "not_found": not_found,
        }

    async def memory_admin_list(
        self,
        *,
        tenant_id: str | None = None,
        fleet_id: str | None = None,
        agent_id: str | None = None,
        memory_type: str | None = None,
        status: str | None = None,
        include_deleted: bool = False,
        sort: str = "created_at",
        order: str = "desc",
        offset: int = 0,
        limit: int = 50,
        cursor_ts: datetime | None = None,
        cursor_id: UUID | None = None,
    ) -> list[Memory]:
        """Admin cross-tenant memory list (NO visibility scoping).

        Returns up to ``limit`` rows (the route passes ``limit`` already
        widened to ``limit+1`` so it can detect ``has_more`` and build the
        next cursor). Mirrors the prior inline admin query's filter, cursor,
        and tiebreaker exactly. Read-only (reader replica).
        """
        stmt = select(Memory)
        if tenant_id:
            stmt = stmt.where(Memory.tenant_id == tenant_id)
        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        if not include_deleted:
            stmt = stmt.where(Memory.deleted_at.is_(None))
        if agent_id:
            stmt = stmt.where(Memory.agent_id == agent_id)
        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        if status:
            stmt = stmt.where(Memory.status == status)

        using_cursor = cursor_ts is not None and cursor_id is not None
        if using_cursor:
            # Row-value comparison ``(created_at, id) < (cursor_ts, cursor_id)``
            # — same form core-api's ``memory_repository.list_by_filters`` uses.
            # ``type: ignore`` because the SQLAlchemy stubs don't model bare
            # Python literals as ``tuple_`` args.
            stmt = stmt.where(tuple_(Memory.created_at, Memory.id) < tuple_(cursor_ts, cursor_id))  # type: ignore[arg-type]

        # core-api restricts ``sort`` via a route regex, but this endpoint is
        # independently callable — allowlist the column so an unknown value
        # falls back to created_at instead of AttributeError-ing (500) on
        # getattr(Memory, sort).
        if sort not in _ADMIN_LIST_SORTABLE:
            sort = "created_at"
        col = getattr(Memory, sort)
        if order == "desc":
            stmt = stmt.order_by(col.desc(), Memory.id.desc())
        else:
            stmt = stmt.order_by(col.asc(), Memory.id.asc())
        if not using_cursor:
            stmt = stmt.offset(offset)
        stmt = stmt.limit(limit)

        async with get_read_session() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def memory_admin_stats(
        self,
        tenant_id: str | None,
        fleet_id: str | None,
    ) -> dict:
        """Admin memory stats — ``{total, by_type, by_agent, by_status}``.

        Single GROUPING SETS scan over ``(memory_type), (agent_id), (status),
        ()``. Unlike ``memory_compute_health_stats`` (which has no ``by_agent``
        and a different shape), this matches the admin route's response. NO
        visibility scoping (admin sees everything). Cross-tenant when
        ``tenant_id`` is None. Read-only (reader replica).
        """
        # Single GROUPING SETS scan. Optional filters use the
        # ``(:p IS NULL OR col = :p)`` idiom with bound params — no compiled-SQL
        # splicing, injection-safe by construction. ``CAST(:p AS text)`` (not
        # ``:p::text`` — the ``::`` collides with text()'s ``:param`` colon
        # syntax) is required so asyncpg can infer the type of a NULL bound
        # param used in ``IS NULL`` (else AmbiguousParameterError).
        # ``GROUPING(col)=0`` ⇒ this output row groups on that column; the
        # all-bits-set value (7) flags the overall total (the empty grouping set).
        sql = text(
            """
            SELECT
                CASE
                    WHEN GROUPING(memory_type, agent_id, status) = 7 THEN 'total'
                    WHEN GROUPING(memory_type) = 0 THEN 'by_type'
                    WHEN GROUPING(agent_id) = 0 THEN 'by_agent'
                    WHEN GROUPING(status) = 0 THEN 'by_status'
                END                                              AS bucket,
                memory_type, agent_id, status,
                COUNT(*)                                         AS cnt
            FROM memories
            WHERE deleted_at IS NULL
              AND (CAST(:tenant_id AS text) IS NULL OR tenant_id = CAST(:tenant_id AS text))
              AND (CAST(:fleet_id AS text) IS NULL OR fleet_id = CAST(:fleet_id AS text))
            GROUP BY GROUPING SETS ((memory_type), (agent_id), (status), ())
            """
        ).bindparams(tenant_id=tenant_id, fleet_id=fleet_id)

        total = 0
        by_type: dict = {}
        by_agent: dict = {}
        by_status: dict = {}
        async with get_read_session() as session:
            rows = (await session.execute(sql)).all()
        for row in rows:
            bucket = row[0]
            cnt = int(row[4] or 0)
            if bucket == "total":
                total = cnt
            elif bucket == "by_type":
                by_type[row[1]] = cnt
            elif bucket == "by_agent":
                by_agent[row[2]] = cnt
            elif bucket == "by_status":
                by_status[row[3]] = cnt
        return {"total": total, "by_type": by_type, "by_agent": by_agent, "by_status": by_status}

    async def memory_list_by_filters(
        self,
        *,
        tenant_id: str,
        caller_agent_id: str | None = None,
        fleet_id: str | None = None,
        written_by: str | None = None,
        memory_type: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        weight_min: float | None = None,
        weight_max: float | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        include_deleted: bool = False,
        sort: str = "created_at",
        order: str = "desc",
        limit: int = 25,
        offset: int = 0,
        cursor_ts: datetime | None = None,
        cursor_id: UUID | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[Memory]:
        """Filter, sort, paginate memories WITH visibility scoping.

        Ports core-api ``memory_repository.list_by_filters`` verbatim — same
        visibility predicate, filters, cursor predicate, ``(sort, id)``
        tiebreaker, and ``limit + 1`` over-fetch (the caller slices + builds
        the next cursor). Distinct from ``memory_admin_list`` which has NO
        visibility scoping. Read-only (reader replica).

        **Visibility:** when ``caller_agent_id`` is set, ``scope_agent`` rows
        are visible only to the authoring agent; team/org always visible. When
        unset, all ``scope_agent`` rows are excluded. **Cross-tenant widening:**
        a non-empty ``readable_tenant_ids`` expands ``tenant_id = $1`` to
        ``tenant_id = ANY($1)``; ``tenant_id`` stays the binding/home tenant.
        """
        if readable_tenant_ids:
            stmt = select(Memory).where(Memory.tenant_id.in_(readable_tenant_ids))
        else:
            stmt = select(Memory).where(Memory.tenant_id == tenant_id)

        # Visibility predicate (critical: prevents scope_agent leaks).
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

        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        if written_by:
            stmt = stmt.where(Memory.agent_id == written_by)
        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        if status:
            stmt = stmt.where(Memory.status == status)
        if run_id is not None:
            stmt = stmt.where(Memory.run_id == run_id)
        if weight_min is not None:
            stmt = stmt.where(Memory.weight >= weight_min)
        if weight_max is not None:
            stmt = stmt.where(Memory.weight <= weight_max)
        if created_after is not None:
            stmt = stmt.where(Memory.created_at >= created_after)
        if created_before is not None:
            stmt = stmt.where(Memory.created_at <= created_before)
        if not include_deleted:
            stmt = stmt.where(Memory.deleted_at.is_(None))

        if cursor_ts is not None and cursor_id is not None:
            # Cursor predicate direction must match the ORDER BY below: a desc
            # page walks toward older rows (tuple `<` cursor), an asc page
            # toward newer rows (tuple `>` cursor). Splitting on `order` keeps
            # asc pagination moving forward (regression-covered by
            # test_list_by_filters_asc_cursor_returns_forward_page).
            if order == "desc":
                stmt = stmt.where(tuple_(Memory.created_at, Memory.id) < tuple_(cursor_ts, cursor_id))  # type: ignore[arg-type]
            else:
                stmt = stmt.where(tuple_(Memory.created_at, Memory.id) > tuple_(cursor_ts, cursor_id))  # type: ignore[arg-type]

        # Allowlist the sort column — this endpoint is independently callable,
        # so an unknown value falls back to created_at rather than
        # AttributeError-ing (500) on getattr(Memory, sort). core-api applies
        # its own stricter allowlist upstream.
        if sort not in _ADMIN_LIST_SORTABLE:
            sort = "created_at"
        col = getattr(Memory, sort)
        if order == "desc":
            stmt = stmt.order_by(col.desc(), Memory.id.desc())
        else:
            stmt = stmt.order_by(col.asc(), Memory.id.asc())
        if offset and cursor_ts is None:
            stmt = stmt.offset(offset)
        stmt = stmt.limit(limit + 1)

        async with get_read_session() as session:
            return list((await session.execute(stmt)).scalars().all())

    async def memory_stats_breakdown(
        self,
        *,
        tenant_id: str | None,
        fleet_id: str | None = None,
        agent_id: str | None = None,
        memory_type: str | None = None,
        status: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        exclude_memory_types: list[str] | None = None,
        exclude_agent_ids: list[str] | None = None,
        exclude_title_regex: str | None = None,
        include_deleted: bool = False,
        readable_tenant_ids: list[str] | None = None,
    ) -> dict:
        """Return ``{total, by_type, by_agent, by_status}`` (+ optional
        ``by_tenant`` / ``deleted`` / ``total_including_deleted``).

        ``created_after`` / ``created_before`` bound the aggregation to a
        half-open ``[after, before)`` window — used by the daily/weekly report
        (GET /api/v1/reports) for "what each agent did in the period". Both
        optional; omitting them aggregates all-time (the MCP ``memclaw_stats``
        behaviour, unchanged).

        Ports core-api ``services.memory_stats.compute_memory_stats`` verbatim —
        same visibility scoping (``agent_id`` doubles as visibility identity AND
        author filter; when omitted, ``scope_agent`` rows are excluded so totals
        match what a non-semantic list would return), same single-pass GROUPING
        SETS aggregation, same cross-tenant widening + ``by_tenant`` breakdown,
        and same ``include_deleted`` CTE. Read-only (reader replica).
        """
        scope_filters = []
        if readable_tenant_ids:
            scope_filters.append(Memory.tenant_id.in_(readable_tenant_ids))
        else:
            # Always bind a tenant predicate — never run unscoped. With tenant_id
            # None this renders ``tenant_id IS NULL`` (matches nothing), so a caller
            # that omits tenant scope gets empty stats, never cross-tenant rows.
            scope_filters.append(Memory.tenant_id == tenant_id)
        if fleet_id:
            scope_filters.append(Memory.fleet_id == fleet_id)
        if agent_id:
            scope_filters.append(Memory.agent_id == agent_id)
            scope_filters.append(
                or_(
                    Memory.visibility == "scope_org",
                    Memory.visibility == "scope_team",
                    and_(
                        Memory.visibility == "scope_agent",
                        Memory.agent_id == agent_id,
                    ),
                )
            )
        else:
            scope_filters.append(Memory.visibility != "scope_agent")
        if memory_type:
            scope_filters.append(Memory.memory_type == memory_type)
        if status:
            scope_filters.append(Memory.status == status)
        # Report time-window: half-open [created_after, created_before). Added to
        # ``scope_filters`` so it flows through BOTH the live and include_deleted
        # query paths below (each derives from this list).
        if created_after:
            scope_filters.append(Memory.created_at >= created_after)
        if created_before:
            scope_filters.append(Memory.created_at < created_before)
        # Report "durable, decision-bearing" filter: drop episodic activity-log
        # types and the unattributed firehose agent ("main") so the report
        # reflects real per-agent work rather than the raw activity stream.
        if exclude_memory_types:
            scope_filters.append(Memory.memory_type.notin_(exclude_memory_types))
        if exclude_agent_ids:
            scope_filters.append(Memory.agent_id.notin_(exclude_agent_ids))
        # Report "cohesive" filter: drop heartbeat / health-check / status-poll
        # noise that isn't type=episode (e.g. action/outcome "heartbeat" rows) so
        # the per-agent leaderboard reflects real work, not monitoring pings.
        # ``coalesce(title,'')`` keeps null-title rows instead of dropping them on
        # the NULL-propagating negation. Case-insensitive POSIX regex (``~*``).
        if exclude_title_regex:
            scope_filters.append(~func.coalesce(Memory.title, "").op("~*")(exclude_title_regex))

        filters = [Memory.deleted_at.is_(None), *scope_filters]

        include_by_tenant = bool(readable_tenant_ids and len(readable_tenant_ids) > 1)

        grouping_sets = ["()", "(memory_type)", "(agent_id)", "(status)"]
        if include_by_tenant:
            grouping_sets.append("(tenant_id)")
        grouping_sets_sql = ", ".join(grouping_sets)

        grouping_total_cols = ["memory_type", "agent_id", "status"]
        if include_by_tenant:
            grouping_total_cols.append("tenant_id")
        grouping_total_arg = ", ".join(grouping_total_cols)
        grouping_total_value = (1 << len(grouping_total_cols)) - 1
        bucket_when = [
            f"WHEN GROUPING({grouping_total_arg}) = {grouping_total_value} THEN 'total'",
            "WHEN GROUPING(memory_type) = 0 THEN 'by_type'",
            "WHEN GROUPING(agent_id) = 0 THEN 'by_agent'",
            "WHEN GROUPING(status) = 0 THEN 'by_status'",
        ]
        if include_by_tenant:
            bucket_when.append("WHEN GROUPING(tenant_id) = 0 THEN 'by_tenant'")
        bucket_when_sql = "\n                ".join(bucket_when)

        # Compile the SQLAlchemy filter expressions to a WHERE fragment with BOUND
        # parameters (not literal_binds) — keeps the dynamic visibility / scoping
        # rules out of hand-written SQL while leaving user-supplied filter values
        # (fleet_id/agent_id/memory_type/status) parameterised, never inlined.
        # Returns (where_fragment, params) to thread into the ``text()`` execute.
        def _predicate_sql(filter_list) -> tuple[str, dict]:
            compiled = (
                select(Memory.id)
                .where(*filter_list)
                # ``render_postcompile`` expands IN/NOT IN bind lists (e.g. the
                # exclude_memory_types / exclude_agent_ids filters) into individual
                # named params at compile time. Without it the compiled string
                # carries an unexpanded ``[POSTCOMPILE_x]`` placeholder that the
                # raw ``text()`` re-execution below cannot bind ("column __ ...").
                .compile(
                    dialect=postgresql.dialect(paramstyle="named"),
                    compile_kwargs={"render_postcompile": True},
                )
            )
            rendered = str(compiled)
            idx = rendered.upper().find("WHERE ")
            if idx < 0:
                # Never fall back to "TRUE": a missing WHERE would run the
                # aggregation UNSCOPED across all tenants. If SQLAlchemy ever
                # changes its compiled format, fail loudly instead.
                raise RuntimeError(
                    "memory_stats_breakdown: no WHERE clause in compiled output "
                    f"(refusing to run unscoped): {rendered[:200]!r}"
                )
            fragment = rendered[idx + len("WHERE ") :]
            return fragment, dict(compiled.params)

        select_cols = "memory_type, agent_id, status"
        if include_by_tenant:
            select_cols = f"{select_cols}, tenant_id"

        if include_deleted:
            all_predicate, pred_params = _predicate_sql(scope_filters)
            sql = f"""
            WITH base AS (
                SELECT memory_type, agent_id, status, tenant_id,
                       (deleted_at IS NULL) AS alive
                FROM memories
                WHERE {all_predicate}
            )
            SELECT
                CASE
                    {bucket_when_sql}
                END                                              AS bucket,
                {select_cols},
                COUNT(*) FILTER (WHERE alive)                    AS live_cnt,
                COUNT(*) FILTER (WHERE NOT alive)                AS deleted_cnt
            FROM base
            GROUP BY GROUPING SETS ({grouping_sets_sql})
            """
        else:
            predicate, pred_params = _predicate_sql(filters)
            sql = f"""
            SELECT
                CASE
                    {bucket_when_sql}
                END                                              AS bucket,
                {select_cols},
                COUNT(*)                                         AS live_cnt,
                0                                                AS deleted_cnt
            FROM memories
            WHERE {predicate}
            GROUP BY GROUPING SETS ({grouping_sets_sql})
            """

        async with get_read_session() as session:
            rows = (await session.execute(text(sql), pred_params)).all()

        total = 0
        by_type: dict = {}
        by_agent: dict = {}
        by_status: dict = {}
        by_tenant: dict = {}
        deleted = 0

        live_idx = 5 if include_by_tenant else 4
        deleted_idx = live_idx + 1

        for row in rows:
            bucket = row[0]
            live = int(row[live_idx] or 0)
            dead = int(row[deleted_idx] or 0)
            if bucket == "total":
                total = live
                deleted = dead
            elif bucket == "by_type":
                by_type[row[1]] = live
            elif bucket == "by_agent":
                by_agent[row[2]] = live
            elif bucket == "by_status":
                by_status[row[3]] = live
            elif bucket == "by_tenant":
                by_tenant[row[4]] = live

        result = {
            "total": total,
            "by_type": by_type,
            "by_agent": by_agent,
            "by_status": by_status,
        }
        if include_by_tenant:
            result["by_tenant"] = by_tenant
        if include_deleted:
            result["deleted"] = deleted
            result["total_including_deleted"] = total + deleted
        return result

    async def memory_daily_durable_counts(
        self,
        *,
        tenant_id: str,
        since: datetime,
        fleet_id: str | None = None,
        exclude_memory_types: list[str] | None = None,
        exclude_agent_ids: list[str] | None = None,
        exclude_title_regex: str | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[dict]:
        """Per-day durable-write counts since ``since`` — the report's
        activity-over-time trend. Same durable/firehose exclusions and
        team/org visibility scope as ``memory_stats_breakdown`` (no agent_id ⇒
        excludes ``scope_agent``). Runs as a plain ORM ``GROUP BY`` executed
        directly (no compile→text round-trip, so ``NOT IN`` expands natively).
        Read-only (reader replica).
        """
        conds = [
            Memory.deleted_at.is_(None),
            Memory.created_at >= since,
            Memory.visibility != "scope_agent",
        ]
        if readable_tenant_ids:
            conds.append(Memory.tenant_id.in_(readable_tenant_ids))
        else:
            conds.append(Memory.tenant_id == tenant_id)
        if fleet_id:
            conds.append(Memory.fleet_id == fleet_id)
        if exclude_memory_types:
            conds.append(Memory.memory_type.notin_(exclude_memory_types))
        if exclude_agent_ids:
            conds.append(Memory.agent_id.notin_(exclude_agent_ids))
        if exclude_title_regex:
            conds.append(~func.coalesce(Memory.title, "").op("~*")(exclude_title_regex))
        # Bucket by UTC day: the report caller builds its day-keys in UTC
        # (datetime.now(UTC)), but bare date_trunc uses the PG session TimeZone —
        # a non-UTC session TZ would shift the buckets so every raw_counts.get()
        # misses and silently zeroes the whole trend. ``timezone('UTC', ...)``
        # normalizes the timestamptz to UTC wall-clock before truncating.
        day = func.date_trunc("day", func.timezone("UTC", Memory.created_at))
        stmt = select(day.label("d"), func.count().label("c")).where(*conds).group_by(day).order_by(day)
        async with get_read_session() as session:
            rows = (await session.execute(stmt)).all()
        return [{"day": r.d.date().isoformat(), "count": int(r.c)} for r in rows]

    async def memory_quality_metrics(
        self,
        *,
        tenant_id: str | None,
        fleet_id: str | None = None,
        agent_id: str | None = None,
        created_after: datetime | None = None,
        exclude_memory_types: list[str] | None = None,
        exclude_agent_ids: list[str] | None = None,
        exclude_title_regex: str | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> dict:
        """Reuse / recall quality aggregates over the SAME scoped corpus as
        ``memory_stats_breakdown`` (durable+cohesive when the exclude_* filters are
        passed). Backs the report Quality section:

        - ``by_type`` = ``{type: {total, reused}}`` → reuse RATE per type,
        - ``total`` / ``reused`` → never-recalled %,
        - ``total_recalls`` + ``top_recalls`` (top 6 values) → recall concentration.

        Kept separate from the GROUPING SETS breakdown so that shared (MCP stats)
        path stays untouched. The scope/visibility block below MUST mirror
        ``memory_stats_breakdown`` so the two report surfaces reconcile. Read-only
        (reader replica).
        """
        # ── Scope/visibility — MUST mirror memory_stats_breakdown. ──
        scope_filters = []
        if readable_tenant_ids:
            scope_filters.append(Memory.tenant_id.in_(readable_tenant_ids))
        else:
            scope_filters.append(Memory.tenant_id == tenant_id)
        if fleet_id:
            scope_filters.append(Memory.fleet_id == fleet_id)
        if agent_id:
            scope_filters.append(Memory.agent_id == agent_id)
            scope_filters.append(
                or_(
                    Memory.visibility == "scope_org",
                    Memory.visibility == "scope_team",
                    and_(Memory.visibility == "scope_agent", Memory.agent_id == agent_id),
                )
            )
        else:
            scope_filters.append(Memory.visibility != "scope_agent")
        if created_after:
            scope_filters.append(Memory.created_at >= created_after)
        if exclude_memory_types:
            scope_filters.append(Memory.memory_type.notin_(exclude_memory_types))
        if exclude_agent_ids:
            scope_filters.append(Memory.agent_id.notin_(exclude_agent_ids))
        if exclude_title_regex:
            scope_filters.append(~func.coalesce(Memory.title, "").op("~*")(exclude_title_regex))
        filters = [Memory.deleted_at.is_(None), *scope_filters]

        reused = case((Memory.recall_count > 0, 1), else_=0)
        per_type_stmt = (
            select(
                Memory.memory_type,
                func.count().label("n"),
                func.coalesce(func.sum(reused), 0).label("r"),
            )
            .where(*filters)
            .group_by(Memory.memory_type)
        )
        overall_stmt = select(
            func.count(),
            func.coalesce(func.sum(reused), 0),
            func.coalesce(func.sum(Memory.recall_count), 0),
        ).where(*filters)
        top_stmt = select(Memory.recall_count).where(*filters).order_by(Memory.recall_count.desc()).limit(6)
        async with get_read_session() as session:
            per_type = (await session.execute(per_type_stmt)).all()
            total, total_reused, total_recalls = (await session.execute(overall_stmt)).one()
            top_recalls = [int(x[0] or 0) for x in (await session.execute(top_stmt)).all()]
        return {
            "total": int(total or 0),
            "reused": int(total_reused or 0),
            "total_recalls": int(total_recalls or 0),
            "top_recalls": top_recalls,
            "by_type": {r.memory_type: {"total": int(r.n), "reused": int(r.r or 0)} for r in per_type},
        }

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

        Frontier-size cap (CAURA-000): on a dense relation graph (e.g. an
        enterprise tenant with tens of thousands of cross-linked entities)
        the BFS frontier grows multiplicatively each hop. The
        ``Relation.from/to_entity_id.in_(frontier)`` clause then becomes a
        SQL statement with a bind parameter per frontier entry. Customer
        log capture showed a single relations query reach **42,146 bind
        parameters** before failing — that exceeds asyncpg's safe window
        and crashed the request (the F4 500s observed on goodclaw / etoro
        06-07). We cap each hop's frontier at ``GRAPH_MAX_EXPANDED_ENTITIES``
        (200), keeping the **highest-weighted** edges so the most-relevant
        branches are preserved. Trade-off: low-weight branches past the
        cap are dropped from this hop's expansion (they may still appear
        via other paths). The ID tiebreak makes the selection deterministic
        across calls — the same query returns the same result twice.

        The downstream ``parallel_embed_entity_boost`` step applies the
        same cap defensively at the call boundary so a future regression
        here can't blow up ``get_memory_ids_by_entity_ids`` either.
        """
        async with get_session() as session:
            entity_hops: dict[UUID, tuple[int, float]] = dict.fromkeys(seed_entity_ids, (0, 1.0))
            frontier: set[UUID] | list[UUID] = set(seed_entity_ids)

            for hop in range(1, max_hops + 1):
                if not frontier:
                    break

                # Bound the IN-clause size BEFORE building the query. The
                # seed-set is small by construction (entity-FTS hits) but
                # subsequent hops can explode.
                if len(frontier) > GRAPH_MAX_EXPANDED_ENTITIES:
                    # Order by (weight desc, id asc) so the cap keeps the
                    # most-relevant edges deterministically. ``entity_hops``
                    # carries the weight assigned when this entity was
                    # first discovered (see end of loop) — seeds default to
                    # 1.0 so they're never dropped by the cap.
                    capped = sorted(
                        frontier,
                        key=lambda eid: (
                            -entity_hops.get(eid, (hop, 0.0))[1],
                            eid,
                        ),
                    )[:GRAPH_MAX_EXPANDED_ENTITIES]
                    logger.info(
                        "entity_expand_graph: frontier capped at %d (tenant=%s fleet=%s hop=%d dropped=%d)",
                        GRAPH_MAX_EXPANDED_ENTITIES,
                        tenant_id,
                        fleet_id,
                        hop,
                        len(frontier) - GRAPH_MAX_EXPANDED_ENTITIES,
                    )
                    frontier = capped

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

    # ------------------------------------------------------------------
    # Entity-linking pipeline (Fix 2 Ph6 — resolve / cross-links /
    # relation-inference / embedding-backfill, all routed off core-api's
    # direct DB access). Tuning constants travel in the request body
    # (storage must not import ``core_api``). SQL/ORM is ported VERBATIM
    # from the four ``core_api/pipeline/steps/entity_linking/*`` steps.
    # ------------------------------------------------------------------

    async def entity_resolve_duplicates(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        batch_size: int,
        threshold: float,
        candidate_limit: int,
    ) -> dict:
        """Merge duplicate entities whose name embeddings exceed ``threshold``.

        Folds the entire ``resolve_entities`` step into ONE ``get_session()``
        transaction so the per-dupe ``begin_nested()`` SAVEPOINT semantics — an
        HTTP boundary cannot express a SAVEPOINT — survive the move:

        * R1 pgvector LATERAL pair-find (read-your-writes on the SAME write
          session; NOT ``get_read_session`` — the merge loop re-reads rows it
          mutates).
        * union-find clustering (ports ``_find``/``_union`` verbatim via the
          module-level ``_entity_uf_*`` helpers).
        * per cluster: R2 load + canonical pick (longest name, smallest UUID on
          tie); per-cluster try/except continue-on-error.
        * per dupe: ``session.begin_nested()`` SAVEPOINT around R4-R13.

        Returns ``{merge_count, clusters, cluster_errors, merged_entity_ids}``
        mirroring the step's ``StepResult.detail``. Reproduces the "all clusters
        failed → error" branch as ``{"error": ...}`` in the dict (the caller
        maps it to a FAILED StepResult)."""
        fleet_clause = ""
        params: dict = {
            "tenant_id": tenant_id,
            "threshold": threshold,
            "batch_size": batch_size,
            "candidate_limit": candidate_limit,
        }
        if fleet_id is not None:
            fleet_clause = "AND fleet_id = :fleet_id"
            params["fleet_id"] = fleet_id

        pair_sql = text(f"""
            WITH batch AS (
                SELECT id, canonical_name, entity_type, name_embedding
                FROM entities
                WHERE tenant_id = :tenant_id
                  AND name_embedding IS NOT NULL
                  {fleet_clause}
                ORDER BY id
                LIMIT :batch_size
            )
            SELECT b.id AS id_a, nb.id AS id_b,
                   b.canonical_name AS name_a, nb.canonical_name AS name_b,
                   b.entity_type,
                   nb.sim
            FROM batch b
            JOIN LATERAL (
                SELECT e.id, e.canonical_name,
                       1 - (e.name_embedding <=> b.name_embedding) AS sim
                FROM entities e
                WHERE e.tenant_id = :tenant_id
                  AND e.name_embedding IS NOT NULL
                  AND e.id > b.id
                  AND e.entity_type = b.entity_type
                  {fleet_clause}
                  AND (1 - (e.name_embedding <=> b.name_embedding)) >= :threshold
                ORDER BY e.name_embedding <=> b.name_embedding
                LIMIT :candidate_limit
            ) nb ON true
        """)

        async with get_session() as session:
            rows = (await session.execute(pair_sql, params)).all()
            if not rows:
                # ``skipped`` lets the core-api step reproduce the source's
                # early ``StepResult(SKIPPED)`` ONLY for the no-pairs case
                # (the source returns SUCCESS(0) when pairs exist but nothing
                # merges).
                return {
                    "skipped": True,
                    "merge_count": 0,
                    "clusters": 0,
                    "cluster_errors": 0,
                    "merged_entity_ids": [],
                }

            # ── union-find clustering ──────────────────────────────────
            all_ids: set[UUID] = set()
            for r in rows:
                all_ids.add(r.id_a)
                all_ids.add(r.id_b)

            parent: dict[UUID, UUID] = {uid: uid for uid in all_ids}
            rank: dict[UUID, int] = dict.fromkeys(all_ids, 0)

            for r in rows:
                _entity_uf_union(parent, rank, r.id_a, r.id_b)

            clusters: dict[UUID, list[UUID]] = defaultdict(list)
            for uid in all_ids:
                clusters[_entity_uf_find(parent, uid)].append(uid)

            # ── Process each cluster ───────────────────────────────────
            merge_count = 0
            merged_ids: list[UUID] = []
            clusters_processed = 0
            cluster_errors = 0

            for root, cluster_ids in clusters.items():
                if len(cluster_ids) < 2:
                    continue

                try:
                    before = len(merged_ids)
                    await self._entity_merge_cluster(session, cluster_ids, merged_ids, tenant_id)
                    actual_merges = len(merged_ids) - before
                    merge_count += actual_merges
                    if actual_merges > 0:
                        clusters_processed += 1
                except Exception:
                    cluster_errors += 1
                    logger.exception(
                        "Failed to merge entity cluster root=%s (%d members)",
                        root,
                        len(cluster_ids),
                    )

            if clusters_processed == 0 and cluster_errors > 0:
                return {
                    "error": "all clusters failed to merge",
                    "cluster_errors": cluster_errors,
                }

            return {
                "merge_count": merge_count,
                "clusters": clusters_processed,
                "cluster_errors": cluster_errors,
                "merged_entity_ids": [str(eid) for eid in merged_ids],
            }

    async def _entity_merge_cluster(
        self,
        session: AsyncSession,
        cluster_ids: list[UUID],
        merged_ids: list[UUID],
        tenant_id: str,
    ) -> None:
        """Pick canonical entity and merge all duplicates into it."""

        # ── pick canonical (longest name, smallest UUID on tie) ──
        entities = (
            (
                await session.execute(
                    select(Entity).where(
                        Entity.id.in_(cluster_ids),
                        Entity.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        if not entities:
            return

        canonical = max(
            entities,
            key=lambda e: (len(e.canonical_name), -e.id.int),
        )
        dupes = [e for e in entities if e.id != canonical.id]

        # ── merge each duplicate (savepoint per dupe) ──
        for dupe in dupes:
            async with session.begin_nested():  # SAVEPOINT per dupe
                await self._entity_merge_dupe_into_canonical(session, canonical, dupe, tenant_id)
            merged_ids.append(dupe.id)

    async def _entity_merge_dupe_into_canonical(
        self,
        session: AsyncSession,
        canonical: Entity,
        dupe: Entity,
        tenant_id: str,
    ) -> None:
        """Re-point links/relations, merge aliases, delete duplicate."""
        db = session
        canonical_id = canonical.id
        dupe_id = dupe.id

        # 4a. Repoint MemoryEntityLink (scoped via memories.tenant_id) ──
        await db.execute(
            text("""
                DELETE FROM memory_entity_links
                WHERE entity_id = :dupe_id
                  AND memory_id IN (
                    SELECT mel.memory_id FROM memory_entity_links mel
                    JOIN memories m ON m.id = mel.memory_id
                      AND m.tenant_id = :tenant_id
                    WHERE mel.entity_id = :canonical_id
                  )
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        await db.execute(
            text("""
                UPDATE memory_entity_links
                SET entity_id = :canonical_id
                WHERE entity_id = :dupe_id
                  AND memory_id IN (
                    SELECT m.id FROM memories m
                    WHERE m.tenant_id = :tenant_id
                  )
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )

        # 4b. Repoint Relations (from_entity_id) ───────────────────────
        # Preserve the higher weight before deleting conflicting dupe relations.
        await db.execute(
            text("""
                UPDATE relations r_canonical
                SET weight = GREATEST(r_canonical.weight, r_dupe.weight)
                FROM relations r_dupe
                WHERE r_dupe.from_entity_id = :dupe_id
                  AND r_dupe.tenant_id = :tenant_id
                  AND r_canonical.from_entity_id = :canonical_id
                  AND r_canonical.tenant_id = :tenant_id
                  AND r_canonical.relation_type = r_dupe.relation_type
                  AND r_canonical.to_entity_id = r_dupe.to_entity_id
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        # Delete dupe's outgoing relations that would become self-loops
        # (dupe→canonical) or duplicates of canonical's existing relations.
        await db.execute(
            text("""
                DELETE FROM relations
                WHERE from_entity_id = :dupe_id
                  AND tenant_id = :tenant_id
                  AND (
                    to_entity_id = :canonical_id
                    OR (tenant_id, relation_type, to_entity_id) IN (
                        SELECT tenant_id, relation_type, to_entity_id
                        FROM relations WHERE from_entity_id = :canonical_id
                          AND tenant_id = :tenant_id
                    )
                  )
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        await db.execute(
            text("""
                UPDATE relations
                SET from_entity_id = :canonical_id
                WHERE from_entity_id = :dupe_id
                  AND tenant_id = :tenant_id
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )

        # 4c. Repoint Relations (to_entity_id) ─────────────────────────
        # Preserve the higher weight before deleting conflicting dupe relations.
        await db.execute(
            text("""
                UPDATE relations r_canonical
                SET weight = GREATEST(r_canonical.weight, r_dupe.weight)
                FROM relations r_dupe
                WHERE r_dupe.to_entity_id = :dupe_id
                  AND r_dupe.tenant_id = :tenant_id
                  AND r_canonical.to_entity_id = :canonical_id
                  AND r_canonical.tenant_id = :tenant_id
                  AND r_canonical.from_entity_id = r_dupe.from_entity_id
                  AND r_canonical.relation_type = r_dupe.relation_type
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        # Delete dupe's incoming relations that would become self-loops
        # (canonical→dupe) or duplicates of canonical's existing relations.
        await db.execute(
            text("""
                DELETE FROM relations
                WHERE to_entity_id = :dupe_id
                  AND tenant_id = :tenant_id
                  AND (
                    from_entity_id = :canonical_id
                    OR (tenant_id, from_entity_id, relation_type) IN (
                        SELECT tenant_id, from_entity_id, relation_type
                        FROM relations WHERE to_entity_id = :canonical_id
                          AND tenant_id = :tenant_id
                    )
                  )
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        await db.execute(
            text("""
                UPDATE relations
                SET to_entity_id = :canonical_id
                WHERE to_entity_id = :dupe_id
                  AND tenant_id = :tenant_id
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )

        # 4d. Merge aliases ─────────────────────────────────────────────
        canonical_attrs = dict(canonical.attributes or {})
        dupe_attrs = dict(dupe.attributes or {})
        aliases: set[str] = set(canonical_attrs.get("_aliases", []))
        aliases.add(canonical.canonical_name)
        aliases.add(dupe.canonical_name)
        aliases.update(dupe_attrs.get("_aliases", []))
        canonical_attrs["_aliases"] = sorted(aliases)  # sorted for determinism
        canonical.attributes = canonical_attrs

        # 4e. Delete duplicate entity ──────────────────────────────────
        await db.delete(dupe)

    async def entity_discover_cross_links(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        batch_size: int,
        threshold: float,
        text_verify: bool,
        target_memory_ids: list | None,
    ) -> dict:
        """Link under-connected memories to similar entities (both modes).

        Folds D1/D2 candidate-find + D3 pgvector LATERAL + the Python
        text-verify filter + D4 bulk ON-CONFLICT insert into ONE
        ``get_session()`` transaction. ``target_memory_ids`` (non-empty) selects
        targeted mode (D1); otherwise batch mode (D2). Returns
        ``{links_created}``.

        D4 keeps the single multi-VALUES ``pg_insert(...).values(rows)`` form
        (CAURA-686) — NOT ``execute(stmt, rows)`` (executemany kills RETURNING)."""
        # ── 1. Find candidate memories ──────────────────────────────
        fleet_clause = "AND m.fleet_id = :fleet_id" if fleet_id else ""

        async with get_session() as session:
            if target_memory_ids:
                # Targeted mode: specific memories (e.g. after entity extraction)
                candidates = (
                    await session.execute(
                        text(f"""
                            SELECT m.id, m.content, m.embedding
                            FROM memories m
                            WHERE m.id = ANY(CAST(:memory_ids AS uuid[]))
                              AND m.tenant_id = :tenant_id
                              AND m.deleted_at IS NULL
                              AND m.status = 'active'
                              AND m.embedding IS NOT NULL
                              {fleet_clause}
                        """),
                        {
                            "tenant_id": tenant_id,
                            "memory_ids": [str(mid) for mid in target_memory_ids],
                            **({"fleet_id": fleet_id} if fleet_id else {}),
                        },
                    )
                ).all()
            else:
                # Batch mode: under-connected memories (lifecycle / scheduled)
                candidates = (
                    await session.execute(
                        text(f"""
                            SELECT m.id, m.content, m.embedding
                            FROM memories m
                            LEFT JOIN memory_entity_links mel ON mel.memory_id = m.id
                            WHERE m.tenant_id = :tenant_id
                              AND m.deleted_at IS NULL
                              AND m.status = 'active'
                              AND m.embedding IS NOT NULL
                              {fleet_clause}
                            GROUP BY m.id
                            HAVING COUNT(mel.entity_id) < 3
                            ORDER BY m.created_at DESC
                            LIMIT :batch_size
                        """),
                        {
                            "tenant_id": tenant_id,
                            **({"fleet_id": fleet_id} if fleet_id else {}),
                            "batch_size": batch_size,
                        },
                    )
                ).all()

            if not candidates:
                # ``skipped`` so the step can reproduce the source's
                # StepOutcome.SKIPPED on the no-candidates case (parity with
                # resolve/backfill; keeps pipeline skipped_count accurate).
                return {"skipped": True, "links_created": 0}

            # ── 2. Find similar entities for all candidate memories (LATERAL JOIN) ──
            entity_fleet_clause = "AND e.fleet_id = :fleet_id" if fleet_id else ""
            memory_id_strs = [str(row[0]) for row in candidates]
            # ``content`` is only consulted by the text-verify filter below; skip
            # building the map (and holding every candidate's content in memory)
            # when text-verify is off.
            content_map = {row[0]: row[1] for row in candidates} if text_verify else {}

            lateral_query = text(f"""
                SELECT m.id AS memory_id,
                       e.id AS entity_id, e.canonical_name, e.attributes, e.sim
                FROM (SELECT id, embedding FROM memories
                      WHERE id = ANY(CAST(:memory_ids AS uuid[])) AND tenant_id = :tenant_id) m
                JOIN LATERAL (
                    SELECT e.id, e.canonical_name, e.attributes,
                           1 - (e.name_embedding <=> m.embedding) AS sim
                    FROM entities e
                    WHERE e.tenant_id = :tenant_id
                      AND e.name_embedding IS NOT NULL
                      AND (1 - (e.name_embedding <=> m.embedding)) >= :threshold
                      {entity_fleet_clause}
                    ORDER BY e.name_embedding <=> m.embedding
                    LIMIT 10
                ) e ON true
                ORDER BY m.id, e.sim DESC
            """)

            lateral_rows = (
                await session.execute(
                    lateral_query,
                    {
                        "tenant_id": tenant_id,
                        "memory_ids": memory_id_strs,
                        "threshold": threshold,
                        **({"fleet_id": fleet_id} if fleet_id else {}),
                    },
                )
            ).all()

            # Filter candidates in Python, then bulk-insert
            to_insert: list[dict] = []
            for memory_id, entity_id, canonical_name, attributes, _sim in lateral_rows:
                if text_verify:
                    content = content_map.get(memory_id, "")
                    names_to_check = [canonical_name]
                    if attributes and isinstance(attributes, dict):
                        names_to_check.extend(attributes.get("_aliases", []))
                    content_lower = content.lower() if content else ""
                    if not any(n.lower() in content_lower for n in names_to_check):
                        continue
                to_insert.append({"memory_id": memory_id, "entity_id": entity_id})

            links_created = 0
            if to_insert:
                # Single multi-VALUES statement via ``pg_insert(...).values(rows)``
                # (the CAURA-686 pattern) — NOT ``execute(stmt, rows)``, which
                # takes SQLAlchemy's executemany path where RETURNING rows are
                # unavailable and ``result.all()`` raises ResourceClosedError.
                # memory_entity_links has a composite PK (memory_id, entity_id)
                # and no surrogate ``id`` column, so RETURNING must reference
                # real columns; with ON CONFLICT DO NOTHING only actually-
                # inserted rows return, keeping the count accurate.
                rows = [{**row, "role": "mentioned"} for row in to_insert]
                insert_link_returning = (
                    pg_insert(MemoryEntityLink)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["memory_id", "entity_id"])
                    .returning(MemoryEntityLink.memory_id, MemoryEntityLink.entity_id)
                )
                result = await session.execute(insert_link_returning)
                links_created = len(result.all())

            logger.info(
                "Created %d cross-links for %d candidate memories (tenant %s)",
                links_created,
                len(candidates),
                tenant_id,
            )
            return {"links_created": links_created}

    async def entity_infer_relations(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        batch_size: int,
        min_cooccurrence: int,
        reinforce_delta: float,
        max_relation_weight: float,
    ) -> dict:
        """Infer 'related_to' relations from entity co-occurrence.

        Folds I1 co-occurrence + I2 existing-relations + the reinforce-vs-create
        Python split + I3 reinforce UPDATE + I4 ON-CONFLICT INSERT into ONE
        ``get_session()`` transaction. Tuning (``min_cooccurrence``,
        ``reinforce_delta``, ``max_relation_weight``) arrives in the body.

        I3 binds the Python-clamped ``:new_weight`` directly — NOT
        ``LEAST(:a,:b)`` over untyped binds (asyncpg DatatypeMismatchError, prod
        2026-06-13). Returns ``{relations_created, relations_reinforced}``."""
        # ── 1. Co-occurrence query ────────────────────────────────────
        entity_fleet_clause = "AND fleet_id = :fleet_id" if fleet_id else ""
        memory_fleet_clause = "AND mem.fleet_id = :fleet_id" if fleet_id else ""
        async with get_session() as session:
            cooccurrences = (
                await session.execute(
                    text(f"""
                        WITH tenant_entity_ids AS (
                            SELECT id FROM entities
                            WHERE tenant_id = :tenant_id
                              {entity_fleet_clause}
                        )
                        SELECT a.entity_id AS from_id, b.entity_id AS to_id,
                               COUNT(*) AS cooccur
                        FROM memory_entity_links a
                        JOIN memory_entity_links b
                          ON a.memory_id = b.memory_id
                          AND a.entity_id < b.entity_id
                        JOIN memories mem
                          ON mem.id = a.memory_id
                          AND mem.tenant_id = :tenant_id
                          AND mem.deleted_at IS NULL
                          {memory_fleet_clause}
                        WHERE a.entity_id IN (SELECT id FROM tenant_entity_ids)
                          AND b.entity_id IN (SELECT id FROM tenant_entity_ids)
                        GROUP BY a.entity_id, b.entity_id
                        HAVING COUNT(*) >= :min_cooccurrence
                        ORDER BY cooccur DESC
                        LIMIT :batch_size
                    """),
                    {
                        "tenant_id": tenant_id,
                        **({"fleet_id": fleet_id} if fleet_id else {}),
                        "min_cooccurrence": min_cooccurrence,
                        "batch_size": batch_size,
                    },
                )
            ).all()

            if not cooccurrences:
                # ``skipped`` so the step reproduces the source's SKIPPED on the
                # no-co-occurrence case (parity with resolve/backfill).
                return {"skipped": True, "relations_created": 0, "relations_reinforced": 0}

            # ── 2. Bulk-fetch existing 'related_to' relations for all pairs ─
            # Scoped by tenant_id only (not fleet_id) so fleet-scoped runs can
            # reinforce relations created by full runs; the unique constraint
            # uq_relations_natural_key does not include fleet_id.
            all_entity_ids = {eid for row in cooccurrences for eid in (row[0], row[1])}
            existing_rows = (
                await session.execute(
                    text("""
                        SELECT from_entity_id, to_entity_id, id, weight
                        FROM relations
                        WHERE tenant_id = :tenant_id
                          AND relation_type = 'related_to'
                          AND (from_entity_id = ANY(CAST(:ids AS uuid[])) OR to_entity_id = ANY(CAST(:ids AS uuid[])))
                    """),
                    {
                        "tenant_id": tenant_id,
                        "ids": [str(eid) for eid in all_entity_ids],
                    },
                )
            ).all()

            # Build lookup: frozenset({from_id, to_id}) -> (rel_id, weight)
            existing_map: dict[frozenset, tuple] = {
                frozenset({r[0], r[1]}): (r[2], r[3]) for r in existing_rows
            }

            # ── 3. Split into reinforce vs. create batches ────────────────
            reinforce_batch: list[dict] = []
            insert_batch: list[dict] = []

            for from_id, to_id, cooccur in cooccurrences:
                pair_key = frozenset({from_id, to_id})
                existing = existing_map.get(pair_key)

                if existing:
                    rel_id, current_weight = existing
                    new_weight = min(
                        current_weight + cooccur * reinforce_delta,
                        max_relation_weight,
                    )
                    reinforce_batch.append(
                        {
                            "rel_id": rel_id,
                            # Already clamped to max_relation_weight above — bound
                            # directly below (no SQL-side LEAST), matching the
                            # INSERT path's ``:weight``.
                            "new_weight": new_weight,
                            "tenant_id": tenant_id,
                        }
                    )
                else:
                    weight = min(cooccur * reinforce_delta, max_relation_weight)
                    insert_batch.append(
                        {
                            "tenant_id": tenant_id,
                            "fleet_id": fleet_id,
                            "from_id": from_id,
                            "to_id": to_id,
                            "weight": weight,
                        }
                    )

            # ── 4. Execute batched UPDATEs ────────────────────────────────
            relations_reinforced = 0
            if reinforce_batch:
                await session.execute(
                    # ``SET weight = :new_weight`` NOT ``LEAST(:new_weight, :max_weight)``:
                    # Postgres resolves ``LEAST`` over two untyped bind params as
                    # ``text`` and then rejects the assignment to the
                    # double-precision ``weight`` column (asyncpg
                    # DatatypeMismatchError, prod 2026-06-13). Direct assignment
                    # infers the column type from context; new_weight is already
                    # clamped in Python.
                    text("""
                        UPDATE relations
                        SET weight = :new_weight
                        WHERE id = :rel_id AND tenant_id = :tenant_id
                    """),
                    reinforce_batch,
                )
                relations_reinforced = len(reinforce_batch)

            # ── 5. Execute batched INSERTs ────────────────────────────────
            relations_created = 0
            if insert_batch:
                result = await session.execute(
                    text("""
                        INSERT INTO relations
                            (tenant_id, fleet_id, from_entity_id, relation_type,
                             to_entity_id, weight)
                        VALUES
                            (:tenant_id, :fleet_id, :from_id, 'related_to',
                             :to_id, :weight)
                        ON CONFLICT ON CONSTRAINT uq_relations_natural_key
                        DO NOTHING
                    """),
                    insert_batch,
                )
                rc = result.rowcount  # type: ignore[attr-defined]
                relations_created = rc if rc >= 0 else len(insert_batch)

            logger.info(
                "Inferred relations for tenant %s: created=%d reinforced=%d",
                tenant_id,
                relations_created,
                relations_reinforced,
            )
            return {
                "relations_created": relations_created,
                "relations_reinforced": relations_reinforced,
            }

    async def entity_list_null_embeddings(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        batch_size: int,
    ) -> list[dict]:
        """Entities whose ``name_embedding`` is NULL (read half of backfill).

        Ports B1 verbatim. Read-only → ``get_read_session()``. Returns
        ``[{id, canonical_name}, ...]`` for core-api's LLM embed loop."""
        fleet_clause = "AND fleet_id = :fleet_id" if fleet_id else ""
        async with get_read_session() as session:
            rows = (
                await session.execute(
                    text(f"""
                        SELECT id, canonical_name
                        FROM entities
                        WHERE tenant_id = :tenant_id
                          AND name_embedding IS NULL
                          {fleet_clause}
                        LIMIT :batch_size
                    """),
                    {
                        "tenant_id": tenant_id,
                        **({"fleet_id": fleet_id} if fleet_id else {}),
                        "batch_size": batch_size,
                    },
                )
            ).all()
        return [{"id": str(eid), "canonical_name": canonical_name} for eid, canonical_name in rows]

    async def entity_set_embeddings(
        self,
        *,
        tenant_id: str,
        updates: list[dict],
    ) -> int:
        """Write back computed name embeddings (write half of backfill).

        Ports B3 verbatim: a Core ``update(Entity.__table__)`` executemany — NOT
        ``update(Entity)`` (ORM bulk-by-PK requires ``id`` in each dict →
        InvalidRequestError, prod 2026-06-16). Each update is
        ``{"id": <uuid str>, "embedding": [float, ...]}``; tenant-scoped. Returns the
        count of rows written (``len(updates)``)."""
        if not updates:
            return 0
        params = [{"eid": UUID(u["id"]), "emb": u["embedding"]} for u in updates]
        async with get_session() as session:
            await session.execute(
                # Target the Core ``entities`` table, NOT the ORM-mapped ``Entity``.
                # ``session.execute(update(Entity), <list of param dicts>)`` routes to
                # SQLAlchemy's "ORM Bulk UPDATE by Primary Key", which requires every
                # dict to carry the PK column ``id`` — but our dicts key the PK off a
                # custom ``eid`` bindparam in the WHERE clause, so that path raised
                # ``InvalidRequestError: No primary key value supplied for column(s)
                # entities.id`` (prod 2026-06-16). ``update(Entity.__table__)`` is a
                # plain Core executemany UPDATE that honours the custom bindparams and
                # has no ORM bulk-by-PK or session-synchronisation behaviour at all.
                sql_update(Entity.__table__)
                .where(
                    Entity.__table__.c.id == bindparam("eid"),
                    Entity.__table__.c.tenant_id == tenant_id,
                )
                .values(name_embedding=bindparam("emb")),
                params,
            )
        return len(updates)

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
        embedding: list[float] | None = None,
        system: bool = False,
    ) -> tuple:
        """Upsert and return (id, created_at, updated_at, xmax) for MCP callers.

        ``embedding`` is opt-in — callers that skip it leave the column
        ``NULL`` and the doc won't participate in semantic search. Upsert
        always writes the embedding column, so passing ``None`` on a
        re-write will clear a previously-indexed doc (intentional — the
        caller chose not to index this version).

        Mirrors ``document_upsert``'s system-collection guard: writes to
        ``_``-prefixed (system-managed, e.g. ``_keystones``) collections
        require ``system=True``, so the public endpoint can't reach them.
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
                    embedding=embedding,
                )
                .on_conflict_do_update(
                    constraint="uq_documents_tenant_collection_doc",
                    set_={
                        "data": data,
                        "fleet_id": fleet_id,
                        "embedding": embedding,
                        "updated_at": text("now()"),
                    },
                )
                .returning(Document.id, Document.created_at, Document.updated_at, text("xmax"))
            )
            result = await session.execute(stmt)
            return result.one()  # type: ignore[return-value]

    async def document_list_collections(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[tuple[str, int]]:
        """Enumerate collections a tenant has written to, with per-collection
        document counts.

        Returns rows of ``(collection, count)`` sorted alphabetically by
        collection name. If ``fleet_id`` is supplied, only documents matching
        that fleet are counted; otherwise counts span every fleet within the
        tenant.

        ``readable_tenant_ids`` widens to ``ANY($readable)`` — counts then
        span every collection across the readable set (collections with the
        same name across multiple tenants merge into one row).
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        stmt = (
            select(Document.collection, func.count().label("count"))
            .where(tenant_pred)
            .group_by(Document.collection)
            .order_by(Document.collection)
        )
        if fleet_id:
            stmt = stmt.where(Document.fleet_id == fleet_id)
        async with get_read_session() as session:
            result = await session.execute(stmt)
            # Positional access: ``row.count`` resolves to ``Row.count()`` (the
            # tuple method) under the type checker; index by position instead.
            return [(row[0], int(row[1])) for row in result.all()]

    async def document_count_in_collection(
        self,
        *,
        tenant_id: str,
        collection: str,
        status: str | None = None,
        fleet_id: str | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> int:
        """Count documents in one collection, optionally filtered by a
        ``data->>'status'`` value.

        Backs the MCP ``list_collections`` skills active-only count correction
        (the server-owned active-only gate): an opted-in tenant's listing must
        not advertise non-active skills in the count. ``readable_tenant_ids``
        widens to ``ANY($readable)`` over the same scope the listing used.
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        stmt = (
            select(func.count()).select_from(Document).where(tenant_pred, Document.collection == collection)
        )
        if status is not None:
            stmt = stmt.where(Document.data["status"].astext == status)
        if fleet_id:
            stmt = stmt.where(Document.fleet_id == fleet_id)
        async with get_read_session() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def document_search(
        self,
        *,
        tenant_id: str,
        query_embedding: list[float],
        collection: str | None = None,
        top_k: int = 5,
        fleet_id: str | None = None,
        readable_tenant_ids: list[str] | None = None,
        status: str | None = None,
    ) -> list[tuple[Document, float]]:
        """Semantic search over docs — scoped or cross-collection.

        If ``collection`` is supplied, search is restricted to that
        collection (narrow / strategy 1). If ``collection`` is ``None``,
        search spans every collection in the tenant (broad / strategy 2).
        Only rows with ``embedding IS NOT NULL`` are considered.

        ``readable_tenant_ids`` widens to ``ANY($readable)`` — semantic
        search then spans every document across the readable set, sorted
        by global cosine distance.

        ``status`` (optional) adds a ``data->>'status' = :status``
        equality filter. Returns ``(Document, similarity)`` pairs where
        ``similarity = 1 - cosine_distance``.
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        distance = Document.embedding.cosine_distance(query_embedding)
        stmt = (
            select(Document, distance.label("distance"))
            .where(
                tenant_pred,
                Document.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(max(top_k, 1))
        )
        if collection is not None:
            stmt = stmt.where(Document.collection == collection)
        if fleet_id:
            stmt = stmt.where(Document.fleet_id == fleet_id)
        if status is not None:
            stmt = stmt.where(Document.data["status"].astext == status)
        async with get_read_session() as session:
            result = await session.execute(stmt)
            return [(row.Document, 1.0 - float(row.distance)) for row in result.all()]

    async def document_get_by_doc_id(
        self,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
        readable_tenant_ids: list[str] | None = None,
    ) -> Document | None:
        """Fetch one document by (tenant, collection, doc_id).

        ``readable_tenant_ids`` widens the tenant predicate to
        ``ANY($readable)`` so cross-tenant credentials can read docs from
        sibling tenants; ``tenant_id`` stays the binding/home tenant.
        Mirrors core-api ``document_repository.get_by_doc_id``.
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        async with get_session() as session:
            stmt = select(Document).where(
                tenant_pred,
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
        readable_tenant_ids: list[str] | None = None,
    ) -> list[Document]:
        """Query documents with optional JSONB field-equality filters.

        ``readable_tenant_ids`` widens ``tenant_id`` to ``ANY($readable)``
        for cross-tenant credentials. Mirrors core-api
        ``document_repository.query``.
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        async with get_session() as session:
            stmt = select(Document).where(
                tenant_pred,
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
        require_status: str | None = None,
    ) -> UUID | None:
        """Delete by (tenant_id, collection, doc_id). Returns the deleted id or None.

        Mirrors the ``system`` guard on ``document_upsert`` — deletes against
        system-managed collections (``_``-prefixed) require ``system=True``.

        ``require_status`` (optional) folds a ``data->>'status' = :status``
        guard directly into the DELETE's WHERE so the check and the delete are
        a single atomic statement — no TOCTOU window. Backs the MCP skills
        active-only delete gate: a non-matching (or missing) doc deletes zero
        rows and returns ``None``, indistinguishable from a missing one (no
        existence leak). Home-tenant scoped (deletes never span readable
        tenants).
        """
        if collection.startswith("_") and not system:
            raise ValueError(f"Collection '{collection}' is system-managed; use the dedicated endpoint.")
        async with get_session() as session:
            base = delete(Document).where(
                Document.tenant_id == tenant_id,
                Document.collection == collection,
                Document.doc_id == doc_id,
            )
            if require_status is not None:
                base = base.where(Document.data["status"].astext == require_status)
            stmt = base.returning(Document.id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def document_update_status(
        self,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
        new_status: str,
        expected_status: str,
    ) -> bool:
        """Conditional (CAS) status flip on one document's ``data`` jsonb.

        Ports ``skill_promoter.make_db_status_updater._update`` verbatim:
        narrows the UPDATE on the EXPECTED source status so a concurrent
        writer that already transitioned the row matches zero rows. Stamps
        ``<new_status>_at`` alongside the status flip (mirroring
        ``routes/skills_inbox._persist_status_transition``) so a worker-driven
        promotion leaves a ``staged_at`` / ``active_at`` timestamp the
        "promoted age" queries rely on.

        Returns ``True`` when a row matched and was updated, ``False`` when
        the CAS missed (no row with ``data->>'status' = expected_status``).
        The route translates ``False`` to a 404 so core-api raises
        ``AlreadyTransitionedError``.
        """
        at_key = f"{new_status}_at"
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE documents
                    SET data = jsonb_set(
                                   jsonb_set(data::jsonb, '{status}', to_jsonb(CAST(:new_status AS text))),
                                   ARRAY[:at_key],
                                   to_jsonb(CAST(:now_iso AS text))
                               )::json
                    WHERE tenant_id = :tenant_id
                      AND collection = :collection
                      AND doc_id     = :doc_id
                      AND (data->>'status') = :expected_status
                    RETURNING doc_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "collection": collection,
                    "doc_id": doc_id,
                    "new_status": new_status,
                    "expected_status": expected_status,
                    "at_key": at_key,
                    "now_iso": now_iso,
                },
            )
            return result.fetchone() is not None

    # ══════════════════════════════════════════════════════════════════════
    #  SKILL FACTORY — forge poison, session traces, outcome-signal reads
    #  (Fix 2 Ph5a)
    # ══════════════════════════════════════════════════════════════════════
    #
    # These port the raw PG SQL from the core-api skill-factory pipeline
    # (services/forge/poison.py, services/session_trace.py,
    # services/outcome_inference/*) VERBATIM — DISTINCT ON, the self-join on
    # supersedes_id, the 3-arm fleet predicate, INTERVAL '1 day' * cooloff_days,
    # ANY(CAST(:ids AS uuid[])), ON CONFLICT ... DO UPDATE, RETURNING. Each
    # takes an explicit tenant_id (+ fleet/window/params); there are no RLS
    # GUCs server-side.

    async def forge_write_rejected_fingerprint(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        cluster_fingerprint: str,
        rejected_by_agent: str,
        cooloff_days: int,
        reason: str | None = None,
    ) -> str:
        """Insert one ``forge_rejected_fingerprints`` row; return its id.

        Ports ``forge/poison.write_rejected_fingerprint`` verbatim. The
        ValueError guards (empty fingerprint / cooloff_days < 1) stay on the
        core-api side so the existing route's 422 contract is unchanged; this
        method trusts its inputs.
        """
        async with get_session() as session:
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO forge_rejected_fingerprints
                            (tenant_id, fleet_id, cluster_fingerprint,
                             rejected_by_agent, cooloff_days, reason)
                        VALUES
                            (:tenant_id, :fleet_id, :cluster_fingerprint,
                             :rejected_by_agent, :cooloff_days, :reason)
                        RETURNING id::text AS id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "cluster_fingerprint": cluster_fingerprint,
                        "rejected_by_agent": rejected_by_agent,
                        "cooloff_days": cooloff_days,
                        "reason": reason,
                    },
                )
            ).fetchone()
            return row.id if row else "unknown"

    async def forge_is_fingerprint_poisoned(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        cluster_fingerprint: str,
    ) -> bool:
        """Return True iff a live cooloff row exists for this (tenant, fleet,
        fp) triple. Ports ``forge/poison.is_fingerprint_poisoned`` verbatim,
        including the 3-arm fleet predicate and the
        ``rejected_at + (interval '1 day' * cooloff_days) > now()`` window.

        Reads the PRIMARY (``get_session``), not a replica: this is a write-path
        guard — the forge tick gates candidate creation on it, so it must see a
        just-committed rejection. A replica-lag miss would let a freshly rejected
        cluster be re-proposed. Mirrors ``memory_find_by_content_hash``; the pure
        analytics reads below stay on the replica.
        """
        async with get_session() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT 1
                        FROM forge_rejected_fingerprints
                        WHERE tenant_id = :tenant_id
                          AND cluster_fingerprint = :cluster_fingerprint
                          AND (
                              fleet_id IS NULL
                              OR CAST(:fleet_id AS text) IS NULL
                              OR fleet_id = :fleet_id
                          )
                          AND rejected_at + (interval '1 day' * cooloff_days) > now()
                        LIMIT 1
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "cluster_fingerprint": cluster_fingerprint,
                    },
                )
            ).fetchone()
            return row is not None

    async def session_traces_upsert(self, *, tenant_id: str, traces: list[dict]) -> None:
        """Batch-upsert ``session_traces`` rows keyed by
        ``(tenant_id, run_id, agent_id)``. Ports
        ``session_trace._upsert_session_traces`` verbatim (one statement per
        row, jsonb-cast binds). Every trace's ``tenant_id`` is forced to the
        batch-level ``tenant_id`` so a caller can't smuggle a foreign tenant
        into the batch.
        """
        if not traces:
            return
        sql = """
            INSERT INTO session_traces (
                tenant_id, fleet_id, run_id, agent_id,
                outcome_label, memory_ids, entity_ids,
                signals_summary, goal_phrase, started_at, ended_at
            )
            VALUES (
                :tenant_id, :fleet_id, :run_id, :agent_id,
                :outcome_label,
                CAST(:memory_ids AS jsonb), CAST(:entity_ids AS jsonb),
                CAST(:signals_summary AS jsonb), :goal_phrase,
                :started_at, :ended_at
            )
            ON CONFLICT (tenant_id, run_id, agent_id) DO UPDATE SET
                fleet_id         = EXCLUDED.fleet_id,
                outcome_label    = EXCLUDED.outcome_label,
                memory_ids       = EXCLUDED.memory_ids,
                entity_ids       = EXCLUDED.entity_ids,
                signals_summary  = EXCLUDED.signals_summary,
                goal_phrase      = EXCLUDED.goal_phrase,
                started_at       = EXCLUDED.started_at,
                ended_at         = EXCLUDED.ended_at
        """
        async with get_session() as session:
            for trace in traces:
                bind = {
                    "tenant_id": tenant_id,
                    "fleet_id": trace.get("fleet_id"),
                    "run_id": trace["run_id"],
                    "agent_id": trace["agent_id"],
                    "outcome_label": trace["outcome_label"],
                    # JSONB params need to be JSON strings for asyncpg. The
                    # caller may hand either a python object or a pre-dumped
                    # string; normalise to a string here.
                    "memory_ids": _as_json_str(trace.get("memory_ids", [])),
                    "entity_ids": _as_json_str(trace.get("entity_ids", [])),
                    "signals_summary": _as_json_str(trace.get("signals_summary", {})),
                    "goal_phrase": trace.get("goal_phrase"),
                    "started_at": _coerce_dt(trace["started_at"]),
                    "ended_at": _coerce_dt(trace["ended_at"]),
                }
                await session.execute(text(sql), bind)

    async def session_memories_in_window(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> list[dict]:
        """Return run-scoped memories in the window for trace enumeration.

        Ports ``session_trace._query_memories_in_window`` verbatim. Rows
        carry ``memory_id, run_id, agent_id, fleet_id, created_at``; the
        builder groups them by ``(run_id, agent_id)``.
        """
        sql = """
            SELECT
                m.id           AS memory_id,
                m.run_id       AS run_id,
                m.agent_id     AS agent_id,
                m.fleet_id     AS fleet_id,
                m.created_at   AS created_at
            FROM memories AS m
            WHERE m.tenant_id = :tenant_id
              AND m.created_at >= :w_start
              AND m.created_at <  :w_end
              AND m.run_id IS NOT NULL
              AND (CAST(:fleet_id AS text) IS NULL OR m.fleet_id = :fleet_id OR m.fleet_id IS NULL)
            ORDER BY m.run_id, m.agent_id, m.created_at ASC
        """
        async with get_read_session() as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "w_start": window_start,
                        "w_end": window_end,
                    },
                )
            ).fetchall()
        return [
            {
                "memory_id": str(r.memory_id),
                "run_id": r.run_id,
                "agent_id": r.agent_id,
                "fleet_id": r.fleet_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    async def memory_entity_links_batch(self, *, tenant_id: str, memory_ids: list[str]) -> list[dict]:
        """Return ``(memory_id, entity_id)`` pairs for a batch of memory ids.

        Ports ``session_trace._query_entity_ids_for_memories`` — casts the
        PARAMETER to ``uuid[]`` (NOT the column to text) so the index on
        ``memory_entity_links.memory_id`` stays eligible. Scoped to
        ``tenant_id`` via a join on ``memories`` (the link table has no
        tenant column) so the HTTP boundary can't return another tenant's
        links for a smuggled id. Empty input → empty list (no query issued).
        """
        if not memory_ids:
            return []
        sql = """
            SELECT mel.memory_id::text AS memory_id, mel.entity_id::text AS entity_id
            FROM memory_entity_links AS mel
            JOIN memories AS m ON m.id = mel.memory_id AND m.tenant_id = :tenant_id
            WHERE mel.memory_id = ANY(CAST(:memory_ids AS uuid[]))
        """
        async with get_read_session() as session:
            rows = (
                await session.execute(text(sql), {"tenant_id": tenant_id, "memory_ids": list(memory_ids)})
            ).fetchall()
        return [{"memory_id": r.memory_id, "entity_id": r.entity_id} for r in rows]

    async def memory_content_by_ids(self, *, tenant_id: str, memory_ids: list[str]) -> list[dict]:
        """Bulk-load ``(id, content)`` by memory id. Ports
        ``forge/cron_handler._make_memory_fetcher._fetch`` — the param-cast
        (text[] → uuid[]) preserves the btree index on ``memories.id``.
        Scoped to ``tenant_id`` so the HTTP boundary can't return another
        tenant's content for a smuggled id. Empty input → empty list.
        """
        if not memory_ids:
            return []
        sql = (
            "SELECT id::text AS id, content FROM memories "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND tenant_id = :tenant_id"
        )
        async with get_read_session() as session:
            rows = (
                await session.execute(text(sql), {"ids": list(memory_ids), "tenant_id": tenant_id})
            ).fetchall()
        return [{"id": r.id, "content": r.content if r.content is not None else ""} for r in rows]

    async def outcome_contradiction_signals(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        window_start: datetime,
        window_end: datetime,
        contradicted_statuses: list[str],
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """Contradicted memories whose status flip falls in the window.

        Ports ``outcome_inference/contradictions.extract`` (``status =
        ANY(:contradicted_statuses)`` + a window on the status-transition
        time).

        DEVIATION (Fix 2 Ph5a): the source SQL windowed on
        ``COALESCE(m.updated_at, m.created_at)``, but the OSS ``memories``
        table has NO ``updated_at`` column (verified against
        ``001_initial_schema`` + the ``Memory`` model — only ``agents`` /
        ``documents`` carry one). That reference was a latent bug against the
        OSS schema (the extractor was only ever unit-tested with a mocked
        ``db``, never executed against a real OSS DB). We window on
        ``created_at`` instead — the faithful OSS-correct approximation of the
        same intent. When a ``status_changed_at`` / ``updated_at`` column lands
        (the CAURA-future the source docstring anticipates), swap it back in.
        """
        sql = """
            SELECT
                m.id          AS memory_id,
                m.run_id      AS run_id,
                m.agent_id    AS agent_id,
                m.status      AS status,
                m.created_at  AS observed_at
            FROM memories AS m
            WHERE m.tenant_id = :tenant_id
              AND m.status = ANY(CAST(:contradicted_statuses AS text[]))
              AND m.created_at >= :w_start
              AND m.created_at <  :w_end
              AND (CAST(:fleet_id AS text) IS NULL OR m.fleet_id = :fleet_id OR m.fleet_id IS NULL)
              AND (CAST(:run_id AS text) IS NULL OR m.run_id   = :run_id)
              AND (CAST(:agent_id AS text) IS NULL OR m.agent_id = :agent_id)
              AND m.run_id IS NOT NULL
        """
        async with get_read_session() as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "w_start": window_start,
                        "w_end": window_end,
                        "run_id": run_id,
                        "agent_id": agent_id,
                        "contradicted_statuses": list(contradicted_statuses),
                    },
                )
            ).fetchall()
        return [
            {
                "memory_id": str(r.memory_id),
                "run_id": r.run_id,
                "agent_id": r.agent_id,
                "status": r.status,
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
            }
            for r in rows
        ]

    async def outcome_supersession_signals(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        window_start: datetime,
        window_end: datetime,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """Memories superseded within the window (self-join on
        ``supersedes_id``). Ports ``outcome_inference/supersessions.extract``
        SQL verbatim, including the cross-fleet isolation predicate on the
        OLD memory.
        """
        sql = """
            SELECT
                old_mem.id           AS superseded_id,
                old_mem.run_id       AS run_id,
                old_mem.agent_id     AS agent_id,
                new_mem.id           AS by_id,
                new_mem.created_at   AS observed_at
            FROM memories AS new_mem
            JOIN memories AS old_mem
              ON old_mem.id = new_mem.supersedes_id
             AND old_mem.tenant_id = new_mem.tenant_id
            WHERE new_mem.tenant_id = :tenant_id
              AND new_mem.created_at >= :w_start
              AND new_mem.created_at <  :w_end
              AND (CAST(:fleet_id AS text) IS NULL OR new_mem.fleet_id  = :fleet_id  OR new_mem.fleet_id IS NULL)
              AND (CAST(:fleet_id AS text) IS NULL OR old_mem.fleet_id  = :fleet_id  OR old_mem.fleet_id IS NULL)
              AND (CAST(:run_id AS text) IS NULL OR old_mem.run_id    = :run_id)
              AND (CAST(:agent_id AS text) IS NULL OR old_mem.agent_id  = :agent_id)
              AND old_mem.run_id IS NOT NULL
        """
        async with get_read_session() as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "w_start": window_start,
                        "w_end": window_end,
                        "run_id": run_id,
                        "agent_id": agent_id,
                    },
                )
            ).fetchall()
        return [
            {
                "superseded_id": str(r.superseded_id),
                "run_id": r.run_id,
                "agent_id": r.agent_id,
                "by_id": str(r.by_id),
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
            }
            for r in rows
        ]

    async def outcome_cross_agent_reuse_signals(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        window_start: datetime,
        window_end: datetime,
        threshold: int,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """Load-bearing memories (``recall_count >= threshold``) authored in
        the window. Ports ``outcome_inference/cross_agent_reuse.extract`` SQL
        verbatim.
        """
        sql = """
            SELECT
                m.id           AS memory_id,
                m.run_id       AS run_id,
                m.agent_id     AS agent_id,
                m.recall_count AS recall_count,
                m.last_recalled_at AS observed_at
            FROM memories AS m
            WHERE m.tenant_id = :tenant_id
              AND m.recall_count >= :threshold
              AND m.created_at >= :w_start
              AND m.created_at <  :w_end
              AND m.run_id IS NOT NULL
              AND (CAST(:fleet_id AS text) IS NULL OR m.fleet_id = :fleet_id OR m.fleet_id IS NULL)
              AND (CAST(:run_id AS text) IS NULL OR m.run_id   = :run_id)
              AND (CAST(:agent_id AS text) IS NULL OR m.agent_id = :agent_id)
        """
        async with get_read_session() as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "w_start": window_start,
                        "w_end": window_end,
                        "run_id": run_id,
                        "agent_id": agent_id,
                        "threshold": threshold,
                    },
                )
            ).fetchall()
        return [
            {
                "memory_id": str(r.memory_id),
                "run_id": r.run_id,
                "agent_id": r.agent_id,
                "recall_count": r.recall_count,
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
            }
            for r in rows
        ]

    async def outcome_terminal_memory_signals(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        window_start: datetime,
        window_end: datetime,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """The LAST memory of each session in the window (``DISTINCT ON
        (run_id, agent_id) ... ORDER BY run_id, agent_id, created_at DESC``).
        Ports ``outcome_inference/terminal_memory.extract`` SQL verbatim; the
        keyword classifier stays on the core-api side.
        """
        sql = """
            SELECT DISTINCT ON (m.run_id, m.agent_id)
                m.id          AS memory_id,
                m.run_id      AS run_id,
                m.agent_id    AS agent_id,
                m.content     AS content,
                m.created_at  AS observed_at
            FROM memories AS m
            WHERE m.tenant_id = :tenant_id
              AND m.created_at >= :w_start
              AND m.created_at <  :w_end
              AND m.run_id IS NOT NULL
              AND (CAST(:fleet_id AS text) IS NULL OR m.fleet_id = :fleet_id OR m.fleet_id IS NULL)
              AND (CAST(:run_id AS text) IS NULL OR m.run_id   = :run_id)
              AND (CAST(:agent_id AS text) IS NULL OR m.agent_id = :agent_id)
            ORDER BY m.run_id, m.agent_id, m.created_at DESC
        """
        async with get_read_session() as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "w_start": window_start,
                        "w_end": window_end,
                        "run_id": run_id,
                        "agent_id": agent_id,
                    },
                )
            ).fetchall()
        return [
            {
                "memory_id": str(r.memory_id),
                "run_id": r.run_id,
                "agent_id": r.agent_id,
                "content": r.content,
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
            }
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    #  INSIGHTS — analytic memory reads + supersede/restore writes
    #  (Fix 2 Ph5b)
    # ══════════════════════════════════════════════════════════════════════
    #
    # These port the SQLAlchemy ORM queries from the core-api insights service
    # (services/insights_service.py ``_query_*`` + ``_persist_findings``) and
    # the lifecycle_audit ``insights()`` activity gate VERBATIM. The 6 analytic
    # READS use ``select(Memory)`` (sidesteps the asyncpg array-cast risk and
    # matches ``memory_find_successors``); the supersede/restore UPDATEs use
    # raw ``text()`` with ``ANY(CAST(:ids AS uuid[]))`` where it's natural.
    #
    # The ``scope`` argument reconstructs ``_scope_filters``: base
    # ``tenant_id == :tid AND deleted_at IS NULL``; scope='agent' adds
    # ``agent_id == :aid`` (+ fleet when given); scope='fleet' adds
    # ``fleet_id == :fid``; scope='all' adds nothing. Every read also excludes
    # ``memory_type != 'insight'`` (feedback-loop guard). Each takes an explicit
    # ``tenant_id`` — there are no RLS GUCs server-side. Rows are returned as
    # plain dicts in the ``_rows_to_dicts`` shape the core-api prompt formatter
    # expects (NO embedding) except for discover-sample, which includes the
    # embedding (client-side k-means).

    @staticmethod
    def _insights_scope_filters(tenant_id: str, fleet_id: str | None, agent_id: str, scope: str) -> list:
        """Reconstruct ``insights_service._scope_filters`` ORM WHERE clauses."""
        base = [Memory.tenant_id == tenant_id, Memory.deleted_at.is_(None)]
        if scope == "agent":
            base.append(Memory.agent_id == agent_id)
            if fleet_id:
                base.append(Memory.fleet_id == fleet_id)
        elif scope == "fleet":
            if not fleet_id:
                raise ValueError("fleet_id is required when scope is 'fleet'")
            base.append(Memory.fleet_id == fleet_id)
        # scope == "all": tenant-wide, no additional filters
        return base

    @staticmethod
    def _insights_rows_to_dicts(rows, *, include_embedding: bool = False) -> list[dict]:
        """Port ``insights_service._rows_to_dicts`` (NO embedding) so the
        core-api prompt formatter consumes the same dict shape it did when it
        held the rows directly. ``include_embedding`` adds the raw vector for
        the discover-sample path (client-side k-means)."""
        out: list[dict] = []
        for r in rows:
            d = {
                "id": str(r.id),
                "memory_type": r.memory_type,
                "title": r.title or "",
                "content": r.content,
                "weight": r.weight,
                "agent_id": r.agent_id,
                "fleet_id": r.fleet_id,
                "created_at": r.created_at.isoformat() if r.created_at else "",
                "status": r.status,
                "recall_count": r.recall_count or 0,
                "last_recalled_at": r.last_recalled_at.isoformat() if r.last_recalled_at else None,
                "supersedes_id": str(r.supersedes_id) if r.supersedes_id else None,
                "subject_entity_id": str(r.subject_entity_id) if r.subject_entity_id else None,
                "object_value": r.object_value,
                "ts_valid_start": r.ts_valid_start.isoformat() if r.ts_valid_start else None,
            }
            if include_embedding:
                # pgvector returns a numpy-ish sequence; normalise to a plain
                # list of floats so it JSON-serialises over the HTTP boundary.
                emb = r.embedding
                d["embedding"] = [float(x) for x in emb] if emb is not None else None
            out.append(d)
        return out

    async def insights_query_contradictions(
        self, *, tenant_id: str, fleet_id: str | None, agent_id: str, scope: str, max_memories: int
    ) -> list[dict]:
        """Memories that supersede others, are conflicted, or share entities
        with divergent values. Ports ``_query_contradictions`` verbatim (the
        3-step supersede / superseded-by-id / entity-divergence build), dedup
        by id, capped at ``max_memories`` (``INSIGHTS_MAX_MEMORIES`` forwarded
        from core-api — the tuning constant stays the single source of truth
        on the core-api side, mirroring Ph5a's ``threshold`` param)."""
        base = self._insights_scope_filters(tenant_id, fleet_id, agent_id, scope)
        async with get_read_session() as session:
            stmt = (
                select(Memory)
                .where(
                    *base,
                    Memory.status != "deleted",
                    Memory.memory_type != "insight",
                )
                .where((Memory.supersedes_id.isnot(None)) | (Memory.status == "conflicted"))
                .order_by(Memory.created_at.desc())
                .limit(max_memories)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            seen_ids = {r.id for r in rows}

            superseded_ids = [
                r.supersedes_id
                for r in rows
                if r.supersedes_id is not None and r.supersedes_id not in seen_ids
            ]
            if superseded_ids and len(rows) < max_memories:
                sup_stmt = (
                    select(Memory)
                    .where(
                        *base,
                        Memory.memory_type != "insight",
                        Memory.id.in_(superseded_ids),
                    )
                    .limit(max_memories - len(rows))
                )
                sup_result = await session.execute(sup_stmt)
                for r in sup_result.scalars().all():
                    if r.id not in seen_ids:
                        rows.append(r)
                        seen_ids.add(r.id)

            if len(rows) < max_memories:
                remaining = max_memories - len(rows)
                entity_stmt = (
                    select(Memory.subject_entity_id)
                    .where(
                        *base,
                        Memory.status != "deleted",
                        Memory.memory_type != "insight",
                        Memory.subject_entity_id.isnot(None),
                        Memory.object_value.isnot(None),
                    )
                    .group_by(Memory.subject_entity_id)
                    .having(func.count(distinct(Memory.object_value)) > 1)
                    .limit(10)
                )
                entity_result = await session.execute(entity_stmt)
                entity_ids = [r[0] for r in entity_result.all()]

                if entity_ids:
                    extra_stmt = (
                        select(Memory)
                        .where(
                            *base,
                            Memory.status != "deleted",
                            Memory.memory_type != "insight",
                            Memory.subject_entity_id.in_(entity_ids),
                        )
                        .order_by(Memory.created_at.desc())
                        .limit(remaining)
                    )
                    extra_result = await session.execute(extra_stmt)
                    for r in extra_result.scalars().all():
                        if r.id not in seen_ids:
                            rows.append(r)
                            seen_ids.add(r.id)

        return self._insights_rows_to_dicts(rows[:max_memories])

    async def insights_query_failures(
        self, *, tenant_id: str, fleet_id: str | None, agent_id: str, scope: str, max_memories: int
    ) -> list[dict]:
        """Low-weight memories that were recalled (agents acted on weak info).
        Ports ``_query_failures`` verbatim."""
        base = self._insights_scope_filters(tenant_id, fleet_id, agent_id, scope)
        async with get_read_session() as session:
            stmt = (
                select(Memory)
                .where(
                    *base,
                    Memory.memory_type != "insight",
                    Memory.weight < 0.3,
                    Memory.recall_count > 0,
                    Memory.status == "active",
                )
                .order_by(Memory.recall_count.desc(), Memory.weight.asc())
                .limit(max_memories)
            )
            result = await session.execute(stmt)
            return self._insights_rows_to_dicts(result.scalars().all())

    async def insights_query_stale(
        self,
        *,
        tenant_id: str,
        fleet_id: str | None,
        agent_id: str,
        scope: str,
        thirty_days_ago: datetime,
        fourteen_days_ago: datetime,
        max_memories: int,
    ) -> list[dict]:
        """Memories likely outdated based on age + recall activity. Ports
        ``_query_stale`` verbatim. The two age thresholds are passed from the
        caller's clock (core-api) and bound as datetimes server-side."""
        base = self._insights_scope_filters(tenant_id, fleet_id, agent_id, scope)
        async with get_read_session() as session:
            stmt = (
                select(Memory)
                .where(
                    *base,
                    Memory.memory_type != "insight",
                    Memory.status == "active",
                )
                .where(
                    ((Memory.recall_count == 0) & (Memory.created_at < thirty_days_ago))
                    | (
                        (Memory.weight < 0.3)
                        & or_(
                            Memory.last_recalled_at.is_(None),
                            Memory.last_recalled_at < fourteen_days_ago,
                        )
                    )
                )
                .order_by(Memory.created_at.asc())
                .limit(max_memories)
            )
            result = await session.execute(stmt)
            return self._insights_rows_to_dicts(result.scalars().all())

    async def insights_query_divergence(
        self, *, tenant_id: str, fleet_id: str | None, agent_id: str, scope: str, max_memories: int
    ) -> list[dict]:
        """Memories where multiple agents reference the same entities
        differently. Ports ``_query_divergence`` verbatim: entity pre-query
        (GROUP BY subject_entity_id HAVING COUNT(DISTINCT agent_id) >= 2) then
        fetch; ``[]`` when no entity qualifies."""
        base = self._insights_scope_filters(tenant_id, fleet_id, agent_id, scope)
        async with get_read_session() as session:
            entity_stmt = (
                select(Memory.subject_entity_id)
                .where(
                    *base,
                    Memory.memory_type != "insight",
                    Memory.subject_entity_id.isnot(None),
                )
                .group_by(Memory.subject_entity_id)
                .having(func.count(distinct(Memory.agent_id)) >= 2)
                .limit(10)
            )
            entity_result = await session.execute(entity_stmt)
            entity_ids = [r[0] for r in entity_result.all()]

            if not entity_ids:
                return []

            mem_stmt = (
                select(Memory)
                .where(
                    *base,
                    Memory.status != "deleted",
                    Memory.memory_type != "insight",
                    Memory.subject_entity_id.in_(entity_ids),
                )
                .order_by(Memory.subject_entity_id, Memory.agent_id, Memory.created_at.desc())
                .limit(max_memories)
            )
            result = await session.execute(mem_stmt)
            return self._insights_rows_to_dicts(result.scalars().all())

    async def insights_query_patterns(
        self, *, tenant_id: str, fleet_id: str | None, agent_id: str, scope: str, max_memories: int
    ) -> list[dict]:
        """Recent active memories for trend/pattern analysis. Ports
        ``_query_patterns`` verbatim."""
        base = self._insights_scope_filters(tenant_id, fleet_id, agent_id, scope)
        async with get_read_session() as session:
            stmt = (
                select(Memory)
                .where(
                    *base,
                    Memory.memory_type != "insight",
                    Memory.status == "active",
                )
                .order_by(Memory.created_at.desc())
                .limit(max_memories)
            )
            result = await session.execute(stmt)
            return self._insights_rows_to_dicts(result.scalars().all())

    async def insights_discover_sample(
        self, *, tenant_id: str, fleet_id: str | None, agent_id: str, scope: str, sample_size: int
    ) -> list[dict]:
        """Sample active memories WITH embeddings for client-side k-means.
        Ports ``_query_discover``'s row-fetch (the numpy clustering + cluster
        build stay on the core-api side). Returns rows INCLUDING ``embedding``,
        capped at ``sample_size`` (``INSIGHTS_DISCOVER_SAMPLE_SIZE`` forwarded
        from core-api)."""
        base = self._insights_scope_filters(tenant_id, fleet_id, agent_id, scope)
        async with get_read_session() as session:
            stmt = (
                select(Memory)
                .where(
                    *base,
                    Memory.status == "active",
                    Memory.memory_type != "insight",
                    Memory.embedding.isnot(None),
                )
                .order_by(Memory.created_at.desc())
                .limit(sample_size)
            )
            result = await session.execute(stmt)
            return self._insights_rows_to_dicts(result.scalars().all(), include_embedding=True)

    async def insights_supersede_priors(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        focus: str,
        scope: str,
        fleet_id: str | None = None,
    ) -> dict:
        """Atomically select + outdate prior active insights for this
        focus/scope/fleet. Ports ``_persist_findings`` prior-select + outdate
        UPDATE into ONE transaction on the PRIMARY.

        ``:focus`` / ``:scope`` compare text-to-text via the ``->>`` jsonb
        text-accessor (NO jsonb cast). Returns ``{prior_ids, outdated_count}``.
        """
        # Single atomic UPDATE ... RETURNING: the returned ids are EXACTLY the
        # rows THIS call transitioned active→outdated. A SELECT-then-UPDATE
        # would capture ids a concurrent caller outdates first; the total-
        # failure restore in _persist_findings would then re-activate the other
        # caller's legitimately-outdated priors, leaving two insight generations
        # active at once. The single statement locks + updates atomically, so a
        # concurrent caller either sees zero active priors or blocks until commit.
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE memories
                    SET status = 'outdated'
                    WHERE tenant_id = :tenant_id
                      AND agent_id = :agent_id
                      AND memory_type = 'insight'
                      AND status = 'active'
                      AND deleted_at IS NULL
                      AND metadata->>'insight_focus' = :focus
                      AND metadata->>'insight_scope' = :scope
                      AND (
                          (CAST(:fleet_id AS text) IS NULL AND fleet_id IS NULL)
                          OR fleet_id = :fleet_id
                      )
                    RETURNING id::text AS id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "focus": focus,
                    "scope": scope,
                    "fleet_id": fleet_id,
                },
            )
            prior_ids = [r.id for r in result.fetchall()]
        return {"prior_ids": prior_ids, "outdated_count": len(prior_ids)}

    async def insights_restore_priors(self, *, tenant_id: str, prior_ids: list[str]) -> dict:
        """Restore previously-outdated prior insights to ``active`` (the
        total-failure safety net in ``_persist_findings``). Scoped to
        ``tenant_id`` so a smuggled id can't flip another tenant's row.
        Returns ``{restored: rowcount}``."""
        if not prior_ids:
            return {"restored": 0}
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE memories
                    SET status = 'active'
                    WHERE id = ANY(CAST(:prior_ids AS uuid[]))
                      AND status = 'outdated'
                      AND tenant_id = :tenant_id
                    """
                ),
                {"prior_ids": list(prior_ids), "tenant_id": tenant_id},
            )
            return {"restored": result.rowcount or 0}  # type: ignore[attr-defined]

    async def insights_activity_gate(self, *, tenant_id: str, fleet_id: str | None) -> dict:
        """Cheap two-query activity gate for the lifecycle insights pass.
        Ports ``lifecycle_audit.insights()`` gate queries: ``MAX(created_at)``
        for non-insight vs insight memories, scoped to tenant (+ fleet).
        Returns ``{latest_non_insight: iso|null, latest_insight: iso|null}``."""
        scope_filter = [Memory.tenant_id == tenant_id, Memory.deleted_at.is_(None)]
        if fleet_id:
            scope_filter.append(Memory.fleet_id == fleet_id)
        async with get_read_session() as session:
            latest_non_insight = await session.scalar(
                select(func.max(Memory.created_at)).where(*scope_filter, Memory.memory_type != "insight")
            )
            latest_insight = await session.scalar(
                select(func.max(Memory.created_at)).where(*scope_filter, Memory.memory_type == "insight")
            )
        return {
            "latest_non_insight": latest_non_insight.isoformat() if latest_non_insight else None,
            "latest_insight": latest_insight.isoformat() if latest_insight else None,
        }

    # ══════════════════════════════════════════════════════════════════════
    #  EVOLVE — scope-filter read + atomic weight-adjust/backfill write
    #  (Fix 2 Ph5b, PR2)
    # ══════════════════════════════════════════════════════════════════════
    #
    # Ports the two raw-DB passes the core-api evolve service held against
    # ``memories`` (services/evolve_service.py): the ``_filter_by_scope``
    # SELECT and the ``_ADJUST_WEIGHTS_BULK_SQL`` CTE + ``_BACKFILL_RULE_OUTCOME_SQL``
    # UPDATE. The filter-by-scope READ uses ``select(Memory.id)`` (sidesteps the
    # asyncpg array-cast risk, mirrors insights/skill_factory); the apply-weights
    # WRITE ports the CTE + jsonb_set backfill VERBATIM as raw ``text()`` (ORM-
    # awkward) inside ONE transaction so the weight clamp and the rule→outcome
    # backfill commit atomically. The ``IN :mids`` expanding-bindparam from the
    # source becomes ``ANY(CAST(:ids AS uuid[]))`` for the asyncpg driver. Each
    # method takes an explicit ``tenant_id`` and scopes every statement by it —
    # there are no RLS GUCs server-side. The dedup / UUID-parse / cap / rounding
    # / skip-reason logic stays client-side in ``_adjust_weights`` /
    # ``_filter_by_scope``; only the DB passes move here.

    async def evolve_filter_by_scope(
        self,
        *,
        tenant_id: str,
        caller_agent_id: str,
        fleet_id: str | None,
        scope: str,
        ids: list[str],
    ) -> list[str]:
        """Return the subset of ``ids`` visible to the caller under ``scope``.

        Ports ``_filter_by_scope``'s SELECT verbatim: base
        ``id IN (...) AND tenant_id == :tid AND deleted_at IS NULL``; scope
        ='agent' adds ``agent_id == :caller``, scope='fleet' adds
        ``fleet_id == :fid`` (fleet_id required), scope='all' adds nothing.
        Uses ``select(Memory.id).where(Memory.id.in_(...))`` (UUID objects,
        not stringified) so the asyncpg array-cast risk is avoided and
        canonical-form mismatches don't drop valid ids. Returns the matched
        ids as plain strings; the caller maps these back to its first-seen
        ordering + tallies ``out_of_scope_count``."""
        if not ids:
            return []
        if scope == "fleet" and fleet_id is None:
            raise ValueError("evolve_filter_by_scope: fleet_id is required when scope is 'fleet'")
        uuids = [UUID(s) for s in ids]
        stmt = (
            select(Memory.id)
            .where(Memory.id.in_(uuids))
            .where(Memory.tenant_id == tenant_id)
            .where(Memory.deleted_at.is_(None))
        )
        if scope == "agent":
            stmt = stmt.where(Memory.agent_id == caller_agent_id)
        elif scope == "fleet":
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        async with get_read_session() as session:
            result = await session.execute(stmt)
            return [str(row[0]) for row in result]

    async def evolve_apply_weights(
        self,
        *,
        tenant_id: str,
        ids: list[str],
        delta: float,
        floor: float,
        cap: float,
        rule_id: str | None = None,
        outcome_id: str | None = None,
    ) -> dict:
        """Clamp-and-adjust weights for ``ids`` and (atomically) backfill the
        rule→outcome link, in ONE transaction.

        Stmt 1 ports ``_ADJUST_WEIGHTS_BULK_SQL`` verbatim: an ``old_vals`` CTE
        captures the pre-update weight, the UPDATE clamps
        ``GREATEST(:floor, LEAST(:cap, weight + :delta))`` and RETURNs
        ``(id, old_weight, new_weight)``. The source's ``id IN :mids``
        expanding bindparam becomes ``ANY(CAST(:ids AS uuid[]))`` for asyncpg.

        Stmt 2 ports ``_BACKFILL_RULE_OUTCOME_SQL`` verbatim and runs ONLY when
        both ``rule_id`` and ``outcome_id`` are present: a ``jsonb_set`` of
        ``metadata.source_outcome_id`` on the rule memory. Folding it into this
        endpoint keeps the weight clamp + the backfill in a single storage
        transaction so evolve's documented split-commit isn't widened into two
        HTTP calls.

        Every statement is scoped by ``tenant_id``. Returns
        ``{adjustments:[{id, old_weight, new_weight}], backfilled: bool}`` —
        the caller (``_adjust_weights``) applies rounding / ordering / the
        ``delta`` + ``memory_id`` key shape from these rows."""
        if not ids:
            return {"adjustments": [], "backfilled": False}
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    WITH old_vals AS (
                        SELECT id, weight AS old_weight
                          FROM memories
                         WHERE id = ANY(CAST(:ids AS uuid[]))
                           AND tenant_id = :tid
                           AND deleted_at IS NULL
                    )
                    UPDATE memories
                       SET weight = GREATEST(:floor, LEAST(:cap, weight + :delta))
                      FROM old_vals
                     WHERE memories.id = old_vals.id
                       AND memories.tenant_id = :tid
                       AND memories.deleted_at IS NULL
                    RETURNING memories.id AS id, old_vals.old_weight AS old_weight,
                              memories.weight AS new_weight
                    """
                ),
                {
                    "ids": list(ids),
                    "tid": tenant_id,
                    "floor": floor,
                    "cap": cap,
                    "delta": delta,
                },
            )
            adjustments = [
                {
                    "id": str(row.id),
                    "old_weight": float(row.old_weight),
                    "new_weight": float(row.new_weight),
                }
                for row in result.fetchall()
            ]
            backfilled = False
            if rule_id and outcome_id:
                br = await session.execute(
                    text(
                        """
                        UPDATE memories
                           SET metadata = jsonb_set(
                               -- ``metadata::jsonb`` cast: legacy/test rows store
                               -- the column as ``json`` (CAURA-595 drift); without
                               -- it COALESCE(json, '{}'::jsonb) raises CannotCoerceError.
                               -- Mirrors memory_update's metadata-merge.
                               COALESCE(metadata::jsonb, '{}'::jsonb),
                               '{source_outcome_id}',
                               to_jsonb(CAST(:outcome_id AS text))
                           )
                         WHERE id = CAST(:rule_id AS uuid) AND tenant_id = :tid
                        """
                    ),
                    {"rule_id": rule_id, "outcome_id": outcome_id, "tid": tenant_id},
                )
                # Report ``backfilled`` honestly: a soft-deleted, never-committed,
                # or cross-tenant ``rule_id`` matches 0 rows, so the link wasn't
                # actually written. ``rowcount`` is reliable for an UPDATE on the
                # asyncpg dialect (parsed from the ``UPDATE N`` command tag).
                backfilled = (br.rowcount or 0) > 0  # type: ignore[attr-defined]
        return {"adjustments": adjustments, "backfilled": backfilled}

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

    async def fleet_has_recent_in_flight_deploy(
        self,
        *,
        node_id: UUID,
        since: datetime,
    ) -> bool:
        """True if a ``deploy`` command for this node is still in flight.

        "In flight" means status in (``pending``, ``acked``) and
        ``created_at >= since``. Used by the auto-upgrade gate to suppress
        queueing duplicate deploys when a previous one has been sent to
        the plugin but never reported back as completed/failed.
        """
        # Primary (writer) session, not get_read_session: this is a read-after-write
        # deploy-dedup gate — a replica read under lag could miss a just-queued command
        # and let a duplicate deploy through.
        async with get_session() as session:
            result = await session.execute(
                select(FleetCommand.id)
                .where(
                    FleetCommand.node_id == node_id,
                    FleetCommand.command == "deploy",
                    FleetCommand.status.in_(("pending", "acked")),
                    FleetCommand.created_at >= since,
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def fleet_count_recent_deploys_for_target(
        self,
        *,
        node_id: UUID,
        target_version: str,
        since: datetime,
    ) -> int:
        """Count auto-upgrade ``deploy`` commands queued for this node at
        ``target_version`` since ``since`` — ALL statuses.

        Backs the auto-upgrade attempt budget (CAURA-000). Counting ALL
        statuses is deliberate: the nastiest mode is ``status=done`` with
        no version progress, which a status filter would miss. Keyed on
        ``target_version`` so a NEW release starts a fresh budget.
        """
        # Primary (writer) session, not get_read_session: the attempt budget must
        # count deploys queued by a prior heartbeat (replica lag would under-count
        # and let the budget be exceeded).
        async with get_session() as session:
            result = await session.execute(
                select(func.count(FleetCommand.id)).where(
                    FleetCommand.node_id == node_id,
                    FleetCommand.command == "deploy",
                    FleetCommand.payload["target_version"].astext == target_version,
                    FleetCommand.created_at >= since,
                )
            )
            return result.scalar_one() or 0

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

    async def fleet_update_command_result(
        self,
        *,
        command_id: UUID,
        status: str,
        tenant_id: str | None = None,
        result: dict | None = None,
        completed_at: datetime | None = None,
    ) -> bool:
        """Record a command's completion (``done`` / ``failed`` / ``acked``).

        ``tenant_id`` scopes the UPDATE so a caller can only touch its own
        tenant's commands — keying on ``command_id`` alone let any
        authenticated tenant complete another tenant's command by UUID
        (cross-tenant BOLA). ``None`` (admin / unscoped callers) skips the
        filter. Returns ``True`` iff a row matched.
        """
        values: dict
        if status == "acked":
            values = {"status": "acked", "acked_at": completed_at}
        else:
            values = {"status": status}
            if result is not None:
                values["result"] = result
            if completed_at is not None:
                values["completed_at"] = completed_at
        async with get_session() as session:
            stmt = sql_update(FleetCommand).where(FleetCommand.id == command_id)
            if tenant_id is not None:
                stmt = stmt.where(FleetCommand.tenant_id == tenant_id)
            res = await session.execute(stmt.values(**values))
            return (res.rowcount or 0) > 0  # type: ignore[attr-defined]

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
        """Persist one audit event (single-event + sync-fallback path).

        Chains the event so single-event inserts (the sync fallback in
        core-api's ``log_action``, plus keystone audit writes) land in the
        same per-tenant hash chain as the batched path — otherwise they
        would write NULL-hash rows that break the chain (eToro governance).
        Calls the per-tenant writer directly (one tenant, one event) rather
        than paying the batch group-by for a singleton.
        """
        await self._audit_chain_one_tenant(
            tenant_id,
            [
                {
                    "agent_id": agent_id,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "detail": detail,
                }
            ],
        )

    async def audit_add_batch(self, events: list[dict]) -> None:
        """Persist N audit events (CAURA-628 batched path).

        Thin alias for :meth:`audit_add_batch_chained` so the existing
        bulk router call site keeps working while every audit event now
        joins the tamper-evident per-tenant hash chain.
        """
        await self.audit_add_batch_chained(events)

    async def audit_add_batch_chained(self, events: list[dict]) -> None:
        """Persist audit events into the per-tenant tamper-evident chain.

        Each event gets a monotonic per-tenant ``seq`` and an
        ``event_hash = SHA256(canonical_event || prev_hash)`` linking it to
        the prior event. Events are grouped by tenant and each tenant's
        group is built + committed in its OWN transaction, so one tenant's
        failure can't roll back another's and the per-tenant head-row lock
        only serializes same-tenant writers.

        Empty ``events`` short-circuits (the audit flusher ticks on an
        interval even when idle).
        """
        if not events:
            return
        by_tenant: dict[str, list[dict]] = {}
        for ev in events:
            by_tenant.setdefault(ev["tenant_id"], []).append(ev)
        for tenant_id, tenant_events in by_tenant.items():
            await self._audit_chain_one_tenant(tenant_id, tenant_events)

    async def _audit_chain_one_tenant(self, tenant_id: str, events: list[dict]) -> None:
        """Chain + insert one tenant's events inside a single transaction.

        The ``audit_chain_head`` row is the serialization point: we
        ``SELECT ... FOR UPDATE`` it so concurrent same-tenant writers run
        one-at-a-time (different tenants lock different rows and never
        block). ``ON CONFLICT DO NOTHING`` resolves the concurrent-genesis
        race (two workers racing a tenant's first-ever event). The
        ``FOR UPDATE`` lock releases atomically with the row inserts on
        commit, so the chain can never interleave.
        """
        async with get_session() as session:
            # Lock-or-create the head row. The insert is a no-op once the
            # head exists; the unconditional FOR UPDATE select below is what
            # actually serializes writers.
            await session.execute(
                pg_insert(AuditChainHead.__table__)
                .values(tenant_id=tenant_id, last_seq=0, last_hash=GENESIS_PREV_HASH)
                .on_conflict_do_nothing(index_elements=["tenant_id"])
            )
            head = (
                await session.execute(
                    select(AuditChainHead).where(AuditChainHead.tenant_id == tenant_id).with_for_update()
                )
            ).scalar_one()

            # Idempotent retry: the audit bulk flush retries transient errors,
            # so a lost-ack batch is re-sent with the SAME client_event_ids.
            # Drop any event already chained for this tenant — looked up here,
            # under the head FOR UPDATE lock, so the read is serialized with
            # concurrent same-tenant writers — plus any duplicated within this
            # batch. Survivors below get contiguous seqs, so the chain stays
            # gap-free and the head consistent. Events with no client_event_id
            # (legacy single-event path) are never deduped.
            # ``is not None`` (not a truthy check) to match the per-event loop's
            # guard below, so both paths treat the field identically.
            incoming_ids = [ev["client_event_id"] for ev in events if ev.get("client_event_id") is not None]
            already_chained: set[str | None] = set()
            if incoming_ids:
                already_chained = set(
                    (
                        await session.execute(
                            select(AuditLog.client_event_id).where(
                                AuditLog.tenant_id == tenant_id,
                                AuditLog.client_event_id.in_(incoming_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

            prev_hash = head.last_hash
            seq = head.last_seq
            now = datetime.now(UTC)
            seen_in_batch: set[str] = set()
            rows: list[AuditLog] = []
            for ev in events:
                client_event_id = ev.get("client_event_id")
                if client_event_id is not None:
                    # Already committed by a prior attempt, or a duplicate
                    # within this batch — skip without consuming a seq.
                    if client_event_id in already_chained or client_event_id in seen_in_batch:
                        continue
                    seen_in_batch.add(client_event_id)
                # Scrub-before-hash: refuse to chain a raw secret. Runs
                # BEFORE hashing so the chain only ever attests the redacted
                # detail (raising here fails the write loudly instead).
                assert_pii_safe(ev.get("detail"))
                seq += 1
                # Assign created_at in-app (not server_default now()) because
                # the hash binds it — reading it back post-insert would risk
                # the stored value differing from the hashed one. Events from
                # the queue carry no created_at, so a batch shares one `now`;
                # `seq` disambiguates same-timestamp events.
                created = ev.get("created_at") or now
                resource_id = ev.get("resource_id")
                canon = canonical_event(
                    tenant_id=tenant_id,
                    seq=seq,
                    agent_id=ev.get("agent_id"),
                    action=ev["action"],
                    resource_type=ev["resource_type"],
                    resource_id=resource_id,
                    detail=ev.get("detail"),
                    created_at_iso=canonical_created_at(created),
                )
                this_hash = compute_event_hash(canon, prev_hash)
                rows.append(
                    AuditLog(
                        tenant_id=tenant_id,
                        agent_id=ev.get("agent_id"),
                        action=ev["action"],
                        resource_type=ev["resource_type"],
                        resource_id=resource_id,
                        detail=ev.get("detail"),
                        created_at=created,
                        seq=seq,
                        prev_hash=prev_hash,
                        event_hash=this_hash,
                        client_event_id=client_event_id,
                    )
                )
                prev_hash = this_hash
            session.add_all(rows)
            head.last_seq = seq
            head.last_hash = prev_hash
            head.updated_at = now

    async def audit_verify_chain(self, tenant_id: str, *, limit: int = 100_000) -> dict:
        """Walk a tenant's hash chain in ``seq`` order and verify integrity.

        Recomputes each ``event_hash`` and checks ``prev_hash`` linkage +
        genesis; stops at and reports the first broken link (everything
        after it is untrustworthy). A final tail-check against
        ``audit_chain_head`` catches rows deleted off the END of the chain
        (a forward walk alone can't see a missing tail). ``limit`` bounds
        the walk — when hit, the tail-check is skipped and ``truncated`` is
        set so the caller knows to paginate.
        """
        async with get_read_session() as session:
            # Pin one snapshot across BOTH reads (rows + head). Under READ
            # COMMITTED each statement gets its own snapshot, so a concurrent
            # same-tenant insert committing between the two reads makes the head
            # look one seq ahead of the fetched rows and fires a FALSE
            # tail_truncated "tampering" alert. REPEATABLE READ freezes the
            # snapshot at the first query for the rest of the transaction
            # (must be set before any query in the tx).
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            rows = list(
                (
                    await session.execute(
                        select(AuditLog)
                        .where(AuditLog.tenant_id == tenant_id, AuditLog.seq.isnot(None))
                        .order_by(AuditLog.seq.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            head = (
                await session.execute(select(AuditChainHead).where(AuditChainHead.tenant_id == tenant_id))
            ).scalar_one_or_none()

        # The walk is pure CPU (SHA-256 + JSON canonicalization per row) and can
        # cover up to `limit` (≤ 500k) rows — offload it so it doesn't block the
        # event loop and starve concurrent requests. The rows/head are already
        # fully loaded, so the thread only touches in-memory attributes.
        return await asyncio.to_thread(_verify_audit_chain_rows, tenant_id, rows, head, limit)

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
    #  ORGANIZATION SETTINGS (OrganizationSettings + audit)
    # ══════════════════════════════════════════════════════════════════════

    async def organization_settings_get(self, org_id: str) -> dict:
        """Return the org's raw override JSONB, or ``{}`` when no row exists.

        Read-only; safe on the reader replica. core-api fronts this with a
        5-min TTL cache, so it's hit only on a cache miss.
        """
        async with get_read_session() as session:
            row = await session.execute(
                select(OrganizationSettings.settings).where(OrganizationSettings.org_id == org_id)
            )
            settings = row.scalar_one_or_none()
            return settings if isinstance(settings, dict) else {}

    async def organization_settings_update(
        self,
        *,
        org_id: str,
        new_settings: dict,
        changed_by: str | None = None,
    ) -> dict:
        """Upsert org overrides + append an audit row, in ONE transaction.

        The flat diff is computed against the ``FOR UPDATE``-locked current
        row so the read and write can't interleave with a concurrent writer
        for the same org (lost-update guard). Returns
        ``{"settings": <merged overrides>, "changed": bool}``. A no-op payload
        (the diff is empty) writes neither row and returns the current
        overrides with ``changed=False``.

        Schema validation is the caller's responsibility — core-api validates
        keys / leaf types / governance enums / cron before calling.
        """
        async with get_session() as session:
            result = await session.execute(
                select(OrganizationSettings.settings)
                .where(OrganizationSettings.org_id == org_id)
                .with_for_update()
            )
            current_row = result.scalar_one_or_none()
            current: dict = current_row if isinstance(current_row, dict) else {}

            diff = diff_settings(current, new_settings)
            if not diff:
                # Identical payload — skip the write and the audit row entirely.
                return {"settings": current, "changed": False}

            merged = deep_merge(current, new_settings)

            # FOR UPDATE serialises writes once the row exists. Concurrent
            # first-time inserts (no row yet) use JSONB || to merge at the DB
            # level so two racing inserts don't silently overwrite each other;
            # the shallow || is safe because top-level schema keys (enrichment,
            # recall, …) are independent.
            upsert = pg_insert(OrganizationSettings).values(org_id=org_id, settings=merged)
            await session.execute(
                upsert.on_conflict_do_update(
                    index_elements=["org_id"],
                    set_={
                        "settings": text("organization_settings.settings || EXCLUDED.settings"),
                        "updated_at": func.now(),
                    },
                )
            )
            await session.execute(
                pg_insert(OrganizationSettingsAudit).values(org_id=org_id, changed_by=changed_by, diff=diff)
            )
            return {"settings": merged, "changed": True}

    # ══════════════════════════════════════════════════════════════════════
    #  TENANT DISCOVERY (lifecycle fanout target lists)
    # ══════════════════════════════════════════════════════════════════════

    async def tenants_list_active(self) -> list[str]:
        """Distinct ``tenant_id`` from non-soft-deleted memories, sorted.

        Archive/lifecycle fanout target: an org with no live memories has
        nothing to archive. Read-only (reader replica).
        """
        async with get_read_session() as session:
            result = await session.execute(
                select(Memory.tenant_id).where(Memory.deleted_at.is_(None)).distinct()
            )
            return sorted(row[0] for row in result.all())

    async def tenants_list_purgeable(self) -> list[str]:
        """Distinct ``tenant_id`` from soft-deleted memories older than the max
        retention window (``MEMORY_RETENTION_MAX_DAYS``), sorted.

        Orgs whose soft-deleted rows are all newer than the max window are
        guaranteed no-ops on the purge primitive, so excluding them keeps the
        discovery scan bounded as ``memories`` grows. ``func.now()`` (DB clock)
        matches the purge primitive's cutoff and avoids client-clock drift.
        """
        cutoff = func.now() - timedelta(days=MEMORY_RETENTION_MAX_DAYS)
        async with get_read_session() as session:
            result = await session.execute(
                select(Memory.tenant_id)
                .where(Memory.deleted_at.is_not(None))
                .where(Memory.deleted_at < cutoff)
                .distinct()
            )
            return sorted(row[0] for row in result.all())

    async def tenants_list_skills_factory_enabled(self) -> list[str]:
        """``org_id`` values whose ``skills_factory.enabled`` JSONB flag is True,
        sorted.

        The forge-distill lifecycle fanout uses this so a tenant that hasn't
        opted in pays ZERO per-tick cost. Orgs with no settings row are excluded
        (the ``DEFAULT_SETTINGS`` default of ``enabled=False`` applies).
        """
        async with get_read_session() as session:
            result = await session.execute(
                select(OrganizationSettings.org_id).where(
                    OrganizationSettings.settings["skills_factory"]["enabled"].as_boolean().is_(True)
                )
            )
            return sorted(row[0] for row in result.all())

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
