"""Value metering's write and recall hooks (docs/plans/value-metering.md).

After a write: record the memory's origin — its session, how long that session
had worked since starting or its previous write, the calls to Caura in that
stretch, and tokens/turns when a hook, plugin or SDK reported them.

After a recall: record each memory from another session the first time this
session recalls it.

Both run after the response, fire-and-forget, and never fail the request.
A request with no session records nothing: "per session" needs a session.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from core_api.clients.storage_client import get_storage_client
from core_api.services import session_tracker
from core_api.session_context import current_session_id, current_session_source
from core_api.tasks import track_task

logger = logging.getLogger(__name__)


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def _session_of(metadata: Any) -> str | None:
    """The broker stamps its own session on what it writes; that one wins."""
    if isinstance(metadata, dict):
        value = metadata.get("session_id")
        if isinstance(value, str) and value:
            return value
    return current_session_id()


async def _record_origin(tenant_id: str, row: dict) -> None:
    try:
        await get_storage_client().value_record_origin(tenant_id, row)
    except Exception:
        logger.warning("value metering: origin not recorded", exc_info=True)


def schedule_origin(memory: Any, *, origin: dict | None = None) -> None:
    """After a write: record where the memory came from, once."""
    try:
        tenant_id = _field(memory, "tenant_id")
        memory_id = _field(memory, "id")
        session_id = _session_of(_field(memory, "metadata"))
        if not tenant_id or not memory_id or not session_id:
            return
        written = _field(memory, "created_at") or datetime.now(UTC)
        row: dict[str, Any] = {
            "memory_id": str(memory_id),
            "session_id": session_id,
            "session_source": current_session_source()
            or ("broker" if session_id != current_session_id() else ""),
            "agent_id": _field(memory, "agent_id") or "",
            "written_at": written.isoformat() if isinstance(written, datetime) else str(written),
            "origin_calls": session_tracker.take_calls_since_write(tenant_id, session_id),
        }
        if origin:
            row.update(
                {
                    "origin_tokens_in": origin.get("tokens_in"),
                    "origin_tokens_out": origin.get("tokens_out"),
                    "origin_turns": origin.get("turns"),
                    "origin_source": origin.get("source"),
                }
            )
        track_task(_record_origin(str(tenant_id), row))
    except Exception:
        logger.warning("value metering: origin skipped", exc_info=True)


def schedule_bulk_origins(data: Any, results: list[Any]) -> None:
    """After a bulk write: an origin for each item this attempt created.

    One write per created item, in order, so each item's origin runs from the
    previous item's — a batch written at once is one stretch of work, and its
    first item carries it.
    """
    try:
        now = datetime.now(UTC)
        for step, r in enumerate(results):
            if _field(r, "status") != "created" or not _field(r, "id"):
                continue
            item = data.items[_field(r, "index")]
            schedule_origin(
                {
                    "id": _field(r, "id"),
                    "tenant_id": data.tenant_id,
                    "agent_id": data.agent_id,
                    "metadata": item.metadata,
                    # A microsecond apart, so each item measures from the one
                    # before it: equal times would give every item the whole stretch.
                    "created_at": now + timedelta(microseconds=step),
                },
                origin=item.origin.model_dump() if getattr(item, "origin", None) else None,
            )
    except Exception:
        logger.warning("value metering: bulk origins skipped", exc_info=True)


async def _record_reuse(tenant_id: str, body: dict) -> None:
    try:
        await get_storage_client().value_record_reuse(tenant_id, body)
    except Exception:
        logger.warning("value metering: reuse not recorded", exc_info=True)


def schedule_reuse(results: list[Any], *, tenant_id: str | None, reader_agent_id: str | None) -> None:
    """After a recall: the memories this session sees for the first time from other sessions."""
    try:
        session_id = current_session_id()
        if not tenant_id or not session_id or not results:
            return
        ids = [str(_field(r, "id")) for r in results if _field(r, "id")][:200]
        if not ids:
            return
        track_task(
            _record_reuse(
                tenant_id,
                {
                    "session_id": session_id,
                    "reader_agent_id": reader_agent_id or "",
                    "memory_ids": ids,
                    "recalled_at": datetime.now(UTC).isoformat(),
                },
            )
        )
    except Exception:
        logger.warning("value metering: reuse skipped", exc_info=True)
