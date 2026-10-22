"""The required ``CI`` check passes only when every job it stands for passed.

Main's branch protection requires a check named ``CI``. That name belonged to
one job that ran the lint and type checks, the root suite and the service
suites in series. They now run as three jobs side by side, and ``CI`` is a last
job that needs them. It can stop gating in two quiet ways: a job left out of
its ``needs``, or a lost ``always()``, which leaves it skipped whenever a needed
job fails, and GitHub counts a skipped required check as passing.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CI = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
# Jobs that report checks of their own rather than through ``CI``. Main's branch
# protection requires three of them by name; the other two are advisory. Every
# other job in the file belongs under ``CI``.
STANDALONE = {
    "evidence-registry",
    "image-version",
    "legacy-name-ratchet",
    "do-not-touch-sentinel",
    "pypi-packages-build",
}
UNION_INSTALL = "uv pip install -e core-storage-api/[dev]"


def _jobs() -> dict[str, dict[str, Any]]:
    return yaml.safe_load(CI.read_text())["jobs"]


def _gate() -> tuple[str, dict[str, Any]]:
    """The one job whose check is named ``CI``."""
    gates = [(key, job) for key, job in _jobs().items() if job.get("name") == "CI"]
    assert len(gates) == 1, [key for key, _ in gates]
    return gates[0]


def _verdict(results: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the gate's check step the way GitHub does, given these job results."""
    _, gate = _gate()
    (script,) = [s["run"] for s in gate["steps"] if "NEEDS" in s.get("env", {})]
    needs = {job: {"result": result, "outputs": {}} for job, result in results.items()}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={**os.environ, "NEEDS": json.dumps(needs)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_gate_needs_every_job_but_the_standalone_ones() -> None:
    key, gate = _gate()
    gated = set(_jobs()) - STANDALONE - {key}
    assert gated, "no job left for CI to gate"
    assert set(gate.get("needs", [])) == gated


def test_the_gate_runs_whatever_its_needs_did() -> None:
    _, gate = _gate()
    assert "always()" in gate.get("if", "")


def test_the_gate_passes_when_every_job_succeeded() -> None:
    _, gate = _gate()
    verdict = _verdict(dict.fromkeys(gate["needs"], "success"))
    assert verdict.returncode == 0, verdict.stdout + verdict.stderr


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
def test_the_gate_fails_unless_every_job_succeeded(outcome: str) -> None:
    _, gate = _gate()
    for job in gate["needs"]:
        results = dict.fromkeys(gate["needs"], "success")
        results[job] = outcome
        verdict = _verdict(results)
        assert verdict.returncode == 1, (job, verdict.stdout + verdict.stderr)
        assert f"did not succeed: {job}" in verdict.stdout


def test_both_jobs_install_the_same_union_environment() -> None:
    """The root suite must run in the environment the lint job checks."""
    installs = {
        key: step["run"]
        for key, job in _jobs().items()
        for step in job.get("steps", [])
        if UNION_INSTALL in step.get("run", "")
    }
    assert set(installs) == {"lint", "tests"}
    assert installs["lint"] == installs["tests"]
