"""CAURA-719 — ``tags`` and ``status`` leave the enrichment prompt.

Both fields cost tokens on every write and neither earned them:

* **tags** — no reader. Nothing filters, queries, or full-text-indexes
  them (the FTS vector is ``title || content``, migration 034), so a
  measured 95.5% of the eToro corpus (46,203 / 48,374 rows) carried
  generated tags that only ever travelled back out to the caller. C25
  made tags a CALLER-ownable key, so dropping the LLM's copy leaves a
  coherent field rather than a gap: callers who want tags supply them.

* **status** — a measured constant. All 48,374 rows carried ``active``;
  the classifier never once chose ``pending`` or ``confirmed``. Worse
  than useless: ~11 query paths filter the literal string ``'active'``
  instead of ``LIVE_MEMORY_STATUSES``, so a memory the LLM labelled
  ``confirmed`` went invisible to insights. That was observed in
  production and already worked around by pinning ``status="active"``
  in ``doc_memory.py`` and ``insights_service.py``. ``status`` is a
  LIFECYCLE field owned by explicit setters: ``caura_manage``'s
  transition op, the contradiction detector (``outdated`` /
  ``conflicted``), the crystallizer (``archived``), the delete path.

Both keep their ``EnrichmentResult`` entry, default, and validator, so
the schema stays deliberately WIDER than the prompt. Three consumers
depend on that: historical rows, caller-supplied values, and the
deferred worker's patch path.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _prompt() -> str:
    from common.enrichment._prompts import ENRICHMENT_PROMPT

    return ENRICHMENT_PROMPT


# ---------------------------------------------------------------------------
# Prompt — the fields are gone, and the fields that remain stay coherent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["tags", "status"])
def test_prompt_does_not_request_field(field: str):
    assert f'"{field}"' not in _prompt(), (
        f"CAURA-719 removed {field!r} from the enrichment prompt"
    )


@pytest.mark.parametrize("field", ["tags", "status"])
def test_json_template_omits_field(field: str):
    """The template is what the model pattern-matches on, so a stale key
    there would reintroduce the field even with its description gone."""
    template = _prompt().rsplit("Return ONLY valid JSON", 1)[-1]
    assert f'"{field}"' not in template


def test_prompt_still_requests_every_field_the_schema_needs():
    """Guard the other direction: removing two fields must not have
    disturbed the eleven that remain."""
    prompt = _prompt()
    for field in (
        "memory_type",
        "weight",
        "title",
        "summary",
        "ts_valid_start",
        "ts_valid_end",
        "contains_pii",
        "pii_types",
        "retrieval_hint",
        "atomic_facts",
        "business_relevance",
    ):
        assert f'"{field}"' in prompt, f"{field!r} disappeared from the prompt"


def test_field_numbering_is_contiguous():
    """The numbered list drives nothing mechanically, but a gap (or a
    duplicate) reads as a malformed spec to the model — and the
    ``atomic_facts`` block cross-references ``retrieval_hint`` BY NUMBER,
    so the numbering has to actually be right."""
    import re

    numbers = [int(m) for m in re.findall(r"^(\d+)\. \"", _prompt(), re.MULTILINE)]
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"prompt field numbering is not contiguous: {numbers}"
    )


def test_atomic_facts_cross_reference_points_at_retrieval_hint():
    """``atomic_facts`` says "same guidance as field N" for its per-fact
    hint. After renumbering, N must still be ``retrieval_hint``."""
    import re

    prompt = _prompt()
    ref = re.search(r"same guidance as field (\d+)", prompt)
    assert ref, "atomic_facts lost its retrieval_hint cross-reference"
    target = int(ref.group(1))
    heading = re.search(rf'^{target}\. "(\w+)"', prompt, re.MULTILINE)
    assert heading and heading.group(1) == "retrieval_hint", (
        f"cross-reference points at field {target} "
        f"({heading.group(1) if heading else 'missing'}), not retrieval_hint"
    )


# ---------------------------------------------------------------------------
# Schema — deliberately wider than the prompt.
# ---------------------------------------------------------------------------


def test_enrichment_result_defaults_when_llm_omits_both_fields():
    """The realistic post-CAURA-719 payload: the model returns neither
    key. Both must land on their schema defaults rather than raising."""
    from common.enrichment.schema import EnrichmentResult

    result = EnrichmentResult(memory_type="fact", title="t", summary="s")
    assert result.tags == []
    assert result.status == "active"


def test_validate_enrichment_defaults_status_when_absent():
    """``_validate_enrichment`` is the layer that sees raw LLM JSON. With
    ``status`` no longer requested, the key is simply missing — which must
    coerce to ``active``, not ``None`` (the row column is NOT NULL)."""
    from common.enrichment.service import _validate_enrichment

    result = _validate_enrichment(
        {"memory_type": "fact", "title": "t", "summary": "s"}, llm_ms=12
    )
    assert result.status == "active"
    assert result.tags == []


def test_caller_supplied_tags_still_normalise():
    """Tags are caller-owned now, so the A8 normaliser has to keep
    working on values the platform did not generate."""
    from common.enrichment.schema import EnrichmentResult

    result = EnrichmentResult(tags=["Code Review", "code_review", "  DEPLOY  "])
    assert result.tags == ["code-review", "deploy"]


def test_historical_llm_status_is_still_accepted():
    """Rows enriched before CAURA-719 carry a model-assigned status, and
    the deferred worker replays stored enrichment payloads. A value we no
    longer ask for must still deserialise rather than being demoted."""
    from common.enrichment.service import _validate_enrichment

    result = _validate_enrichment(
        {"memory_type": "task", "status": "pending"}, llm_ms=1
    )
    assert result.status == "pending"


def test_out_of_vocabulary_status_still_demotes_to_active():
    """The existing guard is unchanged: anything outside
    ``MEMORY_STATUSES`` falls back to ``active``."""
    from common.enrichment.service import _validate_enrichment

    result = _validate_enrichment({"memory_type": "fact", "status": "banana"}, llm_ms=1)
    assert result.status == "active"


# ---------------------------------------------------------------------------
# Agent-facing surface — the tool docs must not promise tags any more.
# ---------------------------------------------------------------------------


def test_caura_write_description_does_not_promise_tags():
    """``caura_write`` advertised "auto-classifies … tags" to every agent.
    Leaving that in place would document a feature the platform no longer
    performs."""
    from core_api.tools.caura_write import _DESCRIPTION

    assert "tags" not in _DESCRIPTION.lower()


def test_mcp_server_instructions_do_not_promise_tags():
    from core_api import mcp_server

    instructions = mcp_server.mcp.instructions or ""
    assert "and tags via LLM" not in instructions
