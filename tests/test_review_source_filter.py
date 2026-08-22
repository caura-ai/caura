"""What `pre-checks` counts as "source" decides whether a review happens at all.

`claude_code_review.yml` skips the whole review job when a pull request
changes no source files. #872 told the reviewer to review ``conftest.py``
and ``tests/_*.py``, but that instruction lives in ``REVIEW_PROMPT``, which
is only read once the job runs — and a change confined to those files still
counted zero source files, so the job skipped and the prompt was never
reached. #866 was that case, and #872 itself was too: a lone workflow
``.yml`` is config, so the pull request removing the exclusion was not
reviewed either.

The expression is extracted from the workflow and executed here rather than
restated. A copy would pin the copy: the two would drift, this file would
stay green, and the thing that decides whether code gets reviewed would go
back to being unverified. Extraction also means a reformat of that line
fails loudly here instead of silently unpinning it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/claude_code_review.yml"

# The `SRC_FILES=` assignment, then the single-quoted argument to `--jq` that
# follows it. Anchored on the variable name because the same file runs several
# `gh api --jq` calls and only this one decides the skip.
_FILTER_RE = re.compile(r"SRC_FILES=.*?--jq\s+'(?P<expr>[^']*)'", re.DOTALL)


def _source_filter() -> str:
    match = _FILTER_RE.search(WORKFLOW.read_text())
    assert match is not None, (
        f"could not find the SRC_FILES --jq expression in {WORKFLOW}. If that step was "
        "reworded, update this regex — do not delete the test: the expression decides "
        "whether any pull request gets reviewed."
    )
    return match.group("expr")


def _counts_as_source(*filenames: str) -> int:
    """Run the workflow's own filter over *filenames*, as the GitHub API shapes them."""
    payload = json.dumps([{"filename": f} for f in filenames])
    result = subprocess.run(
        ["jq", _source_filter()],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


pytestmark = [
    pytestmark,
    pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed"),
]


class TestTheSharedTestFilesCountAsSource:
    """The regression #872 aimed at and did not reach."""

    @pytest.mark.parametrize(
        "path",
        [
            "tests/conftest.py",
            "tests/pipeline/conftest.py",
            "tests/_scoped_module.py",
            "tests/_mcp_test_helpers.py",
        ],
    )
    def test_a_shared_helper_alone_is_enough_to_trigger_a_review(self, path: str) -> None:
        assert _counts_as_source(path) == 1, (
            f"{path} counts as zero source files, so a pull request touching only it "
            "skips review entirely — regardless of what REVIEW_PROMPT says"
        )


class TestOrdinaryTestsAndDocsStillDoNot:
    """The carve-out must not become 'review everything'.

    Widening it far enough to catch the helpers would spend a review run on
    every docs typo, which is the cost the exclusion exists to avoid.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_llm_retry_after.py",
            "tests/pipeline/test_write_pipeline.py",
            "test_toplevel.py",
            "docs/runbooks/pubsub.md",
            "README.md",
            "pyproject.toml",
            "core-api/uv.lock",
            "Dockerfile",
            "helm/values.yaml",
            ".github/workflows/ci.yml",
        ],
    )
    def test_it_does_not_count_as_source(self, path: str) -> None:
        assert _counts_as_source(path) == 0


class TestTheCountIsACount:
    def test_real_source_still_counts(self) -> None:
        assert _counts_as_source("core-api/src/core_api/services/memory_service.py") == 1

    def test_a_mixed_pull_request_counts_only_the_source(self) -> None:
        """The skip is `-eq 0`, so what matters is that the total is not zero —
        but a wrong count here would also mean a wrong `MAX_FILES` intuition."""
        assert (
            _counts_as_source(
                "docs/x.md",
                "tests/test_foo.py",
                "core-api/src/core_api/main.py",
                "tests/conftest.py",
            )
            == 2
        )

    def test_an_empty_pull_request_is_zero(self) -> None:
        assert _counts_as_source() == 0
