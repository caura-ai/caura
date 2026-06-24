"""Evolve service -- outcome-driven weight adjustment and rule generation.

The 'adapt' step of the Karpathy Loop. Agents report real-world outcomes,
the system adjusts memory weights to reinforce or dampen information, and
optionally generates preventive rules via LLM on failure/partial outcomes.
"""

import asyncio
import logging
import time
from uuid import UUID

from core_api.constants import (
    EVOLVE_FAILURE_DELTA,
    EVOLVE_MAX_RELATED_IDS,
    EVOLVE_OUTCOME_TYPES,
    EVOLVE_PARTIAL_DELTA,
    EVOLVE_RULE_CONFIDENCE_THRESHOLD,
    EVOLVE_RULE_TEMPERATURE,
    EVOLVE_SUCCESS_DELTA,
    EVOLVE_WEIGHT_CAP,
    EVOLVE_WEIGHT_FLOOR,
    VALID_SCOPES,
)
from core_api.utils.sanitize import sanitize_content as _sanitize_content

logger = logging.getLogger(__name__)


# A10 — slugs identifying every silent-exit path on the rule synthesis
# flow. Callers (the harness, the dashboard, an operator inspecting
# ``report_outcome`` results) read these from the response's
# ``rule_skipped_reason`` field instead of grep'ing log strings, so
# downstream tooling can pattern-match without parsing prose.
RULE_SKIP_REASONS: tuple[str, ...] = (
    "not_failure_or_partial",  # success outcomes don't generate rules
    "no_related_ids",  # failure/partial but no memories supplied
    "no_memories_fetched",  # every storage fetch failed
    "llm_failed",  # provider raised or returned non-dict
    "below_confidence_threshold",  # rule generated but conf < threshold
    "persist_failed",  # _persist_rule returned None
)


def _log_rule_skip(reason: str, tenant_id: str, outcome_type: str, **extra) -> None:
    """Always-fire log line for an evolve rule-synthesis skip.

    Mirrors the ``path_a_completed`` / ``path_c_completed`` pattern in
    ``contradiction_detector.py`` (Gap 06): the reason slug lives in
    the message string itself so a plain
    ``grep evolve_rule_skipped <reason>`` works regardless of the
    structlog renderer's ``extra={}`` handling."""
    extras = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
    logger.info(
        "evolve_rule_skipped reason=%s tenant_id=%s outcome_type=%s %s",
        reason,
        tenant_id,
        outcome_type,
        extras,
    )


# A15 — slugs identifying every silent-exit path on the weight-adjustment
# flow. Mirrors RULE_SKIP_REASONS for the parallel observability story
# on ``weight_adjustment_skipped_reason`` in the report_outcome response.
# An evolve call that returns 200 OK with ``weight_adjustments=[]`` now
# also carries the reason — callers can distinguish "no rows moved
# because nothing was supplied" from "no rows moved because every ID was
# out of scope" without parsing out_of_scope_count.
WEIGHT_ADJUSTMENT_SKIP_REASONS: tuple[str, ...] = (
    "no_related_ids",  # caller supplied no related_ids
    "agent_id_mismatch",  # scope=agent, every related_id dropped by scope filter
    "fleet_id_mismatch",  # scope=fleet, every related_id dropped by scope filter
    "all_out_of_scope",  # scope=all, every related_id invalid UUID or missing row
    "no_rows_updated",  # bulk UPDATE matched 0 rows (race: row deleted between filter and update)
)


def _log_weight_adjustment_skip(reason: str, tenant_id: str, scope: str, **extra) -> None:
    """Always-fire log line for an evolve weight-adjustment skip.

    Same grep-friendly shape as ``_log_rule_skip``: the slug lives in
    the message itself so ``grep evolve_weight_adjustment_skipped <reason>``
    works regardless of structlog ``extra={}`` handling."""
    extras = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
    logger.info(
        "evolve_weight_adjustment_skipped reason=%s tenant_id=%s scope=%s %s",
        reason,
        tenant_id,
        scope,
        extras,
    )


# -- Delta map ----------------------------------------------------------------

_DELTA_MAP = {
    "success": EVOLVE_SUCCESS_DELTA,
    "failure": EVOLVE_FAILURE_DELTA,
    "partial": EVOLVE_PARTIAL_DELTA,
}

# -- Scope → outcome/rule memory visibility -----------------------------------
# Mirrors insights_service._SCOPE_TO_VISIBILITY so a scope='fleet' evolve
# writes an outcome memory with scope_team visibility (fleet-wide reach) and
# scope='agent' stays private to the reporting agent.

_SCOPE_TO_VISIBILITY = {
    "agent": "scope_agent",
    "fleet": "scope_team",
    "all": "scope_org",
}


# -- Rule generation prompt ---------------------------------------------------

