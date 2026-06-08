"""A5b — entity-extraction prompt + schema overhaul.

Tests written BEFORE the code changes (red→green discipline). They
FAIL against current main; implementation makes them PASS. See
``memory/fast-vs-strong-investigation.md`` (atomic items 1-5) for
full context.

Five atomic changes pinned by these tests
─────────────────────────────────────────
1. **Extend entity_type vocab**: add ``identifier``, ``artifact``,
   ``role`` to the EXTRACTION_PROMPT vocabulary. Today's 7-type
   enum has nowhere to put ``Vermillion-7``, ``gpt-5.4-nano``, or
   ``PR-WET-2026-05-17-B``, so the LLM returns zero entities for
   identifier-heavy content.

2. **Replace the "Skip generic/common words" rule** with inclusive
   wording. The current phrasing actively suppresses identifier-
   shaped tokens at prompt level — even after #1, the LLM would
   still filter them out.

3. **Pass the full ExtractedGraph schema to the LLM** via a new
   ``response_schema`` kwarg on ``complete_json``. Today's
   ``response_format={"type": "json_object"}`` lets the LLM return
   any JSON shape; missing fields then crash downstream parsers.
   With ``response_format={"type": "json_schema", ...}`` the API
   enforces the shape server-side.

4. **Add optional ``mentions`` list** to ExtractedGraph so the
   extractor can return coreference clusters (Anna / she → same
   cluster_id). Today "Anna" and "She" in the same memory are
   two unlinked extractions; coreference is invisible.

5. **Prompt-level distinction between role and person**. Today the
   prompt lists ``person`` as a type and the LLM forces ``ceo`` /
   ``engineer`` / ``manager`` into ``person`` (nearest enum value),
   then cross-link discovery merges every "ceo" mention into one
   bogus identity. After #5, titles map to ``entity_type=role``.

The implementation that turns these red tests green lives in:
- ``core-api/src/core_api/services/entity_extraction.py``
  (EXTRACTION_PROMPT text, ExtractedGraph + Mention models, _do_extract).
- ``common/llm/providers/openai.py`` (``response_schema`` kwarg in
  ``complete_json`` → OpenAI ``response_format={"type":"json_schema"}``).

The narrow ``LLMProvider`` Protocol in ``common/llm/protocols.py`` is
intentionally left untouched — matches the A5a precedent for the
``seed`` kwarg, which OpenAI accepts but the Protocol does not declare.
Other providers (gemini / vertex / fake) will TypeError on the kwarg
in fallback paths; that is existing tech debt outside A5b's scope.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Change 1 — vocab expansion
# ---------------------------------------------------------------------------


def test_extraction_prompt_includes_identifier_vocab() -> None:
    """The entity_type enum in EXTRACTION_PROMPT must list ``identifier``,
    ``artifact``, and ``role`` alongside the existing 7 types.

    Verification anchor for atomic item #1. Today ``identifier`` and
    ``artifact`` are absent, so the LLM has no slot for content like
    ``Vermillion-7`` or ``gpt-5.4-nano`` and returns 0 entities. ``role``
    is also absent, forcing job titles into ``person`` (see Change 5).
    """
    from core_api.services.entity_extraction import EXTRACTION_PROMPT

    for vocab_term in ("identifier", "artifact", "role"):
        assert vocab_term in EXTRACTION_PROMPT, (
            f"EXTRACTION_PROMPT must list '{vocab_term}' as an entity_type; "
            f"missing terms collapse identifier-heavy content into 0 entities."
        )


# ---------------------------------------------------------------------------
# Change 2 — inclusive extraction wording
# ---------------------------------------------------------------------------


def test_extraction_prompt_does_not_suppress_generic_terms() -> None:
    """The "Skip generic/common words" rule must be removed.

    That phrasing actively filters identifier-shaped content (PR codes,
    model names, version strings) at prompt level. Even with the
    expanded vocab from Change 1, the LLM would still drop these
    because the prompt told it to skip them.
    """
    from core_api.services.entity_extraction import EXTRACTION_PROMPT

    assert "Skip generic" not in EXTRACTION_PROMPT, (
        "The 'Skip generic/common words' instruction suppressed legitimate "
        "identifier/artifact entities in production — must be removed."
    )


def test_extraction_prompt_encourages_inclusive_extraction() -> None:
    """After removing the suppression rule, the prompt must positively
    instruct the LLM to extract all named subjects including identifiers.

    Implementation is free to choose the exact phrasing; this test
    accepts any of several reasonable wordings so it doesn't pin
    cosmetic word choices.
    """
    from core_api.services.entity_extraction import EXTRACTION_PROMPT

    text = EXTRACTION_PROMPT.lower()
    acceptable_phrasings = (
        "every distinct named",
        "every named",
        "extract every",
        "include identifiers",
        "include all identifiers",
        "include product codes",
    )
    assert any(p in text for p in acceptable_phrasings), (
        f"Prompt must positively instruct inclusive extraction; "
        f"none of {acceptable_phrasings} found in prompt."
    )


# ---------------------------------------------------------------------------
# Change 3 — response_schema forwarded to LLM (provider + caller layers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_json_forwards_response_schema_to_openai_client() -> None:
    """``OpenAILLMProvider.complete_json`` must accept a ``response_schema``
    kwarg and translate it to OpenAI's structured-output format
    (``response_format={"type": "json_schema", "json_schema": {...}}``).

    Today complete_json hardcodes ``response_format={"type": "json_object"}``
    — that lets the LLM return any JSON shape, including missing required
    fields. After this change, callers can pin the exact schema and the
    API rejects malformed completions before they reach the parser.
    """
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    schema = {
        "type": "object",
        "properties": {
            "entities": {"type": "array"},
            "relations": {"type": "array"},
        },
        "required": ["entities", "relations"],
    }

    mock_create = AsyncMock(
        return_value=MagicMock(
            choices=[
                MagicMock(message=MagicMock(content='{"entities":[],"relations":[]}'))
            ],
        )
    )
    with patch.object(provider._client.chat.completions, "create", mock_create):
        await provider.complete_json("test", response_schema=schema)

    kwargs = mock_create.call_args.kwargs
    rf = kwargs.get("response_format", {})
    assert rf.get("type") == "json_schema", (
        f"Expected response_format.type='json_schema' when caller passes "
        f"response_schema; got {rf!r}"
    )
    nested = rf.get("json_schema", {})
    nested_schema = nested.get("schema") if isinstance(nested, dict) else None
    assert nested_schema == schema, (
        f"Caller-provided schema must be forwarded verbatim under "
        f"response_format.json_schema.schema; got {nested_schema!r}"
    )


@pytest.mark.asyncio
async def test_complete_json_preserves_json_object_when_no_schema() -> None:
    """Backward compat: callers that don't pass ``response_schema`` must
    still see ``response_format={"type": "json_object"}`` so existing
    non-extraction call sites (enrichment, dedup judge, etc.) are
    untouched.
    """
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    mock_create = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"ok":true}'))],
        )
    )
    with patch.object(provider._client.chat.completions, "create", mock_create):
        await provider.complete_json("test")

    rf = mock_create.call_args.kwargs.get("response_format", {})
    assert rf.get("type") == "json_object", (
        f"Without response_schema, response_format.type must remain "
        f"'json_object' for back-compat; got {rf!r}"
    )


@pytest.mark.asyncio
async def test_extract_passes_response_schema_to_llm() -> None:
    """``_do_extract`` inside ``extract_entities_from_content`` must
    pass a non-None ``response_schema`` describing ExtractedGraph to
    ``complete_json``.

    We don't pin the exact schema dict — implementation may use
    ``ExtractedGraph.model_json_schema()`` or a hand-rolled equivalent.
    We require: (a) non-None value, (b) mentions the ``entities`` field
    name so the LLM at least gets the top-level shape.
    """
    from common.llm.providers.openai import OpenAILLMProvider
    from core_api.services.entity_extraction import extract_entities_from_content

    schemas_seen: list[dict | None] = []

    async def fake_complete_json(self, prompt: str, **kwargs):
        schemas_seen.append(kwargs.get("response_schema"))
        return {"entities": [], "relations": []}

    with (
        patch.object(OpenAILLMProvider, "complete_json", fake_complete_json),
        patch(
            "common.llm.registry.get_llm_provider",
            return_value=OpenAILLMProvider(api_key="sk-test", model="gpt-test"),
        ),
        patch(
            "core_api.services.entity_extraction.settings.entity_extraction_provider",
            "openai",
        ),
    ):
        await extract_entities_from_content("Anna ships Vermillion", "fact")

    assert len(schemas_seen) == 1, f"Expected 1 LLM call, got {len(schemas_seen)}"
    schema = schemas_seen[0]
    assert schema is not None, (
        "Extraction must pass response_schema=ExtractedGraph schema to lock the LLM output shape."
    )
    assert "entities" in str(schema), (
        f"response_schema must describe the ExtractedGraph top-level shape "
        f"(at minimum reference the 'entities' field); got {schema!r}"
    )


# ---------------------------------------------------------------------------
# Change 4 — mentions / cluster_id support on ExtractedGraph
# ---------------------------------------------------------------------------


def test_extracted_graph_parses_mentions_with_cluster_id() -> None:
    """ExtractedGraph must accept an optional ``mentions`` list where each
    mention has ``surface``, ``cluster_id``, and ``entity_canonical``.

    The example "Anna joined. She led the migration." should produce two
    mentions in the same coreference cluster (cluster_id=0) both linked
    to the canonical "anna".
    """
    from core_api.services.entity_extraction import ExtractedGraph

    raw = {
        "entities": [
            {"canonical_name": "anna", "entity_type": "person", "role": "subject"},
        ],
        "relations": [],
        "mentions": [
            {"surface": "Anna", "cluster_id": 0, "entity_canonical": "anna"},
            {"surface": "She", "cluster_id": 0, "entity_canonical": "anna"},
        ],
    }

    graph = ExtractedGraph(**raw)

    assert len(graph.mentions) == 2, (
        f"Expected 2 mentions parsed; got {len(graph.mentions)}"
    )
    surfaces = [m.surface for m in graph.mentions]
    assert surfaces == ["Anna", "She"]
    # Coreference: both share cluster_id 0 → same referent
    assert graph.mentions[0].cluster_id == graph.mentions[1].cluster_id == 0
    # Linked to the same canonical entity
    assert (
        graph.mentions[0].entity_canonical
        == graph.mentions[1].entity_canonical
        == "anna"
    )


def test_extracted_graph_parses_without_mentions_field() -> None:
    """Back-compat: payloads from before A5b (no mentions key) must
    continue to parse cleanly with mentions defaulting to an empty list.

    Existing fake-provider output, persisted-replay fixtures, and the
    fallback regex extractor all produce mentions-less payloads.
    """
    from core_api.services.entity_extraction import ExtractedGraph

    raw = {
        "entities": [
            {"canonical_name": "anna", "entity_type": "person", "role": "subject"},
        ],
        "relations": [],
    }

    graph = ExtractedGraph(**raw)
    assert graph.mentions == [], (
        f"mentions must default to [] when not provided; got {graph.mentions!r}"
    )


# ---------------------------------------------------------------------------
# Change 5 — prompt-level role-vs-person distinction
# ---------------------------------------------------------------------------


def test_extraction_prompt_distinguishes_role_from_person() -> None:
    """The prompt must give explicit guidance that job titles map to
    ``entity_type=role``, not ``person`` — at least one concrete title
    example (ceo / engineer / manager / etc.) referenced near the role
    type.

    Without this signal, even with ``role`` in the vocab (Change 1),
    the LLM defaults to ``person`` for "the CEO" / "an engineer"
    because ``person`` is the nearest enum value semantically.
    """
    from core_api.services.entity_extraction import EXTRACTION_PROMPT

    text = EXTRACTION_PROMPT.lower()
    title_examples = ("ceo", "engineer", "manager", "officer", "director", "title")
    found = [t for t in title_examples if t in text]
    assert found, (
        f"Prompt must include at least one job-title example "
        f"({title_examples}) so the LLM has guidance that titles map to "
        f"entity_type=role rather than entity_type=person."
    )
