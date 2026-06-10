"""CAURA-129 — Direct unit tests for the entity-aware retraction prompt
and judge.

The retraction flow itself is covered by
``tests/test_a4_13_path_c_retraction.py``. This file exercises the new
plumbing in isolation:

  * ``ENTITY_AWARE_CONTRADICTION_PROMPT`` template invariants —
    required placeholders, required JSON keys, authoritative-entity
    framing.
  * ``_format_entity_context`` — readable rendering, empty-list path.
  * ``_fetch_entity_context`` — storage round-trip composition,
    canonical_name fallback, error swallowing.
  * ``_llm_entity_aware_contradiction_check`` — wiring to
    ``call_with_fallback`` with the right service label, timeout, and
    ``_judge_contradiction`` parser reuse.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# ENTITY_AWARE_CONTRADICTION_PROMPT template invariants
# ---------------------------------------------------------------------------


def test_prompt_has_required_placeholders():
    """The prompt must format with the four expected fields and only
    those four. Missing placeholders fail at .format() time in
    production — this test catches drift early."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
    )

    for placeholder in (
        "{new_content}",
        "{old_content}",
        "{new_entities}",
        "{old_entities}",
    ):
        assert placeholder in ENTITY_AWARE_CONTRADICTION_PROMPT, (
            f"prompt missing placeholder {placeholder!r}"
        )


def test_prompt_renders_with_realistic_inputs():
    """End-to-end .format() with realistic values — catches format-spec
    bugs (e.g. an accidentally-unescaped JSON brace would raise here)."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
    )

    rendered = ENTITY_AWARE_CONTRADICTION_PROMPT.format(
        new_content="Project Helios has release date 2027-05-01.",
        old_content="Project Helios has release date 2028-10-15.",
        new_entities='- "Project Helios" (type: project, role: subject)',
        old_entities='- "Project Helios" (type: project, role: subject)',
    )
    assert "Project Helios" in rendered
    assert "2027-05-01" in rendered
    assert "2028-10-15" in rendered
    # JSON braces should survive the format() call as literal braces.
    assert '"contradicts": true/false' in rendered


def test_prompt_keeps_contradiction_json_schema_for_parser_reuse():
    """``_judge_contradiction`` parses the same five keys for both
    prompts. If the entity-aware prompt drifts on the schema, the
    parser silently mis-classifies. Lock the schema here."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
    )

    for key in (
        "subject_a",
        "subject_b",
        "same_subject",
        "non_conflict_reason",
        "contradicts",
    ):
        assert key in ENTITY_AWARE_CONTRADICTION_PROMPT, (
            f"prompt missing JSON key {key!r} expected by _judge_contradiction"
        )


def test_prompt_frames_resolved_entities_as_authoritative():
    """The whole point of the new prompt is that resolved entities
    override raw-text NER. If this framing weakens, we're back to
    CAURA-128's stochastic flips."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
    )

    text = ENTITY_AWARE_CONTRADICTION_PROMPT.lower()
    assert "authoritative" in text, (
        "prompt must explicitly mark the resolved entities as authoritative"
    )
    assert "resolved entities" in text


def test_prompt_decides_same_subject_mechanically_on_entity_id():
    """CAURA-133 — the priya silence on dev v2.12.1 showed that the
    original ``If the subjects are the SAME canonical entity row, treat
    same_subject as true`` wording wasn't forceful enough: the LLM still
    fell back to text-NER and used surface qualifiers ("Priya from
    AcmeCorp" vs "Priya from BetaIndustries") to set same_subject=false.

    The fix anchors the decision on a mechanical entity_id comparison
    that doesn't leave room for text reasoning. Lock the mechanical
    framing here so a future prompt edit doesn't quietly weaken it."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
    )

    text = ENTITY_AWARE_CONTRADICTION_PROMPT.lower()
    # The instruction must explicitly use the word "mechanically" (or
    # close synonym) and must reference ``entity_id`` equality as the
    # decision rule. Both halves of the rule must be present.
    assert "mechanically" in text, (
        "prompt must instruct the model to decide same_subject MECHANICALLY"
    )
    assert "entity_id == subject" in text or "entity_id ==" in text, (
        "prompt must show the entity_id-equality decision rule"
    )
    assert "entity_id !=" in text, (
        "prompt must show the entity_id-inequality decision rule (the symmetric "
        "same_name_distinct_subject case)"
    )


