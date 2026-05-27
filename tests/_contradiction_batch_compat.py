"""Test-side compatibility shim for the P2 batch-status migration.

The contradiction detector used to issue one ``sc.update_memory_status``
HTTP per affected row inside its detection loops. After audit P2, all
three paths (semantic / Path C / RDF) accumulate updates and flush one
``sc.batch_update_status`` call after the loop instead.

Existing tests assert on ``mock_sc.update_memory_status.call_args_list``
to check direction, supersedes_id wiring, status values, etc. — pure
behavior checks expressed through the legacy call shape. Migrating
every assertion to ``batch_update_status.call_args_list`` would be
mostly mechanical noise (~80 sites across 7 files); instead this
helper installs a ``batch_update_status`` side-effect that replays
each row of the payload as if it were an individual
``update_memory_status`` call. The legacy assertions continue to fire
unchanged.

New tests that want to assert on the batched shape directly should not
install the shim — they can read ``mock_sc.batch_update_status.call_args``
directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from typing import Any


def install_batch_status_replay_shim(mock_sc: Any) -> None:
    """Make ``mock_sc.batch_update_status({"updates": [...]})`` replay each
    row as a per-row ``mock_sc.update_memory_status(memory_id, status,
    **kwargs)`` call on the same mock, so existing tests' call-arg
    assertions stay valid post-P2.

    Idempotent: safe to call after ``mock_sc.update_memory_status`` has
    already been replaced with an ``AsyncMock`` (the common pattern).
    Does NOT overwrite an existing ``update_memory_status`` mock — it
    only routes the batch-side replay into it.
    """
    if not isinstance(getattr(mock_sc, "update_memory_status", None), AsyncMock):
        mock_sc.update_memory_status = AsyncMock()

    update_mock = mock_sc.update_memory_status

    async def _replay(payload: dict) -> dict:
        for row in payload.get("updates", []):
            mid = row["memory_id"]
            status = row["status"]
            kwargs: dict[str, Any] = {}
            if "supersedes_id" in row:
                kwargs["supersedes_id"] = row["supersedes_id"]
            if "unset_supersedes" in row:
                kwargs["unset_supersedes"] = row["unset_supersedes"]
            if "expected_supersedes_id" in row:
                kwargs["expected_supersedes_id"] = row["expected_supersedes_id"]
            await update_mock(mid, status, **kwargs)
        return {"ok": True, "skipped": []}

    mock_sc.batch_update_status = AsyncMock(side_effect=_replay)
