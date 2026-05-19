"""Entity/relation extraction from memory content."""

import logging
import re
import zlib

from pydantic import BaseModel

from core_api.config import settings
from core_api.protocols import LLMProvider
from core_api.providers._retry import call_with_fallback

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """\
Extract named entities, their relations, and surface-form mentions from the following memory content.

Rules:
- canonical_name: lowercase, no articles (the, a, an)
- entity_type: one of person, organization, technology, project, concept, location, event, identifier, artifact, role
- role: one of subject, object, mentioned
- relation_type: short verb phrase like works_on, uses, belongs_to, created_by, depends_on, manages, located_in
- Extract every distinct named subject. Include identifiers (PR-2025-A, build-734), product codes (Vermillion-7), model names (gpt-5.4-nano), and version strings as entity_type=identifier or entity_type=artifact when they refer to a specific named thing.
- Job titles (ceo, engineer, manager, director, officer) classify as entity_type=role — NOT person. Use entity_type=person only when a named individual is referenced (e.g., "Anna Bergstrom"). "the CEO" alone is a role; "Anna, the CEO" is one person entity plus one role entity.
- mentions: list every surface form referring to an entity in the content, including pronouns. Assign coreferring mentions the same cluster_id integer (0, 1, 2, ...). Link each mention to its entity_canonical when known; use null for unresolved pronouns.
- If no entities found, return empty lists

Return ONLY valid JSON matching this schema (no markdown fences):
{{
  "entities": [{{"canonical_name": "...", "entity_type": "...", "role": "..."}}],
  "relations": [{{"from_entity": "...", "relation_type": "...", "to_entity": "..."}}],
  "mentions": [{{"surface": "...", "cluster_id": 0, "entity_canonical": "..."}}]
}}

Memory type: {memory_type}
Content:
{content}
"""


class ExtractedEntity(BaseModel):
    canonical_name: str
    entity_type: str
    role: str


class ExtractedRelation(BaseModel):
    from_entity: str
    relation_type: str
    to_entity: str


class Mention(BaseModel):
    surface: str
    cluster_id: int
    entity_canonical: str | None = None


class ExtractedGraph(BaseModel):
    entities: list[ExtractedEntity] = []
    relations: list[ExtractedRelation] = []
    mentions: list[Mention] = []


def _fake_extract(content: str) -> ExtractedGraph:
    """Regex-based: extract capitalized multi-word phrases as person entities."""
    pattern = r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b"
    matches = list(set(re.findall(pattern, content)))
    entities = [
        ExtractedEntity(
            canonical_name=m.lower(),
            entity_type="person",
            role="mentioned",
        )
        for m in matches
    ]
    return ExtractedGraph(entities=entities, relations=[])


async def extract_entities_from_content(
    content: str,
    memory_type: str,
    tenant_config=None,
) -> ExtractedGraph:
    """Extract entities from content with retry + fallback chain.

    Fallback chain:
      1. Configured provider (with retry)
      2. Alternative LLM provider (with retry) — if API key available
      3. Regex heuristic (_fake_extract) — always succeeds

    Never raises; always returns an ExtractedGraph.
    """
    if tenant_config:
        provider_name = tenant_config.entity_extraction_provider
    else:
        provider_name = settings.entity_extraction_provider

    if provider_name == "fake":
        return _fake_extract(content)
    if provider_name == "none":
        return ExtractedGraph(entities=[], relations=[])

    async def _do_extract(llm: LLMProvider) -> ExtractedGraph:
        prompt = EXTRACTION_PROMPT.format(memory_type=memory_type, content=content)
        # Stable seed per prompt (A5 #2): without it, gpt-5.4-nano returns
        # different entity sets / types across retries on identical content
        # (e.g., "helios-9" → 'technology' on one call, 'project' on the
        # next). CRC32 of the encoded prompt gives a deterministic 32-bit
        # integer that survives process restarts — unlike ``hash()`` which
        # is salted per-process for str inputs.
        seed = zlib.crc32(prompt.encode("utf-8"))
        # A5b #3 — pin the output shape server-side via response_schema.
        # ExtractedGraph.model_json_schema() encodes the entities /
        # relations / mentions structure. Providers that don't support
        # structured outputs ignore this kwarg.
        raw = await llm.complete_json(
            prompt,
            seed=seed,
            response_schema=ExtractedGraph.model_json_schema(),
        )
        return ExtractedGraph(**raw)

    extraction_model = (
        (getattr(tenant_config, "entity_extraction_model", None) if tenant_config else None)
        or settings.entity_extraction_model
        or None
    )
    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do_extract,
        fake_fn=lambda: _fake_extract(content),
        tenant_config=tenant_config,
        service_label="entity-extraction",
        model_override=extraction_model,
        model_attr="entity_extraction_model",
    )


# Backward-compat re-exports for tests
from core_api.providers._retry import call_with_retry as _call_extract_with_retry  # noqa: F401