def test_prompt_contains_positive_worked_example_for_priya_silence():
    """CAURA-133 — the wet-test on dev v2.12.1 (CAURA-132 logs) showed
    the entity-aware judge returned ``verdict=False`` on 15 of 16
    candidate-pairs with shared canonical subject. A worked example
    that mirrors the exact silence shape anchors the model on the
    correct verdict for the dominant production failure mode."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
    )

    # The positive example shows: same entity_id on both sides, but
    # surface text uses different employer qualifiers.
    assert "AcmeCorp" in ENTITY_AWARE_CONTRADICTION_PROMPT
    assert "BetaIndustries" in ENTITY_AWARE_CONTRADICTION_PROMPT
    # And the correct verdict for that shape.
    assert "same_subject=true" in ENTITY_AWARE_CONTRADICTION_PROMPT


def test_prompt_contains_counter_example_for_distinct_entity_same_name():
    """CAURA-133 — preserve the L3.4 boundary. The counter-example
    keeps the model from over-flagging when canonical names happen to
    collide but the resolved entity_ids are distinct. Lock it in so
    we don't trade priya-silence (false negatives) for a different
    false-positive class."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
    )

    # Distinct entity_ids in the counter-example (any two distinct
    # tokens work for the lock-in; we use the literal IDs from the
    # prompt's worked-example block).
    assert "XYZ789" in ENTITY_AWARE_CONTRADICTION_PROMPT, (
        "counter-example (distinct entity_id, same canonical name) must be present"
    )
    assert "ABC123" in ENTITY_AWARE_CONTRADICTION_PROMPT
    # And the correct verdict for that shape — same_subject=false.
    assert "same_subject=false" in ENTITY_AWARE_CONTRADICTION_PROMPT


# ---------------------------------------------------------------------------
# _format_entity_context — rendering shape
# ---------------------------------------------------------------------------


def test_format_entity_context_renders_bullets():
    from core_api.services.contradiction_detector import _format_entity_context

    out = _format_entity_context(
        [
            {
                "name": "Project Helios",
                "entity_type": "project",
                "role": "subject",
                "entity_id": "ent-helios",
            },
            {
                "name": "2027-05-01",
                "entity_type": "date",
                "role": "object",
                "entity_id": "ent-date-1",
            },
        ]
    )
    # CAURA-133 — entity_id is now rendered into each bullet so the
    # mechanical entity_id-equality check the prompt asks for has the
    # data it needs.
    assert (
        '- "Project Helios" (type: project, role: subject, entity_id: ent-helios)'
        in out
    )
    assert '- "2027-05-01" (type: date, role: object, entity_id: ent-date-1)' in out


def test_format_entity_context_renders_entity_id_for_priya_repro():
    """CAURA-133 — lock in the priya-shape rendering. Both sides must
    carry the same ``entity_id`` in the rendered text so the LLM sees
    the equality the prompt instructs it to compare."""
    from core_api.services.contradiction_detector import _format_entity_context

    side_a = _format_entity_context(
        [
            {
                "name": "Priya",
                "entity_type": "person",
                "role": "subject",
                "entity_id": "ABC123",
            }
        ]
    )
    side_b = _format_entity_context(
        [
            {
                "name": "Priya",
                "entity_type": "person",
                "role": "subject",
                "entity_id": "ABC123",
            }
        ]
    )
    assert "entity_id: ABC123" in side_a
    assert "entity_id: ABC123" in side_b


