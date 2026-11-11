"""Service-only, read-only matching of install-bound rule fetch records."""

import asyncio
import logging

from fastapi import APIRouter, Depends

from common.keystone_fetches import FetchMatchRequest
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/keystone-fetches", include_in_schema=False)


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
