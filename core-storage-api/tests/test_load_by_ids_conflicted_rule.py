"""``/memories/load-by-ids`` applies the scored path's conflicted rule when given the query.

The ENTITY_LOOKUP short-circuit loads its pool through this route. It used to keep
every ``conflicted`` row, so a present-state question that took the short-circuit
was served the stale side of a correction next to the correction itself, while the
same question on the scored path was not. With the query forwarded, a conflicted
row is kept only when it matches the query and its successor does not; without the
query (an older core-api) the previous rule stays.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX
from tests.test_scored_search_explain import _sql, _write

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def scope() -> tuple[str, str, str]:
    run = uuid.uuid4().hex[:8]
    return f"lbi-{run}", f"fleet-{run}", f"lbikw{run}"


async def _supersede(old: str, new: str, status: str = "conflicted") -> None:
    await _sql("UPDATE memories SET status = :s WHERE id = :id", s=status, id=uuid.UUID(old))
    await _sql(
        "UPDATE memories SET supersedes_id = :old WHERE id = :id", old=uuid.UUID(old), id=uuid.UUID(new)
    )


async def _load(client: AsyncClient, tenant: str, ids: list[str], query: str | None) -> set[str]:
    body: dict = {"tenant_id": tenant, "memory_ids": ids}
    if query is not None:
        body["query"] = query
    resp = await client.post(f"{PREFIX}/memories/load-by-ids", json=body)
    assert resp.status_code == 200, resp.text
    return {r["id"] for r in resp.json()}


async def test_conflicted_hidden_when_successor_matches(client: AsyncClient, scope) -> None:
    tenant, fleet, kw = scope
    old = await _write(client, tenant, fleet, kw)
    new = await _write(client, tenant, fleet, kw)
    await _supersede(old, new)
    assert await _load(client, tenant, [old, new], kw) == {new}


async def test_conflicted_kept_when_successor_does_not_match(client: AsyncClient, scope) -> None:
    tenant, fleet, kw = scope
    old = await _write(client, tenant, fleet, kw)
    new = await _write(client, tenant, fleet, f"{kw}other")
    await _supersede(old, new)
    # The successor (different keyword) does not match the query, so the old row is
    # kept: it is the row the query names. Active rows are not text-filtered here.
    assert await _load(client, tenant, [old, new], kw) == {old, new}


async def test_conflicted_not_matching_query_is_hidden(client: AsyncClient, scope) -> None:
    tenant, fleet, kw = scope
    old = await _write(client, tenant, fleet, kw)
    new = await _write(client, tenant, fleet, kw)
    await _supersede(old, new)
    assert old not in await _load(client, tenant, [old, new], "zqxv unrelated")


async def test_without_query_the_previous_rule_holds(client: AsyncClient, scope) -> None:
    tenant, fleet, kw = scope
    old = await _write(client, tenant, fleet, kw)
    new = await _write(client, tenant, fleet, kw)
    await _supersede(old, new)
    assert await _load(client, tenant, [old, new], None) == {old, new}


async def test_outdated_always_excluded(client: AsyncClient, scope) -> None:
    tenant, fleet, kw = scope
    old = await _write(client, tenant, fleet, kw)
    new = await _write(client, tenant, fleet, f"{kw}other")
    await _supersede(old, new, status="outdated")
    assert old not in await _load(client, tenant, [old, new], kw)
    assert old not in await _load(client, tenant, [old, new], None)
