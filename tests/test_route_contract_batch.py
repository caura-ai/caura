"""Audit batch: route parameters that lied, and inputs that were not validated.

Seven findings on the HTTP boundary. Most share one shape — the route declares
something and then does not honour it — and the rest are inputs that reached
storage unchecked and came back as 500s.

  * OSS 09/02 L-23 — ``next_cursor`` minted for sorts the same route then 400s.
  * OSS 08/14 M-11 — ``GET /audit-log`` ignored ``since``.
  * OSS 09/02 M-26 — ``GET /fleet/commands`` ignored ``node_id``.
  * OSS 09/02 M-27 — ``GET /memories`` dropped ``visibility``.
  * OSS 08/14 L-10 — ``purge-data`` 500'd on bodies its preview twin rejects.
  * OSS 09/02 L-24 — unvalidated ``ids`` / ``exclude_ids`` became gateway 500s.
  * OSS 09/02 L-22 — ``related_ids`` uncapped at the boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, patch

import pytest

from core_api import mcp_server
from core_api.constants import EVOLVE_MAX_RELATED_IDS
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = pytest.mark.asyncio


# ── OSS 09/02 L-23 ──────────────────────────────────────────────────────────


async def test_a_sort_without_cursor_support_returns_no_cursor(client):
    """Never hand back a token this endpoint would refuse.

    The gate 400s a cursor unless ``sort=created_at, order=desc``; the mint was
    unconditional. Following the documented pagination contract — take
    ``next_cursor``, send it back — was therefore the way to get a 400.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    for i in range(3):
        r = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": f"cursor-agent-{tag}",
                "memory_type": "fact",
                "content": f"L-23 cursor probe {tag} number {i} with enough body to store.",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text

    resp = await client.get(
        "/api/v1/memories",
        params={
            "tenant_id": tenant_id,
            "agent_id": f"cursor-agent-{tag}",
            "sort": "weight",
            "order": "desc",
            "limit": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["next_cursor"] is None, (
        "minted a cursor for sort=weight, which this same endpoint rejects with 400"
    )


async def test_the_supported_sort_still_returns_a_usable_cursor(client):
    """The fix must not stop paginating the sort that does work."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    for i in range(3):
        await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": f"cursor-ok-{tag}",
                "memory_type": "fact",
                "content": f"L-23 supported-sort probe {tag} number {i}, long enough to store.",
            },
            headers=headers,
        )

    resp = await client.get(
        "/api/v1/memories",
        params={
            "tenant_id": tenant_id,
            "agent_id": f"cursor-ok-{tag}",
            "sort": "created_at",
            "order": "desc",
            "limit": 1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    cursor = resp.json()["next_cursor"]
    assert cursor, "created_at/desc must still paginate"

    # And the token it minted is one it accepts back.
    nxt = await client.get(
        "/api/v1/memories",
        params={
            "tenant_id": tenant_id,
            "agent_id": f"cursor-ok-{tag}",
            "sort": "created_at",
            "order": "desc",
            "limit": 1,
            "cursor": cursor,
        },
        headers=headers,
    )
    assert nxt.status_code == 200, nxt.text


async def test_admin_list_also_refuses_to_mint_an_unusable_cursor(client):
    """The admin twin had the identical split, and it is the riskier one.

    ``/admin/memories`` lists across ALL tenants with no visibility scoping, so
    a caller following its own ``next_cursor`` gets a 400 on a cross-tenant
    page. Same gate, same mint, same drift.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    for i in range(3):
        r = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": f"admin-cursor-{tag}",
                "memory_type": "fact",
                "content": f"L-23 admin cursor probe {tag} number {i}, long enough to store.",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text

    admin_headers = {"X-API-Key": "test-admin-key"}
    resp = await client.get(
        "/api/v1/admin/memories",
        params={"sort": "weight", "order": "desc", "limit": 1},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["next_cursor"] is None, (
        "admin list minted a cursor for sort=weight, which it rejects with 400"
    )


# ── OSS 08/14 M-11 ──────────────────────────────────────────────────────────


async def test_audit_log_since_actually_filters(client):
    """``since`` reaches the SQL that has always supported it.

    On an audit log a dropped time filter is the worst shape of wrong: the
    caller gets the unfiltered tail, and the extra entries read as events
    inside the window they asked for.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    r = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"audit-since-{tag}",
            "memory_type": "fact",
            "content": f"M-11 since probe {tag}, long enough to be stored happily.",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text

    unfiltered = await client.get(
        "/api/v1/audit-log", params={"tenant_id": tenant_id}, headers=headers
    )
    assert unfiltered.status_code == 200, unfiltered.text
    assert len(unfiltered.json()) >= 1

    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    filtered = await client.get(
        "/api/v1/audit-log",
        params={"tenant_id": tenant_id, "since": future},
        headers=headers,
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json() == [], (
        "entries returned for a window starting an hour from now — ``since`` "
        "is still being dropped"
    )


# ── OSS 09/02 M-26 ──────────────────────────────────────────────────────────


async def test_fleet_commands_forwards_node_id(client):
    """The parameter and the docstring both promised a filter never applied."""
    from core_api.routes import fleet

    tenant_id, headers = get_test_auth()
    node_id = uuid.uuid4()
    seen: dict = {}

    async def _list_commands(**kwargs):
        seen.update(kwargs)
        return []

    sc = AsyncMock()
    sc.list_commands = _list_commands
    with patch.object(fleet, "get_storage_client", return_value=sc):
        resp = await client.get(
            "/api/v1/fleet/commands",
            params={"tenant_id": tenant_id, "node_id": str(node_id)},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    assert seen.get("node_id") == str(node_id), (
        f"node_id never reached the storage client: {seen}"
    )


# ── OSS 09/02 M-27 ──────────────────────────────────────────────────────────


async def test_memories_visibility_filter_narrows_the_page(client):
    """A narrowing filter, ANDed onto a query the scoping predicate bounds."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    for vis in ("scope_team", "scope_org"):
        r = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": f"vis-agent-{tag}",
                "memory_type": "fact",
                "visibility": vis,
                "content": f"M-27 visibility probe {tag} for {vis}, stored with room to spare.",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text

    resp = await client.get(
        "/api/v1/memories",
        params={
            "tenant_id": tenant_id,
            "agent_id": f"vis-agent-{tag}",
            "visibility": "scope_org",
            "limit": 50,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    returned = {m["visibility"] for m in resp.json()["items"]}
    # Equality, not subset: ``<=`` is satisfied by the empty set, so it would
    # also pass if the filter over-narrowed to nothing and the scope_team seed
    # row below became decorative.
    assert returned == {"scope_org"}, f"visibility filter wrong; got {returned}"


# ── OSS 08/14 L-10 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("body", [[], "nope", 3])
async def test_purge_data_rejects_non_object_bodies(client, body):
    """The destructive route was laxer than its read-only twin.

    ``preview-data`` grew both guards in review; ``purge-data`` — which
    permanently deletes — never got them, so a non-object body reached
    ``.get`` and surfaced as a 500.
    """
    _tenant_id, headers = get_test_auth()
    resp = await client.post("/api/v1/admin/org/purge-data", json=body, headers=headers)
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"


# ── OSS 09/02 L-24 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ids",
    ["abc", ["not-a-uuid"], [123]],
    ids=["string-not-list", "non-uuid-element", "non-string-element"],
)
async def test_bulk_delete_rejects_malformed_ids(client, ids):
    """A caller's typo is their 4xx, not our 500."""
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories/bulk-delete",
        json={"tenant_id": tenant_id, "ids": ids},
        headers=headers,
    )
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"


@pytest.mark.parametrize("exclude_ids", ["abc", ["not-a-uuid"], [123], {"a": 1}])
async def test_delete_all_rejects_malformed_exclude_ids(client, exclude_ids):
    """A malformed ``exclude_ids`` must not disarm the unbounded-delete guard.

    This one does not merely 500 later. ``exclude_ids`` feeds the
    ``is_tenant_wide`` test, which asks whether every narrowing input is absent
    — and a non-empty string is present and truthy. So ``exclude_ids="abc"``
    read as "this delete is narrowed" while narrowing nothing, turning the
    tenant-wide safety check off on a request that would then delete the
    tenant. It must be refused before it reaches that test.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.request(
        "DELETE",
        "/api/v1/memories",
        json={"tenant_id": tenant_id, "exclude_ids": exclude_ids},
        headers=headers,
    )
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"


# ── OSS 09/02 L-22 ──────────────────────────────────────────────────────────


async def test_evolve_rejects_oversized_related_ids(client):
    """Bounded where the caller can be told, not silently truncated later.

    The 50-id cap lives in ``_adjust_weights``, which runs after
    ``_persist_outcome`` — so an oversized list was written verbatim into the
    outcome memory's metadata and only then cut down.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/evolve/report",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"evolve-{_uid()}",
            "outcome": "L-22 probe: too many related ids",
            "outcome_type": "success",
            "related_ids": [str(uuid.uuid4()) for _ in range(51)],
        },
        headers=headers,
    )
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"


# ── OSS 09/02 L-23 (MCP surface) ────────────────────────────────────────────


async def test_mcp_list_does_not_mint_a_cursor_for_an_unsupported_sort(
    mcp_env, monkeypatch
):
    """The third copy of the gate/mint pair, and the one still drifted.

    ``caura_list`` rejects an incoming cursor unless ``created_at``/``desc``
    (``test_mcp_list.test_list_cursor_with_non_default_sort_errors``) but minted
    one for every sort. This is the agent-facing surface, so it was also the
    highest-traffic instance of the bug.
    """
    rows = [
        {"id": str(uuid.uuid4()), "created_at": datetime.now(UTC).isoformat()}
        for _ in range(3)
    ]
    stub_storage_client(monkeypatch, list_memories_by_filters=rows)
    monkeypatch.setattr(
        mcp_server,
        "_memory_to_out",
        lambda m: SimpleNamespace(model_dump=lambda mode="python": m),
    )

    out = await mcp_server.caura_list(limit=2, sort="weight")
    payload = parse_envelope(out)
    assert payload["count"] == 2, "the page itself must be unaffected"
    assert payload["next_cursor"] is None, (
        "minted a cursor for sort=weight, which caura_list itself rejects"
    )


async def test_mcp_list_still_mints_for_the_supported_sort(mcp_env, monkeypatch):
    """The gate must not be so tight that the working sort stops paginating."""
    rows = [
        {"id": str(uuid.uuid4()), "created_at": datetime.now(UTC).isoformat()}
        for _ in range(3)
    ]
    stub_storage_client(monkeypatch, list_memories_by_filters=rows)
    monkeypatch.setattr(
        mcp_server,
        "_memory_to_out",
        lambda m: SimpleNamespace(model_dump=lambda mode="python": m),
    )

    out = await mcp_server.caura_list(limit=2)  # created_at/desc are the defaults
    assert parse_envelope(out)["next_cursor"] is not None


# ── OSS 09/02 L-22 (MCP surface) ────────────────────────────────────────────


def _declared_max_length(fn, param: str) -> int | None:
    """Return the ``max_length`` an MCP tool declares for one parameter.

    Introspection rather than a call, because FastMCP applies these constraints
    at the tool-call boundary — invoking the Python function directly would skip
    them and the test would pass for the wrong reason.

    ``get_type_hints(..., include_extras=True)`` to resolve the ``Annotated``
    metadata, then the pydantic-v2 shape: ``Field(max_length=...)`` is not an
    attribute of the ``FieldInfo``, it is an ``annotated_types.MaxLen`` entry in
    its ``.metadata`` list. Reading it as an attribute finds nothing and, inside
    a generator expression, surfaces as a bare ``StopIteration`` rather than a
    failed assertion.
    """
    annotation = get_type_hints(fn, include_extras=True)[param]
    for meta in annotation.__metadata__:
        for constraint in getattr(meta, "metadata", []):
            if (value := getattr(constraint, "max_length", None)) is not None:
                return value
    return None


async def test_mcp_evolve_caps_related_ids_like_rest():
    """Both boundaries reach the same ``report_outcome``; both must bound it.

    Capping only REST left an MCP caller with exactly the behaviour the cap
    exists to prevent: an oversized list written verbatim into the outcome
    memory's metadata by ``_persist_outcome``, then silently cut to 50 by
    ``_adjust_weights`` with a log line nobody reads.
    """
    assert (
        _declared_max_length(mcp_server.caura_evolve, "related_ids")
        == EVOLVE_MAX_RELATED_IDS
    )
