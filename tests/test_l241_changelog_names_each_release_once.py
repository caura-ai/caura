"""L-241: each changelog section is one release, compared from an earlier one.

release-please-config.json pinned the collaboration clients with ``release-as:
"0.3.0"`` for their first release, and the pin outlived it. Every release PR
after ``collaboration-clients-v0.3.0`` re-proposed 0.3.0: it prepended another
0.3.0 section, compared from 0.3.0 to itself, crediting 0.3.0 with fixes merged
after it, and left those fixes no version to ship in.

A ``release-as`` equal to the manifest's version is not by itself the fault: the
release PR that delivers the pinned version looks the same. So the check reads
what a stale pin writes, a version heading that repeats or compares with itself,
and fails the release PR that would write it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
SECTION = re.compile(r"^#{2,3} \[(\d+\.\d+\.\d+)\]\(([^)]*)\)", re.MULTILINE)
COMPARE = re.compile(r"/compare/(\S+)\.\.\.(\S+)$")


def _changelogs() -> list[Path]:
    config = json.loads((REPO / "release-please-config.json").read_text())
    default = config.get("changelog-path", "CHANGELOG.md")
    return [
        REPO / root / package.get("changelog-path", default)
        for root, package in config["packages"].items()
    ]


@pytest.mark.parametrize(
    "changelog", _changelogs(), ids=lambda path: str(path.relative_to(REPO))
)
def test_each_release_has_one_section_compared_from_an_earlier_one(
    changelog: Path,
) -> None:
    sections = SECTION.findall(changelog.read_text())
    assert sections
    versions = [version for version, _ in sections]
    assert sorted({v for v in versions if versions.count(v) > 1}) == []
    self_compared = [
        url for _, url in sections if (m := COMPARE.search(url)) and m[1] == m[2]
    ]
    assert self_compared == []
