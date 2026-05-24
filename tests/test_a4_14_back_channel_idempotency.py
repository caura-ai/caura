"""A4 #14 — back-channel idempotency for contradiction detection.

Context
───────
Path A is triggered from two events: ``Topics.Memory.ENRICHED`` and
``Topics.Memory.EMBEDDED``. The intent is to handle whichever lands
first; the second event is a back-channel safety net. But the current
handlers both call ``detect_contradictions_async`` unconditionally, so
once both events fire (the common case), the detector runs **twice**.
Storage writes are idempotent (status revert is a no-op; supersedes
CAS rejects subsequent sets), but each invocation re-issues every
LLM judgement call against the candidate set — wasted API spend +
latency.

Same pattern for Path C: entity extraction can complete more than
once per memory (re-extraction after enrichment delta, partial
fan-out, retries), and each completion fires
``detect_contradictions_by_entities_async`` independently. Wet test
on 2026-05-24 observed Path C completing twice for every memory.

A4 #14 adds a per-memory SETNX-based idempotency check at the
top of each detector entry point. First caller wins and runs;
subsequent callers within the TTL window skip with a structured log
line. Falls back gracefully (allow detection) when Redis is
unavailable, preserving current behaviour rather than silently
blocking detection.

Lock keys (separate per path so Path A and Path C don't block each other):
  ``contradiction:path_a:<memory_id>``
  ``contradiction:path_c:<memory_id>``

TTL: 3600s (1h). Long enough for any detection to complete (worst
case ~10s × N candidates); short enough that a process crash holding
the lock doesn't permanently block re-detection.

What this PR does NOT change:
- Storage-side idempotency (CAS on supersedes_id, status writes).
- Detection logic itself.
- Handler-level deferral (ENRICHED handler still skips when embedding
  missing; that's complementary, not redundant, with A4 #14).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest


pytestmark = pytest.mark.unit


def _make_memory(mid: UUID, *, status: str = "active", supersedes_id=None) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "agent_id": "a1",
        "content": "memory content",
        "status": status,
        "visibility": "scope_team",
        "supersedes_id": str(supersedes_id) if supersedes_id else None,
        "deleted_at": None,
        "created_at": "2026-05-24T10:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Path A — detect_contradictions_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_a_second_call_skips_when_lock_already_held():
    """When the first Path A call has acquired the lock, the second
    call MUST skip the expensive work (no ``_detect`` invocation, no
    storage calls beyond the initial guard fetch)."""
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    new_memory = _make_memory(mid)

    # Simulate a Redis lock holder by patching SETNX to return False
    # (lock not acquired). This is what the SECOND back-channel call
    # would see.
    with (
        patch(
            "core_api.services.contradiction_detector._acquire_path_a_lock",
            new_callable=AsyncMock,
            return_value=False,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            return_value=[],
        ) as detect_mock,
    ):
        await detect_contradictions_async(
            mid, "t1", "f1", "content", [0.1] * 10, new_memory=new_memory
        )

    detect_mock.assert_not_called()


@pytest.mark.asyncio
async def test_path_a_first_call_runs_when_lock_acquired():
    """The first Path A caller must acquire the lock AND run detection."""
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    new_memory = _make_memory(mid)

    with (
        patch(
            "core_api.services.contradiction_detector._acquire_path_a_lock",
            new_callable=AsyncMock,
            return_value=True,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            return_value=[],
        ) as detect_mock,
    ):
        await detect_contradictions_async(
            mid, "t1", "f1", "content", [0.1] * 10, new_memory=new_memory
        )

    detect_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Path C — detect_contradictions_by_entities_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_c_second_call_skips_when_lock_already_held():
    """When the first Path C call has the lock, the second skips
    without fetching the new memory or running detection."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=_make_memory(mid))
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[])
    sc.update_memory_status = AsyncMock()

    with (
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=False,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
    ):
        await detect_contradictions_by_entities_async(mid, "t1", "f1")

    sc.find_entity_overlap_candidates.assert_not_called()
    sc.update_memory_status.assert_not_called()


