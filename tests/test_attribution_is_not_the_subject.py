"""A fact reported by a named agent was never corrected.

Fleet Memory Bench, changed-twice set where each fact names its writer:

    "Tavi Ren recorded the Velquill gateway's primary region as amber basin."
    "Nera Sol changed the Velquill gateway's primary region to cobalt ridge."
    "Pelo Varn confirmed that the Velquill gateway's primary region is now silver delta."

Extraction took the grammatical subject of the main clause, so the stored
subjects were ``tavi ren``, ``nera sol`` and ``pelo varn``. The Path C preflight
then dropped every candidate (distinct ``subject_entity_id``) and the Path A
judge set same_subject=false: 0 of 165 cases had any row marked conflicted, and
the stale facts stayed in the top 5 in 0.99 of them.

These tests pin the prompt rule. Only a live run proves model behaviour.
"""

import pytest

from core_api.services.contradiction_detector import (
    BATCH_CONTRADICTION_PROMPT,
    CONTRADICTION_PROMPT,
)
from core_api.services.entity_extraction import EXTRACTION_PROMPT

pytestmark = pytest.mark.unit


def test_extraction_says_the_reporter_is_not_the_subject():
    assert "ATTRIBUTION IS NOT THE SUBJECT" in EXTRACTION_PROMPT
    assert "The reporter is role=mentioned" in EXTRACTION_PROMPT


def test_extraction_carries_a_reported_example():
    assert "Nera Sol changed the Velquill gateway's primary region" in EXTRACTION_PROMPT
    assert "mentioned: nera sol" in EXTRACTION_PROMPT


@pytest.mark.parametrize("prompt", [CONTRADICTION_PROMPT, BATCH_CONTRADICTION_PROMPT])
def test_judge_prompts_say_the_reporter_is_not_the_subject(prompt):
    assert "not the person or agent who" in prompt


def test_extraction_says_v_as_the_s_puts_the_subject_last():
    assert '"V AS THE S" PUTS THE SUBJECT LAST' in EXTRACTION_PROMPT
    assert "subject: orvella api rollout policy" in EXTRACTION_PROMPT


def test_extraction_names_the_subject_with_its_attribute():
    assert "Name the subject with its attribute" in EXTRACTION_PROMPT