def test_format_entity_context_renders_per_row_none_sentinel_when_entity_id_missing():
    """Defensive — production data may have a subject-role link with
    no ``entity_id`` (e.g., legacy rows). Render a per-row sentinel
    ``<none-{process_uuid}-{i}>`` shape:

      * The process-scoped ``_NONE_ID_PREFIX`` ensures the sentinel
        can never collide with a real entity_id (real ids are UUIDs
        written by the entity-extraction worker).
      * The per-row ``-{i}>`` suffix ensures two missing rows in the
        SAME call never render the same string.

    Cross-side disambiguation (both ``_format_entity_context`` calls
    in a single judge invocation produce ``<none-{prefix}-0>``) is
    handled by the prompt's same_subject step rule 3 — exercised by
    a separate test below."""
    from core_api.services.contradiction_detector import (
        _NONE_ID_PREFIX,
        _format_entity_context,
    )

    expected_prefix = _NONE_ID_PREFIX  # e.g. "<none-a1b2c3d4"

    # Single missing entity_id -> index 0 sentinel.
    out = _format_entity_context(
        [{"name": "Lost Soul", "entity_type": "person", "role": "subject"}]
    )
    assert f"entity_id: {expected_prefix}-0>" in out, (
        f"expected sentinel ending in '-0>' with prefix {expected_prefix!r}; got: {out}"
    )

    # Two missing-on-same-side entity_ids -> distinct sentinels (the
    # within-side disambiguation layer).
    out_two = _format_entity_context(
        [
            {"name": "Lost Soul", "entity_type": "person", "role": "subject"},
            {"name": "Also Lost", "entity_type": "person", "role": "object"},
        ]
    )
    assert f"entity_id: {expected_prefix}-0>" in out_two
    assert f"entity_id: {expected_prefix}-1>" in out_two


def test_none_id_prefix_is_process_scoped_and_distinctive():
    """The ``_NONE_ID_PREFIX`` constant is generated once per process
    at import time. It starts with ``<none-`` and includes a random
    hex segment so the sentinel can never collide with a real
    entity_id (which is always a canonical UUID written by the
    entity-extraction worker — no ``<``, no ``none-`` literal text).
    """
    from core_api.services.contradiction_detector import _NONE_ID_PREFIX

    assert _NONE_ID_PREFIX.startswith("<none-"), (
        f"sentinel prefix must start with '<none-' to match the prompt's "
        f"rule 3 (case-insensitive substring check); got {_NONE_ID_PREFIX!r}"
    )
    # Eight hex chars after the prefix marker — enough entropy that
    # the literal text could not have been written into a real row.
    suffix = _NONE_ID_PREFIX[len("<none-") :]
    assert len(suffix) == 8, f"expected 8-hex random segment; got {suffix!r}"
    assert all(c in "0123456789abcdef" for c in suffix), (
        f"random segment must be lowercase hex; got {suffix!r}"
    )


