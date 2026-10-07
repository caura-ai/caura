"""M-44: Forge skips a cluster the model marked undistillable.

The distill prompt tells the model to answer a cluster it cannot distill (mixed
outcomes, divergent step orders, too few traces) with a valid candidate whose
goal_phrase is "" and step_skeleton is [], adding that "the auto-gates
downstream will reject those". None did: parse_distill_response accepted both,
the six auto-gates read origin, fingerprint, scan and kind, and neither field is
stored on the doc. Forge staged the candidate like any other, and one that
cleared the gates could auto-activate.
"""

import json
from datetime import UTC, datetime

import pytest

import core_api.services.forge.forge_service as forge_service
from core_api.services.forge.distill_prompt import (
    DistillParseError,
    parse_distill_response,
)
from core_api.services.forge.forge_service import run_forge_distill
from tests.test_forge_distill import (
    _capture_writer,
    _golden_llm_response,
    _memory_fetcher_always,
    _poison_never,
    _two_eligible_clusters,
)

pytestmark = pytest.mark.unit

_UNDISTILLABLE = {"goal_phrase": "", "step_skeleton": []}


@pytest.mark.parametrize(
    "marker",
    [_UNDISTILLABLE, {"goal_phrase": "   "}, {"step_skeleton": []}],
    ids=["as-prompted", "blank-goal", "no-steps"],
)
def test_the_undistillable_marker_is_a_distill_error(marker: dict) -> None:
    raw = json.dumps(_golden_llm_response(**marker))

    with pytest.raises(DistillParseError, match="undistillable"):
        parse_distill_response(raw)


async def test_forge_writes_no_candidate_for_an_undistillable_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipped and counted like a kind="update" reply; the run goes on."""

    async def traces(*_args, **_kwargs):
        return _two_eligible_clusters()

    monkeypatch.setattr(forge_service, "build_session_traces", traces)
    calls = 0

    async def llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(_golden_llm_response(**_UNDISTILLABLE))
        return json.dumps(_golden_llm_response(slug=f"c-{calls}"))

    captured, writer = _capture_writer()
    result = await run_forge_distill(
        run_label="test-run",
        tenant_id="t1",
        fleet_id=None,
        window_start=datetime(2026, 5, 1, tzinfo=UTC),
        window_end=datetime(2026, 5, 15, tzinfo=UTC),
        llm_fn=llm,
        memory_fetcher=_memory_fetcher_always,
        poison_checker=_poison_never,
        candidate_writer=writer,
    )

    assert result.candidates_skipped_distill_error == 1
    assert result.candidates_written == 1
    assert [doc["data"]["slug"] for doc in captured] == ["c-2"]
