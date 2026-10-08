"""L-84: the root suite runs with the same provider settings locally and in CI.

``tests/conftest.py`` defaulted ``ENTITY_EXTRACTION_PROVIDER`` to ``fake``
while CI exported ``none``, and conftest only ``setdefault``s, so CI's value
won. A test that does not pin a provider ran with enrichment and entity
extraction on locally and off in CI, so green in one place said nothing about
the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.conftest import _TEST_DEFAULTS

pytestmark = pytest.mark.unit

CI = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
PROVIDERS = (
    "EMBEDDING_PROVIDER",
    "ENTITY_EXTRACTION_PROVIDER",
    "USE_LLM_FOR_MEMORY_CREATION",
)


def _root_suite_env() -> dict[str, str]:
    """The env of the ci.yml step that runs pytest over the root ``tests/``."""
    steps = (
        step
        for job in yaml.safe_load(CI.read_text())["jobs"].values()
        for step in job.get("steps", [])
    )
    runs = [
        step
        for step in steps
        if "pytest" in step.get("run", "")
        and re.search(r"(^|\s)tests/(\s|$)", step["run"])
    ]
    assert len(runs) == 1
    return runs[0].get("env", {})


def test_ci_runs_the_suite_with_the_provider_settings_it_defaults_to() -> None:
    env = _root_suite_env()
    ci = {name: env.get(name) for name in PROVIDERS}
    assert ci == {name: _TEST_DEFAULTS[name] for name in PROVIDERS}