_RULE_GENERATION_PROMPT = """\
You are analyzing a {outcome_type} outcome to generate a preventive rule.

OUTCOME: {outcome}

RELATED MEMORIES ({count} memories the agent used before this outcome):
{memories}

Based on this {outcome_type}, generate a rule that would help avoid this outcome \
in the future. The rule should be:
- Specific enough to trigger in similar situations
- General enough to apply beyond this exact case
- Actionable — the agent should know what to DO differently

Respond with JSON:
{{
  "condition": "IF/WHEN this situation arises (describe the trigger condition)",
  "action": "THEN do this instead (describe the corrective action)",
  "confidence": 0.0 to 1.0 (how confident are you this rule is correct and useful),
  "reasoning": "brief explanation of why this rule would help"
}}
"""


# -- Weight adjustment --------------------------------------------------------
#
# Fix 2 Ph5b (PR2): the weight-clamp CTE and the rule→outcome jsonb_set backfill
# moved to core-storage-api (``sc.evolve_apply_weights`` — they were ported
# VERBATIM there and now run in ONE atomic storage transaction). The
# scope-filter SELECT moved too (``sc.evolve_filter_by_scope``). The module no
# longer holds those SQL constants or a direct ``db.execute`` against
# ``memories``; the dedup / UUID-parse / cap / rounding / skip-reason logic
# stays here on the client side.


async def _filter_by_scope(
    tenant_id: str,
    caller_agent_id: str,
    fleet_id: str | None,
    scope: str,
    related_ids: list[str],
) -> tuple[list[str], int]:
    """Drop IDs the caller cannot touch under ``scope``.

    Scope rules (mirror insights_service._scope_filters):
      - ``agent``: keep memories where ``memory.agent_id = caller_agent_id``.
      - ``fleet``: keep memories where ``memory.fleet_id = fleet_id`` (fleet_id required).
      - ``all``:   keep any memory in the tenant.

    Invalid UUIDs, missing rows, soft-deleted rows, and rows that fail the
    scope predicate are dropped silently — same behavior as the existing
    missing-row handling in ``_adjust_weights``. Duplicates supplied by the
    caller are collapsed to the first occurrence and the extra copies count
    toward ``out_of_scope_count`` (duplicates can't be adjusted twice in a
    single evolve call anyway).

    Returns (in_scope_ids, out_of_scope_count). The returned list is
    deduplicated and preserves first-seen order of the input so downstream
    consumers get a stable, canonical view of the caller's intent.

    Fix 2 Ph5b (PR2): the scope SELECT moved to core-storage-api
    (``sc.evolve_filter_by_scope``); the UUID-parse + first-seen dedup +
    ``out_of_scope_count`` arithmetic stays here. ``db`` is retained in the
    signature for REST back-compat but is ignored.
    """
    from core_api.clients.storage_client import get_storage_client

    if not related_ids:
        return [], 0

    # Precondition guard: scope='fleet' without a fleet_id is unsatisfiable.
    # The docstring promises fleet_id is required for that scope; report_outcome
    # already rejects this upstream, but _filter_by_scope is a public-ish
    # helper that tests and future callers may invoke directly — fail loud
    # rather than silently matching nothing in a bound-param NULL.
    if scope == "fleet" and fleet_id is None:
        raise ValueError("_filter_by_scope: fleet_id is required when scope is 'fleet'.")

    # Convert to UUIDs, silently dropping non-parseable strings and
    # duplicates (dict assignment overwrites prior occurrences). First-seen
    # string is retained as the dict key so the returned list keeps the
    # caller's ordering.
    valid_uuids: dict[str, UUID] = {}
    for s in related_ids:
        try:
            valid_uuids[s] = UUID(s)
        except (ValueError, TypeError):
            continue

    if not valid_uuids:
        # All caller inputs failed UUID parsing — every slot counts against
        # out_of_scope. Matches the formula below: n_invalid_or_dup equals
        # ``len(related_ids) - len(valid_uuids) = len(related_ids) - 0``.
        return [], len(related_ids)

    # The scope SELECT runs storage-side (``sc.evolve_filter_by_scope`` ports it
    # VERBATIM — UUID-object comparison via ``select(Memory.id).in_(...)``, so
    # the canonical-form mismatch that ``id::text`` would produce for uppercase
    # or unhyphenated callers is still avoided). The allowed ids come back as
    # canonical strings; normalise to UUID so membership against the caller's
    # ``valid_uuids`` (also UUIDs) doesn't miss on string-vs-UUID identity.
    sc = get_storage_client()
    allowed_strs = await sc.evolve_filter_by_scope(
        tenant_id=tenant_id,
        caller_agent_id=caller_agent_id,
        fleet_id=fleet_id,
        scope=scope,
        ids=[str(u) for u in valid_uuids.values()],
    )
    allowed: set[UUID] = {UUID(s) for s in allowed_strs}

    in_scope = [s for s, uid in valid_uuids.items() if uid in allowed]
    # out_of_scope_count has two components we keep separate so the name
    # actually tells the truth under duplicate or invalid input:
    #   - n_invalid_or_dup: entries the caller sent that never made it into
    #     valid_uuids (unparseable strings, or duplicates of an earlier ID
    #     that `valid_uuids[s] = UUID(s)` collapsed).
    #   - n_out_of_scope_unique: unique, parseable IDs that failed the DB
    #     scope predicate (wrong owner/fleet, missing row, or soft-deleted).
    # The total is algebraically identical to ``len(related_ids) - len(in_scope)``
    # but the breakdown makes the warning log below interpretable.
    n_invalid_or_dup = len(related_ids) - len(valid_uuids)
    n_out_of_scope_unique = len(valid_uuids) - len(in_scope)
    out_of_scope = n_invalid_or_dup + n_out_of_scope_unique

    if out_of_scope > 0:
        logger.warning(
            "evolve: dropped %d related_ids that failed scope=%s checks (tenant=%s, caller=%s, fleet=%s)",
            out_of_scope,
            scope,
            tenant_id,
            caller_agent_id,
            fleet_id,
        )

    return in_scope, out_of_scope


