"""Cron scheduling helper for ``memclaw-interviewer install`` / ``uninstall``.

`pip install` cannot register a cron (wheels run no install-time code), and
silently scheduling a job that reads transcripts and phones home would be the
wrong consent posture anyway. So scheduling is one explicit command that
writes:

- a **crontab line** (idempotent — keyed by a marker comment) invoking
  ``memclaw-interviewer run`` on an interval; and
- a **0600 env file** the cron line sources, because cron does NOT inherit
  the user's shell environment — the connection identity (base URL, key,
  tenant) would otherwise be absent when the job fires.

The pure functions here (interval parsing, cron-line building, crontab merge,
env-file rendering) hold the logic and are unit-tested; the thin
subprocess/filesystem wrappers are monkeypatched in tests.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Marker comment appended to our managed cron line so uninstall/idempotent
# re-install can find and remove exactly our entry, never the user's others.
CRON_MARKER = "# memclaw-interviewer (managed)"

# Only the connection identity is persisted to the env file (0600). Non-secret
# behavior flags (--harness, --all-projects) ride on the cron command instead.
_ENV_KEYS = (
    "MEMCLAW_BASE_URL",
    "MEMCLAW_API_KEY",
    "MEMCLAW_TENANT_ID",
    "MEMCLAW_AGENT_ID",
    "MEMCLAW_FLEET_ID",
    "MEMCLAW_INTERVIEWER_PROJECTS",
)

_INTERVAL_RE = re.compile(r"^(\d+)\s*([mh])$", re.IGNORECASE)


def config_dir() -> Path:
    return Path.home() / ".config" / "memclaw-interviewer"


def env_file_path() -> Path:
    return config_dir() / "env"


def log_file_path() -> Path:
    return config_dir() / "cron.log"


def interval_to_cron(interval: str) -> str:
    """'30m' -> '*/30 * * * *'; '1h' -> '0 * * * *'; 'Nh' -> '0 */N * * *'.

    Raises ValueError on an unparseable or out-of-range interval.
    """
    m = _INTERVAL_RE.match(interval.strip())
    if not m:
        raise ValueError(f"interval must look like '30m' or '2h', got {interval!r}")
    n, unit = int(m.group(1)), m.group(2).lower()
    if n < 1:
        raise ValueError("interval must be >= 1")
    if unit == "m":
        if n > 59:
            raise ValueError("minute interval must be 1-59 (use e.g. '1h' for hourly)")
        return f"*/{n} * * * *"
    # hours
    if n > 23:
        raise ValueError("hour interval must be 1-23 (use a daily cron for longer)")
    return "0 * * * *" if n == 1 else f"0 */{n} * * *"


def resolve_cmd() -> str:
    """Absolute invocation for the CLI, resilient to how it was installed.

    Prefer the console-script on PATH; fall back to ``<python> -m`` so a
    venv/editable install still schedules a working command. Each path
    component is shell-quoted here (NOT the whole string — the fallback is
    two words and quoting it wholesale would make sh treat it as one
    command name).
    """
    exe = shutil.which("memclaw-interviewer")
    if exe:
        return shlex.quote(exe)
    return f"{shlex.quote(sys.executable)} -m caura_client.interviewer.cli"


def build_run_command(
    *, harness: str, all_projects: bool, env_file: Path, log_file: Path, cmd: Optional[str] = None
) -> str:
    """The shell the cron line executes: source env, then drain.

    ``cmd``, when given, is used verbatim and must already be shell-quoted
    (``resolve_cmd()`` quotes its own components).
    """
    invocation = cmd or resolve_cmd()
    run = f"{invocation} run --harness {shlex.quote(harness)}"
    if all_projects:
        run += " --all-projects"
    # `.` (POSIX source) — cron runs /bin/sh. Group both steps so the log
    # redirect covers the source step too (a bare `A && B >> log` would only
    # redirect B, losing any error from sourcing the env file to cron's mailer).
    return f"{{ . {shlex.quote(str(env_file))} && {run}; }} >> {shlex.quote(str(log_file))} 2>&1"


def build_cron_line(schedule: str, command: str) -> str:
    return f"{schedule} {command} {CRON_MARKER}"


def merge_crontab(existing: str, new_line: Optional[str]) -> str:
    """Remove any prior managed line (idempotent), then optionally append.

    ``new_line=None`` is the uninstall case (strip only). Preserves every
    other line and a single trailing newline.
    """
    kept = [ln for ln in existing.splitlines() if CRON_MARKER not in ln]
    if new_line is not None:
        kept.append(new_line)
    body = "\n".join(kept)
    return body + "\n" if body else ""


def render_env_file(env: dict[str, str]) -> str:
    """Shell-sourceable ``export`` lines for the set connection vars only."""
    lines = ["# Written by `memclaw-interviewer install` — sourced by the cron job.", ""]
    for k in _ENV_KEYS:
        v = env.get(k)
        if v:
            # Single-quote the value; escape embedded single quotes safely.
            safe = v.replace("'", "'\\''")
            lines.append(f"export {k}='{safe}'")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- thin IO seams
def read_crontab() -> str:
    """Current user crontab, or '' when none is installed.

    Only the "no crontab for <user>" case reads as empty. Any OTHER failure
    raises — silently returning '' there would make the next write replace
    (i.e. wipe) a crontab we never actually read.
    """
    try:
        proc = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            # Force C locale so the empty-crontab sentinel below ("no crontab
            # for <user>") is always English, not localized under LANG=fr_FR
            # etc. — otherwise a localized message reads as a real failure.
            env={**os.environ, "LANG": "C", "LC_ALL": "C"},
        )
    except OSError as exc:
        # Binary vanished / lost +x since cron_available() (TOCTOU). Convert
        # to RuntimeError so callers' existing except clauses recognise it as
        # a crontab failure rather than an opaque escaping OSError.
        raise RuntimeError(f"crontab -l could not be executed: {exc}") from exc
    if proc.returncode == 0:
        return proc.stdout
    if "no crontab for" in proc.stderr.lower():
        return ""
    raise RuntimeError(f"crontab -l failed (exit {proc.returncode}): {proc.stderr.strip()}")


def write_crontab(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def write_env_file(path: Path, content: str) -> None:
    """Write secrets 0600 with no TOCTOU window.

    ``O_CREAT``'s mode only applies when the file is CREATED — writing into a
    pre-existing looser-mode file would expose the secrets until a trailing
    chmod. So: write a fresh 0600 temp file in the same directory, then
    atomically rename over the target; a pre-existing file's permissions
    never apply to the new content.
    """
    # 0o700: the config dir also holds the cron log (created world-readable by
    # the shell redirect), so a default-0o755 dir would let other users list
    # and read it. parents=True applies this mode only to the leaf dir, so a
    # shared ~/.config keeps its own permissions.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Enforce 0o700 unconditionally: mkdir's mode is subject to umask AND is
    # ignored entirely when the dir already exists (exist_ok) — so a
    # pre-existing looser dir would stay world-listable without this.
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    try:
        try:
            os.chmod(tmp, 0o600)
            os.write(fd, content.encode("utf-8"))
            # fsync before rename: guarantee the credential bytes are on disk,
            # so a crash can't leave a durable rename over an empty/partial file.
            os.fsync(fd)
        finally:
            # Exactly ONE close, on every path (incl. an fsync/write failure)
            # — the inner finally owns the fd, so there is no sentinel to get
            # wrong and no descriptor left open if a step above raises.
            os.close(fd)
        os.replace(tmp, path)  # atomic; consumes tmp on success
    except BaseException:
        # Any failure above (chmod/write/fsync/close/replace): don't leave the
        # 0600 temp file — it holds the API key — behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cron_available() -> bool:
    return shutil.which("crontab") is not None
