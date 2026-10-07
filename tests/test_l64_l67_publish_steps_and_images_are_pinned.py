"""L-64, L-67: the publish steps and the service images run pinned code.

A tag or a branch can be moved to other code after review; a commit or a digest
cannot. The PyPI publish step, in a job that can mint the PyPI publishing token,
ran ``pypa/gh-action-pypi-publish@release/v1``, a branch, while the same files
pinned every other action to a commit. One PyPI workflow also took ``checkout``
and ``setup-python`` by tag, and the npm publish ran ``npm install`` where CI
runs ``npm ci``, so it could build from versions CI never tested. The four
service images were built ``FROM python:3.12-slim`` with ``uv`` copied from
``ghcr.io/astral-sh/uv:0.9.18``, both by tag, so a retagged upstream image
changed what we shipped with no diff here. Dependabot's github-actions and
docker updates keep the pins fresh.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"
DOCKERFILES = sorted(REPO.glob("*/Dockerfile"))
# The four service images, whose directories Dependabot's docker updates watch.
SERVICES = ("core-api", "core-storage-api", "core-worker", "core-operations")

# ``- uses: owner/repo@ref # comment``: group 1 is the ref, group 2 the comment.
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?([^\s'\"#]+)['\"]?(?:\s+#\s*(\S+))?")
_COMMIT = re.compile(r"[^@\s]+@[0-9a-f]{40}")
_VERSION = re.compile(r"v?\d+(?:\.\d+)*")
_NPM = re.compile(r"(?<![\w-])npm\s+(ci|install|i|add)(?![\w-])")
# A global tool has no lockfile, so it names an exact version.
_GLOBAL_EXACT = re.compile(r"\s(?:-g|--global)\s+(?:@[\w.-]+/)?[\w.-]+@\d+\.\d+\.\d+\b")
_FROM = re.compile(r"FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.I)
_COPY_FROM = re.compile(r"(?:COPY|ADD)\s.*?--from=(\S+)", re.I)
_DIGEST = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _uses(path: Path) -> list[tuple[int, str, str | None]]:
    """``(line, ref, trailing comment)`` for each ``uses:`` in a workflow."""
    lines = enumerate(path.read_text().splitlines(), 1)
    return [(n, m[1], m[2]) for n, line in lines if (m := _USES.match(line))]


def _yaml_uses(node: object) -> list[str]:
    """Every ``uses:`` value in a parsed workflow, however it is laid out."""
    found: list[str] = []
    if isinstance(node, dict):
        found += [v for k, v in node.items() if k == "uses" and isinstance(v, str)]
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            found += _yaml_uses(child)
    return found


def _pinned(ref: str, comment: str | None) -> bool:
    """``owner/repo@<40-hex commit> # vX.Y.Z``, or an action in this repo."""
    if ref.startswith("./"):
        return True
    return bool(_COMMIT.fullmatch(ref) and comment and _VERSION.fullmatch(comment))


def _npm_installs(path: Path) -> list[tuple[int, str, str]]:
    """``(line, verb, text)`` for each npm install a workflow runs."""
    found = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.lstrip().startswith("#") and (m := _NPM.search(line)):
            found.append((n, m[1], line.strip()))
    return found


def _is_stage(name: str, stages: set[str]) -> bool:
    """A stage of this Dockerfile, named or picked by a build arg (``dd-apm-${X}``)."""
    name = name.lower()
    if "${" in name:
        return any(stage.startswith(name.split("${")[0]) for stage in stages)
    return name in stages


def _images(path: Path) -> list[tuple[int, str]]:
    """``(line, image)`` for each image a Dockerfile pulls, not its own stages."""
    stages: set[str] = set()
    found = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if m := _FROM.match(line):
            if not _is_stage(m[1], stages):
                found.append((n, m[1]))
            if m[2]:
                stages.add(m[2].lower())
        elif (m := _COPY_FROM.match(line)) and not _is_stage(m[1], stages):
            found.append((n, m[1]))
    return found


def test_every_action_runs_a_pinned_commit() -> None:
    found = [
        f"{path.name}:{n} {ref}"
        for path in _workflow_files()
        for n, ref, comment in _uses(path)
        if not _pinned(ref, comment)
    ]
    assert found == []


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda path: path.name)
def test_the_line_scan_sees_every_uses(path: Path) -> None:
    """Guards the pin check, which reads lines: it must see what YAML sees."""
    parsed = _yaml_uses(yaml.safe_load(path.read_text()))
    assert sorted(ref for _, ref, _ in _uses(path)) == sorted(parsed)


def test_the_scan_sees_the_pypi_publish_step() -> None:
    refs = [ref for _, ref, _ in _uses(WORKFLOWS / "publish-python-client.yml")]
    assert any(ref.startswith("pypa/gh-action-pypi-publish@") for ref in refs)


def test_every_npm_install_is_pinned() -> None:
    """A project installs its lockfile (``npm ci``); a global tool, one version."""
    found = [
        f"{path.name}:{n} {text}"
        for path in _workflow_files()
        for n, verb, text in _npm_installs(path)
        if verb != "ci" and not _GLOBAL_EXACT.search(text)
    ]
    assert found == []


def test_the_scan_sees_the_npm_publish_install() -> None:
    assert _npm_installs(WORKFLOWS / "publish-npm-client.yml")


def test_every_service_image_is_built_from_digests() -> None:
    found = [
        f"{path.relative_to(REPO)}:{n} {image}"
        for path in DOCKERFILES
        for n, image in _images(path)
        if not _DIGEST.fullmatch(image)
    ]
    assert found == []


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda path: path.parent.name)
def test_the_image_scan_sees_the_base_images(path: Path) -> None:
    """Guards the digest check against passing because it found nothing."""
    names = {image.split("@")[0].rsplit(":", 1)[0] for _, image in _images(path)}
    assert {"python", "ghcr.io/astral-sh/uv", "datadog/serverless-init"} <= names


def test_the_four_service_images_are_scanned() -> None:
    assert set(SERVICES) <= {path.parent.name for path in DOCKERFILES}
