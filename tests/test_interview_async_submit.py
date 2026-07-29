"""Async interview submit (#665): persist-and-accept route + job processor.

With ``interview_async_submit=True`` the submit route masks and persists
the window as a durable ``interview_jobs`` doc, advances the watermark,
and replies 200 ``accepted`` immediately; synthesis runs in
``process_interview_job`` (fire-and-forget at submit + the scheduler
sweep). The conftest env default pins the flag OFF so the legacy suite
(``tests/test_api_interview.py``) keeps asserting the inline path; tests
here flip it on explicitly. The LLM map call is stubbed like the legacy
suite (``canned_llm``); everything else runs the real in-process stack.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import core_api.routes.interview as interview_route
import core_api.services.interview_service as interview_service
from core_api.services.interview_service import (
    JOBS_COLLECTION,
    enqueue_interview_job,
    interview_job_doc_id,
    process_interview_job,
    process_pending_interview_jobs,
)
from tests.conftest import get_admin_headers, get_test_auth, uid
from tests.test_api_interview import (
    _CANNED_REPORT,
    _enable_interviewer,
    _events,
    _payload,
)


# ── helpers ──


@pytest.fixture
def async_submit(monkeypatch):
    """Flip the #665 flag on (the conftest test default pins it off)."""
    monkeypatch.setattr(interview_route.app_settings, "interview_async_submit", True)


@pytest.fixture
def canned_llm(monkeypatch):
    """Stub the map-phase LLM call with the legacy suite's canned report."""
    calls: list[str] = []

    async def _fake_chunk(prompt, config, events):
        calls.append(prompt)
        return _CANNED_REPORT

    monkeypatch.setattr(interview_service, "_interview_chunk", _fake_chunk)
    return calls


async def _job_doc(tenant_id: str, doc_id: str) -> dict | None:
    sc = interview_service.get_storage_client()
    return await sc.get_document(tenant_id, JOBS_COLLECTION, doc_id, read=False)


async def _overwrite_job_data(tenant_id: str, doc_id: str, **overrides) -> dict:
    """Merge ``overrides`` onto the job doc's current data (test harness for
    forcing lifecycle states the public API won't produce on demand)."""
    sc = interview_service.get_storage_client()
    doc = await _job_doc(tenant_id, doc_id)
    assert doc is not None
    data = {**doc["data"], **overrides}
    await sc.upsert_document(
        {
            "tenant_id": tenant_id,
            "collection": JOBS_COLLECTION,
            "doc_id": doc_id,
            "data": data,
        }
    )
    return data


async def _drain_inflight() -> None:
    """Await the route's fire-and-forget synthesis tasks for determinism."""
    tasks = list(interview_route._inflight_jobs)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _interviewer_memories(client, tenant_id: str, agent_id: str, headers: dict) -> list[dict]:
    listing = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&agent_id={agent_id}", headers=headers
    )
    assert listing.status_code == 200
    rows = listing.json()["items"] if isinstance(listing.json(), dict) else listing.json()
    return [r for r in rows if (r.get("metadata") or {}).get("source") == "interviewer"]


def _enqueue_kwargs(tenant_id: str, node_id: str, agent_id: str, **kw) -> dict:
    kwargs = {
        "tenant_id": tenant_id,
        "fleet_id": None,
        "agent_id": agent_id,
        "node_id": node_id,
        "command_id": "cmd-1",
        "cursor_from": 0,
        "cursor_to": 10,
        "events": _events(),
    }
    kwargs.update(kw)
    return kwargs


# ── (a) accept fast, persist masked job, advance watermark ──


async def test_async_submit_accepts_with_watermark_and_masked_job(
    client, canned_llm, async_submit
):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    events = _events()
    events[0]["content"] = "emailed ran@caura.ai about the ingest refactor"

    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, node_id, agent_id, events=events),
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["watermark"] == 10  # == cursor_to: advanced at accept time
    assert body["memories_written"] == 0
    assert body["errors"] == 0

    doc = await _job_doc(tenant_id, interview_job_doc_id(node_id, 0, 10))
    assert doc is not None
    data = doc["data"]
    # The fire-and-forget task may already be running (or finished).
    assert data["status"] in {"pending", "processing", "done"}
    assert data["node_id"] == node_id
    assert data["agent_id"] == agent_id
    assert data["cursor_from"] == 0 and data["cursor_to"] == 10
    # The job doc must never store unmasked PII (masked BEFORE persist).
    assert "ran@caura.ai" not in data["events"][0]["content"]
    await _drain_inflight()