async def _adjust_weights(
    tenant_id: str,
    related_ids: list[str],
    outcome_type: str,
    agent_id: str,
    *,
    rule_id: str | None = None,
    outcome_id: str | None = None,
) -> tuple[str | None, list[str], list[dict]]:
    """Adjust weights on related memories atomically.

    Fix 2 Ph5b (PR2): the clamp-and-return CTE now runs storage-side
    (``sc.evolve_apply_weights`` ports ``_ADJUST_WEIGHTS_BULK_SQL`` VERBATIM and
    executes it in ONE transaction), eliminating core-api's direct
    ``db.execute`` against ``memories``. The CTE still captures the pre-update
    weight so ``old_weight`` is exact even when clamping at the floor or cap.
    The same storage call folds in the rule→outcome ``jsonb_set`` backfill when
    ``rule_id`` and ``outcome_id`` are supplied, so the weight clamp and the
    backfill commit atomically without a second HTTP round-trip — the caller
    re-sequences so both ids are resolved before invoking this. ``db`` is
    retained in the signature for REST back-compat but is ignored.

    Concurrency: ``related_ids`` is still deduped + sorted before the call so
    the storage-side UPDATE locks rows in a deterministic global order,
    avoiding cycle-based deadlocks across concurrent evolve calls.

    Audit: update_memory's audit hook is bypassed; the outcome memory's
    metadata records the change as a compensating trail.

    Returns a tuple of (skip_reason, processed_ids, adjustments):
    - skip_reason: A15 — ``None`` when at least one row was updated;
      ``"no_rows_updated"`` when every supplied id failed UUID parsing,
      the apply-weights call returned no rows (row deleted between filter and
      update), or the call raised. Callers feed this into ``report_outcome``'s
      ``weight_adjustment_skipped_reason`` response field so a 200 OK with
      empty adjustments is no longer indistinguishable from success.
    - processed_ids: IDs whose weights were actually updated in the DB.
      Excludes invalid UUIDs, missing rows, and rows whose UPDATE raised.
      Callers persist this in the outcome metadata so it reflects reality
      rather than the caller's optimistic input.
    - adjustments: [{memory_id, old_weight, new_weight, delta}].
    """
    from core_api.clients.storage_client import get_storage_client

    # Dedup + sort first so the cap counts unique IDs (truncating before
    # dedup can leave fewer than EVOLVE_MAX_RELATED_IDS distinct items when
    # the caller passes duplicates). Sorted order also ensures every
    # concurrent evolve call locks rows in the same global order,
    # avoiding cycle-based deadlocks.
    deduped = sorted(set(related_ids))
    if len(deduped) > EVOLVE_MAX_RELATED_IDS:
        logger.warning(
            "evolve: related_ids truncated from %d unique to %d",
            len(deduped),
            EVOLVE_MAX_RELATED_IDS,
        )
        deduped = deduped[:EVOLVE_MAX_RELATED_IDS]
    related_ids = deduped

    delta = _DELTA_MAP[outcome_type]
    adjustments: list[dict] = []
    successfully_adjusted: list[str] = []

    # Parse all UUIDs up front. Invalid strings are logged + dropped
    # before the SQL roundtrip rather than failing the whole batch.
    parsed: list[tuple[str, UUID]] = []
    for mid_str in related_ids:
        try:
            parsed.append((mid_str, UUID(mid_str)))
        except ValueError:
            logger.warning("evolve: skipping invalid UUID: %s", mid_str)

    if not parsed:
        return "no_rows_updated", [], []

    valid_uuids = [u for _, u in parsed]

    # Single atomic storage call: the clamp-and-return CTE (+ the conditional
    # rule→outcome backfill) commits in ONE transaction storage-side. A failure
    # is surfaced as the ``no_rows_updated`` slug rather than aborting the whole
    # evolve call — the rule/outcome memories were already persisted.
    sc = get_storage_client()
    try:
        resp = await sc.evolve_apply_weights(
            tenant_id=tenant_id,
            ids=[str(u) for u in valid_uuids],
            delta=delta,
            floor=EVOLVE_WEIGHT_FLOOR,
            cap=EVOLVE_WEIGHT_CAP,
            rule_id=rule_id,
            outcome_id=outcome_id,
        )
    except Exception:
        logger.warning("evolve: bulk weight update failed for %d memories", len(valid_uuids), exc_info=True)
        return "no_rows_updated", [], []

    # Map returned rows by id so we can preserve the caller's input ordering
    # when building the response. Rows missing from the result correspond to
    # ids not found (or filtered by the tenant + deleted_at predicate); skip
    # them with the same warning the prior path emitted. ``UUID(row["id"])``
    # normalises the canonical string back to a UUID so the lookup against the
    # parsed ``UUID`` keys can't silently miss.
    row_by_id = {UUID(row["id"]): row for row in resp.get("adjustments", [])}
    for mid_str, mid in parsed:
        row = row_by_id.get(mid)
        if row is None:
            logger.warning("evolve: memory %s not found for tenant %s, skipping", mid, tenant_id)
            continue
        successfully_adjusted.append(mid_str)
        adjustments.append(
            {
                "memory_id": mid_str,
                "old_weight": round(float(row["old_weight"]), 4),
                "new_weight": round(float(row["new_weight"]), 4),
                "delta": round(delta, 4),
            }
        )

    # Race / no-match path: parse + UPDATE succeeded but no rows came back
    # (a row was deleted between _filter_by_scope and the UPDATE, or every
    # id failed the tenant+deleted_at predicate). Surface as a slug so the
    # caller can distinguish from a success.
    skip_reason = "no_rows_updated" if not successfully_adjusted else None
    return skip_reason, successfully_adjusted, adjustments