def test_prompt_treats_none_prefixed_entity_id_as_same_subject_false():
    """The mechanical entity_id-equality rule (rule 1) would say two
    degenerate rows (both rendering as ``<none-{prefix}-0>`` because
    both ``_format_entity_context`` calls in one judge invocation
    share the process-scoped prefix and both first-index missing rows)
    are the same subject — exactly wrong. Rule 3 of the prompt's
    same_subject step overrides rule 1 for any ``<none-``-prefixed
    value, treating it as same_subject=false. Lock the rule in so a
    future prompt edit can't silently drop it.

    NB: the prefix in the prompt is ``<none-`` (with hyphen) — tighter
    than just ``<none`` so a hypothetical real entity literally named
    ``<none>`` doesn't accidentally trigger the override."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
    )

    text = ENTITY_AWARE_CONTRADICTION_PROMPT.lower()
    assert "<none-" in text, (
        "prompt must reference the <none-prefixed sentinel (with hyphen) "
        "matching the format produced by _format_entity_context"
    )
    assert "no stable identifier" in text or "do not infer identity" in text, (
        "prompt must explain WHY <none>-id rows aren't equal under the mechanical rule"
    )


def test_format_entity_context_empty_returns_sentinel():
    """Empty list renders as a placeholder so the prompt stays
    well-formed even if a buggy caller skips the guard."""
    from core_api.services.contradiction_detector import _format_entity_context

    assert _format_entity_context([]) == "(none resolved)"


def test_format_entity_context_tolerates_missing_fields():
    """Defensive — production data may have null role / type. Render
    them as ``<unknown>`` / ``<unspecified>`` instead of raising."""
    from core_api.services.contradiction_detector import _format_entity_context

    out = _format_entity_context([{"name": None, "entity_type": None, "role": None}])
    assert "<unknown>" in out
    assert "<unspecified>" in out


def test_format_entity_context_caps_list_at_ten_entities():
    """Bound the prompt blast radius — only the first
    ``_ENTITY_CONTEXT_MAX_ENTITIES`` (10) bullets are rendered. Mirrors
    the ``[:500]`` content truncation in the judge."""
    from core_api.services.contradiction_detector import (
        _ENTITY_CONTEXT_MAX_ENTITIES,
        _format_entity_context,
    )

    many = [
        {"name": f"entity-{i}", "entity_type": "project", "role": "subject"}
        for i in range(25)
    ]
    out = _format_entity_context(many)
    bullet_count = out.count("\n- ") + (1 if out.startswith("- ") else 0)
    assert bullet_count == _ENTITY_CONTEXT_MAX_ENTITIES == 10
    # The 11th entity onwards must be dropped.
    assert "entity-10" not in out
    # Sanity: the rendered ones ARE present.
    assert "entity-0" in out
    assert "entity-9" in out


def test_format_entity_context_truncates_long_names():
    """Each rendered name is truncated to
    ``_ENTITY_CONTEXT_NAME_MAX_CHARS`` (100). Adversarial / runaway
    canonical names can't blow up the prompt token cost."""
    from core_api.services.contradiction_detector import (
        _ENTITY_CONTEXT_NAME_MAX_CHARS,
        _format_entity_context,
    )

    out = _format_entity_context(
        [{"name": "A" * 500, "entity_type": "project", "role": "subject"}]
    )
    # The rendered bullet must contain at most _ENTITY_CONTEXT_NAME_MAX_CHARS A's.
    a_run = out.count("A")
    assert a_run <= _ENTITY_CONTEXT_NAME_MAX_CHARS == 100, (
        f"expected name truncated to ≤100 chars; got {a_run} A's"
    )


def test_format_entity_context_prefers_canonical_name_over_name():
    """When the input dict carries both ``canonical_name`` and ``name``
    (e.g. a raw entity row rather than the normalised shape from
    ``_fetch_entity_context``), prefer the canonical."""
    from core_api.services.contradiction_detector import _format_entity_context

    out = _format_entity_context(
        [
            {
                "canonical_name": "Project Helios",
                "name": "Helios",
                "entity_type": "project",
                "role": "subject",
            }
        ]
    )
    assert '"Project Helios"' in out
    assert '"Helios"' not in out


# ---------------------------------------------------------------------------
# _fetch_entity_context — storage composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_entity_context_composes_two_round_trips():
    """One call to ``get_entity_links_for_memories`` (batch endpoint,
    one round-trip) + one ``get_entity`` per link, gathered in
    parallel. Caller gets a clean list of ``{name, entity_type,
    role}`` dicts."""
    from core_api.services.contradiction_detector import _fetch_entity_context

    mem_id = str(uuid4())
    ent_a, ent_b = str(uuid4()), str(uuid4())

    sc = AsyncMock()
    sc.get_entity_links_for_memories = AsyncMock(
        return_value={
            mem_id: [
                {"entity_id": ent_a, "role": "subject"},
                {"entity_id": ent_b, "role": "object"},
            ]
        }
    )

    async def get_entity(eid: str) -> dict | None:
        return {
            ent_a: {"canonical_name": "Project Helios", "entity_type": "project"},
            ent_b: {"canonical_name": "2027-05-01", "entity_type": "date"},
        }.get(eid)

    sc.get_entity = AsyncMock(side_effect=get_entity)

    out = await _fetch_entity_context(sc, mem_id)
    by_name = {e["name"]: e for e in out}
    assert by_name["Project Helios"]["entity_type"] == "project"
    assert by_name["Project Helios"]["role"] == "subject"
    assert by_name["2027-05-01"]["entity_type"] == "date"
    assert by_name["2027-05-01"]["role"] == "object"


