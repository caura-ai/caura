"""L-90: the contributor docs list the files a release rewrites, and only those.

CONTRIBUTING said merging the release PR bumps ``VERSION`` and every
``pyproject.toml``. There is no ``VERSION`` file, release-please rewrites
three of the services' pyprojects and their ``uv.lock`` entries, and the client
packages are bumped by hand. RELEASING.md had the list but missed the locks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
VERSION_FILE = re.compile(r"`([\w./-]+\.(?:json|lock|toml|ts))`")


def _rewritten() -> set[str]:
    """Every file release-please-config.json has a release rewrite."""
    config = REPO / "release-please-config.json"
    packages = json.loads(config.read_text())["packages"]
    files = set()
    for root, package in packages.items():
        prefix = "" if root == "." else f"{root}/"
        files |= {prefix + extra["path"] for extra in package.get("extra-files", [])}
        if package.get("release-type") == "node":
            files.add(f"{prefix}package.json")
    return files


def _section(doc: str, heading: str) -> str:
    text = (REPO / doc).read_text()
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def test_the_release_config_and_docs_are_found() -> None:
    assert "core-api/pyproject.toml" in _rewritten()
    assert _section("CONTRIBUTING.md", "## Release process")


def test_releasing_lists_every_file_a_release_rewrites() -> None:
    section = _section("RELEASING.md", "## Version files release-please rewrites")
    assert set(VERSION_FILE.findall(section)) == _rewritten()


def test_contributing_points_to_that_list_and_names_no_other_file() -> None:
    section = _section("CONTRIBUTING.md", "## Release process")
    assert "RELEASING.md" in section
    assert "`VERSION`" not in section
    assert set(VERSION_FILE.findall(section)) <= _rewritten()
