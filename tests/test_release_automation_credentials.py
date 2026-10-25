"""Release automation holds a credential for this repository and nothing else.

release-please needs a GitHub App token: a release PR opened with the default
token never starts the workflows its required checks come from. That makes the
App's private key the strongest secret a workflow here can read, so it belongs
to an App installed on this repository alone, stored in an environment that only
main can deploy to, and minted into a token scoped to this repository with the
three permissions release-please uses. No workflow here holds a credential for
another repository.

What this file cannot see: the App's installation and permissions, and the
environment's branch rule. Both are repository settings.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"
RELEASE_PLEASE = WORKFLOWS / "release-please.yml"
_OTHER_REPO = re.compile(r"repos/caura-ai/([\w.-]+)")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _release_job() -> dict[str, Any]:
    return _load(RELEASE_PLEASE)["jobs"]["release-please"]


def test_release_please_runs_in_a_main_only_environment() -> None:
    document = _load(RELEASE_PLEASE)
    triggers = document.get("on", document.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    # The App token does the writing; the job's own token only needs to read.
    assert document["permissions"] == {"contents": "read"}
    job = _release_job()
    assert job["environment"] == "release-please"
    assert job["steps"][0]["name"] == "Require this repository's main branch"
    assert not any("checkout@" in step.get("uses", "") for step in job["steps"])


def test_the_release_token_reaches_only_this_repository() -> None:
    steps = _release_job()["steps"]
    mint = next(step for step in steps if step.get("id") == "app-token")
    assert steps.index(mint) == 1
    assert mint["with"] == {
        "app-id": "${{ secrets.RELEASE_APP_ID }}",
        "private-key": "${{ secrets.RELEASE_APP_PRIVATE_KEY }}",
        "owner": "caura-ai",
        "repositories": "caura",
        "permission-contents": "write",
        "permission-pull-requests": "write",
        "permission-issues": "write",
    }
    assert "skip-token-revoke" not in mint["with"]
    release = next(step for step in steps if step.get("id") == "release")
    assert release["with"]["token"] == "${{ steps.app-token.outputs.token }}"


@pytest.mark.parametrize(
    ("override", "accepted"),
    [
        ({}, True),
        ({"GITHUB_EVENT_NAME": "workflow_dispatch"}, True),
        ({"GITHUB_REF": "refs/heads/feature"}, False),
        ({"GITHUB_REF": "refs/tags/main"}, False),
        ({"GITHUB_EVENT_NAME": "pull_request"}, False),
        ({"GITHUB_REPOSITORY": "someone-else/caura"}, False),
        ({"REPOSITORY_ID": "1"}, False),
    ],
)
def test_the_source_guard_admits_only_this_repositorys_main(
    override: dict[str, str], accepted: bool
) -> None:
    guard = _release_job()["steps"][0]
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_REPOSITORY": "caura-ai/caura",
        "REPOSITORY_ID": "1222639711",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push",
    }
    env.update(override)
    assert guard["env"] == {"REPOSITORY_ID": "${{ github.repository_id }}"}
    result = subprocess.run(["bash", "-c", guard["run"]], env=env, check=False)
    assert (result.returncode == 0) is accepted


def test_no_workflow_reads_the_shared_deploy_app_key() -> None:
    found = [
        path.name for path in _workflow_files() if "DEPLOY_APP" in path.read_text()
    ]
    assert found == []


def test_every_app_token_is_minted_for_this_repository_from_an_environment() -> None:
    mints = []
    for path in _workflow_files():
        for name, job in _load(path)["jobs"].items():
            for step in job.get("steps", []):
                if "create-github-app-token@" in step.get("uses", ""):
                    mints.append((path.name, name, job, step["with"]))
    assert mints, "no workflow mints an App token; the scan has gone blind"
    for workflow, name, job, inputs in mints:
        where = f"{workflow}:{name}"
        assert job.get("environment"), (
            f"{where} reads an App key outside an environment"
        )
        assert inputs.get("owner") == "caura-ai", where
        assert inputs.get("repositories") == "caura", where


def test_no_workflow_calls_the_api_of_another_repository() -> None:
    found = {
        f"{path.name}: {match}"
        for path in _workflow_files()
        for match in _OTHER_REPO.findall(path.read_text())
        if match != "caura"
    }
    assert found == set()
