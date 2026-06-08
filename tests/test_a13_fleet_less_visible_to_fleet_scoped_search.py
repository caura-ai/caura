"""A13 — Fleet-less writes MUST be visible to fleet-scoped searches.

Contract under test: when an agent writes a memory **without** specifying
``fleet_id``, that memory must still surface for a later
``POST /api/v1/search`` that **does** pass ``fleet_ids=[...]``.

Storage's fleet predicate is expected to be an ``OR (memories.fleet_id IS NULL)``
fallback so single-fleet deployments — and any caller that hasn't adopted
fleets yet — don't silently lose their data behind a strict-equality match.

The clauses pinned here:

  1. Fleet-less write → fleet-scoped search (the regression that motivated
     A13). The fleet-less row must surface.
  2. Fleet=X write → fleet=X search (sanity — strict-match still works).
  3. Fleet=X write → fleet=Y search MUST NOT surface. The OR-IS-NULL widens
     for nulls only; cross-fleet isolation still holds.
  4. Fleet-less write → unscoped search (sanity — fleet_ids omitted matches
     everything in the tenant).
  5. (Bonus) ``visibility=scope_org`` crosses fleet boundaries on its own
     — fleet=X + scope_org seen by a fleet=Y search.

Black-box through the HTTP API; matches the style of
``tests/test_pre_public_cleanup_regressions.py`` and
``tests/test_api_memories.py``. ``write_mode="strong"`` keeps embedding /
enrichment synchronous so the assertion is deterministic — the row is
queryable the moment the 201 lands.
"""

from __future__ import annotations

import uuid


from tests.conftest import get_test_auth, uid as _uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nonce() -> str:
    """Per-test unique substring so the search assertion is precise."""
    return f"a13-test-{uuid.uuid4().hex}"


async def _write_memory(
    client,
    tenant_id: str,
    headers: dict,
    *,
    content: str,
    agent_id: str,
    fleet_id: str | None,
    visibility: str = "scope_team",
    memory_type: str = "fact",
) -> dict:
    """POST /api/v1/memories with strong-mode write — fleet_id is OMITTED
    from the JSON body entirely when None (the contract is about fleet-less
    rows, not rows with ``fleet_id=null`` strings or empty strings).
    """
    body: dict = {
        "tenant_id": tenant_id,
        "content": content,
        "agent_id": agent_id,
        "memory_type": memory_type,
        "visibility": visibility,
        "write_mode": "strong",
    }
    if fleet_id is not None:
        body["fleet_id"] = fleet_id
    resp = await client.post("/api/v1/memories", json=body, headers=headers)
    assert resp.status_code == 201, f"Write failed: {resp.text}"
    return resp.json()


async def _delete_memory(client, tenant_id: str, headers: dict, memory_id: str) -> None:
    """Best-effort cleanup; don't fail the test on a 404 if a prior assert
    already failed and short-circuited the test."""
    try:
        await client.delete(
            f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
            headers=headers,
        )
    except Exception:
        pass


async def _search_ids(
    client,
    tenant_id: str,
    headers: dict,
    *,
    query: str,
    fleet_ids: list[str] | None = None,
    top_k: int = 10,
) -> list[str]:
    """POST /api/v1/search and return the list of result memory IDs.

    ``fleet_ids=None`` omits the field from the body (unscoped search).
    """
    body: dict = {"tenant_id": tenant_id, "query": query, "top_k": top_k}
    if fleet_ids is not None:
        body["fleet_ids"] = fleet_ids
    resp = await client.post("/api/v1/search", json=body, headers=headers)
    assert resp.status_code == 200, f"Search failed: {resp.text}"
    data = resp.json()
    assert isinstance(data, dict) and "items" in data, (
        f"search must return {{items: [...]}} envelope, got: {data!r}"
    )
    return [m["id"] for m in data["items"]]


# ---------------------------------------------------------------------------
# 1. The regression the gap closes: fleet-less write → fleet-scoped search.
# ---------------------------------------------------------------------------


