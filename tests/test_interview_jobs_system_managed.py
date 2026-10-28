"""``interview_jobs`` belongs to the interview service, for reads too.

A job document holds a node's masked activity window until synthesis commits.
Writes and deletes through the public document routes were already refused
(M-86). Reads are refused now as well, on REST and MCP, for every credential:
the service reads its jobs through storage directly. The watermark collection
stays readable, because ``caura-interviewer`` reads its cursor there.

A job keeps its events only while it can still be synthesized: the ``done``
transition drops them, and the scheduler sweep deletes finished and parked jobs
once they are older than the retention window.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import core_api.services.interview_service as interview_service
from core_api import mcp_server
from core_api.services.interview_service import (
    JOBS_COLLECTION,
    WATERMARK_COLLECTION,
    enqueue_interview_job,
    process_interview_job,
    process_pending_interview_jobs,
)
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_test_auth, new_tenant_id, uid
from tests.test_api_interview import _CANNED_REPORT, _enable_interviewer, _events


@pytest.fixture
def canned_llm(monkeypatch):
    async def _fake_chunk(prompt, config, events):
        return _CANNED_REPORT

    monkeypatch.setattr(interview_service, "_interview_chunk", _fake_chunk)


async def _job_doc(tenant_id: str, doc_id: str) -> dict | None:
    sc = interview_service.get_storage_client()
    return await sc.get_document(tenant_id, JOBS_COLLECTION, doc_id, read=False)


async def _enqueue(tenant_id: str) -> str:
    return await enqueue_interview_job(
        tenant_id=tenant_id,
        fleet_id=None,
        agent_id=f"agent-{uid()}",
        node_id=f"node-{uid()}",
        command_id="cmd-1",
        cursor_from=0,
        cursor_to=10,
        events=_events(),
    )


async def test_rest_document_reads_of_jobs_are_refused(client):
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _enable_interviewer(client, tenant_id, headers)
    doc_id = await _enqueue(tenant_id)

    # Each request is made only when awaited, so a failing assertion leaves no
    # un-awaited request behind.
    reads = [
        lambda: client.get(
            f"/api/v1/documents?tenant_id={tenant_id}&collection={JOBS_COLLECTION}",
            headers=headers,
        ),
        lambda: client.get(
            f"/api/v1/documents/{doc_id}?tenant_id={tenant_id}&collection={JOBS_COLLECTION}",
            headers=headers,
        ),
        lambda: client.post(
            "/api/v1/documents/query",
            json={"tenant_id": tenant_id, "collection": JOBS_COLLECTION},
            headers=headers,
        ),
    ]
    for read in reads:
        resp = await read()
        assert resp.status_code == 400, resp.text
        assert "events" not in resp.text


async def test_the_watermark_stays_readable(client):
    tenant_id, headers = get_test_auth(new_tenant_id())
    resp = await client.get(
        f"/api/v1/documents/node-x?tenant_id={tenant_id}&collection={WATERMARK_COLLECTION}",
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("op", ["read", "query", "search"])
async def test_mcp_document_reads_of_jobs_are_refused(mcp_env, monkeypatch, op):
    sc = stub_storage_client(
        monkeypatch,
        get_document={
            "collection": JOBS_COLLECTION,
            "doc_id": "j",
            "data": {"events": ["x"]},
        },
        query_documents=[],
        search_documents_vector=[],
    )
    monkeypatch.setattr(
        "common.embedding.get_embedding", AsyncMock(return_value=[0.1] * 8)
    )
    out = await mcp_server.caura_doc(
        op=op, collection=JOBS_COLLECTION, doc_id="j", query="q"
    )
    assert "error" in parse_envelope(out), out
    sc.get_document.assert_not_awaited()
    sc.query_documents.assert_not_awaited()
    sc.search_documents_vector.assert_not_awaited()


async def test_done_drops_the_events(client, canned_llm):
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _enable_interviewer(client, tenant_id, headers)
    doc_id = await _enqueue(tenant_id)
    assert (await _job_doc(tenant_id, doc_id))["data"]["events"]

    await process_interview_job(tenant_id, doc_id)
    data = (await _job_doc(tenant_id, doc_id))["data"]
    assert data["status"] == "done"
    assert "events" not in data


async def test_the_sweep_purges_finished_and_parked_jobs_past_retention(
    client, canned_llm, monkeypatch
):
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _enable_interviewer(client, tenant_id, headers)
    done_id = await _enqueue(tenant_id)
    await process_interview_job(tenant_id, done_id)

    parked_id = await enqueue_interview_job(
        tenant_id=tenant_id,
        fleet_id=None,
        agent_id=f"agent-{uid()}",
        node_id=f"node-{uid()}",
        command_id="cmd-2",
        cursor_from=0,
        cursor_to=10,
        events=_events(),
    )
    sc = interview_service.get_storage_client()
    doc = await _job_doc(tenant_id, parked_id)
    await sc.upsert_document(
        {
            "tenant_id": tenant_id,
            "collection": JOBS_COLLECTION,
            "doc_id": parked_id,
            "data": {**doc["data"], "status": "failed_permanent"},
        }
    )

    # Inside the window: kept.
    await process_pending_interview_jobs()
    assert await _job_doc(tenant_id, done_id) is not None
    assert await _job_doc(tenant_id, parked_id) is not None

    # Past it: both gone.
    monkeypatch.setattr(
        interview_service, "INTERVIEW_JOB_RETENTION_DAYS", 0, raising=False
    )
    summary = await process_pending_interview_jobs()
    assert await _job_doc(tenant_id, done_id) is None
    assert await _job_doc(tenant_id, parked_id) is None
    assert summary.get("jobs_purged", 0) >= 2


async def test_a_job_with_no_readable_timestamp_is_kept():
    """Retention needs to know when a job was last written. A job that does
    not say is left alone rather than guessed old."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    rows = [
        {"doc_id": "old", "updated_at": (now - timedelta(days=60)).isoformat()},
        {"doc_id": "garbled", "updated_at": "last tuesday"},
        {"doc_id": "unstamped"},
    ]
    sc = AsyncMock()
    sc.query_documents = AsyncMock(
        side_effect=lambda q: rows if q["where"]["status"] == "done" else []
    )
    sc.delete_document = AsyncMock(return_value=True)

    assert await interview_service._purge_terminal_jobs(sc, "t1", now) == 1
    assert [c.args[2] for c in sc.delete_document.await_args_list] == ["old"]


