"""Internal, writer-only admission and durable retry references. Never stores text."""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import case, delete, func, literal, select, text, true, update
from sqlalchemy.dialects.postgresql import insert

from common.models.background_work import (
    BackgroundModelBudget as Budget,
)
from common.models.background_work import (
    BackgroundModelReservation as Reservation,
)
from common.models.background_work import (
    BackgroundModelWork as Work,
)
from core_storage_api.config import settings
from core_storage_api.routers._validation import _require
from core_storage_api.services.postgres_service import get_session

router = APIRouter(prefix="/background-work", tags=["BackgroundWork"])
logger = logging.getLogger(__name__)
MAX_REPLAY_ATTEMPTS = 8


def _due_tenants_query(after_tenant: str | None, limit: int):
    """Seek the next due tenant, skipping its whole backlog before the next probe.

    The C-collated partial index supports each ordered LIMIT 1 probe. The
    recursion emits at most one page; only those tenant ids reach the final
    sort, rather than every due reference. A stable statement time also lets
    PostgreSQL use the timestamp predicates as index conditions.
    """
    tenant_key = Work.tenant_id.collate("C").label("tenant_id")
    due = (
        Work.terminal_reason.is_(None),
        Work.available_at <= func.statement_timestamp(),
        Work.lease_until <= func.statement_timestamp(),
    )
    first = select(tenant_key, literal(1).label("position")).where(*due)
    if after_tenant is not None:
        first = first.where(tenant_key > after_tenant)
    tenants = first.order_by(tenant_key).limit(1).cte("due_tenants", recursive=True)
    next_tenant = (
        select(tenant_key)
        .where(*due, tenant_key > tenants.c.tenant_id)
        .order_by(tenant_key)
        .limit(1)
        .correlate(tenants)
        .lateral("next_tenant")
    )
    tenants = tenants.union_all(
        select(next_tenant.c.tenant_id, (tenants.c.position + 1).label("position"))
        .select_from(tenants.join(next_tenant, true()))
        .where(tenants.c.position < limit)
    )
    return select(tenants.c.tenant_id).order_by(tenants.c.tenant_id)


async def due_tenants(after_tenant: str | None, limit: int) -> list[str]:
    """Bounded discovery for the existing internal tenant-list route."""
    async with get_session() as session:
        return list((await session.execute(_due_tenants_query(after_tenant, limit))).scalars().all())


def _log_exhausted(tenant_id: str, kind: str, memory_id: UUID) -> None:
    logger.warning(
        "background retry exhausted",
        extra={
            "reason": "retry_exhausted",
            "tenant_id": tenant_id,
            "kind": kind,
            "memory_id": str(memory_id),
        },
    )


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("tenant_id must not be blank")
    return value


Tenant = Annotated[str, StringConstraints(min_length=1, max_length=200), AfterValidator(_nonblank)]
Kind = Literal["extraction", "contradiction_content", "contradiction_entity", "enrichment"]


def _validated[Model: BaseModel](model: type[Model], data: dict) -> Model:
    try:
        return model.model_validate(data)
    except ValidationError:
        # A validation error must not reflect work/request content in its detail.
        raise HTTPException(status_code=422, detail="Invalid background work request") from None


class Reserve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: Tenant
    attempt_id: UUID
    operation: str = Field(min_length=1, max_length=200)


