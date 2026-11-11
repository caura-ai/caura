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


# On 2026-11-11 GitHub's runners stopped resolving azure.archive.ubuntu.com and
# then archive.ubuntu.com too, so pruning to the official mirror no longer helped.
# The installer now falls back to a mirror outside ubuntu.com. These run the
# real package stage against stubbed sudo, timeout and apt-get.
STUBS = {
    "sudo": 'exec "$@"\n',
    # Drops timeout's options and duration, and tells apt-get it was bounded.
    "timeout": """while [ "$#" -gt 0 ]; do
  case "$1" in
    -k|-s) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
STUB_BOUNDED=1 exec "$@"
""",
    # A call passing -o Dir::Etc::SourceList=FILE uses the fallback list; any
    # other call uses the runner's own mirrors.
    "apt-get": """list=
prev=
for a in "$@"; do
  if [ "$prev" = -o ]; then
    case "$a" in Dir::Etc::SourceList=*) list=${a#Dir::Etc::SourceList=} ;; esac
  fi
  prev=$a
done
{
  printf 'apt-get %s | bounded=%s\\n' "$*" "${STUB_BOUNDED:-0}"
  [ -z "$list" ] || sed 's/^/  list: /' "$list"
} >> "$STUB_APT_LOG"
if [ -n "$list" ]; then exit "${STUB_APT_FALLBACK_RC:-0}"; fi
exit "${STUB_APT_RUNNER_RC:-0}"
""",
}


def _install_packages(tmp_path, runner_rc=0, fallback_rc=0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text("#!/usr/bin/env bash\n" + body)
        stub.chmod(0o755)
    log = tmp_path / "apt.log"
    log.touch()
    source = INSTALLER.read_text()
    stage, separator, _ = source.partition("\n# Ubuntu restricts")
    assert separator, "installer AppArmor stage was not found"
    stage = stage.replace(
        "/etc/apt/apt-mirrors.txt", shlex.quote(str(tmp_path / "no mirror list"))
    )
    result = subprocess.run(
        ["bash", "-c", stage],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "STUB_APT_LOG": str(log),
            "STUB_APT_RUNNER_RC": str(runner_rc),
            "STUB_APT_FALLBACK_RC": str(fallback_rc),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, log.read_text()


def test_healthy_runner_mirrors_need_no_fallback(tmp_path):
    result, log = _install_packages(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "install -y bubblewrap apparmor-utils" in log
    assert "list:" not in log
    assert "::warning::" not in result.stdout


def test_failed_runner_mirrors_fall_back_to_a_signed_mirror_outside_ubuntu_com(
    tmp_path,
):
    result, log = _install_packages(tmp_path, runner_rc=100)
    assert result.returncode == 0, result.stdout + result.stderr
    key = "signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg"
    for suite in ("noble", "noble-updates", "noble-security"):
        assert (
            f"  list: deb [{key}] https://mirrors.edge.kernel.org/ubuntu {suite} main"
            in log.splitlines()
        )
    installs = [line for line in log.splitlines() if "install -y" in line]
    assert any("Dir::Etc::SourceList=" in line for line in installs)
    assert "fallback mirror" in result.stdout
    assert "::error::" not in result.stdout + result.stderr


def test_no_mirror_at_all_fails_the_install(tmp_path):
    result, log = _install_packages(tmp_path, runner_rc=100, fallback_rc=100)
    assert result.returncode != 0
    assert "::error::" in result.stdout + result.stderr
    assert "bounded=0" not in log
