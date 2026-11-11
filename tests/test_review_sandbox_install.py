"""Exercise mirror pruning with public runner-list fixtures, without host writes."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
INSTALLER = (
    Path(__file__).resolve().parents[1] / ".github/scripts/install_review_sandbox.sh"
)


def _prune(tmp_path, initial):
    mirrors = tmp_path / "mirror list"
    if initial is not None:
        mirrors.write_text(initial)
    source = INSTALLER.read_text()
    # Execute the real shell guard/filter before package installation. Stub
    # only sudo + GNU sed's in-place interface so this also runs on macOS.
    stage, separator, _ = source.partition("\nAPT=(")
    assert separator, "installer package stage was not found"
    stage = stage.replace("/etc/apt/apt-mirrors.txt", shlex.quote(str(mirrors)))
    prefix = r"""
sudo() {
  [ "$1" = sed ] && [ "$2" = -i ] && [ "$#" -eq 4 ] || return 98
  sed "$3" "$4" > "$4.next" || return
  cat "$4.next" > "$4"
}
"""
    subprocess.run(
        ["bash", "-c", prefix + stage],
        env={"PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return mirrors.read_text() if mirrors.exists() else None


@pytest.mark.parametrize("fallback_scheme", ["http", "https"])
@pytest.mark.parametrize("azure_scheme", ["http", "https"])
def test_official_fallback_allows_pruning_without_changing_other_mirrors(
    tmp_path, fallback_scheme, azure_scheme
):
    remaining = (
        f"{fallback_scheme}://archive.ubuntu.com/ubuntu/\tpriority:2\n"
        "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
    )
    mirrors = (
        f"{azure_scheme}://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n" + remaining
    )
    assert _prune(tmp_path, mirrors) == remaining


@pytest.mark.parametrize(
    "initial",
    [
        "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n",
        "http://azure.archive.ubuntu.com/ubuntu/\nhttps://archive.ubuntu.com.example/ubuntu/\n",
        "",
        None,
    ],
    ids=["only-azure", "lookalike-fallback", "empty", "missing"],
)
def test_without_official_fallback_the_list_is_preserved(tmp_path, initial):
    assert _prune(tmp_path, initial) == initial