# -- Rule generation ----------------------------------------------------------


def _fake_rule() -> dict:
    """Placeholder rule for fake/test LLM provider."""
    return {
        "condition": "When encountering a similar situation",
        "action": "Verify information against the most recent source before acting",
        "confidence": 0.6,
        "reasoning": "Fake rule generated for testing.",
    }


async def _generate_rule(
    tenant_id: str,
    outcome: str,
    outcome_type: str,
    related_ids: list[str],
    config,
    agent_id: str,
    fleet_id: str | None,
) -> tuple[str | None, dict | None]:
    """Ask LLM to generate a preventive rule from a failure/partial outcome.

    Returns ``(skip_reason, rule)``:
      - ``(None, {...})`` on success
      - ``(<RULE_SKIP_REASONS slug>, None)`` on any silent-exit path

    A10 widened the return from ``dict | None`` to the tuple so callers
    (``report_outcome``) can propagate the specific reason out to the
    response + structured log instead of conflating "no candidates",
    "LLM blew up", and "fetched zero memories" into a single None.

    Audit P3 (evolve): ``db`` was removed from this signature. The
    function only used it to resolve tenant config; callers now do
    that themselves and pass ``config`` in. This lets the MCP tool
    (``memclaw_evolve``) close its DB session before invoking the
    LLM round-trip — which can take multiple seconds and would
    otherwise pin a pooled connection.

    ``agent_id`` and ``fleet_id`` are reserved arguments; the body
    does not use them today but they're preserved so future
    rule-generation strategies (per-agent prompts, fleet-scoped
    examples) can light up without a signature break.
    """
    from core_api.clients.storage_client import get_storage_client
    from core_api.providers._retry import call_with_fallback

    sc = get_storage_client()

    # Fetch related memories for context (cap at 10 for prompt size).
    # Parallelize HTTP fetches so latency is O(1) instead of O(n) round trips.
    # Sanitize title/content before putting them into the LLM prompt — they
    # originate from agent writes and can contain injection attempts.
    async def _fetch(mid_str: str) -> tuple[str, dict | None]:
        try:
            return mid_str, await sc.get_memory_for_tenant(tenant_id, mid_str)
        except Exception:
            logger.warning("evolve: fetch failed for memory %s", mid_str, exc_info=True)
            return mid_str, None

    # Dedup before slicing so the prompt doesn't repeat the same memory
    # if the caller passed duplicate UUIDs.
    unique_ids = list(dict.fromkeys(related_ids))[:10]
    fetched = await asyncio.gather(*[_fetch(m) for m in unique_ids])
    memories_text_lines = []
    for mid_str, mem in fetched:
        if not mem:
            continue
        title = _sanitize_content(mem.get("title") or "", max_len=120)
        content = _sanitize_content(mem.get("content") or "", max_len=500)
        weight = mem.get("weight", 0.5)
        mtype = mem.get("memory_type", "fact")
        memories_text_lines.append(f"- (id:{mid_str}) [{mtype}] {title}: {content} [weight: {weight:.2f}]")

    if not memories_text_lines:
        return ("no_memories_fetched", None)

    memories_text = "\n".join(memories_text_lines)
    # ``str.format`` inserts substituted values literally (it never re-scans them
    # for fields), so outcome/memories must NOT be brace-escaped — escaping would
    # corrupt literal {word} / JSON / code in agent input or DB content. A
    # substituted value never raises KeyError. Still cap the outcome to bound
    # prompt tokens.
    safe_outcome = _sanitize_content(outcome, max_len=2000)
    prompt = _RULE_GENERATION_PROMPT.format(
        outcome=safe_outcome,
        outcome_type=outcome_type,
        memories=memories_text,
        count=len(memories_text_lines),
    )

    async def _do_generate(llm) -> dict:
        return await llm.complete_json(prompt, temperature=EVOLVE_RULE_TEMPERATURE)

    try:
        raw = await call_with_fallback(
            primary_provider_name=config.enrichment_provider,
            call_fn=_do_generate,
            fake_fn=_fake_rule,
            tenant_config=config,
            service_label="evolve-rule",
            model_override=config.enrichment_model,
        )
    except Exception:
        logger.exception("evolve: rule generation failed")
        return ("llm_failed", None)

    if not isinstance(raw, dict):
        return ("llm_failed", None)

    # LLMs occasionally return confidence as None, a string like "high", or
    # omit it entirely. Coerce defensively to avoid TypeError/ValueError
    # propagating out of the service.
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    return (
        None,
        {
            "condition": str(raw.get("condition", ""))[:500],
            "action": str(raw.get("action", ""))[:500],
            "confidence": confidence,
            "reasoning": str(raw.get("reasoning", ""))[:500],
        },
    )


