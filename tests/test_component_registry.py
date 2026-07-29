"""Drift gate: docs/component-registry.yaml ⇄ core-operations cron ticks.

The registry is the checked-in inventory of shipped moving parts; the
scheduler registrations in ``core-operations/src/core_operations/app.py``
are the runtime truth for cron ticks. This test fails when either side
drifts — a ``scheduler.register(...)`` call with no registry entry (a
shipped component absent from docs), or a registry entry with no
registration (docs describing a component that no longer ships).

The registry's ``cron_ticks`` section is parsed with a line-based regex
rather than a YAML library so the gate carries no extra dependency —
keep each tick as a ``- name: <tick>`` list item (see the format note in
the registry file itself).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "docs" / "component-registry.yaml"
APP_PATH = REPO_ROOT / "core-operations" / "src" / "core_operations" / "app.py"


def _registered_ticks() -> set[str]:
    """First-arg string literals of every ``scheduler.register(...)`` call."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    ticks: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "scheduler"
        ):
            assert node.args and isinstance(node.args[0], ast.Constant), (
                "scheduler.register call whose first arg is not a string "
                "literal — the drift gate can't see it; use a literal name"
            )
            ticks.add(node.args[0].value)
    return ticks


def _registry_ticks() -> set[str]:
    """``- name:`` entries inside the registry's ``cron_ticks:`` section."""
    text = REGISTRY_PATH.read_text(encoding="utf-8")
    match = re.search(r"^cron_ticks:\n(.*?)(?=^\S|\Z)", text, re.M | re.S)
    assert match, "component-registry.yaml has no cron_ticks: section"
    return set(re.findall(r"^\s*-\s*name:\s*(\S+)", match.group(1), re.M))


def test_scheduler_registers_at_least_one_tick() -> None:
    # Guards the extractor itself: if app.py is refactored so the AST walk
    # stops matching (e.g. scheduler is renamed), both directional checks
    # below would trivially pass on an empty set.
    assert _registered_ticks(), "no scheduler.register calls found in app.py"


def test_every_registered_tick_is_in_the_registry() -> None:
    missing = _registered_ticks() - _registry_ticks()
    assert not missing, (
        f"cron ticks registered in core_operations.app but absent from "
        f"docs/component-registry.yaml: {sorted(missing)} — add them to the "
        f"registry's cron_ticks section"
    )


def test_every_registry_tick_is_registered() -> None:
    stale = _registry_ticks() - _registered_ticks()
    assert not stale, (
        f"cron ticks listed in docs/component-registry.yaml but not "
        f"registered in core_operations.app: {sorted(stale)} — remove them "
        f"from the registry or restore the registration"
    )
