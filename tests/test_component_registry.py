"""Drift gate: docs/component-registry.yaml ⇄ core-operations cron ticks.

The registry is the checked-in inventory of shipped moving parts; the
scheduler registrations in ``core-operations/src/core_operations/app.py``
are the runtime truth for cron ticks. This test fails when either side
drifts — a ``scheduler.register(...)`` call with no registry entry (a
shipped component absent from docs), or a registry entry with no
registration (docs describing a component that no longer ships).

The same gate covers ``event_topic_families`` against the str-enums in
``common/events/topics.py``. That section was added as prose — a name and a
hand-counted number — and prose is how it went stale: the 2026-08-14 audit
found it still claiming a ``Pipeline`` family whose topics had been deleted,
alongside a ``Memory`` count that no longer matched. Nothing failed, because
nothing was checking. A count a human maintains by hand is a comment wearing a
number's clothes; this makes it a comparison.

Both sections are parsed with line-based regexes rather than a YAML library so
the gate carries no extra dependency — keep each entry as a ``- name: <name>``
list item (see the format note in the registry file itself).
"""

from __future__ import annotations

import ast
import enum
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


def _registry_topic_families() -> dict[str, int]:
    """``name`` -> declared topic count from the registry's own section."""
    text = REGISTRY_PATH.read_text(encoding="utf-8")
    match = re.search(r"^event_topic_families:\n(.*?)(?=^\S|\Z)", text, re.M | re.S)
    assert match, "component-registry.yaml has no event_topic_families: section"
    entries = re.findall(
        r"^\s*-\s*name:\s*(\S+)\s*\n\s*topics:\s*(\d+)", match.group(1), re.M
    )
    return {name: int(count) for name, count in entries}


def _declared_topic_families() -> dict[str, int]:
    """``name`` -> member count for every str-enum on ``Topics``.

    Walks the facade the same way ``topics.all_topics`` does, so a family added
    there is covered here without anyone remembering to.
    """
    from common.events.topics import Topics

    return {
        name: len(list(attr))
        for name, attr in vars(Topics).items()
        if isinstance(attr, type) and issubclass(attr, enum.StrEnum)
    }


def test_topic_family_extractors_find_something() -> None:
    # Guards the guards, like the cron equivalent above: if either shape
    # changes so its regex or walk stops matching, the two comparisons below
    # would pass vacuously on empty dicts.
    assert _registry_topic_families(), "no event_topic_families parsed from registry"
    assert _declared_topic_families(), "no str-enum families found on Topics"


def test_every_declared_topic_family_matches_the_registry() -> None:
    declared = _declared_topic_families()
    listed = _registry_topic_families()

    missing = sorted(set(declared) - set(listed))
    assert not missing, (
        f"topic families declared in common/events/topics.py but absent from "
        f"docs/component-registry.yaml: {missing} — add them to the registry's "
        f"event_topic_families section"
    )

    wrong = {
        name: (listed[name], declared[name])
        for name in declared
        if listed[name] != declared[name]
    }
    assert not wrong, (
        f"topic counts in docs/component-registry.yaml disagree with the enums "
        f"in common/events/topics.py — {{family: (registry, actual)}}: {wrong}. "
        f"A count nobody checks is the failure this gate exists for; fix the "
        f"registry rather than the test."
    )


def test_every_registry_topic_family_is_declared() -> None:
    stale = sorted(set(_registry_topic_families()) - set(_declared_topic_families()))
    assert not stale, (
        f"topic families listed in docs/component-registry.yaml but declared "
        f"nowhere in common/events/topics.py: {stale} — remove them from the "
        f"registry. This is the exact shape the 2026-08-14 audit found: a "
        f"deleted family still documented as shipping."
    )