# ── (b) processor writes memories and marks the job done ──


async def test_processing_writes_typed_memories_and_marks_done(client, canned_llm, async_submit):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"

    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, node_id, agent_id),
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    await _drain_inflight()

    # Drive the processor directly for determinism (no-op if the
    # fire-and-forget task already completed the job).
    doc_id = interview_job_doc_id(node_id, 0, 10)
    await process_interview_job(tenant_id, doc_id)

    doc = await _job_doc(tenant_id, doc_id)
    assert doc["data"]["status"] == "done"
    assert doc["data"]["memories_written"] == 3  # episode + decision + outcome
    assert doc["data"]["completed_at"]

    ours = await _interviewer_memories(client, tenant_id, agent_id, headers)
    assert len(ours) == 3
    assert sorted(r["memory_type"] for r in ours) == ["decision", "episode", "outcome"]
    for row in ours:
        assert (row.get("metadata") or {}).get("node_id") == node_id
        assert (row.get("metadata") or {}).get("written_by") == "interviewer"


# ── (c) duplicate submits upsert one job; re-synthesis dedups ──


async def test_resubmit_same_window_is_idempotent(client, canned_llm, async_submit):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    payload = _payload(tenant_id, node_id, agent_id)

    first = await client.post("/api/v1/interview/submit", json=payload, headers=headers)
    assert first.status_code == 200
    await _drain_inflight()
    doc_id = interview_job_doc_id(node_id, 0, 10)
    await process_interview_job(tenant_id, doc_id)
    assert len(await _interviewer_memories(client, tenant_id, agent_id, headers)) == 3

    # Re-enqueue the SAME window directly (deterministic — no route task
    # racing the assertion): the deterministic doc id resolves to the same
    # job, and a "done" job is NOT flipped back to pending (#667
    # no-downgrade ladder) — the window was already synthesized.
    doc_id2 = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))
    assert doc_id2 == doc_id
    assert (await _job_doc(tenant_id, doc_id))["data"]["status"] == "done"
    assert await process_interview_job(tenant_id, doc_id) is None  # done → no-op

    # Crash-recovery replay (the stale-processing sweep's shape): force
    # the done job back through synthesis — the deterministic bulk attempt
    # id dedups every row as duplicate_attempt, so 0 new memories land.
    await _overwrite_job_data(tenant_id, doc_id, status="pending")
    result = await process_interview_job(tenant_id, doc_id)
    assert result is not None
    assert result["status"] == "committed"
    assert result["memories_written"] == 0
    assert (await _job_doc(tenant_id, doc_id))["data"]["status"] == "done"
    assert len(await _interviewer_memories(client, tenant_id, agent_id, headers)) == 3


async def test_enqueue_over_processing_job_does_not_downgrade(client, canned_llm):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    # Simulate a concurrent processor owning the job.
    started_at = datetime.now(UTC).isoformat()
    await _overwrite_job_data(
        tenant_id, doc_id, status="processing", attempts=1, processing_started_at=started_at
    )

    doc_id2 = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))
    assert doc_id2 == doc_id
    after = (await _job_doc(tenant_id, doc_id))["data"]
    assert after["status"] == "processing"
    assert after["attempts"] == 1
    # The enqueue skipped the upsert entirely — nothing was refreshed.
    assert after["processing_started_at"] == started_at


