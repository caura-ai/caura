"""Idempotency keys replay what they promise to replay (audit batch B31).

Each test names the finding it pins:

- L-07: the bulk write claims its key for longer than it may run. The claim
  expired after the 90s pending TTL, but bulk runs its own 90s budget after its
  agent and fleet checks, so a same-key retry could run the bulk a second time
  while the first was still in flight.
- L-12: keys are scoped per agent (Eldad, 2026-10-11). Tenant-wide, a second
  agent that sent the same key and byte-identical body got the first agent's
  reply, and its own write never happened: REST hashes only the body, and the
  agent travels in the credential.
- L-30: evolve takes an opt-in key (Eldad, 2026-10-11). Every report adjusts
  weights, by design; a retry with the key replays the first answer instead.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_test_auth, uid


@contextlib.contextmanager
def _as_agent(tenant_id: str, agent_id: str):
    """Run the enclosed requests as an agent credential, as the gateway sends
    one: no OSS auth path sets ``auth.agent_id`` (see test_ax_m16)."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        tenant_id=tenant_id, agent_id=agent_id
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


@pytest.fixture
def hosted(monkeypatch):
    from core_api.config import settings as app_settings

    monkeypatch.setattr(app_settings, "is_standalone", False)


def _idempotent_storage(monkeypatch):
    """A storage client that grants every claim, for the MCP handlers."""
    sc = stub_storage_client(
        monkeypatch,
        get_idempotency=None,
        claim_idempotency=(True, None),
        upsert_idempotency=None,
    )
    monkeypatch.setattr(
        "core_api.middleware.idempotency.get_storage_client", lambda: sc
    )
    return sc


def _replay_recorded(sc) -> None:
    """Answer the next lookup with what the last call recorded."""
    recorded = sc.upsert_idempotency.await_args.kwargs
    sc.get_idempotency = AsyncMock(
        return_value={
            "request_hash": recorded["request_hash"],
            "is_pending": False,
            "response_body": recorded["response_body"],
            "status_code": recorded["status_code"],
        }
    )


class _Out:
    def __init__(self, memory_id: str) -> None:
        self.memory_id = memory_id

    def model_dump(self, mode: str = "python") -> dict:
        return {"id": self.memory_id, "status": "created"}


# --- L-07: a slow bulk keeps its claim ---------------------------------------


@pytest.mark.integration
async def test_l07_a_retry_during_a_slow_bulk_waits_for_it(client, monkeypatch):
    """The first bulk is held past the pending TTL but inside its own budget.
    A same-key retry must wait and replay it, not run the bulk again."""
    from core_api import config as cfg
    from core_api.routes import memories as routes_mem

    monkeypatch.setattr(cfg.settings, "idempotency_pending_ttl_seconds", 1)
    monkeypatch.setattr(cfg.settings, "bulk_request_timeout_seconds", 30.0)
    real_create = routes_mem.create_memories_bulk
    release = asyncio.Event()
    runs = 0

    async def held_create(*args, **kwargs):
        nonlocal runs
        runs += 1
        await release.wait()
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(routes_mem, "create_memories_bulk", held_create)
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"b31-slow-{uid()}",
        "items": [{"content": f"b31 slow bulk {uid()}"}],
    }
    keyed = {
        **headers,
        "Idempotency-Key": f"b31-bulk-{uid()}",
        "X-Bulk-Attempt-Id": f"b31-attempt-{uid()}",
    }

    first = asyncio.create_task(
        client.post("/api/v1/memories/bulk", json=body, headers=keyed)
    )
    await asyncio.sleep(1.5)  # past the pending TTL, well inside the budget
    retry = asyncio.create_task(
        client.post("/api/v1/memories/bulk", json=body, headers=keyed)
    )
    await asyncio.sleep(0.5)
    release.set()
    first_resp, retry_resp = await first, await retry

    assert first_resp.status_code == 200, first_resp.text
    assert runs == 1, "the retry ran the bulk a second time"
    assert retry_resp.json() == first_resp.json()


# --- L-12: keys are scoped per agent -----------------------------------------


@pytest.mark.integration
async def test_l12_two_agents_sharing_a_key_each_get_their_own_write(client, hosted):
    tenant_id = f"b31-tenant-{uid()}"
    body = {"tenant_id": tenant_id, "content": f"daily summary {uid()}"}
    keyed = {"Idempotency-Key": f"daily-summary-{uid()}"}

    with _as_agent(tenant_id, "b31-agent-a"):
        first = await client.post("/api/v1/memories", json=body, headers=keyed)
        again = await client.post("/api/v1/memories", json=body, headers=keyed)
    with _as_agent(tenant_id, "b31-agent-b"):
        second = await client.post("/api/v1/memories", json=body, headers=keyed)

    assert first.status_code == 201, first.text
    assert again.json()["id"] == first.json()["id"], "an agent's retry replays"
    assert second.status_code == 201, second.text
    assert second.json()["agent_id"] == "b31-agent-b"
    assert second.json()["id"] != first.json()["id"]


