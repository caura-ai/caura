"""P0-2: Temporal-window-aware freshness decay.

Unit tests validate the freshness logic using pure math (no DB).
Integration tests verify the SQL expressions produce correct values.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from core_api.constants import FRESHNESS_DECAY_DAYS, FRESHNESS_FLOOR


# ---------------------------------------------------------------------------
# Pure math helpers — replicate the SQL logic in Python for unit testing
# ---------------------------------------------------------------------------

def compute_freshness(
    created_at: datetime,
    ts_valid_start: datetime | None = None,
    ts_valid_end: datetime | None = None,
    now: datetime | None = None,
) -> float:
    """Python equivalent of the SQL freshness CASE expression.

    anchor = GREATEST(created_at, COALESCE(ts_valid_start, created_at))
    """
    now = now or datetime.now(timezone.utc)

    # Expired: validity window passed → floor
    if ts_valid_end is not None and ts_valid_end < now:
        return FRESHNESS_FLOOR

    # Still within validity window → full freshness
    if ts_valid_end is not None:
        return 1.0

    # No validity window: standard decay from anchor
    anchor = max(created_at, ts_valid_start or created_at)
    age_days = (now - anchor).total_seconds() / 86400.0

    if age_days < FRESHNESS_DECAY_DAYS:
        return 1.0 - (age_days / FRESHNESS_DECAY_DAYS) * (1.0 - FRESHNESS_FLOOR)
    return FRESHNESS_FLOOR


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFreshnessDecay:

    def _now(self):
        return datetime.now(timezone.utc)

    # -- Anchor date tests --

    def test_fresh_memory_full_score(self):
        """Memory created just now → freshness = 1.0."""
        f = compute_freshness(created_at=self._now())
        assert f == pytest.approx(1.0, abs=0.01)

    def test_old_memory_hits_floor(self):
        """Memory older than FRESHNESS_DECAY_DAYS → floor."""
        old = self._now() - timedelta(days=FRESHNESS_DECAY_DAYS + 1)
        f = compute_freshness(created_at=old)
        assert f == FRESHNESS_FLOOR

    def test_anchor_uses_valid_start_when_newer(self):
        """ts_valid_start is more recent than created_at → anchor = ts_valid_start."""
        now = self._now()
        created = now - timedelta(days=60)  # old creation
        valid_start = now - timedelta(days=5)  # recent event
        f = compute_freshness(created_at=created, ts_valid_start=valid_start, now=now)
        # Should be fresh (5 days old from anchor, not 60)
        expected = 1.0 - (5 / FRESHNESS_DECAY_DAYS) * (1.0 - FRESHNESS_FLOOR)
        assert f == pytest.approx(expected, abs=0.01)

    def test_anchor_uses_created_at_when_newer(self):
        """created_at is more recent than ts_valid_start → anchor = created_at."""
        now = self._now()
        created = now - timedelta(days=10)
        valid_start = now - timedelta(days=60)  # old event recorded recently
        f = compute_freshness(created_at=created, ts_valid_start=valid_start, now=now)
        expected = 1.0 - (10 / FRESHNESS_DECAY_DAYS) * (1.0 - FRESHNESS_FLOOR)
        assert f == pytest.approx(expected, abs=0.01)

    def test_no_valid_start_falls_back_to_created_at(self):
        """No ts_valid_start → anchor = created_at (original behavior)."""
        now = self._now()
        created = now - timedelta(days=45)
        f = compute_freshness(created_at=created, ts_valid_start=None, now=now)
        expected = 1.0 - (45 / FRESHNESS_DECAY_DAYS) * (1.0 - FRESHNESS_FLOOR)
        assert f == pytest.approx(expected, abs=0.01)

    # -- Validity window tests --

    def test_expired_memory_gets_floor(self):
        """ts_valid_end in the past → forced to FRESHNESS_FLOOR regardless of age."""
        now = self._now()
        created = now - timedelta(days=5)  # very recent
        valid_end = now - timedelta(hours=1)  # expired 1 hour ago
        f = compute_freshness(created_at=created, ts_valid_end=valid_end, now=now)
        assert f == FRESHNESS_FLOOR

    def test_still_valid_memory_full_freshness(self):
        """ts_valid_end in the future → full freshness regardless of creation age."""
        now = self._now()
        created = now - timedelta(days=200)  # very old
        valid_end = now + timedelta(days=30)  # still valid
        f = compute_freshness(created_at=created, ts_valid_end=valid_end, now=now)
        assert f == 1.0

    def test_valid_end_today_still_valid(self):
        """ts_valid_end = now (still in the future by a tiny margin)."""
        now = self._now()
        valid_end = now + timedelta(seconds=1)
        f = compute_freshness(created_at=now - timedelta(days=100), ts_valid_end=valid_end, now=now)
        assert f == 1.0

    # -- Decay curve shape tests --

    def test_linear_decay_midpoint(self):
        """At FRESHNESS_DECAY_DAYS/2, freshness should be midway between 1.0 and floor."""
        now = self._now()
        created = now - timedelta(days=FRESHNESS_DECAY_DAYS / 2)
        f = compute_freshness(created_at=created, now=now)
        midpoint = 1.0 - 0.5 * (1.0 - FRESHNESS_FLOOR)
        assert f == pytest.approx(midpoint, abs=0.01)

    def test_decay_is_monotonic(self):
        """Freshness decreases monotonically with age."""
        now = self._now()
        values = []
        for days in range(0, FRESHNESS_DECAY_DAYS + 10, 5):
            created = now - timedelta(days=days)
            values.append(compute_freshness(created_at=created, now=now))
        # Each value should be <= previous
        for i in range(1, len(values)):
            assert values[i] <= values[i - 1] + 0.001

    # -- Key fix validation: the original bug --

    def test_old_event_recorded_today_stays_fresh(self):
        """THE FIX: memory created today about old event → anchor = created_at → fresh.

        Before the fix, this was creation-biased and got full freshness.
        After the fix, it's still fresh because created_at IS recent.
        The fix matters when ts_valid_start pushes the anchor forward.
        """
        now = self._now()
        created = now  # just created
        valid_start = now - timedelta(days=365 * 5)  # about 5-year-old event
        f = compute_freshness(created_at=created, ts_valid_start=valid_start, now=now)
        # anchor = max(now, 5_years_ago) = now → fresh
        assert f == pytest.approx(1.0, abs=0.01)

    def test_old_creation_recent_event_stays_fresh(self):
        """THE KEY FIX: memory created 91 days ago about an event that happened 3 days ago.

        Before: anchor = created_at (91 days ago) → FLOOR (0.5)
        After:  anchor = max(created_at, ts_valid_start) = 3 days ago → ~0.98
        """
        now = self._now()
        created = now - timedelta(days=91)
        valid_start = now - timedelta(days=3)
        f = compute_freshness(created_at=created, ts_valid_start=valid_start, now=now)
        assert f > 0.95, f"Expected >0.95, got {f} — ts_valid_start should pull anchor forward"
