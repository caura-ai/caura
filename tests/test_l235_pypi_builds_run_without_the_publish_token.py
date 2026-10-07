"""L-235: the PyPI publish token never shares a job with the build.

GitHub gives every step of a job with ``id-token: write`` the two token-request
variables, so anything that job installs can mint a PyPI upload token. Each PyPI
workflow ran ``pip install build`` and ``python -m build`` in that job: the newest
build, packaging and setuptools ran with the means to publish, and wrote the files
the publish action then uploaded and attested. Now a job without the permission
builds the files with hash-pinned tools, and the job that can publish only
downloads them and runs the pinned publish action. Those workflows run only on a
release tag, so CI builds every published package with the same tools.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"
TOOLS = ".github/pypi-build/requirements.txt"
PUBLISH = "pypa/gh-action-pypi-publish@"
# All a job that can mint the token may use; its ``run:`` steps install nothing.
_TOKEN_JOB_ACTIONS = ("actions/download-artifact@", PUBLISH)
_INSTALLS = re.compile(r"\b(?:pip3?|pipx|uvx?|poetry|hatch|pdm)\s|-m\s+(?:pip|build)\b")
_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b.*")
_BUILD = re.compile(r"-m\s+build\b.*")
_PINNED = re.compile(r"[\w.-]+==\S+(?:\s+--hash=sha256:[0-9a-f]{64})+")


def _pypi_workflows() -> dict[str, dict]:
    """Every workflow with a PyPI publish step, parsed, by file name."""
    found = {}
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        text = path.read_text()
        if PUBLISH in text:
            found[path.name] = yaml.safe_load(text)
    return found


def _jobs(workflow: dict) -> list[tuple[str, dict, bool]]:
    """``(name, job, can mint the token)``; job permissions replace the workflow's."""
    found = []
    for name, job in workflow["jobs"].items():
        perms = job.get("permissions", workflow.get("permissions"))
        if isinstance(perms, dict):
            perms = perms.get("id-token")
        found.append((name, job, perms in ("write", "write-all")))
    return found


def _requirements(path: Path) -> list[str]:
    """Logical requirement lines: continuations joined, comments dropped."""
    text = re.sub(r"\\\n\s*", " ", path.read_text())
    lines = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return [line for line in lines if line]


def test_the_scan_sees_the_pypi_workflows() -> None:
    """Guards the checks below against passing because they found nothing."""
    workflows = _pypi_workflows()
    assert len(workflows) >= 4
    for workflow in workflows.values():
        assert any(token for _, _, token in _jobs(workflow))


def test_the_job_that_can_publish_runs_nothing_else() -> None:
    found = []
    for name, workflow in _pypi_workflows().items():
        for job_name, job, token in _jobs(workflow):
            for step in job.get("steps", []) if token else []:
                uses = step.get("uses", "")
                if uses and not uses.startswith(_TOKEN_JOB_ACTIONS):
                    found.append(f"{name} {job_name}: {uses.split('@')[0]}")
                if _INSTALLS.search(step.get("run", "")):
                    found.append(f"{name} {job_name}: runs {step.get('name')}")
    assert found == []


def test_every_pypi_build_installs_pinned_tools_and_fetches_nothing() -> None:
    """``pip install --require-hashes -r`` the tools file; ``--no-isolation`` builds."""
    found = []
    for name, workflow in _pypi_workflows().items():
        for job_name, job, _ in _jobs(workflow):
            for step in job.get("steps", []):
                run = step.get("run", "")
                for cmd in _PIP_INSTALL.findall(run):
                    if "--require-hashes" not in cmd or TOOLS not in cmd:
                        found.append(f"{name} {job_name}: {cmd}")
                for cmd in _BUILD.findall(run):
                    if "--no-isolation" not in cmd:
                        found.append(f"{name} {job_name}: python {cmd}")
    assert found == []


def test_the_build_tools_are_pinned_by_hash() -> None:
    """What ``--require-hashes`` installs: each tool at one version, by its hashes."""
    path = REPO / TOOLS
    assert path.is_file(), f"{TOOLS} is missing"
    requirements = _requirements(path)
    names = {re.split(r"[^\w.-]", line, maxsplit=1)[0] for line in requirements}
    assert {"build", "packaging", "pyproject-hooks", "setuptools"} <= names
    assert [line for line in requirements if not _PINNED.fullmatch(line)] == []


def test_ci_builds_every_pypi_package_from_the_pinned_tools() -> None:
    """The PyPI workflows run on a release tag; CI is where a bad pin shows first."""
    ci = (WORKFLOWS / "ci.yml").read_text()
    assert f"pip install --require-hashes -r {TOOLS}" in ci
    assert "python -m build --no-isolation" in ci
