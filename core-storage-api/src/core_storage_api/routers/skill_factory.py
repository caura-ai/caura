"""Skill-factory pipeline endpoints (Fix 2 Ph5a).

Routes the skill-factory background pipeline's direct DB access through
core-storage-api HTTP so core-api stops holding raw ``db.execute`` against
``forge_rejected_fingerprints``, ``session_traces``, ``memories`` and
``memory_entity_links`` for these analytic reads/writes.

Every endpoint takes an explicit ``tenant_id`` (and ``fleet_id`` / window /
params where the source query scopes by them) — there are NO RLS GUCs
server-side; 422 if a required ``tenant_id`` is missing (mirrors the sibling
routers). The PostgresService methods these call port the source PG SQL
VERBATIM.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.routers._validation import _require
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(tags=["SkillFactory"])
_svc = PostgresService()


def _parse_window(body: dict) -> tuple[datetime, datetime]:
    """Parse ``window_start`` / ``window_end`` (ISO strings) → datetimes.

    422 on a missing or malformed bound rather than letting
    ``fromisoformat`` surface as a 500 — a bad window is a client error.
    """
    out: list[datetime] = []
    for key in ("window_start", "window_end"):
        raw = body.get(key)
        if not raw:
            raise HTTPException(status_code=422, detail=f"{key} is required")
        try:
            out.append(datetime.fromisoformat(raw))
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"Invalid ISO datetime for {key!r}: {raw!r}")
    return out[0], out[1]


# ──────────────────────────────────────────────────────────────────────
#  Forge rejected fingerprints (anti-poison memory)
# ──────────────────────────────────────────────────────────────────────


@router.post("/forge/rejected-fingerprints")
async def write_rejected_fingerprint(request: Request) -> dict:
    """Insert one ``forge_rejected_fingerprints`` row; return ``{id}``."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    cluster_fingerprint = _require(body, "cluster_fingerprint")
    rejected_by_agent = _require(body, "rejected_by_agent")
    cooloff_days = body.get("cooloff_days")
    if not isinstance(cooloff_days, int) or cooloff_days < 1:
        raise HTTPException(status_code=422, detail="cooloff_days (int >= 1) is required")
    new_id = await _svc.forge_write_rejected_fingerprint(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        cluster_fingerprint=cluster_fingerprint,
        rejected_by_agent=rejected_by_agent,
        cooloff_days=cooloff_days,
        reason=body.get("reason"),
    )
    return {"id": new_id}


@router.post("/forge/rejected-fingerprints/check")
async def check_fingerprint_poisoned(request: Request) -> dict:
    """Return ``{poisoned: bool}`` for a (tenant, fleet, fp) triple."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    cluster_fingerprint = _require(body, "cluster_fingerprint")
    poisoned = await _svc.forge_is_fingerprint_poisoned(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        cluster_fingerprint=cluster_fingerprint,
    )
    return {"poisoned": poisoned}


# ──────────────────────────────────────────────────────────────────────
#  Session traces
# ──────────────────────────────────────────────────────────────────────


@router.post("/session-traces/upsert")
async def upsert_session_traces(request: Request) -> dict:
    """Batch-upsert ``session_traces`` keyed by (tenant_id, run_id, agent_id).

    Body ``{tenant_id, traces: [...]}``. The batch-level ``tenant_id`` is
    forced onto every row server-side (cross-tenant write guard).
    """
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    traces = body.get("traces")
    if not isinstance(traces, list):
        raise HTTPException(status_code=422, detail="traces (list) is required")
    await _svc.session_traces_upsert(tenant_id=tenant_id, traces=traces)
    return {"upserted": len(traces)}


@router.post("/session-traces/memories-window")
async def session_memories_in_window(request: Request) -> list[dict]:
    """Run-scoped memories in the window, for trace enumeration."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    window_start, window_end = _parse_window(body)
    return await _svc.session_memories_in_window(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        window_start=window_start,
        window_end=window_end,
    )


@router.post("/session-traces/entity-links")
async def memory_entity_links_batch(request: Request) -> list[dict]:
    """``(memory_id, entity_id)`` pairs for a batch of memory ids."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    memory_ids = body.get("memory_ids")
    if not isinstance(memory_ids, list):
        raise HTTPException(status_code=422, detail="memory_ids (list) is required")
    return await _svc.memory_entity_links_batch(tenant_id=tenant_id, memory_ids=memory_ids)


@router.post("/forge/memories-content")
async def memory_content_by_ids(request: Request) -> list[dict]:
    """Bulk ``(id, content)`` by memory id for the forge memory fetcher."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    memory_ids = body.get("memory_ids")
    if not isinstance(memory_ids, list):
        raise HTTPException(status_code=422, detail="memory_ids (list) is required")
    return await _svc.memory_content_by_ids(tenant_id=tenant_id, memory_ids=memory_ids)


# ──────────────────────────────────────────────────────────────────────
#  Outcome-inference signal reads
# ──────────────────────────────────────────────────────────────────────


@router.post("/outcome-signals/contradictions")
async def outcome_contradictions(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    window_start, window_end = _parse_window(body)
    statuses = body.get("contradicted_statuses")
    if not isinstance(statuses, list) or not statuses:
        raise HTTPException(status_code=422, detail="contradicted_statuses (non-empty list) is required")
    return await _svc.outcome_contradiction_signals(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        window_start=window_start,
        window_end=window_end,
        contradicted_statuses=statuses,
        run_id=body.get("run_id"),
        agent_id=body.get("agent_id"),
    )


@router.post("/outcome-signals/supersessions")
async def outcome_supersessions(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    window_start, window_end = _parse_window(body)
    return await _svc.outcome_supersession_signals(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        window_start=window_start,
        window_end=window_end,
        run_id=body.get("run_id"),
        agent_id=body.get("agent_id"),
    )


@router.post("/outcome-signals/cross-agent-reuse")
async def outcome_cross_agent_reuse(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    window_start, window_end = _parse_window(body)
    threshold = body.get("threshold")
    if not isinstance(threshold, int):
        raise HTTPException(status_code=422, detail="threshold (int) is required")
    return await _svc.outcome_cross_agent_reuse_signals(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        window_start=window_start,
        window_end=window_end,
        threshold=threshold,
        run_id=body.get("run_id"),
        agent_id=body.get("agent_id"),
    )


@router.post("/outcome-signals/terminal-memory")
async def outcome_terminal_memory(request: Request) -> list[dict]:
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    window_start, window_end = _parse_window(body)
    return await _svc.outcome_terminal_memory_signals(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        window_start=window_start,
        window_end=window_end,
        run_id=body.get("run_id"),
        agent_id=body.get("agent_id"),
    )
