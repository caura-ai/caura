"""Handoff baton notices on recall: the fallback for a receiver not on the bus (HB-09).

Handoff batons (``docs/plans/handoff-batons.md``), D4. A baton reaches a
receiver on the Caura bus through ``peer wait``. A receiver that is not
listening there (an OpenClaw agent, a session that only recalls) hears about it
on its next recall instead: every recall or search response for that agent
carries ``baton_notices``, whatever the query, until it accepts the baton
(``POST /batons/{id}/accept``, MCP ``caura_manage op=accept_baton``), takes it
up on the bus (ack or reply), or the baton is 7 days old.

The rules, all here so the call sites stay two lines, as for correction notices
(``services/correction_notices.py``):

* At most :data:`MAX_NOTICES`, newest first, compact: who sent it, the goal
  (cut to :data:`GOAL_CHARS`), when, and one ``ask`` line. The whole baton is
  read with :func:`read_as_agent` (``GET /batons/{id}``, MCP ``caura_manage
  op=read_baton``), or from the bus by an agent on it.
* No agent identity, or a signed-in person reading as an agent, looks nothing
  up: only an agent can receive a baton.
* The lookup runs beside the search (:func:`start_notices`), one indexed
  storage read. A failure is logged and reads as no notices.
* No notices, no key: the response is byte-for-byte what it was.
* The bus owns the delivery state. After a response that showed a notice,
  core-api asks the bus whether that receiver acked or replied (at most every
  :data:`BUS_CHECK_S` seconds per row, fire-and-forget) and copies a taken-up
  state into ``baton_recipient``, so the notice stops. Standalone OSS has no
  bus, and nothing writes the rows a recall reads: the lookup finds nothing.

Accepting marks the row ``accepted`` and, when a bus is configured, asks the
bus to ack the receiver's delivery, so the sender sees ``accepted`` in the
baton status. No LLM calls.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from fastapi import HTTPException

from core_api.agent_ids import AgentIdentity
from core_api.audit_actor import audit_principal
from core_api.clients.identity_token import fetch_auth_header
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.services.agent_service import memory_reader
from core_api.services.correction_sender import _bus_secret, bus_configured
from core_api.services.interview_service import BATON_COLLECTION
from core_api.tasks import track_task

logger = logging.getLogger(__name__)

#: Notices on one response (wave-2 contract).
MAX_NOTICES = 2
#: The goal as a notice shows it; the bus serves the whole baton.
GOAL_CHARS = 300
#: A notice's bus state is read again at most this often.
BUS_CHECK_S = 30
BUS_TIMEOUT_S = 5.0

_ASK = {
    "mcp": "Read it with caura_manage op=read_baton, tag what you write for it with "
    "metadata.baton_id={id}, then accept with caura_manage op=accept_baton.",
    "rest": "Read it with GET /api/v1/batons/{id}, tag what you write for it with "
    "metadata.baton_id={id}, then accept with POST /api/v1/batons/{id}/accept.",
}


def _is_person() -> bool:
    principal = audit_principal.get() or {}
    return principal.get("credential_kind") == "person"


def _goal(goal: str) -> str:
    return goal if len(goal) <= GOAL_CHARS else goal[: GOAL_CHARS - 1].rstrip() + "…"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


#: What a lookup found: the compact notices, and the rows whose bus state is due a check.
_Found = tuple[list[dict], list[dict]]


async def _lookup(tenant_id: str, agent_id: str, *, surface: Literal["mcp", "rest"]) -> _Found:
    try:
        rows = await get_storage_client().list_baton_notices(tenant_id, agent_id, limit=MAX_NOTICES)
    except Exception:
        logger.warning("baton notices not read", exc_info=True)
        return [], []
    rows = rows[:MAX_NOTICES]
    ask = _ASK[surface]
    notices = [
        {
            "baton_id": r["baton_id"],
            "from_agent": r["from_agent"],
            "goal": _goal(r["goal"]),
            "created_at": r["created_at"],
            "ask": ask.replace("{id}", r["baton_id"]),
        }
        for r in rows
    ]
    stale = datetime.now(UTC) - timedelta(seconds=BUS_CHECK_S)
    due = [
        r
        for r in rows
        if r.get("message_id") and ((checked := _parse(r.get("bus_checked_at"))) is None or checked < stale)
    ]
    return notices, due


async def _bus_headers() -> dict[str, str]:
    """The system secret, and a Cloud Run ID token for the bus when one is available."""
    headers = {"X-Caura-System-Secret": _bus_secret()}
    base = settings.collaboration_api_url.strip().rstrip("/")
    if base.startswith("https://"):
        token = (await fetch_auth_header(base)).get("Authorization")
        if token:
            headers["X-Serverless-Authorization"] = token
    return headers


def _bus_url(baton_id: str, suffix: str = "") -> str:
    return f"{settings.collaboration_api_url.strip().rstrip('/')}/api/v1/bus/system/batons/{baton_id}{suffix}"


async def _bus_state(tenant_id: str, agent_id: str, baton_id: str) -> str | None:
    """The receiver's baton state on the bus (``delivered|accepted|replied|late|failed``); ``None`` if unknown."""
    async with httpx.AsyncClient(timeout=BUS_TIMEOUT_S) as client:
        resp = await client.get(
            _bus_url(baton_id),
            params={"tenant_id": tenant_id, "agent_id": agent_id},
            headers=await _bus_headers(),
        )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    for receiver in resp.json().get("receivers") or ():
        if receiver.get("agent_id") == agent_id:
            return receiver.get("state")
    return None


