"""oss-0814-m-37 — the crystallizer near-duplicate scan, as a transfer shape.

Two claims, pinned separately because they were two separate defects that
happened to live in one loop:

1. SERIAL N+1. ``_check_near_duplicates`` fetched a page of candidates and then
   issued one neighbour POST per candidate, awaited in-loop — 1 + 500 round
   trips per batch at the shipped ``CRYSTALLIZER_DEDUP_BATCH_SIZE``.
2. FULL EMBEDDINGS BOTH DIRECTIONS. Each candidate's 1024-dim vector came down
   in the candidate page and went straight back up in that candidate's
   neighbour request. core-api never read it — pgvector computes the
   similarity — so ~22 KB x 2 per row was pure relay.

These are stubbed, not DB-backed, on purpose. The finding is about how many
times core-api talks to storage and what it puts in the bodies; a real Postgres
would exercise the SQL (``core-storage-api/tests/test_m61_dedup_excludes_archived.py``
and the integration half of ``tests/test_p5_crystallizer.py`` already do) but
would hide the round trips behind an in-process ASGI bridge, which is exactly
the thing under test.

The fake storage below deliberately answers BOTH the old and the new protocol.
That is what makes these tests a real gate: reverted to the serial version, the
scan still runs to completion against this fake and the assertions fail on the
round-trip count and on the vectors in the request bodies — not on a KeyError
from a response shape the old code never saw.

Behaviour identity is asserted against ``_reference_serial_scan``, a transcript
of the pre-fix loop. The perf half of the finding ships ahead of the parked
crystallizer retune (reg-a72), so "same pairs, same similarities, same order,
same cap" is the contract, not an aspiration.
"""

import json

import pytest

from core_api.constants import (
    CRYSTALLIZER_DEDUP_NEIGHBORS,
    CRYSTALLIZER_DEDUP_THRESHOLD,
    VECTOR_DIM,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# A corpus + similarity oracle, standing in for pgvector
# ---------------------------------------------------------------------------


def _vec(seed: int, dim: int = VECTOR_DIM) -> list[float]:
    """A unit-ish vector that is deterministic in ``seed``.

    Full ``VECTOR_DIM`` rather than a toy width: half of what is being measured
    is how many bytes a vector costs on the wire, and a 4-float stand-in would
    make the payload assertions vacuous.
    """
    base = [0.05] * dim
    base[seed % dim] += 1.0
    return base


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb)


class FakeStorage:
    """Answers the dedup sweep both ways, and records every call.

    ``corpus`` is ordered the way ``created_at DESC`` orders candidates, so a
    slice of it is a page.
    """

    def __init__(self, corpus: list[tuple[str, list[float]]]):
        self.corpus = corpus
        self.calls: list[tuple[str, dict]] = []
        self.marked: list[str] = []

    # -- the oracle: what the LATERAL / the per-row query would return --------
    def _neighbours(
        self, mem_id: str, emb: list[float], threshold: float, limit: int
    ) -> list[dict]:
        scored = [
            (other_id, _cos(emb, other_emb))
            for other_id, other_emb in self.corpus
            if other_id != mem_id and _cos(emb, other_emb) >= threshold
        ]
        # Nearest first, id as the tiebreaker — the order the SQL emits.
        scored.sort(key=lambda t: (-t[1], t[0]))
        return [{"id": i, "similarity": s} for i, s in scored[:limit]]

    # -- new protocol: one call resolves the whole batch ----------------------
    async def check_near_duplicates(self, data: dict) -> dict:
        self.calls.append(("check_near_duplicates", data))
        offset = data.get("offset", 0)
        size = data.get("batch_size", 100)
        threshold = data.get("threshold", 0.95)
        limit = data.get("neighbor_limit", 5)
        page = self.corpus[offset : offset + size]

        pairs: list[dict] = []
        for mem_id, emb in page:
            for nb in self._neighbours(mem_id, emb, threshold, limit):
                pairs.append(
                    {
                        "id": mem_id,
                        "neighbor_id": nb["id"],
                        "similarity": nb["similarity"],
                    }
                )

        return {
            "candidate_ids": [mem_id for mem_id, _ in page],
            "pairs": pairs,
            # The retired shape, still served so the pre-fix loop runs to
            # completion here and fails on the assertions rather than on a
            # missing key.
            "candidates": [{"id": mem_id, "embedding": emb} for mem_id, emb in page],
        }

    # -- retired protocol: one call per candidate ----------------------------
    async def find_neighbors_by_embedding(self, data: dict) -> list[dict]:
        self.calls.append(("find_neighbors_by_embedding", data))
        return self._neighbours(
            data["exclude_id"],
            data["query_embedding"],
            data.get("threshold", 0.95),
            data.get("limit", 5),
        )

    async def mark_dedup_checked(self, memory_ids: list[str], tenant_id: str) -> dict:
        self.calls.append(
            ("mark_dedup_checked", {"n": len(memory_ids), "tenant_id": tenant_id})
        )
        self.marked.extend(memory_ids)
        return {"ok": True}

    # -- helpers -------------------------------------------------------------
    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def bodies(self, name: str) -> list[dict]:
        return [body for n, body in self.calls if n == name]