# -- Persist ------------------------------------------------------------------


async def _persist_outcome(
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
    outcome: str,
    outcome_type: str,
    related_ids: list[str],
    weight_adjustments: list[dict],
    rule_memory_id: str | None,
    scope: str,
) -> str:
    """Write the outcome as a memory of type 'outcome'. Returns outcome memory ID.

    `rule_memory_id` is the ID of the rule memory persisted in Phase 4 (or None
    if no rule met the confidence threshold). Storing the resolved ID — rather
    than a `rule_generated` boolean — avoids the case where the flag claims a
    rule exists but no corresponding memory was actually created.

    `scope` determines the outcome memory's visibility via _SCOPE_TO_VISIBILITY
    so scope='agent' outcomes stay private and scope='all' outcomes are visible
    tenant-wide.

    Fix 2 Ph5b (PR2): ``create_memory`` is fully storage-routed and commits
    independently, so ``db`` may be ``None`` (MCP ``_no_db`` path). The savepoint
    only isolates a real REST-path session; with ``db=None`` there is no session
    to protect, so the savepoint is skipped via ``nullcontext``.
    """
    from core_api.schemas import MemoryCreate
    from core_api.services.memory_service import create_memory

    # Failure outcomes get higher weight — more informative for future analysis
    weight_map = {"success": 0.6, "failure": 0.7, "partial": 0.5}

    # Cap outcome length to bound persisted content size (consistent with
    # rule fields capped at 500 chars in _generate_rule).
    content = f"[Outcome/{outcome_type}] {outcome[:2000]}"

    data = MemoryCreate(
        tenant_id=tenant_id,
        agent_id=agent_id,
        fleet_id=fleet_id,
        memory_type="outcome",
        content=content,
        weight=weight_map.get(outcome_type, 0.5),
        metadata={
            "outcome_type": outcome_type,
            "related_memory_ids": related_ids,
            "weight_adjustments": weight_adjustments,
            "rule_memory_id": rule_memory_id,
            "scope": scope,
        },
        visibility=_SCOPE_TO_VISIBILITY.get(scope, "scope_team"),
        write_mode="fast",
    )

    # Outcome persistence is mandatory (unlike the rule): create_memory commits
    # independently via the storage client, so re-raise on failure to signal
    # the caller.
    try:
        result = await create_memory(data)
    except Exception:
        logger.exception("evolve: failed to persist outcome")
        raise
    return str(result.id)


