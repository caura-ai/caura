"""``GET /api/v1/reports/evidence-coverage``: who may read it, and which denominator it reports."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.constants import PLACEHOLDER_AGENT_IDS
from core_api.agent_ids import DEFAULT_AGENT_ID, RESERVED_MAIN_AGENT_ID
from core_api.auth import AuthContext
from core_api.routes import evidence_coverage as route


def _counts(rows: int, provenance: int, external: int) -> dict:
    return {
        "rows": rows,
        "with_agent": rows,
        "with_written_at": rows,
        "with_trace_id": provenance,
        "provenance": provenance,
        "with_tool_call_id": external,
        "with_real_source_uri": 0,
        "non_evidence_source_uri": 0,
        "external_evidence": external,
    }


RAW = {
    "memories": _counts(200, 20, 10),
    "memories_in_window": _counts(40, 20, 10),
    "recall": {
        "events": 12,
        "events_with_results": 10,
        "returned": 51,
        "memory_gone": 1,
        **_counts(50, 48, 20),
    },
    "verification": {"unverified": 190, "tool_observed": 10},
}


def test_logged_returns_are_the_denominator_when_there_are_any():
    r = route.shape(RAW, recall_logging={"recall_logging_enabled": True})
    assert r["denominator"] == "recall_candidates"
    assert r["provenance"] == {"share": 0.96, "target": 0.95, "meets_target": True}
    assert r["external_evidence"]["share"] == 0.4
    assert r["external_evidence"]["meets_target"] is True
    assert r["memories"]["provenance_share"] == 0.1
    assert r["memories_written_in_window"]["provenance_share"] == 0.5
    assert r["verification_status"] == {"unverified": 190, "tool_observed": 10}


def test_without_logged_returns_it_falls_back_to_memories():
    raw = {**RAW, "recall": {"events": 0, "returned": 0, **_counts(0, 0, 0)}}
    r = route.shape(raw, recall_logging={"recall_logging_enabled": False})
    assert r["denominator"] == "memories"
    assert r["provenance"]["share"] == 0.1 and r["provenance"]["meets_target"] is False
    assert r["external_evidence"]["share"] == 0.05
    assert r["recall_candidates"]["provenance_share"] is None

    empty = route.shape({}, recall_logging={})
    assert (
        empty["provenance"]["share"] is None
        and empty["provenance"]["meets_target"] is None
    )


def test_the_placeholder_list_covers_the_ids_core_api_treats_as_unattributed():
    assert {"", DEFAULT_AGENT_ID, RESERVED_MAIN_AGENT_ID} <= set(PLACEHOLDER_AGENT_IDS)


class FakeStorage:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def evidence_coverage(self, tenant_id, body):
        self.calls.append((tenant_id, body))
        return RAW


@pytest.fixture
def caller(monkeypatch):
    from core_api.app import app
    from core_api.auth import get_auth_context

    storage = FakeStorage()
    monkeypatch.setattr(route, "get_storage_client", lambda: storage)

    async def config(tenant_id):
        return SimpleNamespace(
            recall_logging_enabled=True, search_recall_logging_enabled=False
        )

    monkeypatch.setattr(route, "resolve_config", config)

    def install(**ctx):
        async def dep():
            return AuthContext(**ctx)

        app.dependency_overrides[get_auth_context] = dep
        return storage

    yield install
    app.dependency_overrides.pop(get_auth_context, None)


async def test_a_tenant_credential_reads_its_coverage(client, caller):
    storage = caller(tenant_id="t1", readable_tenant_ids=["t1"])
    r = await client.get(
        "/api/v1/reports/evidence-coverage",
        params={"tenant_id": "t1", "window_days": 7},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "t1" and body["window_days"] == 7
    assert body["recall_logging"] == {
        "recall_logging_enabled": True,
        "search_recall_logging_enabled": False,
    }
    assert body["denominator"] == "recall_candidates"
    assert storage.calls[0][0] == "t1"
    assert set(storage.calls[0][1]) == {"start", "end"}


async def test_agent_keys_org_members_and_other_tenants_are_refused(client, caller):
    path = "/api/v1/reports/evidence-coverage"
    caller(tenant_id="t1", agent_id="bob", readable_tenant_ids=["t1"])
    assert (await client.get(path, params={"tenant_id": "t1"})).status_code == 403
    caller(tenant_id="t1", readable_tenant_ids=["t1"], org_role="member")
    assert (await client.get(path, params={"tenant_id": "t1"})).status_code == 403
    caller(tenant_id="t1", readable_tenant_ids=["t1"])
    assert (await client.get(path, params={"tenant_id": "t2"})).status_code == 403
    assert (
        await client.get(path, params={"tenant_id": "t1", "window_days": 0})
    ).status_code == 422
    assert (
        await client.get(path, params={"tenant_id": "t1", "window_days": 400})
    ).status_code == 422
