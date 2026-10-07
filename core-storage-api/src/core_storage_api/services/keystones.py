"""Keystone-rules service.

Keystones are governance rules an agent must obey. They are stored as
documents in the system-managed collection ``_keystones`` so we get
upsert-by-doc_id, JSONB payload, and tenant/fleet isolation for free —
no schema migration, no new table.

Resolution returns the union of three scopes:

* ``tenant`` — fleet_id IS NULL, applies org-wide.
* ``fleet``  — fleet_id matches, applies to every agent in the fleet.
* ``agent``  — fleet_id matches AND data.agent_id matches.

Ordered by ``data.weight DESC, updated_at DESC`` (then ``doc_id``) and capped
at ``KEYSTONE_MAX_RESULTS`` so a runaway tenant can't bloat the plugin/MCP
context window.

Versions (migration 061): every set and delete records the tenant's whole
keystone set after it, numbered per tenant, in the write's own transaction.
A version's rules for an agent are its snapshot resolved by the SQL that
resolves the live set (``_resolution``), so its rule-set hash for the agent is
the hash of what the list returned the agent then.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import ColumnElement, DateTime, Row, Select, cast, column, func, or_, select, text, true
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.ext.asyncio import AsyncSession

from common.governance.ruleset_hash import RuleSetHashError, rule_set_hash, rules_from_keystone_rows
from common.models import Document, KeystoneVersion
from core_storage_api.schemas import KEYSTONE_VERSION_FIELDS, orm_to_dict
from core_storage_api.services.postgres_service import (
    PostgresService,
    get_read_session,
    get_session,
)

logger = logging.getLogger(__name__)
_svc = PostgresService()

# ---------------------------------------------------------------------------
# Constants — kept local (no global constants module exists in core-storage-api).
# ---------------------------------------------------------------------------

KEYSTONE_COLLECTION = "_keystones"

KEYSTONE_MAX_RESULTS = 50

# Fixed weight buckets — admins choose a label, we map to an int.
# Buckets (not free-form) keep ranking predictable across authors.
KEYSTONE_WEIGHT_BUCKETS: dict[str, int] = {
    "low": 25,
    "med": 50,
    "high": 100,
}

KEYSTONE_VALID_SCOPES: frozenset[str] = frozenset({"tenant", "fleet", "agent"})


class _Keystone(NamedTuple):
    """The columns resolution reads: a live document's, or a snapshot row's."""

    doc_id: Any
    fleet_id: Any
    data: Any
    updated_at: Any


def _resolution(
    keystone: _Keystone, *, fleet_id: str | None, agent_id: str | None
) -> tuple[ColumnElement[bool], tuple[Any, ...]]:
    """``(where, order_by)`` for the keystones ``(fleet_id, agent_id)`` gets:
    the scope union (see module docstring), heaviest first."""
    scope = keystone.data["scope"].astext
    # We always include tenant scope; fleet and agent layers stack on top
    # when their inputs are present.
    predicates = [keystone.fleet_id.is_(None) & (scope == "tenant")]
    if fleet_id is not None:
        predicates.append((keystone.fleet_id == fleet_id) & (scope == "fleet"))
        if agent_id is not None:
            predicates.append(
                (keystone.fleet_id == fleet_id)
                & (scope == "agent")
                & (keystone.data["agent_id"].astext == agent_id)
            )
    # Weight, then updated_at, then doc_id in byte order: deterministic, and
    # unlike a linguistic collation no library update can reorder the ties,
    # so the rules a cap keeps at a version stay the ones it kept.
    order = (
        keystone.data["weight"].as_float().desc(),
        keystone.updated_at.desc(),
        keystone.doc_id.collate("C"),
    )
    return or_(*predicates), order


async def list_keystones(
    *,
    tenant_id: str,
    fleet_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[list[Document], bool]:
    """Return ``(docs, truncated)`` for ``(tenant_id, fleet_id, agent_id)``.

    The result is the scope union — see module docstring. Empty fleet/agent
    args narrow the result, never broaden it (e.g. fleet=None drops fleet
    AND agent rules).

    ``truncated`` is True when more than ``KEYSTONE_MAX_RESULTS`` rules
    matched. We over-fetch by one and slice so callers can signal the
    cap was hit (e.g. via an ``X-Truncated`` response header) — silent
    truncation hides governance gaps.
    """
    where, order = _resolution(
        _Keystone(Document.doc_id, Document.fleet_id, Document.data, Document.updated_at),
        fleet_id=fleet_id,
        agent_id=agent_id,
    )
    stmt = (
        select(Document)
        .where(
            Document.tenant_id == tenant_id,
            Document.collection == KEYSTONE_COLLECTION,
            where,
        )
        # NOTE: data["weight"] sort requires a functional index:
        #   CREATE INDEX idx_keystones_weight ON documents
        #   (tenant_id, ((data->>'weight')::int) DESC, updated_at DESC)
        #   WHERE collection = '_keystones';
        # Without it this is a sequential scan. Track in #112.
        .order_by(*order)
        # Over-fetch by one so the router can detect truncation.
        .limit(KEYSTONE_MAX_RESULTS + 1)
    )

    async with get_read_session() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
    truncated = len(rows) > KEYSTONE_MAX_RESULTS
    return rows[:KEYSTONE_MAX_RESULTS], truncated


# ---------------------------------------------------------------------------
# Writes, each recording a version
# ---------------------------------------------------------------------------

# The tenant's keystone set as a version stores it, one object per keystone in
# doc_id byte order: the shape migration 061's baseline writes. ``json`` built
# as text, so a keystone's text versions as stored, even a ``\u0000`` escape,
# which JSONB refuses.
_SNAPSHOT_SQL = """(
    SELECT coalesce(
        json_agg(
            json_build_object(
                'doc_id', d.doc_id,
                'fleet_id', d.fleet_id,
                'data', d.data,
                'updated_at', to_char(d.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            )
            ORDER BY d.doc_id COLLATE "C"
        ),
        '[]'::json
    )
    FROM documents d
    WHERE d.tenant_id = :tenant_id AND d.collection = '_keystones'
)"""

_RECORD_VERSION = text(
    f"""
    INSERT INTO keystone_versions
        (tenant_id, version, op, doc_id, snapshot, actor_agent_id, actor_user_id)
    VALUES (
        :tenant_id,
        coalesce((SELECT max(version) FROM keystone_versions WHERE tenant_id = :tenant_id), 0) + 1,
        :op,
        :doc_id,
        {_SNAPSHOT_SQL},
        :actor_agent_id,
        :actor_user_id
    )
    RETURNING version
    """
)

# Whether the live set differs from the latest version's (from none, for a
# tenant without versions). Compared as the text each was built as.
_SET_HAS_DRIFTED = text(
    f"""
    SELECT {_SNAPSHOT_SQL}::text IS DISTINCT FROM coalesce(
        (SELECT snapshot::text FROM keystone_versions
         WHERE tenant_id = :tenant_id ORDER BY version DESC LIMIT 1),
        '[]'
    )
    """
)


async def set_keystone(
    *,
    tenant_id: str,
    doc_id: str,
    data: dict,
    fleet_id: str | None,
    actor_agent_id: str | None,
    actor_user_id: str | None,
) -> tuple[Document, int]:
    """Upsert a keystone and record the version it makes: ``(doc, version)``."""
    async with get_session() as session:
        await _begin_write(session, tenant_id)
        doc = await _svc.document_upsert(
            tenant_id=tenant_id,
            collection=KEYSTONE_COLLECTION,
            doc_id=doc_id,
            data=data,
            fleet_id=fleet_id,
            system=True,
            # The shrink guard is for client-synced documents, where a failed
            # read can upsert an empty file. A keystone is a short rule its
            # author rewrites whole, and no surface could pass the override,
            # so a long rule could never be cut down (L-39). Every version is
            # kept besides, so a shrink is recoverable.
            force=True,
            session=session,
        )
        version = await _record_version(
            session,
            tenant_id=tenant_id,
            op="set",
            doc_id=doc_id,
            actor_agent_id=actor_agent_id,
            actor_user_id=actor_user_id,
        )
    return doc, version


async def remove_keystone(
    *,
    tenant_id: str,
    doc_id: str,
    actor_agent_id: str | None,
    actor_user_id: str | None,
) -> tuple[UUID, int] | None:
    """Delete a keystone and record the version it makes:
    ``(deleted_id, version)``, or ``None`` when there was no such keystone
    (and so no version)."""
    async with get_session() as session:
        await _begin_write(session, tenant_id)
        deleted_id = await _svc.document_delete_by_doc_id(
            tenant_id=tenant_id,
            collection=KEYSTONE_COLLECTION,
            doc_id=doc_id,
            system=True,
            session=session,
        )
        if deleted_id is None:
            return None
        version = await _record_version(
            session,
            tenant_id=tenant_id,
            op="delete",
            doc_id=doc_id,
            actor_agent_id=actor_agent_id,
            actor_user_id=actor_user_id,
        )
    return deleted_id, version


async def _begin_write(session: AsyncSession, tenant_id: str) -> None:
    """Take the tenant's keystone lock, and version any change made around it.

    The lock holds the tenant's keystone writes to one at a time until commit.
    Taken before the write, so each version's snapshot is the set its own
    change left and no two writers take one number; other tenants take other
    keys and never wait.

    A change that reached the set without a version (a fleet purge, a storage
    instance still on old code during a deploy, a hand-run fix) becomes its own
    version first, op ``resync``, with no rule or actor. The write's version
    then shows its own change alone, not someone else's under its name.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('keystone_versions'), hashtext(:tenant_id))"),
        {"tenant_id": tenant_id},
    )
    if await session.scalar(_SET_HAS_DRIFTED, {"tenant_id": tenant_id}):
        await _record_version(session, tenant_id=tenant_id, op="resync")


