#!/usr/bin/env python3
"""Count legacy-name lines in explicit git refs across repositories.

The census is deliberately ref-only. It resolves every supplied ref to a commit
and runs ``git grep`` against that commit; modified and untracked working-tree
files cannot affect the result.

Usage::

    scripts/legacy_name_census.py \
      --repo caura . origin/main \
      --repo caura-enterprise ../caura-enterprise origin/dev
    scripts/legacy_name_census.py --repo caura . origin/main --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Literal, NotRequired, TypedDict

import legacy_name_ratchet as ratchet

CountField = Literal[
    "all_match_lines",
    "unclassified_lines",
    "deliberate_alias_lines",
    "deliberate_floor_lines",
    "marker_metadata_lines",
]

_COUNT_FIELDS: tuple[CountField, ...] = (
    "all_match_lines",
    "unclassified_lines",
    "deliberate_alias_lines",
    "deliberate_floor_lines",
    "marker_metadata_lines",
)

# Ref scans cannot see untracked worktrees in the first place. These pathspecs
# also make the exclusion explicit for a ref that accidentally committed a
# worktree copy, at the repository root or below another directory.
_NESTED_CHECKOUT_EXCLUSIONS = (
    ":(exclude,glob,top).claude/worktrees/**",
    ":(exclude,glob,top)**/.claude/worktrees/**",
    ":(exclude,glob,top).worktrees/**",
    ":(exclude,glob,top)**/.worktrees/**",
)


class Counts(TypedDict):
    all_match_lines: int
    unclassified_lines: int
    deliberate_alias_lines: int
    deliberate_floor_lines: int
    marker_metadata_lines: int


class Match(TypedDict):
    path: str
    text: str


class ControlReport(TypedDict):
    pattern: str
    matched_lines: int
    query_shape: str


class RepositoryReport(TypedDict):
    name: str
    ref: str
    commit: str
    counts: Counts
    top_level: dict[str, Counts]
    control: ControlReport


class BaselineComparison(TypedDict):
    remaining: int
    cleared: int
    cleared_percent: float


class BaselineReport(TypedDict):
    original: int
    provenance_verified: bool
    note: str
    raw_matches: BaselineComparison


class CensusPayload(TypedDict):
    schema: str
    query: dict[str, object]
    repositories: list[RepositoryReport]
    aggregate: Counts
    baseline: NotRequired[BaselineReport]


class CensusError(RuntimeError):
    """The requested ref could not be measured safely."""


def _empty_counts() -> Counts:
    return {
        "all_match_lines": 0,
        "unclassified_lines": 0,
        "deliberate_alias_lines": 0,
        "deliberate_floor_lines": 0,
        "marker_metadata_lines": 0,
    }


def _git(
    repo: Path,
    args: list[str],
    *,
    no_match_is_empty: bool = False,
) -> bytes:
    command = ["git", "-C", str(repo), *args]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode == 0:
        return result.stdout
    if no_match_is_empty and result.returncode == 1:
        return b""
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    raise CensusError(f"{' '.join(command)} failed ({result.returncode}): {detail}")


def _repo_root(path: str) -> Path:
    requested = Path(path).expanduser().resolve()
    root = _git(requested, ["rev-parse", "--show-toplevel"])
    return Path(root.decode("utf-8", errors="strict").strip())


def _validated_ref(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise CensusError("ref must be non-empty and have no outer whitespace")
    if value.startswith("-") or "\0" in value:
        raise CensusError(f"ref must not be a git option: {value!r}")
    return value


def _resolve_commit(repo: Path, ref: str) -> str:
    value = _validated_ref(ref)
    resolved = _git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}"],
    )
    return resolved.decode("ascii", errors="strict").strip()


def _grep_lines(repo: Path, commit: str, pattern: str) -> list[Match]:
    """Run the census query and parse git's NUL-delimited line records."""
    output = _git(
        repo,
        [
            "grep",
            "-I",
            "-i",
            "-n",
            "-z",
            "--full-name",
            "-e",
            pattern,
            commit,
            "--",
            ":/",
            *_NESTED_CHECKOUT_EXCLUSIONS,
        ],
        no_match_is_empty=True,
    )
    prefix = f"{commit}:".encode()
    matches: list[Match] = []
    cursor = 0
    while cursor < len(output):
        path_end = output.find(b"\0", cursor)
        line_end = output.find(b"\0", path_end + 1)
        text_end = output.find(b"\n", line_end + 1)
        if min(path_end, line_end, text_end) < 0:
            raise CensusError("git grep returned a malformed NUL-delimited record")
        raw_path = output[cursor:path_end]
        raw_text = output[line_end + 1 : text_end]
        if not raw_path.startswith(prefix):
            raise CensusError("git grep returned a path outside the requested ref")
        matches.append(
            {
                "path": raw_path[len(prefix) :].decode(
                    "utf-8", errors="surrogateescape"
                ),
                "text": raw_text.decode("utf-8", errors="replace"),
            }
        )
        cursor = text_end + 1
    return matches


