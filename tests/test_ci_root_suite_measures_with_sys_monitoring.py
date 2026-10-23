"""The root suite's coverage runs through ``sys.monitoring``, and nothing turns that off.

``COVERAGE_CORE=sysmon`` in the Root tests job runs the suite about 25% faster
than coverage's trace function. It also measures code that runs inside
SQLAlchemy's greenlets, which the trace function missed. On Python 3.13,
sys.monitoring measures lines but not branches. A coverage config at the repo
root that turned on ``branch`` would make coverage fall back to the trace
function, with only a warning in the log, so the job would quietly get slower
again.
"""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text())


def _run_tests_step() -> dict[str, Any]:
    (job,) = [j for j in _workflow()["jobs"].values() if j.get("name") == "Root tests"]
    (step,) = [s for s in job["steps"] if s.get("name") == "Run tests"]
    return step


def _branch_settings(root: Path) -> dict[str, str]:
    """Every ``branch`` setting a coverage run started in ``root`` would read."""
    found = {}
    for name, section in (
        (".coveragerc", "run"),
        ("setup.cfg", "coverage:run"),
        ("tox.ini", "coverage:run"),
    ):
        path = root / name
        if path.exists():
            parser = configparser.ConfigParser()
            parser.read(path)
            if parser.has_option(section, "branch"):
                found[name] = parser.get(section, "branch")
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        run = (
            tomllib.loads(pyproject.read_text())
            .get("tool", {})
            .get("coverage", {})
            .get("run", {})
        )
        if "branch" in run:
            found["pyproject.toml"] = str(run["branch"])
    return found


def test_the_root_suite_measures_through_sys_monitoring():
    assert _run_tests_step()["env"].get("COVERAGE_CORE") == "sysmon"


def test_no_root_coverage_config_turns_on_branch_measurement():
    python = tuple(
        int(part) for part in str(_workflow()["env"]["PYTHON_VERSION"]).split(".")
    )
    if python >= (3, 14):
        pytest.skip("sys.monitoring measures branches from Python 3.14")
    on = {
        name: value
        for name, value in _branch_settings(ROOT).items()
        if value.lower() in ("true", "1", "yes", "on")
    }
    assert not on, (
        f"branch coverage would make COVERAGE_CORE=sysmon fall back to the trace function: {on}"
    )


def test_a_root_config_that_turns_on_branch_measurement_is_found(tmp_path):
    (tmp_path / ".coveragerc").write_text("[run]\nbranch = True\n")
    (tmp_path / "pyproject.toml").write_text("[tool.coverage.run]\nbranch = true\n")

    assert _branch_settings(tmp_path) == {
        ".coveragerc": "True",
        "pyproject.toml": "True",
    }
