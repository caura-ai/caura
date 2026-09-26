"""Org scoping for lifecycle-audit status updates."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete

from common.models import LifecycleAudit
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage/lifecycle-audit"


def _new_org_id() -> str:
    """Return a unique org id that matches the end-of-run sweep prefix."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def lifecycle_marker() -> str:
    marker = f"tenant-scope:{uuid.uuid4().hex}"
    yield marker
    async with get_session() as session:
        await session.execute(delete(LifecycleAudit).where(LifecycleAudit.triggered_by == marker))


async def _audit(client: AsyncClient, org_id: str, marker: str) -> int:
    response = await client.post(
        PREFIX,
        json={
            "org_id": org_id,
            "action": "archive-expired",
            "triggered_by": marker,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["audit_id"]


async def _row(client: AsyncClient, audit_id: int, org_id: str) -> dict:
    response = await client.get(f"{PREFIX}/{audit_id}", params={"org_id": org_id})
    assert response.status_code == 200, response.text
    return response.json()


class TestLifecycleAuditFinalizeTenantScope:
    async def test_route_does_not_finalize_another_orgs_audit(
        self,
        client: AsyncClient,
        lifecycle_marker: str,
    ) -> None:
        victim = _new_org_id()
        attacker = _new_org_id()
        audit_id = await _audit(client, victim, lifecycle_marker)

        response = await client.patch(
            f"{PREFIX}/{audit_id}",
            json={"org_id": attacker, "status": "success", "stats": {"archived": 9}},
        )

        assert response.status_code == 404, response.text
        row = await _row(client, audit_id, victim)
        assert row["status"] == "pending"
        assert row["stats"] is None

    async def test_route_requires_org_id(self, client: AsyncClient) -> None:
        response = await client.patch(f"{PREFIX}/1", json={"status": "success"})

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "'org_id' must be a non-empty string"

    async def test_route_passes_org_to_finalize_and_preserves_sticky_success(
        self,
        client: AsyncClient,
        lifecycle_marker: str,
    ) -> None:
        org_id = _new_org_id()
        audit_id = await _audit(client, org_id, lifecycle_marker)

        response = await client.patch(
            f"{PREFIX}/{audit_id}",
            json={"org_id": org_id, "status": "success", "stats": {"archived": 3}},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "noop": False,
            "claim_conflict": False,
            "claim_lost": False,
        }

        redelivery = await client.patch(
            f"{PREFIX}/{audit_id}",
            json={"org_id": org_id, "status": "failure", "error_message": "late"},
        )
        assert redelivery.status_code == 200, redelivery.text
        assert redelivery.json() == {
            "ok": True,
            "noop": True,
            "claim_conflict": False,
            "claim_lost": False,
        }

        row = await _row(client, audit_id, org_id)
        assert row["status"] == "success"
        assert row["stats"] == {"archived": 3}
        assert row["error_message"] is None


async def test_claim_is_single_winner_but_idempotent_for_the_same_claimant(
    client: AsyncClient, lifecycle_marker: str
) -> None:
    """The claim must block a competitor without blocking its own retry.

    Both halves matter and they pull in opposite directions, so they are
    asserted together: a guard that ignored ``claim_token`` would pass the
    competitor half and fail the retry half, and a guard that accepted every
    ``in_progress`` write would pass the retry half and fail the competitor
    half. Only an implementation that keys on the claimant passes both.

    The retry half is not hypothetical. ``CoreStorageClient._patch`` retries
    on ``ReadTimeout`` and 5xx, which was harmless while this write was
    idempotent; against a compare-and-swap, a claim that succeeded
    server-side but lost its response is re-sent verbatim, and reading that
    as a competitor would make the consumer nack a delivery it had won.
    """
    org_id = _new_org_id()
    audit_id = await _audit(client, org_id, lifecycle_marker)
    mine = uuid.uuid4().hex
    theirs = uuid.uuid4().hex

    first = await client.patch(
        f"{PREFIX}/{audit_id}",
        json={"org_id": org_id, "status": "in_progress", "claim_token": mine},
    )
    assert first.status_code == 200, first.text
    assert first.json()["claim_conflict"] is False

    # Same claimant, same token — this is the client's own HTTP retry.
    retry = await client.patch(
        f"{PREFIX}/{audit_id}",
        json={"org_id": org_id, "status": "in_progress", "claim_token": mine},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["claim_conflict"] is False, (
        "a claimant re-presenting its own token was treated as a competitor"
    )

    # A genuinely different delivery — must lose.
    other = await client.patch(
        f"{PREFIX}/{audit_id}",
        json={"org_id": org_id, "status": "in_progress", "claim_token": theirs},
    )
    assert other.status_code == 200, other.text
    assert other.json()["claim_conflict"] is True, "a second delivery took a live claim"

    # The row still belongs to the original claimant.
    row = await _row(client, audit_id, org_id)
    assert row["status"] == "in_progress"


async def test_only_the_claim_holder_may_finalize(client: AsyncClient, lifecycle_marker: str) -> None:
    """A terminal write from a consumer that lost its claim is rejected.

    Winning the claim is not the same as still holding it. The staleness arm
    exists so an abandoned claim can be taken over, and it cannot tell an
    abandoned consumer from a slow one, so a primitive that outruns the lease
    is preempted while still running. Both consumers then finalize. Without
    this guard the loser's write lands and the duplicate run leaves no trace.

    The guard is asserted directly on token mismatch rather than by waiting
    out a real lease: the contract is "a terminal write whose token is not the
    row's is refused", and lease expiry is only how that state is reached.

    The third case is the one a naive guard breaks. A terminal write from a
    caller that never presented a token at all must still be admitted -- the
    embed-backfill consumer claims without one -- or those rows become
    unfinalizable, trading a silent duplicate for a stuck row.
    """
    org_id = _new_org_id()
    audit_id = await _audit(client, org_id, lifecycle_marker)
    holder = uuid.uuid4().hex
    loser = uuid.uuid4().hex

    claim = await client.patch(
        f"{PREFIX}/{audit_id}",
        json={"org_id": org_id, "status": "in_progress", "claim_token": holder},
    )
    assert claim.status_code == 200, claim.text

    # The preempted consumer finishes and tries to record its own result.
    stolen = await client.patch(
        f"{PREFIX}/{audit_id}",
        json={
            "org_id": org_id,
            "status": "success",
            "stats": {"archived": 99},
            "claim_token": loser,
        },
    )
    assert stolen.status_code == 200, stolen.text
    assert stolen.json()["claim_lost"] is True, (
        "a terminal write from a consumer that no longer holds the claim was "
        "accepted; the duplicate run would be invisible"
    )
    row = await _row(client, audit_id, org_id)
    assert row["status"] == "in_progress", "the loser's write must not land"
    assert row["stats"] != {"archived": 99}

    # The actual holder finalizes normally.
    mine = await client.patch(
        f"{PREFIX}/{audit_id}",
        json={
            "org_id": org_id,
            "status": "success",
            "stats": {"archived": 3},
            "claim_token": holder,
        },
    )
    assert mine.status_code == 200, mine.text
    assert mine.json()["claim_lost"] is False
    row = await _row(client, audit_id, org_id)
    assert row["status"] == "success"
    assert row["stats"] == {"archived": 3}

    # The ordering case. The winner has now SUCCEEDED, which is what a
    # preempted consumer almost always finds, so the sticky-success no-op and
    # the lost-claim check both match. If the status is tested first this
    # reports an ordinary redelivery and the duplicate run is never seen --
    # which would leave the guard catching only the rare case where the winner
    # failed.
    late = await client.patch(
        f"{PREFIX}/{audit_id}",
        json={
            "org_id": org_id,
            "status": "success",
            "stats": {"archived": 99},
            "claim_token": loser,
        },
    )
    assert late.status_code == 200, late.text
    assert late.json()["claim_lost"] is True, (
        "a lost claim was reported as an ordinary no-op because the winner "
        "had succeeded; this is the common case, not an edge case"
    )
    assert late.json()["noop"] is False
    assert (await _row(client, audit_id, org_id))["stats"] == {"archived": 3}

    # The holder's own retry of its successful write is still a plain no-op.
    retry = await client.patch(
        f"{PREFIX}/{audit_id}",
        json={
            "org_id": org_id,
            "status": "success",
            "stats": {"archived": 3},
            "claim_token": holder,
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["claim_lost"] is False, "the claim holder's own retry was reported as a lost claim"
    assert retry.json()["noop"] is True

    # A row claimed without a token stays finalizable by a tokenless caller.
    other_id = await _audit(client, org_id, lifecycle_marker)
    await client.patch(f"{PREFIX}/{other_id}", json={"org_id": org_id, "status": "in_progress"})
    untokened = await client.patch(
        f"{PREFIX}/{other_id}",
        json={"org_id": org_id, "status": "success", "stats": {"archived": 1}},
    )
    assert untokened.status_code == 200, untokened.text
    assert untokened.json()["claim_lost"] is False
    assert (await _row(client, other_id, org_id))["status"] == "success"
