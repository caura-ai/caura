"""Tests for _extract_temporal_hint and _extract_temporal_date_range."""

from datetime import datetime, timedelta

from core_api.services.memory_service import (
    _extract_temporal_date_range,
    _extract_temporal_hint,
)


# ---------------------------------------------------------------------------
# Existing phrases (regression)
# ---------------------------------------------------------------------------


class TestExistingPhrases:
    def test_today(self):
        assert _extract_temporal_hint("what happened today") == timedelta(days=1)

    def test_yesterday(self):
        assert _extract_temporal_hint("show me yesterday's notes") == timedelta(days=2)

    def test_this_week(self):
        assert _extract_temporal_hint("updates this week") == timedelta(days=7)

    def test_past_week(self):
        assert _extract_temporal_hint("changes in the past week") == timedelta(days=7)

    def test_last_week(self):
        assert _extract_temporal_hint("what happened last week") == timedelta(days=14)

    def test_this_month(self):
        assert _extract_temporal_hint("this month summary") == timedelta(days=30)

    def test_last_month(self):
        assert _extract_temporal_hint("last month report") == timedelta(days=60)

    def test_this_quarter(self):
        assert _extract_temporal_hint("this quarter goals") == timedelta(days=90)

    def test_no_temporal_intent(self):
        assert _extract_temporal_hint("tell me about kafka") is None

    def test_empty_query(self):
        assert _extract_temporal_hint("") is None


# ---------------------------------------------------------------------------
# New phrases
# ---------------------------------------------------------------------------


class TestNewPhrases:
    def test_past_month(self):
        assert _extract_temporal_hint("changes in the past month") == timedelta(days=30)

    def test_this_year(self):
        assert _extract_temporal_hint("what happened this year") == timedelta(days=365)

    def test_last_year(self):
        assert _extract_temporal_hint("last year's incidents") == timedelta(days=730)


# ---------------------------------------------------------------------------
# Parameterized "last N days/weeks/months"
# ---------------------------------------------------------------------------


class TestParameterized:
    def test_last_3_days(self):
        assert _extract_temporal_hint("changes in the last 3 days") == timedelta(days=3)

    def test_last_2_weeks(self):
        assert _extract_temporal_hint("last 2 weeks of work") == timedelta(days=14)

    def test_last_6_months(self):
        assert _extract_temporal_hint("last 6 months summary") == timedelta(days=180)

    def test_last_1_day(self):
        assert _extract_temporal_hint("last 1 day") == timedelta(days=1)

    def test_plural_days(self):
        assert _extract_temporal_hint("last 10 days") == timedelta(days=10)

    def test_plural_weeks(self):
        assert _extract_temporal_hint("last 4 weeks") == timedelta(days=28)

    def test_zero_days(self):
        """'last 0 days' should NOT trigger temporal routing."""
        assert _extract_temporal_hint("last 0 days") is None

    def test_zero_weeks(self):
        assert _extract_temporal_hint("last 0 weeks") is None

    def test_no_match_last_chance(self):
        """'last chance' should NOT trigger parameterized pattern."""
        assert _extract_temporal_hint("last chance to submit") is None

    def test_no_match_latest(self):
        """'latest' should NOT trigger any temporal hint."""
        assert _extract_temporal_hint("show me the latest updates") is None


# ===========================================================================
# _extract_temporal_date_range — hard date-range filter extraction
# ===========================================================================

# Fixed reference date for deterministic tests.
_REF = datetime(2026, 4, 14, 12, 0, 0)


