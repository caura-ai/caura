"""memclaw-interviewer CLI — run / status / hook.

Config precedence: flags > env > defaults. Env vars:
  MEMCLAW_API_KEY (required)      MEMCLAW_TENANT_ID (required)
  MEMCLAW_BASE_URL                MEMCLAW_AGENT_ID (default user@host)
  MEMCLAW_FLEET_ID                MEMCLAW_INTERVIEWER_PROJECTS (comma-sep globs)

Exit codes: 0 success / nothing to do; 1 every attempted file failed;
2 configuration or authorization error. ``hook`` ALWAYS exits 0 — a
memory-harvest failure must never fail a Claude Code session hook.
"""

from __future__ import annotations

import argparse
import errno
import getpass
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Optional

from ..client import MemClaw
from ..exceptions import AuthError
from .discovery import (
    DEFAULT_PROJECTS_ROOT,
    Transcript,
    find_transcripts,
    list_project_dirs,
    project_allowed,
)
from .machine import machine_id_short
from .parser import count_lines
from .runner import RunConfig, node_id_for, read_watermark, run_all

DEFAULT_BASE_URL = "https://memclaw.net"


def _default_agent_id() -> str:
    return f"cc-{getpass.getuser()}@{socket.gethostname()}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memclaw-interviewer",
        description="MemClaw Interviewer adapter for Claude Code transcripts (read-only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base-url", default=os.environ.get("MEMCLAW_BASE_URL", DEFAULT_BASE_URL))
        p.add_argument("--api-key", default=os.environ.get("MEMCLAW_API_KEY", ""))
        p.add_argument("--tenant-id", default=os.environ.get("MEMCLAW_TENANT_ID", ""))
        p.add_argument("--agent-id", default=os.environ.get("MEMCLAW_AGENT_ID", "") or _default_agent_id())
        p.add_argument("--fleet-id", default=os.environ.get("MEMCLAW_FLEET_ID", "") or None)
        p.add_argument(
            "--projects",
            nargs="+",
            default=None,
            help="project-dir globs to allow (default: MEMCLAW_INTERVIEWER_PROJECTS env; DEFAULT-DENY without either)",
        )
        p.add_argument("--all-projects", action="store_true", help="explicit opt-in: harvest every project dir")
        p.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
        p.add_argument("-v", "--verbose", action="store_true")

    run_p = sub.add_parser("run", help="scan transcripts and submit due interview windows")
    common(run_p)
    run_p.add_argument("--transcript", type=Path, default=None, help="drain ONE transcript file (bypasses discovery)")
    run_p.add_argument("--dry-run", action="store_true", help="parse + window, submit nothing")
    run_p.add_argument("--max-windows", type=int, default=8)
    run_p.add_argument("--since-hours", type=float, default=168.0, help="only files modified in this window (0 = all)")
    run_p.add_argument("--min-events", type=int, default=10, help="dribble gate for the final window")
    run_p.add_argument("--max-event-chars", type=int, default=4_000)
    run_p.add_argument("--flush", action="store_true", help="submit even below the dribble gate")

    status_p = sub.add_parser("status", help="show per-file cursor vs local line counts")
    common(status_p)
    status_p.add_argument("--since-hours", type=float, default=168.0)

    hook_p = sub.add_parser("hook", help="Claude Code SessionEnd hook: drain the session transcript (stdin JSON)")
    common(hook_p)
    hook_p.add_argument("--max-windows", type=int, default=2)
    hook_p.add_argument("--max-event-chars", type=int, default=4_000)
    return parser


def _resolve_allowlist(args: argparse.Namespace) -> list[str]:
    if args.projects is not None:
        return list(args.projects)
    env = os.environ.get("MEMCLAW_INTERVIEWER_PROJECTS", "")
    return [g.strip() for g in env.split(",") if g.strip()]


def _require_config(args: argparse.Namespace) -> Optional[str]:
    if not args.api_key:
        return "MEMCLAW_API_KEY (or --api-key) is required"
    if not args.tenant_id:
        return "MEMCLAW_TENANT_ID (or --tenant-id) is required"
    return None


def _deny_guidance(args: argparse.Namespace) -> str:
    projects = list_project_dirs(args.projects_root)
    lines = [
        "No project allowlist configured - refusing to harvest by default.",
        "Claude Code transcripts can contain sensitive work across ALL projects;",
        "opt in explicitly with --projects <glob...>, MEMCLAW_INTERVIEWER_PROJECTS,",
        "or --all-projects.",
        "",
        "Discovered project dirs:",
    ]
    lines += [f"  {p}" for p in projects] or ["  (none)"]
    return "\n".join(lines)


