"""Invariants for the client metapackages under ``clients/``.

Three distributions on PyPI (``caura``, ``caura-sdk``, ``caura-client``) and
three on npm (``@caura/client``, ``@caura/sdk``, and the implementation they
re-export) install the same client. The aliases exist because install instructions in the
wild — much of it AI-generated — point at names we did not choose, and a name
that 404s is either a dead instruction or, on PyPI where anyone may claim an
unused name, an open door.

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
import tomllib
from pathlib import Path

import pytest

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
    "typescript": "@caura/memclaw-client",  # legacy-name-floor: the published implementation name
    "npm-client": "@caura/client",
    "npm-sdk": "@caura/sdk",
}

# Each alias re-exports the package it declares as a dependency. They differ on
# purpose: ``@caura/client`` wraps the implementation, and ``@caura/sdk`` wraps
# ``@caura/client`` rather than reaching past it — so a future rename of the
# implementation is a one-package change, not a two-package change.
NPM_REEXPORT_TARGET = {
    "npm-client": NPM_DISTS["typescript"],
    "npm-sdk": "@caura/client",
}

# Metapackages only — the implementation is excluded, since it is the thing
# they all depend on.
PY_METAPACKAGES = ("caura-meta", "caura-sdk-meta")
NPM_METAPACKAGES = ("npm-client", "npm-sdk")


def _pyproject(directory: str) -> dict:
    return tomllib.loads((CLIENTS / directory / "pyproject.toml").read_text())


def _package_json(directory: str) -> dict:
    return json.loads((CLIENTS / directory / "package.json").read_text())


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
