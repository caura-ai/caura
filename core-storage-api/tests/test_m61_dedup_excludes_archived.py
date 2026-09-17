"""M-61 — the crystallizer's dedup sweep must not see the rows it archived.

Against real Postgres, because the finding is entirely about which rows two SQL
predicates reach. Stubs would assert the code calls itself.

The loop this closes: run 1 merges cluster {S1,S2,S3} into crystal F and archives
the sources. F is >=0.95 similar to them — that is what made them a cluster — and
F is written with ``last_dedup_checked_at`` NULL, so run 2 picks F as a candidate,
finds the ARCHIVED S1/S2 as neighbours, re-forms {F,S1,S2}, re-sends it to the
LLM, and then archives F, because the archive step takes every cluster member.

A pair is (candidate, neighbour), so both ends need the filter: an archived row
excluded from one end still reaches a cluster through the other. There is a test
for each end.

Both ends now live in one statement — ``memory_find_near_duplicate_pairs``,
which oss-0814-m-37 fused out of the candidate query and the per-candidate
neighbour query. These tests moved onto it rather than being retired: the
predicates are the same two, and asserting them through the query the sweep
actually runs is what the M-61 guard is for.
"""

import uuid

import pytest

from core_storage_api.services.postgres_service import PostgresService

pytestmark = pytest.mark.asyncio

# 1024 is the column's dimensionality. Two vectors this close sit well past the
# 0.95 the sweep uses, so similarity never decides these tests — status does,
# which is the only thing they are about.
_VEC_A = [0.1] * 1024
_VEC_B = [0.1] * 1023 + [0.1001]


def _candidates(rows) -> set:
    """The swept set: the distinct left side of the LEFT JOIN.

    A candidate repeats once per neighbour and appears once with a NULL
    neighbour when it has none, so membership here is exactly "was offered to
    the sweep" — independent of whether anything matched it.
    """
    return {r[0] for r in rows}


def _neighbours_of(rows, candidate_id) -> set:
    """The non-NULL right side for one candidate."""
    return {r[1] for r in rows if r[0] == candidate_id and r[1] is not None}


async def _memory(svc: PostgresService, tenant: str, *, status: str, embedding=None):
    return await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "m61-tester",
            "content": f"m61 canary {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "status": status,
            "visibility": "scope_team",
            "embedding": embedding if embedding is not None else _VEC_A,
        }
    )


async def test_an_archived_row_is_not_a_dedup_candidate():
    """The candidate side of the pair.

    Filtering only the neighbour query would leave this open: an archived row
    still enters a cluster by being the row whose neighbours are scanned.
    """
    svc = PostgresService()
    tenant = f"m61c-{uuid.uuid4().hex[:8]}"

    live = await _memory(svc, tenant, status="active")
    archived = await _memory(svc, tenant, status="archived")

    rows = await svc.memory_find_near_duplicate_pairs(tenant_id=tenant, fleet_id=None, batch_size=50)
    ids = _candidates(rows)

    assert live.id in ids, "a live unchecked row must still be swept"
    assert archived.id not in ids, "an archived row was offered as a dedup candidate"


async def test_an_archived_neighbour_is_not_returned():
    """The neighbour side, and the one that closes the reported loop.

    ``archived`` here stands for a source the previous run merged away; ``live``
    stands for the crystal it produced.
    """
    svc = PostgresService()
    tenant = f"m61n-{uuid.uuid4().hex[:8]}"

    crystal = await _memory(svc, tenant, status="confirmed")
    live_peer = await _memory(svc, tenant, status="active", embedding=_VEC_B)
    archived_source = await _memory(svc, tenant, status="archived", embedding=_VEC_B)

    rows = await svc.memory_find_near_duplicate_pairs(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=50,
        threshold=0.5,
        neighbor_limit=50,
    )
    # The crystal is itself a candidate (confirmed, unchecked, embedded), so its
    # neighbour list is the LATERAL's output for that row — the same list the
    # per-candidate query used to return for it.
    ids = _neighbours_of(rows, crystal.id)

    assert live_peer.id in ids, "a live neighbour must still be found"
    assert archived_source.id not in ids, (
        "the crystal was handed back the source it was merged from; "
        "that re-forms the cluster and re-archives the crystal"
    )


async def test_confirmed_and_pending_are_live_for_this_sweep():
    """OVER-REFUSAL GUARD, and the reason this is not ``!= 'archived'``.

    A crystal is written ``confirmed``. If the filter had been the literal
    ``active`` — the easy thing to write — the sweep would stop seeing crystals
    entirely and never dedup-check them, which is a different bug in the
    opposite direction and would not have failed either test above.
    """
    svc = PostgresService()
    tenant = f"m61l-{uuid.uuid4().hex[:8]}"

    confirmed = await _memory(svc, tenant, status="confirmed")
    pending = await _memory(svc, tenant, status="pending")

    rows = await svc.memory_find_near_duplicate_pairs(tenant_id=tenant, fleet_id=None, batch_size=50)
    ids = _candidates(rows)

    assert {confirmed.id, pending.id} <= ids, f"a live status was dropped from the sweep: {ids}"


async def test_non_live_statuses_other_than_archived_are_excluded_too():
    """``outdated`` and ``conflicted`` are no better as merge inputs.

    Naming the live set rather than excluding one status means a status added
    later is excluded by default instead of silently admitted.
    """
    svc = PostgresService()
    tenant = f"m61x-{uuid.uuid4().hex[:8]}"

    outdated = await _memory(svc, tenant, status="outdated")
    conflicted = await _memory(svc, tenant, status="conflicted")

    rows = await svc.memory_find_near_duplicate_pairs(tenant_id=tenant, fleet_id=None, batch_size=50)
    ids = _candidates(rows)

    assert outdated.id not in ids
    assert conflicted.id not in ids
