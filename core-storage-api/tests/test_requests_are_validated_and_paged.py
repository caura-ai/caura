"""Storage answers a bad request with a 4xx, a miss with a 404, and pages forward.

The storage half of the 2026-10-01 audit's API validation and pagination batch.
Each case named here failed on main: a negative LIMIT or a missing field escaped
as a 500, which core-api retries as a 503; a PATCH or DELETE of no row answered
``{"ok": true}``; an ascending admin cursor paged backwards; a ``LIKE`` wildcard
hid real nodes from a fleet's count; and the audit list carried a column the
table does not have.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from common.models.audit import AuditLog
from common.models.background_task import BackgroundTaskLog
from core_storage_api import schemas
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration]


def _tenant() -> str:
    """A fresh tenant, under the prefix the end-of-run sweep removes."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _agent(client: AsyncClient, tenant: str) -> str:
    agent_id = f"paged-agent-{uuid.uuid4().hex[:8]}"
    resp = await client.post(f"{PREFIX}/agents", json={"tenant_id": tenant, "agent_id": agent_id})
    assert resp.status_code == 200, resp.text
    return agent_id


# ── L-38: a write that matched no agent says so ──────────────────────────


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("PATCH", "/trust-level", {"trust_level": 2}),
        ("PATCH", "/fleet", {"fleet_id": "f1"}),
        ("PATCH", "/search-profile", {"search_profile": {"min_similarity": 0.4}}),
        ("DELETE", "", None),
    ],
)
async def test_l38_a_write_to_a_missing_agent_is_a_404(
    client: AsyncClient, method: str, suffix: str, body: dict | None
) -> None:
    tenant = _tenant()
    path = f"{PREFIX}/agents/no-such-agent{suffix}"
    if method == "DELETE":
        resp = await client.delete(path, params={"tenant_id": tenant})
    else:
        resp = await client.patch(path, json={"tenant_id": tenant, **(body or {})})
    assert resp.status_code == 404, resp.text


async def test_l38_another_tenants_agent_is_a_miss_too(client: AsyncClient) -> None:
    agent_id = await _agent(client, _tenant())
    resp = await client.patch(
        f"{PREFIX}/agents/{agent_id}/trust-level", json={"tenant_id": _tenant(), "trust_level": 3}
    )
    assert resp.status_code == 404, resp.text


# ── L-140: one path, one meaning for {agent_id} ──────────────────────────


async def test_l140_the_search_profile_patch_takes_the_agent_id_its_siblings_take(
    client: AsyncClient,
) -> None:
    """GET and POST /reset under the same path read ``{agent_id}`` as the
    agent's own id; PATCH read it as the row's primary key."""
    tenant = _tenant()
    agent_id = await _agent(client, tenant)
    profile = {"min_similarity": 0.4}

    resp = await client.patch(
        f"{PREFIX}/agents/{agent_id}/search-profile",
        json={"tenant_id": tenant, "search_profile": profile},
    )
    assert resp.status_code == 200, resp.text
    got = await client.get(f"{PREFIX}/agents/{agent_id}/search-profile", params={"tenant_id": tenant})
    assert got.json()["search_profile"] == profile


# ── L-39: a keystone can be shortened ────────────────────────────────────


async def test_l39_a_long_keystone_can_be_rewritten_short(client: AsyncClient) -> None:
    """The documents shrink guard is for client-synced documents, where an empty
    payload from a failed read would wipe a file. A keystone is a short policy
    record written at trust 2 or more, and nothing could pass the guard's
    override, so a rule of 2KB or more could never be cut down: it was a 500."""
    tenant = _tenant()
    rule = {"doc_id": "long-rule", "title": "long-rule", "scope": "tenant", "weight": "med"}
    first = await client.post(
        f"{PREFIX}/keystones", json={"tenant_id": tenant, **rule, "content": "x" * 4000}
    )
    assert first.status_code == 200, first.text

    short = await client.post(
        f"{PREFIX}/keystones", json={"tenant_id": tenant, **rule, "content": "Do less."}
    )
    assert short.status_code == 200, short.text
    assert short.json()["data"]["content"] == "Do less."


# ── L-40: page bounds are checked where they enter SQL ───────────────────


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/memories/near-duplicates", {"batch_size": 0}),
        ("/memories/near-duplicates", {"offset": -1}),
        ("/memories/archive-expired", {"batch_size": 0}),
        ("/memories/archive-stale", {"batch_size": -1}),
        ("/memories/list", {"offset": -1}),
        ("/memories/admin-list", {"offset": -1}),
        ("/memories/admin-list", {"limit": 0}),
    ],
)
async def test_l40_a_bad_page_bound_in_a_body_is_a_422(client: AsyncClient, path: str, body: dict) -> None:
    """A negative LIMIT or OFFSET is a Postgres error (a 500), and a batch of 0
    makes a lifecycle sweep archive nothing while reporting success."""
    resp = await client.post(f"{PREFIX}{path}", json={"tenant_id": _tenant(), **body})
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("path", ["/memories/recent", "/memories/dedup-reviews"])
async def test_l40_a_bad_limit_in_a_query_is_a_422(client: AsyncClient, path: str) -> None:
    resp = await client.get(f"{PREFIX}{path}", params={"tenant_id": _tenant(), "limit": -1})
    assert resp.status_code == 422, resp.text


