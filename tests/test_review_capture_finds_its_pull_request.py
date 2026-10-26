"""Capture on a push to main finds the pull request whose merge made that push.

A push carries no pull request number, so the capture job looks it up: the merged
pull request into main whose merge commit is the pushed commit. Anything else (a
push no merge produced, a pull request that merely contains the commit, a failed
lookup) captures nothing and still exits 0, because a job that runs after a merge
must never fail an already-merged pull request. A capture started by hand takes
its number from the input, which has to be a plain number.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed"),
]

REVIEW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/claude_code_review.yml"
)
SHA = "a" * 40
OTHER_SHA = "b" * 40
STUB = """
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["LOOKUP_TEST_DIR"])
with (root / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
if (root / "fail").exists():
    sys.exit(1)
print((root / "pulls.json").read_text())
"""


def _steps() -> list[dict[str, Any]]:
    return yaml.safe_load(REVIEW.read_text())["jobs"]["claude-capture"]["steps"]


def _lookup_step() -> dict[str, Any]:
    return next(step for step in _steps() if step.get("id") == "merged")


def _pull(number, merge_sha=SHA, merged=True, base="main"):
    return {
        "number": number,
        "merged_at": "2026-10-26T08:39:49Z" if merged else None,
        "merge_commit_sha": merge_sha,
        "base": {"ref": base},
    }


def _run(tmp_path, event, pulls=None, dispatch_input="", fail=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n{STUB}")
    gh.chmod(0o755)
    (tmp_path / "pulls.json").write_text(json.dumps([] if pulls is None else pulls))
    if fail:
        (tmp_path / "fail").touch()
    output = tmp_path / "github-output"
    output.touch()
    result = subprocess.run(
        ["bash", "-c", _lookup_step()["run"]],
        env={
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", os.defpath),
            "LOOKUP_TEST_DIR": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_EVENT_NAME": event,
            "REPO": "example/review-test",
            "MERGE_SHA": SHA,
            "DISPATCH_PR": dispatch_input,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    calls_file = tmp_path / "calls.jsonl"
    calls = (
        [json.loads(line) for line in calls_file.read_text().splitlines()]
        if calls_file.exists()
        else []
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return outputs["pr_number"], calls, result.stdout


def test_the_capture_steps_take_the_number_the_lookup_found() -> None:
    assert _lookup_step()["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "REPO": "${{ github.repository }}",
        "MERGE_SHA": "${{ github.sha }}",
        "DISPATCH_PR": "${{ inputs.pr_number }}",
    }
    later = _steps()[_steps().index(_lookup_step()) + 1 :]
    assert [step["name"] for step in later] == [
        "Install Claude CLI",
        "Capture declined review findings",
    ]
    for step in later:
        assert step["if"] == "steps.merged.outputs.pr_number != ''"
    assert later[-1]["env"]["PR_NUMBER"] == "${{ steps.merged.outputs.pr_number }}"


def test_a_merge_into_main_finds_its_pull_request(tmp_path) -> None:
    pulls = [_pull(7, merge_sha=OTHER_SHA, base="dev"), _pull(2220)]
    number, calls, _ = _run(tmp_path, "push", pulls)
    assert number == "2220"
    assert calls == [["api", f"repos/example/review-test/commits/{SHA}/pulls"]]


@pytest.mark.parametrize(
    "pulls",
    [
        [],
        [_pull(5, merged=False)],
        [_pull(5, merge_sha=OTHER_SHA)],
        [_pull(5, base="dev")],
    ],
    ids=["no-pull-request", "not-merged", "merged-elsewhere", "other-base"],
)
def test_a_push_no_merge_into_main_made_captures_nothing(tmp_path, pulls) -> None:
    number, _, stdout = _run(tmp_path, "push", pulls)
    assert number == ""
    assert "::notice::" in stdout


def test_a_failed_lookup_warns_and_captures_nothing(tmp_path) -> None:
    number, calls, stdout = _run(tmp_path, "push", [_pull(2220)], fail=True)
    assert number == ""
    assert calls
    assert "::warning::" in stdout


def test_an_answer_that_is_not_a_list_captures_nothing(tmp_path) -> None:
    number, _, stdout = _run(tmp_path, "push", {"message": "Server Error"})
    assert number == ""
    assert "::notice::" in stdout


def test_a_capture_by_hand_takes_the_number_from_its_input(tmp_path) -> None:
    number, calls, _ = _run(tmp_path, "workflow_dispatch", dispatch_input="2220")
    assert number == "2220"
    assert calls == []


@pytest.mark.parametrize("value", ["", "12 34", "1;id", "../1", "-1", "0x10"])
def test_a_hand_input_that_is_not_a_number_captures_nothing(tmp_path, value) -> None:
    number, calls, stdout = _run(tmp_path, "workflow_dispatch", dispatch_input=value)
    assert number == ""
    assert calls == []
    assert "::warning::" in stdout
