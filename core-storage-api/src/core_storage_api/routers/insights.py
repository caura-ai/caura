"""Insights analytic endpoints (Fix 2 Ph5b).

Routes the ``insights_service`` query/persist passes + the
``lifecycle_audit.insights()`` activity gate through core-storage-api HTTP so
core-api stops holding raw ``db.execute`` against ``memories`` for these
analytic reads and the supersede/restore UPDATEs.

Every endpoint takes an explicit ``tenant_id`` (and ``agent_id`` / ``scope`` /
``fleet_id`` where the source query scopes by them) — there are NO RLS GUCs
server-side; 422 if a required field is missing (mirrors the sibling routers).
The tuning caps (``max_memories`` = ``INSIGHTS_MAX_MEMORIES``, ``sample_size``
= ``INSIGHTS_DISCOVER_SAMPLE_SIZE``) are forwarded from core-api so the
constants stay the single source of truth there (mirrors skill_factory's
``threshold`` param). The PostgresService methods these call port the source
ORM/SQL VERBATIM.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(tags=["Insights"])
_svc = PostgresService()


def _require(body: dict, key: str) -> str:
    val = body.get(key)
    if not val:
        raise HTTPException(status_code=422, detail=f"{key} is required")
    return val


def _scope_args(body: dict) -> tuple[str, str | None, str, str]:
    """Pull the four scope params common to the 6 analytic reads.

    ``tenant_id`` + ``agent_id`` + ``scope`` are required; ``fleet_id`` is
    optional (but the underlying ``_scope_filters`` raises ValueError → 500 if
    scope='fleet' arrives without it — the core-api request validators block
    that combination before it reaches storage)."""
    tenant_id = _require(body, "tenant_id")
    agent_id = _require(body, "agent_id")
    scope = _require(body, "scope")
    return tenant_id, body.get("fleet_id"), agent_id, scope


def _max_memories(body: dict) -> int:
    val = body.get("max_memories")
    if not isinstance(val, int) or val < 1:
        raise HTTPException(status_code=422, detail="max_memories (int >= 1) is required")
    return val


# ──────────────────────────────────────────────────────────────────────
#  Analytic reads (one per focus)
# ──────────────────────────────────────────────────────────────────────


@router.post("/insights/contradictions")
async def insights_contradictions(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id, fleet_id, agent_id, scope = _scope_args(body)
    return await _svc.insights_query_contradictions(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=_max_memories(body),
    )


@router.post("/insights/failures")
async def insights_failures(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id, fleet_id, agent_id, scope = _scope_args(body)
    return await _svc.insights_query_failures(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=_max_memories(body),
    )


@router.post("/insights/stale")
async def insights_stale(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id, fleet_id, agent_id, scope = _scope_args(body)
    # The two age thresholds are computed on the caller's clock (core-api) and
    # sent as ISO strings; bind as datetimes server-side.
    for key in ("thirty_days_ago", "fourteen_days_ago"):
        if not body.get(key):
            raise HTTPException(status_code=422, detail=f"{key} is required")
    try:
        thirty = datetime.fromisoformat(body["thirty_days_ago"])
        fourteen = datetime.fromisoformat(body["fourteen_days_ago"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid ISO datetime for the stale-age thresholds")
    return await _svc.insights_query_stale(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        thirty_days_ago=thirty,
        fourteen_days_ago=fourteen,
        max_memories=_max_memories(body),
    )


@router.post("/insights/divergence")
async def insights_divergence(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id, fleet_id, agent_id, scope = _scope_args(body)
    return await _svc.insights_query_divergence(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=_max_memories(body),
    )


@router.post("/insights/patterns")
async def insights_patterns(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id, fleet_id, agent_id, scope = _scope_args(body)
    return await _svc.insights_query_patterns(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=_max_memories(body),
    )


@router.post("/insights/discover-sample")
async def insights_discover_sample(request: Request) -> list[dict]:
    """Rows (INCLUDING ``embedding``) for client-side k-means clustering."""
    body: dict = await request.json()
    tenant_id, fleet_id, agent_id, scope = _scope_args(body)
    sample_size = body.get("sample_size")
    if not isinstance(sample_size, int) or sample_size < 1:
        raise HTTPException(status_code=422, detail="sample_size (int >= 1) is required")
    return await _svc.insights_discover_sample(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        sample_size=sample_size,
    )


# ──────────────────────────────────────────────────────────────────────
#  Supersede / restore writes + activity gate
# ──────────────────────────────────────────────────────────────────────


@router.post("/insights/supersede-priors")
async def insights_supersede_priors(request: Request) -> dict:
    """Atomically select + outdate prior active insights for a focus/scope/fleet.

    Body ``{tenant_id, agent_id, focus, scope, fleet_id?}``. Returns
    ``{prior_ids, outdated_count}``."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    agent_id = _require(body, "agent_id")
    focus = _require(body, "focus")
    scope = _require(body, "scope")
    return await _svc.insights_supersede_priors(
        tenant_id=tenant_id,
        agent_id=agent_id,
        focus=focus,
        scope=scope,
        fleet_id=body.get("fleet_id"),
    )


@router.post("/insights/restore-priors")
async def insights_restore_priors(request: Request) -> dict:
    """Restore previously-outdated priors to ``active`` (total-failure net).

    Body ``{tenant_id, prior_ids:[...]}``. Returns ``{restored}``."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    prior_ids = body.get("prior_ids")
    if not isinstance(prior_ids, list):
        raise HTTPException(status_code=422, detail="prior_ids (list) is required")
    return await _svc.insights_restore_priors(tenant_id=tenant_id, prior_ids=prior_ids)


@router.post("/insights/activity-gate")
async def insights_activity_gate(request: Request) -> dict:
    """``MAX(created_at)`` for non-insight vs insight memories, scoped to
    tenant (+ fleet). Body ``{tenant_id, fleet_id?}``. Returns
    ``{latest_non_insight, latest_insight}`` (ISO or null)."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    return await _svc.insights_activity_gate(tenant_id=tenant_id, fleet_id=body.get("fleet_id"))
