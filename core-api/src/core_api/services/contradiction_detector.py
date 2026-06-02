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

from core_api.cache import cache_set_nx
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.constants import SINGLE_VALUE_PREDICATES
from core_api.providers._retry import call_with_fallback
from core_api.schemas import ContradictionInfo
from core_api.services.subject_preflight import _subjects_differ_with_certainty

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# A4 #14 — back-channel idempotency for back-to-back detection invocations.
# ---------------------------------------------------------------------------

# Lock TTL. Long enough for any detection to complete (worst case
# ~10s per candidate; ~80s for a typical batch); short enough that
# a process crash holding the lock doesn't permanently block
# re-detection for the same memory.
_DETECTION_LOCK_TTL_SECONDS = 3600


async def _acquire_path_a_lock(memory_id) -> bool:
    """Try to acquire the Path A (semantic + RDF) idempotency lock for
    ``memory_id``. Returns True iff this caller owns the lock and should
    proceed; False if another caller already holds it and we should skip.

    Lock is keyed per-path so Path A and Path C run independently for
    the same memory.
    """
    return await cache_set_nx(f"contradiction:path_a:{memory_id}", "1", _DETECTION_LOCK_TTL_SECONDS)


async def _acquire_path_c_lock(memory_id) -> bool:
    """Try to acquire the Path C (entity-overlap) idempotency lock for
    ``memory_id``. Returns True iff this caller owns the lock; False
    means another caller already ran detection for this memory."""
    return await cache_set_nx(f"contradiction:path_c:{memory_id}", "1", _DETECTION_LOCK_TTL_SECONDS)


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


def _pick_older(a: dict, b: dict) -> dict:
    """Return whichever of ``a`` or ``b`` was created first.

    Used at *attribution* time to keep the supersession chain pointing
    newer→older regardless of which row carried the "new memory"
    framing at detection time. CAURA-125 (audit gap A6) — prior to
    this helper, the detector used a strict
    ``candidate.created_at < new_memory.created_at`` filter to GATE
    detection itself, causing the verdict to depend on which row
    happened to be written first. That's now split: detection is
    symmetric (both shapes get checked), and direction is decided
    here after a conflict is confirmed.

    Tiebreaker rules, in order:
      1. Both ``created_at`` parseable and different → strictly older
         timestamp wins.
      2. Tied or unparseable timestamp → smaller-by-string UUID wins.
         Deterministic across replays and across UUID versions (v4/v6/
         v7); we don't depend on UUIDs being time-ordered.

    The caller is responsible for using the returned identity to
    decide which row to mark ``outdated`` / ``conflicted`` and where
    to point the ``supersedes_id`` edge.
    """
    a_dt = _parse_dt(a.get("created_at"))
    b_dt = _parse_dt(b.get("created_at"))
    if a_dt is not None and b_dt is not None and a_dt != b_dt:
        return a if a_dt < b_dt else b
    # Tied or unparseable — fall back to UUID string ordering.
    a_id = str(a.get("id") or "")
    b_id = str(b.get("id") or "")
    if a_id < b_id:
        return a
    if b_id < a_id:
        return b
    # Truly equal (same row twice, or both ids missing/None).
    # Return ``a`` by convention; callers should not pass the same
    # row on both sides in production, but tests do for invariants.
    return a


