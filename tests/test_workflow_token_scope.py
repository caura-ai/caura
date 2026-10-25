"""Every job states what its token may do, and nothing it installs keeps one.

A job with no ``permissions:`` block, at its own level or the workflow's, runs
with the repository's default token, which may be able to write. A checkout
that persists its credential leaves that token in ``.git/config`` for every
later step, including the dependency installs. And a test runner resolved from
a version range at run time executes whatever release the range admits that
day. The legacy-name ratchet runs as a required check in other repositories'
pull requests, so its installs are pinned by hash from the same commit as the
workflow itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"
RATCHET = WORKFLOWS / "legacy-name-ratchet.yml"
_REQUIREMENTS = re.compile(r"--require-hashes\s+-r\s+(\S+)")
_PIN = re.compile(r"^[A-Za-z0-9_.-]+==[^\s\\]+")


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda path: path.name)
def test_every_job_states_its_token_permissions(path: Path) -> None:
    document = _load(path)
    if "permissions" in document:
        return
    unstated = [
        name for name, job in document["jobs"].items() if "permissions" not in job
    ]
    assert unstated == [], f"{path.name}: jobs run with the default token"


def test_every_checkout_leaves_no_credential_behind() -> None:
    found = [
        f"{path.name}:{name}"
        for path in _workflow_files()
        for name, job in _load(path)["jobs"].items()
        for step in job.get("steps", [])
        if "actions/checkout@" in step.get("uses", "")
        and (step.get("with") or {}).get("persist-credentials") is not False
    ]
    assert found == []


def _ratchet_steps() -> list[dict[str, Any]]:
    return _load(RATCHET)["jobs"]["ratchet"]["steps"]


def test_the_ratchet_installs_its_test_runner_by_hash() -> None:
    steps = _ratchet_steps()
    gate = next(step for step in steps if step.get("name") == "The gate's own tests")
    installs = [line for line in gate["run"].splitlines() if "pip install" in line]
    assert installs, "the gate's own tests install nothing; the scan has gone blind"
    for line in installs:
        assert "--require-hashes" in line, line
    (requirements,) = _REQUIREMENTS.findall(gate["run"])

    # Read from the engine source, the commit this workflow was loaded from,
    # never from the candidate under review.
    assert gate["working-directory"] == "engine-source"
    engine = next(
        step
        for step in steps
        if (step.get("with") or {}).get("path") == "engine-source"
    )
    assert requirements in engine["with"]["sparse-checkout"].split()

    pins = [
        line
        for line in (REPO / requirements).read_text().splitlines()
        if line and not line.startswith((" ", "#"))
    ]
    assert any(line.startswith("pytest==") for line in pins), pins
    for line in pins:
        assert _PIN.match(line), line
    assert "--hash=sha256:" in (REPO / requirements).read_text()
