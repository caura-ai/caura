"""EC-06 — ``/memories/scored-search/explain`` names the filter that removed a row.

Every case runs the SAME body through ``/scored-search`` and through the explain
route, and asserts both halves: the search drops the row, and the explanation
names the filter that dropped it as the first failed check. That pairing is the
point of the route — the explanation is only worth having while it agrees with
the search, and both read ``_scored_search_row_filters``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session
from tests.test_integration import PREFIX, _memory_payload, fake_embedding

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_SEARCH_PARAMS = {
    "fts_weight": 0.5,
    "freshness_floor": 0.5,
    "freshness_decay_days": 30.0,
    "recall_boost_cap": 2.0,
    "recall_decay_window_days": 7.0,
    "similarity_blend": 0.7,
}


async def _write(client: AsyncClient, tenant: str, fleet: str, keyword: str, **fields) -> str:
    payload = _memory_payload(tenant, fleet, content=f"explain probe {keyword}")
    payload["embedding"] = fake_embedding(keyword)
    payload.update(fields)
    resp = await client.post(f"{PREFIX}/memories", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _sql(stmt: str, **params) -> None:
    async with get_session() as db:
        await db.execute(text(stmt), params)


def _body(tenant: str, keyword: str, **extra) -> dict:
    return {
        "tenant_id": tenant,
        "embedding": fake_embedding(keyword),
        "query": keyword,
        "top_k": 10,
        "search_params": _SEARCH_PARAMS,
        **extra,
    }


async def _search_and_explain(client: AsyncClient, memory_id: str, body: dict) -> tuple[bool, dict]:
    found = await client.post(f"{PREFIX}/memories/scored-search", json=body)
    assert found.status_code == 200, found.text
    returned = memory_id in {r["id"] for r in found.json()}
    explained = await client.post(
        f"{PREFIX}/memories/scored-search/explain", json={**body, "memory_id": memory_id}
    )
    assert explained.status_code == 200, explained.text
    return returned, explained.json()


def _first_failed(explanation: dict) -> str | None:
    return next((c["filter"] for c in explanation["checks"] if not c["passed"]), None)


@pytest.fixture
def scope() -> tuple[str, str, str]:
    run = uuid.uuid4().hex[:8]
    return f"explain-{run}", f"fleet-{run}", f"explainkw{run}"


class TestExplainAgreesWithSearch:
    async def test_all_filters_pass_when_returned(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw))
        assert returned
        assert ex["exists"] is True
        assert _first_failed(ex) is None
        assert [c["filter"] for c in ex["checks"]] == [
            "tenant_scope",
            "deleted",
            "held",
            "embedding_missing",
            "visibility_scope",
            "status_superseded",
        ]
        assert ex["memory"]["fleet_id"] == fleet

    async def test_unknown_id(self, client: AsyncClient, scope) -> None:
        tenant, _, kw = scope
        resp = await client.post(
            f"{PREFIX}/memories/scored-search/explain",
            json={**_body(tenant, kw), "memory_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"exists": False, "checks": []}

    async def test_tenant_scope(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        returned, ex = await _search_and_explain(client, mid, _body(f"{tenant}-other", kw))
        assert not returned
        assert _first_failed(ex) == "tenant_scope"

    async def test_deleted(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        await _sql("UPDATE memories SET deleted_at = now() WHERE id = :id", id=uuid.UUID(mid))
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw))
        assert not returned
        assert _first_failed(ex) == "deleted"
        assert ex["memory"]["deleted"] is True

    async def test_held(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        await _sql("UPDATE memories SET status = 'quarantined' WHERE id = :id", id=uuid.UUID(mid))
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw))
        assert not returned
        assert _first_failed(ex) == "held"

    async def test_embedding_missing_without_keyword_match(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        await _sql("UPDATE memories SET embedding = NULL WHERE id = :id", id=uuid.UUID(mid))
        # A keyword match still admits an unembedded row ...
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw))
        assert returned and _first_failed(ex) is None
        # ... a query that does not match it lexically does not.
        returned, ex = await _search_and_explain(
            client, mid, {**_body(tenant, kw), "query": "zqxv unrelated"}
        )
        assert not returned
        assert _first_failed(ex) == "embedding_missing"
        assert ex["memory"]["has_embedding"] is False

    async def test_fleet_scope(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw, fleet_ids=[f"{fleet}-other"]))
        assert not returned
        assert _first_failed(ex) == "fleet_scope"

    async def test_visibility_scope(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw, visibility="scope_agent", agent_id="owner-agent")
        returned, ex = await _search_and_explain(
            client, mid, _body(tenant, kw, caller_agent_id="other-agent")
        )
        assert not returned
        assert _first_failed(ex) == "visibility_scope"
        # The owner itself passes the same check.
        returned, ex = await _search_and_explain(
            client, mid, _body(tenant, kw, caller_agent_id="owner-agent")
        )
        assert returned and _first_failed(ex) is None

    async def test_agent_filter(self, client: AsyncClient, scope) -> None:
        """Auto-recall filters to the agent's own writes (``filter_agent_id``)."""
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw, agent_id="writer-agent")
        returned, ex = await _search_and_explain(
            client, mid, _body(tenant, kw, filter_agent_id="reader-agent")
        )
        assert not returned
        assert _first_failed(ex) == "agent_filter"

    async def test_memory_type_filter(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        returned, ex = await _search_and_explain(
            client, mid, _body(tenant, kw, memory_type_filter="decision")
        )
        assert not returned
        assert _first_failed(ex) == "memory_type_filter"

    async def test_status_filter(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw, status_filter="archived"))
        assert not returned
        assert _first_failed(ex) == "status_filter"

    async def test_status_superseded_and_history_lift(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        await _sql("UPDATE memories SET status = 'outdated' WHERE id = :id", id=uuid.UUID(mid))
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw))
        assert not returned
        assert _first_failed(ex) == "status_superseded"
        # A63: a history query lifts the exclusion, and the check is not built.
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw, history_query=True))
        assert returned
        assert "status_superseded" not in {c["filter"] for c in ex["checks"]}

    async def test_conflicted_exact_match_carve_out(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        await _sql("UPDATE memories SET status = 'conflicted' WHERE id = :id", id=uuid.UUID(mid))
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw))
        assert returned and _first_failed(ex) is None
        returned, ex = await _search_and_explain(
            client, mid, {**_body(tenant, kw), "query": "zqxv unrelated"}
        )
        assert not returned
        assert _first_failed(ex) == "status_superseded"

    async def test_temporal_valid_at(self, client: AsyncClient, scope) -> None:
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        start = datetime.now(UTC) + timedelta(days=30)
        await _sql("UPDATE memories SET ts_valid_start = :s WHERE id = :id", s=start, id=uuid.UUID(mid))
        valid_at = (datetime.now(UTC)).isoformat()
        returned, ex = await _search_and_explain(client, mid, _body(tenant, kw, valid_at=valid_at))
        assert not returned
        assert _first_failed(ex) == "temporal_valid_at"

    async def test_first_failure_is_reported_in_search_order(self, client: AsyncClient, scope) -> None:
        """Deleted AND in another fleet: ``deleted`` comes first in the search."""
        tenant, fleet, kw = scope
        mid = await _write(client, tenant, fleet, kw)
        await _sql("UPDATE memories SET deleted_at = now() WHERE id = :id", id=uuid.UUID(mid))
        _, ex = await _search_and_explain(client, mid, _body(tenant, kw, fleet_ids=[f"{fleet}-other"]))
        failed = [c["filter"] for c in ex["checks"] if not c["passed"]]
        assert failed == ["deleted", "fleet_scope"]

    async def test_missing_memory_id_is_422(self, client: AsyncClient, scope) -> None:
        tenant, _, kw = scope
        resp = await client.post(f"{PREFIX}/memories/scored-search/explain", json=_body(tenant, kw))
        assert resp.status_code == 422
