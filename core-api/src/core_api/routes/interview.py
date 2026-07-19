"""Interviewer Phase 1 — the submit endpoint.

``POST /api/v1/interview/submit`` receives one node's buffered event
window from the OpenClaw plugin (delivered in response to an
``interview_request`` fleet command) and hands it to the interview worker
(``services/interview_service.py``): mask → chunked interview → typed
memories via the idempotent bulk path → forward-only watermark.

Dark by default: the per-tenant ``interviewer.enabled`` flag gates the
endpoint (the scheduler also never queues commands for disabled tenants —
this check is defense in depth, mirroring the skills_factory pattern).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core_api.auth import AuthContext, get_auth_context
from core_api.config import settings as app_settings
from core_api.constants import INTERVIEW_EVENT_MAX_CHARS, INTERVIEW_MAX_EVENTS_PER_SUBMIT
from core_api.services.interview_service import run_interview, run_interview_schedule
from core_api.services.organization_settings import get_settings_for_display

router = APIRouter(tags=["Interview"])


class InterviewEventIn(BaseModel):
    """One normalized trail event (contract C2)."""

    seq: int = Field(ge=0)
    ts: datetime
    session_id: str | None = None
    role: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    # Matches the worker's processing limit (mask_events truncates to the
    # same constant) — accepting more would silently drop the excess from
    # the LLM prompt with no error to the plugin caller.
    content: str = Field(min_length=0, max_length=INTERVIEW_EVENT_MAX_CHARS)
    tool: str | None = Field(default=None, max_length=200)
    outcome: str | None = Field(default=None, max_length=200)


class InterviewSubmitIn(BaseModel):
    tenant_id: str | None = None
    fleet_id: str | None = None
    node_id: str = Field(min_length=1, max_length=200)
    # The WORKER agent the window belongs to (memory subject).
    agent_id: str = Field(min_length=1, max_length=200)
    command_id: str | None = Field(default=None, max_length=200)
    cursor_from: int = Field(ge=0)
    cursor_to: int = Field(ge=0)
    events: list[InterviewEventIn] = Field(min_length=1, max_length=INTERVIEW_MAX_EVENTS_PER_SUBMIT)


class InterviewSubmitOut(BaseModel):
    status: str  # committed | partial | failed
    watermark: int | None
    memories_written: int
    errors: int


@router.post("/interview/submit", response_model=InterviewSubmitOut)
async def submit_interview(
    body: InterviewSubmitIn,
    auth: AuthContext = Depends(get_auth_context),
):
    """Interview one node's event window and persist the report as memories.

    Idempotent per (node, window): the worker derives the bulk attempt id
    from ``sha1(node_id:cursor_from:cursor_to)`` server-side, so any retry
    of the same window resolves to ``duplicate_attempt`` rows and a
    forward-only watermark — never duplicates, never a gap.
    """
    auth.enforce_read_only()
    auth.enforce_usage_limits()

    tenant_id = body.tenant_id or auth.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required")
    auth.enforce_tenant(tenant_id)

    if body.cursor_to < body.cursor_from:
        raise HTTPException(status_code=422, detail="cursor_to must be >= cursor_from")
    seqs = [ev.seq for ev in body.events]
    if any(seqs[i] >= seqs[i + 1] for i in range(len(seqs) - 1)):
        raise HTTPException(status_code=422, detail="events must be strictly seq-ascending (no duplicates)")
    if seqs[0] < body.cursor_from or seqs[-1] > body.cursor_to:
        raise HTTPException(
            status_code=422,
            detail="event seq range must lie within [cursor_from, cursor_to]",
        )

    settings = await get_settings_for_display(tenant_id)
    interviewer_cfg = settings.get("interviewer") or {}
    if not interviewer_cfg.get("enabled"):
        # Defense in depth: the scheduler shouldn't have queued a command
        # for a disabled tenant; refuse rather than silently ingest.
        raise HTTPException(status_code=403, detail="interviewer is not enabled for this tenant")

    # Route-enforced deadline (the path opts out of the blanket 45s
    # middleware, which 504'd every realistic window — the synchronous
    # map-reduce interview measured ~63s for a full 400-event window in
    # the real-LLM pilot). A 504 here is retry-safe end-to-end: the
    # watermark advances only after the bulk write commits, the plugin
    # never prunes on error, and the deterministic attempt id dedups any
    # rows that did land before the deadline.
    try:
        result = await asyncio.wait_for(
            run_interview(
                tenant_id=tenant_id,
                fleet_id=body.fleet_id,
                agent_id=body.agent_id,
                node_id=body.node_id,
                command_id=body.command_id,
                cursor_from=body.cursor_from,
                cursor_to=body.cursor_to,
                events=[ev.model_dump(mode="json") for ev in body.events],
            ),
            timeout=app_settings.interview_request_timeout_seconds,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="interview exceeded its request budget; window not consumed",
        )

    if result["status"] == "failed":
        # Whole window failed to persist: watermark NOT advanced; the
        # plugin must NOT prune. 500 (origin error, not 502 — proxies/ALBs
        # rewrite 502 and strip the JSON body) → the command retries next
        # tick (caller checks >= 400).
        raise HTTPException(status_code=500, detail="interview ingest failed; window not consumed")
    if result["status"] == "partial":
        # Mirror the bulk endpoint's 207 semantics: some rows landed, the
        # cursor advanced, caller reads per-field detail.
        return JSONResponse(status_code=207, content=InterviewSubmitOut(**result).model_dump())
    return InterviewSubmitOut(**result)


@router.post("/admin/interview/schedule/run")
async def run_interview_schedule_endpoint(
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Queue due ``interview_request`` fleet commands (admin/cron only).

    The core-operations hourly tick POSTs this. Enumerates orgs with
    ``interviewer.enabled``, and per live node queues at most one pending
    command, gated by the watermark's ``last_interview_at`` against the
    tenant's ``period_hours``. Returns a bounded counts summary.
    """
    auth.enforce_admin()
    return await run_interview_schedule()
