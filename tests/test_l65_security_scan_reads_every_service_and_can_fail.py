"""L-65: the security scan reads every service, audits what ships, and can fail.

Both scan steps ran with ``continue-on-error``, so the check passed whatever
bandit or pip-audit found. bandit read only core-api's and core-storage-api's
source, not ``common/``, core-worker's or core-operations', and pip-audit
checked a fresh resolution of those two services' pyproject ranges instead of
the ``uv.lock`` each image installs. Now bandit reads ``common/`` and every
service's source, pip-audit reads every service's lock as its Dockerfile
exports it, and a finding fails the job. A pip-audit crash fails it too, as
unaudited rather than as a finding.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
SCAN = REPO / ".github/workflows/security-scan.yml"
SERVICES = {"core-api", "core-storage-api", "core-worker", "core-operations"}
LOCKED = sorted(path.parent.name for path in REPO.glob("*/uv.lock"))


def _jobs() -> list[dict]:
    return list(yaml.safe_load(SCAN.read_text())["jobs"].values())


def _runs(command: str) -> str:
    """Every scan ``run:`` script that runs ``command``, joined."""
    runs = (step.get("run", "") for job in _jobs() for step in job["steps"])
    return "\n".join(run for run in runs if command in run)


def test_the_scan_sees_the_services_and_its_tools() -> None:
    assert set(LOCKED) >= SERVICES
    assert "core-api/src" in _runs("bandit -r")
    assert _runs("pip-audit --")


def test_a_finding_fails_the_scan() -> None:
    lenient = [job.get("name") for job in _jobs() if job.get("continue-on-error")]
    lenient += [
        step.get("name")
        for job in _jobs()
        for step in job["steps"]
        if step.get("continue-on-error")
    ]
    assert lenient == []


def test_bandit_reads_common_and_every_service() -> None:
    read = set(re.findall(r"[\w./-]+", _runs("bandit -r")))
    wanted = ["common", *(f"{name}/src" for name in LOCKED)]
    assert [path for path in wanted if path not in read] == []


def test_pip_audit_reads_every_service_lock() -> None:
    audit = _runs("pip-audit --")
    assert "uv export --frozen" in audit
    assert "--no-deps" in audit
    named = set(re.findall(r"[\w.-]+", audit))
    assert [name for name in LOCKED if name not in named] == []


def test_pip_audit_reads_what_the_images_install() -> None:
    """Each service's extras come from its Dockerfile. ``--all-extras`` also
    exported extras no image installs, so it failed the scan on pins that
    never ship."""
    audit = _runs("pip-audit --")
    assert "--all-extras" not in audit
    assert "/Dockerfile" in audit


def test_pip_audit_reads_osv() -> None:
    """L-239: pip-audit's default service, PyPI's JSON feed, had no entry for
    GHSA-5639-2j2p-m4mx (mako < 1.4.2) two weeks after OSV and the GitHub
    advisory database published it, so the scan passed while two images
    installed mako 1.3.12. OSV carries both the PYSEC and the GHSA advisories."""
    assert re.search(r"(--vulnerability-service|-s)[ =]osv\b", _runs("pip-audit --"))


def test_every_accepted_advisory_says_why() -> None:
    """An advisory is accepted only with the reason it does not apply, written
    beside the step, so each ignored ID appears in that comment as well. None
    may be accepted, so instead of requiring one, every ``--ignore-vuln`` must
    parse: a spelling the pattern missed would leave its ID unchecked."""
    audit = _runs("pip-audit --")
    ignored = re.findall(r"--ignore-vuln[ =]([\w-]+)", audit)
    text = SCAN.read_text()
    assert len(ignored) == audit.count("--ignore-vuln")
    assert [vid for vid in ignored if text.count(vid) < 2] == []


# The step's own script, run with stand-ins for uv, pip-audit and sleep. The
# stand-in pip-audit plays clean, finding, crash or crash-once for each service
# named in FAKE_AUDIT ("core-api=finding core-worker=crash"), as pip-audit
# 2.10.1 does: a clean audit exits 0 and writes no -o report, a finding exits 1
# and writes it, and a crash exits 1 with only a traceback.
_UV = """#!/bin/sh
while [ $# -gt 0 ]; do
  if [ "$1" = -o ]; then : > "$2"; fi
  shift
done
"""

_PIP_AUDIT = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in -r) req=$2; shift ;; -o) out=$2; shift ;; esac
  shift
done
service=$(basename "$req" .txt)
echo call >> "$RUNNER_TEMP/$service.calls"
mode=clean
for pair in $FAKE_AUDIT; do
  if [ "${pair%%=*}" = "$service" ]; then mode=${pair#*=}; fi
done
case "$mode" in
  finding) echo "mako 1.3.12 PYSEC-0000-0 1.4.2 a finding" > "$out"; exit 1 ;;
  crash) echo "requests.exceptions.ReadTimeout: api.osv.dev" >&2; exit 1 ;;
  crash-once)
    if [ "$(wc -l < "$RUNNER_TEMP/$service.calls")" -eq 1 ]; then exit 1; fi ;;
esac
echo "No known vulnerabilities found" >&2
"""


def _has_mapfile() -> bool:
    bash = shutil.which("bash")
    if bash is None:
        return False
    probe = subprocess.run(
        [bash, "-c", "mapfile -t x < /dev/null"], capture_output=True, check=False
    )
    return probe.returncode == 0


needs_mapfile = pytest.mark.skipif(
    not _has_mapfile(),
    reason="the step uses mapfile, which needs bash 4; GitHub's runners have bash 5",
)


def _audit(
    tmp_path: Path, fake_audit: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in {
        "uv": _UV,
        "pip-audit": _PIP_AUDIT,
        "sleep": "#!/bin/sh\n",
    }.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    for service in SERVICES:
        (tmp_path / service).mkdir()
        (tmp_path / service / "Dockerfile").write_text(
            "RUN uv export --frozen --extra datadog\n"
        )
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = {
        **os.environ,
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "FAKE_AUDIT": fake_audit,
    }
    # ``bash -e`` is how Actions runs a ``run:`` step that names no shell.
    result = subprocess.run(
        ["bash", "-e", "-c", _runs("pip-audit --")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, runner_temp


def _calls(runner_temp: Path, service: str) -> int:
    return (runner_temp / f"{service}.calls").read_text().count("call")


@needs_mapfile
def test_a_clean_audit_passes(tmp_path: Path) -> None:
    result, runner_temp = _audit(tmp_path, "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [_calls(runner_temp, service) for service in sorted(SERVICES)] == [
        1,
        1,
        1,
        1,
    ]


@needs_mapfile
def test_a_finding_fails_the_step_with_its_report(tmp_path: Path) -> None:
    result, runner_temp = _audit(tmp_path, "core-api=finding")
    assert result.returncode == 1
    assert "Known-vulnerable dependencies in: core-api." in result.stdout
    assert "PYSEC-0000-0" in result.stdout
    assert _calls(runner_temp, "core-api") == 1


@needs_mapfile
def test_a_crash_fails_the_step_as_unaudited_not_vulnerable(tmp_path: Path) -> None:
    """On 9 Nov 2026 api.osv.dev timed out, pip-audit crashed, and the step
    reported core-storage-api as known-vulnerable."""
    result, runner_temp = _audit(tmp_path, "core-storage-api=crash")
    assert result.returncode == 1
    assert "Known-vulnerable" not in result.stdout
    assert "pip-audit did not finish for: core-storage-api, twice" in result.stdout
    assert _calls(runner_temp, "core-storage-api") == 2


@needs_mapfile
def test_a_crash_the_retry_gets_past_passes(tmp_path: Path) -> None:
    result, runner_temp = _audit(tmp_path, "core-worker=crash-once")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(runner_temp, "core-worker") == 2


@needs_mapfile
def test_a_finding_and_a_crash_are_both_reported(tmp_path: Path) -> None:
    result, _ = _audit(tmp_path, "core-api=finding core-operations=crash")
    assert result.returncode == 1
    assert "Known-vulnerable dependencies in: core-api." in result.stdout
    assert "pip-audit did not finish for: core-operations, twice" in result.stdout
