"""A contradiction's further losers show what replaced them (M-34).

``supersedes_id`` is one column, so a verdict wires its winner to one loser only,
and every other loser it demotes is named only by its ``memory_conflicts``
record (#1815). The lookups behind "corrected by" read only ``supersedes_id``:
search's successor injection (``find-successors``) and the contradiction rows
behind ``GET /memories/{id}/contradictions`` and MCP lineage found nothing for
such a loser. They now also take the other side of an undismissed record naming
a demoted memory, when that side is the newer one, the winner ``_pick_older``
chose, and search holds it to the same scope as an edge's successor.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"


async def _memory(client: AsyncClient, tenant_id: str, fleet_id: str, agent_id: str = "m34-tester") -> str:
    """A live memory; each one is created after, so newer than, the last."""
    content = f"recorded successor {uuid.uuid4().hex}"
    resp = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": "fact",
            "content": content,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "visibility": "scope_team",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _status(client: AsyncClient, tenant_id: str, memory_id: str, status: str, **edge: str) -> None:
    resp = await client.patch(
        f"{PREFIX}/memories/{memory_id}/status", json={"tenant_id": tenant_id, "status": status, **edge}
    )
    assert resp.status_code == 200, resp.text


async def _record(client: AsyncClient, tenant_id: str, fleet_id: str, new_id: str, old_id: str) -> str:
    resp = await client.post(
        f"{PREFIX}/memories/conflicts",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "new_memory_id": new_id,
            "old_memory_id": old_id,
            "relationship": "exact_value",
            "action": "supersede",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _successors(
    client: AsyncClient, tenant_id: str, loser_id: str, **scope
) -> list[tuple[str, str | None]]:
    """``(id, successor_of)`` for each successor search would inject for the loser."""
    resp = await client.post(
        f"{PREFIX}/memories/find-successors",
        json={"supersedes_ids": [loser_id], "tenant_id": tenant_id, **scope},
    )
    assert resp.status_code == 200, resp.text
    return [(row["id"], row.get("successor_of")) for row in resp.json()]


async def _supersessors(client: AsyncClient, tenant_id: str, memory_id: str) -> list[str]:
    resp = await client.get(f"{PREFIX}/memories/{memory_id}/contradictions", params={"tenant_id": tenant_id})
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()["supersessors"]]


async def test_search_finds_the_winner_only_a_record_names(client, tenant_id, fleet_id) -> None:
    loser = await _memory(client, tenant_id, fleet_id)
    winner = await _memory(client, tenant_id, fleet_id)
    await _status(client, tenant_id, loser, "conflicted")
    await _record(client, tenant_id, fleet_id, winner, loser)

    assert await _successors(client, tenant_id, loser) == [(winner, loser)]


async def test_a_flipped_record_names_its_newer_side(client, tenant_id, fleet_id) -> None:
    """Detection ran for the older memory, which lost: the record's
    ``new_memory_id`` is the loser."""
    loser = await _memory(client, tenant_id, fleet_id)
    winner = await _memory(client, tenant_id, fleet_id)
    await _status(client, tenant_id, loser, "conflicted")
    await _record(client, tenant_id, fleet_id, loser, winner)

    assert await _successors(client, tenant_id, loser) == [(winner, loser)]
    assert await _supersessors(client, tenant_id, loser) == [winner]


async def test_the_contradiction_rows_list_the_winner_only_a_record_names(
    client, tenant_id, fleet_id
) -> None:
    loser = await _memory(client, tenant_id, fleet_id)
    winner = await _memory(client, tenant_id, fleet_id)
    await _status(client, tenant_id, loser, "conflicted")
    await _record(client, tenant_id, fleet_id, winner, loser)

    assert await _supersessors(client, tenant_id, loser) == [winner]


async def test_an_edge_successor_names_the_row_it_replaced(client, tenant_id, fleet_id) -> None:
    """And a pair both an edge and a record join comes back once."""
    loser = await _memory(client, tenant_id, fleet_id)
    winner = await _memory(client, tenant_id, fleet_id)
    await _status(client, tenant_id, loser, "conflicted")
    await _status(client, tenant_id, winner, "active", supersedes_id=loser)
    await _record(client, tenant_id, fleet_id, winner, loser)

    assert await _successors(client, tenant_id, loser) == [(winner, loser)]
    assert await _supersessors(client, tenant_id, loser) == [winner]


async def test_a_dismissed_record_names_no_successor(client, tenant_id, fleet_id) -> None:
    loser = await _memory(client, tenant_id, fleet_id)
    winner = await _memory(client, tenant_id, fleet_id)
    await _status(client, tenant_id, loser, "conflicted")
    conflict = await _record(client, tenant_id, fleet_id, winner, loser)
    resp = await client.patch(
        f"{PREFIX}/memories/memory-conflicts/{conflict}/resolve",
        json={"tenant_id": tenant_id, "review_status": "dismissed"},
    )
    assert resp.status_code == 200, resp.text

    assert await _successors(client, tenant_id, loser) == []
    assert await _supersessors(client, tenant_id, loser) == []


async def test_the_older_side_of_a_record_is_not_a_successor(client, tenant_id, fleet_id) -> None:
    """The record's loser is its older side; the newer row is demoted for some
    other reason, and the older one did not replace it."""
    older = await _memory(client, tenant_id, fleet_id)
    newer = await _memory(client, tenant_id, fleet_id)
    await _status(client, tenant_id, newer, "conflicted")
    await _record(client, tenant_id, fleet_id, newer, older)

    assert await _successors(client, tenant_id, newer) == []
    assert await _supersessors(client, tenant_id, newer) == []


async def test_a_memory_back_in_force_has_no_recorded_successor(client, tenant_id, fleet_id) -> None:
    """A record outlives the verdict it carries: a reviewer can put the loser
    back without dismissing it."""
    loser = await _memory(client, tenant_id, fleet_id)
    winner = await _memory(client, tenant_id, fleet_id)
    await _record(client, tenant_id, fleet_id, winner, loser)

    assert await _successors(client, tenant_id, loser) == []
    assert await _supersessors(client, tenant_id, loser) == []


async def test_search_holds_a_recorded_winner_to_the_callers_scope(client, tenant_id, fleet_id) -> None:
    loser = await _memory(client, tenant_id, fleet_id)
    winner = await _memory(client, tenant_id, f"{fleet_id}-other", agent_id="m34-other-agent")
    await _status(client, tenant_id, loser, "conflicted")
    await _record(client, tenant_id, fleet_id, winner, loser)

    assert await _successors(client, tenant_id, loser, filter_agent_id="m34-tester") == []
    assert await _successors(client, tenant_id, loser, fleet_ids=[fleet_id], strict_fleet_scoping=True) == []
    assert await _successors(client, tenant_id, loser) == [(winner, loser)]
