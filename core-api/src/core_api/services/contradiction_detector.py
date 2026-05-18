"""Detect and resolve contradictions between memories on write.

P1 fixes:
- Post-commit async detection (sees all committed data, no concurrency blind spot)
- Correct supersession semantics (new_memory.supersedes_id -> old memory)
- Broader candidate search (threshold 0.70, limit 8)

Multi-provider support:
- Vertex AI, OpenAI, Anthropic, OpenRouter via provider layer
- Automatic fallback chain: configured provider -> fallback -> heuristic
"""

import asyncio
import logging
import time
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.constants import SINGLE_VALUE_PREDICATES
from core_api.providers._retry import call_with_fallback
from core_api.schemas import ContradictionInfo

logger = logging.getLogger(__name__)


def _parse_dt(value) -> datetime | None:
    """Best-effort parse of an ISO datetime string or pass-through datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _candidate_is_older(candidate: dict, new_memory: dict) -> bool:
    """Return True only if ``candidate`` was created strictly before ``new_memory``.

    Enforces the supersession-direction invariant: ``NEW.supersedes_id = OLD.id``
    (CAURA-000). Detection has four call sites, two of them event-driven
    (``MemoryEnriched`` / ``MemoryEmbedded`` consumers), so it can fire on a row
    long after that row was written — by which time newer contradicting rows
    may exist. Without this guard, the detector would happily mark the newer
    candidate as ``outdated`` and write ``older.supersedes_id = newer.id``,
    inverting the chain and producing cycles like ``A → B → A``.

    If either timestamp is missing or unparseable we conservatively return
    False — a missed detection is cheaper than a corrupted chain.
    """
    cand_dt = _parse_dt(candidate.get("created_at"))
    new_dt = _parse_dt(new_memory.get("created_at"))
    if cand_dt is None or new_dt is None:
        return False
    return cand_dt < new_dt


# ---------------------------------------------------------------------------
# Public API: async post-commit entry point (P1-1)
# ---------------------------------------------------------------------------


async def detect_contradictions_async(
    memory_id: UUID,
    tenant_id: str,
    fleet_id: str | None,
    content: str,
    embedding: list[float],
    *,
    new_memory: dict | None = None,
) -> None:
    """Post-commit contradiction detection — runs independently.

    Follows the same fire-and-forget pattern as entity extraction:
    uses the storage client so it can see all committed data (including
    concurrent writes that were invisible in the caller's transaction).

    ``new_memory`` is an optional pass-through for callers that already
    fetched the row (e.g. the CAURA-595 ``handle_memory_enriched``
    consumer). Passing it skips one HTTP GET per call to core-storage-
    api on the async write path. We still re-check ``deleted_at`` here
    so a soft-delete that landed AFTER the caller's fetch but BEFORE
    detection runs cleanly aborts.
    """
    from core_api.services.organization_settings import resolve_config

    # Always-fire completion log (Gap 06): without this, "function ran and
    # found nothing" is indistinguishable from "function never fired" — the
    # exact failure mode that hid Gap 01 and Gap 04 for weeks. Memory id is
    # in the message string itself (rather than ``extra``) so a plain
    # ``grep path_a_completed <memory_id>`` works regardless of the
    # structlog renderer's ``extra={}`` behaviour.
    t_start = time.monotonic()
    n_conflicts = 0
    try:
        if new_memory is None:
            sc = get_storage_client()
            new_memory = await sc.get_memory(str(memory_id))
        if not new_memory or new_memory.get("deleted_at") is not None:
            return

        tenant_config = await resolve_config(None, tenant_id)
        contradictions = await _detect(new_memory, embedding, tenant_config)
        n_conflicts = len(contradictions) if contradictions else 0

        if contradictions:
            logger.info(
                "Async contradiction detection found %d conflict(s) for memory %s",
                len(contradictions),
                memory_id,
            )
    except Exception:
        logger.exception("Async contradiction detection failed for memory %s", memory_id)
    finally:
        elapsed_ms = round((time.monotonic() - t_start) * 1000)
        logger.info(
            "path_a_completed for memory %s n_conflicts=%d elapsed_ms=%d tenant_id=%s",
            memory_id,
            n_conflicts,
            elapsed_ms,
            tenant_id,
        )


# ---------------------------------------------------------------------------
# Synchronous API (kept for direct-call use cases, e.g. tests)
# ---------------------------------------------------------------------------


async def detect_contradictions(
    db: AsyncSession,
    new_memory,
    embedding: list[float],
    tenant_config=None,
) -> list[ContradictionInfo]:
    """In-session contradiction detection (caller manages commit).

    Kept for backward compatibility and testing. For production writes,
    prefer detect_contradictions_async which runs post-commit.

    new_memory can be an ORM Memory object or a dict from the storage client.
    """
    # Normalize to dict if ORM object
    if not isinstance(new_memory, dict):
        new_memory = {
            "id": str(new_memory.id),
            "tenant_id": new_memory.tenant_id,
            "fleet_id": new_memory.fleet_id,
            "content": new_memory.content,
            "subject_entity_id": str(new_memory.subject_entity_id) if new_memory.subject_entity_id else None,
            "predicate": new_memory.predicate,
            "object_value": new_memory.object_value,
            "supersedes_id": str(new_memory.supersedes_id) if new_memory.supersedes_id else None,
            "status": new_memory.status,
        }
    return await _detect(new_memory, embedding, tenant_config)


# ---------------------------------------------------------------------------
# Core detection logic (shared by sync and async paths)
# ---------------------------------------------------------------------------


async def _detect(
    new_memory: dict,
    embedding: list[float],
    tenant_config=None,
) -> list[ContradictionInfo]:
    """Find active memories that contradict the new one.

    Two detection paths:
    1. RDF conflict (single-value predicates only): same subject_entity_id +
       single-value predicate + different object_value -> old memory outdated.
       Multi-value predicates skip this path (additive, not contradictory).
    2. Semantic conflict: high vector similarity, LLM confirms contradiction

    Returns list of contradictions found (may be empty).
    Side-effect: marks contradicted memories as outdated/conflicted and
    sets supersession chain (new_memory.supersedes_id -> old memory).
    """
    sc = get_storage_client()
    contradictions: list[ContradictionInfo] = []

    memory_id = new_memory.get("id")
    subject_entity_id = new_memory.get("subject_entity_id")
    predicate = new_memory.get("predicate")
    object_value = new_memory.get("object_value")
    tenant_id = new_memory.get("tenant_id")
    content = new_memory.get("content", "")
    supersedes_id = new_memory.get("supersedes_id")

    # --- Path 1: RDF triple contradiction (single-value predicates only) ---
    if subject_entity_id and predicate and object_value and predicate.lower() in SINGLE_VALUE_PREDICATES:
        rdf_conflicts = await sc.find_rdf_conflicts(
            tenant_id,
            subject_entity_id,
            predicate,
            exclude_id=str(memory_id),
            # CAURA-123 — scope by fleet so RDF detection respects the
            # same isolation boundary as semantic detection. Without
            # this the storage-api router previously forced
            # ``fleet_id IS NULL`` and the path was unreachable for
            # any fleeted write.
            fleet_id=new_memory.get("fleet_id"),
            # CAURA-123 — pass the new memory's own object_value so
            # the storage layer's ``Memory.object_value != :ov`` filter
            # excludes same-value rows. Otherwise two writes of the
            # same fact trigger a false conflict on themselves.
            object_value=object_value,
        )
        for old in rdf_conflicts:
            if not _candidate_is_older(old, new_memory):
                # Skip: candidate is newer than us. Marking it outdated and
                # writing our supersedes_id at it would invert direction.
                continue
            old_id = old.get("id")
            # Mark old memory as outdated via storage client
            await sc.update_memory_status(str(old_id), "outdated")
            # P1-2: correct supersession -- NEW supersedes OLD
            if not supersedes_id:
                supersedes_id = old_id
                await sc.update_memory_status(
                    str(memory_id),
                    new_memory.get("status", "active"),
                    supersedes_id=str(old_id),
                )
            contradictions.append(
                ContradictionInfo(
                    old_memory_id=old_id,
                    old_status="outdated",
                    reason="rdf_conflict",
                    old_content_preview=old.get("content", "")[:200],
                )
            )
            logger.info(
                "RDF contradiction: memory %s outdated by %s "
                "(subject=%s predicate=%s old_value=%s new_value=%s)",
                old_id,
                memory_id,
                subject_entity_id,
                predicate,
                old.get("object_value"),
                object_value,
            )

    # --- Path 2: Semantic contradiction (vector similarity + batch LLM check) ---
    if not contradictions:
        candidates = await sc.find_similar_candidates(
            {
                "memory_id": str(memory_id),
                "tenant_id": tenant_id,
                "fleet_id": new_memory.get("fleet_id"),
                "embedding": embedding,
                # Scope candidates to the writer's visibility tier — prevents
                # scope_org/scope_agent writes from being marked as superseding
                # scope_team memories (cross-scope chain pollution).
                "visibility": new_memory.get("visibility", "scope_team"),
            }
        )
        if candidates:
            # Fire all LLM checks concurrently instead of serially
            tasks = [
                asyncio.wait_for(
                    _llm_contradiction_check(content, c.get("content", ""), tenant_config),
                    timeout=10.0,
                )
                for c in candidates
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for candidate, result in zip(candidates, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "Contradiction check failed for candidate %s: %s",
                        candidate.get("id"),
                        result,
                    )
                    continue
                if result:
                    if not _candidate_is_older(candidate, new_memory):
                        # Same direction-invariant guard as path 1.
                        continue
                    cand_id = candidate.get("id")
                    # Mark candidate as conflicted via storage client
                    await sc.update_memory_status(str(cand_id), "conflicted")
                    # P1-2: correct supersession -- NEW supersedes OLD
                    if not supersedes_id:
                        supersedes_id = cand_id
                        await sc.update_memory_status(
                            str(memory_id),
                            new_memory.get("status", "active"),
                            supersedes_id=str(cand_id),
                        )
                    contradictions.append(
                        ContradictionInfo(
                            old_memory_id=cand_id,
                            old_status="conflicted",
                            reason="semantic_conflict",
                            old_content_preview=candidate.get("content", "")[:200],
                        )
                    )
                    logger.info(
                        "Semantic contradiction: memory %s conflicted by %s",
                        cand_id,
                        memory_id,
                    )

    return contradictions


CONTRADICTION_PROMPT = """\
You are a contradiction detector for a business memory system.

Two statements contradict ONLY IF they make incompatible claims about the
SAME real-world subject. Different subjects -> NOT a contradiction, even if
the predicates look opposite or the statements look semantically similar.

Statement A (NEW): {new_content}

Statement B (EXISTING): {old_content}

Follow these steps in order:

1. Extract subject_a: the entity Statement A is primarily about
   (person, company, project, product, etc.). Use a short noun phrase.
2. Extract subject_b: the entity Statement B is primarily about.
3. Decide same_subject. Set true ONLY when subject_a and subject_b refer
   to the SAME real-world entity. Treat these as same_subject=true:
     - exact name match
     - known alias / nickname / abbreviation of the same entity
     - role description and proper name referring to the same individual
       in context (e.g., "the CEO" and "Sarah Johnson" when context makes
       it unambiguous)
     - pronoun resolved unambiguously to the other statement's subject
   Treat these as same_subject=false:
     - two different people who share a first name or last name
     - two different companies, products, projects, or teams
     - any case where you are not confident the subjects are the same entity
4. Decide non_conflict_reason. Even when same_subject is true, certain
   shapes describe two claims that BOTH hold and so are NOT a
   contradiction. Pick at most one value; pick "none" when the two
   statements really do assert mutually exclusive states.
     - "temporal_supersession": the statements describe sequential
       states of the same subject's lifecycle (planned -> shipped,
       open -> closed, draft -> published, beta -> GA, hired ->
       promoted). The newer state simply supersedes the older one;
       both were true in sequence.
     - "list_valued_predicate": the two statements describe attributes
       of the same subject that do not compete for a single slot.
       Two shapes both qualify:
       (a) one predicate that naturally holds multiple values at the
       same time — "supports English" / "supports French"; speaks
       multiple languages; "reports_to" in matrix orgs; "works_on"
       parallel projects;
       (b) two entirely different attributes of the same subject —
       e.g., "Alice was promoted to Senior Engineer" (her title)
       and "Alice is on the platform team" (her team) are
       complementary facts; both hold simultaneously. Different
       attributes do not exclude each other.
     - "refinement": one statement is a more specific version of the
       other ("Europe" vs "Munich"; "tech" vs "Google"; "Q3" vs
       "September 15"). Both hold; finer granularity does not negate
       coarser.
     - "scope_mismatch": the statements describe the same subject with
       different implicit qualifiers — whole vs part (parent company
       vs division), different time windows (annual vs quarterly), or
       different context qualifiers (weekday vs weekend, work vs
       residence). Both can hold simultaneously.
     - "same_name_distinct_subject": subject_a and subject_b share a
       surface name but plausibly refer to different real-world
       instances (two different builds of "the nightly build", two
       different days of "today's standup", two different people both
       called "John" without disambiguator). This is the symmetric
       complement to same_subject=false for cases where the names
       happen to match.
     - "conditional_unrealized": one statement is conditional /
       hypothetical / irrealis ("if X then Y", "would", "could",
       "might"), and the other is a realised state. The conditional
       does not assert a claim that can contradict.
     - "event_restatement": the two statements describe the SAME
       event with different tense, aspect, or synonymous verbs
       ("acquired" / "is acquiring" the same deal; "was hired" /
       "joined"). They restate the same fact, not different facts.
     - "none": none of the above applies. Use this when the two
       statements really do make incompatible claims about the same
       subject at the same time frame (e.g., "X lives in Tel Aviv"
       vs "X lives in Haifa" as undated current-state claims).
   Two state claims about the same subject are also NOT a
   contradiction when BOTH statements explicitly reference
   non-overlapping past time periods (e.g., "X lived in Tel Aviv
   from 2010 to 2014" vs "X lived in Haifa from 2015 to 2018").
   In that case set non_conflict_reason="scope_mismatch".
5. Decide contradicts:
   - If same_subject is false, contradicts MUST be false.
   - If non_conflict_reason is not "none", contradicts MUST be false.
   - Otherwise contradicts is true only when the two statements assert
     mutually exclusive states about that subject in the same time
     frame. Updates / corrections about the same subject ARE
     contradictions (e.g., "X lives in Tel Aviv" vs "X lives in
     Haifa"). Do not speculate that one statement might describe a
     future state that resolves the conflict — if it does, choose
     temporal_supersession explicitly.

Reply with ONLY a JSON object, no prose, no markdown fences:
{{"subject_a": "<short noun phrase>",
  "subject_b": "<short noun phrase>",
  "same_subject": true/false,
  "non_conflict_reason": "none|temporal_supersession|list_valued_predicate|refinement|scope_mismatch|same_name_distinct_subject|conditional_unrealized|event_restatement",
  "contradicts": true/false,
  "reason": "one short phrase referencing the subjects and the conflict (or its absence)"}}
"""


# CAURA-124 — within-subject false-positive shapes that hard-gate
# ``contradicts=true`` to ``false``. ``none`` (or absent / unknown
# value) is the only enum value that allows a contradiction to stand.
# Keep this set in sync with the enum listed in ``CONTRADICTION_PROMPT``
# and with the wet-test fixtures in
# ``scripts/wet_test_contradiction_prompt.py``.
NON_CONFLICT_REASONS: frozenset[str] = frozenset(
    {
        "temporal_supersession",
        "list_valued_predicate",
        "refinement",
        "scope_mismatch",
        "same_name_distinct_subject",
        "conditional_unrealized",
        "event_restatement",
    }
)


def _parse_contradiction_response(raw: dict) -> bool:
    """Apply the structured-output safety gates.

    Two gates run, in order:

    1. **Cross-subject gate (CAURA-111).** The prompt requires the model
       to commit to ``same_subject`` before ``contradicts``. If
       ``same_subject`` is false (or missing), ``contradicts`` MUST be
       false regardless of what the model emitted.

    2. **Within-subject FP gate (CAURA-124).** Even when same_subject is
       true, certain shapes describe two claims that both hold and
       must not be flagged. The model classifies the shape into
       ``non_conflict_reason``; any value in ``NON_CONFLICT_REASONS``
       forces ``contradicts=false``. ``"none"`` (or absent / unknown
       value) leaves ``contradicts`` untouched.

    Missing keys and non-boolean values are treated conservatively
    (False for booleans; None for non_conflict_reason, which has the
    same effect as "none" — neither fires Gate 2). ``bool("false")``
    is True in Python, so a model returning the *string* "false"
    instead of the boolean would have silently bypassed the gate —
    both gates use identity-against-True comparisons to avoid that
    trap.
    """
    if not isinstance(raw, dict):
        return False

    same_subject = raw.get("same_subject") is True
    contradicts = raw.get("contradicts") is True

    # Gate 1 — cross-subject (CAURA-111).
    if contradicts and not same_subject:
        logger.warning(
            "Contradiction model returned contradicts=true with same_subject=false; "
            "overriding to false. subject_a=%r subject_b=%r reason=%r",
            raw.get("subject_a"),
            raw.get("subject_b"),
            raw.get("reason"),
        )
        return False

    # Gate 2 — within-subject FP shapes (CAURA-124). Only fires when
    # the model both flagged a contradiction AND named a recognised
    # non-conflict shape — otherwise it's a no-op. Locals are named
    # ``non_conflict_reason`` (not ``reason``) so they don't collide
    # with the model's free-text ``raw["reason"]`` field that the
    # logger calls pass through verbatim.
    raw_ncr = raw.get("non_conflict_reason")
    non_conflict_reason = raw_ncr if isinstance(raw_ncr, str) else None
    if contradicts and non_conflict_reason in NON_CONFLICT_REASONS:
        # WARNING (not INFO): this branch fires only when the model
        # returned an internally inconsistent response — contradicts=true
        # alongside a recognised non_conflict_reason, which the prompt
        # explicitly forbids in step 5. Same severity as Gate 1's
        # cross-subject override so model regressions surface at the
        # standard WARNING level rather than getting buried in INFO.
        logger.warning(
            "Contradiction model returned contradicts=true with "
            "non_conflict_reason=%r; overriding to false. "
            "subject_a=%r subject_b=%r reason=%r",
            non_conflict_reason,
            raw.get("subject_a"),
            raw.get("subject_b"),
            raw.get("reason"),
        )
        return False

    # Both gates passed (or contradicts is already False) — return the
    # model's verdict.
    return contradicts


# ---------------------------------------------------------------------------
# Multi-provider LLM contradiction check with fallback chain
# ---------------------------------------------------------------------------


async def _llm_contradiction_check(
    new_content: str,
    old_content: str,
    tenant_config=None,
) -> bool:
    """Ask the LLM whether two texts contradict each other.

    Uses the standard 3-tier fallback chain:
    1. Try the configured provider (with retry)
    2. Try the configured fallback provider (via resolve_fallback)
    3. Fall back to negation-word heuristic

    Note: the previous implementation tried ALL providers with valid
    credentials before falling back to the heuristic. This was replaced
    with the standard single-fallback pattern for consistency across
    services. The heuristic fallback (step 3) is now reachable, which
    it previously was not due to the FakeLLMProvider short-circuit bug.
    """
    provider_name = (
        tenant_config.entity_extraction_provider if tenant_config else settings.entity_extraction_provider
    )

    prompt = CONTRADICTION_PROMPT.format(new_content=new_content[:500], old_content=old_content[:500])

    async def _do_check(llm) -> bool:
        raw = await llm.complete_json(prompt)
        return _parse_contradiction_response(raw)

    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do_check,
        fake_fn=lambda: _fake_contradiction_check(new_content, old_content),
        tenant_config=tenant_config,
        service_label="contradiction",
        model_attr="entity_extraction_model",
        timeout=10.0,
    )


def _fake_contradiction_check(new_content: str, old_content: str) -> bool:
    """Simple heuristic for testing: flag if negation words differ."""
    negations = {
        "not",
        "no",
        "never",
        "none",
        "isn't",
        "wasn't",
        "doesn't",
        "can't",
        "won't",
    }
    new_words = set(new_content.lower().split())
    old_words = set(old_content.lower().split())
    new_has_neg = bool(new_words & negations)
    old_has_neg = bool(old_words & negations)
    # If one has negation and the other doesn't, and they share significant overlap
    if new_has_neg != old_has_neg:
        shared = new_words & old_words - negations
        if len(shared) >= 3:
            return True
    return False


# ---------------------------------------------------------------------------
# Entity-based contradiction detection (post entity extraction)
# ---------------------------------------------------------------------------


async def detect_contradictions_by_entities_async(
    memory_id: UUID,
    tenant_id: str,
    fleet_id: str | None,
) -> None:
    """Post-entity-extraction contradiction detection using shared entities.

    Runs after entity extraction completes, so MemoryEntityLink rows exist.
    Finds memories that share entities with the new memory and checks for
    contradictions via LLM -- catches by-the-way updates that embedding
    similarity misses.
    """
    from core_api.services.organization_settings import resolve_config

    # Always-fire completion log (Gap 06) — see ``detect_contradictions_async``
    # above for the rationale. Same memory-id-in-message convention.
    t_start = time.monotonic()
    n_candidates = 0
    n_conflicts = 0
    try:
        sc = get_storage_client()
        new_memory = await sc.get_memory(str(memory_id))
        if not new_memory or new_memory.get("deleted_at") is not None:
            return

        tenant_config = await resolve_config(None, tenant_id)
        candidates = await sc.find_entity_overlap_candidates(
            {
                "memory_id": str(memory_id),
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                # Same visibility scoping as the semantic path above.
                "visibility": new_memory.get("visibility", "scope_team"),
            }
        )
        n_candidates = len(candidates) if candidates else 0
        if not candidates:
            return

        new_content = new_memory.get("content", "")
        tasks = [
            asyncio.wait_for(
                _llm_contradiction_check(new_content, c.get("content", ""), tenant_config),
                timeout=10.0,
            )
            for c in candidates
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        found = False
        for candidate, result in zip(candidates, results):
            if isinstance(result, Exception):
                logger.warning(
                    "Entity contradiction check failed for candidate %s: %s",
                    candidate.get("id"),
                    result,
                )
                continue
            if result:
                if not _candidate_is_older(candidate, new_memory):
                    # Same direction-invariant guard as paths 1 and 2.
                    continue
                cand_id = candidate.get("id")
                await sc.update_memory_status(str(cand_id), "conflicted")
                if not found:
                    # First match is most relevant (ordered by shared entity
                    # count DESC). Entity-based detection is more precise than
                    # embedding-based, so overwrite supersedes_id.
                    await sc.update_memory_status(
                        str(memory_id),
                        new_memory.get("status", "active"),
                        supersedes_id=str(cand_id),
                    )
                found = True
                n_conflicts += 1
                logger.info(
                    "Entity-based contradiction: %s conflicted by %s",
                    cand_id,
                    memory_id,
                )
    except Exception:
        logger.exception("Entity-based contradiction detection failed for %s", memory_id)
    finally:
        elapsed_ms = round((time.monotonic() - t_start) * 1000)
        logger.info(
            "path_c_completed for memory %s n_candidates=%d n_conflicts=%d elapsed_ms=%d tenant_id=%s",
            memory_id,
            n_candidates,
            n_conflicts,
            elapsed_ms,
            tenant_id,
        )


# Backward-compat re-exports for tests
from core_api.providers._credentials import has_credentials as _has_api_key  # noqa: F401
from core_api.providers._credentials import (
    resolve_openai_compatible as _resolve_openai_compatible,  # noqa: F401
)
