"""A64 — recall premise guard (org-opt-in prompt block).

The recall answer LLM used to comply with a question whose assumption its own
memories refute (STALE dim2: 0/15 in every retrieval config, oracle included).
One benchmark-tuned instruction block fixes that; it ships flag-gated behind
``recall.premise_guard`` and MUST leave the prompt byte-identical when off.
"""

import pytest
from core_api.services import recall_service as rs
from core_api.services.organization_settings import DEFAULT_SETTINGS, ResolvedConfig

pytestmark = pytest.mark.unit


def _cfg(guard):
    return ResolvedConfig({"recall": {"premise_guard": guard}})


def test_default_settings_carry_the_knob_off():
    assert DEFAULT_SETTINGS["recall"]["premise_guard"] is None
    assert _cfg(None).recall_premise_guard is False
    assert _cfg(False).recall_premise_guard is False
    assert _cfg(True).recall_premise_guard is True


def test_prompt_identical_when_guard_off():
    """The off path must not change a byte of the existing prompt."""
    rendered = rs.RECALL_PROMPT.format(
        query="q", memories="m", reference_date_line="", premise_guard_block=""
    )
    assert "assumption" not in rendered
    assert "Grounding rules — follow strictly:" in rendered


def test_prompt_carries_guard_when_on():
    rendered = rs.RECALL_PROMPT.format(
        query="q",
        memories="m",
        reference_date_line="",
        premise_guard_block=rs.PREMISE_GUARD_BLOCK,
    )
    assert "assumption appears outdated" in rendered
    # the anti-abstention clause (v2) must ride along — it is what keeps
    # knowledge-update questions answered (control pair evidence)
    assert "do not abstain merely because a memory is older" in rendered
    # guard sits above the grounding rules, not inside them
    assert rendered.index("assumption") < rendered.index("Grounding rules")


def test_block_ends_with_blank_line_for_clean_concatenation():
    assert rs.PREMISE_GUARD_BLOCK.endswith("\n\n")
