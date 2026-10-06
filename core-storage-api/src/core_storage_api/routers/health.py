"""Health check endpoints."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from core_storage_api.config import settings
from core_storage_api.database.init import get_engine, get_read_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

# Bound on the readiness query, inside any probe's own timeout: a database that
# accepts the connection and never answers must not hold the probe open.
_READY_TIMEOUT_SECONDS = 2.0


def _pool_is_busy(engine: AsyncEngine) -> bool:
    """No idle connection, and at least one is out serving a request."""
    pool = engine.pool
    return isinstance(pool, QueuePool) and pool.checkedin() == 0 and pool.checkedout() > 0


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    if not settings.core_storage_shared_secret.get_secret_value():
        raise HTTPException(
            status_code=503,
            detail="storage service credentials not configured",
        )
    # Ready only when the database this role serves from answers (M-59). On the
    # reader role nothing at startup touches it and the engines are lazy, so an
    # unreachable DSN passed this probe and failed every data request; on any
    # role a database lost after boot never failed it. /healthz stays shallow,
    # so a lost database takes the instance out of rotation without a restart.
    engine = get_read_engine() if settings.core_storage_role == "reader" else get_engine()
    try:
        async with asyncio.timeout(_READY_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as exc:
        # A probe that timed out queueing behind requests found a busy pod, not
        # a lost database: failing it would take every busy pod out of rotation
        # at once under load. The probe's own connection is checked back in by
        # now, so a timeout on an idle pool still fails. A database that hangs
        # on a busy pod passes, as every probe did before M-59.
        if isinstance(exc, TimeoutError | PoolTimeoutError) and _pool_is_busy(engine):
            logger.info("readyz: every pooled connection is serving a request")
            return {"status": "ok"}
        # The class only: a driver's message can carry the DSN's host or user.
        logger.warning("readyz: database unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable") from None
    return {"status": "ok"}