@router.post("/reserve")
async def reserve(request: Request) -> dict:
    data = await request.json()
    body = _validated(Reserve, data)
    tenant_id = _require(data, "tenant_id")
    async with get_session() as session:
        # Lock one tenant row before inspecting either its window or replay
        # ledger. A replica read or per-process bucket cannot authorize calls.
        await session.execute(
            insert(Budget)
            .values(
                tenant_id=tenant_id,
                window_start=func.clock_timestamp(),
                used=0,
            )
            .on_conflict_do_nothing()
        )
        budget = (
            await session.execute(
                select(Budget)
                .where(
                    Budget.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        await session.execute(
            delete(Reservation).where(
                Reservation.tenant_id == tenant_id,
                Reservation.expires_at <= now,
            )
        )
        replay = await session.get(Reservation, (tenant_id, body.attempt_id))
        if replay is not None:
            if replay.released:
                return {"allowed": False, "retry_after_seconds": 60, "reason": "reservation_released"}
            return {"allowed": True, "retry_after_seconds": 0}
        # Count the last 60 seconds, not a wall-clock minute: adjacent bursts
        # cannot get two windows' allowance. The tenant row serializes count +
        # insert, and released reservations still count as spent attempts.
        recent, oldest = (
            await session.execute(
                select(
                    func.count(),
                    func.min(Reservation.admitted_at),
                ).where(
                    Reservation.tenant_id == tenant_id,
                    Reservation.admitted_at > now - timedelta(seconds=60),
                )
            )
        ).one()
        budget.used = recent
        budget.window_start = oldest or now
        if recent >= settings.background_llm_attempts_per_minute:
            return {
                "allowed": False,
                "retry_after_seconds": max(
                    1, math.ceil((budget.window_start + timedelta(seconds=60) - now).total_seconds())
                ),
            }
        active = (
            await session.execute(
                select(func.count())
                .select_from(Reservation)
                .where(
                    Reservation.tenant_id == tenant_id,
                    Reservation.released.is_(False),
                    Reservation.expires_at > now,
                )
            )
        ).scalar_one()
        if active >= settings.background_llm_concurrency:
            return {"allowed": False, "retry_after_seconds": 1, "reason": "tenant_slots_busy"}
        inserted = (
            await session.execute(
                insert(Reservation)
                .values(
                    tenant_id=tenant_id,
                    attempt_id=body.attempt_id,
                    admitted_at=now,
                    expires_at=now + timedelta(minutes=15),
                    released=False,
                )
                .on_conflict_do_nothing()
                .returning(Reservation.attempt_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            # Release may insert its tombstone after the replay lookup above.
            return {"allowed": False, "retry_after_seconds": 60, "reason": "reservation_released"}
        budget.used += 1
        return {"allowed": True, "retry_after_seconds": 0}


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: Tenant
    attempt_id: UUID


@router.post("/release")
async def release(request: Request) -> dict:
    data = await request.json()
    body = _validated(Release, data)
    tenant_id = _require(data, "tenant_id")
    async with get_session() as session:
        await session.execute(
            insert(Reservation)
            .values(
                tenant_id=tenant_id,
                attempt_id=body.attempt_id,
                admitted_at=None,
                released=True,
                expires_at=func.clock_timestamp() + text("interval '15 minutes'"),
            )
            .on_conflict_do_update(
                index_elements=[Reservation.tenant_id, Reservation.attempt_id],
                set_={"released": True, "expires_at": func.clock_timestamp() + text("interval '15 minutes'")},
            )
        )
    return {"released": True}


class Defer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: Tenant
    kind: Kind
    memory_id: UUID
    retry_after_seconds: int = Field(default=60, ge=1, le=3600)


@router.post("/defer")
async def defer(request: Request) -> dict:
    data = await request.json()
    body = _validated(Defer, data)
    tenant_id = _require(data, "tenant_id")
    async with get_session() as session:
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        # One reference per memory/path, not one row per trigger. A generation
        # prevents completion of an older claim from deleting a newer deferral.
        await session.execute(
            insert(Work)
            .values(
                tenant_id=tenant_id,
                kind=body.kind,
                memory_id=body.memory_id,
                available_at=now + timedelta(seconds=body.retry_after_seconds),
                lease_until=now,
            )
            .on_conflict_do_update(
                index_elements=[Work.tenant_id, Work.kind, Work.memory_id],
                set_={
                    "available_at": now + timedelta(seconds=body.retry_after_seconds),
                    "lease_until": now,
                    "generation": Work.generation + 1,
                    "terminal_reason": None,
                    "attempts": case((Work.terminal_reason.is_not(None), 0), else_=Work.attempts),
                },
            )
        )
    return {"queued": True}


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: Tenant
    limit: int = Field(default=1, ge=1, le=8)


@router.post("/claim")
async def claim(request: Request) -> dict:
    data = await request.json()
    body = _validated(Claim, data)
    tenant_id = _require(data, "tenant_id")
    async with get_session() as session:
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        works = (
            (
                await session.execute(
                    select(Work)
                    .where(
                        Work.tenant_id == tenant_id,
                        Work.terminal_reason.is_(None),
                        Work.available_at <= now,
                        Work.lease_until <= now,
                    )
                    .order_by(Work.available_at, Work.kind, Work.memory_id)
                    .limit(body.limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        items = []
        exhausted = []
        for work in works:
            if work.attempts >= MAX_REPLAY_ATTEMPTS:
                # Also bound claims whose processor crashed before /finish.
                work.terminal_reason = "retry_exhausted"
                exhausted.append((work.kind, work.memory_id))
                continue
            work.attempts += 1
            work.generation += 1
            work.lease_until = now + timedelta(minutes=15)
            items.append(
                {
                    "tenant_id": tenant_id,
                    "kind": work.kind,
                    "memory_id": work.memory_id,
                    "generation": work.generation,
                    "attempts": work.attempts,
                }
            )
    for kind, memory_id in exhausted:
        _log_exhausted(tenant_id, kind, memory_id)
    return {"items": items}


class Finish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: Tenant
    kind: Kind
    memory_id: UUID
    generation: int = Field(ge=1)
    retry: bool = False
    deferred: bool = False
    retry_after_seconds: int = Field(default=1, ge=1, le=3600)


@router.post("/finish")
async def finish(request: Request) -> dict:
    data = await request.json()
    body = _validated(Finish, data)
    tenant_id = _require(data, "tenant_id")
    async with get_session() as session:
        predicate = (
            Work.tenant_id == tenant_id,
            Work.kind == body.kind,
            Work.memory_id == body.memory_id,
            Work.generation == body.generation,
            Work.terminal_reason.is_(None),
        )
        work = (await session.execute(select(Work).where(*predicate).with_for_update())).scalar_one_or_none()
        if work is None:
            return {"finished": True}
        # Claims count provisionally so crashed processors still exhaust. A
        # confirmed scheduling refusal refunds only this matching claim.
        attempts = max(0, work.attempts - 1) if body.deferred else work.attempts
        exhausted = body.retry and attempts >= MAX_REPLAY_ATTEMPTS
        if body.retry and not exhausted:
            delay = (
                body.retry_after_seconds
                if body.deferred
                else max(body.retry_after_seconds, min(3600, 60 * 2 ** max(0, attempts - 1)))
            )
            await session.execute(
                update(Work)
                .where(*predicate)
                .values(
                    attempts=attempts,
                    # Consume this completion exactly once: a repeated finish
                    # must not refund another failure or postpone work again.
                    generation=work.generation + 1,
                    available_at=func.clock_timestamp() + timedelta(seconds=delay),
                    lease_until=func.clock_timestamp(),
                )
            )
        elif exhausted:
            work.terminal_reason = "retry_exhausted"
        else:
            await session.execute(delete(Work).where(*predicate))
    if exhausted:
        _log_exhausted(tenant_id, body.kind, body.memory_id)
    return {"finished": True}
