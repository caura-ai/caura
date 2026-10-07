"""Exercise the Linux boundary and publication guard with synthetic secrets only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
SCRIPTS = Path(__file__).resolve().parents[1] / ".github/scripts"
KEY = "synthetic-provider-key-for-tests-only"
GITHUB_KEY = "synthetic-github-key-for-tests-only"
CAURA_KEY = "synthetic-caura-key-for-tests-only"


def test_linux_sandbox_hides_host_credentials_and_keeps_source_readable(tmp_path):
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("Sandbox integration runs on the provisioned Linux CI runner")
    # CI must install the boundary; do not silently skip if provisioning drifts.
    assert shutil.which("bwrap"), "bubblewrap is required on Linux"
    runtime = tmp_path / "runtime/bin"
    runtime.mkdir(parents=True)
    (runtime / "node").write_text("#!/bin/sh\nexit 0\n")
    (runtime / "node").chmod(0o755)
    probe = runtime / "claude"
    probe.write_text(
        "#!/usr/bin/python3\n"
        + """
import os
import sys
from pathlib import Path

assert Path.cwd() == Path('/workspace')
assert Path('source.txt').read_text() == 'public source'
assert 'GH_TOKEN' not in os.environ
assert 'CAURA_AGENTS_KEY' not in os.environ
assert 'NODE_OPTIONS' not in os.environ
assert os.environ['ANTHROPIC_API_KEY'] == sys.argv[1]
# Inspect every visible process, including PID 1: no real job credential may
# survive in environ, argv or a root/fd route back to the host.
for proc in Path('/proc').iterdir():
    if proc.name.isdigit():
        for name in ('environ', 'cmdline'):
            try:
                content = (proc / name).read_bytes()
            except OSError:
                continue
            assert b'synthetic-provider-key-for-tests-only' not in content
            assert b'synthetic-github-key-for-tests-only' not in content
            assert b'synthetic-caura-key-for-tests-only' not in content
for name in sys.argv[2:]:
    try:
        Path(name).read_bytes()
    except OSError:
        pass
    else:
        raise AssertionError('host file was readable')
try:
    Path('source.txt').write_text('modified')
except OSError:
    pass
else:
    raise AssertionError('source was writable')
assert sys.stdin.read() == 'untrusted diff'
print('sandbox checks passed')
"""
    )
    probe.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.txt").write_text("public source")
    host_file = tmp_path / "host-credential"
    host_file.write_text("synthetic-host-only")
    (repo / "escape").symlink_to(host_file)
    for args in (
        ["init", "-q"],
        ["add", "source.txt", "escape"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo, check=True
        )
    # Untracked credential-shaped files must never enter the snapshot.
    (repo / "untracked-credential").write_text("synthetic-untracked")
    (repo / ".git/config").write_text("[test]\ncredential = synthetic-git-config\n")
    result = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "claude_review_sandbox.sh"),
            "synthetic-proxy-token-only",
            str(host_file),
            str(repo / ".git/config"),
            "/workspace/.git/config",
            "/workspace/untracked-credential",
            "/workspace/escape",
            "/proc/1/root" + str(host_file),
            "/proc/self/root" + str(host_file),
            "/dev/fd/3",
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": str(runtime) + os.pathsep + os.environ["PATH"],
            "ANTHROPIC_API_KEY": KEY,
            "GH_TOKEN": GITHUB_KEY,
            "CAURA_AGENTS_KEY": CAURA_KEY,
            "NODE_OPTIONS": "--invalid-host-option",
            "REVIEW_PROXY_TOKEN": "synthetic-proxy-token-only",
            "REVIEW_PROXY_URL": "http://127.0.0.1:1",
        },
        input="untrusted diff",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sandbox checks passed"
    assert (repo / "source.txt").read_text() == "public source"


def _review(tmp_path, output, stderr="", code=0):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "claude_pr_review.sh",
        "claude_review_output.py",
        "claude_review_proxy.py",
    ):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    # Publication tests replace only the boundary; the test above uses real bwrap.
    (scripts / "claude_review_sandbox.sh").write_text(
        'cat "$REVIEW_TEST_DIR/model-output"\n'
        'cat "$REVIEW_TEST_DIR/model-stderr" >&2\n'
        f"exit {code}\n"
    )
    (tmp_path / "model-output").write_text(output)
    (tmp_path / "model-stderr").write_text(stderr)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "if '-f' in sys.argv:\n"
        "    Path(os.environ['REVIEW_TEST_DIR'], 'posted').write_text(\n"
        "        sys.argv[sys.argv.index('-f') + 1])\n"
        "else:\n"
        "    print('synthetic diff')\n"
    )
    gh.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nprintf '{}'\n")
    curl.chmod(0o755)
    summary = tmp_path / "summary"
    result = subprocess.run(
        ["bash", str(scripts / "claude_pr_review.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "REVIEW_TEST_DIR": str(tmp_path),
            "REPO": "example/review",
            "PR_NUMBER": "1",
            "EXTRA_PROMPT": "",
            "REVIEW_PROMPT": "Review the diff",
            "ANTHROPIC_API_KEY": KEY,
            "GH_TOKEN": GITHUB_KEY,
            "CAURA_AGENTS_KEY": CAURA_KEY,
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    published = (tmp_path / "posted").read_text()
    summary_text = summary.read_text() if summary.exists() else ""
    return result, published, summary_text


@pytest.mark.parametrize("key", [KEY, GITHUB_KEY, CAURA_KEY])
@pytest.mark.parametrize("where", ["result", "stderr", "escaped"])
def test_credentials_are_withheld_on_all_output_paths(tmp_path, key, where):
    output = json.dumps({"result": key if where == "result" else "safe review"})
    if where == "escaped":
        output = '{"result":"' + "".join(f"\\u{ord(c):04x}" for c in key) + '"}'
    result, posted, summary = _review(
        tmp_path, output, key if where == "stderr" else ""
    )
    assert result.returncode == 1
    assert "withheld by credential guard" in posted
    assert key not in result.stdout + result.stderr + posted + summary
    assert not summary


@pytest.mark.parametrize("code", [0, 1])
def test_raw_invalid_output_is_never_logged(tmp_path, code):
    raw = "sensitive unknown diagnostic"
    result, posted, summary = _review(tmp_path, raw, raw, code)
    assert result.returncode == 1
    assert raw not in result.stdout + result.stderr + posted + summary


def test_generic_key_shape_is_withheld_even_when_not_a_job_secret(tmp_path):
    raw = "sk-ant-" + "synthetic" * 8
    result, posted, _ = _review(tmp_path, json.dumps({"result": raw}))
    assert result.returncode == 1
    assert "withheld by credential guard" in posted


def test_safe_review_preserves_body_and_validates_telemetry(tmp_path):
    result, posted, summary = _review(
        tmp_path,
        json.dumps(
            {
                "result": "Review: missing tenant predicate.",
                "total_cost_usd": 0.25,
                "usage": {"input_tokens": "untrusted telemetry", "output_tokens": 20},
            }
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "Review: missing tenant predicate." in posted
    assert "$0.25" in posted and "$0.25" in summary
    assert "| Input tokens | ? |" in summary
    assert "untrusted telemetry" not in result.stdout + result.stderr + posted + summary


def test_budget_failure_publishes_only_numeric_cost(tmp_path):
    result, posted, summary = _review(
        tmp_path,
        json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": "unsafe"}),
        code=1,
    )
    assert result.returncode == 1
    assert "spend ceiling after $unknown" in posted
    assert "unsafe" not in result.stdout + result.stderr + posted + summary