async def test_duplicate_enqueue_preserves_attempts(client, monkeypatch):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    async def _always_boom(prompt, config, events):
        raise RuntimeError("llm down")

    monkeypatch.setattr(interview_service, "_interview_chunk", _always_boom)
    assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_transient"}
    doc = await _job_doc(tenant_id, doc_id)
    assert doc["data"]["status"] == "pending"
    assert doc["data"]["attempts"] == 1

    # A plugin resubmit of the same failing window must NOT reset the
    # retry budget (it would cycle past interview_job_max_attempts forever).
    assert await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id)) == doc_id
    doc = await _job_doc(tenant_id, doc_id)
    assert doc["data"]["status"] == "pending"
    assert doc["data"]["attempts"] == 1


async def test_enqueue_prior_read_failure_raises_and_leaves_done_job_untouched(
    client, canned_llm, monkeypatch
):
    """When the prior-doc read fails during enqueue, the doc's status is
    UNKNOWN — the enqueue must RAISE (the route 500s and the plugin
    resubmits the window next tick; returning success on a first-time
    submit would lose the window, see the round-5 test below) and must
    NOT upsert (writing "pending" over a "done" job would re-open the
    consumed window and reset the retry budget) (#667)."""
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))
    result = await process_interview_job(tenant_id, doc_id)
    assert result is not None and result["status"] == "committed"
    before = (await _job_doc(tenant_id, doc_id))["data"]
    assert before["status"] == "done"

    sc = interview_service.get_storage_client()
    real_get = sc.get_document
    boom = {"on": True}

    async def _flaky_get(tenant, collection, did, **kw):
        if boom["on"] and collection == JOBS_COLLECTION and did == doc_id:
            raise RuntimeError("storage read blip")
        return await real_get(tenant, collection, did, **kw)

    monkeypatch.setattr(sc, "get_document", _flaky_get)
    with pytest.raises(RuntimeError, match="storage read blip"):
        await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))
    boom["on"] = False
    after = (await _job_doc(tenant_id, doc_id))["data"]
    assert after == before  # untouched — still done, attempts unchanged


async def test_first_submit_prior_read_failure_500s_without_advancing_watermark(
    client, canned_llm, async_submit, monkeypatch
):
    """Round 5 (#667): FIRST-TIME submit with a transient storage blip on
    the prior-doc read. No job doc exists yet, so swallowing the error and
    returning success would let the route advance the watermark and 200 —
    the plugin prunes its buffer and the window is permanently lost. The
    enqueue must raise → route 500s → the plugin keeps the window and
    resubmits next tick."""
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = interview_job_doc_id(node_id, 0, 10)

    sc = interview_service.get_storage_client()
    real_get = sc.get_document
    boom = {"on": True}

    async def _flaky_get(tenant, collection, did, **kw):
        if boom["on"] and collection == JOBS_COLLECTION and did == doc_id:
            raise RuntimeError("storage read blip")
        return await real_get(tenant, collection, did, **kw)

    monkeypatch.setattr(sc, "get_document", _flaky_get)

    # Service level: the transient error propagates out of the enqueue.
    with pytest.raises(RuntimeError, match="storage read blip"):
        await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    # Route level: 500 out; watermark NOT advanced; nothing persisted.
    # A dedicated non-raising client so the test observes the response the
    # plugin would see (the shared ``client`` fixture's ASGI transport
    # re-raises app exceptions — established pattern, see test_role_flag).
    from httpx import ASGITransport, AsyncClient

    from core_api.app import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw_client:
        resp = await raw_client.post(
            "/api/v1/interview/submit",
            json=_payload(tenant_id, node_id, agent_id),
            headers=headers,
        )
    assert resp.status_code == 500

    boom["on"] = False
    assert await _job_doc(tenant_id, doc_id) is None
    watermark_doc = await sc.get_document(
        tenant_id,
        interview_service.WATERMARK_COLLECTION,
        interview_service.watermark_doc_id(node_id),
        read=False,
    )
    assert watermark_doc is None  # first-time: never created, no advance


# ── (d) failure → pending w/ attempts; exhaustion → failed_permanent ──


