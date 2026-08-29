"""caura-interviewer CLI — run / status / hook.

Config precedence: flags > env > defaults. Env vars:
  CAURA_API_KEY (required)      CAURA_TENANT_ID (required)
  CAURA_BASE_URL                CAURA_AGENT_ID (default user@host)
  CAURA_FLEET_ID                CAURA_INTERVIEWER_PROJECTS (comma-sep globs)

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
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from ..client import Caura
from ..exceptions import AuthError
from .discovery import (
    DEFAULT_CURSOR_PROJECTS_ROOT,
    DEFAULT_PROJECTS_ROOT,
    HARNESS_CLAUDE_CODE,
    HARNESS_CURSOR,
    find_transcripts,
    list_project_dirs,
    project_allowed,
    transcript_from_path,
)
from . import installer
from .machine import machine_id_short
from .parser import count_lines
from .runner import RunConfig, node_id_for, read_watermark, run_all

DEFAULT_BASE_URL = "https://caura.ai"


def _default_agent_id() -> str:
    return f"cc-{getpass.getuser()}@{socket.gethostname()}"


def _read_env(*names: str, default: str = "") -> str:
    """First alias carrying a value, new names before old ones.

    A blank alias never shadows a working one, but a lone ``X=`` still means
    "empty" rather than falling back to ``default`` — which is what
    ``os.environ.get(name, default)`` did before the aliases existed.
    """
    saw_blank = False
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
        if value is not None:
            saw_blank = True
    return "" if saw_blank else default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caura-interviewer",
        description="Caura Interviewer adapter for Claude Code transcripts (read-only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--base-url",
            default=_read_env("CAURA_BASE_URL", "MEMCLAW_BASE_URL", default=DEFAULT_BASE_URL),  # legacy-name-ok: rule 3 dual-read alias
        )
        p.add_argument("--api-key", default=_read_env("CAURA_API_KEY", "MEMCLAW_API_KEY"))  # legacy-name-ok: rule 3 dual-read alias
        p.add_argument("--tenant-id", default=_read_env("CAURA_TENANT_ID", "MEMCLAW_TENANT_ID"))  # legacy-name-ok: rule 3 dual-read alias
        p.add_argument(
            "--agent-id",
            default=_read_env("CAURA_AGENT_ID", "MEMCLAW_AGENT_ID") or _default_agent_id(),  # legacy-name-ok: rule 3 dual-read alias
        )
        p.add_argument("--fleet-id", default=_read_env("CAURA_FLEET_ID", "MEMCLAW_FLEET_ID") or None)  # legacy-name-ok: rule 3 dual-read alias
        p.add_argument(
            "--projects",
            nargs="+",
            default=None,
            help="project-dir globs to allow (default: CAURA_INTERVIEWER_PROJECTS env; DEFAULT-DENY without either)",
        )
        p.add_argument("--all-projects", action="store_true", help="explicit opt-in: harvest every project dir")
        p.add_argument(
            "--harness",
            choices=(HARNESS_CLAUDE_CODE, HARNESS_CURSOR),
            default=_read_env(
                "CAURA_INTERVIEWER_HARNESS",
                "MEMCLAW_INTERVIEWER_HARNESS",  # legacy-name-ok: rule 3 dual-read alias
                default=HARNESS_CLAUDE_CODE,
            ),
            help="which agent's transcripts to harvest (default: claude-code; env CAURA_INTERVIEWER_HARNESS)",
        )
        p.add_argument(
            "--projects-root",
            type=Path,
            default=None,
            help="override the harness's default projects root (~/.claude/projects or ~/.cursor/projects)",
        )
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

    install_p = sub.add_parser("install", help="schedule a periodic `run` via cron (writes a 0600 env file it sources)")
    common(install_p)
    install_p.add_argument("--interval", default="30m", help="cron cadence, e.g. '30m' or '2h' (default 30m)")

    uninstall_p = sub.add_parser("uninstall", help="remove the cron entry (and env file) written by `install`")
    uninstall_p.add_argument("--keep-env", action="store_true", help="leave the 0600 env file in place")
    return parser


def _projects_root(args: argparse.Namespace) -> Path:
    if args.projects_root is not None:
        return args.projects_root
    if args.harness == HARNESS_CURSOR:
        return DEFAULT_CURSOR_PROJECTS_ROOT
    return DEFAULT_PROJECTS_ROOT


def _resolve_allowlist(args: argparse.Namespace) -> list[str]:
    if args.projects is not None:
        return list(args.projects)
    env = _read_env("CAURA_INTERVIEWER_PROJECTS", "MEMCLAW_INTERVIEWER_PROJECTS")  # legacy-name-ok: rule 3 dual-read alias
    return [g.strip() for g in env.split(",") if g.strip()]


def _require_config(args: argparse.Namespace) -> Optional[str]:
    if not args.api_key:
        return "CAURA_API_KEY (or --api-key) is required"
    if not args.tenant_id:
        return "CAURA_TENANT_ID (or --tenant-id) is required"
    return None


def _deny_guidance(args: argparse.Namespace) -> str:
    projects = list_project_dirs(_projects_root(args))
    lines = [
        "No project allowlist configured - refusing to harvest by default.",
        "Agent transcripts can contain sensitive work across ALL projects;",
        "opt in explicitly with --projects <glob...>, CAURA_INTERVIEWER_PROJECTS,",
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


def _make_client(args: argparse.Namespace) -> Caura:
    return Caura(
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
        # pattern as _cmd_hook and the discovery path below). Harness is
        # inferred from the path shape, not --harness: the file IS the
        # ground truth.
        transcript = transcript_from_path(args.transcript)
        if not args.all_projects and not (allow and project_allowed(transcript.project, allow)):
            print(
                f"[interviewer] transcript project '{transcript.project}' not in allowlist; "
                "pass --all-projects or --projects to opt in",
                file=sys.stderr,
            )
            return 2
        transcripts = [transcript]
    else:
        if not allow and not args.all_projects:
            print(_deny_guidance(args), file=sys.stderr)
            return 2
        transcripts = find_transcripts(
            root=_projects_root(args),
            allow_globs=allow,
            since_hours=args.since_hours or None,
            all_projects=args.all_projects,
            harness=args.harness,
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
        root=_projects_root(args),
        allow_globs=allow,
        since_hours=args.since_hours or None,
        all_projects=args.all_projects,
        harness=args.harness,
    )
    machine12 = machine_id_short()
    with _make_client(args) as mc:
        for transcript in transcripts:
            node_id = node_id_for(machine12, transcript.path, transcript.dialect)
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
    """Session-end hook: read {transcript_path,...} JSON from stdin, drain
    that one file. ALWAYS exit 0 — never fail the user's session.

    Both Claude Code (SessionEnd) and Cursor (sessionEnd/stop) hooks send
    ``transcript_path`` in their stdin payload; the harness is inferred
    from the path shape, so ONE hook command serves both."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        transcript_path = Path(str(payload.get("transcript_path") or ""))
        if not transcript_path.is_file():
            return 0
        allow = _resolve_allowlist(args)
        transcript = transcript_from_path(transcript_path)
        if not args.all_projects and not (allow and project_allowed(transcript.project, allow)):
            # Warn so operators can tell a config gap from an empty
            # session; exit code stays 0 (never fail the session hook).
            print(
                f"[interviewer] hook: project '{transcript.project}' not in allowlist; skipping "
                "(set CAURA_INTERVIEWER_PROJECTS or --all-projects)",
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
                run_all(mc, [transcript], cfg)
        finally:
            if hasattr(lock, "close"):
                lock.close()
    except Exception as exc:  # noqa: BLE001 - hook must never fail the session
        print(f"[interviewer] hook error (ignored): {exc}", file=sys.stderr)
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    """Write an idempotent cron entry that runs `run` on --interval, plus a
    0600 env file it sources (cron does not inherit the shell environment)."""
    if not installer.cron_available():
        print(
            "[interviewer] `crontab` not found — cron install is unsupported here "
            "(Windows: use Task Scheduler to run `caura-interviewer run` on a timer).",
            file=sys.stderr,
        )
        return 2
    if error := _require_config(args):
        print(f"[interviewer] {error} — needed so the scheduled job can authenticate", file=sys.stderr)
        return 2
    allow = _resolve_allowlist(args)
    if not allow and not args.all_projects:
        # Refuse to schedule a job that would just default-deny and no-op.
        print(_deny_guidance(args), file=sys.stderr)
        return 2
    try:
        schedule = installer.interval_to_cron(args.interval)
    except ValueError as exc:
        print(f"[interviewer] {exc}", file=sys.stderr)
        return 2

    # New installs are written with the new names only. Existing env files keep
    # their old names and keep working — every reader above accepts both.
    env = {
        "CAURA_BASE_URL": args.base_url,
        "CAURA_API_KEY": args.api_key,
        "CAURA_TENANT_ID": args.tenant_id,
        "CAURA_AGENT_ID": args.agent_id,
        "CAURA_FLEET_ID": args.fleet_id or "",
    }
    if not args.all_projects:
        env["CAURA_INTERVIEWER_PROJECTS"] = ",".join(allow)

    env_file = installer.env_file_path()
    log_file = installer.log_file_path()
    try:
        installer.write_env_file(env_file, installer.render_env_file(env))
    except OSError as exc:
        print(f"[interviewer] failed to write env file {env_file}: {exc}", file=sys.stderr)
        return 1
    command = installer.build_run_command(
        harness=args.harness, all_projects=args.all_projects, env_file=env_file, log_file=log_file
    )
    line = installer.build_cron_line(schedule, command)
    try:
        installer.write_crontab(installer.merge_crontab(installer.read_crontab(), line))
    except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
        # OSError too: the `crontab` binary can vanish / lose +x between
        # cron_available() and the actual run (TOCTOU), or write_crontab's
        # subprocess can fail to exec — both must hit this clean path, not a
        # raw traceback that skips the env-file cleanup below.
        print(f"[interviewer] failed to update crontab: {exc}", file=sys.stderr)
        # No job was scheduled, so the 0600 env file (holds the API key) is
        # orphaned — clean it up rather than leaving a stray secret behind.
        try:
            env_file.unlink(missing_ok=True)
            print(f"  env file {env_file} has been removed.", file=sys.stderr)
        except OSError as unlink_err:
            print(
                f"  env file was written to {env_file} but could not be removed "
                f"({unlink_err}) — remove it with:",
                file=sys.stderr,
            )
            print(f"  rm {env_file}", file=sys.stderr)
        return 1

    print(f"[interviewer] scheduled: {schedule}  (harness={args.harness})")
    print(f"  env file: {env_file} (0600)")
    print(f"  log:      {log_file}")
    print("  remove with: caura-interviewer uninstall")
    if sys.platform == "darwin":
        print(
            "  note (macOS): if the job can't read the transcripts, grant Full Disk "
            "Access to /usr/sbin/cron in System Settings > Privacy & Security.",
        )
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    if not installer.cron_available():
        print("[interviewer] `crontab` not found; nothing to remove.", file=sys.stderr)
        return 0
    try:
        before = installer.read_crontab()
        after = installer.merge_crontab(before, None)
        removed = before != after
        if removed:
            installer.write_crontab(after)
    except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
        # OSError too (crontab binary un-executable) — surface it cleanly
        # rather than as a raw traceback. Crontab state is unknown, so leave
        # the env file in place so a still-scheduled job keeps working; the
        # user can re-run uninstall.
        print(f"[interviewer] failed to update crontab: {exc}", file=sys.stderr)
        return 1
    env_file = installer.env_file_path()
    env_deleted = False
    if not args.keep_env and env_file.exists():
        try:
            env_file.unlink()
            env_deleted = True
        except OSError as exc:
            print(f"[interviewer] warning: could not remove env file {env_file}: {exc}", file=sys.stderr)
    env_file_present = env_file.exists() or env_deleted
    suffix = "; env file deleted" if env_deleted else ("; env file kept" if (args.keep_env and env_file_present) else "")
    print(f"[interviewer] {'removed cron entry' if removed else 'no cron entry found'}{suffix}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # argparse's choices= only validates values passed on the command line,
    # NOT a default sourced from os.environ — so a bad
    # CAURA_INTERVIEWER_HARNESS (wrong case "Cursor", a typo) would slip
    # through and silently fall back to Claude Code behavior. Fail loudly —
    # but ONLY for run/status: parser.error() exits 2, and `hook` both
    # ignores --harness (it infers from path shape) and must ALWAYS exit 0.
    if (
        args.command in ("run", "status", "install")
        and hasattr(args, "harness")
        and args.harness not in (HARNESS_CLAUDE_CODE, HARNESS_CURSOR)
    ):
        parser.error(
            f"Invalid harness '{args.harness}' — set CAURA_INTERVIEWER_HARNESS to "
            f"'{HARNESS_CLAUDE_CODE}' or '{HARNESS_CURSOR}'"
        )
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "hook":
        return _cmd_hook(args)
    if args.command == "install":
        return _cmd_install(args)
    if args.command == "uninstall":
        return _cmd_uninstall(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
