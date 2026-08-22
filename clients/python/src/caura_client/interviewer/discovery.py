"""Transcript discovery under ~/.claude/projects with allowlist gating.

Privacy posture (user-confirmed): DEFAULT-DENY. With no allowlist the CLI
lists what it found and exits with guidance — transcripts can contain
NDA/client work and pasted secrets across every project on the machine,
and installing the tool is not consent to harvest all of them.
"""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Cursor session dirs are UUIDs; a prefix match (8-4-4) is enough to
# distinguish them from an arbitrarily-named dir that merely happens to
# sit under a folder called "agent-transcripts".
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", re.IGNORECASE)

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_CURSOR_PROJECTS_ROOT = Path.home() / ".cursor" / "projects"

HARNESS_CLAUDE_CODE = "claude-code"
HARNESS_CURSOR = "cursor"

# Where session transcripts live RELATIVE to a project dir, per harness.
# Claude Code: <project>/<uuid>.jsonl (subagents live one level deeper).
# Cursor:      <project>/agent-transcripts/<uuid>/<uuid>.jsonl (subagents
#              live under the session dir, one level deeper again).
# Both globs exclude subagent transcripts BY CONSTRUCTION — they sit at a
# depth the glob never reaches.
_SESSION_GLOB = {
    HARNESS_CLAUDE_CODE: "*.jsonl",
    HARNESS_CURSOR: "agent-transcripts/*/*.jsonl",
}


@dataclass
class Transcript:
    path: Path
    project: str  # project dir name (the slug)
    dialect: str = HARNESS_CLAUDE_CODE


def transcript_from_path(path: Path) -> Transcript:
    """Infer harness + project from a transcript's path shape.

    Used where the file arrives without discovery context (--transcript,
    hook stdin). A Cursor transcript always sits inside a session dir under
    ``agent-transcripts``; anything else is treated as Claude Code, whose
    project dir is the immediate parent.
    """
    session_dir = path.parent
    if session_dir.parent.name == "agent-transcripts" and _UUID_RE.match(session_dir.name):
        return Transcript(
            path=path,
            project=session_dir.parent.parent.name,
            dialect=HARNESS_CURSOR,
        )
    return Transcript(path=path, project=path.parent.name)


def list_project_dirs(root: Path = DEFAULT_PROJECTS_ROOT) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def project_allowed(project: str, allow_globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(project, glob) for glob in allow_globs)


def find_transcripts(
    *,
    root: Path = DEFAULT_PROJECTS_ROOT,
    allow_globs: list[str],
    since_hours: Optional[float] = None,
    all_projects: bool = False,
    harness: str = HARNESS_CLAUDE_CODE,
) -> list[Transcript]:
    """Enumerate session ``.jsonl`` files in allowed projects.

    Skips subagent transcripts and offloaded tool results by construction —
    the per-harness glob only reaches session-transcript depth (see
    ``_SESSION_GLOB``). ``since_hours`` prefilters by mtime so steady-state
    runs don't rescan months of history.
    """
    if not root.is_dir():
        return []
    glob = _SESSION_GLOB.get(harness, _SESSION_GLOB[HARNESS_CLAUDE_CODE])
    cutoff = time.time() - since_hours * 3600 if since_hours else None
    found: list[Transcript] = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        if not all_projects and not project_allowed(project_dir.name, allow_globs):
            continue
        for path in sorted(project_dir.glob(glob)):
            try:
                if cutoff is not None and path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue  # deleted/renamed between glob and stat (TOCTOU)
            found.append(Transcript(path=path, project=project_dir.name, dialect=harness))
    return found
