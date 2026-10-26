"""The review's keys are read only by jobs that run main's own code.

The review workflow holds two keys: the Anthropic key the review model uses and
the Caura key that records declined findings. Both live in the ``claude-review``
environment, which only main can deploy to, so they reach a job only when the
workflow and scripts it runs are main's: a member's ``@claude`` comment, the
capture that runs when a merge lands on main (a push to main, so main's copy of
the workflow), or a capture started by hand from main. No job here runs a pull
request's own copy of the workflow, and none checks out a pull request.

What this file cannot see: the environment's branch rule, and whether a
repository or organization secret of the same name is still visible here. Both
are settings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"
REVIEW = WORKFLOWS / "claude_code_review.yml"
KEYS = ("CLAUDE_CODE_REVIEW_API_KEY", "CAURA_AGENTS_KEY")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _triggers(document: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare ``on:`` key as the boolean True (YAML 1.1).
    return document.get("on", document.get(True))


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def test_the_review_never_runs_a_pull_requests_copy_of_itself() -> None:
    triggers = _triggers(_load(REVIEW))
    assert set(triggers) == {"push", "issue_comment", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["issue_comment"] == {"types": ["created"]}


def test_no_workflow_uses_pull_request_target() -> None:
    # GitHub turns this trigger off by default in public repositories, so a job
    # that relies on it would stop running without any change here.
    found = [
        path.name
        for path in _workflow_files()
        if "pull_request_target" in _triggers(_load(path))
    ]
    assert found == []


def test_every_job_that_reads_a_review_key_runs_in_the_review_environment() -> None:
    readers = []
    for path in _workflow_files():
        for name, job in _load(path)["jobs"].items():
            text = json.dumps(job)
            if any(f"secrets.{key}" in text for key in KEYS):
                readers.append((path, name, job))
    assert readers, "no job reads a review key; the scan has gone blind"
    for path, name, job in readers:
        where = f"{path.name}:{name}"
        assert path == REVIEW, f"{where} reads a review key outside the review"
        assert job.get("environment") == "claude-review", where


def test_a_review_starts_only_from_a_members_comment() -> None:
    jobs = _load(REVIEW)["jobs"]
    assert set(jobs) == {"claude-review", "claude-capture"}
    condition = jobs["claude-review"]["if"]
    assert "github.event_name == 'issue_comment'" in condition
    assert "'@claude'" in condition
    assert '["MEMBER", "OWNER"]' in condition


def test_capture_runs_only_for_a_push_to_main_or_by_hand() -> None:
    condition = _load(REVIEW)["jobs"]["claude-capture"]["if"]
    assert condition == (
        "github.event_name == 'push' || github.event_name == 'workflow_dispatch'"
    )


def test_no_review_job_checks_out_a_pull_request() -> None:
    checkouts = [
        step
        for job in _load(REVIEW)["jobs"].values()
        for step in job.get("steps", [])
        if "actions/checkout@" in step.get("uses", "")
    ]
    assert checkouts
    for step in checkouts:
        inputs = step.get("with", {})
        assert "ref" not in inputs, "a checkout must take main, never the PR"
        assert "repository" not in inputs
        assert inputs.get("persist-credentials") is False