async def _record_version(
    session: AsyncSession,
    *,
    tenant_id: str,
    op: str,
    doc_id: str | None = None,
    actor_agent_id: str | None = None,
    actor_user_id: str | None = None,
) -> int:
    """Insert the tenant's next version: its keystone set as it now stands."""
    result = await session.execute(
        _RECORD_VERSION,
        {
            "tenant_id": tenant_id,
            "op": op,
            "doc_id": doc_id,
            "actor_agent_id": actor_agent_id,
            "actor_user_id": actor_user_id,
        },
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_keystone_versions(
    *,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str | None,
    limit: int,
    before: int | None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Return ``(versions, next_before)``, newest first, each summarised for
    ``(fleet_id, agent_id)``. ``next_before`` pages on: the versions below
    it, or ``None`` when there are none."""
    stmt = select(KeystoneVersion).where(KeystoneVersion.tenant_id == tenant_id)
    if before is not None:
        stmt = stmt.where(KeystoneVersion.version < before)
    resolved = await _resolve_versions(
        stmt.order_by(KeystoneVersion.version.desc()).limit(limit + 1),
        fleet_id=fleet_id,
        agent_id=agent_id,
    )
    page = [summary for summary, _ in resolved[:limit]]
    next_before = page[-1]["version"] if len(resolved) > limit else None
    return page, next_before


async def get_keystone_version(
    *,
    tenant_id: str,
    version: int,
    fleet_id: str | None,
    agent_id: str | None,
) -> dict[str, Any] | None:
    """One version, summarised for ``(fleet_id, agent_id)`` and with the
    rules it gives them as ``items``; ``None`` if the tenant has no such
    version."""
    resolved = await _resolve_versions(
        select(KeystoneVersion).where(
            KeystoneVersion.tenant_id == tenant_id, KeystoneVersion.version == version
        ),
        fleet_id=fleet_id,
        agent_id=agent_id,
    )
    if not resolved:
        return None
    [(summary, rules)] = resolved
    return {**summary, "items": rules}


async def _resolve_versions(
    versions: Select[Any], *, fleet_id: str | None, agent_id: str | None
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Each of ``versions``, newest first, as ``(summary, rules)``: the rules
    its snapshot gives ``(fleet_id, agent_id)``, resolved in Postgres as the
    list resolves the live set, and their rule-set hash."""
    v = versions.subquery("v")
    element = func.json_array_elements(v.c.snapshot).table_valued(column("value", JSON)).alias("element")
    row = element.c.value
    where, order = _resolution(
        _Keystone(
            row["doc_id"].astext,
            row["fleet_id"].astext,
            row["data"],
            cast(row["updated_at"].astext, DateTime(timezone=True)),
        ),
        fleet_id=fleet_id,
        agent_id=agent_id,
    )
    rank = func.row_number().over(order_by=order).label("rank")
    # One past the cap, as the list over-fetches, to tell ``truncated``.
    rules = (
        select(row.label("rule"), rank)
        .select_from(element)
        .where(where)
        .order_by(rank)
        .limit(KEYSTONE_MAX_RESULTS + 1)
        .lateral("rules")
    )
    stmt = (
        select(v.c.tenant_id, *(v.c[field] for field in KEYSTONE_VERSION_FIELDS), rules.c.rule)
        .select_from(v.outerjoin(rules, true()))
        .order_by(v.c.version.desc(), rules.c.rank)
    )
    async with get_read_session() as session:
        rows = (await session.execute(stmt)).all()

    resolved: dict[int, tuple[Row[Any], list[dict[str, Any]]]] = {}
    for found in rows:
        _, matched = resolved.setdefault(found.version, (found, []))
        if found.rule is not None:
            matched.append(found.rule)
    return [_summary(found, matched) for found, matched in resolved.values()]


def _summary(version: Row[Any], matched: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rules = matched[:KEYSTONE_MAX_RESULTS]
    try:
        hashed: str | None = rule_set_hash(rules_from_keystone_rows(rules))
    except RuleSetHashError as exc:
        logger.warning(
            "keystone version %s of tenant %s has no rule-set hash: %s",
            version.version,
            version.tenant_id,
            exc,
        )
        hashed = None
    summary = {
        **orm_to_dict(version, KEYSTONE_VERSION_FIELDS),
        "rule_set_hash": hashed,
        "rule_count": len(rules),
        "truncated": len(matched) > KEYSTONE_MAX_RESULTS,
    }
    return summary, rules
