"""Cursor-dialect tests: agent-transcript parsing, discovery layout,
node-id namespacing, hook auto-detection.

Fixture lines mirror a REAL ~/.cursor/projects/<slug>/agent-transcripts
file captured 2026-07-21 (content anonymized, structure verbatim).
"""

from __future__ import annotations

import json

from memclaw_client.interviewer.discovery import (
    HARNESS_CURSOR,
    find_transcripts,
    transcript_from_path,
)
from memclaw_client.interviewer.parser import (
    _parse_cursor_timestamp,
    _strip_redacted,
    scan_events,
)
from memclaw_client.interviewer.runner import node_id_for

LONG = "x" * 80  # comfortably past MIN_EVENT_CHARS

USER_LINE = {
    "role": "user",
    "message": {
        "content": [
            {
                "type": "text",
                "text": (
                    "<timestamp>Tuesday, Jul 21, 2026, 1:11 PM (UTC+3)</timestamp>\n"
                    f"<user_query>\nPlease review the widget module. {LONG}\n</user_query>"
                ),
            }
        ]
    },
}
# Thinking-only intermediate turn: text is ONLY the redaction marker.
THINKING_ONLY_LINE = {
    "role": "assistant",
    "message": {
        "content": [
            {"type": "text", "text": "[REDACTED]"},
            {"type": "tool_use", "name": "Read", "input": {"path": "/tmp/x"}},
        ]
    },
}
REPLY_LINE = {
    "role": "assistant",
    "message": {
        "content": [
            {"type": "text", "text": f"Here is the review you asked for. {LONG}\n\n[REDACTED]"}
        ]
    },
}
STATUS_LINE = {"type": "turn_ended", "status": "success"}


def _write_session(tmp_path, project="Users-alice-work-app", stem="fad15427-9db8-4e39"):
    session_dir = tmp_path / project / "agent-transcripts" / stem
    session_dir.mkdir(parents=True)
    path = session_dir / f"{stem}.jsonl"
    lines = [USER_LINE, THINKING_ONLY_LINE, REPLY_LINE, STATUS_LINE]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_cursor_scan_emits_prompt_and_reply_only(tmp_path):
    path = _write_session(tmp_path)
    scan = scan_events(path, start_line=0, project="Users-alice-work-app", max_event_chars=4000, dialect="cursor")
    assert [e.kind for e in scan.events] == ["prompt", "reply"]
    assert scan.last_complete_line == 3  # status line consumed as noise
    prompt, reply = scan.events
    # Wrapper tags stripped from the prompt; nothing leaks.
    assert prompt.content.startswith("Please review the widget module.")
    assert "<user_query>" not in prompt.content and "<timestamp>" not in prompt.content
    # Trailing [REDACTED] stripped from the reply.
    assert reply.content.startswith("Here is the review")
    assert not reply.content.endswith("[REDACTED]")
    # seq = raw line index (thinking-only line 1 filtered, sparse seqs legal).
    assert [e.seq for e in scan.events] == [0, 2]


def test_cursor_timestamps_parse_and_propagate(tmp_path):
    path = _write_session(tmp_path)
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000, dialect="cursor")
    prompt, reply = scan.events
    assert prompt.ts == "2026-07-21T13:11:00+03:00"
    # Assistant lines carry no timestamp: they inherit the last user tag.
    assert reply.ts == prompt.ts
    # Session id = project:file-stem (no per-line sessionId in this dialect).
    assert prompt.session_id == "p:fad15427-9db8-4e39"


def test_parse_cursor_timestamp_variants():
    assert _parse_cursor_timestamp("Tuesday, Jul 21, 2026, 1:11 PM (UTC+3)") == "2026-07-21T13:11:00+03:00"
    assert _parse_cursor_timestamp("Friday, Dec 5, 2025, 12:07 AM (UTC-5:30)") == "2025-12-05T00:07:00-05:30"
    assert _parse_cursor_timestamp("Sunday, Jan 1, 2026, 12:00 PM (UTC+0)") == "2026-01-01T12:00:00+00:00"
    assert _parse_cursor_timestamp("not a timestamp") is None
    assert _parse_cursor_timestamp("Tuesday, Foo 21, 2026, 1:11 PM (UTC+3)") is None