def _merge_status_update(acc: dict[str, dict], row: dict) -> None:
    """Merge a per-row update into the accumulator keyed by ``memory_id``.

    The contradiction-detection loops can append two writes for the same
    ``memory_id`` in a mixed canonical/flipped run — one bare
    ``{status: ...}`` from the top-of-loop "older → outdated" branch and
    one ``{status: ..., supersedes_id: ...}`` from the inner attribution
    branch. The old serial code relied on those two writes landing in
    iteration order and the second one's ``supersedes_id`` taking
    effect; the new batched code passes both rows in one
    ``batch_update_status`` payload, where last-write-wins semantics
    inside the endpoint are an implementation detail, not part of the
    contract (a future ``UPDATE FROM VALUES`` optimisation would make
    order undefined). Dedupe per ``memory_id`` here so the wire payload
    carries one merged row per memory and the storage endpoint can be
    optimised freely without changing the resulting DB state.

    Later writes override earlier writes on a per-field basis (plain
    ``dict.update``) — preserving today's last-write-wins semantics
    on the fields the inner branch sets (``status``, ``supersedes_id``)
    while keeping any earlier fields that the later write doesn't touch.
    This matches the existing ``TestMixedDirectionStateGuard`` invariant
    in ``test_contradiction_direction_invariance.py``: both the
    canonical iteration's ``supersedes_id`` chain link AND the flipped
    iteration's status update must land for the same ``memory_id``.
    """
    mid = row["memory_id"]
    if mid in acc:
        acc[mid].update(row)
    else:
        acc[mid] = dict(row)


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
    skipped = False
    try:
        # A4 #14 — back-channel idempotency. Both the ENRICHED and
        # EMBEDDED handlers fire ``detect_contradictions_async`` for
        # the same memory; whichever arrives first owns the lock and
        # runs detection, the other skips. Fail-open: if Redis is
        # unavailable, ``_acquire_path_a_lock`` returns True and we
        # fall back to the prior double-detection behaviour (storage
        # CAS still keeps writes idempotent).
        if not await _acquire_path_a_lock(memory_id):
            skipped = True
            return

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
            "path_a_completed for memory %s n_conflicts=%d skipped=%s elapsed_ms=%d tenant_id=%s",
            memory_id,
            n_conflicts,
            str(skipped).lower(),
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
        # CAURA-125 — state-corruption guard. When ``rdf_conflicts``
        # mixes older and newer candidates relative to ``new_memory``,
        # an earlier flipped iteration already marked ``new_memory``
        # outdated; a later canonical iteration must NOT then re-write
        # ``new_memory`` back to its previous status ("active") while
        # setting ``supersedes_id``.
        new_memory_is_outdated = False
        # Collapsed-write accumulator — folded in per audit P2 even
        # though the RDF loop isn't gather-prefaced; same N+1 shape and
        # keeps the file consistent with semantic / Path C. Keyed by
        # ``memory_id`` so a mixed canonical/flipped run that touches
        # ``new_memory`` twice collapses into one merged row (see
        # ``_merge_status_update`` for the ordering rationale).
        rdf_updates: dict[str, dict] = {}
        for old in rdf_conflicts:
            # CAURA-125 — decide attribution direction AFTER confirming
            # the conflict, not before. ``_pick_older`` chooses which
            # row carries ``outdated`` status; the other carries the
            # supersedes_id edge pointing at the older row. This makes
            # the verdict symmetric under candidate vs. new_memory swap
            # while preserving the chain's newer→older direction.
            older = _pick_older(old, new_memory)
            older_is_new = str(older.get("id")) == str(memory_id)
            newer = new_memory if not older_is_new else old
            older_id = older.get("id")
            newer_id = newer.get("id")

            _merge_status_update(rdf_updates, {"memory_id": str(older_id), "status": "outdated"})
            if newer is new_memory:
                # Canonical case (candidate is older). Track via local
                # ``supersedes_id`` so multiple conflict candidates in
                # this run don't each issue a write; storage's CAS
                # ``WHERE supersedes_id IS NULL`` would only honour the
                # first anyway.
                if not supersedes_id:
                    supersedes_id = older_id
                    # Separate the status-reversion guard from the
                    # chain edge. When a prior flipped iteration has
                    # already marked ``new_memory`` ``"outdated"``,
                    # the canonical iteration must still wire
                    # ``new_memory.supersedes_id`` to ``older_id`` —
                    # otherwise the older canonical candidate is left
                    # orphaned (outdated but unreachable via the
                    # chain). Using ``"outdated"`` as the target
                    # status here is idempotent with the flipped
                    # iteration's earlier write.
                    target_status = (
                        "outdated" if new_memory_is_outdated else new_memory.get("status", "active")
                    )
                    _merge_status_update(
                        rdf_updates,
                        {
                            "memory_id": str(memory_id),
                            "status": target_status,
                            "supersedes_id": str(older_id),
                        },
                    )
            else:
                # Flipped case (candidate is newer). The just-written
                # memory is the older row and is now ``outdated``; the
                # pre-existing candidate carries supersedes_id pointing
                # back at new_memory.
                new_memory_is_outdated = True
                # Application-level guard against overwriting an
                # existing supersedes_id on the candidate. Storage CAS
                # (``WHERE supersedes_id IS NULL``) is the
                # last-line-of-defence; this guard logs an explicit
                # warning so the orphaning attempt is visible in logs
                # rather than silently no-op'd at the DB.
                if newer.get("supersedes_id"):
                    logger.warning(
                        "Flipped contradiction skipped supersedes_id overwrite "
                        "for candidate %s (already supersedes %s)",
                        newer_id,
                        newer.get("supersedes_id"),
                    )
                else:
                    _merge_status_update(
                        rdf_updates,
                        {
                            "memory_id": str(newer_id),
                            "status": newer.get("status", "active"),
                            "supersedes_id": str(older_id),
                        },
                    )

            # ``ContradictionInfo.old_memory_id`` is documented as the
            # pre-existing candidate. Always populate from ``old``
            # (the candidate row from storage), never from ``older``
            # — those diverge in the flipped case.
            direction = "canonical" if newer is new_memory else "flipped"
            contradictions.append(
                ContradictionInfo(
                    old_memory_id=old.get("id"),
                    # In canonical, the candidate is the row we just
                    # marked outdated. In flipped, the candidate's
                    # status is unchanged — surface its actual current
                    # state rather than a misleading "outdated".
                    old_status="outdated" if direction == "canonical" else old.get("status", "active"),
                    reason="rdf_conflict",
                    old_content_preview=old.get("content", "")[:200],
                    direction=direction,
                )
            )
            logger.info(
                "RDF contradiction: memory %s outdated by %s "
                "(subject=%s predicate=%s old_value=%s new_value=%s direction=%s)",
                older_id,
                newer_id,
                subject_entity_id,
                predicate,
                older.get("object_value"),
                newer.get("object_value"),
                direction,
            )

        if rdf_updates:
            rdf_result = await sc.batch_update_status({"updates": list(rdf_updates.values())})
            if rdf_result.get("skipped"):
                # ``skipped`` carries rows the storage-side dropped — CAS
                # gate fail (caller-supplied ``expected_supersedes_id``
                # mismatch) or row already deleted. Pre-batch, the single-
                # row PATCH route surfaced 404 as a hard error; the batch
                # route returns the list instead so we don't abort the
                # whole detection cycle. Log so the dropped writes are
                # visible in tracing — the contradiction detector itself
                # doesn't use ``expected_supersedes_id`` today, so a
                # non-empty list usually means the target row was
                # soft-deleted between detect-and-flush.
                logger.warning(
                    "batch_update_status (RDF path) skipped %d row(s) (trigger memory %s): %s",
                    len(rdf_result["skipped"]),
                    memory_id,
                    rdf_result["skipped"],
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

            # CAURA-125 — state-corruption guard, same rationale as the
            # RDF path above.
            new_memory_is_outdated = False
            # Collect per-row status updates and flush them with one
            # ``batch_update_status`` HTTP after the loop. The prior shape
            # (one ``update_memory_status`` per row) issued up to 3K
            # writes per detection cycle — same wire effect, ~Kx the
            # round-trips (audit P2). All branches that set status here
            # share one batch; the post-loop call is a no-op when
            # ``updates`` is empty. Keyed by ``memory_id`` — see
            # ``_merge_status_update`` for the dedupe rationale.
            updates: dict[str, dict] = {}
            for candidate, result in zip(candidates, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "Contradiction check failed for candidate %s: %s",
                        candidate.get("id"),
                        result,
                    )
                    continue
                # A4 #12 — judge now returns (verdict, confidence).
                # Path A continues to gate only on verdict at this site;
                # A4 #13 will introduce confidence-weighted vetoes on
                # Path C's else-branch.
                verdict, _confidence = result  # type: ignore[misc]
                if verdict:
                    # CAURA-125 — symmetric attribution; see RDF path
                    # above for the rationale.
                    older = _pick_older(candidate, new_memory)
                    older_is_new = str(older.get("id")) == str(memory_id)
                    newer = new_memory if not older_is_new else candidate
                    older_id = older.get("id")
                    newer_id = newer.get("id")

                    _merge_status_update(updates, {"memory_id": str(older_id), "status": "conflicted"})
                    if newer is new_memory:
                        if not supersedes_id:
                            supersedes_id = older_id
                            # See RDF path above for the rationale of
                            # separating the status-reversion guard
                            # from the chain edge. Semantic-path
                            # status literal is ``"conflicted"`` (not
                            # ``"outdated"``), matching what the
                            # flipped iteration would have just set.
                            target_status = (
                                "conflicted" if new_memory_is_outdated else new_memory.get("status", "active")
                            )
                            _merge_status_update(
                                updates,
                                {
                                    "memory_id": str(memory_id),
                                    "status": target_status,
                                    "supersedes_id": str(older_id),
                                },
                            )
                    else:
                        new_memory_is_outdated = True
                        # Application-level guard; see RDF flipped
                        # branch for rationale.
                        if newer.get("supersedes_id"):
                            logger.warning(
                                "Flipped contradiction skipped supersedes_id overwrite "
                                "for candidate %s (already supersedes %s)",
                                newer_id,
                                newer.get("supersedes_id"),
                            )
                        else:
                            _merge_status_update(
                                updates,
                                {
                                    "memory_id": str(newer_id),
                                    "status": newer.get("status", "active"),
                                    "supersedes_id": str(older_id),
                                },
                            )

                    direction = "canonical" if newer is new_memory else "flipped"
                    contradictions.append(
                        ContradictionInfo(
                            old_memory_id=candidate.get("id"),
                            old_status="conflicted"
                            if direction == "canonical"
                            else candidate.get("status", "active"),
                            reason="semantic_conflict",
                            old_content_preview=candidate.get("content", "")[:200],
                            direction=direction,
                        )
                    )
                    logger.info(
                        "Semantic contradiction: memory %s conflicted by %s direction=%s",
                        older_id,
                        newer_id,
                        direction,
                    )

            if updates:
                sem_result = await sc.batch_update_status({"updates": list(updates.values())})
                if sem_result.get("skipped"):
                    # See RDF path above for the ``skipped`` semantics.
                    logger.warning(
                        "batch_update_status (semantic path) skipped %d row(s) (trigger memory %s): %s",
                        len(sem_result["skipped"]),
                        memory_id,
                        sem_result["skipped"],
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


# CAURA-129 — entity-aware retraction prompt. Path C's retraction judge
# runs AFTER entity extraction has resolved the canonical entities for
# both memories. This prompt is the "real fix" tier — it asks a
# structurally different question than ``CONTRADICTION_PROMPT`` (which
# Path A's semantic judge already used) by surfacing the resolved
# entity rows authoritatively. Disagreement with Path A now carries
# meaningful signal (the model is reasoning over RESOLVED entities,
# not over the raw text NER it ran once already).
#
# JSON output schema is intentionally identical to ``CONTRADICTION_PROMPT``
# so ``_judge_contradiction`` parses both unchanged. The new placeholders
# are ``{new_entities}`` and ``{old_entities}`` — rendered by
# ``_format_entity_context``.
ENTITY_AWARE_CONTRADICTION_PROMPT = """\
You are a contradiction detector for a business memory system, running
the second-pass judgement after entity resolution has already linked
both statements to their canonical entities.

Two statements contradict ONLY IF they make incompatible claims about
the SAME real-world subject. Different subjects -> NOT a contradiction,
even if the predicates look opposite or the statements look semantically
similar.

CRITICAL: Resolved entities below are AUTHORITATIVE. If the subjects in
the two RESOLVED-ENTITIES blocks are different entity rows (different
canonical names or different entity IDs), same_subject MUST be false
regardless of how similar the raw statement text looks. If the subjects
are the SAME canonical entity row, treat same_subject as true even if
the statement text refers to it by alias / role / pronoun.

Statement A (NEW): {new_content}
RESOLVED ENTITIES for Statement A:
{new_entities}

Statement B (EXISTING): {old_content}
RESOLVED ENTITIES for Statement B:
{old_entities}

Follow these steps in order:

1. Identify subject_a: from Statement A's resolved entities, the entity
   with role="subject" (or the canonical subject if no role is marked).
   Use its canonical name.
2. Identify subject_b: same from Statement B's resolved entities.
3. Decide same_subject. Set true ONLY when subject_a and subject_b are
   the same resolved entity (same canonical name AND same entity_type).
   Set false when:
     - the resolved subjects are different entity rows
     - either side has no resolved subject (degenerate input — this
       prompt should not have been invoked, but if it is, prefer false)
     - the canonical names match but entity_types differ (two
       different real-world things that happen to share a name)
4. Decide non_conflict_reason. Even when same_subject is true, certain
   shapes describe two claims that BOTH hold and so are NOT a
   contradiction. Pick at most one value; pick "none" when the two
   statements really do assert mutually exclusive states.
     - "temporal_supersession": sequential states of the same subject's
       lifecycle (planned -> shipped, open -> closed, draft ->
       published, beta -> GA, hired -> promoted). The newer state
       supersedes the older one; both held in sequence.
     - "list_valued_predicate": attributes that do not compete for a
       single slot — multi-value predicates (supports English /
       supports French) or two entirely different attributes of the
       same subject (Alice's title vs Alice's team).
     - "refinement": one statement is a more specific version of the
       other (Europe vs Munich; Q3 vs September 15). Both hold.
     - "scope_mismatch": same subject, different implicit qualifiers
       (whole vs part, different time windows, different contexts).
     - "same_name_distinct_subject": surface names match but the
       resolved entities differ — choose this when you also set
       same_subject=false because the entity blocks disambiguate.
     - "conditional_unrealized": one statement is hypothetical /
       irrealis; conditionals do not contradict realised states.
     - "event_restatement": the two statements describe the SAME event
       with different tense / aspect / synonyms.
     - "none": none of the above; the two statements assert mutually
       exclusive states about the same subject at the same time frame.
5. Decide contradicts:
   - If same_subject is false, contradicts MUST be false.
   - If non_conflict_reason is not "none", contradicts MUST be false.
   - Otherwise contradicts is true only when the two statements assert
     mutually exclusive states about that subject in the same time
     frame. Corrections / updates about the same subject ARE
     contradictions.

Reply with ONLY a JSON object, no prose, no markdown fences:
{{"subject_a": "<canonical name from RESOLVED ENTITIES for A>",
  "subject_b": "<canonical name from RESOLVED ENTITIES for B>",
  "same_subject": true/false,
  "non_conflict_reason": "none|temporal_supersession|list_valued_predicate|refinement|scope_mismatch|same_name_distinct_subject|conditional_unrealized|event_restatement",
  "contradicts": true/false,
  "reason": "one short phrase referencing the resolved subjects and the conflict (or its absence)"}}
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
# A4 #12 — Confidence-scored judge wrapper around the bool parser.
# ---------------------------------------------------------------------------


# Confidence rubric (see ``tests/test_a4_12_contradiction_judge_confidence.py``
# for the per-branch pin):
#   0.90 — Clean LLM agreement (both gates aligned with the verdict).
#   0.85 — Gate 2 fired (model itself named a non-conflict pattern,
#          parser overrode contradicts=true to false).
#   0.60 — Gate 1 fired (model's same_subject contradicted its
#          contradicts=true; parser overrode to false).
#   0.50 — Malformed / unparseable response (parser conservative-default).
_CONF_CLEAN = 0.90
_CONF_GATE2 = 0.85
_CONF_GATE1 = 0.60
_CONF_FALLBACK = 0.50


def _judge_contradiction(raw) -> tuple[bool, float]:
    """A4 #12 — wrap ``_parse_contradiction_response`` with a confidence
    score derived from the model's own coherence.

    Callers (A4 #13 retraction's confidence-weighted veto, A1 #16
    dedup danger-zone judge) use the score to decide whether the
    final verdict is trustworthy enough to act on. Verdict semantics
    are identical to ``_parse_contradiction_response``; this is purely
    additive on top.
    """
    if not isinstance(raw, dict) or not raw:
        return False, _CONF_FALLBACK

    verdict = _parse_contradiction_response(raw)
    same_subject = raw.get("same_subject") is True
    model_contradicts = raw.get("contradicts") is True
    raw_ncr = raw.get("non_conflict_reason")
    non_conflict_reason = raw_ncr if isinstance(raw_ncr, str) else None

    if model_contradicts and not same_subject:
        # Gate 1 fired — model said contradicts=true but its own
        # same_subject said false. Internal inconsistency → lower trust.
        return verdict, _CONF_GATE1
    if model_contradicts and non_conflict_reason in NON_CONFLICT_REASONS:
        # Gate 2 fired — model recognised a non-conflict pattern AND
        # said contradicts=true. The pattern-recognition signal is
        # what we trust — high confidence in NOT-a-contradiction.
        return verdict, _CONF_GATE2
    # Either contradicts=False with consistent ancillary fields, or
    # contradicts=True with both gates aligned. Clean case.
    return verdict, _CONF_CLEAN


# ---------------------------------------------------------------------------
# Multi-provider LLM contradiction check with fallback chain
# ---------------------------------------------------------------------------


async def _llm_contradiction_check(
    new_content: str,
    old_content: str,
    tenant_config=None,
) -> tuple[bool, float]:
    """Ask the LLM whether two texts contradict each other.

    Returns ``(verdict, confidence)`` — see ``_judge_contradiction`` for
    the rubric. A4 #12 widened this from ``bool`` so downstream
    consumers (A4 #13 retraction, A1 #16 dedup judge) can gate
    decisions on the model's coherence.

    Uses the standard 3-tier fallback chain:
    1. Try the configured provider (with retry)
    2. Try the configured fallback provider (via resolve_fallback)
    3. Fall back to negation-word heuristic
    """
    provider_name = (
        tenant_config.entity_extraction_provider if tenant_config else settings.entity_extraction_provider
    )

    prompt = CONTRADICTION_PROMPT.format(new_content=new_content[:500], old_content=old_content[:500])

    async def _do_check(llm) -> tuple[bool, float]:
        raw = await llm.complete_json(prompt)
        return _judge_contradiction(raw)

    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do_check,
        fake_fn=lambda: (_fake_contradiction_check(new_content, old_content), _CONF_FALLBACK),
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
# CAURA-129 — Entity-aware retraction judge.
# ---------------------------------------------------------------------------
#
# Path C runs AFTER ``process_entity_extraction`` has resolved the
# canonical entities and written ``MemoryEntityLink`` rows. The
# retraction judge below leverages that — it fetches resolved entity
# names for both memories and asks ``ENTITY_AWARE_CONTRADICTION_PROMPT``
# which authoritatively grounds same_subject on entity identity rather
# than on raw-text NER (Path A's semantic judge already did that, so
# re-asking the same question gave us LLM-stochastic flips —
# CAURA-128). Shape mirrors ``_llm_contradiction_check`` so the
# retraction call site is a one-line swap.


_ENTITY_CONTEXT_MAX_ENTITIES = 10
_ENTITY_CONTEXT_NAME_MAX_CHARS = 100

# CAURA-130 (L3.4) — upper bound on the fall-through set size before
# the entity-links subject preflight starts fetching. Each candidate
# costs one parallel storage round-trip; ``find_entity_overlap_candidates``
# can theoretically return many rows for a high-fanout subject (popular
# entity referenced by hundreds of memories), so cap the fan-out before
# it becomes a thundering-herd risk on the storage API. Above the cap
# we fail open (skip the L3.4 stage, let the LLM judge decide) rather
# than drop everything — the legacy A1 #17 gate has already done what
# it can.
_ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES = 20


def _format_entity_context(entities: list[dict]) -> str:
    """Render resolved entity rows into a readable block for the prompt.

    ``entities`` is a list of dicts shaped ``{name, entity_type, role}``
    (also accepts ``canonical_name`` as a primary alias for ``name``).
    Returns one bullet per entity, e.g.

        - "Project Helios" (type: project, role: subject)
        - "2027-05-01" (type: date, role: object)

    Bounds (mirror the ``[:500]`` content truncation in
    ``_llm_entity_aware_contradiction_check`` — keep prompt token
    cost bounded against runaway / adversarial inputs):

      * At most ``_ENTITY_CONTEXT_MAX_ENTITIES`` (10) bullets rendered;
        excess entities are dropped silently. The judge's signal
        saturates well before 10 entities per memory; in practice we
        see 1-5.
      * Each ``name`` is truncated to
        ``_ENTITY_CONTEXT_NAME_MAX_CHARS`` (100) characters. Canonical
        names are short by construction (the entity extractor produces
        ≤30-char names typically); 100 chars covers the long tail
        without giving an attacker a runaway lever.

    Returns the literal string ``"(none resolved)"`` when ``entities``
    is empty — defensive; callers SHOULD have guarded earlier, but if
    they didn't the prompt still reads as well-formed.
    """
    if not entities:
        return "(none resolved)"
    capped = entities[:_ENTITY_CONTEXT_MAX_ENTITIES]
    lines: list[str] = []
    for e in capped:
        name = (e.get("canonical_name") or e.get("name") or "<unknown>")[:_ENTITY_CONTEXT_NAME_MAX_CHARS]
        etype = e.get("entity_type") or "<unknown>"
        role = e.get("role") or "<unspecified>"
        lines.append(f'- "{name}" (type: {etype}, role: {role})')
    return "\n".join(lines)


def _extract_subject_canonical_identity(
    entities: list[dict],
) -> tuple[str, str, str] | None:
    """CAURA-130 (L3.4) — extract the canonical subject identity from a
    list of resolved entity rows.

    Returns ``(canonical_name, entity_type, entity_id)`` of the FIRST
    entity with ``role == "subject"``, or ``None`` if no subject-role
    entity is present (degenerate / object-only link sets, empty
    lists, malformed data). Used by the forward-Path-C preflight to
    catch first-name collisions when ``subject_entity_id`` is NULL on
    either memory (the A1 #17 legacy gate can't decide those cases —
    see the inline TODO at the preflight site for the original
    ``priya``-collision write-up).

    Identity is keyed on ``entity_id`` ONLY — two ``priya`` rows with
    the same canonical name but different entity rows ARE distinct
    subjects (that's the whole point). The canonical name + type are
    returned for logging / debugging, not for equality.
    """
    if not entities:
        return None
    for e in entities:
        if (e.get("role") or "").lower() == "subject":
            name = e.get("canonical_name") or e.get("name") or "<unknown>"
            etype = e.get("entity_type") or "<unknown>"
            entity_id = e.get("entity_id") or e.get("id")
            if not entity_id:
                # No identity to key on — caller should treat as
                # "unknown subject" rather than asserting mismatch.
                continue
            return (str(name), str(etype), str(entity_id))
    return None


async def _fetch_entity_context(sc, memory_id: str) -> list[dict]:
    """Fetch and denormalise ``MemoryEntityLink`` rows for a single memory.

    Two storage round-trips at worst:
      1. ``get_entity_links_for_memories([memory_id])`` — batch endpoint;
         returns ``{memory_id: [{entity_id, role}, ...]}``.
      2. ``get_entity(entity_id)`` per link, fan-out via ``asyncio.gather``
         — Path C is post-commit async so the round-trip parallelism is
         latency-invisible to the write path.

    Returns ``[]`` (not ``None``) when the memory has no resolved links;
    the caller treats empty as the skip-retraction signal so this
    function never raises on missing data.
    """
    try:
        links_by_mem = await sc.get_entity_links_for_memories([memory_id])
    except Exception as e:
        logger.warning(
            "Path C entity-context fetch failed (links) for memory %s: %s",
            memory_id,
            e,
        )
        return []
    links = (links_by_mem or {}).get(memory_id, []) if isinstance(links_by_mem, dict) else []
    if not links:
        return []

    async def _hydrate(link: dict) -> dict | None:
        entity_id = link.get("entity_id")
        if not entity_id:
            return None
        try:
            entity = await sc.get_entity(str(entity_id))
        except Exception as e:
            logger.warning(
                "Path C entity-context fetch failed (entity %s) for memory %s: %s",
                entity_id,
                memory_id,
                e,
            )
            return None
        if not entity:
            return None
        # ``canonical_name`` is the column on Entity; fall back to
        # ``name`` for any future schema change / mocked-test data.
        # ``entity_id`` is preserved on the normalised shape (CAURA-
        # 130 L3.4 — the forward-Path-C preflight uses it as the
        # canonical-subject identity key).
        return {
            "name": entity.get("canonical_name") or entity.get("name"),
            "entity_type": entity.get("entity_type"),
            "role": link.get("role"),
            "entity_id": str(entity_id),
        }

    hydrated = await asyncio.gather(*(_hydrate(link) for link in links))
    return [h for h in hydrated if h is not None]


async def _llm_entity_aware_contradiction_check(
    new_content: str,
    old_content: str,
    new_entities: list[dict],
    old_entities: list[dict],
    tenant_config=None,
) -> tuple[bool, float]:
    """Entity-aware variant of ``_llm_contradiction_check``.

    Same return shape, same fallback chain, same ``_judge_contradiction``
    parser; differs only in the prompt template (which receives resolved
    entity context as ``{new_entities}`` / ``{old_entities}``). Callers
    MUST have verified both ``new_entities`` and ``old_entities`` are
    non-empty before invoking — see ``_attempt_path_c_retraction`` for
    the guard.
    """
    provider_name = (
        tenant_config.entity_extraction_provider if tenant_config else settings.entity_extraction_provider
    )

    prompt = ENTITY_AWARE_CONTRADICTION_PROMPT.format(
        new_content=new_content[:500],
        old_content=old_content[:500],
        new_entities=_format_entity_context(new_entities),
        old_entities=_format_entity_context(old_entities),
    )

    async def _do_check(llm) -> tuple[bool, float]:
        raw = await llm.complete_json(prompt)
        return _judge_contradiction(raw)

    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do_check,
        fake_fn=lambda: (_fake_contradiction_check(new_content, old_content), _CONF_FALLBACK),
        tenant_config=tenant_config,
        service_label="contradiction-entity-aware",
        model_attr="entity_extraction_model",
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# A4 #13 — Retraction phase: re-judge a Path A verdict with full entity
# context and undo it via the A4 #10 storage primitive when the re-judge
# disagrees with sufficient confidence.
# ---------------------------------------------------------------------------


# Minimum confidence the judge must report for a ``verdict=False`` to
# trigger retraction. Above this we trust the "not a contradiction"
# call; below this we leave Path A's verdict in place.
#
# CAURA-128 — tightened from 0.60 → 0.90.
#
# The retraction code was originally written as "re-judge with full
# entity context"; the comment block at A4 #13's introduction promised
# the judge would see the resolved entity_links and answer a different,
# entity-aware question than Path A's semantic similarity check. In
# practice ``_attempt_path_c_retraction`` calls ``_llm_contradiction_check
# (new_content, old_content, ...)`` with the SAME prompt and SAME inputs
# as Path A's semantic judge. There is no entity context in the request
# — it is the same LLM call rolled twice.
#
# Wet-tested on memclaw.net 2026-05-26 (S2 race probe, scripts/
# repro_contradictions_race.py). Two memories with directly conflicting
# release dates about a synthetic proper-noun subject. Path A correctly
# flagged the conflict; Path C's independent roll returned
# ``(verdict=False, confidence=0.60)`` on a non-trivial fraction of runs
# and silently retracted the correct flag. Confidence rubric:
#   0.90 — clean LLM agreement (both gates aligned on "not contradict")
#   0.85 — gate 2 fired (model named a non_conflict_reason)
#   0.60 — gate 1 fired (model said contradicts=True same_subject=False;
#          parser overrode). THIS IS THE STOCHASTIC FLIP CASE.
#   0.50 — malformed / heuristic fallback
#
# Raising the floor to 0.90 means retraction only fires on clean
# agreement — both gates of the parser say "not a contradiction" with
# no parser-override. Gate-1 (the stochastic-flip case) and gate-2
# (single-gate non_conflict_reason) both now leave Path A's verdict in
# place. This is the "quick fix" tier; the deeper fix (an entity-aware
# prompt that actually receives entity_links + canonical names) is
# tracked separately and will revisit this threshold once the judge has
# a different question to answer.
RETRACTION_CONFIDENCE_THRESHOLD = _CONF_CLEAN


async def _attempt_path_c_retraction(
    sc,
    new_memory: dict,
    tenant_config,
) -> bool:
    """Re-judge whatever candidate Path A retracted; undo if the judge
    disagrees with sufficient confidence. Returns True iff a retraction
    was performed.

    Lookup is a direct dereference of ``new_memory.supersedes_id`` —
    bypasses A4 #11's ``include_supersedes`` filter (which is structurally
    inverted relative to Path A's chain shape — see follow-up). Works
    in both canonical and flipped Path A directions: the dereferenced
    row IS the conflicted candidate in either case.

    Retraction is a two-step write via A4 #10:
      1. ``update_memory_status(candidate.id, "active")`` — revert
         the conflicted row. Idempotent if a concurrent writer beat
         us to it.
      2. ``update_memory_status(new_memory.id, status,
         unset_supersedes=True, expected_supersedes_id=candidate.id)``
         — clear the chain edge with a CAS anchor. A 409 from the
         storage layer means someone else mutated the chain between
         our read and our write; we treat that as "the retraction is
         no longer ours to do" and swallow it.
    """
    # CAURA-130 (L3.8) — per-tenant retraction kill-switch. Ops escape
    # valve: flip ``retraction_enabled`` to False on a misbehaving
    # tenant to leave Path A's verdict in place unconditionally,
    # without a deploy. Default ON (no behavior change for existing
    # tenants — the resolver returns True when the JSONB key is
    # absent / None). Check before any other work so the early exit
    # is also cheap.
    if tenant_config is not None and not getattr(tenant_config, "retraction_enabled", True):
        logger.info(
            "Path C retraction skipped — disabled by tenant config for memory %s",
            new_memory.get("id"),
        )
        return False

    retraction_target_id = new_memory.get("supersedes_id")
    if not retraction_target_id:
        return False

    candidate = await sc.get_memory(str(retraction_target_id))
    if not candidate or candidate.get("deleted_at") is not None:
        return False
    # Only retract rows still in the conflicted state Path A produced.
    # If the row has already been moved on by another writer (or by
    # a previous Path C invocation), our retraction is no longer
    # meaningful — skip without calling the judge.
    if candidate.get("status") != "conflicted":
        return False

    new_content = new_memory.get("content", "") or ""
    old_content = candidate.get("content", "") or ""

    # CAURA-129 — fetch resolved entity context for BOTH memories. If
    # either side has no resolved entities, the entity-aware judge has
    # nothing to ground same_subject on, and we'd degenerate to the
    # CAURA-128 pre-fix state (same prompt + same inputs as Path A,
    # stochastically flipping). Empty on either side → skip retraction;
    # Path A's verdict stands. This is correct for the common case (the
    # entity-extraction worker that *enqueued* Path C populates the
    # links by definition), and conservative for the edge case
    # (degenerate inputs / extractor failure).
    # Wrap the fetch in ``asyncio.wait_for`` so a hung storage
    # round-trip cannot block Path C indefinitely (mirrors the LLM
    # call's cancellation boundary below). 5s budget is ample for two
    # parallel storage calls (~50-200ms p95 in practice); on failure
    # (timeout, network, storage error), treat as "no context, leave
    # Path A alone" rather than retrying.
    try:
        new_entities, old_entities = await asyncio.wait_for(
            asyncio.gather(
                _fetch_entity_context(sc, str(new_memory.get("id"))),
                _fetch_entity_context(sc, str(candidate.get("id"))),
            ),
            timeout=5.0,
        )
    except Exception as e:
        logger.warning(
            "Path C entity-context fetch failed for memory %s candidate %s: %s. Path A's verdict stands.",
            new_memory.get("id"),
            candidate.get("id"),
            e,
        )
        return False

    if not new_entities or not old_entities:
        logger.info(
            "Path C retraction skipped — empty entity context for "
            "memory %s (new_n=%d cand_n=%d). Path A's verdict stands.",
            new_memory.get("id"),
            len(new_entities),
            len(old_entities),
        )
        return False

    try:
        verdict, confidence = await asyncio.wait_for(
            _llm_entity_aware_contradiction_check(
                new_content, old_content, new_entities, old_entities, tenant_config
            ),
            timeout=10.0,
        )
    except (TimeoutError, Exception) as e:
        logger.warning(
            "Path C retraction judge failed for memory %s candidate %s: %s",
            new_memory.get("id"),
            candidate.get("id"),
            e,
        )
        return False

    if verdict:
        # Judge agrees with Path A — real contradiction, leave it.
        return False
    if confidence < RETRACTION_CONFIDENCE_THRESHOLD:
        # Below the CAURA-128 floor (0.90). Covers gate-1 (0.60, the
        # stochastic-flip case where parser overrode ``contradicts=True
        # same_subject=False`` to False), gate-2 (0.85, single-gate
        # ``non_conflict_reason``), and the heuristic / malformed
        # fallback (0.50). None are trustworthy enough on their own —
        # the judge call is the same prompt + same inputs as Path A's
        # semantic judge, so a single-gate disagreement is just an
        # independent LLM roll flipping. Leave Path A's verdict in
        # place until the deeper entity-aware-prompt fix lands.
        logger.info(
            "Path C retraction skipped low-confidence verdict for memory %s "
            "candidate %s (confidence=%.2f < threshold=%.2f)",
            new_memory.get("id"),
            candidate.get("id"),
            confidence,
            RETRACTION_CONFIDENCE_THRESHOLD,
        )
        return False

    # Two-step retraction via A4 #10.
    await sc.update_memory_status(str(candidate.get("id")), "active")
    try:
        await sc.update_memory_status(
            str(new_memory.get("id")),
            new_memory.get("status", "active"),
            unset_supersedes=True,
            expected_supersedes_id=str(candidate.get("id")),
        )
    except Exception as e:
        # CAS rejection (409) means another writer mutated the chain
        # — the candidate revert above still landed (idempotent), and
        # the chain edge is whatever the other writer chose. Don't
        # roll the candidate back.
        logger.warning(
            "Path C retraction chain-clear failed for memory %s candidate %s: %s",
            new_memory.get("id"),
            candidate.get("id"),
            e,
        )

    logger.info(
        "Path C retracted Path A's verdict for memory %s: candidate %s reverted to active (confidence=%.2f)",
        new_memory.get("id"),
        candidate.get("id"),
        confidence,
    )
    return True


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

    Also re-judges any candidate Path A retracted (A4 #13). If the
    entity-aware re-judge disagrees with Path A's verdict at sufficient
    confidence, the retraction is undone via the A4 #10 storage primitive.
    """
    from core_api.services.organization_settings import resolve_config

    # Always-fire completion log (Gap 06) — see ``detect_contradictions_async``
    # above for the rationale. Same memory-id-in-message convention.
    t_start = time.monotonic()
    n_candidates = 0
    n_conflicts = 0
    n_retractions = 0
    skipped = False
    try:
        # A4 #14 — back-channel idempotency. Entity extraction can
        # complete more than once per memory (delta re-extraction,
        # partial retry), each completion firing Path C. First caller
        # wins the lock; the rest skip. Fail-open on Redis outage.
        if not await _acquire_path_c_lock(memory_id):
            skipped = True
            return

        sc = get_storage_client()
        new_memory = await sc.get_memory(str(memory_id))
        if not new_memory or new_memory.get("deleted_at") is not None:
            return

        tenant_config = await resolve_config(None, tenant_id)

        # A4 #13 — re-judge Path A's verdict (if any) before the
        # standard entity-overlap detection. Phases are independent:
        # this lookup dereferences ``new_memory.supersedes_id``, while
        # the detection phase below looks at memories that share
        # entities with new_memory. A retraction in this phase doesn't
        # short-circuit the detection phase — Path C may still find a
        # different (genuine) contradiction below.
        if await _attempt_path_c_retraction(sc, new_memory, tenant_config):
            n_retractions = 1
            # The new memory's ``supersedes_id`` was just cleared.
            # Re-fetch so the detection phase below sees the fresh state.
            refreshed = await sc.get_memory(str(memory_id))
            if refreshed and refreshed.get("deleted_at") is None:
                new_memory = refreshed

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

        # A1 #17 — subject preflight. Drop candidates whose
        # ``subject_entity_id`` is non-NULL AND differs from the new
        # memory's: they're definitionally about different subjects,
        # so the LLM judge would be (1) at risk of a false-positive
        # contradiction call when entities share canonical names like
        # "priya" but resolve to distinct entity rows (see
        # ``followup-path-c-judge-first-name-collisions``) and (2)
        # wasteful API spend regardless. Candidates with NULL
        # ``subject_entity_id`` on either side fall through to the
        # entity-links preflight below.
        new_subject = new_memory.get("subject_entity_id")
        filtered_candidates = [
            c
            for c in candidates
            if not _subjects_differ_with_certainty(new_subject, c.get("subject_entity_id"))
        ]
        n_preflight_skipped = len(candidates) - len(filtered_candidates)
        candidates = filtered_candidates
        if not candidates:
            logger.info(
                "Path C preflight skipped all %d candidates for memory %s (distinct subject_entity_id)",
                n_preflight_skipped,
                memory_id,
            )
            return

        # CAURA-130 (L3.4) — entity-links subject preflight. The A1 #17
        # gate above only fires when BOTH sides have non-NULL
        # ``subject_entity_id``. When one side is NULL (heuristic
        # missed but entity-extraction worker populated entity_links
        # with a subject-role entity), same-canonical-name distinct-
        # entity pairs (the ``priya``-collision case in the original
        # followup TODO) silently fell through to the LLM judge — which
        # then mis-classified as a contradiction. Here we resolve the
        # canonical subject identity from ``entity_links`` for the
        # affected candidates and drop on identity mismatch.
        #
        # Cost: only fetch entity context for candidates whose legacy
        # ``subject_entity_id`` gate fell through (at least one side
        # NULL). When BOTH sides have non-NULL ids the A1 #17 gate
        # already handled them — those candidates skip this stage.
        # New memory's context fetched once (1 storage round-trip);
        # remaining work scales with the size of the fall-through set.
        # The whole stage is wrapped in ``asyncio.wait_for(timeout=
        # 5.0)`` — on failure we fail-open (keep candidates) rather
        # than dropping potentially-real contradictions.
        # Track drops from THIS stage separately from the A1 #17
        # legacy gate above — the final log message attributes the
        # count to the entity-links stage, so mixing in the legacy
        # drops would be misleading.
        n_entity_links_skipped = 0
        fallthrough = [c for c in candidates if new_subject is None or c.get("subject_entity_id") is None]
        if len(fallthrough) > _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES:
            # Bound the storage fan-out. A popular entity can produce
            # a large fall-through set; gathering N parallel fetches
            # against the storage API risks a thundering herd. Above
            # the cap we fail open: skip the L3.4 stage entirely and
            # let the LLM judge handle the candidates. The legacy A1
            # #17 gate has already done what it can; we don't drop
            # candidates we couldn't verify.
            logger.warning(
                "Path C entity-links preflight skipped for memory %s — "
                "fall-through set size %d > cap %d. Falling through to LLM judge.",
                memory_id,
                len(fallthrough),
                _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES,
            )
        elif fallthrough:
            try:
                new_ctx, *cand_ctxs = await asyncio.wait_for(
                    asyncio.gather(
                        _fetch_entity_context(sc, str(memory_id)),
                        *(_fetch_entity_context(sc, str(c.get("id"))) for c in fallthrough),
                    ),
                    timeout=5.0,
                )
                new_identity = _extract_subject_canonical_identity(new_ctx)
                if new_identity is not None:
                    new_eid = new_identity[2]
                    drop_ids: set[str] = set()
                    for c, ctx in zip(fallthrough, cand_ctxs, strict=False):
                        cand_identity = _extract_subject_canonical_identity(ctx)
                        if cand_identity is None:
                            continue  # No subject resolved — fail open.
                        if cand_identity[2] != new_eid:
                            drop_ids.add(str(c.get("id")))
                    if drop_ids:
                        before = len(candidates)
                        candidates = [c for c in candidates if str(c.get("id")) not in drop_ids]
                        n_entity_links_skipped = before - len(candidates)
                        logger.info(
                            "Path C entity-links preflight dropped %d candidate(s) "
                            "for memory %s (canonical subjects differ by entity_id)",
                            n_entity_links_skipped,
                            memory_id,
                        )
            except Exception as e:
                # Fail open — keep candidates and let the LLM judge
                # decide. Conservative against losing real
                # contradictions on a transient storage hiccup.
                logger.warning(
                    "Path C entity-links preflight failed for memory %s: %s. Falling through to LLM judge.",
                    memory_id,
                    e,
                )
        if not candidates:
            logger.info(
                "Path C preflight skipped all %d candidates for memory %s (entity-links subject mismatch)",
                n_entity_links_skipped,
                memory_id,
            )
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
        # CAURA-125 — state-corruption guard; mirrors the RDF and
        # semantic paths in ``_detect()``.
        new_memory_is_outdated = False
        # Collapsed-write accumulator — same rationale as the semantic
        # path's ``updates`` dict (audit P2 Path C). Keyed by
        # ``memory_id`` so a mixed canonical/flipped run produces one
        # merged row per memory; see ``_merge_status_update``.
        updates: dict[str, dict] = {}
        for candidate, result in zip(candidates, results):
            if isinstance(result, Exception):
                logger.warning(
                    "Entity contradiction check failed for candidate %s: %s",
                    candidate.get("id"),
                    result,
                )
                continue
            # A4 #12 — judge now returns (verdict, confidence).
            # Path C continues to gate only on verdict at this site;
            # A4 #13 will introduce confidence-weighted vetoes here.
            verdict, _confidence = result  # type: ignore[misc]
            if verdict:
                # CAURA-125 — symmetric attribution; see RDF path for
                # the rationale. First match sets supersedes_id on the
                # newer row (most relevant — candidates are ordered by
                # shared-entity-count DESC); subsequent matches only
                # update the older row's status.
                older = _pick_older(candidate, new_memory)
                older_is_new = str(older.get("id")) == str(memory_id)
                newer = new_memory if not older_is_new else candidate
                older_id = older.get("id")
                newer_id = newer.get("id")

                _merge_status_update(updates, {"memory_id": str(older_id), "status": "conflicted"})
                if not found:
                    if newer is new_memory:
                        # See RDF path above for the rationale of
                        # separating the status-reversion guard from
                        # the chain edge. Entity-based path uses
                        # ``"conflicted"`` (matching the flipped
                        # iteration's earlier write to new_memory).
                        target_status = (
                            "conflicted" if new_memory_is_outdated else new_memory.get("status", "active")
                        )
                        _merge_status_update(
                            updates,
                            {
                                "memory_id": str(memory_id),
                                "status": target_status,
                                "supersedes_id": str(older_id),
                            },
                        )
                    else:
                        new_memory_is_outdated = True
                        # Application-level guard; see RDF flipped
                        # branch in _detect() for rationale.
                        if newer.get("supersedes_id"):
                            logger.warning(
                                "Flipped contradiction skipped supersedes_id overwrite "
                                "for candidate %s (already supersedes %s)",
                                newer_id,
                                newer.get("supersedes_id"),
                            )
                        else:
                            _merge_status_update(
                                updates,
                                {
                                    "memory_id": str(newer_id),
                                    "status": newer.get("status", "active"),
                                    "supersedes_id": str(older_id),
                                },
                            )
                found = True
                n_conflicts += 1
                logger.info(
                    "Entity-based contradiction: %s conflicted by %s direction=%s",
                    older_id,
                    newer_id,
                    "canonical" if newer is new_memory else "flipped",
                )

        if updates:
            path_c_result = await sc.batch_update_status({"updates": list(updates.values())})
            if path_c_result.get("skipped"):
                # See RDF path in ``_detect`` for the ``skipped`` semantics.
                logger.warning(
                    "batch_update_status (Path C entity-overlap) skipped %d row(s) (trigger memory %s): %s",
                    len(path_c_result["skipped"]),
                    memory_id,
                    path_c_result["skipped"],
                )
    except Exception:
        logger.exception("Entity-based contradiction detection failed for %s", memory_id)
    finally:
        elapsed_ms = round((time.monotonic() - t_start) * 1000)
        logger.info(
            "path_c_completed for memory %s n_candidates=%d n_conflicts=%d "
            "n_retractions=%d skipped=%s elapsed_ms=%d tenant_id=%s",
            memory_id,
            n_candidates,
            n_conflicts,
            n_retractions,
            str(skipped).lower(),
            elapsed_ms,
            tenant_id,
        )


# Backward-compat re-exports for tests
from core_api.providers._credentials import has_credentials as _has_api_key  # noqa: F401
from core_api.providers._credentials import (
    resolve_openai_compatible as _resolve_openai_compatible,  # noqa: F401
)
