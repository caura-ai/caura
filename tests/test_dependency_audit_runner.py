"""Offline fail-closed checks and opt-in real advisory tests in disposable branches."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = os.environ.get("RUN_LIVE_DEPENDENCY_TESTS") == "1"
ENTERPRISE = (ROOT / "platform-collaboration-api").exists()
ADVISORY = "GHSA-xxxx-xxxx-xxxx"


class AuditRunner(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.output = self.directory / "reports"
        self.policy = self.repo / ".github/security-scan/npm-exceptions.json"
        self.policy.parent.mkdir(parents=True)
        self.policy.write_text("[]")
        subprocess.run(
            ["git", "init", "-q", "-b", "seeded-vulnerable-version", str(self.repo)],
            check=True,
        )
        (self.repo / "package.json").write_text(
            '{"name":"audit-fixture","version":"1.0.0","private":true}'
        )
        (self.repo / "package-lock.json").write_text("{}")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}

    def fake_npm(self, payload, status):
        response = self.directory / "response.json"
        response.write_text(
            json.dumps(payload) if not isinstance(payload, str) else payload
        )
        executable = self.bin / "npm"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import sys\nfrom pathlib import Path\n"
            f"print(Path({str(response)!r}).read_text())\nsys.exit({status})\n"
        )
        executable.chmod(0o700)

    def run_npm(self):
        script = ROOT / "scripts/audit_npm_dependencies.py"
        self.assertTrue(script.is_file(), "no npm audit runner")
        return subprocess.run(
            [
                sys.executable,
                str(script),
                "--repo",
                str(self.repo),
                "--output",
                str(self.output),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def commit_fixture(self):
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Dependency test",
                "-c",
                "user.email=dependency@example.invalid",
                "commit",
                "-qm",
                "Seed dependency fixture",
            ],
            check=True,
        )

    def response(self, severity="high"):
        return {
            "auditReportVersion": 2,
            "vulnerabilities": {
                "fixture": {
                    "name": "fixture",
                    "severity": severity,
                    "range": "<2",
                    "via": [
                        {
                            "name": "fixture",
                            "severity": severity,
                            "url": f"https://github.com/advisories/{ADVISORY}",
                        }
                    ],
                }
            },
            "metadata": {"vulnerabilities": {"total": 1, severity: 1}},
        }

    def test_a_high_advisory_fails(self):
        self.fake_npm(self.response(), 1)
        self.assertEqual(self.run_npm().returncode, 1)
        self.assertTrue(list(self.output.glob("*.json")))

    def test_low_advisory_does_not_cross_the_high_threshold(self):
        self.fake_npm(self.response("low"), 0)
        self.assertEqual(self.run_npm().returncode, 0)

    def test_clean_response_passes(self):
        self.fake_npm(
            {
                "auditReportVersion": 2,
                "vulnerabilities": {},
                "metadata": {"vulnerabilities": {"total": 0}},
            },
            0,
        )
        self.assertEqual(self.run_npm().returncode, 0)

    def test_registry_errors_fail_instead_of_looking_clean(self):
        for payload, status in (
            ({"error": {"code": "ENETWORK"}}, 1),
            ("not json", 1),
            ({}, 0),
        ):
            with self.subTest(payload=payload):
                self.fake_npm(payload, status)
                self.assertNotEqual(self.run_npm().returncode, 0)

    def test_expired_exception_fails(self):
        self.fake_npm(self.response(), 1)
        self.policy.write_text(
            json.dumps(
                [
                    {
                        "lockfile": "package-lock.json",
                        "advisory": ADVISORY,
                        "reason": "fixture only",
                        "expires": "2000-01-01",
                    }
                ]
            )
        )
        self.assertNotEqual(self.run_npm().returncode, 0)

    def test_exception_is_scoped_to_one_lock_and_advisory(self):
        self.fake_npm(self.response(), 1)
        entry = {
            "lockfile": "package-lock.json",
            "advisory": ADVISORY,
            "reason": "fixture only",
            "expires": "2099-01-01",
        }
        self.policy.write_text(json.dumps([entry]))
        self.assertEqual(self.run_npm().returncode, 0)
        entry["lockfile"] = "other/package-lock.json"
        self.policy.write_text(json.dumps([entry]))
        self.assertNotEqual(self.run_npm().returncode, 0)

    @unittest.skipUnless(ENTERPRISE, "enterprise deployment audit")
    def test_python_scanner_failures_and_findings_are_distinct(self):
        script = ROOT / "scripts/audit_python_dependencies.py"
        self.assertTrue(script.is_file(), "no Python audit runner")
        requirements = self.repo / "pinned.txt"
        requirements.write_text("fixture==1.0.0\n")
        stub = self.bin / "uv"
        for status in (0, 1, 2):
            with self.subTest(status=status):
                payload = {
                    "dependencies": [
                        {
                            "name": "fixture",
                            "version": "1.0.0",
                            "vulns": [{"id": ADVISORY}] if status == 1 else [],
                        }
                    ]
                }
                stub.write_text(
                    f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                    "args=sys.argv[1:]\nassert args[:2] == ['tool','run']\n"
                    "assert all(flag in args for flag in ['--python','--from','--strict','--no-deps','--disable-pip'])\n"
                    f"Path(args[args.index('--output')+1]).write_text({json.dumps(payload)!r})\n"
                    f"print('scanner diagnostic')\nsys.exit({status})\n"
                )
                stub.chmod(0o700)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "--repo",
                        str(self.repo),
                        "--requirements",
                        str(requirements),
                        "--output",
                        str(self.output),
                    ],
                    env=self.env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, status)
                self.assertIn(
                    "scanner diagnostic", (self.output / "requirements.log").read_text()
                )

    @unittest.skipUnless(LIVE, "real registry check runs in the dependency CI job")
    def test_real_npm_advisory_fails_in_a_seeded_branch(self):
        (self.repo / "package-lock.json").unlink()
        (self.repo / "package.json").write_text(
            json.dumps(
                {
                    "name": "audit-fixture",
                    "version": "1.0.0",
                    "private": True,
                    "dependencies": {"lodash": "4.17.20"},
                }
            )
        )
        result = subprocess.run(
            [
                "npm",
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--userconfig=/dev/null",
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0, "could not prepare the known-vulnerable lock"
        )
        self.commit_fixture()
        self.assertEqual(self.run_npm().returncode, 1)

    @unittest.skipUnless(
        LIVE and ENTERPRISE, "real frozen-Python audit runs in enterprise dependency CI"
    )
    def test_real_python_advisory_fails_in_a_seeded_branch(self):
        script = ROOT / "scripts/audit_python_dependencies.py"
        self.assertTrue(script.is_file(), "no Python audit runner")
        requirements = self.repo / "vulnerable.txt"
        requirements.write_text("urllib3==1.25.0\n")
        self.commit_fixture()
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--repo",
                str(self.repo),
                "--requirements",
                str(requirements),
                "--output",
                str(self.output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        reports = [json.loads(path.read_text()) for path in self.output.glob("*.json")]
        self.assertTrue(
            any(
                v.get("vulns")
                for report in reports
                for v in report.get("dependencies", [])
            )
        )


if __name__ == "__main__":
    unittest.main()
