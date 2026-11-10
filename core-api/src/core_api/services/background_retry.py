"""Persist minimal deferred references and re-read live state before retrying."""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from common.background_work import BackgroundWorkDeferred
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings

logger = logging.getLogger(__name__)


@dataclass
class _ReplayState:
    claim: tuple[str, str, str]
    active: bool = True
    deferred: bool = False
    retry_after_seconds: int = 1


_replay_state: ContextVar[_ReplayState | None] = ContextVar("background_replay", default=None)


async def defer_work(sc, tenant_id: str, memory_id, kind: str, exc: BackgroundWorkDeferred) -> None:
    state = _replay_state.get()
    if state is not None and state.active and state.claim == (tenant_id, str(memory_id), kind):
        state.deferred = True
        state.retry_after_seconds = max(state.retry_after_seconds, exc.retry_after_seconds)
        # The claim already is durable. Finish reschedules this generation with
        # its admission delay; another upsert here would invalidate the claim.
        return
    try:
        result = await asyncio.wait_for(
            sc.defer_background_work(
                {
                    "tenant_id": tenant_id,
                    "memory_id": str(memory_id),
                    "kind": kind,
                    "retry_after_seconds": exc.retry_after_seconds,
                }
            ),
            timeout=2,
        )
        if not isinstance(result, dict) or result.get("queued") is not True:
            raise RuntimeError("retry was not acknowledged")
        logger.info(
            "background work queued for retry",
            extra={
                "tenant_id": tenant_id,
                "kind": kind,
                "reason": exc.reason,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )
    except Exception:
        # A committed foreground write stays successful. This lost retry is
        # explicit and contains no content/traceback; provider work stays closed.
        logger.error(
            "background retry could not be persisted",
            extra={
                "tenant_id": tenant_id,
                "kind": kind,
                "reason": "retry_unavailable",
            },
        )


async def retry_one(work: dict) -> None:
    state = _ReplayState((work["tenant_id"], str(work["memory_id"]), work["kind"]))
    token = _replay_state.set(state)
    try:
        await _retry_one(work, state)
    finally:
        state.active = False
        _replay_state.reset(token)


async def _retry_one(work: dict, state: _ReplayState) -> None:
    sc = get_storage_client()
    tenant, mid, kind = work["tenant_id"], str(work["memory_id"]), work["kind"]
    retry = True
    failed = True
    deferred = False
    try:
        if await sc.is_tenant_suppressed(tenant):
            retry = False
            return
        row = await sc.get_memory(mid, tenant, read=False)
        if not row or row.get("deleted_at") or row.get("status") in {"deleted", "quarantined"}:
            retry = False
            return
        fleet = row.get("fleet_id")
        if kind == "extraction":
            from core_api.services.entity_extraction_worker import process_entity_extraction

            outcome = await process_entity_extraction(
                UUID(mid),
                tenant,
                fleet,
                row["agent_id"],
                row["content"],
                row["memory_type"],
            )
            failed = outcome == "failed"
            deferred = outcome == "deferred"
            retry = state.deferred or outcome in {"deferred", "failed"}
        elif kind == "enrichment":
            from core_api.services.memory_service import _schedule_enrich_or_inline
            from core_api.services.organization_settings import resolve_config
            from core_api.services.release_replay import release_pins
            from core_api.services.system_metadata import caller_owned_keys, extract_system_metadata

            metadata = row.get("metadata_") or row.get("metadata") or {}
            await _schedule_enrich_or_inline(
                UUID(mid),
                row["content"],
                tenant,
                fleet,
                row["agent_id"],
                await resolve_config(tenant),
                agent_provided_fields=release_pins(row, extract_system_metadata(metadata) or {}),
                caller_owned_metadata_keys=sorted(caller_owned_keys(metadata)) or None,
                run_governance_remediation=True,
            )
            failed = False
            retry = state.deferred
        else:
            from core_api.services.contradiction import Trigger, run_contradiction_detection

            if kind == "contradiction_entity":
                await run_contradiction_detection(UUID(mid), tenant, fleet, trigger=Trigger.ENTITY)
            else:
                from core_api.services.memory_service import _content_hash

                embedding = await sc.find_embedding_by_content_hash(
                    tenant, _content_hash(tenant, fleet, row["content"])
                )
                if embedding is None:
                    # Waiting for the embedding is scheduling too; it must not
                    # exhaust the provider-failure allowance.
                    failed, deferred = False, True
                    state.retry_after_seconds = max(60, state.retry_after_seconds)
                    return  # embedding may still be pending; keep the reference
                await run_contradiction_detection(
                    UUID(mid),
                    tenant,
                    fleet,
                    trigger=Trigger.EMBED,
                    content=row["content"],
                    embedding=embedding,
                    new_memory=row,
                )
            # Matching admission refusals retain this durable claim. External
            # deferrals advance generation and remain protected from this finish.
            failed = False
            retry = state.deferred
    except asyncio.CancelledError:
        raise  # leave the lease to expire; never acknowledge interrupted work
    except BackgroundWorkDeferred as exc:
        failed, deferred, retry = False, True, True
        state.retry_after_seconds = max(state.retry_after_seconds, exc.retry_after_seconds)
    except Exception:
        failed = True
        logger.warning("background retry postponed", extra={"tenant_id": tenant, "kind": kind})
    finally:
        # Freeze the decision before awaiting storage: child tasks inherit the
        # context, but must persist normally once completion has started.
        state.active = False
        # A process cancellation leaves the durable lease, rather than deleting
        # work whose outcome was never known.
        task = asyncio.current_task()
        if task is None or not task.cancelling():
            try:
                await sc.finish_background_work(
                    {
                        "tenant_id": tenant,
                        "memory_id": mid,
                        "kind": kind,
                        "generation": work["generation"],
                        "retry": retry or state.deferred,
                        "deferred": (deferred or state.deferred) and not failed,
                        "retry_after_seconds": min(3600, state.retry_after_seconds),
                    }
                )
            except Exception:
                logger.warning(
                    "background retry lease left to expire",
                    extra={
                        "tenant_id": tenant,
                        "kind": kind,
                        "reason": "finish_unavailable",
                    },
                )


async def retry_loop() -> None:
    cursor: str | None = None
    inflight: set[asyncio.Task] = set()
    busy: dict[str, int] = {}

    async def run(work: dict) -> None:
        try:
            await retry_one(work)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("background retry interrupted", extra={"tenant_id": work["tenant_id"]})
        finally:
            tenant = work["tenant_id"]
            busy[tenant] -= 1
            if not busy[tenant]:
                del busy[tenant]

    try:
        while True:
            try:
                if len(inflight) < 16:
                    sc = get_storage_client()
                    tenants = await sc.list_due_background_tenants(after_tenant=cursor, limit=32)
                    if not tenants and cursor is not None:
                        cursor = None
                        tenants = await sc.list_due_background_tenants(after_tenant=None, limit=32)
                    capacity = 16 - len(inflight)
                    per_tenant = settings.background_retry_per_tenant_concurrency
                    batch = [tenant for tenant in tenants if busy.get(tenant, 0) < per_tenant][:capacity]
                    # Give each eligible tenant a turn before allocating more
                    # slots to it. A lone tenant can drain ordinary bulk bursts
                    # promptly without filling the entire global pool.
                    limits = dict.fromkeys(batch, 1)
                    remaining = capacity - len(batch)
                    for _ in range(per_tenant - 1):
                        for tenant in batch:
                            if remaining and limits[tenant] + busy.get(tenant, 0) < per_tenant:
                                limits[tenant] += 1
                                remaining -= 1
                    if tenants:
                        cursor = batch[-1] if batch else tenants[-1]
                    claims = await asyncio.gather(
                        *(sc.claim_background_work(tenant, limit=limits[tenant]) for tenant in batch),
                        return_exceptions=True,
                    )
                    for rows in claims:
                        if isinstance(rows, BaseException):
                            if isinstance(rows, asyncio.CancelledError):
                                raise rows
                            logger.warning("background retry claim unavailable")
                            continue
                        for work in rows:
                            tenant = work["tenant_id"]
                            busy[tenant] = busy.get(tenant, 0) + 1
                            task = asyncio.create_task(run(work))
                            inflight.add(task)
                            task.add_done_callback(inflight.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("background retry authority unavailable")
            # A slow tenant holds only its configured share of the pool.
            await asyncio.sleep(settings.background_retry_poll_seconds)
    finally:
        for task in inflight:
            task.cancel()
        await asyncio.gather(*inflight, return_exceptions=True)