@pytest.mark.integration
async def test_l12_bulk_agents_sharing_a_key_each_get_their_own_write(client, hosted):
    tenant_id = f"b31-tenant-{uid()}"
    body = {"tenant_id": tenant_id, "items": [{"content": f"bulk {uid()}"}]}
    key = f"nightly-import-{uid()}"

    responses = []
    for agent_id in ("b31-bulk-a", "b31-bulk-b"):
        with _as_agent(tenant_id, agent_id):
            responses.append(
                await client.post(
                    "/api/v1/memories/bulk",
                    json=body,
                    headers={
                        "Idempotency-Key": key,
                        "X-Bulk-Attempt-Id": f"{agent_id}-{uid()}",
                    },
                )
            )

    first, second = (r.json()["results"][0] for r in responses)
    assert responses[1].status_code == 200, responses[1].text
    assert second["status"] == "created", second
    assert second["id"] != first["id"]


@pytest.mark.unit
async def test_l12_mcp_write_keys_are_per_agent(mcp_env, monkeypatch):
    sc = _idempotent_storage(monkeypatch)
    mcp_env["service"]("create_memory").return_value = _Out("m-1")
    metadata = {"idempotency_key": "daily-summary"}

    for agent_id in ("alice", "bob"):
        await mcp_server.caura_write(
            content="hello", metadata=metadata, agent_id=agent_id
        )

    alice, bob = (
        call.kwargs["idempotency_key"] for call in sc.claim_idempotency.await_args_list
    )
    assert alice != bob


@pytest.mark.unit
def test_l12_no_agent_and_key_pair_spells_anothers_stored_key():
    from core_api.middleware.idempotency import stored_key

    assert stored_key("header", "a:b", "k") != stored_key("header", "a", "b:k")
    assert stored_key("header", "a", "k") != stored_key("body", "a", "k")


# --- L-30: a keyed evolve retry replays --------------------------------------


async def _evolve_seed(client, headers, tenant_id: str, agent_id: str) -> str:
    write = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "memory_type": "fact",
            "content": f"b31 evolve seed {uid()}",
            "weight": 0.5,
        },
        headers=headers,
    )
    assert write.status_code == 201, write.text
    return write.json()["id"]


def _report(tenant_id: str, agent_id: str, memory_id: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "outcome": f"acted on it {uid()}",
        "outcome_type": "success",
        "related_ids": [memory_id],
    }


@pytest.mark.integration
async def test_l30_a_keyed_evolve_retry_adjusts_nothing_again(client, sc):
    tenant_id, headers = get_test_auth()
    agent_id = f"b31-evolver-{uid()}"
    memory_id = await _evolve_seed(client, headers, tenant_id, agent_id)
    report = _report(tenant_id, agent_id, memory_id)
    keyed = {**headers, "Idempotency-Key": f"b31-evolve-{uid()}"}

    first = await client.post("/api/v1/evolve/report", json=report, headers=keyed)
    retry = await client.post("/api/v1/evolve/report", json=report, headers=keyed)

    assert first.status_code == 200, first.text
    assert retry.status_code == 200, retry.text
    assert retry.json() == first.json()
    (adjusted,) = first.json()["weight_adjustments"]
    row = await sc.get_memory(memory_id, tenant_id, read=False)
    assert row["weight"] == pytest.approx(adjusted["new_weight"])


@pytest.mark.integration
async def test_l30_a_report_without_a_key_still_adjusts_each_time(client):
    """The control: a repeated report is a second report (Eldad, 2026-10-11)."""
    tenant_id, headers = get_test_auth()
    agent_id = f"b31-evolver-{uid()}"
    memory_id = await _evolve_seed(client, headers, tenant_id, agent_id)
    report = _report(tenant_id, agent_id, memory_id)

    first = await client.post("/api/v1/evolve/report", json=report, headers=headers)
    again = await client.post("/api/v1/evolve/report", json=report, headers=headers)

    assert again.status_code == 200, again.text
    (before,) = first.json()["weight_adjustments"]
    (after,) = again.json()["weight_adjustments"]
    assert after["old_weight"] == pytest.approx(before["new_weight"])
    assert after["new_weight"] > after["old_weight"]


@pytest.mark.unit
async def test_l30_a_keyed_caura_evolve_retry_replays(mcp_env, monkeypatch):
    sc = _idempotent_storage(monkeypatch)
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace()),
    )
    applied = AsyncMock(
        return_value={"outcome_id": "o-1", "weight_adjustments": [{"id": "m-1"}]}
    )
    monkeypatch.setattr(
        "core_api.services.evolve_service._apply_outcome_to_db", applied
    )
    report = {"outcome": "deployed", "outcome_type": "success", "agent_id": "alice"}

    first = await mcp_server.caura_evolve(**report, idempotency_key="retry-1")
    _replay_recorded(sc)
    second = await mcp_server.caura_evolve(**report, idempotency_key="retry-1")

    assert applied.await_count == 1
    assert parse_envelope(second) == parse_envelope(first)