async def test_fleet_less_write_visible_to_fleet_scoped_search(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    nonce = _nonce()
    agent_id = f"a13-agent-{tag}"
    other_fleet = f"a13-other-fleet-{tag}"

    # Write WITHOUT fleet_id — the row lands with fleet_id NULL on the
    # memories table.
    mem = await _write_memory(
        client,
        tenant_id,
        headers,
        content=f"Project decisions memo {nonce}",
        agent_id=agent_id,
        fleet_id=None,
    )
    memory_id = mem["id"]
    assert mem.get("fleet_id") in (None, ""), (
        f"Pre-condition: write without fleet_id must land fleet-less; "
        f"got fleet_id={mem.get('fleet_id')!r}. If this fails, the rest of "
        f"the test is invalid — the contract being verified concerns the "
        f"NULL row, not a row with an auto-assigned fleet."
    )

    try:
        ids = await _search_ids(
            client,
            tenant_id,
            headers,
            query=f"Project decisions memo {nonce}",
            fleet_ids=[other_fleet],
        )
        assert memory_id in ids, (
            f"A13 violation: fleet-less memory {memory_id} not visible to a "
            f"fleet-scoped search (fleet_ids=[{other_fleet!r}]). Fleet-less "
            f"writes are silently invisible — storage's fleet predicate is "
            f"missing the OR (fleet_id IS NULL) fallback clause."
        )
    finally:
        await _delete_memory(client, tenant_id, headers, memory_id)


# ---------------------------------------------------------------------------
# 2. Sanity: fleet=X write IS visible to fleet=X search.
# ---------------------------------------------------------------------------


async def test_fleet_X_write_visible_to_fleet_X_search(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    nonce = _nonce()
    agent_id = f"a13-agent-{tag}"
    fleet_x = f"a13-fleet-X-{tag}"

    mem = await _write_memory(
        client,
        tenant_id,
        headers,
        content=f"Same-fleet visibility check {nonce}",
        agent_id=agent_id,
        fleet_id=fleet_x,
    )
    memory_id = mem["id"]

    try:
        ids = await _search_ids(
            client,
            tenant_id,
            headers,
            query=f"Same-fleet visibility check {nonce}",
            fleet_ids=[fleet_x],
        )
        assert memory_id in ids, (
            "Sanity violation: a fleet=X write must be visible to a "
            "fleet=X-scoped search. If this fails the fleet predicate is "
            "broken at the equality level, not just the IS NULL fallback."
        )
    finally:
        await _delete_memory(client, tenant_id, headers, memory_id)


# ---------------------------------------------------------------------------
# 3. Isolation: fleet=X write is NOT visible to a fleet=Y search.
#    The OR-IS-NULL clause widens for nulls only.
# ---------------------------------------------------------------------------


async def test_fleet_X_write_NOT_visible_to_fleet_Y_search(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    nonce = _nonce()
    agent_id = f"a13-agent-{tag}"
    fleet_x = f"a13-fleet-X-{tag}"
    fleet_y = f"a13-fleet-Y-{tag}"

    mem = await _write_memory(
        client,
        tenant_id,
        headers,
        content=f"Cross-fleet isolation marker {nonce}",
        agent_id=agent_id,
        fleet_id=fleet_x,
    )
    memory_id = mem["id"]

    try:
        ids = await _search_ids(
            client,
            tenant_id,
            headers,
            query=f"Cross-fleet isolation marker {nonce}",
            fleet_ids=[fleet_y],
        )
        assert memory_id not in ids, (
            "Cross-fleet leak: a fleet=X write surfaced in a fleet=Y "
            "search. The OR (fleet_id IS NULL) fallback was misimplemented "
            "as OR TRUE — it must match NULL rows only, not all rows."
        )
    finally:
        await _delete_memory(client, tenant_id, headers, memory_id)


# ---------------------------------------------------------------------------
# 4. Sanity: fleet-less write visible to an unscoped (no fleet_ids) search.
# ---------------------------------------------------------------------------


async def test_fleet_less_write_visible_to_unscoped_search(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    nonce = _nonce()
    agent_id = f"a13-agent-{tag}"

    mem = await _write_memory(
        client,
        tenant_id,
        headers,
        content=f"Unscoped lookup baseline {nonce}",
        agent_id=agent_id,
        fleet_id=None,
    )
    memory_id = mem["id"]

    try:
        ids = await _search_ids(
            client,
            tenant_id,
            headers,
            query=f"Unscoped lookup baseline {nonce}",
            fleet_ids=None,  # omit fleet_ids entirely
        )
        assert memory_id in ids, (
            f"Sanity violation: fleet-less memory {memory_id} not visible "
            f"to an unscoped search. If this fails the row is unreachable "
            f"by any caller — write/search aren't even joined correctly."
        )
    finally:
        await _delete_memory(client, tenant_id, headers, memory_id)


# ---------------------------------------------------------------------------
# 5. Bonus contract clause: scope_org rides past fleet boundaries on its own.
#    A fleet=X + visibility=scope_org row must surface in a fleet=Y search.
#    This is the third OR branch of the predicate
#    (``fleet_id = ANY(:ids) OR fleet_id IS NULL OR visibility = 'scope_org'``).
# ---------------------------------------------------------------------------


async def test_scope_org_visibility_crosses_fleet_boundary(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    nonce = _nonce()
    agent_id = f"a13-agent-{tag}"
    fleet_x = f"a13-fleet-X-{tag}"
    fleet_y = f"a13-fleet-Y-{tag}"

    mem = await _write_memory(
        client,
        tenant_id,
        headers,
        content=f"Org-wide announcement reaches every fleet {nonce}",
        agent_id=agent_id,
        fleet_id=fleet_x,
        visibility="scope_org",
    )
    memory_id = mem["id"]

    try:
        ids = await _search_ids(
            client,
            tenant_id,
            headers,
            query=f"Org-wide announcement reaches every fleet {nonce}",
            fleet_ids=[fleet_y],
        )
        assert memory_id in ids, (
            "scope_org should ignore fleet boundaries: a fleet=X + "
            "scope_org memory must be visible to a fleet=Y search. The "
            "third OR clause (visibility='scope_org') is missing from "
            "the fleet predicate or AND'd where it should be OR'd."
        )
    finally:
        await _delete_memory(client, tenant_id, headers, memory_id)
