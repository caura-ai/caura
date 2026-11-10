"""Correction propagation, CP-10: who to tell about a correction.

``caura.memory.corrected`` (CP-08) is consumed by
:mod:`common.events.correction_handlers`, which records a ``memory_correction``
and its recipients through core-storage-api. core-worker subscribes on SaaS;
core-api subscribes only on the in-process bus (OSS standalone), through the
adapter here.

The routes in ``core_api.routes.corrections`` read the record back as the
person's preview. ``old_summary`` is the text that was wrong, so a caller that
is an agent sees it only when it is on the recipient list
(:func:`redact_for_caller`).
"""

from __future__ import annotations

from common.events.correction_handlers import CorrectionStorageAdapter
from core_api.clients.storage_client import get_storage_client


class _StorageCorrectionAdapter(CorrectionStorageAdapter):
    async def record_correction(self, *, tenant_id: str, body: dict) -> dict:
        return await get_storage_client().record_memory_correction(tenant_id, body)


def make_correction_adapter() -> CorrectionStorageAdapter:
    return _StorageCorrectionAdapter()


def redact_for_caller(correction: dict, *, agent_id: str | None, is_person: bool) -> dict:
    """``correction`` as this caller may see it.

    A person, or a key acting for the tenant (no agent), sees all of it. An
    agent sees ``old_summary`` only when it read the old version, that is, when
    it is a recipient; otherwise ``old_summary`` is None.
    """
    if is_person or not agent_id:
        return correction
    if any(r.get("agent_id") == agent_id for r in correction.get("recipients") or ()):
        return correction
    return {**correction, "old_summary": None}
