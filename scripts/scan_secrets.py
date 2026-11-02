"""Run Gitleaks without publishing matched text or commit messages.

PR scans cover every commit in base..head. Scheduled scans cover all fetched
refs, including deleted files and archive members. Findings and scanner errors
both fail the gate; historical findings are never used as a PR baseline.
"""

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"RuleID", "File", "Commit", "StartLine", "Fingerprint"}
TEMPLATE = """[{{ range $i, $f := . }}{{if $i}},{{end}}
{"RuleID":{{toJson $f.RuleID}},"File":{{toJson $f.File}},
"Commit":{{toJson $f.Commit}},"StartLine":{{$f.StartLine}},
"Fingerprint":{{toJson $f.Fingerprint}}}{{end}}]
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("[]\n")
    if args.all:
        if args.head:
            parser.error("--head requires --base")
        log_opts = "--all"
    else:
        if not all(
            revision and re.fullmatch(r"[0-9a-fA-F]{40,64}", revision)
            for revision in (args.base, args.head)
        ):
            print("Secret scan failed: base and head must be full commit IDs.")
            return 2
        log_opts = f"{args.base}..{args.head}"

    # The custom report omits Match, Secret, Line, Message, author and email.
    # Even fully redacted standard JSON can contain adjacent, unmatched values.
    # Scanner diagnostics are deliberately discarded rather than uploaded.
    with tempfile.TemporaryDirectory(prefix="secret-scan-") as temporary:
        directory = Path(temporary)
        template = directory / "locations.tmpl"
        template.write_text(TEMPLATE)
        report = directory / "locations.json"
        try:
            result = subprocess.run(
                [
                    "gitleaks",
                    "git",
                    str(args.repo.resolve()),
                    f"--log-opts={log_opts}",
                    "--max-archive-depth=3",
                    "--redact=100",
                    "--no-banner",
                    "--log-level=error",
                    "--exit-code=10",
                    "--timeout=900",
                    "--config",
                    str(ROOT / ".gitleaks.toml"),
                    "--gitleaks-ignore-path",
                    str(ROOT / ".gitleaksignore"),
                    "--report-format=template",
                    "--report-template",
                    str(template),
                    "--report-path",
                    str(report),
                ],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=930,
            )
            if (
                result.returncode not in (0, 10)
                or result.stderr
                or not report.is_file()
            ):
                print(
                    "Secret scan failed: scanner did not complete; no diagnostics published."
                )
                return 2
            findings = json.loads(report.read_text())
            if not isinstance(findings, list) or any(
                not isinstance(finding, dict) or set(finding) != FIELDS
                for finding in findings
            ):
                print("Secret scan failed: invalid location report.")
                return 2
            if bool(findings) != (result.returncode == 10):
                print("Secret scan failed: inconsistent scanner outcome.")
                return 2
        except (OSError, ValueError, subprocess.TimeoutExpired):
            print("Secret scan failed: scanner unavailable or report unreadable.")
            return 2
        args.report.write_text(json.dumps(findings, indent=2) + "\n")
        print(
            f"Secret scan completed: {len(findings)} finding(s); see the location report."
        )
        return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
