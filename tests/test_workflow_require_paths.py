"""Every repo path a workflow ``require()``s must actually exist.

``verify-npm-credential.yml`` read ``./clients/npm-caura/package.json`` to learn
the name of the second package a release publishes. #943 moved that directory,
#1099 moved it again and #1244 deleted it outright. Nothing failed at any point,
because the workflow is ``workflow_dispatch``-only: the reference was broken from
2026-08-24 and surfaced on 2026-09-06, when someone ran it to answer a question
and got ``Cannot find module`` instead of an answer.

The shape is worth naming. Reading the name out of the manifest instead of
hard-coding it protects against the package being RENAMED — but it silently
introduces a dependency on the manifest's PATH, and nothing checked that. The
comment on that line claimed the arrangement "cannot drift from what the publish
workflows actually ship", which was true of one kind of drift and false of the
kind that happened. A ``workflow_dispatch``-only file is the worst place for it,
since the gap between breaking and finding out is bounded only by when someone
next needs the thing.

WHAT THIS DOES NOT DO, stated so the green is not read as more than it is.
``require()`` is one of several ways a workflow names a repo path — the others
are ``working-directory:``, ``cd`` in a ``run:`` block, script arguments and
``on: paths:`` filters. None of those are checked here, and a stale ``paths:``
filter fails just as silently as this did. Covering them is worth doing and is
deliberately not done here.

Resolution is also approximate for one shape. A ``defaults.run.working-directory``
set at job level is the base for every run step in that job, so it REPLACES the
repository root rather than adding to it, and that is what ``_bases`` implements.
A workflow whose steps declare several different working directories gets all of
them as candidates, so a path valid under one step's directory would be accepted
from another. No workflow currently mixes ``require()`` with per-step working
directories, so nothing relies on that today.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"

# Only repo-relative requires. A bare `require('fs')` or `require('semver')` is
# resolved from node_modules and has nothing to do with the tree.
_REQUIRE_RE = re.compile(r"""require\(\s*['"](\./[^'"]+)['"]\s*\)""")

_WORKDIR_RE = re.compile(r"^\s*working-directory:\s*(\S+)\s*$", re.MULTILINE)


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _bases(text: str) -> list[Path]:
    """Directories a relative path in this workflow's ``run:`` steps resolves against.

    A declared ``working-directory`` REPLACES the repository root; it does not
    add to it. Getting that wrong is what made the first draft of this test
    almost worthless: a ``package.json`` exists at the repository root, so with
    the root always among the candidates every ``require('./package.json')``
    resolved there regardless of which directory its job actually ran in. Nine
    of the eleven requires in this repo could not have failed, and the only two
    genuinely under test were the two in the file this test was written for.
    """
    declared = _WORKDIR_RE.findall(text)
    return [REPO / d for d in declared] if declared else [REPO]


def _requires() -> list[tuple[Path, list[Path], list[str]]]:
    """Each workflow that has repo-relative requires, with its bases and paths."""
    found = []
    for workflow in _workflow_files():
        text = workflow.read_text()
        # Deduped: a workflow reading the same manifest from five steps is one
        # broken path, and five identical failure lines obscure the other files.
        paths = list(
            dict.fromkeys(raw.removeprefix("./") for raw in _REQUIRE_RE.findall(text))
        )
        if paths:
            found.append((workflow, _bases(text), paths))
    return found


def test_every_workflow_require_path_exists() -> None:
    unresolved = []
    for workflow, bases, paths in _requires():
        shown = ", ".join(
            "<repo root>" if base == REPO else str(base.relative_to(REPO))
            for base in bases
        )
        for rel in paths:
            if not any((base / rel).is_file() for base in bases):
                unresolved.append(
                    f"{workflow.name}: require('./{rel}') — not found under {shown}"
                )

    assert unresolved == [], (
        "workflow requires a path that does not exist:\n" + "\n".join(unresolved)
    )


def test_the_scan_is_not_vacuous() -> None:
    """A regex that matches nothing passes the test above for the wrong reason.

    If the workflows are ever reorganised so that no ``require('./…')`` remains,
    this fails and asks for a decision — delete both tests deliberately, or fix
    the pattern — rather than leaving a green check that verifies nothing.
    """
    assert _workflow_files(), f"no workflow files found under {WORKFLOWS}"
    assert _requires(), (
        "no repo-relative require() found in any workflow — either the pattern "
        "stopped matching or the workflows changed shape; both need a human"
    )
