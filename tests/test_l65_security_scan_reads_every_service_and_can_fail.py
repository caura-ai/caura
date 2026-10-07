"""L-65: the security scan reads every service, audits what ships, and can fail.

Both scan steps ran with ``continue-on-error``, so the check passed whatever
bandit or pip-audit found. bandit read only core-api's and core-storage-api's
source, not ``common/``, core-worker's or core-operations', and pip-audit
checked a fresh resolution of those two services' pyproject ranges instead of
the ``uv.lock`` each image installs. Now bandit reads ``common/`` and every
service's source, pip-audit reads every service's lock as its Dockerfile
exports it, and a finding fails the job.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
SCAN = REPO / ".github/workflows/security-scan.yml"
SERVICES = {"core-api", "core-storage-api", "core-worker", "core-operations"}
LOCKED = sorted(path.parent.name for path in REPO.glob("*/uv.lock"))


def _jobs() -> list[dict]:
    return list(yaml.safe_load(SCAN.read_text())["jobs"].values())


def _runs(command: str) -> str:
    """Every scan ``run:`` script that runs ``command``, joined."""
    runs = (step.get("run", "") for job in _jobs() for step in job["steps"])
    return "\n".join(run for run in runs if command in run)


def test_the_scan_sees_the_services_and_its_tools() -> None:
    assert set(LOCKED) >= SERVICES
    assert "core-api/src" in _runs("bandit -r")
    assert _runs("pip-audit --")


def test_a_finding_fails_the_scan() -> None:
    lenient = [job.get("name") for job in _jobs() if job.get("continue-on-error")]
    lenient += [
        step.get("name")
        for job in _jobs()
        for step in job["steps"]
        if step.get("continue-on-error")
    ]
    assert lenient == []


def test_bandit_reads_common_and_every_service() -> None:
    read = set(re.findall(r"[\w./-]+", _runs("bandit -r")))
    wanted = ["common", *(f"{name}/src" for name in LOCKED)]
    assert [path for path in wanted if path not in read] == []


def test_pip_audit_reads_every_service_lock() -> None:
    audit = _runs("pip-audit --")
    assert "uv export --frozen" in audit
    assert "--no-deps" in audit
    named = set(re.findall(r"[\w.-]+", audit))
    assert [name for name in LOCKED if name not in named] == []


def test_pip_audit_reads_what_the_images_install() -> None:
    """Each service's extras come from its Dockerfile. ``--all-extras`` also
    exported extras no image installs, so it failed the scan on pins that
    never ship."""
    audit = _runs("pip-audit --")
    assert "--all-extras" not in audit
    assert "/Dockerfile" in audit
