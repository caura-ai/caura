"""A5a — two surgical fixes for entity-extraction reliability.

These tests are written BEFORE the code changes so they cannot be
biased toward whatever the implementation happens to do. They will
FAIL against current main (red). Implementation makes them PASS (green).

Changes pinned by these tests
─────────────────────────────
1. **OpenAI `complete_json` accepts and forwards a `seed=` kwarg**
   (``common/llm/providers/openai.py``). Addresses A5 symptom #2 —
   non-determinism across retries. ``temperature=0.0`` alone is not
   sufficient on gpt-class small models without a seed.

2. **`upsert_entity` preserves the first-seen canonical name** when
   a longer alternative arrives via embedding-similarity resolution
   (``core_api/services/entity_service.py``). The current "longer
   name = canonical" rule actively promotes hallucinated suffixes
   (A5 symptom #3 — e.g., LLM hallucinates ``globex industries`` for
   content saying only ``Globex``, and the canonical is permanently
   overwritten). The alternative surface form is still tracked via
   the ``_aliases`` list, so it remains searchable; it just stops
   being the canonical.

Both changes are surgical (~2-line diffs each). The matching code
changes land in the same PR after these tests are red.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Change 1 — seed forwarding through OpenAILLMProvider.complete_json
# ---------------------------------------------------------------------------


async def test_complete_json_forwards_seed_when_provided() -> None:
    """When the caller passes ``seed=42`` to ``complete_json``, that exact
    value must reach the OpenAI client's ``chat.completions.create`` call.

    Today: ``complete_json`` does not accept a ``seed`` kwarg — calling
    with ``seed=42`` raises ``TypeError: got unexpected keyword``. After
    the fix: signature gains ``seed: int | None = None`` and the value
    is included in the ``create(...)`` kwargs only when not None.
    """
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")

    # Mock the SDK call surface so no network IO happens.
    mock_create = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"ok": true}'))],
        )
    )
    with patch.object(provider._client.chat.completions, "create", mock_create):
        await provider.complete_json("test prompt", seed=42)

    assert mock_create.call_count == 1
    kwargs = mock_create.call_args.kwargs
    assert kwargs.get("seed") == 42, (
        f"Expected seed=42 forwarded to OpenAI client, got kwargs={list(kwargs.keys())}"
    )


async def test_complete_json_omits_seed_when_none() -> None:
    """Backward compatibility: callers that don't pass a seed should not
    see a ``seed`` kwarg sent to the OpenAI client (passing ``seed=None``
    to the OpenAI SDK is accepted but redundant; we keep the call shape
    clean to match today's behaviour for un-seeded calls)."""
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")

    mock_create = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"ok": true}'))],
        )
    )
    with patch.object(provider._client.chat.completions, "create", mock_create):
        await provider.complete_json("test prompt")

    kwargs = mock_create.call_args.kwargs
    # Either seed not present, or explicitly None — both acceptable. The
    # important thing is no ACCIDENTAL seed leak from a previous call.
    assert kwargs.get("seed") is None, (
        f"Expected seed=None or absent when caller didn't supply one; got seed={kwargs.get('seed')!r}"
    )


async def test_extract_entities_uses_stable_seed() -> None:
    """The entity-extraction wrapper must pass a stable seed to the LLM so
    the same content produces the same entity set across retries.

    After the fix, ``_do_extract`` inside ``extract_entities_from_content``
    derives a deterministic seed from the prompt (e.g., hash) and passes
    it to ``complete_json``. Two calls with identical content → identical
    ``seed=`` argument seen by the provider.

    We don't pin the EXACT seed value (implementation may choose
    ``hash(prompt) & 0xFFFFFFFF`` or a fixed constant — either works);
    we only require *stability* and *non-None*.
    """
    from common.llm.providers.openai import OpenAILLMProvider
    from core_api.services.entity_extraction import extract_entities_from_content

    seeds_seen: list[int | None] = []

    async def fake_complete_json(self, prompt: str, **kwargs):
        seeds_seen.append(kwargs.get("seed"))
        return {"entities": [], "relations": []}

    # Patch the provider's complete_json so we observe the seed kwarg
    # without doing real network IO.
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
        await extract_entities_from_content("Anna ships Vermillion to Pelagic.", "fact")
        await extract_entities_from_content("Anna ships Vermillion to Pelagic.", "fact")

    assert len(seeds_seen) == 2, f"Expected 2 LLM calls, got {len(seeds_seen)}"
    assert seeds_seen[0] is not None, (
        "Extraction must pass a non-None seed for determinism"
    )
    assert seeds_seen[0] == seeds_seen[1], (
        f"Same content must produce the same seed across calls; "
        f"got {seeds_seen[0]} != {seeds_seen[1]}"
    )


