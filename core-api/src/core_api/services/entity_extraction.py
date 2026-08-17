"""Entity/relation extraction from memory content."""

import logging
import re
import zlib

from pydantic import BaseModel, ValidationError

from core_api.config import settings
from core_api.protocols import LLMProvider
from core_api.providers._retry import call_with_fallback

logger = logging.getLogger(__name__)


class ExtractionShapeError(ValueError):
    """Provider output could not yield a usable graph.

    Subclasses ``ValueError`` so ``call_with_retry``'s broad ``except`` still
    routes it to the fallback chain unchanged, while giving the pending #788
    follow-up — classifying shape failures as non-retryable in
    ``common/llm/retry.py`` — one narrow type to mark rather than a bare
    ``ValueError`` that shared infra cannot safely single out.

    Deliberately NOT ``common.llm.providers.ProviderResponseShapeError``, which
    is a transport-layer error: its contract is ``(provider, content,
    parsed_type)`` and it STORES and renders ``content`` (1 KiB) in ``__str__``.
    Passing this payload through it would put memory content, and any PII in
    it, into every traceback that error reaches. This condition is a domain
    one — the response parsed fine, it just held nothing usable — so it carries
    diagnostic counts only and never the payload.
    """


EXTRACTION_PROMPT = """\
Extract named entities, their relations, and surface-form mentions from the following memory content.

Rules:
- canonical_name: lowercase, no articles (the, a, an)
- entity_type: one of person, organization, technology, project, concept, location, event, identifier, artifact, role
- role: one of subject, object, mentioned
- relation_type: short verb phrase like works_on, uses, belongs_to, created_by, depends_on, manages, located_in
- Extract every distinct named subject. Include identifiers (PR-2025-A, build-734), product codes (Vermillion-7), model names (gpt-5.4-nano), and version strings as entity_type=identifier or entity_type=artifact when they refer to a specific named thing.
- Job titles (ceo, engineer, manager, director, officer) classify as entity_type=role — NOT person. Use entity_type=person only when a named individual is referenced (e.g., "Anna Bergstrom"). "the CEO" alone is a role; "Anna, the CEO" is one person entity plus one role entity.
- mentions: list every surface form referring to an entity in the content, including pronouns. Assign coreferring mentions the same cluster_id integer (0, 1, 2, ...) — cluster_id is never null. Set entity_canonical to the referenced entity, or null if unresolved.
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
    # Nullable because nothing enforces the schema ``_do_extract`` sends (see
    # there): while this was a bare ``int``, one null cluster_id failed the
    # WHOLE ``ExtractedGraph``, burning the primary provider's retry budget and
    # dropping through to the regex heuristic (prod, 2026-08-16). ``None``
    # means "no coreference cluster assigned" — the surface form is still worth
    # keeping. Nothing reads ``cluster_id`` today; it stays for the coreference
    # work A5b (#169) added it for.
    cluster_id: int | None = None
    entity_canonical: str | None = None


class ExtractedGraph(BaseModel):
    entities: list[ExtractedEntity] = []
    relations: list[ExtractedRelation] = []
    mentions: list[Mention] = []


# Item model per ``ExtractedGraph`` list field. A field missing from here is
# simply never walked — it defaults to empty and nothing complains — so the
# coverage is pinned by a test rather than by this comment
# (``test_registry_covers_every_graph_list_field``).
_GRAPH_ITEM_MODELS: dict[str, type[BaseModel]] = {
    "entities": ExtractedEntity,
    "relations": ExtractedRelation,
    "mentions": Mention,
}


def _parse_graph_lenient(raw: dict) -> tuple[ExtractedGraph, dict[str, int]]:
    """Build an ``ExtractedGraph``, dropping malformed items instead of failing.

    ``ExtractedGraph(**raw)`` validates the whole payload atomically, so ONE
    bad item discarded every good one alongside it. That is not hypothetical:
    a null ``cluster_id`` on mention 10 of 15 took out the entire extraction in
    prod on 2026-08-16 (#788), and every remaining non-optional field here —
    ``canonical_name``, ``entity_type``, ``role``, ``from_entity``,
    ``relation_type``, ``to_entity``, ``surface`` — carries the identical
    exposure. Fixing them one nullable field at a time is whack-a-mole; the
    parse boundary is where the blast radius is set.

    Mirrors the ``atomic_facts`` loop in
    ``common/enrichment/service.py::_validate_enrichment``, which walks that
    provider output item-by-item and ``continue``s past entries it cannot use.
    Note the mirror is only of the LIST walk: that function's outer
    ``EnrichmentResult(**raw)`` is still atomic, so enrichment carries this same
    exposure one level up.

    A relation whose endpoint entity was dropped is not a dangling reference —
    ``entity_extraction_worker`` only writes an edge when both ids resolve, the
    same cascade it already documents for blocklisted nodes.

    Returns the graph plus a per-field count of what was dropped. The caller
    owns the logging so this stays a pure function.

    NOTE it deliberately does NOT swallow a total loss — see
    ``_do_extract`` for why an all-items-dropped payload still raises.
    """
    dropped: dict[str, int] = {}
    fields: dict[str, list] = {}
    for field, model in _GRAPH_ITEM_MODELS.items():
        items = raw.get(field)
        if items is None:
            continue
        if not isinstance(items, list):
            # A non-list where a list belongs is a shape error for the whole
            # field, not a per-item one; treat it as "nothing usable here"
            # rather than guessing at a coercion. Counted as 1 because there
            # are no items to count.
            dropped[field] = 1
            continue
        kept: list[BaseModel] = []
        for item in items:
            try:
                # ``model_validate`` rather than ``model(**item)``: it raises
                # ValidationError for a non-mapping item too, so one narrow
                # except covers junk of every shape. ``**`` on a str/int/None
                # would raise TypeError and force a broader catch that would
                # also swallow unrelated errors.
                kept.append(model.model_validate(item))
            except ValidationError:
                dropped[field] = dropped.get(field, 0) + 1
        fields[field] = kept
    return ExtractedGraph(**fields), dropped


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


# A33 (mechanism ①): the extractor inconsistently splits a trailing
# disambiguator off its subject — "Acme Corp #0033's …" becomes subject
# "acme corp" + a SEPARATE identifier "#0033" (role=mentioned) — so every
# "<Name> #NNNN" collapses onto one bare "<Name>" subject entity. With
# same_subject = (entity_id == entity_id) (CAURA-133) that shared id is a
# guaranteed false contradiction, and the bare hub also dilutes entity_lookup
# (A30). Re-fold a trailing disambiguator (a "#tag" or a "(qualifier)") back
# into its subject when the content shows the two adjacent. Only "#"/paren
# shapes qualify, so real named identifiers ("pr-2025-a", "build-734") are
# left untouched; the content-adjacency gate avoids folding an unrelated tag.
_DISCRIMINATOR_RE = re.compile(r"#\w[\w.\-]*|\([^)]+\)")


def _reattach_subject_discriminators(graph: ExtractedGraph, content: str) -> ExtractedGraph:
    """Fold a split-off trailing disambiguator back into its subject (A33 ①)."""
    subjects = [e for e in graph.entities if e.role == "subject"]
    if not subjects:
        return graph
    content_l = content.lower()
    renames: dict[str, str] = {}  # old canonical -> new, for relation/mention remap
    folded: set[str] = set()  # canonical names of discriminators merged away
    for e in graph.entities:
        if e.role == "subject":
            continue
        disc = e.canonical_name.strip()
        if not _DISCRIMINATOR_RE.fullmatch(disc.lower()):
            continue
        for s in subjects:
            base = s.canonical_name.strip()
            if base and f"{base.lower()} {disc.lower()}" in content_l:
                new_name = f"{base} {disc}"
                renames[s.canonical_name] = new_name
                s.canonical_name = new_name
                folded.add(e.canonical_name)
                break
    if not folded:
        return graph
    graph.entities = [e for e in graph.entities if not (e.role != "subject" and e.canonical_name in folded)]
    for r in graph.relations:
        r.from_entity = renames.get(r.from_entity, r.from_entity)
        r.to_entity = renames.get(r.to_entity, r.to_entity)
    for m in graph.mentions:
        if m.entity_canonical in renames:
            m.entity_canonical = renames[m.entity_canonical]
    return graph


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
        # A5b #3 — declare the expected shape via response_schema. It is a
        # HINT, not the server-side guarantee A5b originally claimed: the
        # OpenAI provider sends it with ``strict=False`` (see
        # ``common/llm/providers/openai.py``) and gemini/vertex accept and
        # ignore it outright, so the provider can and does return values the
        # schema forbids. ``_parse_graph_lenient`` below is the only real
        # enforcement, and it enforces PER ITEM — malformed entries are dropped
        # and counted rather than failing the payload. See
        # ``Mention.cluster_id`` for the prod failure that established this.
        raw = await llm.complete_json(
            prompt,
            seed=seed,
            response_schema=ExtractedGraph.model_json_schema(),
        )
        if not isinstance(raw, dict):
            # Not a per-item problem — there is no payload to salvage items
            # from. Raise so the fallback chain gets its turn.
            raise ExtractionShapeError(f"entity extraction returned {type(raw).__name__}, expected dict")
        graph, dropped = _parse_graph_lenient(raw)
        if dropped:
            # Counts only, never the items: the payload is memory content plus
            # any PII in it, and this is a WARNING bound for log storage. Flat
            # key=value with a stable leading slug so a plain grep finds it
            # regardless of how the structlog renderer handles ``extra`` —
            # same shape as ``contradiction_detector``'s skip logs.
            #
            # ``kept`` is read off the parsed graph, NOT re-derived from
            # ``raw``: a non-list field has no ``len()``, and computing it here
            # would raise INSIDE the salvage branch, discarding the very graph
            # this function just rescued.
            logger.warning(
                "entity_extraction_items_dropped dropped=%s kept=%s",
                dropped,
                {f: len(getattr(graph, f)) for f in dropped},
            )
            # A total loss is NOT salvage: returning an empty graph would
            # silently assert "no entities in this content", indistinguishable
            # from a legitimately entity-free memory, and rob the fallback
            # chain of its turn — the alternative provider is a different model
            # and may well parse.
            #
            # Keyed on entities/relations and an empty ``entities``, NOT on all
            # three lists. ``mentions`` has no consumer that persists anything,
            # so a surviving mention must not suppress the fallback (the worker
            # early-returns on an entity-less graph, losing the extraction with
            # no audit row), and a malformed mention alone must not trigger it
            # (that would let the regex fallback invent entities the LLM
            # correctly reported as absent).
            if (dropped.keys() & {"entities", "relations"}) and not graph.entities:
                raise ExtractionShapeError(
                    f"entity extraction produced no usable entities (dropped={dropped})"
                )
        return graph

    extraction_model = (
        (getattr(tenant_config, "entity_extraction_model", None) if tenant_config else None)
        or settings.entity_extraction_model
        or None
    )
    graph = await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do_extract,
        fake_fn=lambda: _fake_extract(content),
        tenant_config=tenant_config,
        service_label="entity-extraction",
        model_override=extraction_model,
        model_attr="entity_extraction_model",
    )
    # A33 ①: undo the split-discriminator pattern before resolution.
    return _reattach_subject_discriminators(graph, content)


# Backward-compat re-exports for tests
from core_api.providers._retry import call_with_retry as _call_extract_with_retry  # noqa: F401
