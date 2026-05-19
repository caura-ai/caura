"""A4 #10 — storage layer supports retracting a contradiction verdict.

Context
───────
Path A (semantic contradiction) can flip memory B's status to
``conflicted`` with ``supersedes_id=A``. Path C (entity-overlap) can
later prove that A and B actually concern different subjects — Path
A's verdict was wrong. Today the storage layer has no API surface to
reverse it: ``update_memory_status`` can advance status (active →
conflicted/outdated) and set ``supersedes_id`` once (compare-and-swap
against NULL) but it cannot clear ``supersedes_id`` or move status
back to ``active``. A4 needs that capability — items 11-14 build on
top of it.

Tests pinned BEFORE the implementation. They fail against current
main (no retraction surface exists). Implementation makes them pass.

Storage-side contract (new fields on PATCH /memories/{id}/status):
- ``unset_supersedes`` (bool, default False) — when True, clear
  ``supersedes_id`` instead of setting it.
- ``expected_supersedes_id`` (UUID str, required when
  ``unset_supersedes`` is True) — guards against stale retractions.
  The clear only succeeds when the row's current ``supersedes_id``
  matches this value OR is already NULL (idempotent re-fire). When
  the row has a *different* non-NULL value, the clear is rejected
  (409) so the caller knows another writer took the row in the
  meantime.

Client-side contract (``CoreStorageClient.update_memory_status``):
- New kwargs ``unset_supersedes: bool = False`` and
  ``expected_supersedes_id: str | None = None``.
- Pass-through to the PATCH body unchanged.
- ``unset_supersedes=True`` AND ``supersedes_id=<x>`` is an illegal
  combination — caller would be asking to both set and clear in one
  call.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import TENANT_ID, FLEET_ID, AGENT_ID, uid as _uid


# ---------------------------------------------------------------------------
# Helpers — write two memories A and B, manually establish a Path-A-style
# verdict (B.status=conflicted, B.supersedes_id=A) via the existing
# update_memory_status surface, then exercise the new retraction kwargs.
# ---------------------------------------------------------------------------


async def _write_memory(sc, content: str, *, status: str = "active") -> dict[str, Any]:
    """Helper: write a memory directly via the storage client."""
    tag = _uid()
    return await sc.create_memory(
        {
            "tenant_id": TENANT_ID,
            "fleet_id": FLEET_ID,
            "agent_id": AGENT_ID,
            "content": f"{content} [{tag}]",
            "memory_type": "fact",
            "status": status,
        }
    )


async def _establish_conflicted(sc, *, conflicted: dict, by: dict) -> None:
    """Set ``conflicted.status=conflicted`` and ``conflicted.supersedes_id=by.id``
    using the existing (pre-A4) update_memory_status surface."""
    await sc.update_memory_status(
        conflicted["id"], "conflicted", supersedes_id=by["id"]
    )


# ---------------------------------------------------------------------------
# Happy path — retraction clears supersedes_id and reverts status to active
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retraction_clears_supersedes_and_sets_status_active(sc):
    """Path A wrongly marked B conflicted-by-A; Path C calls retraction;
    B must be active with supersedes_id NULL after the call."""
    a = await _write_memory(sc, "User likes oat milk")
    b = await _write_memory(sc, "User prefers oat milk")
    await _establish_conflicted(sc, conflicted=b, by=a)

    # Sanity: B is conflicted-by-A before retraction.
    b_pre = await sc.get_memory(b["id"])
    assert b_pre["status"] == "conflicted"
    assert str(b_pre["supersedes_id"]) == str(a["id"])

    # Act: retract.
    result = await sc.update_memory_status(
        b["id"],
        status="active",
        unset_supersedes=True,
        expected_supersedes_id=a["id"],
    )
    assert result is not None  # PATCH succeeded

    # Assert: B is back to active and the supersedes pointer is cleared.
    b_post = await sc.get_memory(b["id"])
    assert b_post["status"] == "active", (
        f"Retraction must move status back to 'active'; got {b_post['status']!r}"
    )
    assert b_post["supersedes_id"] is None, (
        f"Retraction must clear supersedes_id; got {b_post['supersedes_id']!r}"
    )


# ---------------------------------------------------------------------------
# Stale-retraction guard — caller observed supersedes_id=A but DB has C now
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retraction_rejects_when_supersedes_changed_under_us(sc):
    """Between the caller observing ``B.supersedes_id=A`` and the retraction
    landing, another writer flipped it to C. The retraction must NOT clear
    the new C pointer — the caller's view is stale. Expect a 4xx; B's
    status and supersedes_id must remain untouched."""
    a = await _write_memory(sc, "User likes oat milk")
    b = await _write_memory(sc, "User prefers oat milk")
    c = await _write_memory(sc, "User likes almond milk")
    await _establish_conflicted(sc, conflicted=b, by=c)  # B is now conflicted by C

    from httpx import HTTPStatusError

    with pytest.raises(HTTPStatusError) as exc_info:
        await sc.update_memory_status(
            b["id"],
            status="active",
            unset_supersedes=True,
            expected_supersedes_id=a["id"],  # caller's stale view
        )
    assert exc_info.value.response.status_code == 409, (
        f"Stale retraction must return 409 Conflict; "
        f"got {exc_info.value.response.status_code}"
    )

    # B unchanged.
    b_post = await sc.get_memory(b["id"])
    assert b_post["status"] == "conflicted"
    assert str(b_post["supersedes_id"]) == str(c["id"]), (
        f"Stale retraction must not touch the current supersedes_id; "
        f"got {b_post['supersedes_id']!r}, expected {c['id']!r}"
    )


# ---------------------------------------------------------------------------
# Idempotent re-fire — calling retraction twice in a row is safe
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retraction_is_idempotent_on_already_cleared_row(sc):
    """Two ENRICHED back-channel events can fire Path C twice for the
    same memory pair. The second retraction must not error — the row
    is already in the desired state (supersedes_id NULL, status active)."""
    a = await _write_memory(sc, "User likes oat milk")
    b = await _write_memory(sc, "User prefers oat milk")
    await _establish_conflicted(sc, conflicted=b, by=a)

    # First retraction — clears the pointer.
    await sc.update_memory_status(
        b["id"], status="active", unset_supersedes=True, expected_supersedes_id=a["id"]
    )

    # Second retraction — must not error; row is already in target state.
    result = await sc.update_memory_status(
        b["id"], status="active", unset_supersedes=True, expected_supersedes_id=a["id"]
    )
    assert result is not None

    b_post = await sc.get_memory(b["id"])
    assert b_post["status"] == "active"
    assert b_post["supersedes_id"] is None


# ---------------------------------------------------------------------------
# Validation — illegal kwarg combinations rejected at the client
# ---------------------------------------------------------------------------


def _make_client_with_mock_http():
    """Construct a CoreStorageClient with a mock httpx pool — the client-level
    validation tests don't need a real storage-api; they just exercise the
    signature + pre-flight checks before any HTTP call is made."""
    from unittest.mock import AsyncMock

    import httpx

    from core_api.clients.storage_client import CoreStorageClient

    write_client = AsyncMock(spec=httpx.AsyncClient)
    read_client = AsyncMock(spec=httpx.AsyncClient)
    return CoreStorageClient(
        base_url="http://test-storage",
        read_url="",
        http=write_client,
        read_http=read_client,
    )


@pytest.mark.asyncio
async def test_client_rejects_unset_without_expected_supersedes_id():
    """``unset_supersedes=True`` without ``expected_supersedes_id`` has no
    CAS anchor — would race with concurrent setters. Reject at the
    client (ValueError) so the bad call never reaches the wire."""
    client = _make_client_with_mock_http()
    fake_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="expected_supersedes_id"):
        await client.update_memory_status(
            fake_id, status="active", unset_supersedes=True
        )


@pytest.mark.asyncio
async def test_client_rejects_set_and_unset_in_same_call():
    """``unset_supersedes=True`` AND ``supersedes_id=<x>`` is contradictory —
    one says clear, the other says set to x. Reject at the client."""
    client = _make_client_with_mock_http()
    fake_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="(?i)mutually exclusive|cannot.*both"):
        await client.update_memory_status(
            fake_id,
            status="active",
            supersedes_id=other_id,
            unset_supersedes=True,
        )
