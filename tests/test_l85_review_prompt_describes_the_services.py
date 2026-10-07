"""L-85: the automated reviewer is told how the services pin their dependencies.

``EXTRA_PROMPT``, which claude_code_review.yml puts in front of every review,
said the repo "Uses requirements.txt (not uv/pyproject.toml)" and that tests
live in ``tests/``. Each service is a uv project whose image installs its
committed ``uv.lock``, and each keeps its own tests, so a reviewer primed with
that line could pass a pyproject change that was never re-locked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
REVIEW = REPO / ".github/workflows/claude_code_review.yml"
SERVICES = {"core-api", "core-storage-api", "core-worker", "core-operations"}
LOCKED = sorted(path.parent.name for path in REPO.glob("*/uv.lock"))


def _prompt() -> str:
    return yaml.safe_load(REVIEW.read_text())["env"]["EXTRA_PROMPT"]


def test_the_prompt_and_the_services_are_found() -> None:
    assert set(LOCKED) >= SERVICES
    assert _prompt()


def test_the_prompt_says_the_services_install_their_locks() -> None:
    assert "uv.lock" in _prompt()


def test_the_prompt_names_each_service_tests() -> None:
    assert [name for name in LOCKED if f"{name}/tests" not in _prompt()] == []
