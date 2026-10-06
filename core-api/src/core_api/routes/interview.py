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
import time
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core_api import request_phase
from core_api.agent_ids import canonical_service_agent_id
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings as app_settings
from core_api.constants import (
    INTERVIEW_EVENT_MAX_CHARS,
    INTERVIEW_MAX_CURSOR_ADVANCE,
    INTERVIEW_MAX_EVENTS_PER_SUBMIT,
)
from core_api.errors import (
    AUTH_AGENT_TRUST_TOO_LOW,
    AUTH_FEATURE_DISABLED,
    AUTH_INTERVIEW_REQUEST_REQUIRED,
    REQUEST_BUDGET_EXCEEDED,
    coded_detail,
)
from core_api.routes.memories import _resolve_rest_write_agent_id
from core_api.schemas import STRICT_WRITE_BODY
from core_api.services.agent_service import enforce_fleet_write, resolve_write_agent
from core_api.services.interview_service import (
    InterviewJobPermanentlyFailedError,
    advance_watermark,
    enqueue_interview_job,
    process_interview_job,
    read_watermark_state,
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


def _is_uuid(value: str) -> bool:
    """True when ``value`` has a fleet-node or command id's shape (a UUID)."""
    try:
        UUID(value)
    except ValueError:
        return False
    return True


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

    ``agent_id`` resolves like a memory write (an agent-scoped credential
    writes as itself; install credentials get the broker ownership boundary),
    and an agent awaiting approval is refused (403). A UUID ``node_id`` must
    be a fleet node of the tenant (404 otherwise); any other ``node_id`` is an
    adapter stream, which an agent-bound credential may continue only if its
    own agent last advanced it (409 otherwise). And ``cursor_to`` may run at
    most ``INTERVIEW_MAX_CURSOR_ADVANCE`` past the stream's committed
    watermark (422 otherwise).

    An agent or install credential's window for a fleet node must also cite an
    unused ``interview_request`` that was delivered to that node (403
    otherwise). Each request admits one window, so a resubmit under a spent
    request is refused; the scheduler issues a fresh one, as it does after any
    failed submit.
    """
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    body.agent_id = canonical_service_agent_id(body.agent_id)

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
        raise HTTPException(
            status_code=403,
            detail=coded_detail(AUTH_FEATURE_DISABLED, "interviewer is not enabled for this tenant"),
        )

    # Write identity — the chain ``POST /memories/bulk`` runs, because the
    # synthesized report lands as memories attributed to ``agent_id``. A
    # verified agent identity wins over the body; install credentials get the
    # broker ownership boundary; the agent is registered on first contact; an
    # omitted ``fleet_id`` resolves to its home fleet; a cross-fleet one needs
    # trust >= 3.
    agent, body.agent_id = await resolve_write_agent(
        _resolve_rest_write_agent_id(auth, body.agent_id),
        tenant_id,
        body.fleet_id,
        is_install_credential=auth.is_install_credential,
        install_uuid=auth.install_uuid,
    )
    # M-89: the report lands as memories attributed to this agent, so one
    # awaiting approval is refused, as on ``POST /memories``. The watermark has
    # not moved, so the window can be resubmitted once the agent is approved.
    if agent.get("trust_level", 0) == 0:
        raise HTTPException(
            status_code=403,
            detail=coded_detail(
                AUTH_AGENT_TRUST_TOO_LOW,
                f"Agent '{body.agent_id}' is not approved. Contact tenant admin to set trust_level >= 1.",
            ),
        )
    if not body.fleet_id and agent.get("fleet_id"):
        body.fleet_id = agent["fleet_id"]
    if auth.tenant_id:  # skip enforcement for admin
        await enforce_fleet_write(tenant_id, body.agent_id, body.fleet_id)

    # ``node_id`` keys the watermark and the job doc. Its shape says which of
    # two kinds of stream it names:
    #
    # - A UUID is a FLEET NODE: the fleet-node id the scheduler put in the
    #   ``interview_request`` payload. It must name a node of THIS tenant, with
    #   the same 404 for another tenant's node as for one that never existed.
    # - Anything else is an ADAPTER STREAM, keyed by its client.
    #   ``caura-interviewer`` (``clients/python``) submits one per transcript as
    #   ``cc:<machine>:<session>`` or ``cursor:...``. No fleet node exists for
    #   it, so requiring one refused every window that adapter sends.
    #
    # An adapter stream belongs to the agent that last advanced it. Above, an
    # agent credential's ``body.agent_id`` resolved to its own identity and an
    # install credential's to an agent the install owns, so either may continue
    # only streams of that agent and cannot jump another agent's cursor. Tenant
    # credentials keep tenant-wide authority. 409 rather than 403: the
    # credential is valid and only this stream is refused, which
    # ``caura-interviewer`` handles by skipping the one transcript, where a 403
    # aborts its whole run.
    names_fleet_node = _is_uuid(body.node_id)
    if names_fleet_node:
        nodes = await get_storage_client().list_nodes(tenant_id)
        if not any(str(n.get("id") or "") == body.node_id for n in nodes):
            raise HTTPException(status_code=404, detail="node_id is not a node of this tenant")
    committed, stream_agent_id = await read_watermark_state(tenant_id, body.node_id)
    if (
        not names_fleet_node
        and (auth.agent_id or auth.is_install_credential)
        and stream_agent_id is not None
        and canonical_service_agent_id(stream_agent_id) != body.agent_id
    ):
        raise HTTPException(status_code=409, detail="node_id names another agent's interview stream")
    # The watermark only moves forward, so a window claiming a cursor far past
    # anything the node can hold would permanently skip the node's real events.
    if body.cursor_to - committed > INTERVIEW_MAX_CURSOR_ADVANCE:
        raise HTTPException(
            status_code=422,
            detail=(
                f"cursor_to {body.cursor_to} is more than {INTERVIEW_MAX_CURSOR_ADVANCE} "
                f"past the node's committed watermark ({committed})"
            ),
        )
    # M-86 — a fleet node's window from an agent or install credential must cite
    # the ``interview_request`` the scheduler queued for that node and its
    # heartbeat delivered, and each request admits one window. Naming the node
    # used to be enough, so any such credential could advance any node's
    # watermark and take its job doc. Storage checks and spends the request in
    # one statement; the node's own result report still closes the command. It
    # runs last, so a window refused for its shape does not spend the request.
    if names_fleet_node and (auth.agent_id or auth.is_install_credential):
        command_id = body.command_id or ""
        if not (
            _is_uuid(command_id)
            and await get_storage_client().claim_interview_request(tenant_id, command_id, body.node_id)
        ):
            raise HTTPException(
                status_code=403,
                detail=coded_detail(
                    AUTH_INTERVIEW_REQUEST_REQUIRED,
                    "Cite an unused interview_request that was delivered to this fleet node.",
                ),
            )

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
    #
    # ax-0917-h-01/h-02 follow-up: opting out of the blanket middleware also
    # opted this route out of the phase attribution that middleware arms, so
    # its 504 said the deadline passed and nothing about which of the two very
    # different halves passed it — the map-phase LLM chain or the bulk write
    # it feeds. ``own_deadline`` arms the same recorder against this route's
    # own budget.
    interview_budget = app_settings.interview_request_timeout_seconds
    started_at = time.monotonic()
    try:
        with request_phase.own_deadline(interview_budget) as phases:
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
                timeout=interview_budget,
            )
    except TimeoutError:
        attribution = phases.snapshot()
        elapsed = round(time.monotonic() - started_at, 3)
        logger.warning(
            "interview exceeded its request budget",
            extra={
                "budget_seconds": interview_budget,
                "elapsed_seconds": elapsed,
                "path": "/api/v1/interview/submit",
                "phase": attribution["phase"],
                "phases_cancelled": attribution["phases_cancelled"],
                "phases_completed": attribution["phases_completed"],
            },
        )
        # ``REQUEST_BUDGET_EXCEEDED`` rather than the status-derived
        # ``UPSTREAM_TIMEOUT``: this deadline is ours and no upstream reported
        # anything, the same distinction the middleware's 504 draws.
        raise HTTPException(
            status_code=504,
            detail=coded_detail(
                REQUEST_BUDGET_EXCEEDED,
                "interview exceeded its request budget"
                + (f" while running {attribution['phase']}" if attribution["phase"] else "")
                + "; window not consumed",
                budget_seconds=interview_budget,
                elapsed_seconds=elapsed,
                path="/api/v1/interview/submit",
                **attribution,
            ),
        )

    if result["status"] == "failed":
        # Whole window failed to persist, or no LLM answered (M-50):
        # watermark NOT advanced; the plugin must NOT prune. 500 (origin
        # error, not 502 — proxies/ALBs rewrite 502 and strip the JSON body)
        # → the command retries next tick (caller checks >= 400).
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
