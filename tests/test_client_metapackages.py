"""Invariants for the client metapackages under ``clients/``.

Three distributions on PyPI (``caura``, ``caura-sdk``, ``caura-client``) and
three on npm (``@caura/client``, ``@caura/sdk``, and the legacy alias they
preserve) install the same client. The aliases exist because install
instructions in the wild — much of it AI-generated — point at names we did not
choose, and a name that 404s is either a dead instruction or, on PyPI where
anyone may claim an unused name, an open door.

That shape has two failure modes with no natural symptom, and this module pins
both:

* **Two distributions shipping the same import package.** ``caura`` ships
  ``src/caura/``. If ``caura-sdk`` shipped ``src/caura/`` too, both would
  install to the same path and ``pip install caura caura-sdk`` would leave
  whichever landed last — no warning from pip, no error at import.

* **A package with no way to publish it.** ``caura`` 1.0.0 was published by
  hand and had no workflow for its first week, so the one artifact whose whole
  job is making ``pip install caura`` resolve was also the one nobody could
  ship a fix to. Every metapackage here must have a workflow that builds *its*
  directory.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENTS = REPO_ROOT / "clients"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Directory → the distribution it publishes. The implementation package is
# listed alongside the metapackages because the uniqueness check below is only
# meaningful across the whole set.
PY_DISTS = {
    "python": "caura-client",
    "caura-meta": "caura",
    "caura-sdk-meta": "caura-sdk",
}

NPM_DISTS = {
    "typescript": "@caura/client",
    "npm-legacy-client": "@caura/memclaw-client",  # legacy-name-ok: published alias
    "npm-sdk": "@caura/sdk",
}

# Each alias re-exports the canonical implementation package it declares as a
# dependency. Neither alias reaches through another alias, so a future client
# change remains a one-package change.
NPM_REEXPORT_TARGET = {
    "npm-legacy-client": NPM_DISTS["typescript"],
    "npm-sdk": "@caura/client",
}

# Metapackages only — the implementation is excluded, since it is the thing
# they all depend on.
PY_METAPACKAGES = ("caura-meta", "caura-sdk-meta")
NPM_METAPACKAGES = ("npm-legacy-client", "npm-sdk")
LEGACY_NPM_CLIENT_TAG = (
    "memclaw-client-ts-v1.0.2"  # legacy-name-ok: guard compatibility case
)


def _pyproject(directory: str) -> dict:
    return tomllib.loads((CLIENTS / directory / "pyproject.toml").read_text())


def _package_json(directory: str) -> dict:
    return json.loads((CLIENTS / directory / "package.json").read_text())


def _workflow_step_script(workflow: str, step_name: str) -> str:
    """Return the literal shell body GitHub Actions runs for one workflow step."""
    lines = (WORKFLOWS / workflow).read_text().splitlines(keepends=True)
    marker = f"      - name: {step_name}\n"
    assert marker in lines, f"workflow has no {step_name!r} step"
    start = lines.index(marker)
    step_end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("      - ")),
        len(lines),
    )
    run = next(
        (i for i in range(start + 1, step_end) if lines[i] == "        run: |\n"),
        None,
    )
    assert run is not None, f"workflow step {step_name!r} has no literal run block"
    end = next(
        (
            i
            for i in range(run + 1, step_end)
            if lines[i].strip() and not lines[i].startswith("          ")
        ),
        step_end,
    )
    return "".join(line[10:] for line in lines[run + 1 : end])


def _run_workflow_step(
    workflow: str, step_name: str, working_directory: Path, tag: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            _workflow_step_script(workflow, step_name),
        ],
        cwd=working_directory,
        env={"GITHUB_REF_NAME": tag, "PATH": os.environ["PATH"]},
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
@pytest.mark.parametrize("directory,expected_name", sorted(PY_DISTS.items()))
def test_python_distribution_names_are_stable(
    directory: str, expected_name: str
) -> None:
    """A renamed distribution silently stops publishing to the name people install."""
    assert _pyproject(directory)["project"]["name"] == expected_name


@pytest.mark.unit
@pytest.mark.parametrize("directory", PY_METAPACKAGES)
def test_python_metapackages_depend_on_the_real_client(directory: str) -> None:
    """A metapackage that installs nothing is a stub — the thing we said these are not."""
    deps = _pyproject(directory)["project"]["dependencies"]
    assert any(d.startswith("caura-client") for d in deps), deps


@pytest.mark.unit
def test_python_import_packages_do_not_collide() -> None:
    """Two distributions shipping one import package overwrite each other on install."""
    owners: dict[str, str] = {}
    for directory, dist in sorted(PY_DISTS.items()):
        for module in sorted(
            p.name for p in (CLIENTS / directory / "src").iterdir() if p.is_dir()
        ):
            assert module not in owners, (
                f"import package {module!r} is shipped by both {owners[module]!r} "
                f"and {dist!r}; installing both leaves whichever pip wrote last"
            )
            owners[module] = dist


@pytest.mark.unit
@pytest.mark.parametrize("directory,expected_name", sorted(NPM_DISTS.items()))
def test_npm_package_names_are_stable(directory: str, expected_name: str) -> None:
    assert _package_json(directory)["name"] == expected_name


@pytest.mark.unit
@pytest.mark.parametrize("directory", NPM_METAPACKAGES)
def test_npm_metapackages_reexport_what_they_depend_on(directory: str) -> None:
    """The alias must re-export, not just depend — `import { Caura }` has to work.

    Declaring the dependency and re-exporting a *different* package installs
    cleanly and then fails to resolve at import time, so the two are pinned
    together rather than separately.
    """
    pkg = _package_json(directory)
    target = NPM_REEXPORT_TARGET[directory]
    assert target in pkg["dependencies"], pkg["dependencies"]
    for entry in ("index.js", "index.d.ts"):
        assert f"export * from '{target}';" in (CLIENTS / directory / entry).read_text()
        # A file missing from `files` is absent from the published tarball —
        # the package installs and then fails to resolve its own main.
        assert entry in pkg["files"]


@pytest.mark.unit
def test_npm_alias_dependency_direction_avoids_historical_cycle() -> None:
    """The old canonical 1.0.0 wrapper must not satisfy the forward alias."""
    legacy_alias = _package_json("npm-legacy-client")
    assert legacy_alias["dependencies"]["@caura/client"] == "^1.0.1"

    canonical = _package_json("typescript")
    assert NPM_DISTS["npm-legacy-client"] not in canonical.get("dependencies", {})


@pytest.mark.unit
def test_npm_client_tag_package_agreement_precedes_build_and_skips_dispatch() -> None:
    workflow = (WORKFLOWS / "publish-npm-client.yml").read_text()
    guard = "      - name: The tag must agree with package.json\n"
    assert workflow.index(guard) < workflow.index("      - name: Install + build\n")
    guard_header = workflow[
        workflow.index(guard) : workflow.index(
            "        run: |\n", workflow.index(guard)
        )
    ]
    assert "if: startsWith(github.ref, 'refs/tags/')" in guard_header


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tag", "directory", "should_pass", "message"),
    [
        ("caura-client-ts-v1.0.1", "typescript", True, "both Caura-spelled"),
        ("caura-npm-v1.0.1", "typescript", True, "both Caura-spelled"),
        (
            "caura-client-ts-v1.0.1",
            "npm-legacy-client",
            False,
            "is Caura-spelled but package.json publishes",
        ),
        (
            "unrelated-v1.0.1",
            "typescript",
            False,
            "does not use a supported client prefix",
        ),
        (
            "caura-client-ts-v9.9.9",
            "typescript",
            False,
            "says 9.9.9 but package.json says 1.0.1",
        ),
    ],
)
def test_npm_client_tag_package_agreement_behavior(
    tag: str,
    directory: str,
    should_pass: bool,
    message: str,
) -> None:
    result = _run_workflow_step(
        "publish-npm-client.yml",
        "The tag must agree with package.json",
        CLIENTS / directory,
        tag,
    )
    assert (result.returncode == 0) is should_pass, result.stdout + result.stderr
    assert message in result.stdout


@pytest.mark.unit
def test_npm_client_tag_package_agreement_rejects_an_unknown_brand(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "@other/client"}))
    result = _run_workflow_step(
        "publish-npm-client.yml",
        "The tag must agree with package.json",
        tmp_path,
        "caura-client-ts-v1.0.1",
    )
    assert result.returncode != 0
    assert (
        "package.json publishes @other/client, whose brand is not recognized"
        in result.stdout
    )


@pytest.mark.unit
def test_legacy_npm_tag_still_publishes_the_legacy_alias() -> None:
    workflow = (WORKFLOWS / "publish-npm-legacy-client.yml").read_text()
    tag_pattern = LEGACY_NPM_CLIENT_TAG.removesuffix("1.0.2") + "*"
    assert f'- "{tag_pattern}"' in workflow
    assert "working-directory: clients/npm-legacy-client" in workflow

    result = _run_workflow_step(
        "publish-npm-legacy-client.yml",
        "The tag must agree with package.json",
        CLIENTS / "npm-legacy-client",
        LEGACY_NPM_CLIENT_TAG,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    published_alias = "@caura/memclaw-client@1.0.2"  # legacy-name-ok: alias path
    assert f"publishing {published_alias}" in result.stdout


@pytest.mark.unit
@pytest.mark.parametrize(
    "directory", sorted({*PY_DISTS, *NPM_DISTS} - {"typescript", "python"})
)
def test_every_metapackage_has_a_publish_workflow(directory: str) -> None:
    """`caura` shipped for a week with no workflow. Nothing else does that."""
    needle = f"clients/{directory}"
    matches = [
        wf.name
        for wf in sorted(WORKFLOWS.glob("publish-*.yml"))
        if needle in wf.read_text()
    ]
    assert matches, f"no publish workflow builds {needle}"