@pytest.fixture
def scan(monkeypatch):
    """``_check_near_duplicates`` bound to a fake storage of a given corpus."""
    from core_api.services import crystallizer_service as cs

    def _make(corpus, **const_overrides):
        fake = FakeStorage(corpus)
        monkeypatch.setattr(cs, "get_storage_client", lambda: fake)
        for name, value in const_overrides.items():
            monkeypatch.setattr(cs, name, value)
        return fake

    return _make


def _corpus(n: int, *, twins: int = 0) -> list[tuple[str, list[float]]]:
    """``n`` mutually-distant rows, the first ``twins`` of them paired up.

    A twin is the same vector as its partner, so the pair clears any threshold;
    everything else is a distinct basis-ish direction and clears none.
    """
    rows: list[tuple[str, list[float]]] = []
    for i in range(n):
        seed = i - 1 if (i < twins * 2 and i % 2 == 1) else i
        rows.append((f"{i:08d}-0000-0000-0000-000000000000", _vec(seed)))
    return rows


# ---------------------------------------------------------------------------
# Claim 1 — the serial N+1
# ---------------------------------------------------------------------------


async def test_a_batch_costs_one_round_trip_not_one_per_row(scan):
    """The whole point: the scan no longer talks to storage per candidate."""
    fake = scan(_corpus(40, twins=5))

    await _run()

    assert fake.count("find_neighbors_by_embedding") == 0, (
        "the per-candidate neighbour call is the N+1; it must not survive"
    )
    # Two batch calls: the page, then the empty page that ends the loop.
    assert fake.count("check_near_duplicates") == 2
    assert fake.count("mark_dedup_checked") == 1


async def test_round_trips_do_not_grow_with_the_corpus(scan):
    """N+1 restated as the property that actually matters.

    A 20x bigger corpus that still fits one page must cost the same number of
    round trips. Pre-fix this was 21 vs 401.
    """
    small = scan(_corpus(20, twins=4))
    await _run()
    small_calls = len(small.calls)

    big = scan(_corpus(400, twins=80))
    await _run()

    assert len(big.calls) == small_calls, (
        f"round trips scale with rows: {small_calls} for 20 rows, {len(big.calls)} for 400"
    )


# ---------------------------------------------------------------------------
# Claim 2 — full embeddings, both directions
# ---------------------------------------------------------------------------


def _looks_like_a_vector(value) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != VECTOR_DIM:
        return False
    return all(isinstance(v, (int, float)) for v in value)


async def test_no_embedding_goes_up_to_storage(scan):
    """The 'up' direction: every request body the scan sends, inspected."""
    fake = scan(_corpus(30, twins=6))

    await _run()

    for name, body in fake.calls:
        for key, value in body.items():
            assert not _looks_like_a_vector(value), (
                f"{name} still ships a {VECTOR_DIM}-dim vector in {key!r}"
            )
        assert "query_embedding" not in body, f"{name} still carries query_embedding"
        assert "embedding" not in body, f"{name} still carries embedding"


async def test_the_scan_never_reads_the_embedding_it_is_offered(scan):
    """The 'down' direction, from core-api's side.

    The fake still serves the old ``candidates`` list with vectors in it. If the
    scan consumed them, they would reappear in an outbound body — which is what
    the pre-fix loop did and what the assertion above catches. This test pins
    the complement: the request the scan sends does not ask for them either, so
    storage has no reason to select the column.
    """
    fake = scan(_corpus(12, twins=3))

    await _run()

    (first, *_rest) = fake.bodies("check_near_duplicates")
    assert set(first) == {
        "tenant_id",
        "fleet_id",
        "batch_size",
        "offset",
        "threshold",
        "neighbor_limit",
    }, f"unexpected request shape: {sorted(first)}"


