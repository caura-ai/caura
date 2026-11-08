"""Every workflow that runs uv gets uv and its Pythons from Astral's mirror.

On 8 Nov 2026, GitHub release-asset downloads answered 504 for hours, and every
CI job here failed while setting up, before running anything:

* ``uv python install`` and ``uv venv --python`` fetch python-build-standalone
  builds from GitHub release assets by default. ``UV_PYTHON_INSTALL_MIRROR``
  points them at Astral's mirror of the same builds. uv checks each download
  against the SHA-256 in the metadata it ships with, whichever URL served it,
  so the mirror cannot change what is installed.
* ``astral-sh/setup-uv`` before v9 downloads the uv binary itself from GitHub
  releases. From v9 its ``download-from-astral-mirror`` input, on by default,
  takes it from Astral's mirror instead.

A workflow runs uv if its own steps do, or a local composite action it uses
does. The mirror is set in the workflow's top-level ``env``, so it reaches
those actions and every uv call in any job.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTIONS = ROOT / ".github" / "actions"
MIRROR = "https://releases.astral.sh/github/python-build-standalone/releases/download"
RUNS_UV = re.compile(
    r"(^|[\s;&|(])uv (run|sync|venv|pip|tool|python|export|lock)\b", re.MULTILINE
)
LOCAL_ACTION = re.compile(r"uses:\s*\./\.github/actions/([\w-]+)")
SETUP_UV = re.compile(r"astral-sh/setup-uv@[0-9a-f]{40}(?:\s*#\s*v(\d+)\.)?")


def _runs_uv(text: str, seen: frozenset[str] = frozenset()) -> bool:
    if RUNS_UV.search(text):
        return True
    for name in set(LOCAL_ACTION.findall(text)) - seen:
        action = (ACTIONS / name / "action.yml").read_text()
        if _runs_uv(action, seen | {name}):
            return True
    return False


def _steps(workflow: dict) -> list[dict]:
    return [
        step
        for job in (workflow.get("jobs") or {}).values()
        for step in job.get("steps") or []
    ]


def test_every_workflow_that_runs_uv_uses_the_astral_python_mirror() -> None:
    users = [
        path for path in sorted(WORKFLOWS.glob("*.yml")) if _runs_uv(path.read_text())
    ]
    assert "ci.yml" in {path.name for path in users}, (
        "the scan no longer sees ci.yml's uv calls"
    )
    missing = [
        path.name
        for path in users
        if (yaml.safe_load(path.read_text()).get("env") or {}).get(
            "UV_PYTHON_INSTALL_MIRROR"
        )
        != MIRROR
    ]
    assert missing == [], (
        f"{missing} run uv without UV_PYTHON_INSTALL_MIRROR={MIRROR} in their "
        "top-level env, so their Python setup depends on GitHub release assets"
    )


def test_every_setup_uv_downloads_uv_from_the_astral_mirror() -> None:
    pins = [
        (path.name, match)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for match in SETUP_UV.finditer(path.read_text())
    ]
    assert pins, "the scan no longer sees any setup-uv step"
    unmirrored = [
        f"{name}: {match.group(0)}"
        for name, match in pins
        if match.group(1) is None or int(match.group(1)) < 9
    ]
    assert unmirrored == [], (
        f"{unmirrored} pin setup-uv older than v9, or without its version "
        "comment, so it downloads uv from GitHub releases"
    )
    switched_off = [
        path.name
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for step in _steps(yaml.safe_load(path.read_text()))
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
        and str(
            (step.get("with") or {}).get("download-from-astral-mirror", "true")
        ).lower()
        == "false"
    ]
    assert switched_off == [], (
        f"{switched_off} turn setup-uv's download-from-astral-mirror off"
    )