async def test_failed_synthesis_returns_to_pending_then_retry_succeeds(client, monkeypatch):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    boom = {"on": True}

    async def _flaky_chunk(prompt, config, events):
        if boom["on"]:
            raise RuntimeError("llm down")
        return _CANNED_REPORT

    monkeypatch.setattr(interview_service, "_interview_chunk", _flaky_chunk)

    assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_transient"}  # never raises
    doc = await _job_doc(tenant_id, doc_id)
    assert doc["data"]["status"] == "pending"
    assert doc["data"]["attempts"] == 1

    boom["on"] = False
    result = await process_interview_job(tenant_id, doc_id)
    assert result is not None and result["status"] == "committed"
    doc = await _job_doc(tenant_id, doc_id)
    assert doc["data"]["status"] == "done"
    assert doc["data"]["attempts"] == 2
    assert len(await _interviewer_memories(client, tenant_id, agent_id, headers)) == 3


async def test_job_attempts_exhaustion_parks_as_failed_permanent(client, monkeypatch):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    async def _always_boom(prompt, config, events):
        raise RuntimeError("llm down")

    monkeypatch.setattr(interview_service, "_interview_chunk", _always_boom)
    max_attempts = interview_route.app_settings.interview_job_max_attempts
    for expected_attempts in range(1, max_attempts + 1):
        assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_transient"}
        doc = await _job_doc(tenant_id, doc_id)
        assert doc["data"]["status"] == "pending"
        assert doc["data"]["attempts"] == expected_attempts

    # attempts == max → the next run parks the job instead of retrying,
    # returning the sentinel so the sweep can count it as jobs_parked.
    assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_permanent"}
    assert (await _job_doc(tenant_id, doc_id))["data"]["status"] == "failed_permanent"


async def test_concurrent_processors_increment_attempts_from_fresh_base(client, monkeypatch):
    """Two racing processors both read a stale ``attempts=N`` snapshot; the
    processing-transition write must increment from the FRESH stored value
    (N → N+1 → N+2), not both write the stale N+1 — undercounting would let
    a job exceed interview_job_max_attempts (#667). Simulated by doctoring
    the second run's top-level fetch to return the pre-increment snapshot
    while _set_state's fresh re-fetch sees the real doc."""
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    async def _always_boom(prompt, config, events):
        raise RuntimeError("llm down")

    monkeypatch.setattr(interview_service, "_interview_chunk", _always_boom)

    sc = interview_service.get_storage_client()
    real_get = sc.get_document
    real_upsert = sc.upsert_document
    stale = {"armed": False}
    processing_attempts: list[int] = []

    async def _stale_once_get(tenant, collection, did, **kw):
        doc = await real_get(tenant, collection, did, **kw)
        if stale["armed"] and collection == JOBS_COLLECTION and did == doc_id:
            # Only the run's FIRST fetch (the processor's snapshot read) is
            # stale; _set_state's fresh re-fetch passes through.
            stale["armed"] = False
            doc = {**doc, "data": {**doc["data"], "status": "pending", "attempts": 0}}
        return doc

    async def _spy_upsert(payload):
        data = payload.get("data") or {}
        if payload.get("collection") == JOBS_COLLECTION and data.get("status") == "processing":
            processing_attempts.append(data.get("attempts"))
        return await real_upsert(payload)

    monkeypatch.setattr(sc, "get_document", _stale_once_get)
    monkeypatch.setattr(sc, "upsert_document", _spy_upsert)

    assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_transient"}  # attempts 0 → 1
    stale["armed"] = True  # second run reads the pre-increment snapshot
    assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_transient"}
    # Strictly 1 → 2 — the stale-snapshot run must NOT re-write 1.
    assert processing_attempts == [1, 2]


