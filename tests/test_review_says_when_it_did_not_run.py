"""An ``@claude`` comment always gets an answer, even when the review never ran.

The review script comments on every outcome it reaches. A failure before it (the
checkout, the sandbox install), an error it does not handle, or a cancelled job
used to end the run with nothing on the pull request: caura#2488's first
``@claude`` went unanswered when the runner lost its network mid-install. The
claude-review job's last step answers those runs, and only those.

What this file cannot run: GitHub's evaluation of the step's ``if``. It checks
the expression's terms; ``actionlint`` checks that it parses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REVIEW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/claude_code_review.yml"
)
RUN_URL = "https://github.com/example/review/actions/runs/1"


def _steps() -> list[dict[str, Any]]:
    return yaml.safe_load(REVIEW.read_text())["jobs"]["claude-review"]["steps"]


def _fallback() -> dict[str, Any]:
    return _steps()[-1]


def test_the_last_step_answers_only_a_run_that_posted_nothing() -> None:
    step = _fallback()
    assert step["name"] == "Say the review did not run"
    condition = step["if"]
    assert "(failure() || cancelled())" in condition
    assert "steps.auth.outputs.authorized == 'true'" in condition
    assert "steps.pr.outputs.is_pr == 'true'" in condition
    assert "steps.review.outputs.posted != 'true'" in condition


def test_it_names_the_steps_it_reads_and_needs_no_checkout() -> None:
    ids = {step.get("id") for step in _steps()}
    assert {"checkout", "install", "review"} <= ids
    step = _fallback()
    assert "uses" not in step
    assert ".github/" not in step["run"]
    assert all("secrets." not in str(value) for value in step["env"].values())


@pytest.mark.parametrize(
    "outcomes, why",
    [
        (
            ("failure", "failure", "skipped", "skipped"),
            "checking out the review script failed",
        ),
        (
            ("failure", "success", "failure", "skipped"),
            "installing the reviewer sandbox failed",
        ),
        (
            ("failure", "success", "success", "failure"),
            "the review stopped on an error",
        ),
        (("cancelled", "success", "cancelled", "skipped"), "the job was cancelled"),
        (("cancelled", "success", "success", "success"), "the job was cancelled"),
        # Failed somewhere none of the three named steps covers: say so, not "cancelled".
        (
            ("failure", "success", "success", "success"),
            "the job failed before the review ran",
        ),
    ],
)
def test_the_comment_says_what_stopped_the_review(tmp_path, outcomes, why) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['TEST_DIR'], 'args').write_text('\\0'.join(sys.argv[1:]))\n"
    )
    gh.chmod(0o755)
    job, checkout, install, review = outcomes
    result = subprocess.run(
        ["bash", "-e", "-c", _fallback()["run"]],
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "TEST_DIR": str(tmp_path),
            "GH_TOKEN": "synthetic-github-key-for-tests-only",
            "REPO": "example/review",
            "PR_NUMBER": "7",
            "JOB": job,
            "CHECKOUT": checkout,
            "INSTALL": install,
            "REVIEW": review,
            "RUN_URL": RUN_URL,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    args = (tmp_path / "args").read_text().split("\0")
    assert args[:2] == ["api", "repos/example/review/issues/7/comments"]
    body = args[args.index("-f") + 1]
    assert body.startswith("body=⚠️ Claude Code review did not run: ")
    assert f": {why}, so no review was produced." in body
    assert "Comment `@claude` to try again." in body
    assert f"[Job log]({RUN_URL})" in body