@pytest.mark.asyncio
async def test_fetch_entity_context_empty_when_no_links():
    from core_api.services.contradiction_detector import _fetch_entity_context

    mem_id = str(uuid4())
    sc = AsyncMock()
    sc.get_entity_links_for_memories = AsyncMock(return_value={mem_id: []})
    sc.get_entity = AsyncMock()

    out = await _fetch_entity_context(sc, mem_id)
    assert out == []
    sc.get_entity.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_entity_context_falls_back_to_name_if_no_canonical_name():
    """Some test/legacy fixtures use ``name`` instead of
    ``canonical_name``. Tolerate both."""
    from core_api.services.contradiction_detector import _fetch_entity_context

    mem_id = str(uuid4())
    ent = str(uuid4())
    sc = AsyncMock()
    sc.get_entity_links_for_memories = AsyncMock(
        return_value={mem_id: [{"entity_id": ent, "role": "subject"}]}
    )
    sc.get_entity = AsyncMock(
        return_value={"name": "Legacy Entity", "entity_type": "project"}
    )
    out = await _fetch_entity_context(sc, mem_id)
    assert out[0]["name"] == "Legacy Entity"


@pytest.mark.asyncio
async def test_fetch_entity_context_swallows_links_lookup_error():
    """Storage layer error must NOT propagate — Path C is post-commit
    and best-effort. Return empty so the empty-context guard fires."""
    from core_api.services.contradiction_detector import _fetch_entity_context

    sc = AsyncMock()
    sc.get_entity_links_for_memories = AsyncMock(
        side_effect=RuntimeError("storage down")
    )
    out = await _fetch_entity_context(sc, str(uuid4()))
    assert out == []


@pytest.mark.asyncio
async def test_fetch_entity_context_swallows_per_entity_error():
    """Per-entity lookup failure drops that one link but doesn't kill
    the rest."""
    from core_api.services.contradiction_detector import _fetch_entity_context

    mem_id = str(uuid4())
    ent_ok, ent_bad = str(uuid4()), str(uuid4())
    sc = AsyncMock()
    sc.get_entity_links_for_memories = AsyncMock(
        return_value={
            mem_id: [
                {"entity_id": ent_ok, "role": "subject"},
                {"entity_id": ent_bad, "role": "object"},
            ]
        }
    )

    async def get_entity(eid: str) -> dict | None:
        if eid == ent_bad:
            raise RuntimeError("entity-row gone")
        return {"canonical_name": "Fine Entity", "entity_type": "project"}

    sc.get_entity = AsyncMock(side_effect=get_entity)
    out = await _fetch_entity_context(sc, mem_id)
    assert len(out) == 1
    assert out[0]["name"] == "Fine Entity"


# ---------------------------------------------------------------------------
# _llm_entity_aware_contradiction_check — wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_calls_call_with_fallback_with_correct_args():
    """The judge must route through ``call_with_fallback`` with the
    entity-aware service label and the same 10s timeout as Path A's
    judge."""
    from core_api.services.contradiction_detector import (
        _llm_entity_aware_contradiction_check,
    )

    captured: dict = {}

    async def fake_call_with_fallback(**kwargs):
        captured.update(kwargs)
        return (False, 0.90)

    with patch(
        "core_api.services.contradiction_detector.call_with_fallback",
        side_effect=fake_call_with_fallback,
    ):
        result = await _llm_entity_aware_contradiction_check(
            "new content",
            "old content",
            [{"name": "X", "entity_type": "project", "role": "subject"}],
            [{"name": "Y", "entity_type": "project", "role": "subject"}],
            tenant_config=None,
        )

    assert result == (False, 0.90)
    assert captured["service_label"] == "contradiction-entity-aware"
    assert captured["timeout"] == 10.0
    assert captured["model_attr"] == "entity_extraction_model"
    # fake_fn shape: zero-arg callable returning (bool, float).
    fake_val = captured["fake_fn"]()
    assert isinstance(fake_val, tuple) and len(fake_val) == 2
    assert isinstance(fake_val[0], bool) and isinstance(fake_val[1], float)


