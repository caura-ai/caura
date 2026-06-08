"""Shared cursor-pagination helpers.

Encodes/decodes an opaque base64 cursor of the form ``{created_at}:{id}``
for use with stable, tuple-ordered pagination across the REST memory
list endpoint and the MCP ``memclaw_list`` tool.

Callers must ensure the cursor is only used with queries ordered by
``(created_at desc, id desc)`` — any other ordering breaks the ``(ts,
id) < (cursor_ts, cursor_id)`` boundary predicate.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import UUID


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode a base64 cursor into ``(created_at, id)``.

    Raises ``ValueError`` (or subclasses from base64/UUID parsing) on
    malformed input. Callers should translate to HTTP 400 etc.
    """
    decoded = base64.b64decode(cursor, validate=True).decode()
    ts_str, id_str = decoded.rsplit(":", 1)
    ts = datetime.fromisoformat(ts_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts, UUID(id_str)


def encode_cursor(created_at: datetime, memory_id: UUID) -> str:
    """Encode ``(created_at, id)`` into a base64 cursor string."""
    raw = f"{created_at.isoformat()}:{memory_id}"
    return base64.b64encode(raw.encode()).decode()


def paginated_order_by(primary, id_col, order: str) -> tuple:
    """Return the ORDER BY clause for cursor-stable pagination.

    Pairs the primary sort column with ``id`` as a same-direction
    tiebreaker so rows that share the primary value (a single bulk-
    write tranche collides on ``created_at`` to ms precision; low-
    cardinality columns like ``status`` collide trivially) keep a
    deterministic order across paginated requests. Without it
    Postgres returns same-key rows in implementation-defined order
    and consecutive pages yield duplicates and skips — the load-test
    ``pagination-duplicates`` finding.

    Both pagination call sites — the repository's ``list_by_filters``
    and the admin route's inline query — must call this helper so the
    tiebreaker stays in sync with the ``tuple_(created_at, id)``
    cursor predicate.
    """
    if order == "desc":
        return (primary.desc(), id_col.desc())
    return (primary.asc(), id_col.asc())