async def _sync_from_bus(tenant_id: str, agent_id: str, rows: list[dict]) -> None:
    """Copy a taken-up bus state (acked or replied) into the rows a recall just showed."""
    sc = get_storage_client()
    try:
        for row in rows:
            state = await _bus_state(tenant_id, agent_id, row["baton_id"])
            if state in ("accepted", "replied"):
                await sc.accept_baton(tenant_id, row["baton_id"], agent_id=agent_id, via="bus", state=state)
        await sc.mark_baton_notices_checked(tenant_id, agent_id, [r["baton_id"] for r in rows])
    except Exception:
        logger.warning("baton bus state not read", exc_info=True)


async def baton_notices(
    tenant_id: str | None, agent_id: str | None, *, surface: Literal["mcp", "rest"]
) -> list[dict]:
    """The compact notices ``agent_id`` is owed on this recall; ``[]`` when none or on error."""
    if not tenant_id or not agent_id or _is_person():
        return []
    notices, _ = await _lookup(tenant_id, str(agent_id), surface=surface)
    return notices


class NoticesTask:
    """A lookup started beside the search, and who it is for."""

    def __init__(self, tenant_id: str, agent_id: str, task: asyncio.Task[_Found]) -> None:
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

    The rows shown whose bus state is due a check are checked after the
    response, when a bus is configured.
    """
    if started is None:
        return []
    notices, due = await started.task
    if due and bus_configured():
        track_task(_sync_from_bus(started.tenant_id, started.agent_id, due))
    return notices


async def _bus_accept(tenant_id: str, agent_id: str, baton_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=BUS_TIMEOUT_S) as client:
            resp = await client.post(
                _bus_url(baton_id, "/accept"),
                json={"tenant_id": tenant_id, "agent_id": agent_id},
                headers=await _bus_headers(),
            )
        if resp.status_code != 404:
            resp.raise_for_status()
    except Exception:
        logger.warning("baton accept not acked on the bus", exc_info=True)


async def accept_as_agent(tenant_id: str, baton_id: str, agent_id: str) -> dict:
    """``agent_id`` accepts a baton handed to it; its receiver state. 404 when it is not a receiver.

    Shared by REST ``POST /batons/{id}/accept`` and MCP ``caura_manage
    op=accept_baton``. Idempotent: a second accept keeps the first
    ``accepted_at``. With a bus configured, the receiver's delivery is acked
    there too (after the response; a failure is logged, the accept stands).
    """
    row = await get_storage_client().accept_baton(tenant_id, baton_id, agent_id=agent_id, via="recall")
    if row is None:
        raise HTTPException(status_code=404, detail="Not a receiver of this baton")
    if row.get("message_id") and bus_configured():
        track_task(_bus_accept(tenant_id, agent_id, row["baton_id"]))
    return {
        "baton_id": row["baton_id"],
        "agent_id": agent_id,
        "from_agent": row["from_agent"],
        "state": row["state"],
        "accepted_at": row["accepted_at"],
    }


async def read_as_agent(tenant_id: str, baton_id: str, agent_id: str) -> dict:
    """The baton as ``agent_id`` got it (a receiver) or cited it (the sender); 404 for anyone else.

    Shared by REST ``GET /batons/{id}`` and MCP ``caura_manage op=read_baton``,
    for a receiver with no bus credential. A receiver sees only the links
    delivered to it, never the withheld count. A link is not a grant: each one
    is checked again as this agent, today, and an unreadable one is left out.
    ``linked`` carries each readable link's title.
    """
    sc = get_storage_client()
    doc = await sc.get_document(tenant_id, BATON_COLLECTION, baton_id)
    data = (doc or {}).get("data") or {}
    mine = next((r for r in data.get("recipients") or () if r.get("agent_id") == agent_id), None)
    if mine is None and data.get("from_agent") != agent_id:
        raise HTTPException(status_code=404, detail="Baton not found")
    baton = {k: v for k, v in data.items() if k not in {"key_hash", "recipients"}}
    ids = [str(m) for m in (mine.get("memory_ids") if mine is not None else data.get("memory_ids")) or ()]
    rows = [r for r in await sc.bulk_get_memories(ids, tenant_id) if r] if ids else []
    can_read = await memory_reader(tenant_id, AgentIdentity(agent_id), caller_tenant_id=tenant_id)
    readable = {str(r["id"]): r for r in rows if can_read(r)}
    baton["memory_ids"] = [m for m in ids if m in readable]
    baton["linked"] = [
        {"memory_id": m, "title": readable[m].get("title") or (readable[m].get("content") or "")[:80]}
        for m in baton["memory_ids"]
    ]
    return baton