async def test_pending_reset_retry_survives_one_write_failure(client, monkeypatch):
    """Synthesis fails AND the first pending-reset upsert fails: the reset's
    extra best-effort attempt must still land the job in "pending" — a job
    left in "processing" waits on the (slower) stale sweep."""
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    async def _boom_chunk(prompt, config, events):
        raise RuntimeError("llm down")

    monkeypatch.setattr(interview_service, "_interview_chunk", _boom_chunk)

    sc = interview_service.get_storage_client()
    real_upsert = sc.upsert_document
    blips = {"remaining": 1}

    async def _flaky_upsert(payload):
        if (
            payload.get("collection") == JOBS_COLLECTION
            and (payload.get("data") or {}).get("status") == "pending"
            and blips["remaining"]
        ):
            blips["remaining"] -= 1
            raise RuntimeError("storage blip")
        return await real_upsert(payload)

    monkeypatch.setattr(sc, "upsert_document", _flaky_upsert)

    assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_transient"}  # never raises
    assert blips["remaining"] == 0  # the first reset write did raise
    doc = await _job_doc(tenant_id, doc_id)
    assert doc["data"]["status"] == "pending"
    assert doc["data"]["attempts"] == 1


async def test_pending_reset_does_not_overwrite_concurrent_done(client, monkeypatch):
    """Synthesis fails, but a CONCURRENT run finished the SAME window while
    this run was in flight (flipping the doc to "done"): the finally
    pending-reset must detect the terminal status and back off — writing
    "pending" over it would re-open a consumed window (#667)."""
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))
    completed_at = datetime.now(UTC).isoformat()

    async def _concurrent_done_then_boom(prompt, config, events):
        # Simulate the concurrent processor committing between this run's
        # "processing" write and its finally reset, then fail this run.
        await _overwrite_job_data(
            tenant_id,
            doc_id,
            status="done",
            attempts=7,
            memories_written=3,
            completed_at=completed_at,
        )
        raise RuntimeError("llm down")

    monkeypatch.setattr(interview_service, "_interview_chunk", _concurrent_done_then_boom)
    assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_transient"}  # never raises
    after = (await _job_doc(tenant_id, doc_id))["data"]
    assert after["status"] == "done"  # the reset backed off
    assert after["attempts"] == 7  # the winner's count survives untouched
    assert after["completed_at"] == completed_at


async def test_pending_reset_guard_uses_the_merge_base_fetch(client, monkeypatch):
    """Round 6 (#667): the finally pending-reset no longer does a separate
    pre-check fetch — _set_state(skip_if_terminal=True) backs off from a
    terminal status using the SAME fresh snapshot it merges onto. A
    concurrent "done" landing exactly at that fetch (i.e. AFTER any
    would-be pre-check already ran) is respected, and exactly ONE job
    fetch happens after the synthesis failure — pinning the collapsed
    single fetch-write gap (a reintroduced pre-check would make it two,
    re-opening the gap between check and merge base)."""
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))
    completed_at = datetime.now(UTC).isoformat()

    failed = {"synthesis": False}

    async def _boom_chunk(prompt, config, events):
        failed["synthesis"] = True
        raise RuntimeError("llm down")

    monkeypatch.setattr(interview_service, "_interview_chunk", _boom_chunk)

    sc = interview_service.get_storage_client()
    real_get = sc.get_document
    real_upsert = sc.upsert_document
    post_failure_fetches = {"count": 0}

    async def _inject_done_get(tenant, collection, did, **kw):
        if failed["synthesis"] and collection == JOBS_COLLECTION and did == doc_id:
            post_failure_fetches["count"] += 1
            if post_failure_fetches["count"] == 1:
                # The concurrent winner commits "done" right at the reset's
                # merge-base fetch — after any would-be pre-check.
                doc = await real_get(tenant, collection, did, **kw)
                await real_upsert(
                    {
                        "tenant_id": tenant,
                        "collection": JOBS_COLLECTION,
                        "doc_id": did,
                        "data": {
                            **doc["data"],
                            "status": "done",
                            "attempts": 7,
                            "memories_written": 3,
                            "completed_at": completed_at,
                        },
                    }
                )
        return await real_get(tenant, collection, did, **kw)

    monkeypatch.setattr(sc, "get_document", _inject_done_get)
    assert await process_interview_job(tenant_id, doc_id) == {"status": "failed_transient"}  # never raises
    monkeypatch.setattr(sc, "get_document", real_get)

    after = (await _job_doc(tenant_id, doc_id))["data"]
    assert after["status"] == "done"  # the guard skipped the pending write
    assert after["attempts"] == 7
    assert after["completed_at"] == completed_at
    # Exactly one post-failure fetch: the reset went straight to
    # _set_state's fresh merge-base snapshot — no separate pre-check.
    assert post_failure_fetches["count"] == 1