@pytest.mark.asyncio
async def test_judge_renders_entities_into_prompt_payload():
    """The rendered prompt that reaches ``llm.complete_json`` must
    contain both formatted entity blocks. This is the contract the
    LLM relies on to ground same_subject."""
    from core_api.services.contradiction_detector import (
        _llm_entity_aware_contradiction_check,
    )

    seen_prompt: dict = {}

    class _Recorder:
        async def complete_json(self, prompt: str):
            seen_prompt["text"] = prompt
            return {
                "subject_a": "Helios",
                "subject_b": "Helios",
                "same_subject": True,
                "non_conflict_reason": "none",
                "contradicts": True,
                "reason": "different dates",
            }

    async def fake_call_with_fallback(**kwargs):
        return await kwargs["call_fn"](_Recorder())

    with patch(
        "core_api.services.contradiction_detector.call_with_fallback",
        side_effect=fake_call_with_fallback,
    ):
        verdict, _conf = await _llm_entity_aware_contradiction_check(
            "Project Helios has release date 2027-05-01.",
            "Project Helios has release date 2028-10-15.",
            [
                {
                    "name": "Project Helios",
                    "entity_type": "project",
                    "role": "subject",
                    "entity_id": "ent-helios",
                }
            ],
            [
                {
                    "name": "Project Helios",
                    "entity_type": "project",
                    "role": "subject",
                    "entity_id": "ent-helios",
                }
            ],
        )

    assert verdict is True
    prompt = seen_prompt["text"]
    assert "Project Helios" in prompt
    # Both entity blocks must be present and labelled.
    assert "RESOLVED ENTITIES for Statement A" in prompt
    assert "RESOLVED ENTITIES for Statement B" in prompt
    # The bulleted format from _format_entity_context — CAURA-133 added
    # ``entity_id`` to the rendered bullet (without it, the prompt's
    # mechanical comparison instruction has no data to read).
    assert "(type: project, role: subject, entity_id: ent-helios)" in prompt


@pytest.mark.asyncio
async def test_judge_truncates_long_content_to_500_chars():
    """Mirror ``_llm_contradiction_check``'s 500-char truncation —
    keeps token cost bounded for runaway content.

    The prompt template itself contains literal uppercase characters
    (``Statement A``, ``AUTHORITATIVE``, ``SAME``, ``CRITICAL``,
    etc.), so counting characters in the rendered prompt mixes
    template + content. We compare against a baseline render with
    EMPTY content but identical entities, so the byte-diff equals
    exactly ``len(truncated_new) + len(truncated_old)``."""
    from core_api.services.contradiction_detector import (
        ENTITY_AWARE_CONTRADICTION_PROMPT,
        _format_entity_context,
        _llm_entity_aware_contradiction_check,
    )

    seen_prompt: dict = {}

    class _Recorder:
        async def complete_json(self, prompt: str):
            seen_prompt["text"] = prompt
            return {
                "subject_a": "x",
                "subject_b": "x",
                "same_subject": True,
                "non_conflict_reason": "none",
                "contradicts": False,
                "reason": "ok",
            }

    async def fake_call_with_fallback(**kwargs):
        return await kwargs["call_fn"](_Recorder())

    long = "A" * 5000
    entities = [{"name": "X", "entity_type": "project", "role": "subject"}]
    with patch(
        "core_api.services.contradiction_detector.call_with_fallback",
        side_effect=fake_call_with_fallback,
    ):
        await _llm_entity_aware_contradiction_check(
            long,
            long,
            entities,
            entities,
        )

    entities_block = _format_entity_context(entities)
    baseline = ENTITY_AWARE_CONTRADICTION_PROMPT.format(
        new_content="",
        old_content="",
        new_entities=entities_block,
        old_entities=entities_block,
    )
    content_bytes = len(seen_prompt["text"]) - len(baseline)
    assert content_bytes <= 1000, (
        f"expected ≤500-char truncation per side (≤1000 total); "
        f"got {content_bytes} content bytes"
    )
    # Sanity: truncation actually fired. Without this guard a future
    # template that silently drops the truncation would still pass.
    assert content_bytes < len(long) * 2, (
        f"expected truncation on 5000-char input; got {content_bytes} bytes "
        f"(both inputs passed through untrimmed)"
    )
