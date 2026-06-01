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


# ---------------------------------------------------------------------------
# _format_entity_context — rendering shape
# ---------------------------------------------------------------------------


def test_format_entity_context_renders_bullets():
    from core_api.services.contradiction_detector import _format_entity_context

    out = _format_entity_context(
        [
            {"name": "Project Helios", "entity_type": "project", "role": "subject"},
            {"name": "2027-05-01", "entity_type": "date", "role": "object"},
        ]
    )
    assert '- "Project Helios" (type: project, role: subject)' in out
    assert '- "2027-05-01" (type: date, role: object)' in out


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
            [{"name": "Project Helios", "entity_type": "project", "role": "subject"}],
            [{"name": "Project Helios", "entity_type": "project", "role": "subject"}],
        )

    assert verdict is True
    prompt = seen_prompt["text"]
    assert "Project Helios" in prompt
    # Both entity blocks must be present and labelled.
    assert "RESOLVED ENTITIES for Statement A" in prompt
    assert "RESOLVED ENTITIES for Statement B" in prompt
    # The bulleted format from _format_entity_context.
    assert "(type: project, role: subject)" in prompt


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
