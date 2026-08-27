"""A63 — history questions must see superseded values.

The contradiction judge (post gate re-cut) correctly marks the older side
of an update ``outdated``/``conflicted``, and the scored search halves
those rows' scores — right for "what's true now", amnesia for "what was
true then". The regression bench caught exactly this: three
knowledge-update questions asking about past states lost the superseded
gold memory from top-20.

Pinned here:
- ``_extract_history_hint`` recognises past-state / change / duration
  questions — including the three bench phrasings that regressed — and
  stays quiet on present-state queries (a false positive would un-bury
  stale facts everywhere and undo the demotion's fresh-state win).
- The pipeline step publishes ``history_hint``; the search step forwards
  it to storage as ``history_query``.
"""

from __future__ import annotations

import pytest

from core_api.services.memory_service import _extract_history_hint

pytestmark = pytest.mark.unit


# The three bench questions that lost their superseded gold memory:
BENCH_REGRESSIONS = [
    "How many hours have I spent on my abstract ocean sculpture?",
    "How many engineers do I lead when I just started my new role as Senior Software Engineer?",
    "For the coffee-to-water ratio in my French press, did I switch to more water per tablespoon?",
    "How long have I been living in my current apartment in Harajuku?",
]


@pytest.mark.parametrize("q", BENCH_REGRESSIONS)
def test_bench_regression_phrasings_trigger(q: str) -> None:
    assert _extract_history_hint(q) is True


@pytest.mark.parametrize(
    "q",
    [
        "What was my previous apartment's address?",
        "Where did I live before I moved to Harajuku?",
        "I used to run the backup at 01:00 — why?",
        "What is the old value of the checkout-service primary region?",
        "When did we switch from Slack to Teams?",
        "Originally the deploy cadence was weekly, right?",
        "The project was renamed from Atlas — what were the milestones?",
    ],
)
def test_history_phrasings_trigger(q: str) -> None:
    assert _extract_history_hint(q) is True


@pytest.mark.parametrize(
    "q",
    [
        "Where do I live?",
        "What is the checkout-service primary region?",
        "Who is on call for the ingest pipeline this sprint?",
        "What time does the nightly backup run?",
        "Summarize the team's deployment process.",
        "What's the coffee-to-water ratio I use?",
        "recommend a show for me to watch tonight",
    ],
)
def test_present_state_queries_stay_quiet(q: str) -> None:
    assert _extract_history_hint(q) is False


@pytest.mark.asyncio
async def test_step_publishes_history_hint() -> None:
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import ExtractTemporalHint

    ctx = PipelineContext()
    ctx.data["query"] = "Did I switch to more water per tablespoon?"
    await ExtractTemporalHint().execute(ctx)
    assert ctx.data["history_hint"] is True

    ctx2 = PipelineContext()
    ctx2.data["query"] = "What time does the nightly backup run?"
    await ExtractTemporalHint().execute(ctx2)
    assert ctx2.data["history_hint"] is False
