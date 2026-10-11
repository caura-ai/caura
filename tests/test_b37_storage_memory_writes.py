"""Storage memory writes keep the row's own record straight (audit batch B37/B40).

Each test names the finding it pins:

- L-42: the dedicated embedding PATCH must not land on a soft-deleted row, and
  it refuses ``metadata``, which it wrote over the stored document wholesale.
  Nothing sent it; ``PATCH /memories/{id}`` merges a ``metadata_patch``.
- L-142: ``status_changed_at`` is stamped by every statement that sets
  ``status``, except the expiry sweep's flip to ``outdated`` (Eldad,
  2026-10-11): the contradiction signal windows on that column, and an expired
  row is not a contradiction.
- L-148: the lifecycle report's ``expired_still_active`` lists the rows the
  expiry sweep archives, on either column and in every live status.
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from common.embedding import fake_embedding
from core_storage_api.services.postgres_service import PostgresService, get_session
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.integration

_STORAGE = Path(__file__).resolve().parents[1] / "core-storage-api/src/core_storage_api"


async def _seed(
    tenant: str,
    *,
    status: str = "active",
    memory_type: str = "fact",
    metadata: dict | None = None,
    expires_at: datetime | None = None,
    ts_valid_end: datetime | None = None,
    created_at: datetime | None = None,
    weight: float = 0.5,
    agent_id: str = "b37-probe",
) -> UUID:
    memory_id = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memories "
                "(id, tenant_id, agent_id, memory_type, content, status, weight, "
                " metadata, expires_at, ts_valid_end, created_at) "
                "VALUES (:id, :t, :agent, :type, :content, :status, :weight, "
                " CAST(:metadata AS json), :expires_at, :ts_valid_end, "
                " COALESCE(:created_at, NOW()))"
            ),
            {
                "id": memory_id,
                "t": tenant,
                "agent": agent_id,
                "type": memory_type,
                "content": f"b37 probe {memory_id}",
                "status": status,
                "weight": weight,
                "metadata": json.dumps(metadata or {}),
                "expires_at": expires_at,
                "ts_valid_end": ts_valid_end,
                "created_at": created_at,
            },
        )
    return memory_id


async def _row(memory_id: UUID) -> dict:
    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, status_changed_at, deleted_at, "
                    "embedding IS NOT NULL AS embedded, metadata "
                    "FROM memories WHERE id = :id"
                ),
                {"id": memory_id},
            )
        ).one()
    metadata = row.metadata
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return {
        "status": row.status,
        "status_changed_at": row.status_changed_at,
        "deleted_at": row.deleted_at,
        "embedded": row.embedded,
        "metadata": metadata or {},
    }


# --- L-42: the embedding PATCH ----------------------------------------------


async def test_l42_an_embedding_never_lands_on_a_deleted_row() -> None:
    tenant = new_tenant_id()
    memory_id = await _seed(tenant)
    svc = PostgresService()
    assert await svc.memory_soft_delete_by_ids(tenant, [memory_id]) == 1

    landed = await svc.memory_update_embedding(memory_id, tenant, fake_embedding("v"))

    assert landed is False, "the route answers 404 for a deleted row, as its docs say"
    assert (await _row(memory_id))["embedded"] is False


async def test_l42_the_embedding_route_refuses_metadata(storage_http) -> None:
    tenant = new_tenant_id()
    memory_id = await _seed(tenant, metadata={"summary": "kept"})

    resp = await storage_http.patch(
        f"/api/v1/storage/memories/{memory_id}/embedding",
        json={
            "tenant_id": tenant,
            "embedding": fake_embedding("v"),
            "metadata": {"embedding_pending": False},
        },
    )

    assert resp.status_code == 422, resp.text
    row = await _row(memory_id)
    assert row["metadata"] == {"summary": "kept"}
    assert row["embedded"] is False


# --- L-142: every status writer stamps the change ---------------------------


async def test_l142_a_soft_delete_stamps_the_row_and_its_derived_rows() -> None:
    tenant = new_tenant_id()
    parent = await _seed(tenant)
    child = await _seed(tenant, metadata={"parent_memory_id": str(parent)})

    assert await PostgresService().memory_soft_delete_by_ids(tenant, [parent]) == 2

    for memory_id in (parent, child):
        row = await _row(memory_id)
        assert row["status"] == "deleted"
        assert row["status_changed_at"] == row["deleted_at"]


async def test_l142_the_stale_archive_stamps_the_change() -> None:
    tenant = new_tenant_id()
    old = datetime.now(UTC) - timedelta(days=200)
    memory_id = await _seed(tenant, created_at=old, weight=0.1)

    assert await PostgresService().memory_archive_stale(tenant) == 1

    row = await _row(memory_id)
    assert row["status"] == "archived"
    assert row["status_changed_at"] is not None


async def test_l142_superseding_and_restoring_insights_stamp_the_change() -> None:
    tenant = new_tenant_id()
    insight = await _seed(
        tenant,
        memory_type="insight",
        agent_id="insighter",
        metadata={"insight_focus": "gaps", "insight_scope": "agent"},
    )
    svc = PostgresService()

    out = await svc.insights_supersede_priors(
        tenant_id=tenant, agent_id="insighter", focus="gaps", scope="agent"
    )
    assert out["prior_ids"] == [str(insight)]
    superseded = await _row(insight)
    assert superseded["status"] == "outdated"
    assert superseded["status_changed_at"] is not None

    # Backdate the stamp, so the restore has to move it to count.
    a_day_ago = datetime.now(UTC) - timedelta(days=1)
    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET status_changed_at = :at WHERE id = :id"),
            {"at": a_day_ago, "id": insight},
        )
    restored = await svc.insights_restore_priors(
        tenant_id=tenant, prior_ids=[str(insight)]
    )
    assert restored == {"restored": 1}
    row = await _row(insight)
    assert row["status"] == "active"
    assert row["status_changed_at"] > a_day_ago


async def test_l142_the_retraction_path_stamps_the_change(sc) -> None:
    tenant = new_tenant_id()
    memory_id = await _seed(tenant, status="outdated")

    await sc.update_memory_status(
        str(memory_id),
        "active",
        tenant_id=tenant,
        unset_supersedes=True,
        expected_supersedes_id=str(uuid.uuid4()),
    )

    row = await _row(memory_id)
    assert row["status"] == "active"
    assert row["status_changed_at"] is not None


async def test_l142_a_retraction_that_keeps_the_status_leaves_the_stamp(sc) -> None:
    """core-api retracts by re-writing the winner's current status. That is no
    change of status, and stamping it would move a contradicted winner back
    into the contradiction window."""
    tenant = new_tenant_id()
    memory_id = await _seed(tenant, status="conflicted")

    await sc.update_memory_status(
        str(memory_id),
        "conflicted",
        tenant_id=tenant,
        unset_supersedes=True,
        expected_supersedes_id=str(uuid.uuid4()),
    )

    assert (await _row(memory_id))["status_changed_at"] is None


async def test_l142_the_expiry_sweep_leaves_the_change_unstamped() -> None:
    """Eldad, 2026-10-11: an expired row is not a contradiction. The signal
    counts ``outdated`` rows by this column, so stamping the sweep would report
    every expired row as one at the moment it expired."""
    tenant = new_tenant_id()
    memory_id = await _seed(tenant, expires_at=datetime.now(UTC) - timedelta(hours=1))

    assert await PostgresService().memory_archive_expired(tenant_id=tenant) == 1

    row = await _row(memory_id)
    assert row["status"] == "outdated"
    assert row["status_changed_at"] is None


# Status writers the scan below must find; a shrinking count means the scan
# went blind, not that writers went away.
_MIN_STATUS_WRITERS = 9
# The sweep's predicate, inline or through the constant it shares with the
# hygiene report (L-148).
_EXPIRY_SWEEP = re.compile(
    r"expires_at\s*<\s*NOW\(\)|\{_EXPIRED_LIVE_ROWS\}", re.IGNORECASE
)


def _sql(node: ast.AST) -> str | None:
    """A string's text; an f-string's with each placeholder as ``{expression}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else f"{{{ast.unparse(v.value)}}}"
            for v in node.values
        )
    return None


