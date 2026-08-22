"""Storage-boundary reserved-id chokepoint — defense-in-depth for the bare-"main"
firehose guard.

The service layer (``memory_service.create_memory`` / ``create_memories_bulk``)
already rejects bare reserved ``"main"`` under ``policy=reject``. These tests
prove the SAME rejection now also holds at ``storage_client.create_memory`` /
``create_memories`` — the single boundary every memory insert funnels through —
so no present or future caller can persist a bare-"main" row by reaching storage
directly and skipping the service guard. The de-collapsed ``main-<install_id>``
form (#507) and every named id pass untouched.

Hermetic: ``_post`` is stubbed, so nothing touches the network or a DB.
"""

import pytest

from core_api.clients.storage_client import CoreStorageClient
from core_api.config import settings
from core_api.services.agent_identity import ReservedAgentIdError


@pytest.fixture
def policy(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(settings, "reserved_agent_id_policy", value)

    return _set


@pytest.fixture
def client_no_post(monkeypatch):
    """Client whose ``_post`` explodes if reached — a rejection test then proves
    the guard fired BEFORE any storage round-trip."""
    c = CoreStorageClient()

    async def _boom(*_a, **_k):
        raise AssertionError("storage _post reached — reserved-id guard did not block")

    monkeypatch.setattr(c, "_post", _boom)
    return c


@pytest.fixture
def client_ok(monkeypatch):
    c = CoreStorageClient()

    async def _post(_path, data, *_a, **_k):
        return {"id": "ok"} if isinstance(data, dict) else [{"id": "ok"} for _ in data]

    monkeypatch.setattr(c, "_post", _post)
    return c


async def test_bare_main_rejected_at_create_memory(policy, client_no_post):
    policy("reject")
    with pytest.raises(ReservedAgentIdError):
        await client_no_post.create_memory(
            {"agent_id": "main", "content": "x", "memory_type": "episode"}
        )


async def test_bare_main_rejected_at_create_memories_bulk(policy, client_no_post):
    policy("reject")
    with pytest.raises(ReservedAgentIdError):
        await client_no_post.create_memories(
            [
                {"agent_id": "brandclaw", "content": "ok"},
                {"agent_id": "main", "content": "firehose"},  # one bad item blocks the batch
            ]
        )


@pytest.mark.parametrize("aid", ["main-abc123def456", "brandclaw", "mcp-agent"])
async def test_decollapsed_and_named_pass_under_reject(policy, client_ok, aid):
    policy("reject")
    out = await client_ok.create_memory(
        {"agent_id": aid, "content": "x", "memory_type": "episode"}
    )
    assert out == {"id": "ok"}


async def test_warn_does_not_block_at_storage(policy, client_ok):
    # Matches service semantics: warn logs + proceeds; only reject blocks.
    policy("warn")
    out = await client_ok.create_memory({"agent_id": "main", "content": "x"})
    assert out == {"id": "ok"}
