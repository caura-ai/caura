"""Evolve service -- outcome-driven weight adjustment and rule generation.

The 'adapt' step of the Karpathy Loop. Agents report real-world outcomes,
the system adjusts memory weights to reinforce or dampen information, and
optionally generates preventive rules via LLM on failure/partial outcomes.
"""

import asyncio
import logging
import time
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

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


_ADJUST_WEIGHT_SQL = text(
    """
    WITH old_val AS (
        SELECT weight AS old_weight
          FROM memories
         WHERE id = :mid AND tenant_id = :tid AND deleted_at IS NULL
    )
    UPDATE memories
       SET weight = GREATEST(:floor, LEAST(:cap, weight + :delta))
      FROM old_val
     WHERE memories.id = :mid
       AND memories.tenant_id = :tid
       AND memories.deleted_at IS NULL
    RETURNING old_val.old_weight AS old_weight, memories.weight AS new_weight
    """
)


# Backfill the rule memory's metadata.source_outcome_id after the outcome
# exists. Rule persistence happens before outcome persistence (so the outcome
# can record the rule_memory_id), so at rule-write time the outcome_id is
# unknown. This UPDATE completes the rule→outcome traceability link.
_BACKFILL_RULE_OUTCOME_SQL = text(
    """
    UPDATE memories
       SET metadata = jsonb_set(
           COALESCE(metadata, '{}'::jsonb),
           '{source_outcome_id}',
           to_jsonb(CAST(:outcome_id AS text))
       )
     WHERE id = :rule_id AND tenant_id = :tid
    """
)