@pytest.mark.asyncio
async def test_path_c_first_call_runs_when_lock_acquired():
    """The first Path C caller acquires the lock and runs detection."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=_make_memory(mid))
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[])
    sc.update_memory_status = AsyncMock()

    with (
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(mid, "t1", "f1")

    sc.find_entity_overlap_candidates.assert_called_once()


# ---------------------------------------------------------------------------
# Path A and Path C have INDEPENDENT locks — one shouldn't block the other.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_a_and_path_c_use_independent_locks():
    """The two paths cover different concerns (semantic vs entity
    overlap). Holding the Path A lock for memory X MUST NOT block
    Path C from running on memory X (and vice versa).

    Pinned via the lock key shape: ``contradiction:path_a:<mid>`` vs
    ``contradiction:path_c:<mid>`` — different keys, different
    SETNX calls, no shared state.
    """
    from core_api.services.contradiction_detector import (
        _acquire_path_a_lock,
        _acquire_path_c_lock,
    )

    # Both helpers exist and are callable. Pin the key separation by
    # capturing what cache_set_nx is asked for.
    mid = uuid4()
    captured: list[str] = []

    async def fake_set_nx(key, value, ttl):
        captured.append(key)
        return True

    with patch(
        "core_api.services.contradiction_detector.cache_set_nx",
        new=fake_set_nx,
        create=True,
    ):
        await _acquire_path_a_lock(mid)
        await _acquire_path_c_lock(mid)

    assert len(captured) == 2
    assert captured[0] != captured[1], (
        f"Path A and Path C must use distinct lock keys; got {captured}"
    )
    assert "path_a" in captured[0]
    assert "path_c" in captured[1]
    assert str(mid) in captured[0]
    assert str(mid) in captured[1]


# ---------------------------------------------------------------------------
# Redis-unavailable fallback: MUST allow detection (current behavior).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_a_runs_when_redis_unavailable():
    """If Redis is down, ``cache_set_nx`` returns True (fail-open)
    so the FIRST detection still runs. Subsequent duplicates also
    run — that's the current behavior. The idempotency is best-effort,
    not load-bearing for correctness (storage CAS still guards)."""
    from core_api.services.contradiction_detector import _acquire_path_a_lock

    async def redis_down(key, value, ttl):
        return True  # fail-open

    with patch(
        "core_api.services.contradiction_detector.cache_set_nx",
        new=redis_down,
        create=True,
    ):
        assert await _acquire_path_a_lock(uuid4()) is True


# ---------------------------------------------------------------------------
# cache_set_nx helper — return True only when the key was newly set,
# False when it already existed, and True when Redis is unavailable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_set_nx_returns_true_on_first_set():
    """The first SETNX for a key returns True."""
    from core_api.cache import cache_set_nx

    redis_mock = AsyncMock()
    redis_mock.set = AsyncMock(return_value=True)

    with patch("core_api.cache._get_redis", new=AsyncMock(return_value=redis_mock)):
        assert await cache_set_nx("test_key", "v", 60) is True

    redis_mock.set.assert_called_once_with("test_key", "v", ex=60, nx=True)


@pytest.mark.asyncio
async def test_cache_set_nx_returns_false_when_key_already_exists():
    """SETNX on an existing key returns False (Redis returns None)."""
    from core_api.cache import cache_set_nx

    redis_mock = AsyncMock()
    redis_mock.set = AsyncMock(return_value=None)

    with patch("core_api.cache._get_redis", new=AsyncMock(return_value=redis_mock)):
        assert await cache_set_nx("test_key", "v", 60) is False


@pytest.mark.asyncio
async def test_cache_set_nx_fails_open_when_redis_unavailable():
    """When Redis returns None from ``_get_redis`` (unavailable),
    ``cache_set_nx`` returns True so detection still runs. We do NOT
    want a Redis outage to silently kill contradiction detection."""
    from core_api.cache import cache_set_nx

    with patch("core_api.cache._get_redis", new=AsyncMock(return_value=None)):
        assert await cache_set_nx("test_key", "v", 60) is True


@pytest.mark.asyncio
async def test_cache_set_nx_fails_open_on_exception():
    """If the Redis SET call itself raises (transient network blip),
    ``cache_set_nx`` swallows and returns True. Same rationale as
    Redis-unavailable case."""
    from core_api.cache import cache_set_nx

    redis_mock = AsyncMock()
    redis_mock.set = AsyncMock(side_effect=Exception("connection reset"))

    with patch("core_api.cache._get_redis", new=AsyncMock(return_value=redis_mock)):
        assert await cache_set_nx("test_key", "v", 60) is True
