"""Audit every tracked npm lock without installing or executing package code."""

import argparse
import datetime
import json
import re
import subprocess
from pathlib import Path

HIGH = {"high", "critical"}


def exceptions(path: Path) -> set[tuple[str, str]]:
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError("exception policy must be a list")
    accepted = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "lockfile",
            "advisory",
            "reason",
            "expires",
        }:
            raise ValueError("invalid exception fields")
        if not all(isinstance(value, str) for value in entry.values()):
            raise ValueError("invalid exception values")
        lock = Path(entry["lockfile"])
        if (
            lock.is_absolute()
            or ".." in lock.parts
            or "*" in str(lock)
            or lock.name != "package-lock.json"
        ):
            raise ValueError("exception must name one repository lock")
        if (
            not re.fullmatch(r"GHSA-[a-z0-9-]+", entry["advisory"])
            or not entry["reason"].strip()
        ):
            raise ValueError("exception needs an advisory and reason")
        expiry = datetime.date.fromisoformat(entry["expires"])
        if expiry < datetime.datetime.now(datetime.UTC).date():
            raise ValueError("expired exception")
        accepted.add((lock.as_posix(), entry["advisory"]))
    return accepted


def advisories(report: dict) -> set[str]:
    if report.get("auditReportVersion") != 2 or not isinstance(
        report.get("vulnerabilities"), dict
    ):
        raise ValueError("invalid npm audit report")
    vulnerabilities = report["vulnerabilities"]

    def visit(name: str, seen: set[str]) -> set[str]:
        if name in seen:
            return set()
        visited = seen | {name}
        finding = vulnerabilities[name]
        found = set()
        for source in finding["via"]:
            if isinstance(source, str):
                found.update(visit(source, visited))
            elif isinstance(source, dict):
                if source["severity"] in HIGH:
                    match = re.fullmatch(
                        r"https://github.com/advisories/(GHSA-[a-z0-9-]+)",
                        source["url"],
                    )
                    if not match:
                        raise ValueError("unrecognized advisory identifier")
                    found.add(match[1])
            else:
                raise ValueError("invalid advisory source")
        return found

    result = set()
    for name, finding in vulnerabilities.items():
        if finding["severity"] in HIGH:
            found = visit(name, set())
            if not found:
                raise ValueError("high severity finding has no advisory identifier")
            result.update(found)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.repo.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        accepted = exceptions(root / ".github/security-scan/npm-exceptions.json")
        tracked = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--",
                "package-lock.json",
                "**/package-lock.json",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        locks = sorted(set(filter(None, tracked.split("\0"))))
        if not locks:
            raise ValueError("no tracked npm locks")
    except (OSError, ValueError, subprocess.SubprocessError):
        print("npm audit failed: invalid policy or lock inventory.")
        return 2
    outcome = 0
    for lock in locks:
        destination = args.output / f"{lock.replace('/', '__')}.audit.json"
        try:
            result = subprocess.run(
                [
                    "npm",
                    "audit",
                    "--package-lock-only",
                    "--omit=dev",
                    "--audit-level=high",
                    "--json",
                    "--ignore-scripts",
                    "--userconfig=/dev/null",
                ],
                cwd=(root / lock).parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=180,
            )
            if result.returncode not in (0, 1):
                raise ValueError("npm audit did not complete")
            report = json.loads(result.stdout)
            if not isinstance(report, dict) or "error" in report:
                raise ValueError("npm audit error response")
            high = advisories(report)
            if result.returncode == 1 and not high:
                raise ValueError("npm audit failed without actionable findings")
            destination.write_text(json.dumps(report, indent=2) + "\n")
            unaccepted = high - {
                advisory for path, advisory in accepted if path == lock
            }
            print(f"{lock}: {len(unaccepted)} unaccepted high/critical advisories.")
            if unaccepted:
                outcome = max(outcome, 1)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            destination.write_text('{"error": "NPM_AUDIT_FAILED"}\n')
            print(f"{lock}: audit could not complete.")
            outcome = 2
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