async def test_a_purge_failure_leaves_the_queue_to_run(client, canned_llm, monkeypatch):
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _enable_interviewer(client, tenant_id, headers)
    job_id = await _enqueue(tenant_id)

    async def _purge_fails(*args, **kwargs):
        raise RuntimeError("storage blip")

    monkeypatch.setattr(interview_service, "_purge_terminal_jobs", _purge_fails)
    await process_pending_interview_jobs()

    assert (await _job_doc(tenant_id, job_id))["data"]["status"] == "done"


async def test_an_unscoped_search_never_returns_a_job(client, monkeypatch):
    """A search that names no collection spans the tenant. A job is not among
    its results, even one planted with an embedding (the service writes jobs
    without one)."""
    from core_api.constants import VECTOR_DIM
    from core_api.routes import documents as docs_route

    tenant_id, headers = get_test_auth(new_tenant_id())
    vector = [0.1] * VECTOR_DIM
    monkeypatch.setattr(docs_route, "get_embedding", AsyncMock(return_value=vector))
    sc = interview_service.get_storage_client()
    for collection, doc_id in ((JOBS_COLLECTION, "planted"), ("notes", "note")):
        await sc.upsert_document_xmax(
            {
                "tenant_id": tenant_id,
                "collection": collection,
                "doc_id": doc_id,
                "data": {"status": "done", "events": _events()},
                "embedding": vector,
            }
        )

    resp = await client.post(
        "/api/v1/documents/search",
        json={"tenant_id": tenant_id, "query": "activity", "top_k": 10},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    found = [(item["collection"], item["doc_id"]) for item in resp.json()["items"]]
    assert found == [("notes", "note")]


async def test_an_unscoped_mcp_search_never_returns_a_job(mcp_env, monkeypatch):
    from core_api.constants import VECTOR_DIM

    stub_storage_client(
        monkeypatch,
        search_documents_vector=[
            {
                "collection": JOBS_COLLECTION,
                "doc_id": "planted",
                "tenant_id": "test-tenant",
                "data": {"events": ["x"]},
                "similarity": 0.9,
            },
            {
                "collection": "notes",
                "doc_id": "note",
                "tenant_id": "test-tenant",
                "data": {},
                "similarity": 0.5,
            },
        ],
    )
    monkeypatch.setattr(
        "common.embedding.get_embedding", AsyncMock(return_value=[0.1] * VECTOR_DIM)
    )
    out = await mcp_server.caura_doc(op="search", query="activity")
    assert [row["collection"] for row in parse_envelope(out)["results"]] == ["notes"]


async def test_the_sweep_purges_old_jobs_of_a_tenant_that_turned_the_interviewer_off(
    client, canned_llm, monkeypatch
):
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _enable_interviewer(client, tenant_id, headers)
    job_id = await _enqueue(tenant_id)
    await process_interview_job(tenant_id, job_id)
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"interviewer": {"enabled": False}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    monkeypatch.setattr(
        interview_service, "INTERVIEW_JOB_RETENTION_DAYS", 0, raising=False
    )
    await process_pending_interview_jobs()
    assert await _job_doc(tenant_id, job_id) is None
