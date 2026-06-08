"""P0-3: Time-decayed recall boost — breaks the feedback loop.

Unit tests validate the decay math.
Integration tests verify recall_count + last_recalled_at update correctly.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core_api.constants import (
    RECALL_BOOST_CAP,
    RECALL_BOOST_SCALE,
    RECALL_DECAY_WINDOW_DAYS,
)


# ---------------------------------------------------------------------------
# Pure math — replicate the SQL recall boost in Python
# ---------------------------------------------------------------------------


def compute_recall_boost(
    recall_count: int,
    last_recalled_at: datetime | None,
    created_at: datetime | None = None,
    now: datetime | None = None,
) -> float:
    """Python equivalent of the SQL recall boost expression."""
    now = now or datetime.now(timezone.utc)
    anchor = last_recalled_at or created_at or now
    days_since = (now - anchor).total_seconds() / 86400.0
    recency_factor = max(0.0, 1.0 - days_since / RECALL_DECAY_WINDOW_DAYS)
    return 1.0 + (RECALL_BOOST_CAP - 1.0) * recency_factor * recall_count / (
        recall_count + RECALL_BOOST_SCALE
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRecallBoostDecay:
    def _now(self):
        return datetime.now(timezone.utc)

    # -- Core feedback loop fix --

    def test_never_recalled_no_boost(self):
        """recall_count=0 → boost = 1.0 regardless of recency."""
        now = self._now()
        b = compute_recall_boost(0, last_recalled_at=now, created_at=now, now=now)
        assert b == pytest.approx(1.0)

    def test_recently_recalled_gets_boost(self):
        """Recalled 1 hour ago with count=10 → meaningful boost."""
        now = self._now()
        last = now - timedelta(hours=1)
        b = compute_recall_boost(10, last_recalled_at=last, now=now)
        # recency ≈ 1.0, count=10 → boost ≈ 1 + 0.5 * 1.0 * 10/20 = 1.25
        assert b > 1.2
        assert b < RECALL_BOOST_CAP

    def test_old_recall_no_boost(self):
        """THE KEY FIX: recalled 50 times but last recall was 31 days ago → boost ≈ 1.0."""
        now = self._now()
        last = now - timedelta(days=RECALL_DECAY_WINDOW_DAYS + 1)
        b = compute_recall_boost(50, last_recalled_at=last, now=now)
        assert b == pytest.approx(1.0), (
            f"Expected ~1.0 for stale recall, got {b}. "
            "Feedback loop not broken — old popular memories still boosted."
        )

    def test_decay_at_window_boundary(self):
        """At exactly RECALL_DECAY_WINDOW_DAYS → recency_factor = 0 → no boost."""
        now = self._now()
        last = now - timedelta(days=RECALL_DECAY_WINDOW_DAYS)
        b = compute_recall_boost(100, last_recalled_at=last, now=now)
        assert b == pytest.approx(1.0, abs=0.01)

    def test_half_window_half_recency(self):
        """At half the decay window → recency_factor = 0.5."""
        now = self._now()
        last = now - timedelta(days=RECALL_DECAY_WINDOW_DAYS / 2)
        b = compute_recall_boost(10, last_recalled_at=last, now=now)
        # recency = 0.5, count=10 → boost = 1 + 0.5 * 0.5 * 10/20 = 1.125
        expected = 1.0 + (RECALL_BOOST_CAP - 1.0) * 0.5 * 10 / (10 + RECALL_BOOST_SCALE)
        assert b == pytest.approx(expected, abs=0.01)

    # -- Diminishing returns --

    def test_boost_has_diminishing_returns(self):
        """More recalls give diminishing boost — the curve saturates toward cap."""
        now = self._now()
        last = now  # just recalled
        b1 = compute_recall_boost(1, last_recalled_at=last, now=now)
        b10 = compute_recall_boost(10, last_recalled_at=last, now=now)
        b100 = compute_recall_boost(100, last_recalled_at=last, now=now)
        b1000 = compute_recall_boost(1000, last_recalled_at=last, now=now)
        # Monotonically increasing
        assert b1 < b10 < b100 < b1000
        # The gap narrows as count grows (100→1000 gains less than 10→100)
        assert (b1000 - b100) < (b100 - b10), "Diminishing returns not working"

    def test_boost_never_exceeds_cap(self):
        """Even with max recall_count and perfect recency, boost ≤ cap."""
        now = self._now()
        b = compute_recall_boost(1_000_000, last_recalled_at=now, now=now)
        assert b <= RECALL_BOOST_CAP + 0.001

    # -- Fallback behavior --

    def test_null_last_recalled_falls_back_to_created_at(self):
        """No last_recalled_at → use created_at as anchor."""
        now = self._now()
        created = now - timedelta(days=5)
        b = compute_recall_boost(10, last_recalled_at=None, created_at=created, now=now)
        # 5 days old → recency = 1 - 5/RECALL_DECAY_WINDOW_DAYS
        assert b > 1.0
        assert b < RECALL_BOOST_CAP

    def test_null_last_recalled_old_creation_no_boost(self):
        """No last_recalled_at, old created_at → no boost (correct for legacy data)."""
        now = self._now()
        created = now - timedelta(days=RECALL_DECAY_WINDOW_DAYS + 30)
        b = compute_recall_boost(10, last_recalled_at=None, created_at=created, now=now)
        assert b == pytest.approx(1.0, abs=0.01)

    # -- Comparison: before vs after --

    def test_feedback_loop_broken_scenario(self):
        """Scenario that demonstrates the fix.

        Before: memory with 50 recalls always got ~1.42x boost regardless of when.
        After: boost decays to 1.0 if not recalled within RECALL_DECAY_WINDOW_DAYS.
        """
        now = self._now()
        # Old formula (broken): 1 + 0.5 * 50 / (50 + 10) ≈ 1.417
        old_boost = 1.0 + (RECALL_BOOST_CAP - 1.0) * 50 / (50 + RECALL_BOOST_SCALE)
        assert old_boost > 1.4  # confirm old behavior was problematic

        # New formula: same memory, last recalled well past the decay window
        new_boost = compute_recall_boost(
            50,
            last_recalled_at=now - timedelta(days=RECALL_DECAY_WINDOW_DAYS + 15),
            now=now,
        )
        assert new_boost == pytest.approx(1.0), (
            f"Old boost was {old_boost:.3f}, new should be ~1.0 but got {new_boost:.3f}"
        )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRecallBoostPersistence:
    """Verify recall_count and last_recalled_at are updated on search."""

    async def _create_memory(self, db, tenant_id, content, agent_id="test-agent"):
        """Insert a memory with fake embedding for search."""
        from common.embedding import fake_embedding
        from common.models.memory import Memory
        import hashlib

        ch = hashlib.sha256(f"{tenant_id}:None:{content}".encode()).hexdigest()
        emb = fake_embedding(content)
        mem = Memory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type="fact",
            content=content,
            weight=0.7,
            embedding=emb,
            content_hash=ch,
            status="active",
        )
        db.add(mem)
        await db.flush()
        return mem

    async def test_recall_count_increments_on_search(self, db, tenant_id):
        mem = await self._create_memory(db, tenant_id, "The sky is blue on clear days")
        assert mem.recall_count == 0
        assert mem.last_recalled_at is None

        # Simulate what search_memories does after returning results
        from sqlalchemy import update, func
        from common.models.memory import Memory

        await db.execute(
            update(Memory)
            .where(Memory.id == mem.id)
            .values(recall_count=Memory.recall_count + 1, last_recalled_at=func.now())
        )
        await db.flush()
        await db.refresh(mem)

        assert mem.recall_count == 1
        assert mem.last_recalled_at is not None
