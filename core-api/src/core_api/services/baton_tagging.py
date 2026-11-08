"""Link a receiver's writes to the handoff baton it is working on (``metadata.baton_id``).

Handoff batons (``docs/plans/handoff-batons.md``), D5. A receiver's findings
link back to the baton through ``metadata.baton_id``, which the ``baton_id``
read filter and the bus's ``findings`` read. Asking the model to pass it did not
work: in the HB-14 two-agent run only 9 of 25 receiver writes carried it, and
Claude never called ``memory_context`` once. So the write path sets it, the
way it stamps ``trace_id``:

* A write that names ``metadata.baton_id`` keeps it only when the writer is
  that baton's sender or one of its receivers (``baton_recipient``); anyone
  else gets a 422. A baton is never a tag an agent can borrow.
* A write that names none, from an agent with a baton still waiting on it
  (``delivered``: not accepted on recall, not acked or replied on the bus,
  younger than 7 days), is tagged with that baton, the newest one when there
  are several, and marked ``_system.baton_tagged = "auto"``.
* A write whose metadata carries bus provenance (``source: caura-bus``, from
  ``memory_context``) for a waiting baton's own message gets that baton. Bus
  provenance for another message is no reason to skip: in HB-14 the
  receiver's contract and outcome writes for the baton's work carried the
  context of other threads.

The bus owns delivery state. Before tagging with a bus-delivered baton, the
bus is asked whether that receiver already acked or replied (the
``baton_notices`` sync, without its 30 s throttle: a write right after a reply
must not carry the finished baton); a taken-up baton is copied into the row
and skipped. Only an agent with a waiting baton pays that call.
Writes by a person, and platform writes (crystallizer, insights, the broker)
are never tagged. One indexed storage read per agent write; a failure reads as
no baton. No LLM calls.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException

from core_api.clients.storage_client import get_storage_client
from core_api.services.baton_notices import _bus_state, _is_person
from core_api.services.correction_sender import bus_configured
from core_api.services.system_metadata import SYSTEM_NAMESPACE

logger = logging.getLogger(__name__)

BATON_KEY = "baton_id"
#: ``_system`` marker on a row the write path tagged by itself.
TAGGED_KEY = "baton_tagged"
#: Waiting batons read per write; the newest one that is still open wins.
MAX_OPEN = 5
#: Bus checks one write may wait for.
MAX_BUS_CHECKS = 2


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
        state = await _bus_state(self.tenant_id, agent, row["baton_id"])
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
        # Read the bus every time: a receiver that replied a moment ago must not
        # tag its next write with the baton it just finished.
        checks = MAX_BUS_CHECKS if bus_configured() else 0
        for row in rows:
            if checks and row.get("message_id"):
                checks -= 1
                try:
                    if await self._taken_up(row):
                        continue
                except Exception:
                    logger.warning("baton bus state not read", exc_info=True)
            self._open.append(row)
        return self._open

    async def tag(self, metadata: dict | None) -> dict | None:
        """``metadata`` with the writer's baton checked or filled in; 422 for a baton it is no party to.

        Call after the caller's metadata is sanitised, so the ``_system``
        marker is the platform's own.
        """
        explicit = (metadata or {}).get(BATON_KEY)
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
        if not rows:
            return metadata
        tagged = {**(metadata or {}), BATON_KEY: str(rows[0]["baton_id"])}
        tagged[SYSTEM_NAMESPACE] = {**tagged.get(SYSTEM_NAMESPACE, {}), TAGGED_KEY: "auto"}
        return tagged


async def tag_write(tenant_id: str, agent_id: str | None, metadata: dict | None, sc=None) -> dict | None:
    """One write's metadata through :class:`BatonTagger`."""
    return await BatonTagger(tenant_id, agent_id, sc).tag(metadata)
