"""Route tests for ``POST /api/v1/interview/submit`` (Interviewer Phase 1).

The LLM map call (``_interview_chunk``) is stubbed with a canned report so
assertions are deterministic; everything else — auth, settings gate, bulk
write idempotency, watermark document — runs the real in-process stack.
"""

from datetime import UTC, datetime, timedelta

import pytest

import core_api.services.interview_service as interview_service
from tests.conftest import get_test_auth, uid


# ── helpers ──


def _events(n: int = 3, start_seq: int = 0) -> list[dict]:
    base = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    return [
        {
            "seq": start_seq + i,
            "ts": (base + timedelta(minutes=i)).isoformat(),
            "session_id": "sess-1",
            "role": "assistant",
            "kind": "message",
            "content": f"Worked on step {i}: refactored the ingest pipeline module.",
        }
        for i in range(n)
    ]


def _payload(tenant_id: str, node_id: str, agent_id: str, **kw) -> dict:
    payload = {
        "tenant_id": tenant_id,
        "node_id": node_id,
        "agent_id": agent_id,
        "command_id": "cmd-1",
        "cursor_from": 0,
        "cursor_to": 10,
        "events": _events(),
    }
    payload.update(kw)
    return payload


async def _enable_interviewer(client, tenant_id: str, headers: dict) -> None:
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"interviewer": {"enabled": True}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


_CANNED_REPORT = {
    "worked_on": [
        {
            "summary": "Refactored the ingest pipeline module across three steps.",
            "ts_start": "2026-07-16T08:00:00+00:00",
            "ts_end": "2026-07-16T08:02:00+00:00",
            "session_id": "sess-1",
        }
    ],
    "decisions": [
        {
            "summary": "Adopted append-only buffer for the ingest path.",
            "rationale": "In-memory buffer loses the window on crash.",
            "ts": "2026-07-16T08:01:00+00:00",
        }
    ],
    "outcomes": [
        {"summary": "Refactor landed green.", "result": "success", "ts": "2026-07-16T08:02:00+00:00"}
    ],
    "blockers": [],
    "open_questions": [],
    "preferences_learned": [],
}


@pytest.fixture
def canned_llm(monkeypatch):
    """Stub the map-phase LLM call with a deterministic report."""
    calls: list[str] = []

    async def _fake_chunk(prompt, config, events):
        calls.append(prompt)
        return _CANNED_REPORT

    monkeypatch.setattr(interview_service, "_interview_chunk", _fake_chunk)
    return calls


# ── gate & validation ──


async def test_submit_rejected_when_interviewer_disabled(client):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, f"node-{uid()}", f"agent-{uid()}"),
        headers=headers,
    )
    assert resp.status_code == 403
    assert "not enabled" in resp.json()["detail"]


async def test_submit_rejects_reversed_cursor(client):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, f"node-{uid()}", f"agent-{uid()}", cursor_from=10, cursor_to=5),
        headers=headers,
    )
    assert resp.status_code == 422


async def test_submit_rejects_events_outside_window(client):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(
            tenant_id, f"node-{uid()}", f"agent-{uid()}", cursor_from=0, cursor_to=2, events=_events(5)
        ),
        headers=headers,
    )
    assert resp.status_code == 422
    assert "within" in resp.json()["detail"]


async def test_submit_rejects_unsorted_events(client):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    events = _events(3)
    events[0], events[2] = events[2], events[0]
    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, f"node-{uid()}", f"agent-{uid()}", events=events),
        headers=headers,
    )
    assert resp.status_code == 422
    assert "ascending" in resp.json()["detail"]


# ── happy path & idempotency ──