def _acquire_lock() -> Optional[object]:
    """Best-effort cross-invocation guard (cron + hook overlap).

    The REAL safety is the server's deterministic attempt-id dedup; this
    just avoids burning duplicate LLM windows. Never blocks. On platforms
    without ``fcntl`` (Windows) the guard degrades to a no-op — dedup
    still protects correctness.
    """
    try:
        import fcntl
    except ImportError:
        # No fcntl (Windows): PROCEED without a lock rather than treating
        # it as "already locked" — returning None here would make every
        # Windows run exit immediately. Dedup still protects correctness.
        return object()
    # Per-user filename: a shared /tmp lock owned by another user would
    # fail our open() with EACCES forever, reading as "always locked".
    lock_path = Path(tempfile.gettempdir()) / f"memclaw-interviewer-{getpass.getuser()}.lock"
    handle = None
    try:
        handle = open(lock_path, "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as exc:
        if handle is not None:
            handle.close()
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return None  # genuine contention: another run holds the lock
        # Unrecoverable environment error (EACCES on the lock file,
        # read-only tmpdir, ...): warn and PROCEED lockless, like the
        # no-fcntl path — treating it as contention would silently
        # disable the CLI on that machine forever.
        print(f"[interviewer] lock unavailable ({exc}); proceeding without it", file=sys.stderr)
        return object()


def _make_client(args: argparse.Namespace) -> MemClaw:
    return MemClaw(
        args.api_key,
        tenant_id=args.tenant_id,
        base_url=args.base_url,
        agent_id=args.agent_id,
    )


def _make_config(args: argparse.Namespace, *, flush: bool = False, dry_run: bool = False) -> RunConfig:
    return RunConfig(
        agent_id=args.agent_id,
        machine12=machine_id_short(),
        fleet_id=args.fleet_id,
        max_event_chars=getattr(args, "max_event_chars", 4_000),
        max_windows=getattr(args, "max_windows", 8),
        min_events=getattr(args, "min_events", 10),
        flush=flush,
        dry_run=dry_run,
        verbose=args.verbose,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    if error := _require_config(args):
        print(f"[interviewer] {error}", file=sys.stderr)
        return 2
    allow = _resolve_allowlist(args)
    if args.transcript is not None:
        if not args.transcript.is_file():
            print(f"[interviewer] no such transcript: {args.transcript}", file=sys.stderr)
            return 2
        # --transcript must not bypass the project allowlist — including
        # the EMPTY-allowlist case (default-deny has no carve-outs; same
        # pattern as _cmd_hook and the discovery path below).
        project = args.transcript.parent.name
        if not args.all_projects and not (allow and project_allowed(project, allow)):
            print(
                f"[interviewer] transcript project '{project}' not in allowlist; "
                "pass --all-projects or --projects to opt in",
                file=sys.stderr,
            )
            return 2
        transcripts = [Transcript(path=args.transcript, project=project)]
    else:
        if not allow and not args.all_projects:
            print(_deny_guidance(args), file=sys.stderr)
            return 2
        transcripts = find_transcripts(
            root=args.projects_root,
            allow_globs=allow,
            since_hours=args.since_hours or None,
            all_projects=args.all_projects,
        )
    if not transcripts:
        print("[interviewer] nothing to do (no matching transcripts)")
        return 0

    lock = _acquire_lock()
    if lock is None:
        print("[interviewer] another run holds the lock; exiting", file=sys.stderr)
        return 0
    try:
        cfg = _make_config(args, flush=args.flush, dry_run=args.dry_run)
        with _make_client(args) as mc:
            summary = run_all(mc, transcripts, cfg)
    finally:
        # Deterministic flock release: on non-CPython runtimes the ref-count
        # drop is not prompt, and a lingering handle blocks the next
        # cron/hook invocation until GC. (The Windows sentinel has no close.)
        if hasattr(lock, "close"):
            lock.close()

    submitted = sum(f.windows_submitted for f in summary.files)
    memories = sum(f.memories_written for f in summary.files)
    print(
        f"[interviewer] {len(summary.files)} file(s): {submitted} window(s) submitted, "
        f"{memories} memories written"
        + (" [dry-run]" if args.dry_run else "")
    )
    if summary.aborted:
        return 2
    if summary.failed_all:
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    if error := _require_config(args):
        print(f"[interviewer] {error}", file=sys.stderr)
        return 2
    allow = _resolve_allowlist(args)
    if not allow and not args.all_projects:
        print(_deny_guidance(args), file=sys.stderr)
        return 2
    transcripts = find_transcripts(
        root=args.projects_root,
        allow_globs=allow,
        since_hours=args.since_hours or None,
        all_projects=args.all_projects,
    )
    machine12 = machine_id_short()
    with _make_client(args) as mc:
        for transcript in transcripts:
            node_id = node_id_for(machine12, transcript.path)
            try:
                last_seq = read_watermark(mc, node_id)
                # Inside the guard: the transcript can vanish between
                # discovery and here (same TOCTOU as discovery's stat).
                lines = count_lines(transcript.path)
            except AuthError as exc:
                print(f"[interviewer] ABORT: {exc}", file=sys.stderr)
                return 2
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill status
                print(
                    f"{transcript.project}/{transcript.path.name}: status check failed: {exc}",
                    file=sys.stderr,
                )
                continue
            pending = max(0, lines - (last_seq + 1))
            print(f"{transcript.project}/{transcript.path.name}: lines={lines} cursor={last_seq} pending={pending}")
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    """SessionEnd hook: read {transcript_path,...} JSON from stdin, drain
    that one file. ALWAYS exit 0 — never fail the user's session."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        transcript_path = Path(str(payload.get("transcript_path") or ""))
        if not transcript_path.is_file():
            return 0
        allow = _resolve_allowlist(args)
        project = transcript_path.parent.name
        if not args.all_projects and not (allow and project_allowed(project, allow)):
            # Warn so operators can tell a config gap from an empty
            # session; exit code stays 0 (never fail the session hook).
            print(
                f"[interviewer] hook: project '{project}' not in allowlist; skipping "
                "(set MEMCLAW_INTERVIEWER_PROJECTS or --all-projects)",
                file=sys.stderr,
            )
            return 0
        if error := _require_config(args):
            print(f"[interviewer] hook: {error}; skipping", file=sys.stderr)
            return 0
        lock = _acquire_lock()
        if lock is None:
            return 0
        try:
            cfg = _make_config(args, flush=True)  # session over: drain the tail
            with _make_client(args) as mc:
                run_all(mc, [Transcript(path=transcript_path, project=project)], cfg)
        finally:
            if hasattr(lock, "close"):
                lock.close()
    except Exception as exc:  # noqa: BLE001 - hook must never fail the session
        print(f"[interviewer] hook error (ignored): {exc}", file=sys.stderr)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "hook":
        return _cmd_hook(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