async def test_direct_processor_skips_processing_unless_stale_and_allowed(client, canned_llm):
    """A "processing" doc is owned by the run that wrote it: a direct
    process_interview_job call skips it, flag or not, while it is FRESH;
    only allow_stale_processing=True on a STALE doc (the sweep's reclaim
    path) reprocesses it (#667)."""
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))
    fresh_started = datetime.now(UTC).isoformat()
    await _overwrite_job_data(
        tenant_id, doc_id, status="processing", attempts=1, processing_started_at=fresh_started
    )

    # Fresh + no flag: owned by the fire-and-forget task → skip.
    assert await process_interview_job(tenant_id, doc_id) is None
    # Fresh + flag: the flag alone can't reclaim a live run → still skip.
    assert await process_interview_job(tenant_id, doc_id, allow_stale_processing=True) is None
    after = (await _job_doc(tenant_id, doc_id))["data"]
    assert after["status"] == "processing"
    assert after["processing_started_at"] == fresh_started
    assert len(await _interviewer_memories(client, tenant_id, agent_id, headers)) == 0

    # Stale + flag (the sweep's shape): reclaimed and completed.
    await _overwrite_job_data(
        tenant_id,
        doc_id,
        processing_started_at=(datetime.now(UTC) - timedelta(minutes=11)).isoformat(),
    )
    result = await process_interview_job(tenant_id, doc_id, allow_stale_processing=True)
    assert result is not None and result["status"] == "committed"
    assert (await _job_doc(tenant_id, doc_id))["data"]["status"] == "done"
    assert len(await _interviewer_memories(client, tenant_id, agent_id, headers)) == 3


async def test_done_job_is_a_noop_for_the_processor(client, canned_llm):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    first = await process_interview_job(tenant_id, doc_id)
    assert first is not None and first["status"] == "committed"
    assert await process_interview_job(tenant_id, doc_id) is None  # done → no-op
    assert await process_interview_job(tenant_id, f"job_missing-{uid()}") is None


# ── scheduler sweep drains pending jobs ──


async def test_schedule_sweep_processes_pending_jobs(client, canned_llm, async_submit, monkeypatch):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"

    # Suppress the route's immediate processing so the job stays pending
    # for the sweep — simulates the fire-and-forget task dying with the
    # process.
    async def _noop(tenant, doc):
        return None

    monkeypatch.setattr(interview_route, "process_interview_job", _noop)
    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, node_id, agent_id),
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["status"] == "accepted"
    doc_id = interview_job_doc_id(node_id, 0, 10)
    assert (await _job_doc(tenant_id, doc_id))["data"]["status"] == "pending"

    run = await client.post("/api/v1/admin/interview/schedule/run", headers=get_admin_headers())
    assert run.status_code == 200, run.text
    summary = run.json()
    assert summary["jobs_processed"] >= 1
    assert summary["jobs_done"] >= 1
    assert "jobs_retried" in summary
    assert (await _job_doc(tenant_id, doc_id))["data"]["status"] == "done"
    assert len(await _interviewer_memories(client, tenant_id, agent_id, headers)) == 3


async def test_sweep_recovers_stale_processing_but_not_fresh(client, canned_llm):
    """A job stranded in "processing" past the staleness cutoff (its task
    hard-crashed mid-run) is re-swept and completed; a fresh "processing"
    job (a live concurrent run) is left alone."""
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    now = datetime.now(UTC)

    stale_node, stale_agent = f"node-{uid()}", f"agent-{uid()}"
    stale_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, stale_node, stale_agent))
    await _overwrite_job_data(
        tenant_id,
        stale_id,
        status="processing",
        attempts=1,
        processing_started_at=(now - timedelta(minutes=11)).isoformat(),
    )

    fresh_node, fresh_agent = f"node-{uid()}", f"agent-{uid()}"
    fresh_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, fresh_node, fresh_agent))
    fresh_started = now.isoformat()
    await _overwrite_job_data(
        tenant_id, fresh_id, status="processing", attempts=1, processing_started_at=fresh_started
    )

    await process_pending_interview_jobs()

    stale_after = (await _job_doc(tenant_id, stale_id))["data"]
    assert stale_after["status"] == "done"
    assert stale_after["attempts"] == 2  # the recovery run counted
    assert len(await _interviewer_memories(client, tenant_id, stale_agent, headers)) == 3

    fresh_after = (await _job_doc(tenant_id, fresh_id))["data"]
    assert fresh_after["status"] == "processing"
    assert fresh_after["processing_started_at"] == fresh_started
    assert len(await _interviewer_memories(client, tenant_id, fresh_agent, headers)) == 0


