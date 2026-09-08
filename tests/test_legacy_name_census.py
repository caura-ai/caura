"""Integration tests for the ref-only legacy-name census."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "legacy_name_census.py"
LEGACY = "mem" + "claw"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")

    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "debt.py").write_text(
        f'OLD_URL = "https://{LEGACY}.example"\nOLD_TOPIC = "{LEGACY}-events"\n'
    )
    (root / "src" / "aliases.py").write_text(
        f'OLD_TOOL = "{LEGACY}_write"  # legacy-name-ok: permanent wire alias\n'
    )
    (root / "docs" / "floor.md").write_text(
        f"Run `{LEGACY} status`. <!-- legacy-name-floor: frozen command -->\n"
    )
    (root / "docs" / "programme.md").write_text(
        f"Example: {LEGACY}_write  # legacy-name-ok: marker documentation\n"
        f"Unmarked programme debt: {LEGACY}-later\n"
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "legacy_name_ratchet.json").write_text(
        json.dumps({"marker_inventory_meta_paths": ["docs/programme.md"]})
    )
    (root / "README.md").write_text("Caura is the current product name.\n")
    _git(
        root,
        "add",
        "README.md",
        "docs/floor.md",
        "docs/programme.md",
        "scripts/legacy_name_ratchet.json",
        "src/aliases.py",
        "src/debt.py",
    )
    _git(root, "commit", "-qm", "base")
    return root


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            "sample",
            str(repo),
            "main",
            *extra,
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def test_json_counts_debt_and_permanent_lines_by_top_level(repo: Path) -> None:
    result = _run(repo, "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    report = payload["repositories"][0]
    assert report["ref"] == "main"
    assert report["commit"] == _git(repo, "rev-parse", "main")
    assert report["counts"] == {
        "all_match_lines": 6,
        "unclassified_lines": 3,
        "deliberate_alias_lines": 1,
        "deliberate_floor_lines": 1,
        "marker_metadata_lines": 1,
    }
    assert report["top_level"] == {
        "docs": {
            "all_match_lines": 3,
            "unclassified_lines": 1,
            "deliberate_alias_lines": 0,
            "deliberate_floor_lines": 1,
            "marker_metadata_lines": 1,
        },
        "src": {
            "all_match_lines": 3,
            "unclassified_lines": 2,
            "deliberate_alias_lines": 1,
            "deliberate_floor_lines": 0,
            "marker_metadata_lines": 0,
        },
    }
    assert report["control"]["pattern"] == "caura"
    assert report["control"]["matched_lines"] == 1
    assert payload["aggregate"] == report["counts"]


def test_named_ref_is_scanned_instead_of_the_working_tree(repo: Path) -> None:
    (repo / "src" / "debt.py").write_text(
        "\n".join(f"{LEGACY}-{index}" for index in range(20))
    )
    (repo / "working-tree-only.txt").write_text(f"{LEGACY}-untracked\n")
    (repo / "scripts" / "legacy_name_ratchet.json").write_text(
        json.dumps({"marker_inventory_meta_paths": []})
    )

    report = json.loads(_run(repo, "--json").stdout)["repositories"][0]

    assert report["counts"]["all_match_lines"] == 6
    assert report["counts"]["unclassified_lines"] == 3
    assert report["counts"]["marker_metadata_lines"] == 1


def test_nested_checkout_paths_are_excluded(repo: Path) -> None:
    hidden = repo / ".claude" / "worktrees" / "stale"
    hidden.mkdir(parents=True)
    (hidden / "copy.py").write_text(f"{LEGACY}-stale\n")
    generic = repo / ".worktrees" / "other"
    generic.mkdir(parents=True)
    (generic / "copy.py").write_text(f"{LEGACY}-other\n")
    nested = repo / "vendor" / ".claude" / "worktrees" / "third"
    nested.mkdir(parents=True)
    (nested / "copy.py").write_text(f"{LEGACY}-third\n")
    gitlink = repo / "vendor" / "external"
    gitlink.mkdir()
    _git(gitlink, "init", "-q", "-b", "main")
    _git(gitlink, "config", "user.email", "t@example.com")
    _git(gitlink, "config", "user.name", "t")
    (gitlink / "copy.py").write_text(f"{LEGACY}-gitlink\n")
    _git(gitlink, "add", "copy.py")
    _git(gitlink, "commit", "-qm", "nested checkout")
    _git(repo, "add", "-f", ".claude/worktrees/stale/copy.py")
    _git(repo, "add", "-f", ".worktrees/other/copy.py")
    _git(repo, "add", "-f", "vendor/.claude/worktrees/third/copy.py")
    _git(repo, "add", "vendor/external")
    _git(repo, "commit", "-qm", "add nested checkout fixtures")

    report = json.loads(_run(repo, "--json").stdout)["repositories"][0]

    assert report["counts"]["all_match_lines"] == 6
    assert ".claude" not in report["top_level"]
    assert ".worktrees" not in report["top_level"]
    assert "vendor" not in report["top_level"]


def test_human_summary_names_ref_commit_control_and_baseline(repo: Path) -> None:
    result = _run(repo, "--baseline", "3635")

    assert result.returncode == 0, result.stderr
    assert (
        f"sample @ main ({_git(repo, 'rev-parse', '--short=12', 'main')})"
        in result.stdout
    )
    assert "Control: 1 line(s) matched 'caura'" in result.stdout
    assert "Baseline 3,635" in result.stdout
    assert "raw matches" in result.stdout
    assert "unclassified candidate debt" in result.stdout
    assert "  docs: 3 raw matches" in result.stdout

    payload = json.loads(_run(repo, "--baseline", "3635", "--json").stdout)
    assert payload["baseline"] == {
        "original": 3635,
        "provenance_verified": False,
        "note": "The historical query, refs, and exclusions were not supplied.",
        "raw_matches": {
            "remaining": 6,
            "cleared": 3629,
            "cleared_percent": 99.8,
        },
    }


def test_repeated_repositories_have_individual_and_aggregate_counts(
    repo: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            "first",
            str(repo),
            "main",
            "--repo",
            "second",
            str(repo),
            "main",
            "--json",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [item["name"] for item in payload["repositories"]] == ["first", "second"]
    assert payload["aggregate"] == {
        key: value * 2 for key, value in payload["repositories"][0]["counts"].items()
    }


def test_root_files_are_reported_under_dot(repo: Path) -> None:
    name = "odd\nname.txt"
    (repo / name).write_text(f"{LEGACY}-root\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", "add root legacy line")

    report = json.loads(_run(repo, "--json").stdout)["repositories"][0]

    assert report["top_level"]["."]["all_match_lines"] == 1
    assert report["top_level"]["."]["unclassified_lines"] == 1


def test_missing_ref_is_an_error_not_a_false_zero(repo: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            "sample",
            str(repo),
            "no-such-ref",
            "--json",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "no-such-ref" in result.stderr
    assert result.stdout == ""


def test_missing_control_is_an_error_despite_legacy_matches(repo: Path) -> None:
    (repo / "README.md").write_text("The current product name is absent.\n")
    hidden = repo / ".claude" / "worktrees" / "control"
    hidden.mkdir(parents=True)
    (hidden / "README.md").write_text("Caura exists only in an excluded path.\n")
    _git(repo, "add", "README.md")
    _git(repo, "add", "-f", ".claude/worktrees/control/README.md")
    _git(repo, "commit", "-qm", "remove the positive control")

    result = _run(repo, "--json")

    assert result.returncode == 2
    assert "positive control 'caura' matched no lines" in result.stderr
    assert result.stdout == ""