async def test_submit_happy_path_writes_typed_memories_and_watermark(client, canned_llm):
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
    assert body["memories_written"] == 3  # episode + decision + outcome
    assert body["errors"] == 0
    assert len(canned_llm) == 1  # single chunk → single map call

    # The memories are attributed to the WORKER agent, typed per section,
    # and stamped with the interviewer provenance + real event time.
    listing = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&agent_id={agent_id}", headers=headers
    )
    assert listing.status_code == 200
    rows = listing.json()["items"] if isinstance(listing.json(), dict) else listing.json()
    ours = [r for r in rows if (r.get("metadata") or {}).get("source") == "interviewer"]
    assert len(ours) == 3
    types = sorted(r["memory_type"] for r in ours)
    assert types == ["decision", "episode", "outcome"]
    for row in ours:
        assert (row.get("metadata") or {}).get("node_id") == node_id
        assert (row.get("metadata") or {}).get("written_by") == "interviewer"


async def test_submit_retry_is_idempotent(client, canned_llm):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    payload = _payload(tenant_id, node_id, agent_id)

    first = await client.post("/api/v1/interview/submit", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["memories_written"] == 3

    # Same (node, window) → same server-derived attempt id → every row
    # resolves duplicate_attempt; watermark stays; no new memories.
    second = await client.post("/api/v1/interview/submit", json=payload, headers=headers)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "committed"
    assert body["memories_written"] == 0
    assert body["errors"] == 0
    assert body["watermark"] == 10


async def test_watermark_is_forward_only(client, canned_llm):
    tenant_id, headers = get_test_auth(f"t-{uid()}")
    await _enable_interviewer(client, tenant_id, headers)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"

    first = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, node_id, agent_id, cursor_from=0, cursor_to=10),
        headers=headers,
    )
    assert first.json()["watermark"] == 10

    # A stale window (older cursor range) must not regress the cursor.
    stale = await client.post(
        "/api/v1/interview/submit",
        json=_payload(
            tenant_id, node_id, agent_id, cursor_from=0, cursor_to=5, events=_events(3)
        ),
        headers=headers,
    )
    assert stale.status_code == 200
    assert stale.json()["watermark"] == 10


# ── service-layer unit tests (no DB) ──


def test_attempt_id_is_deterministic_and_valid():
    a = interview_service.interview_attempt_id("node-1", 0, 10)
    b = interview_service.interview_attempt_id("node-1", 0, 10)
    c = interview_service.interview_attempt_id("node-1", 0, 11)
    assert a == b != c
    import re

    assert re.match(r"^[A-Za-z0-9._:\-]{1,128}$", a)


def test_mask_events_masks_pii_before_llm():
    events = [{"seq": 0, "content": "email me at ran@caura.ai, card 4111 1111 1111 1111"}]
    masked, findings = interview_service.mask_events(events)
    assert findings >= 2
    assert "ran@caura.ai" not in masked[0]["content"]
    assert "4111 1111 1111 1111" not in masked[0]["content"]


def test_chunk_events_respects_char_budget(monkeypatch):
    monkeypatch.setattr(interview_service, "INTERVIEW_CHUNK_MAX_CHARS", 500)
    events = [{"seq": i, "content": "x" * 200} for i in range(10)]
    chunks = interview_service.chunk_events(events)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 10  # nothing dropped


def test_merge_reports_dedups_and_caps():
    r1 = {"decisions": [{"summary": "Use Postgres."}, {"summary": "use postgres."}]}
    r2 = {"decisions": [{"summary": "Use Postgres."}, {"summary": "Ship Friday."}]}
    merged = interview_service.merge_reports([r1, r2])
    summaries = [d["summary"] for d in merged["decisions"]]
    assert summaries == ["Use Postgres.", "Ship Friday."]


def test_report_to_items_maps_types_and_event_time():
    items = interview_service.report_to_items(_CANNED_REPORT, node_id="n1", command_id="c1")
    by_type = {i.memory_type: i for i in items}
    assert set(by_type) == {"episode", "decision", "outcome"}
    assert by_type["decision"].content.endswith("In-memory buffer loses the window on crash.")
    assert by_type["outcome"].content.startswith("[success]")
    # Source event time, not report time.
    assert by_type["episode"].ts_valid_start is not None
    assert by_type["episode"].ts_valid_start.year == 2026
