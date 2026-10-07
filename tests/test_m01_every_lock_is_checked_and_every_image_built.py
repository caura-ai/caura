"""M-01: CI checks every service lock and builds every service image.

Each service image installs its own ``uv.lock`` as it stands (``uv export
--frozen``), while CI tests the pyproject ranges, resolved afresh. A pyproject
edit made without a re-lock was therefore tested here but missing from the
image, and nothing failed. CI now runs ``uv lock --check`` for every service.
core-worker's image, the write path in deferred mode, was built by no workflow,
so a broken Dockerfile or a dependency it never declared would surface only in a
deploy; CI now builds it too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github/workflows/ci.yml"
SERVICES = {"core-api", "core-storage-api", "core-worker", "core-operations"}
LOCKED = sorted(path.parent.name for path in REPO.glob("*/uv.lock"))
IMAGES = sorted(path.parent.name for path in REPO.glob("*/Dockerfile"))


def _runs(command: str) -> str:
    """Every ci.yml ``run:`` script that runs ``command``, joined."""
    jobs = yaml.safe_load(CI.read_text())["jobs"].values()
    runs = (step.get("run", "") for job in jobs for step in job.get("steps", []))
    return "\n".join(run for run in runs if command in run)


def test_the_scans_see_every_service() -> None:
    assert set(LOCKED) >= SERVICES
    assert set(IMAGES) >= SERVICES
    assert "-f core-api/Dockerfile" in _runs("docker build")


def test_ci_checks_every_service_lock() -> None:
    named = set(re.findall(r"[\w.-]+", _runs("uv lock --check")))
    assert [name for name in LOCKED if name not in named] == []


def test_ci_builds_every_service_image() -> None:
    builds = _runs("docker build")
    assert [name for name in IMAGES if f"-f {name}/Dockerfile" not in builds] == []
