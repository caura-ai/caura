"""Route-level regression tests for the broker-write ownership boundary.

These complement the pure-helper unit tests in
``test_broker_owned_agent_id.py`` by exercising how the bulk-write route
*wires in* that gate — the two places a naive implementation leaks:

- **Finding 1 (High):** the ownership gate must run on EVERY broker write,
  including one where the caller pre-populates ``agent_id`` in the request
  body. A gate that only fires on the auto-derived path lets a broker write
  under an agent owned by a different install just by naming it explicitly.
- **Finding 2 (Medium):** ``_broker_owned_agent_id`` optimistically keeps a
  not-yet-existing agent id (first-touch). Two installs racing the same new
  id both pass the gate; only one wins the ownership stamp. The post-create
  re-check in ``_write_memories_bulk_inner`` reads the now-authoritative
  ``owner_install_uuid`` and degrades the loser to ``broker:<install>``.

The route's happy-path runtime is otherwise integration-tested on the docker
compose stack; here we mock storage/metering and assert only the attribution
that ends up on ``body.agent_id``.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response

from core_api.auth import AuthContext
from core_api.config import settings as app_settings
from core_api.routes import memories
from core_api.schemas import (
    BulkItemResult,
    BulkMemoryCreate,
    BulkMemoryItem,
    BulkMemoryResponse,
)

pytestmark = pytest.mark.unit


def _broker_auth(
    install_uuid: str | None = "install-1", *, is_install: bool = True
) -> AuthContext:
    return AuthContext(
        tenant_id="tenant-1",
        is_install_credential=is_install,
        install_uuid=install_uuid,
    )


def _bulk_body(agent_id: str | None) -> BulkMemoryCreate:
    return BulkMemoryCreate(
        tenant_id="tenant-1",
        agent_id=agent_id,
        items=[BulkMemoryItem(content="hello world")],
    )


def _ok_response() -> BulkMemoryResponse:
    return BulkMemoryResponse(
        created=1,
        duplicates=0,
        errors=0,
        results=[BulkItemResult(index=0, status="created", id=uuid4())],
        bulk_ms=1,
    )


# ── Finding 1: gate applies to an explicitly-supplied agent_id ──────────


async def _run_outer(
    monkeypatch, *, body: BulkMemoryCreate, auth: AuthContext, owner: str | None
) -> str:
    """Drive ``write_memories_bulk`` with the gate's storage lookup mocked;
    return the ``agent_id`` the route hands to the inner writer."""
    monkeypatch.setattr(
        memories,
        "lookup_agent",
        AsyncMock(
            return_value=None
            if owner is None
            else {"agent_id": body.agent_id, "owner_install_uuid": owner}
        ),
    )
    monkeypatch.setattr(memories, "idempotency_for", AsyncMock(return_value=None))
    monkeypatch.setattr(
        memories, "per_tenant_slot", lambda *a, **k: contextlib.nullcontext()
    )

    captured: dict[str, str | None] = {}

    async def _inner(body_, response_, auth_, idem_, bulk_attempt_id_):
        captured["agent_id"] = body_.agent_id
        return "SENTINEL"

    monkeypatch.setattr(memories, "_write_memories_bulk_inner", _inner)

    result = await memories.write_memories_bulk(
        request=SimpleNamespace(),
        body=body,
        response=Response(),
        auth=auth,
        idempotency_key=None,
        bulk_attempt_id="attempt-1",
    )
    assert result == "SENTINEL"
    return captured["agent_id"]


async def test_explicit_agent_id_owned_by_other_install_is_degraded(monkeypatch):
    # Finding 1: broker names an agent it does NOT own -> must NOT bypass the gate.
    agent_id = await _run_outer(
        monkeypatch,
        body=_bulk_body("victim-agent"),
        auth=_broker_auth("install-1"),
        owner="install-2",
    )
    assert agent_id == "broker:install-1"


async def test_explicit_agent_id_owned_by_self_is_kept(monkeypatch):
    agent_id = await _run_outer(
        monkeypatch,
        body=_bulk_body("my-agent"),
        auth=_broker_auth("install-1"),
        owner="install-1",
    )
    assert agent_id == "my-agent"


async def test_explicit_agent_id_unclaimed_is_kept(monkeypatch):
    # Owner NULL (grandfathered/unclaimed) -> lenient: this write claims it.
    agent_id = await _run_outer(
        monkeypatch,
        body=_bulk_body("fresh-agent"),
        auth=_broker_auth("install-1"),
        owner=None,
    )
    assert agent_id == "fresh-agent"


# ── Finding 2: post-create re-check closes the first-touch race ──────────


async def _run_inner(
    monkeypatch, *, body: BulkMemoryCreate, auth: AuthContext, get_or_create: AsyncMock
) -> str:
    """Drive ``_write_memories_bulk_inner`` with storage/metering mocked;
    return the ``agent_id`` that reaches ``create_memories_bulk``."""
    monkeypatch.setattr(app_settings, "bind_write_identity_to_auth", False)
    monkeypatch.setattr(memories, "get_or_create_agent", get_or_create)
    monkeypatch.setattr(memories, "enforce_fleet_write", AsyncMock(return_value={}))
    monkeypatch.setattr(
        memories, "bulk_check_and_increment", AsyncMock(return_value=None)
    )

    captured: dict[str, str | None] = {}

    async def _create(body_, *, bulk_attempt_id=None):
        captured["agent_id"] = body_.agent_id
        return _ok_response()

    monkeypatch.setattr(memories, "create_memories_bulk", _create)

    await memories._write_memories_bulk_inner(body, Response(), auth, None, "attempt-1")
    return captured["agent_id"]


async def test_post_create_recheck_degrades_when_race_lost(monkeypatch):
    # Gate passed (agent didn't exist at read time); the create returns the
    # committed row owned by a DIFFERENT install -> loser degrades to broker id.
    get_or_create = AsyncMock(
        side_effect=[
            {
                "owner_install_uuid": "install-2",
                "fleet_id": None,
            },  # winner is install-2
            {
                "owner_install_uuid": "install-1",
                "fleet_id": None,
            },  # re-create under broker id
        ]
    )
    agent_id = await _run_inner(
        monkeypatch,
        body=_bulk_body("contested-agent"),
        auth=_broker_auth("install-1"),
        get_or_create=get_or_create,
    )
    assert agent_id == "broker:install-1"
    assert get_or_create.await_count == 2
    # Second create is under the degraded identity.
    assert get_or_create.await_args_list[1].args[1] == "broker:install-1"


async def test_post_create_no_recheck_when_owner_matches(monkeypatch):
    get_or_create = AsyncMock(
        return_value={"owner_install_uuid": "install-1", "fleet_id": None}
    )
    agent_id = await _run_inner(
        monkeypatch,
        body=_bulk_body("my-agent"),
        auth=_broker_auth("install-1"),
        get_or_create=get_or_create,
    )
    assert agent_id == "my-agent"
    assert get_or_create.await_count == 1


async def test_post_create_recheck_skipped_for_non_broker(monkeypatch):
    # A non-install (dashboard/SDK) caller is NOT subject to the broker gate
    # even if the agent row happens to carry an owner_install_uuid.
    get_or_create = AsyncMock(
        return_value={"owner_install_uuid": "install-2", "fleet_id": None}
    )
    agent_id = await _run_inner(
        monkeypatch,
        body=_bulk_body("dash-agent"),
        auth=_broker_auth(None, is_install=False),
        get_or_create=get_or_create,
    )
    assert agent_id == "dash-agent"
    assert get_or_create.await_count == 1


# ── Reserved broker:<install> namespace can't be pre-claimed ─────────────


async def test_cannot_preclaim_another_installs_fallback(monkeypatch):
    # Attacker (install-3) explicitly names victim install-1's reserved fallback.
    # It must degrade to the attacker's OWN fallback so install-3 never creates
    # or owns "broker:install-1" — otherwise it could capture install-1's later
    # degraded writes.
    agent_id = await _run_outer(
        monkeypatch,
        body=_bulk_body("broker:install-1"),
        auth=_broker_auth("install-3"),
        owner=None,
    )
    assert agent_id == "broker:install-3"