# ---------------------------------------------------------------------------
# Change 2 — upsert_entity preserves first-seen canonical name
# ---------------------------------------------------------------------------


async def _make_entity_upsert(canonical_name: str, entity_type: str = "organization"):
    """Convenience builder matching today's EntityUpsert shape."""
    from core_api.schemas import EntityUpsert

    return EntityUpsert(
        tenant_id="t-a5",
        fleet_id="f-a5",
        entity_type=entity_type,
        canonical_name=canonical_name,
    )


async def test_upsert_preserves_canonical_when_longer_alternative_arrives() -> None:
    """The hallucinated-suffix case (A5 symptom #3).

    Setup: an entity ``globex`` already exists (created by an earlier
    upsert). The LLM later returns ``globex industries`` for content
    that only mentions ``Globex``. Phase 2 embedding similarity links
    the two (cosine ≥ 0.85). The canonical_name of the stored entity
    must REMAIN ``globex``; ``globex industries`` is recorded as an
    alias only.

    Pre-fix (current main): canonical is promoted to ``globex industries``
    because of the ``if len(new) > len(existing)`` rule, producing
    permanent damage to the entity row.

    Post-fix: first-seen wins. Alias list contains both forms.
    """
    from core_api.services.entity_service import upsert_entity

    existing_entity = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "t-a5",
        "fleet_id": "f-a5",
        "entity_type": "organization",
        "canonical_name": "globex",
        "attributes": {},
    }

    sc = MagicMock()
    sc.find_exact_entity = AsyncMock(return_value=None)
    sc.find_by_embedding_similarity = AsyncMock(
        return_value=[{**existing_entity, "similarity": 0.95}]
    )
    sc.update_entity = AsyncMock(
        side_effect=lambda eid, data: {**existing_entity, **data}
    )
    sc.create_entity = AsyncMock()

    with patch("core_api.services.entity_service.get_storage_client", return_value=sc):
        result = await upsert_entity(
            await _make_entity_upsert("globex industries"),
            name_embedding=[0.1] * 1536,
        )

    sc.update_entity.assert_called_once()
    _, update_data = sc.update_entity.call_args.args
    assert update_data["canonical_name"] == "globex", (
        f"First-seen canonical must be preserved; the longer 'globex industries' "
        f"was promoted, producing {update_data['canonical_name']!r}"
    )
    assert result.canonical_name == "globex"

    # Alias list must include both surface forms so the longer one
    # remains searchable / discoverable.
    aliases = set(update_data["attributes"].get("_aliases", []))
    assert {"globex", "globex industries"}.issubset(aliases), (
        f"Both surface forms must be tracked as aliases; got {aliases}"
    )


async def test_upsert_preserves_canonical_when_shorter_alternative_arrives() -> None:
    """Symmetric sanity check. If existing canonical is ``globex industries``
    and a shorter ``globex`` arrives later, canonical must also stay as
    the first-seen value (``globex industries``).

    Today the longer-wins rule happens to do the right thing here by
    coincidence (existing wins because it's longer). After the fix, the
    behaviour is principled (first-seen wins) rather than accidental.
    """
    from core_api.services.entity_service import upsert_entity

    existing_entity = {
        "id": "22222222-2222-2222-2222-222222222222",
        "tenant_id": "t-a5",
        "fleet_id": "f-a5",
        "entity_type": "organization",
        "canonical_name": "globex industries",
        "attributes": {},
    }

    sc = MagicMock()
    sc.find_exact_entity = AsyncMock(return_value=None)
    sc.find_by_embedding_similarity = AsyncMock(
        return_value=[{**existing_entity, "similarity": 0.95}]
    )
    sc.update_entity = AsyncMock(
        side_effect=lambda eid, data: {**existing_entity, **data}
    )
    sc.create_entity = AsyncMock()

    with patch("core_api.services.entity_service.get_storage_client", return_value=sc):
        await upsert_entity(
            await _make_entity_upsert("globex"),
            name_embedding=[0.1] * 1536,
        )

    _, update_data = sc.update_entity.call_args.args
    assert update_data["canonical_name"] == "globex industries", (
        "Symmetric case: first-seen 'globex industries' must remain canonical"
    )