async def test_sweep_bounded_concurrency_completes_all_jobs(client, canned_llm, monkeypatch):
    """Fix 2 (#667 round 6): a sweep over MORE jobs than the fan-out bound
    (7 pending jobs across 2 tenants > INTERVIEW_SWEEP_CONCURRENCY=5)
    completes every job with the same summary semantics as the old
    sequential drain, while never exceeding the bound in flight."""
    tenant_a, headers_a = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_a, headers_a)
    tenant_b, headers_b = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_b, headers_b)

    seeded: list[tuple[str, str]] = []
    for tenant_id, count in ((tenant_a, 4), (tenant_b, 3)):
        for _ in range(count):
            node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
            doc_id = await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))
            seeded.append((tenant_id, doc_id))
    assert len(seeded) == 7 > interview_service.INTERVIEW_SWEEP_CONCURRENCY

    real_process = interview_service.process_interview_job
    gauge = {"now": 0, "max": 0}

    async def _tracking_process(tenant_id, doc_id, **kw):
        gauge["now"] += 1
        gauge["max"] = max(gauge["max"], gauge["now"])
        try:
            # Hold every task in flight long enough for the whole gathered
            # batch to start — an unbounded gather would peak at 7 here.
            await asyncio.sleep(0.01)
            return await real_process(tenant_id, doc_id, **kw)
        finally:
            gauge["now"] -= 1

    monkeypatch.setattr(interview_service, "process_interview_job", _tracking_process)
    summary = await process_pending_interview_jobs()

    # >= not ==: the sweep is global, so leftover jobs from other tests'
    # tenants may ride along (same as the other sweep tests here).
    assert summary["jobs_processed"] >= 7
    assert summary["jobs_done"] >= 7
    assert gauge["max"] <= interview_service.INTERVIEW_SWEEP_CONCURRENCY
    for tenant_id, doc_id in seeded:
        assert (await _job_doc(tenant_id, doc_id))["data"]["status"] == "done"


async def test_process_pending_jobs_returns_counts_summary(client, canned_llm):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    await enqueue_interview_job(**_enqueue_kwargs(tenant_id, node_id, agent_id))

    summary = await process_pending_interview_jobs()
    assert set(summary) == {
        "tenants",
        "jobs_processed",
        "jobs_done",
        "jobs_retried",
        "jobs_parked",
        "jobs_skipped",
    }
    assert summary["tenants"] >= 1
    assert summary["jobs_processed"] >= 1
    assert summary["jobs_done"] >= 1


# ── (e) escape hatch: legacy inline path behind the flag ──


async def test_flag_off_runs_legacy_inline_path(client, canned_llm, monkeypatch):
    monkeypatch.setattr(interview_route.app_settings, "interview_async_submit", False)
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"

    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, node_id, agent_id),
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "committed"
    assert body["watermark"] == 10
    assert body["memories_written"] == 3
    # Inline path never persists a job doc.
    assert await _job_doc(tenant_id, interview_job_doc_id(node_id, 0, 10)) is None


# ── unit ──


def test_job_doc_id_is_deterministic_per_window():
    a = interview_job_doc_id("node-1", 0, 10)
    b = interview_job_doc_id("node-1", 0, 10)
    c = interview_job_doc_id("node-1", 0, 11)
    assert a == b != c
    assert a.startswith("job_interview:")
