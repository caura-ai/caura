"""``GET /api/v1/value``: who may read it, and how storage's figures become the report."""

from __future__ import annotations

import pytest

from core_api.auth import AuthContext
from core_api.routes import value as value_route

RAW = {
    "totals": {
        "reuses": 10,
        "sessions_helped": 4,
        "memories_reused": 6,
        "origin_seconds": 5400,
        "reuses_with_time": 10,
        "origin_calls": 30,
        "origin_tokens_in": 80_000,
        "origin_tokens_out": 6_000,
        "reuses_with_tokens": 3,
        "origin_turns": 25,
        "cross_agent_reuses": 7,
    },
    "sessions": 16,
    "daily": [{"day": "2026-10-11", "reuses": 10, "origin_seconds": 5400}],
    "teachers": [{"agent_id": "research-1", "reuses": 5, "origin_seconds": 3000}],
    "learners": [{"agent_id": "support-agent-5", "reuses": 4, "origin_seconds": 2000}],
    "prevented": {"conflicts_caught": 2, "outdated_kept_out": 5},
}


class FakeStorage:
    async def value_query(self, tenant_id, body):
        return RAW

    async def query_usage(self, tenant_id, body):
        return {
            "model_calls": [
                {
                    "calls": 86,
                    "tokens_in": 41_000,
                    "tokens_out": 6_000,
                    "tokens_reasoning": 0,
                }
            ]
        }


def test_the_report_states_shares_and_units():
    r = value_route.shape(RAW, {"tokens_total": 47_000, "model_calls": 86})
    assert r["sessions_helped_share"] == 0.25
    assert r["work_reused"]["minutes"] == 90.0
    assert (
        r["work_reused"]["tokens_reported"] == 86_000
        and r["work_reused"]["reuses_with_tokens"] == 3
    )
    assert r["knowledge_shared"]["cross_agent_share"] == 0.7
    assert r["caura_cost"] == {"model_tokens": 47_000, "model_calls": 86}
    empty = value_route.shape({"totals": {}, "sessions": 0}, None)
    assert empty["sessions_helped_share"] is None and empty["caura_cost"] is None


@pytest.fixture
def caller(monkeypatch):
    from core_api.app import app
    from core_api.auth import get_auth_context

    monkeypatch.setattr(value_route, "get_storage_client", lambda: FakeStorage())

    def install(**ctx):
        async def dep():
            return AuthContext(**ctx)

        app.dependency_overrides[get_auth_context] = dep

    yield install
    app.dependency_overrides.pop(get_auth_context, None)


async def test_a_tenant_credential_reads_its_value(client, caller):
    caller(tenant_id="t1", readable_tenant_ids=["t1"])
    r = await client.get("/api/v1/value", params={"tenant_id": "t1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sessions_helped"] == 4 and body["caura_cost"]["model_tokens"] == 47_000


async def test_agent_keys_org_members_and_other_tenants_are_refused(client, caller):
    caller(tenant_id="t1", agent_id="bob", readable_tenant_ids=["t1"])
    assert (
        await client.get("/api/v1/value", params={"tenant_id": "t1"})
    ).status_code == 403
    caller(tenant_id="t1", readable_tenant_ids=["t1"], org_role="member")
    assert (
        await client.get("/api/v1/value", params={"tenant_id": "t1"})
    ).status_code == 403
    caller(tenant_id="t1", readable_tenant_ids=["t1"])
    assert (
        await client.get("/api/v1/value", params={"tenant_id": "t2"})
    ).status_code == 403
