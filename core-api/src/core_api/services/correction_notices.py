"""Correction notices on recall: the fallback when the bus cannot tell an agent (CP-13).

Correction propagation (``docs/plans/correction-propagation.md``). When a
person corrects a memory, the agents that read the old version are told over
the Caura bus. An agent the bus cannot reach (``channel=recall``: standalone
OSS, an unknown agent, the bus disabled) or could not deliver to (``failed``,
``unreachable``) hears about it on its next recall instead: every recall or
search response for that agent carries ``correction_notices``, whatever the
query, until the agent confirms (``POST /corrections/{id}/confirm``, MCP
``caura_manage op=confirm_correction``) or the correction is 7 days old.

The rules, all here so the call sites stay two lines:

* At most :data:`MAX_NOTICES`, newest first, compact: no recipient list, no
  detail, one ``ask`` line.
* No agent identity, or a signed-in person reading as an agent, looks nothing
  up: as for exposure, only an agent reading is a recipient.
* The lookup runs beside the search (:func:`start_notices`), one indexed
  storage read. A failure is logged and reads as no notices: a notice must
  never slow or break a recall.
* No notices, no key: the response is byte-for-byte what it was.

``old_summary`` is shown here only to the agent the notice is for, which is a
recipient by construction (``common.events.memory_corrected``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from core_api.audit_actor import audit_principal
from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)

#: Notices on one response (WAVE3 contract).
MAX_NOTICES = 3

_ASK = {
    "mcp": "Stop relying on the old version; then confirm with caura_manage op=confirm_correction.",
    "rest": "Stop relying on the old version; then confirm with POST /api/v1/corrections/{id}/confirm.",
}


def _is_person() -> bool:
    principal = audit_principal.get() or {}
    return principal.get("credential_kind") == "person"


async def correction_notices(
    tenant_id: str | None, agent_id: str | None, *, surface: Literal["mcp", "rest"]
) -> list[dict]:
    """The compact notices ``agent_id`` is owed on this recall; ``[]`` when none or on error."""
    if not tenant_id or not agent_id or _is_person():
        return []
    try:
        rows = await get_storage_client().list_correction_notices(tenant_id, str(agent_id), limit=MAX_NOTICES)
    except Exception:
        logger.warning("correction notices not read", exc_info=True)
        return []
    ask = _ASK[surface]
    return [
        {
            "correction_id": r["correction_id"],
            "memory_id": r["memory_id"],
            "old_summary": r.get("old_summary"),
            "new_summary": r.get("new_summary"),
            "corrected_at": r["corrected_at"],
            "ask": ask.replace("{id}", r["correction_id"]),
        }
        for r in rows[:MAX_NOTICES]
    ]


def start_notices(
    tenant_id: str | None, agent_id: str | None, *, surface: Literal["mcp", "rest"]
) -> asyncio.Task[list[dict]] | None:
    """Start the lookup beside the search; await the task after it. ``None`` when there is nothing to look up.

    The task never raises (:func:`correction_notices` swallows), so a search
    that fails and leaves it unawaited leaves no unretrieved exception behind.
    """
    if not tenant_id or not agent_id or _is_person():
        return None
    return asyncio.create_task(correction_notices(tenant_id, agent_id, surface=surface))


async def finish_notices(task: asyncio.Task[list[dict]] | None) -> list[dict]:
    """The notices :func:`start_notices` looked up; ``[]`` for no task."""
    return await task if task is not None else []
