"""Link a receiver's writes to the handoff baton it is working on (``metadata.baton_id``).

Handoff batons (``docs/plans/handoff-batons.md``), D5. A receiver's findings
link back to the baton through ``metadata.baton_id``, which the ``baton_id``
read filter and the bus's ``findings`` read. Asking the model to pass it did not
work: in the HB-14 two-agent run only 9 of 25 receiver writes carried it, and
Claude never called ``memory_context`` once. So the write path sets it, the
way it stamps ``trace_id``:

* With a bus configured, a write that names ``metadata.baton_id`` keeps it
  only when the writer is that baton's sender or one of its receivers
  (``baton_recipient``); anyone else gets a 422. A baton is never a tag an
  agent can borrow. Standalone OSS has no bus and no batons, so it keeps an
  explicit ``baton_id`` unchecked, as before.
* A write that names none, from an agent with exactly one baton still waiting
  on it (``delivered``: not accepted on recall, not acked or replied on the
  bus, younger than 7 days), is tagged with that baton and marked
  ``_system.baton_tagged = "auto"``. With none or several waiting it is left
  as it is: guessing between batons would put findings under the wrong one.
* A write whose metadata carries bus provenance (``source: caura-bus``, from
  ``memory_context``) for a waiting baton's own message gets that baton, even
  with several waiting: the context names it.

The bus owns delivery state, and the row is the write path's copy of it. As on
recall (``baton_notices``), a row is refreshed from the bus at most every
:data:`~core_api.services.baton_notices.BUS_CHECK_S` seconds per (agent, baton),
and a write waits at most :data:`BUS_TIMEOUT_S` for it; on a timeout or error
the row's last known state stands. A taken-up baton is copied into the row and
skipped. Writes by a person, and platform writes (crystallizer, insights, the
broker) are never tagged. One indexed storage read per agent write; a failure
reads as no baton. No LLM calls.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException

from core_api.clients.storage_client import get_storage_client
from core_api.services.baton_notices import BUS_CHECK_S, _bus_state, _is_person, _parse
from core_api.services.correction_sender import bus_configured
from core_api.services.system_metadata import SYSTEM_NAMESPACE

logger = logging.getLogger(__name__)

BATON_KEY = "baton_id"
#: ``_system`` marker on a row the write path tagged by itself.
TAGGED_KEY = "baton_tagged"
#: Waiting batons read per write: enough to tell one from several.
MAX_OPEN = 5
#: The longest a write waits for the bus; the row's state stands after that.
BUS_TIMEOUT_S = 0.3


class BatonTagger:
    """Checks and fills ``metadata.baton_id`` for one request's writes by one agent.

    The waiting batons are read (and their bus state checked) once, however
    many items a bulk write carries. ``sc`` is the write path's storage client.
    """

    def __init__(self, tenant_id: str, agent_id: str | None, sc=None) -> None:
        self.tenant_id, self.agent_id = tenant_id, str(agent_id) if agent_id else None
        self.sc = sc if sc is not None else get_storage_client()
        self._open: list[dict] | None = None

    async def _is_party(self, baton_id: str) -> bool:
        """Whether the writer sent the baton or is one of its receivers."""
        rows = await self.sc.list_baton_recipients(self.tenant_id, baton_id)
        return any(self.agent_id in (r.get("agent_id"), r.get("from_agent")) for r in rows)

    async def _taken_up(self, row: dict) -> bool:
        """Ask the bus whether the receiver acked or replied; copy a taken-up state into the row."""
        agent = str(self.agent_id)
        try:
            state = await asyncio.wait_for(_bus_state(self.tenant_id, agent, row["baton_id"]), BUS_TIMEOUT_S)
        except Exception:
            # Slow or down: the row's last known state stands until the next window.
            logger.warning("baton bus state not read", exc_info=True)
            state = None
        if state in ("accepted", "replied"):
            await self.sc.accept_baton(
                self.tenant_id, row["baton_id"], agent_id=agent, via="bus", state=state
            )
        await self.sc.mark_baton_notices_checked(self.tenant_id, agent, [row["baton_id"]])
        return state in ("accepted", "replied")

    async def _waiting(self) -> list[dict]:
        """The batons still waiting on the writer, newest first; ``[]`` when none or on error."""
        if self._open is not None:
            return self._open
        self._open = []
        try:
            rows = await self.sc.list_baton_notices(self.tenant_id, str(self.agent_id), limit=MAX_OPEN)
        except Exception:
            logger.warning("open batons not read", exc_info=True)
            return self._open
        bus = bus_configured()
        stale = datetime.now(UTC) - timedelta(seconds=BUS_CHECK_S)
        due = [
            r
            for r in rows
            if bus
            and r.get("message_id")
            and ((checked := _parse(r.get("bus_checked_at"))) is None or checked < stale)
        ]
        try:
            # In parallel, so a write waits at most one BUS_TIMEOUT_S however many are due.
            taken = dict(
                zip(
                    (r["baton_id"] for r in due), await asyncio.gather(*map(self._taken_up, due)), strict=True
                )
            )
        except Exception:
            logger.warning("baton rows not refreshed", exc_info=True)
            taken = {}
        self._open = [r for r in rows if not taken.get(r["baton_id"])]
        return self._open

    async def tag(self, metadata: dict | None) -> dict | None:
        """``metadata`` with the writer's baton checked or filled in; 422 for a baton it is no party to.

        Call after the caller's metadata is sanitised, so the ``_system``
        marker is the platform's own.
        """
        explicit = (metadata or {}).get(BATON_KEY)
        if explicit is not None and not bus_configured():
            return metadata
        if explicit is not None:
            try:
                baton_id = str(UUID(str(explicit)))
                party = bool(self.agent_id) and await self._is_party(baton_id)
            except ValueError:
                party = False
            if not party:
                raise HTTPException(
                    status_code=422,
                    detail="metadata.baton_id must name a handoff baton the writing agent sent or received",
                )
            return {**(metadata or {}), BATON_KEY: baton_id}
        if not self.agent_id or _is_person():
            return metadata
        rows = await self._waiting()
        if (metadata or {}).get("source") == "caura-bus":
            # Bus provenance that names a waiting baton's own message picks it.
            message_id = (metadata or {}).get("message_id")
            rows = [r for r in rows if message_id and r.get("message_id") == message_id] or rows
        if len(rows) != 1:
            return metadata
        tagged = {**(metadata or {}), BATON_KEY: str(rows[0]["baton_id"])}
        tagged[SYSTEM_NAMESPACE] = {**tagged.get(SYSTEM_NAMESPACE, {}), TAGGED_KEY: "auto"}
        return tagged


async def tag_write(tenant_id: str, agent_id: str | None, metadata: dict | None, sc=None) -> dict | None:
    """One write's metadata through :class:`BatonTagger`."""
    return await BatonTagger(tenant_id, agent_id, sc).tag(metadata)
