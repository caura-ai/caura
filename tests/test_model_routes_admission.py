"""Admission on the REST routes that call a model, and on ingest commit.

Seven REST routes carried a rate limit, and none of them called a model. Ingest
preview makes one extraction call per section of the document, four at a time,
and like insights, evolve report, interview submit and file ingest it ran with
no budget, no per-tenant slot and no meter. Document search embedded its query
with neither. Ingest commit took any number of facts on the single-write budget,
while the bulk route takes 100 on a stricter one.

Each credential now gets one model budget across those routes (10 a minute by
default), and each tenant one across its credentials (50 a minute). Each tenant
gets two model calls in flight per process. A preview of more than 32 sections
is refused before any call, and every extraction call is metered.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import core_api.middleware.per_tenant_concurrency as ptc
from core_api.config import settings
from core_api.constants import BULK_MAX_ITEMS
from core_api.middleware.rate_limit import limiter
from core_api.routes import documents as documents_routes
from core_api.routes import memories as memories_routes
from core_api.services import ingest_service, usage_service
from core_api.services.ingest_chunking import Section
from tests.conftest import get_test_auth, uid

_LONG = "A paragraph long enough to be sent to the extraction model. " * 3


@pytest.fixture
def limited():
    """The suite runs with the limiter off; these tests need it on, with
    empty buckets."""
    prev = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    yield
    limiter.enabled = prev
    limiter.reset()


@pytest.fixture
def fresh_slots():
    ptc._reset_for_tests()
    yield
    ptc._reset_for_tests()


async def _previews(c: AsyncClient, n: int) -> list[int]:
    """``n`` previews too short to reach the model; each still spends budget."""
    codes = []
    for _ in range(n):
        resp = await c.post(
            "/api/v1/ingest/preview", json={"tenant_id": "default", "content": "short"}
        )
        codes.append(resp.status_code)
    return codes


# ── One model budget per credential, and one per tenant ──


async def test_a_credential_gets_ten_model_calls_a_minute(client, limited):
    assert await _previews(client, 11) == [200] * 10 + [429]


_OTHER_MODEL_ROUTES = {
    "insights": (
        "/api/v1/insights/generate",
        {"json": {"tenant_id": "default", "focus": "stale"}},
    ),
    "evolve": (
        "/api/v1/evolve/report",
        {
            "json": {
                "tenant_id": "default",
                "outcome": "the deploy failed",
                "outcome_type": "failure",
            }
        },
    ),
    "interview": (
        "/api/v1/interview/submit",
        {
            "json": {
                "tenant_id": "default",
                "node_id": "node-1",
                "agent_id": "worker-1",
                "cursor_from": 0,
                "cursor_to": 0,
                "events": [
                    {
                        "seq": 0,
                        "ts": "2026-11-07T00:00:00Z",
                        "role": "user",
                        "kind": "message",
                        "content": "hello",
                    }
                ],
            }
        },
    ),
    "file": (
        "/api/v1/ingest/file",
        {
            "data": {"tenant_id": "default"},
            "files": {"file": ("notes.txt", b"short", "text/plain")},
        },
    ),
}


@pytest.mark.parametrize("route", sorted(_OTHER_MODEL_ROUTES))
async def test_every_model_route_draws_on_the_same_budget(client, limited, route):
    assert await _previews(client, 10) == [200] * 10
    path, kwargs = _OTHER_MODEL_ROUTES[route]
    resp = await client.post(path, **kwargs)
    assert resp.status_code == 429, resp.text


async def test_a_tenant_shares_one_model_budget_across_its_credentials(client, limited):
    """Six credentials (client addresses here, which name the bucket when no
    credential does) stay under their own 10 a minute, and the tenant's 50 a
    minute still stops the 51st call."""
    from core_api.app import app

    codes: list[int] = []
    for n in range(6):
        transport = ASGITransport(app=app, client=(f"198.51.100.{n + 1}", 1))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            codes += await _previews(c, 9)
    assert codes == [200] * 50 + [429] * 4


# ── Two model calls in flight per tenant ──


async def test_a_preview_storm_leaves_another_tenant_alone(
    client, fresh_slots, monkeypatch
):
    """Two previews waiting on the model fill tenant A's slots. A's next
    preview is refused at once, and tenant B's preview and recall go ahead."""
    entered = 0
    both_in = asyncio.Event()
    release = asyncio.Event()

    async def _held(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_in.set()
        await release.wait()
        return []

    monkeypatch.setattr(ingest_service, "_chunk_content", _held)
    monkeypatch.setattr(
        "core_api.services.memory_service.search_memories",
        AsyncMock(return_value=[]),
    )
    _, admin = get_test_auth()
    storm, calm = f"storm-{uid()}", f"calm-{uid()}"

    def _preview(tenant: str, content: str = _LONG):
        return client.post(
            "/api/v1/ingest/preview",
            json={"tenant_id": tenant, "content": content},
            headers=admin,
        )

    held = [asyncio.create_task(_preview(storm)) for _ in range(2)]
    try:
        await asyncio.wait_for(both_in.wait(), 10)
        try:
            third = (await asyncio.wait_for(_preview(storm), 2)).status_code
        except TimeoutError:
            third = None  # admitted, and waiting on the model beside the other two
        other_preview = await _preview(calm, "short")
        other_recall = await client.post(
            "/api/v1/recall",
            json={"tenant_id": calm, "query": "anything"},
            headers=admin,
        )
    finally:
        release.set()
        await asyncio.gather(*held)

    assert third == 429
    assert other_preview.status_code == 200, other_preview.text
    assert other_recall.status_code == 200, other_recall.text


# ── 32 sections a preview ──


def _sections(n: int) -> list[Section]:
    return [Section(text=f"section {i} text", token_count=3) for i in range(n)]


@pytest.mark.parametrize(("sections", "status"), [(32, 200), (33, 413)])
async def test_a_preview_past_32_sections_makes_no_model_call(
    client, monkeypatch, sections, status
):
    calls: list[str] = []

    async def _counted(text, **kwargs):
        calls.append(text)
        return []

    monkeypatch.setattr(ingest_service, "_chunk_content", _counted)
    monkeypatch.setattr(ingest_service, "_sections_of", lambda _: _sections(sections))
    _, admin = get_test_auth()
    resp = await client.post(
        "/api/v1/ingest/preview",
        json={"tenant_id": f"cap-{uid()}", "content": _LONG},
        headers=admin,
    )
    assert resp.status_code == status, resp.text
    assert len(calls) == (sections if status == 200 else 0)


# ── Previews are metered ──


async def test_a_preview_is_metered_one_unit_per_model_call(client, monkeypatch):
    recorded: list[tuple[str, str, int]] = []

    async def _meter(*, tenant_id, operation, count):
        recorded.append((tenant_id, operation, count))

    async def _facts(text, **kwargs):
        return [{"content": f"a fact from {text}", "suggested_type": "fact"}]

    monkeypatch.setattr(
        usage_service, "get_hooks", lambda: SimpleNamespace(usage_meter=_meter)
    )
    monkeypatch.setattr(ingest_service, "_chunk_content", _facts)
    monkeypatch.setattr(ingest_service, "_sections_of", lambda _: _sections(3))
    resp = await client.post(
        "/api/v1/ingest/preview", json={"tenant_id": "default", "content": _LONG}
    )
    assert resp.status_code == 200, resp.text
    assert ("default", "ingest", 3) in recorded, recorded


# ── Document search ──


async def test_document_search_spends_the_search_budget(client, limited, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_search", "1/hour")
    monkeypatch.setattr(documents_routes, "get_embedding", AsyncMock(return_value=None))
    body = {"tenant_id": "default", "query": "release notes"}
    first = await client.post("/api/v1/documents/search", json=body)
    again = await client.post("/api/v1/documents/search", json=body)
    assert first.status_code != 429, first.text
    assert again.status_code == 429, again.text


# ── Ingest commit keeps the bulk contract ──


def _facts_body(n: int) -> dict:
    return {
        "tenant_id": "default",
        "facts": [{"content": f"commit fact {i} {uid()}"} for i in range(n)],
    }


@pytest.fixture
def stub_commit(monkeypatch):
    written = AsyncMock(
        return_value={
            "run_id": "run-1",
            "memories_created": 0,
            "skipped_duplicates": 0,
            "errored": 0,
        }
    )
    monkeypatch.setattr(memories_routes, "ingest_commit", written)
    return written


@pytest.mark.parametrize(
    ("facts", "status"), [(0, 422), (BULK_MAX_ITEMS, 200), (BULK_MAX_ITEMS + 1, 422)]
)
async def test_a_commit_takes_one_to_100_facts(client, stub_commit, facts, status):
    _, admin = get_test_auth()
    resp = await client.post(
        "/api/v1/ingest/commit", json=_facts_body(facts), headers=admin
    )
    assert resp.status_code == status, resp.text
    assert stub_commit.await_count == (1 if status == 200 else 0)


async def test_a_commit_spends_the_bulk_budget(
    client, limited, stub_commit, monkeypatch
):
    """Commit and the bulk route draw on one budget: two bulk writes use it up
    and the commit after them is refused."""
    monkeypatch.setattr(settings, "rate_limit_write_bulk", "2/hour")
    for _ in range(2):
        resp = await client.post(
            "/api/v1/memories/bulk",
            json={
                "tenant_id": "default",
                "agent_id": "bulk-probe",
                "items": [{"content": f"bulk probe {uid()}", "memory_type": "fact"}],
            },
            headers={"X-Bulk-Attempt-Id": f"attempt-{uid()}"},
        )
        assert resp.status_code < 300, resp.text

    resp = await client.post("/api/v1/ingest/commit", json=_facts_body(1))
    assert resp.status_code == 429, resp.text
