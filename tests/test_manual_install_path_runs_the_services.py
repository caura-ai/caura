"""M-71, M-98: the manual (no Docker) install path installs and starts what runs.

``AGENT-INSTALL.md`` Option B installs ``requirements.txt`` and starts both
services. The file had drifted behind the services' own declarations:
``core_api.app`` imports tiktoken, markdown-it-py and pysbd at startup (document
chunking), none of them listed, so ``uvicorn core_api.app:app`` failed to import
(M-71). And the README's manual deployment started core-api alone, with Postgres
as its dependency, although core-api reaches the database only through
core-storage-api and refuses to start without ``CORE_STORAGE_SHARED_SECRET``
(M-98).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[1]
_SERVICES = ("core-api", "core-storage-api")


def _name(spec: str) -> str:
    """A requirement's distribution name, normalised as PEP 503 does."""
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    assert match, spec
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _requirements_txt() -> set[str]:
    lines = (_REPO / "requirements.txt").read_text().splitlines()
    return {_name(line) for line in lines if line.strip() and line.strip()[0] != "#"}


@pytest.mark.parametrize("service", _SERVICES)
def test_requirements_txt_lists_every_service_dependency(service: str) -> None:
    pyproject = tomllib.loads((_REPO / service / "pyproject.toml").read_text())
    declared = {_name(spec) for spec in pyproject["project"]["dependencies"]}
    missing = sorted(declared - _requirements_txt())
    assert not missing, (
        f"{service} depends on {missing}, which requirements.txt, the manual "
        "install's dependency list, does not install"
    )


def test_the_readme_manual_deployment_starts_storage_with_its_secret() -> None:
    readme = (_REPO / "README.md").read_text()
    start = readme.index("### Manual deployment (without Docker)")
    section = readme[start : readme.index("\n### ", start)]
    assert "core_storage_api.app:app" in section, (
        "the README's manual deployment starts core-api without core-storage-api"
    )
    assert "CORE_STORAGE_SHARED_SECRET" in section, (
        "the README's manual deployment never sets the secret both services need"
    )
