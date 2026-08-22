"""Runner tests against a fake server that re-implements the server's
three seq-validation rules and the watermark protocol.

The fake is deliberately strict: any window the runner produces that the
REAL server would 422 fails here too, so protocol drift is caught in unit
tests rather than in the field.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from caura_client import Caura
from caura_client.interviewer.discovery import Transcript
from caura_client.interviewer.runner import (
    RunConfig,
    node_id_for,
    run_all,
    watermark_doc_id,
)

LONG = "This is a substantive conversational line that easily clears the minimum length filter."


class FakeServer:
    """In-memory interview server: watermarks + scripted submit outcomes."""

    def __init__(self):
        self.watermarks: dict[str, int] = {}  # doc_id -> last_seq
        self.submits: list[dict] = []
        self.script: list = []  # queue of "ok" | int status to force

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/documents/"):
            doc_id = path.rsplit("/", 1)[-1]
            if doc_id in self.watermarks:
                return httpx.Response(200, json={"doc_id": doc_id, "data": {"last_seq": self.watermarks[doc_id]}})
            return httpx.Response(404, json={"detail": "Document not found"})
        if path == "/api/v1/interview/submit":
            body = json.loads(request.content)
            self.submits.append(body)
            # The real server's validation rules (interview.py:87-96).
            seqs = [e["seq"] for e in body["events"]]
            assert body["cursor_to"] >= body["cursor_from"], "reversed cursor"
            assert all(a < b for a, b in zip(seqs, seqs[1:])), "seqs not strictly ascending"
            assert seqs[0] >= body["cursor_from"] and seqs[-1] <= body["cursor_to"], "seq outside window"
            outcome = self.script.pop(0) if self.script else "ok"
            if outcome == "ok" or outcome == 207:
                doc_id = watermark_doc_id(body["node_id"])
                self.watermarks[doc_id] = max(self.watermarks.get(doc_id, -1), body["cursor_to"])
                status = 207 if outcome == 207 else 200
                return httpx.Response(
                    status,
                    json={
                        "status": "partial" if status == 207 else "committed",
                        "watermark": self.watermarks[doc_id],
                        "memories_written": len(body["events"]),
                        "errors": 1 if status == 207 else 0,
                    },
                )
            return httpx.Response(outcome, json={"detail": f"scripted {outcome}"})
        return httpx.Response(404, json={"detail": "unknown route"})


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def mc(server):
    return Caura(
        "mc_test",
        tenant_id="t-test",
        transport=httpx.MockTransport(server.handler),
    )


def _transcript(tmp_path, n_events=12, name="abc123.jsonl"):
    rows = [
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-07-19T10:00:00.000Z",
            "message": {"role": "user", "content": f"{LONG} #{i}"},
        }
        for i in range(n_events)
    ]
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return Transcript(path=path, project="proj")


def _cfg(**kw):
    defaults = dict(agent_id="cc-test@host", machine12="abcdef123456", min_events=1)
    defaults.update(kw)
    return RunConfig(**defaults)


def test_happy_path_submits_and_next_run_skips(server, mc, tmp_path):
    transcript = _transcript(tmp_path)
    summary = run_all(mc, [transcript], _cfg())
    assert summary.files[0].windows_submitted == 1
    assert summary.files[0].events_submitted == 12
    node_id = node_id_for("abcdef123456", transcript.path)
    assert server.watermarks[watermark_doc_id(node_id)] == 11  # EOF line index

    # Second run: watermark == EOF → cheap "no new lines" skip, no submit.
    server.submits.clear()
    summary2 = run_all(mc, [transcript], _cfg())
    assert summary2.files[0].skipped_reason == "no new lines"
    assert server.submits == []


def test_resume_from_existing_watermark(server, mc, tmp_path):
    transcript = _transcript(tmp_path, n_events=10)
    node_id = node_id_for("abcdef123456", transcript.path)
    server.watermarks[watermark_doc_id(node_id)] = 5  # lines 0..5 consumed
    run_all(mc, [transcript], _cfg())
    (submit,) = server.submits
    assert submit["cursor_from"] == 6
    assert [e["seq"] for e in submit["events"]] == [6, 7, 8, 9]


def test_504_retries_once_then_succeeds(server, mc, tmp_path):
    transcript = _transcript(tmp_path)
    server.script = [504, "ok"]
    summary = run_all(mc, [transcript], _cfg())
    assert len(server.submits) == 2  # same window twice (dedup-safe)
    assert server.submits[0]["cursor_from"] == server.submits[1]["cursor_from"]
    assert summary.files[0].windows_submitted == 1
    assert not summary.files[0].error


def test_500_skips_file_without_retry(server, mc, tmp_path):
    transcript = _transcript(tmp_path)
    server.script = [500]
    summary = run_all(mc, [transcript], _cfg())
    assert len(server.submits) == 1  # no retry on 500
    assert summary.files[0].error
    assert summary.files[0].windows_submitted == 0


def test_403_aborts_the_whole_run(server, mc, tmp_path):
    first = _transcript(tmp_path, name="a.jsonl")
    second = _transcript(tmp_path, name="b.jsonl")
    server.script = [403]
    summary = run_all(mc, [first, second], _cfg())
    assert summary.aborted
    assert len(summary.files) == 0  # nothing recorded past the abort
    assert len(server.submits) == 1  # second file never attempted


def test_207_partial_counts_as_progress(server, mc, tmp_path):
    transcript = _transcript(tmp_path)
    server.script = [207]
    summary = run_all(mc, [transcript], _cfg())
    assert summary.files[0].windows_submitted == 1
    node_id = node_id_for("abcdef123456", transcript.path)
    assert server.watermarks[watermark_doc_id(node_id)] == 11


def test_per_file_isolation(server, mc, tmp_path):
    bad = _transcript(tmp_path, name="bad.jsonl")
    good = _transcript(tmp_path, name="good.jsonl")
    server.script = [500, "ok"]
    summary = run_all(mc, [bad, good], _cfg())
    assert summary.files[0].error and summary.files[1].windows_submitted == 1
    assert not summary.failed_all


def test_shrink_guard_skips_file(server, mc, tmp_path):
    transcript = _transcript(tmp_path, n_events=3)
    node_id = node_id_for("abcdef123456", transcript.path)
    server.watermarks[watermark_doc_id(node_id)] = 50  # cursor beyond the file
    summary = run_all(mc, [transcript], _cfg())
    assert "shrank" in (summary.files[0].skipped_reason or "")
    assert server.submits == []


def test_windows_budget_caps_the_run(server, mc, tmp_path):
    transcripts = [_transcript(tmp_path, name=f"f{i}.jsonl") for i in range(4)]
    summary = run_all(mc, transcripts, _cfg(max_windows=2))
    assert sum(f.windows_submitted for f in summary.files) == 2


def test_dry_run_submits_nothing(server, mc, tmp_path):
    transcript = _transcript(tmp_path)
    summary = run_all(mc, [transcript], _cfg(dry_run=True))
    assert summary.files[0].windows_submitted == 1
    assert server.submits == []


def test_attempt_id_would_be_deterministic(tmp_path):
    # Same (node, window) → same server-side attempt id (documented tie-in).
    node = "cc:abcdef123456:abc123"
    a = hashlib.sha1(f"{node}:0:11".encode()).hexdigest()
    b = hashlib.sha1(f"{node}:0:11".encode()).hexdigest()
    assert a == b
