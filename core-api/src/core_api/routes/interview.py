"""Interviewer Phase 1 — the submit endpoint.

``POST /api/v1/interview/submit`` receives one node's buffered event
window from the OpenClaw plugin (delivered in response to an
``interview_request`` fleet command).

Default path (``interview_async_submit``, #665): persist-and-accept — the
window is masked and stored as a durable ``interview_jobs`` doc, the
forward-only watermark advances, and the route replies 200 ``accepted``
immediately; synthesis (chunked interview → typed memories via the
idempotent bulk path) runs off the request in a fire-and-forget task plus
the hourly scheduler sweep. The legacy inline path (flag off) runs the
full worker on the request: mask → chunked interview → typed memories →
forward-only watermark.

Dark by default: the per-tenant ``interviewer.enabled`` flag gates the
endpoint (the scheduler also never queues commands for disabled tenants —
this check is defense in depth, mirroring the skills_factory pattern).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core_api.auth import AuthContext, get_auth_context
from core_api.config import settings as app_settings
from core_api.constants import INTERVIEW_EVENT_MAX_CHARS, INTERVIEW_MAX_EVENTS_PER_SUBMIT
from core_api.schemas import STRICT_WRITE_BODY
from core_api.services.interview_service import (
    InterviewJobPermanentlyFailedError,
    advance_watermark,
    enqueue_interview_job,
    process_interview_job,
    run_interview,
    run_interview_schedule,
    synthesis_sem,
)
from core_api.services.organization_settings import get_settings_for_display

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Interview"])

# Strong references to the fire-and-forget per-submit synthesis tasks —
# asyncio only holds weak refs to tasks, so without this set the GC could
# cancel one mid-synthesis (#665). The scheduler sweep remains the durable
# retry path if the process dies anyway.
_inflight_jobs: set[asyncio.Task] = set()


# Bound the route's fire-and-forget synthesis like the scheduler sweep bounds
# its own: without this, a burst of simultaneous submits spawns an unbounded
# number of concurrent LLM map-reduce tasks (rate-quota + storage-pool
# exhaustion). Queued tasks just wait — jobs are durable and the sweep is the
# backstop either way.
async def _bounded_process(tenant_id: str, doc_id: str) -> None:
    # Shares interview_service.synthesis_sem with the scheduler sweep so the
    # global synthesis cap holds even when both paths run concurrently.
    async with synthesis_sem:
        await process_interview_job(tenant_id, doc_id)


def _log_task_exc(task: asyncio.Task) -> None:
    """Surface fire-and-forget synthesis failures in the logs (#667):
    without a done-callback retrieving the exception, asyncio defers the
    'Task exception was never retrieved' report to GC time (and drops it
    entirely on shutdown). ``process_interview_job`` never raises ordinary
    exceptions, so anything landing here is a genuine bug — log loudly.
    ``cancelled()`` is guarded first: calling ``exception()`` on a
    cancelled task raises ``CancelledError``."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("interview submit: fire-and-forget synthesis task crashed", exc_info=exc)


class InterviewEventIn(BaseModel):
    """One normalized trail event (contract C2)."""

    model_config = STRICT_WRITE_BODY

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
    model_config = STRICT_WRITE_BODY

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
    status: str  # accepted | committed | partial | failed
    watermark: int | None
    memories_written: int
    errors: int


@router.post("/interview/submit", response_model=InterviewSubmitOut)
async def submit_interview(
    body: InterviewSubmitIn,
    auth: AuthContext = Depends(get_auth_context),
):
    """Interview one node's event window and persist the report as memories.

    Default (``interview_async_submit``, #665): persist the masked window
    durably, advance the watermark, and return 200 ``accepted`` fast —
    synthesis runs off the request path. Flag off: legacy inline synthesis
    returning ``committed``/``partial``/``failed``.

    Idempotent per (node, window): the worker derives the bulk attempt id
    from ``sha1(node_id:cursor_from:cursor_to)`` server-side, so any retry
    of the same window resolves to ``duplicate_attempt`` rows and a
    forward-only watermark — never duplicates, never a gap (the async job
    doc id is derived from the same identity, so duplicate submits upsert
    one job).
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

    if app_settings.interview_async_submit:
        # Persist-and-accept (#665). The 60-90s inline synthesis outlived
        # intermediate proxy budgets (on-prem nginx defaults to 60s), so
        # plugins saw a 504 while the server committed anyway — misreported
        # failure, never pruned. Order matters: the masked window is durable
        # server-side BEFORE the watermark advances and the 2xx goes out, so
        # the node may safely prune its buffer on this response.
        try:
            doc_id = await enqueue_interview_job(
                tenant_id=tenant_id,
                fleet_id=body.fleet_id,
                agent_id=body.agent_id,
                node_id=body.node_id,
                command_id=body.command_id,
                cursor_from=body.cursor_from,
                cursor_to=body.cursor_to,
                events=[ev.model_dump(mode="json") for ev in body.events],
            )
        except InterviewJobPermanentlyFailedError:
            # 409 (not 500): the window is parked after exhausting its retry
            # budget — permanence deserves a distinct status in access logs
            # and lets a future-smarter plugin stop resubmitting.
            raise HTTPException(
                status_code=409,
                detail="interview job is permanently failed — operator intervention required",
            )
        watermark = await advance_watermark(
            tenant_id,
            node_id=body.node_id,
            agent_id=body.agent_id,
            cursor_to=body.cursor_to,
            command_id=body.command_id,
        )
        # Best-effort immediate synthesis WITHOUT holding the request; the
        # scheduler sweep (process_pending_interview_jobs) is the durable
        # retry path if this task dies with the process.
        task = asyncio.create_task(_bounded_process(tenant_id, doc_id))
        _inflight_jobs.add(task)
        task.add_done_callback(_inflight_jobs.discard)
        task.add_done_callback(_log_task_exc)
        # Plain 200: deployed plugins advance/prune on any 2xx with a
        # numeric watermark and do not gate on ``status`` — wire-compatible.
        return InterviewSubmitOut(status="accepted", watermark=watermark, memories_written=0, errors=0)

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
