"""Claude Code transcript parser → C2 interview events.

Format facts this parser is built on (verified against real transcripts,
2026-07; see the Phase-2 plan):

- A session ``.jsonl`` is append-only but may contain MULTIPLE
  ``sessionId`` values (resuming appends to the original file) and eight
  non-conversation line types interleaved (some without timestamps).
- Tool RESULTS are ``type:"user"`` lines whose ``message.content`` starts
  with a ``tool_result`` block (and carry a top-level ``toolUseResult``).
- Meta/injected lines are flagged ``isMeta`` / ``isCompactSummary`` /
  ``isVisibleInTranscriptOnly``.
- Assistant ``message.content`` is a list of ``thinking|text|tool_use``
  blocks (or occasionally a plain string).

v1 emit policy (pilot-proven quality): REAL user prompts + assistant TEXT
only. Everything else — thinking, tool_use, tool_result, meta, compaction
summaries — is consumed as noise (it still advances the cursor).

``seq`` is the RAW LINE INDEX in the file: monotonic under append-only
writes regardless of session interleave, which is exactly the server's C2
seq contract (sparse seqs are legal; ``cursor_to`` may point past a
filtered tail).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .scrub import scrub

MIN_EVENT_CHARS = 60


@dataclass
class ParsedEvent:
    seq: int
    ts: str
    session_id: str
    role: str
    kind: str
    content: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "session_id": self.session_id,
            "role": self.role,
            "kind": self.kind,
            "content": self.content,
        }


@dataclass
class ScanResult:
    """Events plus the index of the last COMPLETE line scanned."""

    events: list[ParsedEvent]
    last_complete_line: int  # -1 when nothing scannable past start_line
    total_lines: int


def _text_of(content: Any) -> str:
    """Join text blocks; plain strings pass through; everything else is ''."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
        return "\n".join(parts)
    return ""


def _is_tool_result_user(row: dict[str, Any], message: dict[str, Any]) -> bool:
    if row.get("toolUseResult") is not None:
        return True
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
    return False


def _event_from_line(
    row: dict[str, Any],
    seq: int,
    project: str,
    max_event_chars: int,
) -> Optional[ParsedEvent]:
    line_type = row.get("type")
    if line_type not in ("user", "assistant"):
        return None
    message = row.get("message")
    session_id = row.get("sessionId")
    ts = row.get("timestamp")
    if not isinstance(message, dict) or not session_id or not ts:
        return None
    # Meta / injected / compaction lines are noise, not conversation.
    if row.get("isMeta") or row.get("isCompactSummary") or row.get("isVisibleInTranscriptOnly"):
        return None
    if line_type == "user":
        if _is_tool_result_user(row, message):
            return None
        kind = "prompt"
    else:
        kind = "reply"
    text = scrub(_text_of(message.get("content")).strip())
    if len(text) < MIN_EVENT_CHARS:
        return None
    return ParsedEvent(
        seq=seq,
        ts=str(ts),
        session_id=f"{project}:{session_id}",
        role=str(message.get("role") or line_type),
        kind=kind,
        content=text[:max_event_chars],
    )


def count_lines(path: Path) -> int:
    """Cheap line count for the skip-unchanged check (no JSON parsing)."""
    count = 0
    with open(path, "rb") as f:
        for _ in f:
            count += 1
    return count


def scan_events(
    path: Path,
    *,
    start_line: int,
    project: str,
    max_event_chars: int,
) -> ScanResult:
    """Scan complete lines from ``start_line`` (0-based) to EOF.

    A final line without a trailing newline may be mid-append (Claude Code
    is live) — it is NOT counted as complete and will be picked up next
    run. Unparseable COMPLETE lines are consumed as noise (cursor still
    advances past them).
    """
    events: list[ParsedEvent] = []
    last_complete = -1
    total = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for index, raw in enumerate(f):
            total = index + 1
            if not raw.endswith("\n"):
                # Torn/mid-append tail: stop before it; complete next run.
                total = index
                break
            if index < start_line:
                continue
            last_complete = index
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # corrupt line: noise, cursor advances past it
            if not isinstance(row, dict):
                continue
            event = _event_from_line(row, index, project, max_event_chars)
            if event is not None:
                events.append(event)
    return ScanResult(events=events, last_complete_line=last_complete, total_lines=total)
