"""Correction notices on recall: the fallback when the bus cannot tell an agent (CP-13).

Correction propagation (``docs/plans/correction-propagation.md``). When a
person corrects a memory, the agents that read the old version are told over
the Caura bus. An agent the bus cannot reach (``channel=recall``: standalone
OSS, an unknown agent, the bus disabled) or could not deliver to (``failed``, or
the legacy ``unreachable``) hears about it on its next recall instead: every recall or
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
* The first response that hands the agent a notice marks it ``delivered``
  (``notified_at`` set), so the person sees "seen on recall" the way a bus
  claim shows for the bus. Fire-and-forget after the response, one UPDATE
  that matches nothing on a repeat, only for notices not marked yet; a
  failure is logged. A search that fails before :func:`finish_notices` marks
  nothing.

``old_summary`` is shown here only to the agent the notice is for, which is a
recipient by construction (``common.events.memory_corrected``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from core_api.audit_actor import audit_principal
from core_api.clients.storage_client import get_storage_client
from core_api.tasks import track_task

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


#: What a lookup found: the compact notices, and the ids of those no recall handed over yet.
_Owed = tuple[list[dict], list[str]]


async def _lookup(tenant_id: str, agent_id: str, *, surface: Literal["mcp", "rest"]) -> _Owed:
    try:
        rows = await get_storage_client().list_correction_notices(tenant_id, agent_id, limit=MAX_NOTICES)
    except Exception:
        logger.warning("correction notices not read", exc_info=True)
        return [], []
    rows = rows[:MAX_NOTICES]
    ask = _ASK[surface]
    notices = [
        {
            "correction_id": r["correction_id"],
            "memory_id": r["memory_id"],
            "old_summary": r.get("old_summary"),
            "new_summary": r.get("new_summary"),
            "corrected_at": r["corrected_at"],
            "ask": ask.replace("{id}", r["correction_id"]),
        }
        for r in rows
    ]
    return notices, [r["correction_id"] for r in rows if not r.get("notified_at")]


async def correction_notices(
    tenant_id: str | None, agent_id: str | None, *, surface: Literal["mcp", "rest"]
) -> list[dict]:
    """The compact notices ``agent_id`` is owed on this recall; ``[]`` when none or on error."""
    if not tenant_id or not agent_id or _is_person():
        return []
    notices, _ = await _lookup(tenant_id, str(agent_id), surface=surface)
    return notices


async def _mark_delivered(tenant_id: str, agent_id: str, correction_ids: list[str]) -> None:
    try:
        await get_storage_client().mark_correction_notices_delivered(tenant_id, agent_id, correction_ids)
    except Exception:
        logger.warning("correction notice delivery not recorded", exc_info=True)


class NoticesTask:
    """A lookup started beside the search, and who it is for."""

    def __init__(self, tenant_id: str, agent_id: str, task: asyncio.Task[_Owed]) -> None:
        self.tenant_id, self.agent_id, self.task = tenant_id, agent_id, task


def start_notices(
    tenant_id: str | None, agent_id: str | None, *, surface: Literal["mcp", "rest"]
) -> NoticesTask | None:
    """Start the lookup beside the search; finish it after. ``None`` when there is nothing to look up.

    The task never raises (:func:`_lookup` swallows), so a search that fails
    and leaves it unawaited leaves no unretrieved exception behind.
    """
    if not tenant_id or not agent_id or _is_person():
        return None
    agent = str(agent_id)
    return NoticesTask(tenant_id, agent, asyncio.create_task(_lookup(tenant_id, agent, surface=surface)))


async def finish_notices(started: NoticesTask | None) -> list[dict]:
    """The notices :func:`start_notices` looked up, for the response; ``[]`` for none.

    Called only while the response is built, so this is where the notices
    count as handed over: the ones no recall showed before are marked
    ``delivered`` after the response.
    """
    if started is None:
        return []
    notices, unseen = await started.task
    if unseen:
        track_task(_mark_delivered(started.tenant_id, started.agent_id, unseen))
    return notices