async def _filter_by_scope(
    db: AsyncSession,
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
    """
    from common.models.memory import Memory

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

    # Build the scope filter via SQLAlchemy's expression API rather than
    # interpolating a SQL fragment. All user-derived values remain bound
    # parameters and the query plan is visible to tooling (EXPLAIN, SQLA
    # event hooks). Comparing UUID objects — not stringified forms — also
    # eliminates the canonical-form mismatch that ``id::text`` would have
    # produced for callers passing uppercase or unhyphenated UUIDs.
    stmt = (
        select(Memory.id)
        .where(Memory.id.in_(list(valid_uuids.values())))
        .where(Memory.tenant_id == tenant_id)
        .where(Memory.deleted_at.is_(None))
    )
    if scope == "agent":
        stmt = stmt.where(Memory.agent_id == caller_agent_id)
    elif scope == "fleet":
        stmt = stmt.where(Memory.fleet_id == fleet_id)

    result = await db.execute(stmt)
    allowed: set[UUID] = {row[0] for row in result}

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
    db: AsyncSession,
    tenant_id: str,
    related_ids: list[str],
    outcome_type: str,
    agent_id: str,
) -> tuple[list[str], list[dict]]:
    """Adjust weights on related memories atomically.

    Executes a CTE-based UPDATE per memory so each row is read, clamped, and
    written in a single statement — eliminating the TOCTOU race that existed
    when the read (via storage-client HTTP) and the write crossed transaction
    boundaries. The CTE captures the pre-update weight so `old_weight` in the
    response is exact even when clamping occurs at the floor or cap.

    Concurrency: `related_ids` is deduped and sorted before iteration so all
    concurrent callers acquire row locks in the same order, preventing the
    cycle that would otherwise trigger PostgreSQL deadlocks when two evolve
    calls touch overlapping memory sets.

    Audit: update_memory's audit hook is bypassed; the outcome memory's
    metadata (`weight_adjustments`) records the change as a compensating trail.

    Returns a tuple of (processed_ids, adjustments):
    - processed_ids: IDs whose weights were actually updated in the DB.
      Excludes invalid UUIDs, missing rows, and rows whose UPDATE raised.
      Callers persist this in the outcome metadata so it reflects reality
      rather than the caller's optimistic input.
    - adjustments: [{memory_id, old_weight, new_weight, delta}].
    """
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

    for mid_str in related_ids:
        try:
            mid = UUID(mid_str)
        except ValueError:
            logger.warning("evolve: skipping invalid UUID: %s", mid_str)
            continue

        # Savepoint isolates a per-row UPDATE failure: without it, an asyncpg
        # error here would put the connection in aborted-transaction state and
        # the swallowed exception would only surface later as a confusing
        # `RELEASE SAVEPOINT` failure on the final db.commit().
        try:
            async with db.begin_nested():
                result = await db.execute(
                    _ADJUST_WEIGHT_SQL,
                    {
                        "mid": mid,  # UUID object; asyncpg handles type
                        "tid": tenant_id,
                        "floor": EVOLVE_WEIGHT_FLOOR,
                        "cap": EVOLVE_WEIGHT_CAP,
                        "delta": delta,
                    },
                )
                row = result.fetchone()
        except Exception:
            logger.warning("evolve: weight update failed for memory %s", mid, exc_info=True)
            continue

        if not row:
            logger.warning("evolve: memory %s not found for tenant %s, skipping", mid, tenant_id)
            continue

        successfully_adjusted.append(mid_str)
        adjustments.append(
            {
                "memory_id": mid_str,
                "old_weight": round(float(row.old_weight), 4),
                "new_weight": round(float(row.new_weight), 4),
                "delta": round(delta, 4),
            }
        )

    return successfully_adjusted, adjustments


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
    db: AsyncSession,
    tenant_id: str,
    outcome: str,
    outcome_type: str,
    related_ids: list[str],
    agent_id: str,
    fleet_id: str | None,
) -> dict | None:
    """Ask LLM to generate a preventive rule from a failure/partial outcome.

    Returns dict with {condition, action, confidence, reasoning} or None on failure.
    """
    from core_api.clients.storage_client import get_storage_client
    from core_api.providers._retry import call_with_fallback
    from core_api.services.organization_settings import resolve_config

    sc = get_storage_client()
    config = await resolve_config(db, tenant_id)

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
        return None

    memories_text = "\n".join(memories_text_lines)
    # Escape user-controlled braces before .format() — both outcome (agent input)
    # and memories_text (DB content/titles) can contain literal {word} that would
    # otherwise raise KeyError outside the call_with_fallback try/except.
    # Also cap outcome size to bound prompt tokens.
    safe_outcome = _sanitize_content(outcome, max_len=2000).replace("{", "{{").replace("}", "}}")
    safe_memories = memories_text.replace("{", "{{").replace("}", "}}")
    prompt = _RULE_GENERATION_PROMPT.format(
        outcome=safe_outcome,
        outcome_type=outcome_type,
        memories=safe_memories,
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
        return None

    if not isinstance(raw, dict):
        return None

    # LLMs occasionally return confidence as None, a string like "high", or
    # omit it entirely. Coerce defensively to avoid TypeError/ValueError
    # propagating out of the service.
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "condition": str(raw.get("condition", ""))[:500],
        "action": str(raw.get("action", ""))[:500],
        "confidence": confidence,
        "reasoning": str(raw.get("reasoning", ""))[:500],
    }


# -- Persist ------------------------------------------------------------------


async def _persist_outcome(
    db: AsyncSession,
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

    # Savepoint isolates a DB error inside create_memory so the outer
    # transaction stays usable. Outcome persistence is mandatory (unlike
    # the rule), so re-raise after the savepoint rolls back to signal
    # failure to the caller.
    try:
        async with db.begin_nested():
            result = await create_memory(db, data)
    except Exception:
        logger.exception("evolve: failed to persist outcome")
        raise
    return str(result.id)


async def _persist_rule(
    db: AsyncSession,
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

    # Savepoint isolates the rule write: if create_memory raises after dirtying
    # the session (e.g. a side-effect DB write fails before the HTTP call), the
    # outer transaction can still proceed with weight adjustments and the
    # outcome write without the session being in a failed state.
    try:
        async with db.begin_nested():
            result = await create_memory(db, data)
        return str(result.id)
    except Exception:
        logger.exception("evolve: failed to persist rule")
        return None


# -- Public API ---------------------------------------------------------------


async def report_outcome(
    db: AsyncSession,
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
    rules_generated, out_of_scope_count, evolve_ms.
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

    # Phase 0: Filter related_ids by scope. Runs before rule generation so the
    # LLM prompt never sees memory content the caller shouldn't access (e.g.,
    # a scope='agent' caller passing another agent's memory IDs). Dropped IDs
    # are tallied into out_of_scope_count for observability.
    out_of_scope_count = 0
    if related_ids:
        related_ids, out_of_scope_count = await _filter_by_scope(
            db,
            tenant_id=tenant_id,
            caller_agent_id=agent_id,
            fleet_id=fleet_id,
            scope=scope,
            related_ids=related_ids,
        )

    # Phase 1: Generate rule BEFORE touching weights. Rule generation makes
    # HTTP calls to the storage client and an LLM that can take several seconds;
    # doing it first means Phase 2's row locks are held for DB round-trips only,
    # never across HTTP/LLM I/O. The rule prompt only needs memory content for
    # context — it has no dependency on the updated weights.
    rule_result = None
    if outcome_type in ("failure", "partial") and related_ids:
        rule_result = await _generate_rule(
            db,
            tenant_id,
            outcome,
            outcome_type,
            related_ids,
            agent_id,
            fleet_id,
        )

    # Phase 2: Adjust weights atomically. Row locks are acquired and released
    # entirely within this block — no long-running work runs while locks are held.
    processed_ids: list[str] = []
    weight_adjustments: list[dict] = []
    if related_ids:
        processed_ids, weight_adjustments = await _adjust_weights(
            db, tenant_id, related_ids, outcome_type, agent_id
        )

    # Phase 3: Persist rule memory if confidence meets threshold. outcome_id
    # is not known yet; it is backfilled in Phase 5 after the outcome exists.
    rule_memory_id: str | None = None
    if rule_result and rule_result.get("confidence", 0) >= EVOLVE_RULE_CONFIDENCE_THRESHOLD:
        rule_memory_id = await _persist_rule(db, tenant_id, agent_id, fleet_id, rule_result, scope=scope)

    # Phase 4: Persist outcome memory — records rule_memory_id and the IDs
    # that actually got their weights updated.
    outcome_id = await _persist_outcome(
        db,
        tenant_id,
        agent_id,
        fleet_id,
        outcome,
        outcome_type,
        processed_ids,
        weight_adjustments,
        rule_memory_id,
        scope=scope,
    )

    # Phase 5: Backfill the rule's source_outcome_id so rules are queryable
    # back to their originating outcome without scanning every outcome's
    # related_memory_ids list. Best-effort — a failure here doesn't abort
    # the whole call since the outcome already references the rule.
    if rule_memory_id and outcome_id:
        # Savepoint isolates the backfill: a failure here (e.g. snapshot
        # visibility race when the rule was committed on a separate connection)
        # would otherwise abort the outer transaction and surface as a
        # `RELEASE SAVEPOINT` error on the final db.commit().
        try:
            async with db.begin_nested():
                await db.execute(
                    _BACKFILL_RULE_OUTCOME_SQL,
                    {
                        "rule_id": UUID(rule_memory_id),
                        "outcome_id": outcome_id,
                        "tid": tenant_id,
                    },
                )
        except Exception:
            logger.exception("evolve: failed to backfill source_outcome_id on rule %s", rule_memory_id)

    # Commit local DB-session writes (weight UPDATEs + the rule→outcome
    # backfill UPDATE). Split-commit contract: rule/outcome memories were
    # already persisted independently by storage-api over HTTP and are NOT
    # rolled back if this commit fails. On commit failure the outcome and
    # rule remain in storage but weight adjustments are lost and the rule
    # lacks source_outcome_id — log loudly so operators can reconcile.
    try:
        await db.commit()
    except Exception:
        logger.error(
            "evolve: db.commit() failed after storage-api writes succeeded; "
            "rule_memory_id=%s outcome_id=%s — weights and rule→outcome backfill "
            "are lost but the memories remain in storage",
            rule_memory_id,
            outcome_id,
            exc_info=True,
        )
        raise

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
        "out_of_scope_count": out_of_scope_count,
        "evolve_ms": int((time.perf_counter() - t0) * 1000),
    }
