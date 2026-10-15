"""``GET /api/v1/spend``: who may read a workspace's usage, and what it returns.

Spend per agent is an admin-plane report: tenant and admin credentials read
it; an agent key (its peers' spend) and an org member are refused, and a
tenant outside the caller's readable set is refused like everywhere else.
"""

from __future__ import annotations

import pytest

from core_api.auth import AuthContext
from core_api.routes import usage as usage_route


class FakeStorage:
    def __init__(self) -> None:
        self.asked: list[tuple[str, dict]] = []

    async def query_usage(self, tenant_id: str, body: dict) -> dict:
        self.asked.append((tenant_id, body))
        return {
            "model_calls": [
                {
                    "day": "2026-10-11",
                    "calls": 10,
                    "failures": 1,
                    "tokens_in": 1000,
                    "tokens_out": 200,
                    "tokens_reasoning": 50,
                }
            ],
            "savings": [
                {"metric": "recall.count", "quantity": 4},
                {"metric": "recall.returned_tokens", "quantity": 300},
                {"metric": "recall.derived_returned_tokens", "quantity": 120},
                {"metric": "recall.derived_source_tokens", "quantity": 5000},
                {"metric": "recall.cross_agent_results", "quantity": 3},
                {"metric": "hygiene.retired_memories", "quantity": 2},
                {"metric": "hygiene.retired_tokens", "quantity": 400},
            ],
        }


@pytest.fixture
def storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(usage_route, "get_storage_client", lambda: fake)
    return fake


@pytest.fixture
def caller():
    from core_api.app import app
    from core_api.auth import get_auth_context

    def install(**ctx):
        async def dep():
            return AuthContext(**ctx)

        app.dependency_overrides[get_auth_context] = dep

    yield install
    app.dependency_overrides.pop(get_auth_context, None)


def test_the_summary_adds_tokens_and_nets_context_saved():
    s = usage_route.summarize(
        [
            {
                "calls": 2,
                "failures": 0,
                "tokens_in": 10,
                "tokens_out": 5,
                "tokens_reasoning": 1,
            }
        ],
        [
            {"metric": "recall.derived_source_tokens", "quantity": 100},
            {"metric": "recall.derived_returned_tokens", "quantity": 30},
        ],
    )
    assert s["tokens_total"] == 16
    assert s["context_tokens_saved"] == 70
    # Never negative, even on inconsistent rows.
    assert (
        usage_route.summarize(
            [], [{"metric": "recall.derived_returned_tokens", "quantity": 5}]
        )["context_tokens_saved"]
        == 0
    )


async def test_a_tenant_credential_reads_its_own_workspace(client, storage, caller):
    caller(tenant_id="t1", readable_tenant_ids=["t1"])
    r = await client.get(
        "/api/v1/spend", params={"tenant_id": "t1", "group_by": "day,agent_id"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "t1"
    assert body["summary"]["tokens_total"] == 1250
    assert body["summary"]["context_tokens_saved"] == 4880
    assert body["summary"]["duplicates_retired"] == 2
    [(tenant, asked)] = storage.asked
    assert tenant == "t1" and asked["group_by"] == ["day", "agent_id"]


async def test_an_agent_key_cannot_read_usage(client, storage, caller):
    caller(tenant_id="t1", agent_id="bob", readable_tenant_ids=["t1"])
    r = await client.get("/api/v1/spend")
    assert r.status_code == 403
    assert storage.asked == []


async def test_an_org_member_cannot_read_usage(client, storage, caller):
    caller(tenant_id="t1", readable_tenant_ids=["t1"], org_role="member")
    assert (await client.get("/api/v1/spend")).status_code == 403


async def test_another_tenants_usage_is_refused(client, storage, caller):
    caller(tenant_id="t1", readable_tenant_ids=["t1"])
    assert (
        await client.get("/api/v1/spend", params={"tenant_id": "t2"})
    ).status_code == 403
    assert storage.asked == []


async def test_bad_groupings_and_windows_are_422(client, storage, caller):
    caller(tenant_id="t1", readable_tenant_ids=["t1"])
    t1 = {"tenant_id": "t1"}
    assert (
        await client.get("/api/v1/spend", params={**t1, "group_by": "content"})
    ).status_code == 422
    assert (
        await client.get(
            "/api/v1/spend",
            params={
                **t1,
                "start": "2026-10-11T00:00:00Z",
                "end": "2026-10-10T00:00:00Z",
            },
        )
    ).status_code == 422
    assert (
        await client.get(
            "/api/v1/spend",
            params={
                **t1,
                "start": "2024-01-01T00:00:00Z",
                "end": "2026-01-01T00:00:00Z",
            },
        )
    ).status_code == 422