class TestDateRangeWordNumbers:
    """Word-number temporal expressions → date range."""

    def test_two_months_ago(self):
        result = _extract_temporal_date_range("what did I say two months ago", _REF)
        assert result is not None
        # 2 months ≈ 60 days → target ≈ 2026-02-13, range ±3 days (month unit)
        assert result["start_date"] == "2026-02-10"
        assert result["end_date"] == "2026-02-16"

    def test_three_weeks_ago(self):
        result = _extract_temporal_date_range("notes from three weeks ago", _REF)
        assert result is not None
        # 3 weeks = 21 days → target = 2026-03-24, range ±1 day (week unit)
        assert result["start_date"] == "2026-03-23"
        assert result["end_date"] == "2026-03-25"

    def test_five_days_back(self):
        result = _extract_temporal_date_range("something five days back", _REF)
        assert result is not None
        # 5 days → target = 2026-04-09, range ±0 days (day unit)
        assert result["start_date"] == "2026-04-09"
        assert result["end_date"] == "2026-04-09"

    def test_a_couple_of_weeks_ago(self):
        result = _extract_temporal_date_range("a couple of weeks ago I mentioned", _REF)
        assert result is not None
        # couple = 2 weeks = 14 days → target = 2026-03-31, range ±1
        assert result["start_date"] == "2026-03-30"
        assert result["end_date"] == "2026-04-01"

    def test_a_few_days_back(self):
        result = _extract_temporal_date_range("a few days back I said", _REF)
        assert result is not None
        # few = 3 days → target = 2026-04-11, range ±0
        assert result["start_date"] == "2026-04-11"
        assert result["end_date"] == "2026-04-11"


class TestDateRangeNumeric:
    """Numeric temporal expressions → date range."""

    def test_3_months_ago(self):
        result = _extract_temporal_date_range("what happened 3 months ago", _REF)
        assert result is not None
        # 3 * 30 = 90 days → target ≈ 2026-01-14, range ±3
        assert result["start_date"] == "2026-01-11"
        assert result["end_date"] == "2026-01-17"

    def test_10_days_ago(self):
        result = _extract_temporal_date_range("10 days ago I wrote", _REF)
        assert result is not None
        # 10 days → target = 2026-04-04, range ±0
        assert result["start_date"] == "2026-04-04"
        assert result["end_date"] == "2026-04-04"

    def test_2_years_back(self):
        result = _extract_temporal_date_range("2 years back", _REF)
        assert result is not None
        # 2 * 365 = 730 days → target = 2024-04-14 (2024 is leap year), range ±14
        assert result["start_date"] == "2024-03-31"
        assert result["end_date"] == "2024-04-28"


class TestDateRangeLastUnit:
    """'Last week/month/year' expressions."""

    def test_last_week(self):
        result = _extract_temporal_date_range("what did I mention last week", _REF)
        assert result is not None
        # 1 week = 7 days → target = 2026-04-07, range ±1
        assert result["start_date"] == "2026-04-06"
        assert result["end_date"] == "2026-04-08"

    def test_last_month(self):
        result = _extract_temporal_date_range("show me last month notes", _REF)
        assert result is not None
        # 1 * 30 = 30 days → target = 2026-03-15, range ±3
        assert result["start_date"] == "2026-03-12"
        assert result["end_date"] == "2026-03-18"

    def test_last_year(self):
        result = _extract_temporal_date_range("last year review", _REF)
        assert result is not None
        # 1 * 365 = 365 days → target ≈ 2025-04-14, range ±14
        assert result["start_date"] == "2025-03-31"
        assert result["end_date"] == "2025-04-28"


class TestDateRangeFuture:
    """Future-looking temporal expressions."""

    def test_in_two_weeks(self):
        result = _extract_temporal_date_range("what's planned in two weeks", _REF)
        assert result is not None
        # +14 days → target = 2026-04-28, range ±1
        assert result["start_date"] == "2026-04-27"
        assert result["end_date"] == "2026-04-29"

    def test_in_three_months(self):
        result = _extract_temporal_date_range("in three months we need to", _REF)
        assert result is not None
        # +90 days → target ≈ 2026-07-13, range ±3
        assert result["start_date"] == "2026-07-10"
        assert result["end_date"] == "2026-07-16"


class TestDateRangeEdgeCases:
    """Edge cases and non-matches."""

    def test_no_temporal_expression(self):
        assert _extract_temporal_date_range("tell me about kafka", _REF) is None

    def test_empty_query(self):
        assert _extract_temporal_date_range("", _REF) is None

    def test_defaults_to_utcnow(self):
        """When no reference_datetime is provided, uses utcnow."""
        result = _extract_temporal_date_range("two days ago")
        assert result is not None
        assert "start_date" in result
        assert "end_date" in result

    def test_today_not_matched(self):
        """'today' is handled by the soft hint, not the date range filter."""
        assert _extract_temporal_date_range("what happened today", _REF) is None

    def test_yesterday_not_matched(self):
        """'yesterday' is handled by the soft hint, not the date range filter."""
        assert _extract_temporal_date_range("show me yesterday's notes", _REF) is None