async def test_the_storage_response_carries_no_vector(monkeypatch):
    """The 'down' direction at its source: the route's own response body.

    Calls the real handler with a stubbed service, so this pins the wire
    contract rather than core-api's tolerance of it.
    """
    from core_storage_api.routers import memories as route_mod

    rows = [
        (
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            0.97,
        ),
        (
            "11111111-1111-1111-1111-111111111111",
            None,
            None,
        ),  # a no-neighbour candidate
        ("33333333-3333-3333-3333-333333333333", None, None),
    ]

    async def _fake_pairs(**kwargs):
        return rows

    monkeypatch.setattr(route_mod._svc, "memory_find_near_duplicate_pairs", _fake_pairs)

    class _Req:
        async def json(self):
            return {"tenant_id": "t1", "batch_size": 500}

    body = await route_mod.check_near_duplicates(_Req())

    assert set(body) == {"candidate_ids", "pairs"}
    blob = json.dumps(body)
    assert "embedding" not in blob
    assert "search_vector" not in blob
    # Swept set is the DISTINCT left side, and includes the candidate that
    # matched nothing — that is the set the caller stamps.
    assert body["candidate_ids"] == [
        "11111111-1111-1111-1111-111111111111",
        "33333333-3333-3333-3333-333333333333",
    ]
    assert body["pairs"] == [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "neighbor_id": "22222222-2222-2222-2222-222222222222",
            "similarity": 0.97,
        }
    ]


# ---------------------------------------------------------------------------
# Behaviour identity — reg-a72's territory must be untouched
# ---------------------------------------------------------------------------


def _reference_serial_scan(
    fake: FakeStorage, *, batch_size: int, max_pairs: int
) -> dict:
    """A transcript of the pre-fix loop, run over the same oracle.

    Kept as the oracle of record for "nothing about which pairs are found
    changed", since the fix rewrote both the SQL and the client loop.
    """
    pairs: dict[tuple[str, str], float] = {}
    checked: list[str] = []
    offset = 0

    while len(pairs) < max_pairs:
        page = fake.corpus[offset : offset + batch_size]
        if not page:
            break
        for mem_id, emb in page:
            checked.append(mem_id)
            for nb in fake._neighbours(
                mem_id, emb, CRYSTALLIZER_DEDUP_THRESHOLD, CRYSTALLIZER_DEDUP_NEIGHBORS
            ):
                id1, id2 = sorted([mem_id, nb["id"]])
                if (id1, id2) not in pairs and len(pairs) < max_pairs:
                    pairs[(id1, id2)] = nb["similarity"]
        offset += batch_size

    return {
        "count": len(pairs),
        "pairs": [{"id1": a, "id2": b, "similarity": s} for (a, b), s in pairs.items()],
        "checked": checked,
    }


async def test_same_pairs_same_similarities_same_order(scan):
    corpus = _corpus(60, twins=15)
    fake = scan(corpus)

    result = await _run()
    reference = _reference_serial_scan(fake, batch_size=500, max_pairs=1000)

    assert result["pairs"] == reference["pairs"], (
        "the fused scan found different pairs, or found them in a different order"
    )
    assert result["count"] == reference["count"]
    assert fake.marked == reference["checked"]


async def test_the_pair_cap_bites_on_the_same_pairs(scan):
    """Cap + paging together, at constants small enough to reach cheaply.

    The cap is positional — it keeps the first N pairs discovered — so it is the
    assertion most sensitive to a change in discovery order, which is the one
    thing a set-based rewrite could plausibly have broken.
    """
    corpus = _corpus(60, twins=30)
    fake = scan(
        corpus, CRYSTALLIZER_DEDUP_BATCH_SIZE=7, CRYSTALLIZER_MAX_DEDUP_PAIRS=11
    )

    result = await _run()
    reference = _reference_serial_scan(fake, batch_size=7, max_pairs=11)

    assert result["count"] == 11, "the safety valve stopped capping"
    assert result["pairs"] == reference["pairs"]
    assert fake.marked == reference["checked"]


async def test_every_swept_row_is_stamped_not_only_the_matched_ones(scan):
    """The LEFT JOIN's reason for existing.

    An inner join would have stamped only rows that turned out to have a
    duplicate, so every row without one would be re-scanned on every future
    sweep — the N+1 traded for an unbounded re-scan.
    """
    corpus = _corpus(10, twins=2)  # 4 rows in pairs, 6 with no neighbour at all
    fake = scan(corpus)

    result = await _run()

    assert result["count"] == 2
    assert fake.marked == [mem_id for mem_id, _ in corpus], (
        "rows with no near-duplicate were left unstamped"
    )


# ---------------------------------------------------------------------------


async def _run() -> dict:
    """Run the scan against whatever ``scan`` bound as the storage client."""
    from core_api.services.crystallizer_service import _check_near_duplicates

    return await _check_near_duplicates("tenant-m37", None)
