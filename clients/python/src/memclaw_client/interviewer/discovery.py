"""Transcript discovery under ~/.claude/projects with allowlist gating.

Privacy posture (user-confirmed): DEFAULT-DENY. With no allowlist the CLI
lists what it found and exits with guidance — transcripts can contain
NDA/client work and pasted secrets across every project on the machine,
and installing the tool is not consent to harvest all of them.
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


@dataclass
class Transcript:
    path: Path
    project: str  # project dir name (the slug)


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
) -> list[Transcript]:
    """Enumerate top-level session ``.jsonl`` files in allowed projects.

    Skips subagent transcripts (``<uuid>/subagents/``) and offloaded tool
    results by construction — only files DIRECTLY under a project dir are
    session transcripts. ``since_hours`` prefilters by mtime so steady-state
    runs don't rescan months of history.
    """
    if not root.is_dir():
        return []
    cutoff = time.time() - since_hours * 3600 if since_hours else None
    found: list[Transcript] = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        if not all_projects and not project_allowed(project_dir.name, allow_globs):
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            try:
                if cutoff is not None and path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue  # deleted/renamed between glob and stat (TOCTOU)
            found.append(Transcript(path=path, project=project_dir.name))
    return found