def test_parse_cursor_timestamp_rejects_out_of_range_without_raising():
    """The permissive \\d{1,2} groups admit values datetime()/timezone()
    reject — those ValueErrors must be swallowed, never crash the scan."""
    # tz offset >= 24 h (timezone() raises)
    assert _parse_cursor_timestamp("Monday, Jul 21, 2026, 1:11 PM (UTC+99)") is None
    # impossible day (datetime() raises)
    assert _parse_cursor_timestamp("Monday, Feb 30, 2026, 1:11 PM (UTC+3)") is None
    # 12-hour clock overflow (guarded before conversion)
    assert _parse_cursor_timestamp("Monday, Jul 21, 2026, 13:11 PM (UTC+3)") is None


def test_scan_survives_malformed_timestamp(tmp_path):
    """A bad <timestamp> must degrade to mtime fallback, not crash scan."""
    session_dir = tmp_path / "proj" / "agent-transcripts" / "abc"
    session_dir.mkdir(parents=True)
    path = session_dir / "abc.jsonl"
    bad_user = {
        "role": "user",
        "message": {"content": [{"type": "text",
            "text": f"<timestamp>Monday, Jul 21, 2026, 1:11 PM (UTC+99)</timestamp>\n<user_query>\n{LONG}\n</user_query>"}]},
    }
    path.write_text(json.dumps(bad_user) + "\n", encoding="utf-8")
    scan = scan_events(path, start_line=0, project="proj", max_event_chars=4000, dialect="cursor")
    assert len(scan.events) == 1
    assert scan.events[0].ts and "T" in scan.events[0].ts  # mtime fallback, no crash


def test_strip_redacted_trailing_only():
    assert _strip_redacted("Real answer.\n\n[REDACTED]") == "Real answer."
    assert _strip_redacted("[REDACTED]") == ""
    # A literal [REDACTED] mid-prose is content, not a marker.
    kept = "The log printed [REDACTED] where the key was masked."
    assert _strip_redacted(kept) == kept


def test_cursor_reply_before_any_user_tag_falls_back_to_mtime(tmp_path):
    session_dir = tmp_path / "proj" / "agent-transcripts" / "abc"
    session_dir.mkdir(parents=True)
    path = session_dir / "abc.jsonl"
    path.write_text(json.dumps(REPLY_LINE) + "\n", encoding="utf-8")
    scan = scan_events(path, start_line=0, project="proj", max_event_chars=4000, dialect="cursor")
    assert len(scan.events) == 1
    # ISO string derived from file mtime — parseable, non-empty.
    assert scan.events[0].ts and "T" in scan.events[0].ts


def test_cursor_discovery_layout_and_subagent_exclusion(tmp_path):
    path = _write_session(tmp_path)
    # A subagent transcript one level deeper must NOT be discovered.
    sub = path.parent / "subagents"
    sub.mkdir()
    (sub / "helper.jsonl").write_text("{}\n", encoding="utf-8")
    # A Claude Code-shaped file at project top level must not match either.
    (tmp_path / "Users-alice-work-app" / "stray.jsonl").write_text("{}\n", encoding="utf-8")
    found = find_transcripts(
        root=tmp_path, allow_globs=["Users-alice-*"], harness=HARNESS_CURSOR
    )
    assert [t.path for t in found] == [path]
    assert found[0].dialect == HARNESS_CURSOR
    assert found[0].project == "Users-alice-work-app"


def test_cursor_incremental_reply_inherits_earlier_timestamp(tmp_path):
    """The load-bearing incremental case: a window that opens AFTER the
    user turn (cursor already past it) must still stamp replies with the
    earlier <timestamp>, not fall back to file mtime."""
    session_dir = tmp_path / "proj" / "agent-transcripts" / "abcd1234-9db8-4e39"
    session_dir.mkdir(parents=True)
    path = session_dir / "abcd1234-9db8-4e39.jsonl"
    lines = [USER_LINE, REPLY_LINE, REPLY_LINE]  # user@0, replies@1,2
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    # Resume from cursor=2: L0 (the only timestamp-bearing line) is skipped
    # for emission but must still update timestamp state.
    scan = scan_events(path, start_line=2, project="proj", max_event_chars=4000, dialect="cursor")
    assert len(scan.events) == 1
    assert scan.events[0].kind == "reply"
    assert scan.events[0].ts == "2026-07-21T13:11:00+03:00"  # inherited, NOT mtime


