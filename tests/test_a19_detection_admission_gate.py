"""A19 — contradiction detection is admission-gated, process-wide.

Every trigger site schedules detection with ``track_task`` (a bare
``asyncio.create_task``), so the per-tenant bulkheads bound how fast writes are
ADMITTED while nothing bounded how many detections then RAN at once: the tasks
outlive the requests that spawned them, and a stack of bulk writes leaves
hundreds of detection coroutines racing the moment they commit — all sharing
the foreground storage pool (200 conns, 5s pool budget) and the LLM provider.

The fix is one shared admission gate (``_acquire_detection_slot``) across BOTH
detector entry points, capped by ``settings.contradiction_detection_concurrency``.
Excess passes queue FIFO — never shed — because detection is post-commit
background work and a shed pass is a contradiction nobody ever looks for.

The concurrency-tracking shape mirrors ``test_contradiction_fanout_bounded``;
the lock/storage mocking mirrors ``test_h06_contradiction_lock``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.services import contradiction_detector as detector

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeLocks:
    """In-memory SETNX/compare-and-delete — every id below is unique, so the
    idempotency lock never dedupes; the gate alone shapes concurrency."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set_nx(self, key: str, value: str, ttl: int) -> bool:
        if key in self.keys:
            return False
        self.keys[key] = value
        return True

    async def delete_if(self, key: str, expected: str) -> bool:
        if self.keys.get(key) == expected:
            del self.keys[key]
            return True
        return False


def _memory(mid) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "agent_id": "a1",
        "content": f"fact {mid}",
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
    }


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch):
    """Each test gets its own gate instance at its own cap.

    The gate is cached per running loop; tests run one loop each, but resetting
    the cache makes the cap monkeypatch take effect regardless of what an
    earlier test on a reused loop did.
    """
    monkeypatch.setattr(detector, "_DETECTION_GATE", None)
    yield


def _locks_patches(fake: _FakeLocks):
    return (
        patch("core_api.services.contradiction_detector.cache_set_nx", new=fake.set_nx),
        patch(
            "core_api.services.contradiction_detector.cache_delete_if",
            new=fake.delete_if,
        ),
    )


async def _resolve_config(tenant_id):
    return None


async def test_path_a_burst_never_exceeds_the_cap(monkeypatch):
    """The A19 collapse mechanism, pinned: 40 simultaneously-scheduled Path A
    passes must run at most ``contradiction_detection_concurrency`` at a time.

    Pre-fix every pass ran immediately (peak == 40)."""
    monkeypatch.setattr(detector.settings, "contradiction_detection_concurrency", 4)

    active = 0
    peak = 0

    async def fake_detect(new_memory, embedding, tenant_config=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            return []
        finally:
            active -= 1

    fake = _FakeLocks()
    a, d = _locks_patches(fake)
    with (
        a,
        d,
        patch("core_api.services.contradiction_detector._detect", new=fake_detect),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=_resolve_config,
        ),
    ):
        mids = [uuid4() for _ in range(40)]
        await asyncio.gather(
            *(
                detector.detect_contradictions_async(
                    mid, "t1", "f1", f"fact {mid}", [0.1] * 8, new_memory=_memory(mid)
                )
                for mid in mids
            )
        )

    assert peak <= 4, f"admission gate exceeded: peak={peak}"
    assert peak > 1, "sanity: passes did run concurrently up to the cap"


async def test_path_a_and_path_c_share_one_gate(monkeypatch):
    """Path C must draw from the SAME gate as Path A — two independent
    Semaphore(N)s would allow 2N whenever the write burst's Path A passes
    overlap the entity-extraction burst's Path C passes, which is exactly the
    stampede the cap exists to bound (Path C is the heavier occupant: its
    context fetch holds up to 8 storage connections)."""
    monkeypatch.setattr(detector.settings, "contradiction_detection_concurrency", 3)

    active = 0
    peak = 0

    async def _occupy():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
        finally:
            active -= 1

    async def fake_detect(new_memory, embedding, tenant_config=None):
        await _occupy()
        return []

    async def fake_overlap_candidates(payload):
        # Runs while a Path C pass holds its slot; empty -> concluded early.
        await _occupy()
        return []

    sc = AsyncMock()
    sc.find_entity_overlap_candidates = fake_overlap_candidates
    sc.get_memory = AsyncMock(side_effect=lambda mid, tid: _memory(mid))

    fake = _FakeLocks()
    a, d = _locks_patches(fake)
    with (
        a,
        d,
        patch("core_api.services.contradiction_detector._detect", new=fake_detect),
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=_resolve_config,
        ),
    ):
        runs = []
        for _ in range(10):
            mid_a, mid_c = uuid4(), uuid4()
            runs.append(
                detector.detect_contradictions_async(
                    mid_a,
                    "t1",
                    "f1",
                    f"fact {mid_a}",
                    [0.1] * 8,
                    new_memory=_memory(mid_a),
                )
            )
            runs.append(
                detector.detect_contradictions_by_entities_async(mid_c, "t1", "f1")
            )
        await asyncio.gather(*runs)

    assert peak <= 3, f"the two paths must share one gate: peak={peak}"
    assert peak > 1, "sanity: passes did run concurrently up to the cap"


async def test_a_failing_pass_releases_its_slot(monkeypatch):
    """A pass that throws must free its slot — a leaked slot is a silent,
    permanent shrink of detection throughput (and at cap failures, a
    deadlock). The entry's own except swallows the error; the gate release
    lives in the finally, so the failure path is the one worth pinning."""
    monkeypatch.setattr(detector.settings, "contradiction_detection_concurrency", 2)

    async def exploding_detect(new_memory, embedding, tenant_config=None):
        raise RuntimeError("boom")

    ran_after_failures = False

    async def fine_detect(new_memory, embedding, tenant_config=None):
        nonlocal ran_after_failures
        ran_after_failures = True
        return []

    fake = _FakeLocks()
    a, d = _locks_patches(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=_resolve_config,
        ),
    ):
        with patch(
            "core_api.services.contradiction_detector._detect", new=exploding_detect
        ):
            mids = [uuid4() for _ in range(6)]
            # Would hang here (not raise) if failing passes leaked slots.
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        detector.detect_contradictions_async(
                            mid,
                            "t1",
                            "f1",
                            f"fact {mid}",
                            [0.1] * 8,
                            new_memory=_memory(mid),
                        )
                        for mid in mids
                    )
                ),
                timeout=5.0,
            )
        with patch("core_api.services.contradiction_detector._detect", new=fine_detect):
            mid = uuid4()
            await asyncio.wait_for(
                detector.detect_contradictions_async(
                    mid, "t1", "f1", f"fact {mid}", [0.1] * 8, new_memory=_memory(mid)
                ),
                timeout=5.0,
            )

    assert ran_after_failures, "slots leaked by failing passes starved later detection"