# ── L-41: the status route validates what it reads ───────────────────────


async def test_l41_a_status_patch_without_a_status_is_a_422(client: AsyncClient) -> None:
    resp = await client.patch(f"{PREFIX}/memories/{uuid.uuid4()}/status", json={"tenant_id": _tenant()})
    assert resp.status_code == 422, resp.text


async def test_l41_a_malformed_expected_pointer_is_a_422(client: AsyncClient) -> None:
    resp = await client.patch(
        f"{PREFIX}/memories/{uuid.uuid4()}/status",
        json={
            "tenant_id": _tenant(),
            "status": "active",
            "unset_supersedes": True,
            "expected_supersedes_id": "not-a-uuid",
        },
    )
    assert resp.status_code == 422, resp.text


# ── L-45: a cursor pages the way the page is ordered ─────────────────────


async def test_l45_an_ascending_admin_cursor_pages_forward(client: AsyncClient) -> None:
    tenant = _tenant()
    for _ in range(3):
        resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant, "f1"))
        assert resp.status_code in (200, 201), resp.text

    first = await client.post(
        f"{PREFIX}/memories/admin-list", json={"tenant_id": tenant, "order": "asc", "limit": 10}
    )
    assert first.status_code == 200, first.text
    rows = first.json()
    assert len(rows) == 3

    after_oldest = await client.post(
        f"{PREFIX}/memories/admin-list",
        json={
            "tenant_id": tenant,
            "order": "asc",
            "limit": 10,
            "cursor_ts": rows[0]["created_at"],
            "cursor_id": rows[0]["id"],
        },
    )
    assert after_oldest.status_code == 200, after_oldest.text
    assert [r["id"] for r in after_oldest.json()] == [r["id"] for r in rows[1:]]


@pytest.mark.parametrize("path", ["/memories/admin-list", "/memories/list"])
async def test_l45_a_cursor_on_another_sort_is_refused(client: AsyncClient, path: str) -> None:
    """The cursor is a ``(created_at, id)`` position, so on any other sort it
    skips and repeats rows across pages."""
    resp = await client.post(
        f"{PREFIX}{path}",
        json={
            "tenant_id": _tenant(),
            "sort": "weight",
            "cursor_ts": datetime.now(UTC).isoformat(),
            "cursor_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422, resp.text


# ── L-53: only the sentinel is left out of a fleet's node count ──────────


async def test_l53_a_node_named_like_the_sentinel_still_counts(client: AsyncClient) -> None:
    """``startswith("_fleet_")`` rendered ``LIKE '_fleet_%'``, where ``_`` matches
    any character, so ``xfleet1`` was dropped from the count as a sentinel."""
    tenant = _tenant()
    for name in ("xfleet1", "_fleet_marker"):
        resp = await client.post(
            f"{PREFIX}/fleet/nodes",
            json={
                "tenant_id": tenant,
                "fleet_id": "f1",
                "node_name": name,
                "hostname": "h1",
                "last_heartbeat": datetime.now(UTC).isoformat(),
            },
        )
        assert resp.status_code == 200, resp.text

    resp = await client.get(f"{PREFIX}/fleet", params={"tenant_id": tenant})
    assert resp.status_code == 200, resp.text
    assert [(r["fleet_id"], r["node_count"]) for r in resp.json()] == [("f1", 1)]


# ── L-141: apply-weights checks its ids as filter-by-scope does ──────────


async def test_l141_a_malformed_id_is_a_422(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/evolve/apply-weights",
        json={"tenant_id": _tenant(), "ids": ["not-a-uuid"], "delta": 0.1, "floor": 0.0, "cap": 1.0},
    )
    assert resp.status_code == 422, resp.text


# ── L-143: a field list names only columns its model has ────────────────


def test_l143_audit_rows_carry_only_columns_the_table_has() -> None:
    """``orm_to_dict`` reads a missing attribute as null, so a listed name the
    model lacks ships as a field that is always null: ``fleet_id`` on every
    audit row read as "tenant-wide" rather than "no such field"."""
    assert set(schemas.AUDIT_LOG_FIELDS) <= set(AuditLog.__table__.columns.keys())
    task_columns = set(BackgroundTaskLog.__table__.columns.keys())
    assert set(getattr(schemas, "BACKGROUND_TASK_FIELDS", ())) <= task_columns
