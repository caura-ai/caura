"""M-01 (part 2) and L-166: CI checks each service against what its image ships.

Each image installs its service's ``uv.lock`` frozen. CI type-checked and tested
every service in one union venv instead, resolved afresh from the pyproject
ranges, and it matched none of the locks: core-api's pinned SQLAlchemy 2.0.50
and core-storage-api's 2.1.0, while the union held a 2.0.x. So storage was
type-checked and tested against a SQLAlchemy its image never installs. Each
service's mypy and pytest steps now run in that service's own environment,
built from its lock, with the extras its image installs.

L-166: the root ``requirements.txt``, the local-dev install path, allowed
SQLAlchemy up to 3 while the services capped it at 2.1 and 2.2.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github/workflows/ci.yml"
SERVICES = ["core-api", "core-storage-api", "core-operations", "core-worker"]


def _runs() -> list[str]:
    jobs = yaml.safe_load(CI.read_text())["jobs"].values()
    steps = (step for job in jobs for step in job.get("steps", []))
    return [step["run"] for step in steps if "run" in step]


def _project(service: str) -> dict:
    return tomllib.loads((REPO / service / "pyproject.toml").read_text())["project"]


def _uv_run_options(run: str, program: str) -> list[str]:
    """The options of ``uv run [options] <program>`` in ``run``."""
    words = run.replace("\\\n", " ").split()
    for i in range(len(words) - 1):
        if words[i : i + 2] == ["uv", "run"] and program in words[i + 2 :]:
            return words[i + 2 : words.index(program, i + 2)]
    raise AssertionError(f"no uv run {program} in {run!r}")


def _image_extras(service: str) -> set[str]:
    """The extras the service's image always installs: ``EXTRAS="--extra ..."``."""
    dockerfile = (REPO / service / "Dockerfile").read_text()
    default = re.search(r'EXTRAS="([^"]*)"', dockerfile)
    assert default, f"{service}/Dockerfile names no EXTRAS"
    return set(re.findall(r"--extra (\S+)", default.group(1)))


def _installed_extras(service: str, options: list[str]) -> set[str]:
    """The extras ``--extra`` names, and those ``dev`` pulls in by naming the
    service itself (``core-worker[test,pubsub]``)."""
    named = {options[i + 1] for i, word in enumerate(options) if word == "--extra"}
    if "dev" not in named:
        return named
    project = _project(service)
    for item in project["optional-dependencies"]["dev"]:
        requirement = Requirement(item)
        if canonicalize_name(requirement.name) == canonicalize_name(project["name"]):
            named |= requirement.extras
    return named


def _assert_from_the_lock(service: str, options: list[str]) -> None:
    assert "--frozen" in options, options
    assert "dev" in _installed_extras(service, options), options
    missing = _image_extras(service) - _installed_extras(service, options)
    assert not missing, f"the image installs {sorted(missing)}, this step does not"


@pytest.mark.parametrize("service", SERVICES)
def test_the_service_is_type_checked_from_its_lock(service: str) -> None:
    runs = [run for run in _runs() if f"cd {service} " in run and "mypy" in run]
    assert len(runs) == 1, runs
    _assert_from_the_lock(service, _uv_run_options(runs[0], "mypy"))


@pytest.mark.parametrize("service", SERVICES)
def test_the_service_suite_runs_from_its_lock(service: str) -> None:
    runs = [run for run in _runs() if re.search(rf"\bpytest {service}/tests/", run)]
    assert len(runs) == 1, runs
    options = _uv_run_options(runs[0], "pytest")
    assert f"--project {service}" in " ".join(options), options
    _assert_from_the_lock(service, options)


def _caps(requirement: Requirement) -> list[Version]:
    return [Version(s.version) for s in requirement.specifier if s.operator == "<"]


def test_l166_the_root_requirements_cap_no_higher_than_the_services() -> None:
    """The local-dev install runs the services from one environment, so it may
    not admit a version that any service declaring the package excludes: its cap
    is no higher than the lowest cap a service sets."""
    declared: dict[str, list[Requirement]] = {}
    for service in SERVICES:
        project = _project(service)
        items = list(project["dependencies"])
        for extra in project["optional-dependencies"].values():
            items += extra
        for item in items:
            requirement = Requirement(item)
            name = canonicalize_name(requirement.name)
            declared.setdefault(name, []).append(requirement)

    wider = []
    for line in (REPO / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        root = Requirement(line)
        declaring = declared.get(canonicalize_name(root.name), [])
        caps = [cap for requirement in declaring for cap in _caps(requirement)]
        if not caps:
            continue  # no service caps it
        if not _caps(root) or min(_caps(root)) > min(caps):
            wider.append(f"{line} (a service caps it at <{min(caps)})")
    assert wider == []