def _status_writers(tree: ast.AST) -> list[tuple[int, bool]]:
    """``(line, stamps the change)`` for each statement that sets a status."""
    found = []
    # An f-string's literal parts are read with it, not on their own.
    parts = {
        id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "values":
            keys = {kw.arg for kw in node.keywords}
            if "Memory" in ast.unparse(node.func.value) and "status" in keys:
                found.append((node.lineno, "status_changed_at" in keys))
        elif isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if {"status", "deleted_at"} <= keys:
                found.append((node.lineno, "status_changed_at" in keys))
        elif (source := _sql(node)) is not None and id(node) not in parts:
            sql = " ".join(source.split())
            sets = re.search(r"UPDATE memories SET (.*?) WHERE", sql, re.IGNORECASE)
            if sets and re.search(r"\bstatus\s*=", sets.group(1)):
                stamped = "status_changed_at" in sets.group(1)
                found.append((node.lineno, stamped or bool(_EXPIRY_SWEEP.search(sql))))
    return found


def test_l142_every_status_writer_stamps_the_change() -> None:
    writers = {
        f"{path.relative_to(_STORAGE)}:{line}": stamped
        for path in sorted(_STORAGE.rglob("*.py"))
        if "migrations" not in path.parts
        for line, stamped in _status_writers(ast.parse(path.read_text()))
    }
    assert len(writers) >= _MIN_STATUS_WRITERS, f"the scan found only {len(writers)}"
    unstamped = sorted(where for where, stamped in writers.items() if not stamped)
    assert not unstamped, (
        "these statements set a memory's status without status_changed_at: "
        f"{unstamped}. Stamp it in the same statement; only the expiry sweep "
        "is exempt."
    )


# --- L-148: the report lists what the sweep archives ------------------------


async def test_l148_expired_still_active_lists_what_the_sweep_archives() -> None:
    tenant = new_tenant_id()
    past = datetime.now(UTC) - timedelta(hours=1)
    by_expires_at = await _seed(tenant, expires_at=past)
    confirmed = await _seed(tenant, status="confirmed", ts_valid_end=past)
    not_yet = await _seed(tenant, expires_at=datetime.now(UTC) + timedelta(days=1))
    already_outdated = await _seed(tenant, status="outdated", expires_at=past)

    rows = await PostgresService().memory_find_expired_still_active(tenant, None)
    listed = {row[0] for row in rows}

    assert listed == {by_expires_at, confirmed}
    assert not_yet not in listed and already_outdated not in listed
