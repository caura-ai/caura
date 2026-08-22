"""Parser tests — every transcript quirk the on-disk survey found."""

from __future__ import annotations

import json

from caura_client.interviewer.parser import (
    MIN_EVENT_CHARS,
    count_lines,
    scan_events,
)

LONG = "This is a substantive conversational line that easily clears the minimum length filter."


def _user(content, session="s1", **extra):
    row = {
        "type": "user",
        "sessionId": session,
        "timestamp": "2026-07-19T10:00:00.000Z",
        "message": {"role": "user", "content": content},
    }
    row.update(extra)
    return row


def _assistant(blocks, session="s1", **extra):
    row = {
        "type": "assistant",
        "sessionId": session,
        "timestamp": "2026-07-19T10:00:01.000Z",
        "message": {"role": "assistant", "content": blocks},
    }
    row.update(extra)
    return row


def _write(tmp_path, rows, torn_tail=None):
    path = tmp_path / "session.jsonl"
    body = "".join(json.dumps(r) + "\n" for r in rows)
    if torn_tail is not None:
        body += torn_tail  # no trailing newline
    path.write_text(body, encoding="utf-8")
    return path


def test_emits_prompts_and_replies_with_line_index_seq(tmp_path):
    rows = [
        _user(LONG),
        _assistant([{"type": "text", "text": LONG}]),
    ]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=0, project="proj", max_event_chars=4000)
    assert [(e.seq, e.kind) for e in scan.events] == [(0, "prompt"), (1, "reply")]
    assert scan.events[0].session_id == "proj:s1"
    assert scan.last_complete_line == 1


def test_multi_session_file_keeps_per_line_session_ids(tmp_path):
    rows = [_user(LONG, session="s1"), _user(LONG, session="s2")]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000)
    assert [e.session_id for e in scan.events] == ["p:s1", "p:s2"]


def test_tool_result_user_lines_are_noise(tmp_path):
    rows = [
        _user([{"type": "tool_result", "tool_use_id": "t1", "content": LONG}]),
        _user(LONG, toolUseResult={"stdout": "x" * 500}),
        _user(LONG),  # the only real prompt
    ]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000)
    assert [e.seq for e in scan.events] == [2]
    # ...but the cursor still covers the noise lines.
    assert scan.last_complete_line == 2


def test_meta_and_compact_summary_lines_are_noise(tmp_path):
    rows = [
        _user(LONG, isMeta=True),
        _user(LONG, isCompactSummary=True, isVisibleInTranscriptOnly=True),
        _user(LONG),
    ]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000)
    assert [e.seq for e in scan.events] == [2]


def test_assistant_thinking_and_tool_use_blocks_dropped(tmp_path):
    rows = [
        _assistant(
            [
                {"type": "thinking", "thinking": LONG},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": LONG}},
                {"type": "text", "text": LONG},
            ]
        )
    ]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000)
    assert len(scan.events) == 1
    assert scan.events[0].content == LONG  # only the text block survived


def test_timestampless_metadata_types_are_noise(tmp_path):
    rows = [
        {"type": "custom-title", "customTitle": "x", "sessionId": "s1"},
        {"type": "mode", "mode": "plan", "sessionId": "s1"},
        {"type": "ai-title", "aiTitle": "y", "sessionId": "s1"},
        _user(LONG),
    ]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000)
    assert [e.seq for e in scan.events] == [3]
    assert scan.last_complete_line == 3


def test_torn_final_line_is_not_consumed(tmp_path):
    rows = [_user(LONG)]
    path = _write(tmp_path, rows, torn_tail='{"type":"user","sessionId":"s1"')
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000)
    assert scan.last_complete_line == 0  # cursor stops before the torn tail
    assert len(scan.events) == 1
    assert count_lines(path) == 2  # raw line count still sees the fragment


def test_short_prompts_filtered_and_content_capped(tmp_path):
    rows = [
        _user("ok"),  # < MIN_EVENT_CHARS
        _user("x" * (MIN_EVENT_CHARS + 4000)),
    ]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000)
    assert [e.seq for e in scan.events] == [1]
    assert len(scan.events[0].content) == 4000


def test_start_line_skips_consumed_range(tmp_path):
    rows = [_user(LONG), _user(LONG), _user(LONG)]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=2, project="p", max_event_chars=4000)
    assert [e.seq for e in scan.events] == [2]


def test_secrets_scrubbed_before_emit(tmp_path):
    secret = "sk-" + "a" * 40
    rows = [_user(f"deploy with key {secret} and then verify the health endpoint responds")]
    path = _write(tmp_path, rows)
    scan = scan_events(path, start_line=0, project="p", max_event_chars=4000)
    assert secret not in scan.events[0].content
    assert "[REDACTED_SECRET]" in scan.events[0].content