async def _persist_rule(
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
    rule: dict,
    scope: str,
    outcome_id: str | None = None,
) -> str | None:
    """Write a generated rule as a memory of type 'rule'. Returns rule memory ID.

    `outcome_id` is optional because the rule is now persisted before the
    outcome memory (so the outcome metadata can record the resolved
    rule_memory_id). The reverse link from rule → outcome is set to None;
    callers can backfill if a strict bidirectional link is required.

    `scope` controls the persisted rule's visibility so a rule generated from
    scope='agent' evolve stays private to the reporting agent, while a
    scope='all' rule is visible tenant-wide.
    """
    from core_api.schemas import MemoryCreate
    from core_api.services.memory_service import create_memory

    condition = rule.get("condition", "")
    action = rule.get("action", "")
    confidence = rule.get("confidence", 0.5)
    reasoning = rule.get("reasoning", "")

    content = f"RULE: IF {condition} THEN {action}"
    if reasoning:
        content += f" (Reasoning: {reasoning})"

    data = MemoryCreate(
        tenant_id=tenant_id,
        agent_id=agent_id,
        fleet_id=fleet_id,
        memory_type="rule",
        content=content,
        weight=confidence,
        metadata={
            "rule_condition": condition,
            "rule_action": action,
            "rule_confidence": confidence,
            "rule_reasoning": reasoning,
            "source_outcome_id": outcome_id,
            "generated_by": "evolve",
            "scope": scope,
        },
        visibility=_SCOPE_TO_VISIBILITY.get(scope, "scope_team"),
        write_mode="fast",
    )

    # Rule persistence is best-effort: create_memory commits independently via
    # the storage client, so a failure here is logged and swallowed (returns
    # None) without blocking the weight adjustments and outcome write.
    try:
        result = await create_memory(data)
        return str(result.id)
    except Exception:
        logger.exception("evolve: failed to persist rule")
        return None


# -- Public API ---------------------------------------------------------------


async def report_outcome(
    tenant_id: str,
    outcome: str,
    outcome_type: str,
    related_ids: list[str] | None = None,
    scope: str = "agent",
    agent_id: str = "mcp-agent",
    fleet_id: str | None = None,
) -> dict:
    """Record an outcome, adjust related memory weights, and optionally generate rules.

    Parameters
    ----------
    db : AsyncSession
    tenant_id : str
    outcome : str
        Natural language description of what happened.
    outcome_type : str
        "success", "failure", or "partial".
    related_ids : list[str] | None
        Memory UUIDs that influenced the agent's action. Optional.
    scope : str
        "agent" (default, touches only caller-owned memories), "fleet"
        (touches memories in ``fleet_id``), or "all" (tenant-wide).
        Out-of-scope IDs are dropped silently with a warning log.
    agent_id : str
    fleet_id : str | None
        Required when ``scope='fleet'``.

    Returns
    -------
    dict with outcome_id, outcome_type, scope, weight_adjustments,
    rules_generated, rule_skipped_reason, weight_adjustment_skipped_reason,
    out_of_scope_count, evolve_ms.
    """
    t0 = time.perf_counter()

    # Defensive validation — both MCP and REST entry points validate these
    # before calling in, but the service re-checks so any future caller path
    # (direct invocation, tests, new routes) can't bypass the contract. All
    # raises use ValueError so the service layer stays decoupled from
    # FastAPI; callers translate to the appropriate HTTP status.
    if outcome_type not in EVOLVE_OUTCOME_TYPES:
        raise ValueError(
            f"Invalid outcome_type '{outcome_type}'. Must be one of: {', '.join(EVOLVE_OUTCOME_TYPES)}"
        )
    if not outcome or not outcome.strip():
        raise ValueError("outcome must be a non-empty description.")
    if scope not in VALID_SCOPES:
        raise ValueError(f"Invalid scope '{scope}'. Must be: {', '.join(VALID_SCOPES)}.")
    if scope == "fleet" and not fleet_id:
        raise ValueError("fleet_id is required when scope is 'fleet'.")

    from core_api.services.organization_settings import resolve_config

    # Phase 0: Filter related_ids by scope. Runs before rule generation so the
    # LLM prompt never sees memory content the caller shouldn't access (e.g.,
    # a scope='agent' caller passing another agent's memory IDs). Dropped IDs
    # are tallied into out_of_scope_count for observability.
    #
    # A15: alongside the count, classify why no weights will move when the
    # filter returns an empty set. The slug is the first thing populated;
    # downstream paths (``_adjust_weights`` race / DB-failure) may override
    # it. Mirrors the A10 rule_skipped_reason flow: pre-compute upstream,
    # let the deeper stage override on a more-specific failure.
    out_of_scope_count = 0
    weight_adjustment_skipped_reason: str | None = None
    if not related_ids:
        weight_adjustment_skipped_reason = "no_related_ids"
    else:
        original_count = len(related_ids)
        related_ids, out_of_scope_count = await _filter_by_scope(
            tenant_id=tenant_id,
            caller_agent_id=agent_id,
            fleet_id=fleet_id,
            scope=scope,
            related_ids=related_ids,
        )
        if not related_ids and out_of_scope_count >= original_count:
            # Filter dropped everything. Map scope → slug.
            weight_adjustment_skipped_reason = {
                "agent": "agent_id_mismatch",
                "fleet": "fleet_id_mismatch",
                "all": "all_out_of_scope",
            }.get(scope, "all_out_of_scope")
            _log_weight_adjustment_skip(
                weight_adjustment_skipped_reason,
                tenant_id,
                scope,
                out_of_scope_count=out_of_scope_count,
                caller_agent_id=agent_id,
                fleet_id=fleet_id,
            )

    # Resolve tenant config up front so ``_maybe_generate_rule`` can be
    # called without any DB dependency. Both REST (here) and MCP
    # (``memclaw_evolve``) feed config in the same way.
    config = await resolve_config(tenant_id)

    # Phase 1: Generate rule BEFORE touching weights. The MCP tool
    # closes its DB session between this phase and ``_apply_outcome_to_db``
    # so the LLM round-trip (which can take several seconds) doesn't
    # pin a pooled connection. REST callers run the whole chain in one
    # session — same total latency, just no pool relief.
    rule_result, rule_skipped_reason = await _maybe_generate_rule(
        tenant_id,
        outcome,
        outcome_type,
        related_ids,
        config,
        agent_id,
        fleet_id,
    )

    return await _apply_outcome_to_db(
        tenant_id=tenant_id,
        agent_id=agent_id,
        fleet_id=fleet_id,
        outcome=outcome,
        outcome_type=outcome_type,
        related_ids=related_ids,
        rule_result=rule_result,
        rule_skipped_reason=rule_skipped_reason,
        scope=scope,
        out_of_scope_count=out_of_scope_count,
        weight_adjustment_skipped_reason=weight_adjustment_skipped_reason,
        t0=t0,
    )


