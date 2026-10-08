"""L-86: the pre-commit hook runs the ruff CI runs, over the trees CI checks.

The hook says it mirrors ci.yml, but it pinned ruff 0.15.4, below every
service's ``ruff>=0.16.2`` floor, and ran on ``core-api/src`` and
``core-storage-api/src`` only. CI also lints and format-checks ``common/``,
``scripts/``, ``tests/``, every service's tests and core-worker's and
core-operations' source, so a commit the hook passed could fail CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
PRE_COMMIT = REPO / ".pre-commit-config.yaml"
CI = REPO / ".github/workflows/ci.yml"
SERVICES = {"core-api", "core-storage-api", "core-worker", "core-operations"}
LOCKED = sorted(path.parent.name for path in REPO.glob("*/uv.lock"))


def _ruff_repo() -> dict:
    repos = yaml.safe_load(PRE_COMMIT.read_text())["repos"]
    return next(repo for repo in repos if repo["repo"].endswith("/ruff-pre-commit"))


def _ci_ruff(command: str) -> list[tuple[bool, str]]:
    """``(isolated, path)`` for every path ci.yml runs ``ruff <command>`` on."""
    jobs = yaml.safe_load(CI.read_text())["jobs"].values()
    lines = (
        line.split()
        for job in jobs
        for step in job.get("steps", [])
        for line in step.get("run", "").splitlines()
    )
    return [
        ("--isolated" in words, word)
        for words in lines
        if words[:4] == ["uv", "run", "ruff", command]
        for word in words[4:]
        if not word.startswith("-")
    ]


def _isolated(hook: dict) -> bool:
    return "--isolated" in hook.get("args", [])


def _hooked(hook_ids: set[str], isolated: bool, path: str) -> bool:
    probe = path if path.endswith(".py") else path.rstrip("/") + "/probe.py"
    return any(
        re.search(hook.get("files", ""), probe)
        for hook in _ruff_repo()["hooks"]
        if hook["id"] in hook_ids and _isolated(hook) == isolated
    )


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.lstrip("v").split("."))


def _floor(name: str) -> tuple[int, ...]:
    pyproject = (REPO / name / "pyproject.toml").read_text()
    found = re.search(r'"ruff>=([\d.]+)', pyproject)
    assert found, name
    return _version(found[1])


def test_the_hook_and_the_ci_ruff_steps_are_found() -> None:
    assert set(LOCKED) >= SERVICES
    assert _ci_ruff("check")
    assert _ci_ruff("format")
    assert _ruff_repo()["hooks"]


def test_the_hook_is_at_least_every_service_ruff_floor() -> None:
    rev = _version(_ruff_repo()["rev"])
    assert [name for name in LOCKED if rev < _floor(name)] == []


def test_the_hook_lints_every_path_ci_lints() -> None:
    missed = [
        path
        for isolated, path in _ci_ruff("check")
        if not _hooked({"ruff", "ruff-check"}, isolated, path)
    ]
    assert missed == []


def test_the_hook_formats_every_path_ci_format_checks() -> None:
    missed = [
        path
        for isolated, path in _ci_ruff("format")
        if not _hooked({"ruff-format"}, isolated, path)
    ]
    assert missed == []
