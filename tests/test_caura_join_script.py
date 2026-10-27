"""The convenience installer selects a released package and requires uv."""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "caura-join.sh"
MATCH = re.search(r'version="([0-9]+\.[0-9]+\.[0-9]+)"', SCRIPT.read_text())
assert MATCH is not None
VERSION = MATCH.group(1)
README = (ROOT / "clients" / "collaboration" / "README.md").read_text()


def test_join_installs_a_pinned_release_without_replacing_existing_tools(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    capture = tmp_path / "calls"
    uv = binary / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE"\n'
        'case "$*" in "tool dir --bin") printf "%s\\n" "$BIN";; esac\n',
        encoding="utf-8",
    )
    bus = binary / "caura-bus"
    bus.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE"\n', encoding="utf-8")
    for path in (uv, bus):
        path.chmod(0o700)
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT), "--url", "https://example.test"],
        env={
            "PATH": str(binary),
            "HOME": str(tmp_path),
            "CAPTURE": str(capture),
            "BIN": str(binary),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    commands = capture.read_text().splitlines()
    assert commands[0] == f"tool install --quiet caura-bus-cli=={VERSION}"
    assert commands[-1] == "join --url https://example.test"
    assert f"uvx --from caura-bus-cli=={VERSION} caura-bus join" in README
    assert "git+https" not in README
    assert "| sh" not in README


def test_join_without_uv_stops_before_fetching_any_installer(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    capture = tmp_path / "curl-called"
    curl = binary / "curl"
    curl.write_text('#!/bin/sh\n: > "$CAPTURE"\nexit 1\n', encoding="utf-8")
    curl.chmod(0o700)
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env={"PATH": str(binary), "HOME": str(tmp_path), "CAPTURE": str(capture)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not capture.exists()
    assert "install uv first" in result.stderr.lower()