async def _maybe_generate_rule(
    tenant_id: str,
    outcome: str,
    outcome_type: str,
    related_ids: list[str],
    config,
    agent_id: str,
    fleet_id: str | None,
) -> tuple[dict | None, str | None]:
    """Decide whether to invoke ``_generate_rule`` and return ``(rule, skip_reason)``.

    Pure compute + LLM call, NO DB access. Callers resolve tenant config
    first and pass it in. Lets the MCP tool fire this between two
    independent DB sessions so the multi-second LLM round-trip doesn't
    pin a pooled connection (audit P3).

    Returns:
      - ``(rule_dict, None)`` on a successful generation.
      - ``(None, slug)`` for any silent-exit path; ``slug`` names the
        reason (one of ``RULE_SKIP_REASONS``).
    """
    if outcome_type not in ("failure", "partial"):
        reason = "not_failure_or_partial"
        _log_rule_skip(reason, tenant_id, outcome_type)
        return None, reason
    if not related_ids:
        reason = "no_related_ids"
        _log_rule_skip(reason, tenant_id, outcome_type)
        return None, reason
    gen_reason, rule_result = await _generate_rule(
        tenant_id,
        outcome,
        outcome_type,
        related_ids,
        config,
        agent_id,
        fleet_id,
    )
    if gen_reason is not None:
        _log_rule_skip(gen_reason, tenant_id, outcome_type)
        return None, gen_reason
    return rule_result, None


