"""Hold an agent's write for a person's review (g2.8).

An organization can hold the writes of agents below a trust level
(``quarantine.below_trust``, overridden per fleet). A held write is stored with
status ``quarantined``, which keeps it out of every read until a person releases
or rejects it. Both write paths ask this module, ``create_memory``'s pipeline
and ``create_memories_bulk``, so no entry point that reaches them can skip the
hold: REST and MCP writes, STM promotion, ingest and interviews.

The settings come from this process's cache, which can be behind a change made
elsewhere. So a write that goes live tells storage which settings it was decided
under, and storage refuses it if they have changed; the writer then decides it
again under the current ones (``common.settings_version``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar

from fastapi import HTTPException

from common.constants import HOLD_KEY
from common.settings_version import SETTINGS_VERSION_KEY
from core_api.clients.storage_client import StorageSettingsChangedError, get_storage_client
from core_api.services.system_metadata import SYSTEM_NAMESPACE

if TYPE_CHECKING:
    from core_api.services.organization_settings import ResolvedConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def hold_for(
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
    config: ResolvedConfig,
    *,
    is_inferred: bool,
) -> dict | None:
    """Why a write is held, or ``None`` when it goes live.

    Inferred writes (the crystallizer's and the insights pass's) are the
    platform's own, built from memories already live, so they are never held.
    Nothing is looked up while no level is set, so a tenant that holds nothing
    pays nothing for this.

    The agent is read from the primary: every write path creates its agent just
    before it writes, and a replica that hasn't caught up would answer that it
    doesn't exist. An agent that really has no row is held, as trust 0: no write
    path should reach here without one, so a write from one is a write nobody
    vouched for.
    """
    if is_inferred:
        return None
    below_trust = config.quarantine_below_trust(fleet_id)
    if below_trust <= 0:
        return None
    agent = await get_storage_client().get_agent(agent_id, tenant_id, read=False)
    trust_level = int((agent or {}).get("trust_level") or 0)
    if trust_level >= below_trust:
        return None
    return {"reason": "below_trust", "trust_level": trust_level, "below_trust": below_trust}


def claim_settings(payload: dict, config: ResolvedConfig, *, is_inferred: bool) -> None:
    """Have a memory insert say which settings its hold decision was made under.

    Only a write that went live claims them. A held one stays held whatever the
    settings now say, and the platform's own writes are never decided. Called
    again after a write is decided again, so it also drops a claim the new
    decision does not make.
    """
    held = HOLD_KEY in ((payload.get("metadata_") or {}).get(SYSTEM_NAMESPACE) or {})
    if held or is_inferred or config.settings_version is None:
        payload.pop(SETTINGS_VERSION_KEY, None)
    else:
        payload[SETTINGS_VERSION_KEY] = config.settings_version


async def insert_deciding_again(
    tenant_id: str,
    insert: Callable[[], Awaitable[T]],
    decide_again: Callable[[ResolvedConfig], Awaitable[None]],
) -> T:
    """``insert``, and if storage says the settings changed under it, decide again and retry once.

    ``decide_again`` is handed the tenant's settings fresh from storage and must
    leave the payload ``insert`` sends decided under them, its claim included
    (``claim_settings``). Nothing was written by the refused insert, so the
    retry is a first write. If storage refuses that one too, the settings
    changed again in between; the caller is told to retry rather than looped.
    """
    from core_api.services.organization_settings import reload_config

    try:
        return await insert()
    except StorageSettingsChangedError:
        logger.info("settings changed under a live write for %s; deciding its hold again", tenant_id)
    await decide_again(await reload_config(tenant_id))
    try:
        return await insert()
    except StorageSettingsChangedError as exc:
        raise HTTPException(
            status_code=503,
            detail="The organization's settings changed while this write was being decided; retry it.",
        ) from exc
