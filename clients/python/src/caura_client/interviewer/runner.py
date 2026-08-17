"""The per-file interview loop: watermark → scan → windows → submit.

Protocol (inherited from Phase 1, crash-safe by construction):
- the cursor is the SERVER's forward-only watermark doc — no local state;
- the watermark advances only after the server commits, and the attempt id
  is deterministic per (node, window), so any retry dedups;
- this adapter never modifies the transcript (Claude Code owns the trail).

Failure matrix (per the approved plan):
  403  → abort the whole run (tenant off / bad key)         exit 2
  422  → our windowing bug: log + skip file, no retry
  504  → retry the SAME window once (dedup-safe), then skip file
  500  → window not consumed: skip file, next run resumes
  207  → partial success: watermark advanced, keep draining
  transport error → one retry, then skip file
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from ..client import Caura
from ..exceptions import AuthError, CauraAPIError, NotFoundError
from .discovery import HARNESS_CLAUDE_CODE, HARNESS_CURSOR, Transcript
from .parser import count_lines, scan_events
from .windows import Window, build_windows, window_is_worth_interviewing

WATERMARK_COLLECTION = "interview_watermarks"


# Harness → node-id namespace. Distinct prefixes keep a Claude Code and a
# Cursor session with a colliding stem (both UUID4 — effectively never)
# on separate watermark streams, and make node provenance greppable
# server-side.
_NODE_PREFIX = {HARNESS_CLAUDE_CODE: "cc", HARNESS_CURSOR: "cursor"}


def node_id_for(machine12: str, path: Path, dialect: str = HARNESS_CLAUDE_CODE) -> str:
    # The stem is the session UUID — globally unique, so the project dir
    # is DELIBERATELY excluded: (a) renaming/moving a project dir (the
    # slug is the cwd path) must not orphan every watermark under it,
    # and (b) long path slugs could overflow the server's 200-char
    # node_id cap. A moved transcript keeps its cursor, by design.
    return f"{_NODE_PREFIX.get(dialect, 'cc')}:{machine12}:{path.stem}"


def watermark_doc_id(node_id: str) -> str:
    # Mirrors core_api.services.interview_service.watermark_doc_id.
    # usedforsecurity=False: this is a stable identifier, not crypto —
    # without it FIPS-140 Linux systems raise ValueError on sha1.
    return f"wm_{hashlib.sha1(node_id.encode(), usedforsecurity=False).hexdigest()[:40]}"


@dataclass
class RunConfig:
    agent_id: str
    machine12: str
    fleet_id: Optional[str] = None
    max_event_chars: int = 4_000
    max_windows: int = 8
    min_events: int = 10
    flush: bool = False
    dry_run: bool = False
    verbose: bool = False


@dataclass
class FileResult:
    path: Path
    windows_submitted: int = 0
    events_submitted: int = 0
    memories_written: int = 0
    skipped_reason: Optional[str] = None
    error: Optional[str] = None


@dataclass
class RunSummary:
    files: list[FileResult] = field(default_factory=list)
    windows_budget_left: int = 0
    aborted: bool = False

    @property
    def failed_all(self) -> bool:
        attempted = [f for f in self.files if f.skipped_reason is None]
        return bool(attempted) and all(f.error for f in attempted)


def _log(cfg: RunConfig, msg: str) -> None:
    if cfg.verbose:
        print(f"[interviewer] {msg}", file=sys.stderr)


def read_watermark(mc: Caura, node_id: str) -> int:
    """Server-side cursor for this file; -1 when never interviewed."""
    try:
        doc = mc.get_document(watermark_doc_id(node_id), collection=WATERMARK_COLLECTION)
    except NotFoundError:
        return -1
    data = doc.get("data") if isinstance(doc, dict) else None
    try:
        return int(data.get("last_seq", -1)) if isinstance(data, dict) else -1
    except (TypeError, ValueError):
        return -1


def _submit_window(mc: Caura, cfg: RunConfig, node_id: str, window: Window) -> dict:
    return mc.submit_interview(
        node_id=node_id,
        agent_id=cfg.agent_id,
        fleet_id=cfg.fleet_id,
        cursor_from=window.cursor_from,
        cursor_to=window.cursor_to,
        events=[e.to_payload() for e in window.events],
    )


def run_file(mc: Caura, transcript: Transcript, cfg: RunConfig, windows_budget: int) -> FileResult:
    """Drain one transcript up to the shared windows budget."""
    result = FileResult(path=transcript.path)
    node_id = node_id_for(cfg.machine12, transcript.path, transcript.dialect)
    # Dry-run is fully offline: no watermark read, no submit — parse and
    # window from the start of the file as if never interviewed.
    last_seq = -1 if cfg.dry_run else read_watermark(mc, node_id)
    cursor_from = last_seq + 1

    # Cheap skip: nothing appended since the last committed window.
    total_lines = count_lines(transcript.path)
    if total_lines < cursor_from:
        # Append-only violated (rotation/rewrite). Never guess a cursor.
        result.skipped_reason = (
            f"file shrank below watermark ({total_lines} lines < cursor {cursor_from}); "
            "append-only assumption violated - skipping"
        )
        print(f"[interviewer] WARNING {transcript.path.name}: {result.skipped_reason}", file=sys.stderr)
        return result
    if total_lines == cursor_from:
        result.skipped_reason = "no new lines"
        return result
    # NOTE: count_lines counts a torn (newline-less) final fragment, so a
    # file whose only "new" line is mid-append reaches scan_events, which
    # then correctly reports "no complete new lines". That wasted scan is
    # accepted: a REAL single new complete line yields the same
    # total_lines == cursor_from + 1, and only the scan can tell them
    # apart — short-circuiting here would skip real data.

    scan = scan_events(
        transcript.path,
        start_line=cursor_from,
        project=transcript.project,
        max_event_chars=cfg.max_event_chars,
        dialect=transcript.dialect,
    )
    if scan.last_complete_line < cursor_from:
        result.skipped_reason = "no complete new lines (mid-append tail)"
        return result
    if not scan.events:
        # Pure-noise tail: nothing to interview, nothing to submit (server
        # requires >= 1 event). Local-only rescan next run; bounded.
        result.skipped_reason = f"no interview-worthy events in {scan.last_complete_line - cursor_from + 1} new lines"
        return result

    for window in build_windows(
        scan.events, cursor_from=cursor_from, eof_line_index=scan.last_complete_line
    ):
        if windows_budget - result.windows_submitted <= 0:
            _log(cfg, f"{transcript.path.name}: windows budget exhausted, resuming next run")
            break
        if not window_is_worth_interviewing(window, min_events=cfg.min_events, flush=cfg.flush):
            _log(cfg, f"{transcript.path.name}: final window below dribble gate ({len(window.events)} events), deferred")
            break
        if cfg.dry_run:
            print(
                f"[dry-run] {transcript.path.name}: window [{window.cursor_from}..{window.cursor_to}] "
                f"{len(window.events)} events, {window.chars} chars"
            )
            result.windows_submitted += 1
            result.events_submitted += len(window.events)
            continue
        try:
            response = _try_submit(mc, cfg, node_id, window)
        except AuthError:
            raise  # abort the whole run (403: tenant off / bad key)
        except CauraAPIError as exc:
            result.error = f"window [{window.cursor_from}..{window.cursor_to}]: {exc}"
            _log(cfg, f"{transcript.path.name}: {result.error} - skipping file")
            break
        except httpx.TransportError as exc:
            result.error = f"transport: {exc}"
            _log(cfg, f"{transcript.path.name}: {result.error} - skipping file")
            break
        result.windows_submitted += 1
        result.events_submitted += len(window.events)
        result.memories_written += int(response.get("memories_written") or 0)
        _log(
            cfg,
            f"{transcript.path.name}: [{window.cursor_from}..{window.cursor_to}] "
            f"{response.get('status')} watermark={response.get('watermark')} "
            f"memories={response.get('memories_written')}",
        )
    return result


def _try_submit(mc: Caura, cfg: RunConfig, node_id: str, window: Window) -> dict:
    """One submit with a single retry on 504/transport (dedup-safe)."""
    try:
        return _submit_window(mc, cfg, node_id, window)
    except CauraAPIError as exc:
        if exc.status_code == 504:
            _log(cfg, f"504 on [{window.cursor_from}..{window.cursor_to}], one dedup-safe retry")
            return _submit_window(mc, cfg, node_id, window)
        raise
    except httpx.TransportError:
        _log(cfg, f"transport error on [{window.cursor_from}..{window.cursor_to}], one dedup-safe retry")
        return _submit_window(mc, cfg, node_id, window)


def run_all(mc: Caura, transcripts: list[Transcript], cfg: RunConfig) -> RunSummary:
    summary = RunSummary(windows_budget_left=cfg.max_windows)
    for transcript in transcripts:
        if summary.windows_budget_left <= 0:
            break
        try:
            result = run_file(mc, transcript, cfg, summary.windows_budget_left)
        except AuthError as exc:
            print(f"[interviewer] ABORT: {exc} (tenant not enabled, or bad credentials)", file=sys.stderr)
            summary.aborted = True
            break
        except Exception as exc:  # per-file isolation: one bad file never stops the run
            result = FileResult(path=transcript.path, error=f"unexpected: {exc}")
            print(f"[interviewer] ERROR {transcript.path.name}: {exc}", file=sys.stderr)
        summary.windows_budget_left -= result.windows_submitted
        summary.files.append(result)
    return summary