async def _apply_outcome_to_db(
    *,
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
    outcome: str,
    outcome_type: str,
    related_ids: list[str],
    rule_result: dict | None,
    rule_skipped_reason: str | None,
    scope: str,
    out_of_scope_count: int,
    weight_adjustment_skipped_reason: str | None,
    t0: float,
) -> dict:
    """Phases 2-5: the entire write side of evolve, fully storage-routed.

    Audit P3 (evolve): split out of ``report_outcome`` so the MCP tool can call
    it from a fresh session opened AFTER the LLM rule generation.

    Fix 2 Ph5b (PR2): every write now routes through core-storage-api — the rule
    + outcome memories via ``create_memory`` (storage-committed independently,
    as before), and the weight clamp + the rule→outcome ``jsonb_set`` backfill
    via ONE ``sc.evolve_apply_weights`` call (ONE atomic storage transaction).
    There is no local ``db`` session to commit, so the final ``db.commit()`` is
    gone. The phases are RE-SEQUENCED so ``rule_id``/``outcome_id`` are resolved
    BEFORE the single apply-weights call (the backfill needs the outcome_id, and
    the source-outcome link is folded into the same transaction as the clamp):
      1. persist rule (if confident)        → rule_memory_id
      2. persist outcome                    → outcome_id
      3. apply-weights (clamp + backfill)   → adjustments

    Split-commit contract (unchanged in spirit): the rule/outcome memories are
    storage-committed before the apply-weights call; if apply-weights fails the
    memories remain and the weights/backfill are lost — surfaced as the
    ``no_rows_updated`` slug rather than a raised exception.
    """
    # Phase 2: Persist rule memory if confidence meets threshold. The outcome_id
    # is not known yet; the rule→outcome link is backfilled inside the
    # apply-weights call in Phase 4 once the outcome exists.
    rule_memory_id: str | None = None
    if rule_result is not None:
        confidence = rule_result.get("confidence", 0)
        if confidence < EVOLVE_RULE_CONFIDENCE_THRESHOLD:
            rule_skipped_reason = "below_confidence_threshold"
            _log_rule_skip(
                rule_skipped_reason,
                tenant_id,
                outcome_type,
                confidence=confidence,
                threshold=EVOLVE_RULE_CONFIDENCE_THRESHOLD,
            )
        else:
            rule_memory_id = await _persist_rule(tenant_id, agent_id, fleet_id, rule_result, scope=scope)
            if rule_memory_id is None:
                rule_skipped_reason = "persist_failed"
                _log_rule_skip(rule_skipped_reason, tenant_id, outcome_type)

    # Phase 3: Persist outcome memory. It must exist before the apply-weights
    # call so the rule→outcome backfill (folded into that single atomic call)
    # has the outcome_id. The outcome records the rule_memory_id and the
    # in-scope related_ids. ``weight_adjustments`` is persisted EMPTY here BY
    # DESIGN: the post-clamp deltas aren't known until Phase 4, and writing them
    # back would need a second outcome-memory write that we deliberately skip to
    # keep the clamp + rule→outcome backfill in one atomic apply-weights txn. The
    # real deltas live in the report_outcome response built below and in the
    # audit log; nothing reads the persisted ``metadata.weight_adjustments``.
    outcome_id = await _persist_outcome(
        tenant_id,
        agent_id,
        fleet_id,
        outcome,
        outcome_type,
        related_ids,
        [],
        rule_memory_id,
        scope=scope,
    )

    # Phase 4: Adjust weights + (atomically) backfill the rule→outcome link in
    # ONE storage transaction. rule_id/outcome_id are now resolved, so the
    # backfill rides along with the clamp rather than a second HTTP call.
    weight_adjustments: list[dict] = []
    if related_ids:
        # ``_processed_ids`` (the subset actually clamped) is intentionally
        # discarded, not merely unused: the outcome memory was already persisted
        # in Phase 3 with the scope-filtered ``related_ids`` and is deliberately
        # not rewritten, and the per-id post-clamp deltas live in the response's
        # ``weight_adjustments`` below.
        adjust_skip_reason, _processed_ids, weight_adjustments = await _adjust_weights(
            tenant_id,
            related_ids,
            outcome_type,
            agent_id,
            rule_id=rule_memory_id,
            outcome_id=outcome_id,
        )
        # A15: the deeper stage's slug wins. The upstream slug from
        # ``report_outcome`` only fires when the scope filter dropped every id;
        # if we got here, the filter passed at least one but the bulk UPDATE
        # didn't update any (race / parse error).
        if adjust_skip_reason is not None:
            weight_adjustment_skipped_reason = adjust_skip_reason
            _log_weight_adjustment_skip(
                adjust_skip_reason, tenant_id, scope, related_ids_count=len(related_ids)
            )
    # Note: a rule is only ever generated when related_ids is non-empty
    # (``_maybe_generate_rule`` returns the ``no_related_ids`` skip otherwise),
    # so the rule→outcome backfill always rides along with a non-empty clamp
    # call above — there is no no-related-ids-with-rule backfill path to drive
    # separately. The backfill therefore commits atomically with the clamp.

    return {
        "outcome_id": outcome_id,
        "outcome_type": outcome_type,
        "scope": scope,
        "weight_adjustments": weight_adjustments,
        "rules_generated": [
            {
                "rule_memory_id": rule_memory_id,
                "condition": rule_result["condition"],
                "action": rule_result["action"],
                "confidence": rule_result["confidence"],
            }
        ]
        if rule_memory_id and rule_result
        else [],
        # A10 — see ``RULE_SKIP_REASONS`` for the slug taxonomy. None
        # means a rule was generated; a slug names the silent-exit
        # path that fired. Mirrors the always-fire log line emitted
        # alongside.
        "rule_skipped_reason": rule_skipped_reason,
        # A15 — see ``WEIGHT_ADJUSTMENT_SKIP_REASONS``. None when at
        # least one weight moved; a slug otherwise. Distinguishes the
        # silent-noop shape A15 reported (200 OK + ``weight_adjustments=[]``
        # masquerading as success) into a contract callers can inspect.
        "weight_adjustment_skipped_reason": weight_adjustment_skipped_reason,
        "out_of_scope_count": out_of_scope_count,
        "evolve_ms": int((time.perf_counter() - t0) * 1000),
    }
