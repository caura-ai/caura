"""Transcript parsers (Claude Code + Cursor dialects) → C2 interview events.

Claude Code format facts (verified against real transcripts, 2026-07; see
the Phase-2 plan):

- A session ``.jsonl`` is append-only but may contain MULTIPLE
  ``sessionId`` values (resuming appends to the original file) and eight
  non-conversation line types interleaved (some without timestamps).
- Tool RESULTS are ``type:"user"`` lines whose ``message.content`` starts
  with a ``tool_result`` block (and carry a top-level ``toolUseResult``).
- Meta/injected lines are flagged ``isMeta`` / ``isCompactSummary`` /
  ``isVisibleInTranscriptOnly``.
- Assistant ``message.content`` is a list of ``thinking|text|tool_use``
  blocks (or occasionally a plain string).

Cursor agent-transcript facts (verified against real
``~/.cursor/projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl`` files,
2026-07; Cursor calls the format "Claude Code-compatible JSONL"):

- Lines are ``{"role": "user"|"assistant", "message": {"content": [...]}}``
  with no meta/sidechain envelope, plus roleless status lines such as
  ``{"type": "turn_ended", "status": "success"}``.
- There are NO tool_result lines (tool outputs are excluded upstream) and
  no per-line sessionId/timestamp fields.
- USER text arrives wrapped in ``<timestamp>...</timestamp>`` and
  ``<user_query>...</user_query>`` tags; the timestamp is human-formatted
  ("Tuesday, Jul 21, 2026, 1:11 PM (UTC+3)").
- Every assistant text block ends with a trailing ``[REDACTED]`` marker
  (redacted thinking); thinking-only turns are ONLY that marker.

v1 emit policy (pilot-proven quality): REAL user prompts + assistant TEXT
only. Everything else — thinking, tool_use, tool_result, meta, compaction
summaries, status lines — is consumed as noise (it still advances the
cursor).

``seq`` is the RAW LINE INDEX in the file: monotonic under append-only
writes regardless of session interleave, which is exactly the server's C2
seq contract (sparse seqs are legal; ``cursor_to`` may point past a
filtered tail).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .discovery import HARNESS_CLAUDE_CODE, HARNESS_CURSOR
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


# --------------------------------------------------------------- cursor
_REDACTED_MARKER = "[REDACTED]"
# [\s\S] instead of DOTALL: same convention as scrub.py, and these tags
# appear at most once per message so a single non-greedy pass is cheap.
_CURSOR_TS_TAG = re.compile(r"<timestamp>([^<]{0,80})</timestamp>")
_CURSOR_QUERY_TAG = re.compile(r"<user_query>\s*([\s\S]*?)\s*</user_query>")
# "Tuesday, Jul 21, 2026, 1:11 PM (UTC+3)" — day name skipped (redundant),
# offset may be +H / -H / +H:MM. Sub-fields captured directly:
# 1=month 2=day 3=year 4=hour12 5=minute 6=AM/PM 7=sign 8=tz-hour 9=tz-min.
_CURSOR_TS_FMT = re.compile(
    r"^[A-Za-z]+, ([A-Za-z]{3}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}) ([AP])M "
    r"\(UTC([+-])(\d{1,2})(?::(\d{2}))?\)$"
)
# Hard-coded, not strptime("%b"): %b is locale-sensitive, so a user with
# LANG=fr_FR / de_DE etc. would fail to parse Cursor's English month names
# and silently fall back to file mtime for every event's timestamp.
_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_cursor_timestamp(raw: str) -> Optional[str]:
    """Cursor's human-formatted tag → ISO 8601, or None if unparseable.

    Locale-independent, and defensive about the out-of-range fields the
    permissive ``\\d{1,2}`` groups admit (day/hour/tz-offset up to 99):
    ``datetime()`` rejects an impossible day and ``timezone()`` rejects an
    offset >= 24 h, and either ValueError is swallowed here — otherwise it
    would propagate through event_from_line and crash the whole scan.
    """
    match = _CURSOR_TS_FMT.match(raw.strip())
    if not match:
        return None
    month = _MONTH_ABBR.get(match.group(1))
    if month is None:
        return None
    hour12 = int(match.group(4))
    if not 1 <= hour12 <= 12:
        return None
    hour = hour12 % 12 + (12 if match.group(6) == "P" else 0)
    sign = -1 if match.group(7) == "-" else 1
    try:
        offset = timezone(sign * timedelta(hours=int(match.group(8)), minutes=int(match.group(9) or 0)))
        return datetime(
            int(match.group(3)), month, int(match.group(2)),
            hour, int(match.group(5)), tzinfo=offset,
        ).isoformat()
    except ValueError:
        return None


def _strip_redacted(text: str) -> str:
    """Drop the trailing ``[REDACTED]`` thinking marker(s) Cursor appends
    to every assistant text block. Trailing-only by design: a literal
    ``[REDACTED]`` in the MIDDLE of prose is content, not a marker."""
    text = text.strip()
    while text.endswith(_REDACTED_MARKER):
        text = text[: -len(_REDACTED_MARKER)].rstrip()
    return text


class _CursorDialect:
    """Per-file line decoder for Cursor agent-transcripts.

    Stateful on purpose: assistant lines carry no timestamp, so each reply
    inherits the most recent user ``<timestamp>`` tag; before any tag is
    seen (or if parsing fails) events fall back to the file's mtime.
    """

    def __init__(self, path: Path, project: str) -> None:
        self.session_id = f"{project}:{path.stem}"
        try:
            mtime = path.stat().st_mtime
            self.last_ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except OSError:
            self.last_ts = ""
        # Roleless status lines ({"type": "turn_ended", ...}) fall out via
        # the role check below — no explicit type list to chase.

    def update_ts(self, row: dict[str, Any]) -> None:
        """Advance ``last_ts`` from a user turn's ``<timestamp>`` tag.

        Called for EVERY user line, including those BEFORE ``start_line``
        on an incremental run: assistant lines carry no timestamp and
        inherit the last user tag, so a window that opens on replies must
        still have seen the earlier tag or every event in it falls back to
        the file's mtime. Idempotent and side-effect-free beyond
        ``last_ts``.
        """
        if row.get("role") != "user":
            return
        message = row.get("message")
        if not isinstance(message, dict):
            return
        ts_match = _CURSOR_TS_TAG.search(_text_of(message.get("content")))
        if ts_match:
            parsed = _parse_cursor_timestamp(ts_match.group(1))
            if parsed:
                self.last_ts = parsed

    def event_from_line(
        self, row: dict[str, Any], seq: int, max_event_chars: int
    ) -> Optional[ParsedEvent]:
        role = row.get("role")
        if role not in ("user", "assistant"):
            return None
        message = row.get("message")
        if not isinstance(message, dict):
            return None
        text = _text_of(message.get("content"))
        if role == "user":
            kind = "prompt"
            self.update_ts(row)
            query_match = _CURSOR_QUERY_TAG.search(text)
            if query_match:
                text = query_match.group(1)
            elif _CURSOR_TS_TAG.search(text):
                # Tagged timestamp but no query wrapper: drop the tag, keep
                # the rest (unknown wrapper variants stay ingestable).
                text = _CURSOR_TS_TAG.sub("", text)
        else:
            kind = "reply"
            text = _strip_redacted(text)
        text = scrub(text.strip())
        if len(text) < MIN_EVENT_CHARS:
            return None
        return ParsedEvent(
            seq=seq,
            ts=self.last_ts,
            session_id=self.session_id,
            role=role,
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
    dialect: str = HARNESS_CLAUDE_CODE,
) -> ScanResult:
    """Scan complete lines from ``start_line`` (0-based) to EOF.

    A final line without a trailing newline may be mid-append (the agent
    is live) — it is NOT counted as complete and will be picked up next
    run. Unparseable COMPLETE lines are consumed as noise (cursor still
    advances past them). ``dialect`` selects the per-line decoder
    ("claude-code" or "cursor"); the scan mechanics are shared.
    """
    cursor_dialect = _CursorDialect(path, project) if dialect == HARNESS_CURSOR else None
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
                # Cursor timestamps live only on user turns and are
                # inherited forward, so rebuild that state from lines
                # before the cursor. The "<timestamp>" substring
                # pre-filter keeps this proportional to user turns, not
                # every skipped line (assistant/status lines never match).
                if cursor_dialect is not None and "<timestamp>" in raw:
                    try:
                        skipped = json.loads(raw.strip())
                    except ValueError:
                        skipped = None
                    if isinstance(skipped, dict):
                        cursor_dialect.update_ts(skipped)
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
            if cursor_dialect is not None:
                event = cursor_dialect.event_from_line(row, index, max_event_chars)
            else:
                event = _event_from_line(row, index, project, max_event_chars)
            if event is not None:
                events.append(event)
    return ScanResult(events=events, last_complete_line=last_complete, total_lines=total)
