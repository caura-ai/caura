"""M-03: a boolean workflow input is a boolean, so comparing it to 'true' fails.

The ``inputs`` context keeps a ``type: boolean`` input as a real boolean; only
``github.event.inputs`` turns it into a string. An expression's ``==`` converts
mismatched types to numbers, so ``inputs.flag == 'true'`` is ``1 == NaN`` and
false whatever was ticked. ``publish-docker.yml``'s ``push_latest`` recovery
flag could never push ``:latest`` on a manual re-publish, and
``labels-sync.yml``'s ``prune`` could never prune. Both runs went green.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _boolean_inputs(workflow: dict) -> set[str]:
    """Inputs declared ``type: boolean`` on a dispatch or a reusable call."""
    # PyYAML reads the bare ``on:`` key as the boolean True (YAML 1.1).
    triggers = workflow.get("on", workflow.get(True))
    if not isinstance(triggers, dict):
        return set()
    names: set[str] = set()
    for event in ("workflow_dispatch", "workflow_call"):
        inputs = (triggers.get(event) or {}).get("inputs") or {}
        for name, spec in inputs.items():
            if isinstance(spec, dict) and spec.get("type") == "boolean":
                names.add(name)
    return names


def _compared_to_a_string(text: str, name: str) -> list[int]:
    """Line numbers comparing ``inputs.<name>`` with a quoted literal.

    ``github.event.inputs.<name>`` is a string, so comparing that one to
    ``'true'`` is right; the lookbehind leaves it alone.
    """
    ref = rf"(?<![\w.])inputs\.{re.escape(name)}\b"
    pattern = re.compile(rf"{ref}\s*[!=]=\s*'|'\s*[!=]=\s*{ref}")
    return [n for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]


@pytest.mark.parametrize(
    ("workflow", "name"),
    [("publish-docker.yml", "push_latest"), ("labels-sync.yml", "prune")],
)
def test_the_scan_sees_the_boolean_inputs(workflow: str, name: str) -> None:
    """Guards the scan against passing vacuously: PyYAML reads ``on:`` as True."""
    assert name in _boolean_inputs(yaml.safe_load((WORKFLOWS / workflow).read_text()))


def test_boolean_inputs_are_never_compared_to_strings() -> None:
    found: list[str] = []
    for path in _workflow_files():
        text = path.read_text()
        for name in sorted(_boolean_inputs(yaml.safe_load(text))):
            for n in _compared_to_a_string(text, name):
                found.append(f"{path.name}:{n} inputs.{name}")
    assert found == []