def test_transcript_from_path_requires_uuid_session_dir(tmp_path):
    """A non-UUID dir under 'agent-transcripts' must NOT be classified as
    Cursor — the layout coincidence alone is not enough."""
    d = tmp_path / "proj" / "agent-transcripts" / "not-a-uuid"
    d.mkdir(parents=True)
    p = d / "file.jsonl"
    p.write_text("", encoding="utf-8")
    inferred = transcript_from_path(p)
    assert inferred.dialect == "claude-code"


def test_transcript_from_path_infers_harness(tmp_path):
    cursor_path = _write_session(tmp_path)
    inferred = transcript_from_path(cursor_path)
    assert inferred.dialect == HARNESS_CURSOR
    assert inferred.project == "Users-alice-work-app"

    cc_dir = tmp_path / "-Users-alice-work-app"
    cc_dir.mkdir()
    cc_path = cc_dir / "0000.jsonl"
    cc_path.write_text("", encoding="utf-8")
    inferred_cc = transcript_from_path(cc_path)
    assert inferred_cc.dialect == "claude-code"
    assert inferred_cc.project == "-Users-alice-work-app"


def test_node_id_namespaced_per_harness(tmp_path):
    path = _write_session(tmp_path)
    assert node_id_for("abc123def456", path, "cursor") == "cursor:abc123def456:fad15427-9db8-4e39"
    assert node_id_for("abc123def456", path) == "cc:abc123def456:fad15427-9db8-4e39"


def test_invalid_harness_env_fails_loudly(monkeypatch, capsys):
    """A bad MEMCLAW_INTERVIEWER_HARNESS (choices= doesn't validate env
    defaults) must exit 2 with guidance, not silently run as claude-code."""
    import pytest

    from memclaw_client.interviewer.cli import main

    monkeypatch.setenv("MEMCLAW_INTERVIEWER_HARNESS", "Cursor")  # wrong case
    with pytest.raises(SystemExit) as exc:
        main(["run", "--all-projects", "--api-key", "k", "--tenant-id", "t"])
    assert exc.value.code == 2
    assert "Invalid harness 'Cursor'" in capsys.readouterr().err


def test_invalid_harness_env_never_breaks_hook(tmp_path, monkeypatch):
    """The harness guard must NOT fire for `hook`: it exits 2, and hook has
    an ALWAYS-exit-0 contract (and ignores --harness, inferring from path)."""
    import io

    from memclaw_client.interviewer.cli import main

    path = _write_session(tmp_path)
    payload = json.dumps({"transcript_path": str(path), "hook_event_name": "sessionEnd"})
    monkeypatch.setenv("MEMCLAW_INTERVIEWER_HARNESS", "GARBAGE")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc = main(["hook", "--projects", "Users-alice-*", "--api-key", "k", "--tenant-id", "t"])
    assert rc == 0  # bad env or not, the hook never fails the session


def test_cursor_hook_autodetects_and_respects_allowlist(tmp_path, monkeypatch, capsys):
    """The hook must resolve a Cursor transcript's PROJECT (three levels
    up), not its session dir — and still gate on the allowlist."""
    import io

    from memclaw_client.interviewer.cli import main

    path = _write_session(tmp_path)
    payload = json.dumps({"transcript_path": str(path), "hook_event_name": "sessionEnd"})

    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc = main(["hook", "--projects", "other-*", "--api-key", "k", "--tenant-id", "t"])
    assert rc == 0  # hook never fails the session
    err = capsys.readouterr().err
    # Correct project name in the warning proves cursor-shape inference.
    assert "Users-alice-work-app" in err and "not in allowlist" in err
