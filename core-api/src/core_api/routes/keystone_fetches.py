"""Service-only matching and retention of install-bound rule fetch records."""

import asyncio
import logging
import random

from fastapi import APIRouter, Depends

from common.keystone_fetches import FetchMatchRequest
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.services.tenants import list_active_tenant_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/keystone-fetches", include_in_schema=False)

# Each storage call deletes at most PURGE_BATCH rows of one tenant. A tenant
# gets at most PURGE_MAX_BATCHES calls per run; anything left waits for the
# next day's run. Tenants are purged PURGE_CONCURRENCY at a time.
PURGE_BATCH = 1000
PURGE_MAX_BATCHES = 10
PURGE_CONCURRENCY = 4
# A run stops at this share of core-api's request budget, so it answers with
# its counts before RequestTimeoutMiddleware would cut it into a bare 504.
PURGE_BUDGET_SHARE = 0.8


@router.post("/match")
async def match_fetches(body: FetchMatchRequest, auth: AuthContext = Depends(get_auth_context)) -> dict:
    # Same system-admin application identity used by the enterprise core-api
    # client; Cloud Run additionally enforces its existing service ID token.
    # Org admins, install credentials and agent credentials cannot use it.
    auth.enforce_admin()
    try:
        async with asyncio.timeout(5):
            return await get_storage_client().match_keystone_fetches(body.model_dump(mode="json"))
    except Exception:
        logger.warning("keystone fetch matching unavailable")
        return {"results": [{"status": "evidence_unavailable"} for _ in body.checks]}


@router.post("/purge-expired")
async def purge_expired_fetches(auth: AuthContext = Depends(get_auth_context)) -> dict:
    """Delete fetch records past retention, tenant by tenant.

    Run daily by core-operations. Storage refuses an unscoped purge, so this
    walks the tenants with live memories. A tenant with none keeps its
    expired rows until it writes again or the org is deleted; matching
    ignores expired rows either way.

    The run stops at PURGE_BUDGET_SHARE of core-api's request budget: it
    starts no tenant after that, cuts any call still running, and reports
    those tenants as unfinished. Tenants are shuffled each run, so the ones a
    cut run misses are not the same ones every day.
    """
    auth.enforce_admin()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.request_timeout_seconds * PURGE_BUDGET_SHARE
    sc = get_storage_client()
    tenants = list(await list_active_tenant_ids())
    random.shuffle(tenants)
    gate = asyncio.Semaphore(PURGE_CONCURRENCY)
    totals = {"deleted": 0, "failed_tenants": 0, "unfinished_tenants": 0}

    async def purge_tenant(tenant_id: str) -> None:
        async with gate:
            if loop.time() >= deadline:
                totals["unfinished_tenants"] += 1
                return
            try:
                async with asyncio.timeout_at(deadline):
                    for _ in range(PURGE_MAX_BATCHES):
                        result = await sc.purge_expired_keystone_fetches(tenant_id, PURGE_BATCH)
                        batch = int(result.get("deleted", 0))
                        totals["deleted"] += batch
                        if batch < PURGE_BATCH:
                            break
            except TimeoutError:
                totals["unfinished_tenants"] += 1
            except Exception:
                logger.warning("keystone fetch purge failed for one tenant", exc_info=True)
                totals["failed_tenants"] += 1

    await asyncio.gather(*(purge_tenant(t) for t in tenants))
    return {"tenants": len(tenants), **totals}
