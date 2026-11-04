"""The trust a derived row carries from the rows it was built from.

The crystallizer, insights and evolve each write rows of their own built from
other agents' memories. Such a row must not read as more trusted than its
weakest source, or a low-trust writer's text comes back out looking vouched
for. This module works out each source's trust, so the writer can record the
lowest on the derived row (``source_trust``, platform-only metadata that
readers see under ``system_metadata``) and act on it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from core_api.services.system_metadata import SYSTEM_NAMESPACE

SOURCE_TRUST_KEY = "source_trust"


async def author_trust(tenant_id: str, agent_ids: Iterable[str | None], *, sc=None) -> dict[str, int]:
    """Each distinct author's current trust level, read from the primary.

    An author with no agents row reads as 0, as the write hold treats one.
    ``sc`` is the storage client to read through, for a caller holding one.
    """
    ids = sorted({agent_id for agent_id in agent_ids if agent_id})
    if not ids:
        return {}
    if sc is None:
        from core_api.clients.storage_client import get_storage_client

        sc = get_storage_client()
    rows = await asyncio.gather(*(sc.get_agent(agent_id, tenant_id, read=False) for agent_id in ids))
    return {
        agent_id: int((row or {}).get("trust_level") or 0) for agent_id, row in zip(ids, rows, strict=True)
    }


def row_trust(row: dict, by_author: dict[str, int]) -> int:
    """How far one source row can be trusted.

    A row the platform derived (``is_inferred``: a crystal, an insight) has the
    lowest trust of its own sources, which its synthetic author's level does not
    reflect. It is read from the platform namespace only, which callers cannot
    write; storage's insights reads copy it to the row's top level. One derived
    before this was recorded reads as 0. Any other row has its author's level,
    from ``author_trust``.
    """
    if row.get("is_inferred"):
        if SOURCE_TRUST_KEY in row:
            recorded = row[SOURCE_TRUST_KEY]
        else:
            metadata = row.get("metadata_") if row.get("metadata_") is not None else row.get("metadata")
            recorded = ((metadata or {}).get(SYSTEM_NAMESPACE) or {}).get(SOURCE_TRUST_KEY)
        if isinstance(recorded, int) and not isinstance(recorded, bool):
            return recorded
        return 0
    return by_author.get(row.get("agent_id") or "", 0)