def _top_level(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else "."


def _marker_meta_paths(repo: Path, commit: str) -> frozenset[str]:
    """Read analytics-only marker paths from the same ref being counted."""
    path = "scripts/legacy_name_ratchet.json"
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return frozenset(ratchet._MARKER_SYSTEM_PATHS)
    try:
        config = json.loads(result.stdout)
        configured = config.get("marker_inventory_meta_paths", [])
    except (AttributeError, TypeError, ValueError) as exc:
        raise CensusError(f"cannot read {path} from {commit}: {exc}") from exc
    if not isinstance(configured, list) or not all(
        isinstance(item, str) for item in configured
    ):
        raise CensusError(
            f"marker_inventory_meta_paths in {path} at {commit} must be a string list"
        )
    return frozenset(ratchet._MARKER_SYSTEM_PATHS) | frozenset(configured)


def _classification(
    match: Match, marker_meta_paths: frozenset[str]
) -> tuple[CountField, ...]:
    text = match["text"]
    kind = ratchet._kind(text)
    if match["path"] in marker_meta_paths and kind is not None:
        return ("all_match_lines", "marker_metadata_lines")
    if kind == ratchet.EXEMPT_MARKER:
        return ("all_match_lines", "deliberate_alias_lines")
    if kind == ratchet.FLOOR_MARKER:
        return ("all_match_lines", "deliberate_floor_lines")
    return ("all_match_lines", "unclassified_lines")


def _increment(counts: Counts, fields: tuple[CountField, ...]) -> None:
    for field in fields:
        counts[field] += 1


def _sum_counts(items: list[Counts]) -> Counts:
    total = _empty_counts()
    for item in items:
        for field in _COUNT_FIELDS:
            total[field] += item[field]
    return total


def _census_repo(name: str, path: str, ref: str) -> RepositoryReport:
    repo = _repo_root(path)
    commit = _resolve_commit(repo, ref)
    matches = _grep_lines(repo, commit, ratchet.LEGACY_NAME)
    control = _grep_lines(repo, commit, ratchet.NEW_NAME)
    if not control:
        raise CensusError(
            f"{name} @ {ref}: positive control {ratchet.NEW_NAME!r} matched no "
            "lines through the census query; refusing to trust the result"
        )

    marker_meta_paths = _marker_meta_paths(repo, commit)
    counts = _empty_counts()
    top_level: dict[str, Counts] = {}
    for match in matches:
        fields = _classification(match, marker_meta_paths)
        _increment(counts, fields)
        directory = _top_level(match["path"])
        _increment(top_level.setdefault(directory, _empty_counts()), fields)

    return {
        "name": name,
        "ref": ref,
        "commit": commit,
        "counts": counts,
        "top_level": dict(sorted(top_level.items())),
        "control": {
            "pattern": ratchet.NEW_NAME,
            "matched_lines": len(control),
            "query_shape": "same git grep options, ref, pathspec, and exclusions",
        },
    }


def _baseline_comparison(baseline: int, remaining: int) -> BaselineComparison:
    cleared = baseline - remaining
    return {
        "remaining": remaining,
        "cleared": cleared,
        "cleared_percent": round(cleared * 100 / baseline, 1),
    }


def _payload(
    repositories: list[RepositoryReport], baseline: int | None
) -> CensusPayload:
    aggregate = _sum_counts([repo["counts"] for repo in repositories])
    definition_path = Path(ratchet.__file__)
    result: CensusPayload = {
        "schema": "legacy-name-census/v1",
        "query": {
            "method": "git grep against each resolved commit",
            "case_insensitive": True,
            "unit": "matching lines",
            "definition_source": {
                "repository": "caura-ai/caura",
                "module": "scripts/legacy_name_ratchet.py",
                "sha256": hashlib.sha256(definition_path.read_bytes()).hexdigest(),
                "symbols": ["LEGACY_NAME", "NEW_NAME", "_kind"],
                "note": "The org-required gate uses this canonical OSS engine.",
            },
            "excluded_paths": list(_NESTED_CHECKOUT_EXCLUSIONS),
            "aggregation_note": (
                "Raw per-repository counts are summed for baseline comparison; "
                "vendored/generated content can overlap between repositories."
            ),
        },
        "repositories": repositories,
        "aggregate": aggregate,
    }
    if baseline is not None:
        result["baseline"] = {
            "original": baseline,
            "provenance_verified": False,
            "note": "The historical query, refs, and exclusions were not supplied.",
            "raw_matches": _baseline_comparison(baseline, aggregate["all_match_lines"]),
        }
    return result


def _print_counts(prefix: str, counts: Counts) -> None:
    print(
        f"{prefix}{counts['all_match_lines']:,} raw matches; "
        f"{counts['unclassified_lines']:,} unclassified candidate debt; "
        f"{counts['deliberate_alias_lines']:,} deliberate aliases; "
        f"{counts['deliberate_floor_lines']:,} deliberate floor mentions; "
        f"{counts['marker_metadata_lines']:,} marker metadata"
    )


def _print_human(payload: CensusPayload) -> None:
    print("Legacy-name census (matching lines in named git refs)")
    for repo in payload["repositories"]:
        print(f"\n{repo['name']} @ {repo['ref']} ({repo['commit'][:12]})")
        control = repo["control"]
        print(
            f"Control: {control['matched_lines']:,} line(s) matched "
            f"{control['pattern']!r} with the same query shape."
        )
        _print_counts("Total: ", repo["counts"])
        print("By top-level directory:")
        for directory, counts in repo["top_level"].items():
            _print_counts(f"  {directory}: ", counts)

    print("\nAggregate")
    _print_counts("Total: ", payload["aggregate"])
    baseline = payload.get("baseline")
    if baseline is not None:
        print(
            f"Baseline {baseline['original']:,} comparison only; historical query "
            "provenance was not supplied:"
        )
        comparison = baseline["raw_matches"]
        print(
            f"  raw matches: {comparison['remaining']:,} remaining, "
            f"{comparison['cleared']:,} cleared "
            f"({comparison['cleared_percent']:.1f}%)"
        )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Count legacy-name lines in one or more named git refs."
    )
    parser.add_argument(
        "--repo",
        action="append",
        nargs=3,
        required=True,
        metavar=("NAME", "PATH", "REF"),
        help="repository label, local checkout path, and ref to scan; repeatable",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--baseline",
        type=_positive_int,
        help="show arithmetic comparisons to an unverified historical baseline",
    )
    args = parser.parse_args()

    names = [spec[0] for spec in args.repo]
    if len(names) != len(set(names)):
        parser.error("--repo NAME values must be unique")

    try:
        repositories = [_census_repo(*spec) for spec in args.repo]
    except (CensusError, OSError, UnicodeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2

    payload = _payload(repositories, args.baseline)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
