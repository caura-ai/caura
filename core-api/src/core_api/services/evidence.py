"""Evidence contract v1: the provenance columns a write carries to storage.

See ``docs/plans/evidence-contract-v1.md``. ``observed_at``, ``trace_id`` and
``tool_call_id`` describe where a memory came from. They are the writer's, except
``trace_id``, which the server stamps when the writer sent none
(``session_context.server_trace_id``). The verification columns are not here:
they are server-owned, no write sets them, and a new row takes the column
default (``unverified``).

One helper for every payload that inserts a memory row, so a row derived from
another (an auto-chunk child, an atomic fact) carries its parent's provenance
the way it already carries ``source_uri`` and ``run_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

EVIDENCE_WRITE_FIELDS = ("observed_at", "trace_id", "tool_call_id")


def evidence_payload(source: Any) -> dict[str, Any]:
    """The evidence columns of ``source`` as a storage insert payload.

    ``source`` is a write body (``MemoryCreate``, ``BulkMemoryItem``) or a stored
    row as storage returns it. A ``datetime`` goes out as ISO text, as the other
    timestamp fields of these payloads do, since the payload is sent as JSON.
    """
    if isinstance(source, dict):
        values = {f: source.get(f) for f in EVIDENCE_WRITE_FIELDS}
    else:
        values = {f: getattr(source, f, None) for f in EVIDENCE_WRITE_FIELDS}
    observed_at = values["observed_at"]
    if isinstance(observed_at, datetime):
        values["observed_at"] = observed_at.isoformat()
    return values
