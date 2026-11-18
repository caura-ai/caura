"""One pending ``memory_conflicts`` record per pair, whichever detection records it.

Each detection run de-duplicates its own pairs, but nothing de-duplicated across
runs. On 17 Nov a production tenant with ``conflict_records_enabled`` got two
pending records for one pair from one write: the semantic pass recorded it, and
the entity-overlap pass recorded it again 0.4 s later. Overnight, detection ran
for both memories of another pair and recorded it once in each direction, 24 ms
apart. Each record of a pair counts as applied, so review refuses to resolve
either ("Another verdict still holds this pair"), and dismissing one undoes the
verdict the other still stands for.

So ``POST /memories/conflicts`` returns the pending record that already holds
the pair, in either direction, instead of adding a second.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from common.models import MemoryConflict
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _memory(client: AsyncClient, tenant_id: str, fleet_id: str) -> str:
    resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _payload(tenant_id: str, new: str, old: str, *, applied: bool = True) -> dict:
    return {
        "tenant_id": tenant_id,
        "new_memory_id": new,
        "old_memory_id": old,
        "relationship": "exact_value",
        "diagnosis": "temporal_change",
        "evidence_strength": "explicit",
        "action": "supersede" if applied else "mark_disputed",
        "created_by": "contradiction-engine",
        "metadata": {"effect_applied": applied},
    }


async def _record(client: AsyncClient, payload: dict) -> str:
    resp = await client.post(f"{PREFIX}/memories/conflicts", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _pending(client: AsyncClient, tenant_id: str, memory_id: str) -> set[str]:
    resp = await client.get(
        f"{PREFIX}/memories/memory-conflicts",
        params={"tenant_id": tenant_id, "memory_id": memory_id, "review_status": "pending"},
    )
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


async def test_a_pair_recorded_again_is_the_record_it_already_has(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    """The semantic pass and then the entity-overlap pass, for one write."""
    old, new = await _memory(client, tenant_id, fleet_id), await _memory(client, tenant_id, fleet_id)

    first = await _record(client, _payload(tenant_id, new, old))
    again = await _record(client, _payload(tenant_id, new, old))

    assert again == first
    assert await _pending(client, tenant_id, new) == {first}


async def test_the_pair_recorded_the_other_way_round_is_the_same_record(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    """Detection run for each memory of the pair, each finding the other."""
    a, b = await _memory(client, tenant_id, fleet_id), await _memory(client, tenant_id, fleet_id)

    first = await _record(client, _payload(tenant_id, b, a))
    reversed_ = await _record(client, _payload(tenant_id, a, b))

    assert reversed_ == first
    assert await _pending(client, tenant_id, a) == {first}


async def test_two_detections_recording_at_once_leave_one_record(
    client: AsyncClient, tenant_id: str, fleet_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overnight pair: both directions recorded 24 ms apart. A read-then-insert
    with nothing held between lets both read "no record" and both insert.

    Left alone, the first request commits before the second reads. So each insert
    waits at its flush, up to half a second, for the other to get there too: with
    nothing held, both do and both insert; with the pair held, the second is still
    waiting to read when the first gives up waiting and commits."""
    a, b = await _memory(client, tenant_id, fleet_id), await _memory(client, tenant_id, fleet_id)
    flush = AsyncSession.flush
    arrived: list[object] = []
    both = asyncio.Event()

    async def flush_once_both_have_read(self: AsyncSession, objects=None) -> None:
        if any(isinstance(obj, MemoryConflict) for obj in self.sync_session.new):
            arrived.append(self)
            if len(arrived) == 2:
                both.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both.wait(), timeout=0.5)
        await flush(self, objects)

    monkeypatch.setattr(AsyncSession, "flush", flush_once_both_have_read)

    ids = await asyncio.gather(
        _record(client, _payload(tenant_id, b, a)),
        _record(client, _payload(tenant_id, a, b)),
    )

    assert ids[0] == ids[1]
    assert await _pending(client, tenant_id, a) == {ids[0]}


async def test_a_dispute_does_not_absorb_a_verdict_that_was_applied(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    """A dispute records a pair detection changed nothing on; an applied verdict
    records one it did change. One record can't say both, and review undoes a
    dismissed verdict only when its record says it was applied."""
    old, new = await _memory(client, tenant_id, fleet_id), await _memory(client, tenant_id, fleet_id)

    dispute = await _record(client, _payload(tenant_id, new, old, applied=False))
    applied = await _record(client, _payload(tenant_id, new, old))

    assert applied != dispute
    assert await _pending(client, tenant_id, new) == {dispute, applied}


async def test_a_dismissed_record_does_not_absorb_the_pair_detected_again(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    """A dismissal is done with: if detection applies the verdict again, that is
    a new decision for a person to make."""
    old, new = await _memory(client, tenant_id, fleet_id), await _memory(client, tenant_id, fleet_id)
    dismissed = await _record(client, _payload(tenant_id, new, old))
    resp = await client.patch(
        f"{PREFIX}/memories/memory-conflicts/{dismissed}/resolve",
        json={"tenant_id": tenant_id, "review_status": "dismissed", "resolved_by": "reviewer"},
    )
    assert resp.status_code == 200, resp.text

    again = await _record(client, _payload(tenant_id, new, old))

    assert again != dismissed
    assert await _pending(client, tenant_id, new) == {again}
