"""Transparency endpoints for the anonymous heartbeat.

``GET /telemetry`` shows whether the heartbeat runs here, why not if it does
not, and exactly what the next send would contain. ``POST /telemetry/rotate``
replaces the deployment id and token so an operator can start over. Both use
the normal API-key auth; see ``docs/telemetry.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.heartbeat import identity
from core_api.heartbeat.policy import DISABLE_HINT, evaluate
from core_api.heartbeat.sender import get_decision, get_sender

logger = logging.getLogger(__name__)
router = APIRouter(tags=["System"])


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


@router.get("/telemetry")
async def telemetry_status(_auth: AuthContext = Depends(get_auth_context)) -> dict[str, Any]:
    """What the heartbeat does on this server, and what it would send next."""
    # Prefer the decision the lifespan recorded; fall back to evaluating now
    # (tests build the app without running the lifespan).
    decision = get_decision() or evaluate(settings)
    sender = get_sender()
    body: dict[str, Any] = {
        "enabled": decision.enabled,
        "reason": decision.reason,
        "deployment_id": None,
        "endpoint": settings.caura_telemetry_url,
        "last_sent_at": None,
        "last_status": None,
        "next_send_at": None,
        "payload_preview": None,
        "disable": DISABLE_HINT,
    }
    if sender is None:
        # Off means zero work: no identity is generated and no counts are
        # read on a disabled install, so there is nothing to preview.
        return body
    body["last_sent_at"] = _iso(sender.last_sent_at)
    body["last_status"] = sender.last_status
    body["next_send_at"] = _iso(sender.next_send_at)
    try:
        body["payload_preview"] = await sender.preview()
        body["deployment_id"] = sender.deployment_id
    except Exception:
        logger.warning("telemetry preview failed", exc_info=True)
        body["deployment_id"] = sender.deployment_id
    return body


@router.post("/telemetry/rotate")
async def telemetry_rotate(auth: AuthContext = Depends(get_auth_context)) -> dict[str, Any]:
    """Generate a new deployment id and token; the old id goes stale on the collector.

    An operator action: it writes the deployment row, so the demo sandbox and
    read-only credentials are refused, and an agent-scoped credential cannot
    reset the install's identity from inside a session.
    """
    auth.enforce_read_only()
    auth.enforce_not_agent_credential()
    ident = await identity.rotate(get_storage_client())
    sender = get_sender()
    if sender is not None:
        sender.deployment_id = ident.deployment_id
    return {"deployment_id": ident.deployment_id, "rotated": True}
