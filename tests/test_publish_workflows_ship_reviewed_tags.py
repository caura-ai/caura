"""Every publish workflow ships only a tag that main already contains.

A tag can name any commit, and a release or a manual run can name any tag, so
"it was tagged" says nothing about whether the code was reviewed. These checks
pin the shape that ties a publish to review instead:

* a run starts only from a tag push, a published release, or a manual run that
  names an existing tag, and every checkout takes ``refs/tags/<tag>``, never a
  branch;
* the job that builds what is published first proves the tagged commit is on
  main (``git merge-base --is-ancestor``), before it builds anything;
* the job holding the publishing credential runs in a GitHub environment and
  runs no repository code beside it where it does not have to, and npm
  authenticates by trusted publishing (OIDC) instead of a stored token.

WHAT A WORKFLOW FILE CANNOT PROVE, so the green is not read as more than it is:
that each environment carries required reviewers and a deployment rule, and
that only the release automation may create release tags. Those are repository
settings, and a run started from a ref whose workflow file was edited would not
execute the checks pinned here; the environment's rules apply regardless.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"
PUBLISH = sorted(WORKFLOWS.glob("publish-*.yml"))
NPM = [path for path in PUBLISH if path.name.startswith("publish-npm-")]
PYPI = [path for path in PUBLISH if path.name.startswith("publish-python-")]
ON_MAIN = "The tag must point at a commit on main"

ENVIRONMENT = {
    "publish-docker.yml": "ghcr",
    "publish-python-collaboration-clients.yml": "pypi-collaboration",
    **{path.name: "npm" for path in NPM},
    **{
        path.name: "pypi"
        for path in PYPI
        if path.name != "publish-python-collaboration-clients.yml"
    },
}


def _ids(path: Path) -> str:
    return path.name


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _triggers(document: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare ``on:`` key as the boolean True (YAML 1.1).
    return document.get("on", document.get(True))


def _is_checkout(step: dict[str, Any]) -> bool:
    return "actions/checkout@" in step.get("uses", "")


def _builds_the_release(job: dict[str, Any]) -> bool:
    """A job whose output is what gets published: an artifact or an image."""
    return any(
        "actions/upload-artifact@" in step.get("uses", "")
        or "docker/build-push-action@" in step.get("uses", "")
        for step in job.get("steps", [])
    )


def _permissions(document: dict[str, Any], job: dict[str, Any]) -> dict[str, str]:
    return job.get("permissions", document.get("permissions")) or {}


def _environment(job: dict[str, Any]) -> str | None:
    environment = job.get("environment")
    if isinstance(environment, dict):
        return environment.get("name")
    return environment


def _step(path: Path, name: str) -> dict[str, Any]:
    for job in _load(path)["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return step
    raise AssertionError(f"{path.name} has no {name!r} step")


def test_the_scan_finds_every_publish_workflow() -> None:
    """Guards every parametrized check below against passing over nothing."""
    assert {path.name for path in PUBLISH} == set(ENVIRONMENT)
    assert len(NPM) == 4
    assert len(PYPI) == 5


@pytest.mark.parametrize("path", PUBLISH, ids=_ids)
def test_a_publish_starts_only_from_a_tag_a_release_or_a_named_tag(
    path: Path,
) -> None:
    triggers = _triggers(_load(path))
    assert set(triggers) <= {"push", "release", "workflow_dispatch"}
    if "push" in triggers:
        assert set(triggers["push"]) == {"tags"}, "a branch push must not publish"
    if "release" in triggers:
        assert triggers["release"]["types"] == ["published"]
    inputs = (triggers.get("workflow_dispatch") or {}).get("inputs") or {}
    assert "tag" in inputs, "a manual run must name the existing tag it publishes"
    assert inputs["tag"]["required"] is True
    assert inputs["tag"]["type"] == "string"


@pytest.mark.parametrize("path", PUBLISH, ids=_ids)
def test_every_checkout_takes_the_tag_and_never_a_branch(path: Path) -> None:
    checkouts = [
        step
        for job in _load(path)["jobs"].values()
        for step in job.get("steps", [])
        if _is_checkout(step)
    ]
    assert checkouts
    for step in checkouts:
        assert str((step.get("with") or {}).get("ref", "")).startswith("refs/tags/")


@pytest.mark.parametrize("path", PUBLISH, ids=_ids)
def test_the_build_proves_its_tag_is_on_main_before_building(path: Path) -> None:
    jobs = [job for job in _load(path)["jobs"].values() if _builds_the_release(job)]
    assert jobs, f"{path.name}: no job builds what it publishes"
    for job in jobs:
        steps = job["steps"]
        checkout = next(i for i, step in enumerate(steps) if _is_checkout(step))
        # The whole history, so main is there to compare against.
        assert steps[checkout]["with"].get("fetch-depth") == 0
        assert steps[checkout + 1].get("name") == ON_MAIN


def test_every_publish_workflow_runs_the_same_ancestry_check() -> None:
    """One script, so the behaviour test below covers every copy of it."""
    scripts = {path.name: _step(path, ON_MAIN)["run"] for path in PUBLISH}
    assert len(set(scripts.values())) == 1, scripts


def _git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize(
    ("head", "has_main", "accepted"),
    [
        ("main tip", True, True),
        ("older main commit", True, True),
        ("commit main never merged", True, False),
        ("main tip", False, False),
    ],
)
def test_the_ancestry_check_admits_only_commits_on_main(
    tmp_path: Path, head: str, has_main: bool, accepted: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    older = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "unreviewed", older)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "never merged")
    side = _git(repo, "rev-parse", "HEAD")
    # actions/checkout with fetch-depth 0 leaves main as a remote-tracking ref.
    if has_main:
        _git(repo, "update-ref", "refs/remotes/origin/main", tip)
    target = {"main tip": tip, "older main commit": older}.get(head, side)
    _git(repo, "checkout", "-q", "--detach", target)
    # A job's defaults may run it from a package directory, not the root.
    (repo / "clients").mkdir()

    result = _run(
        _step(PUBLISH[0], ON_MAIN)["run"],
        {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"},
        repo / "clients",
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    if not accepted:
        assert "::error::" in result.stdout


@pytest.mark.parametrize("path", PUBLISH, ids=_ids)
def test_the_publishing_credential_is_held_only_in_an_environment(
    path: Path,
) -> None:
    document = _load(path)
    holders = [
        job
        for job in document["jobs"].values()
        if {"id-token", "packages"}
        & {
            scope
            for scope, level in _permissions(document, job).items()
            if level == "write"
        }
    ]
    assert holders, f"{path.name}: no job holds a publishing credential"
    for job in holders:
        assert _environment(job) == ENVIRONMENT[path.name]


@pytest.mark.parametrize("path", [*NPM, *PYPI], ids=_ids)
def test_the_token_minting_job_runs_no_repository_code(path: Path) -> None:
    """Anything a build installs runs where it is built, never beside the
    credential: the job that can mint a registry token checks nothing out."""
    document = _load(path)
    for job in document["jobs"].values():
        if _permissions(document, job).get("id-token") == "write":
            assert not any(_is_checkout(step) for step in job["steps"])


@pytest.mark.parametrize("path", [*NPM, *PYPI], ids=_ids)
def test_the_tag_must_agree_with_the_manifest_on_every_trigger(path: Path) -> None:
    guards = [
        step
        for job in _load(path)["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("name", "")).startswith("The tag must agree with")
        or step.get("name") == "Every package must carry the tag's version"
    ]
    assert guards
    for step in guards:
        assert "if" not in step, "a manual run must be checked against its tag too"


def test_no_workflow_reads_a_stored_npm_token() -> None:
    found = [
        path.name
        for path in sorted(WORKFLOWS.glob("*.yml"))
        if "NPM_TOKEN" in path.read_text() or "NODE_AUTH_TOKEN" in path.read_text()
    ]
    assert found == []


@pytest.mark.parametrize("path", NPM, ids=_ids)
def test_npm_publishes_a_built_tarball_by_trusted_publishing(path: Path) -> None:
    document = _load(path)
    publish = document["jobs"]["publish"]
    assert _permissions(document, publish) == {"id-token": "write"}
    setup = next(
        step
        for step in publish["steps"]
        if "actions/setup-node@" in step.get("uses", "")
    )
    # Trusted publishing needs Node 22.14+ and npm 11.5.1+.
    assert int(str(setup["with"]["node-version"])) >= 24
    command = _step(path, "Publish")["run"]
    assert "npm publish" in command
    # npm reads `package/x.tgz` as a GitHub shorthand; only `./` makes it a file.
    assert "(./package/*.tgz)" in command, "publish the tarball the build job packed"
    assert "--ignore-scripts" in command


def _run(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("npm_version", "oidc", "accepted"),
    [
        ("11.5.1", True, True),
        ("11.19.1", True, True),
        ("12.0.0", True, True),
        ("11.5.0", True, False),
        ("10.9.2", True, False),
        ("11.19.1", False, False),
    ],
)
def test_npm_publish_fails_early_without_what_trusted_publishing_needs(
    tmp_path: Path, npm_version: str, oidc: bool, accepted: bool
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "npm"
    fake.write_text(f"#!/bin/sh\necho {npm_version}\n")
    fake.chmod(0o755)
    env = {"PATH": f"{binary}:{os.environ['PATH']}"}
    if oidc:
        env["ACTIONS_ID_TOKEN_REQUEST_URL"] = "https://token.example.invalid"
        env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = "synthetic-request-token"
    for path in NPM:
        script = _step(path, "The publish credential must be obtainable")["run"]
        result = _run(script, env, tmp_path)
        assert (result.returncode == 0) is accepted, (
            path.name + result.stdout + result.stderr
        )
