"""Commit safeguards, including real scanner runs against disposable Git history."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts/scan_secrets.py"
BINARY = os.environ.get("GITLEAKS_BINARY") or shutil.which("gitleaks")


class CommitSafeguards(unittest.TestCase):
    def test_security_workflow_runs_range_and_history_scans(self):
        workflow = (ROOT / ".github/workflows/security-scan.yml").read_text()
        self.assertIn("  secret-scan:", workflow)
        job = workflow.split("  secret-scan:", 1)[1]
        self.assertIn("fetch-depth: 0", job)
        self.assertIn("github.event.pull_request.base.sha", job)
        self.assertIn("github.event.pull_request.head.sha", job)
        self.assertIn("scan_secrets.py", job)
        self.assertIn("--all", job)
        self.assertNotIn("continue-on-error: true", job)
        self.assertIn("schedule:", workflow)

    def test_pre_commit_has_all_three_secret_hooks(self):
        hooks = (ROOT / ".pre-commit-config.yaml").read_text()
        for hook in ("gitleaks", "detect-private-key", "detect-aws-credentials"):
            with self.subTest(hook=hook):
                self.assertIn(f"- id: {hook}", hooks)
        self.assertIn("--allow-missing-credentials", hooks)
        self.assertIn("--redact", hooks)

    def test_sensitive_paths_are_ignored_without_hiding_templates(self):
        # No file is created or read at any of these hypothetical paths.
        private = (
            ".env.local",
            ".env.production",
            ".env.staging",
            "frontend/.env.production",
            "key.pem",
            "nested/foo.key",
            "sa-key.json",
            "my-service-account.json",
            "my-credentials.json",
            "terraform/secrets.auto.tfvars",
            "kubeconfig",
            "nested/kubeconfig.prod",
            "id_rsa",
            "nested/id_rsa.backup",
        )
        public = (".env.example", ".env.test", "frontend/.env.example")
        if (ROOT / "common/license/keys/license_public.pem").exists():
            public += ("common/license/keys/license_public.pem",)
        for expected, paths in ((0, private), (1, public)):
            for path in paths:
                with self.subTest(path=path):
                    result = subprocess.run(
                        ["git", "check-ignore", "--no-index", "--quiet", path],
                        cwd=ROOT,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected)

    def test_public_plugin_uses_synthetic_cloud_examples(self):
        plugin = ROOT / "plugin/src/identity.ts"
        if not plugin.exists():
            self.skipTest("public plugin is maintained in its own repository")
        for path in (plugin, plugin.with_name("identity.test.ts")):
            with self.subTest(path=path.name):
                self.assertIn("example-project.internal", path.read_text())


@unittest.skipUnless(BINARY, "set GITLEAKS_BINARY to run the scanner integration tests")
class ScannerIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Scanner test")
        self.git("config", "user.email", "scanner@example.invalid")
        # Construct a deliberately fake test value, never a credential from a developer environment.
        self.fake = (
            "ghp_" + hashlib.sha256(b"disposable scanner test value").hexdigest()[:36]
        )
        (self.repo / "README.md").write_text("test repository\n")
        self.base = self.commit()

    def git(self, *args):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "test change")
        return self.git("rev-parse", "HEAD")

    def scan(self, *args, binary=None):
        self.assertTrue(
            SCANNER.is_file(), "the repository has no secret scanner runner"
        )
        report = Path(self.temp.name) / "locations.json"
        executable = Path(binary or BINARY).resolve()
        binary_directory = Path(self.temp.name) / "scanner-bin"
        binary_directory.mkdir(exist_ok=True)
        (binary_directory / "gitleaks").symlink_to(executable)
        result = subprocess.run(
            [
                sys.executable,
                str(SCANNER),
                "--repo",
                str(self.repo),
                "--report",
                str(report),
                *args,
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": f"{binary_directory}{os.pathsep}{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotIn(self.fake, result.stdout + result.stderr)
        self.assertTrue(report.exists(), "scanner must leave a location report")
        text = report.read_text()
        self.assertNotIn(self.fake, text)
        findings = json.loads(text)
        for finding in findings:
            self.assertEqual(
                set(finding), {"RuleID", "File", "Commit", "StartLine", "Fingerprint"}
            )
        return result, findings

    def test_new_fake_key_fails_the_pr_range(self):
        (self.repo / "candidate.txt").write_text(self.fake)
        head = self.commit()
        result, findings = self.scan("--base", self.base, "--head", head)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(f["RuleID"] == "github-pat" for f in findings))

    def test_old_key_does_not_fail_a_clean_pr_range(self):
        (self.repo / "old.txt").write_text(self.fake)
        base = self.commit()
        (self.repo / "README.md").write_text("clean update\n")
        head = self.commit()
        result, findings = self.scan("--base", base, "--head", head)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(findings, [])

    def test_new_fake_oauth_secret_fails_the_range(self):
        self.fake = (
            "GOCSPX-" + hashlib.sha256(b"disposable oauth test value").hexdigest()[:28]
        )
        (self.repo / "candidate.txt").write_text(self.fake)
        head = self.commit()
        result, findings = self.scan("--base", self.base, "--head", head)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(
            any(f["RuleID"] == "google-oauth-client-secret" for f in findings)
        )

    def test_governance_fixture_path_does_not_exempt_a_new_key(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_governance_patterns.py").write_text(self.fake)
        head = self.commit()
        result, findings = self.scan("--base", self.base, "--head", head)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(findings)

    def test_key_added_then_removed_still_fails_the_range(self):
        (self.repo / "transient.txt").write_text(self.fake)
        self.commit()
        (self.repo / "transient.txt").unlink()
        head = self.commit()
        result, findings = self.scan("--base", self.base, "--head", head)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(findings)

    def test_full_history_finds_deleted_archive_on_another_branch(self):
        self.git("checkout", "-qb", "fixture")
        with zipfile.ZipFile(self.repo / "fixture.zip", "w") as archive:
            archive.writestr("payload.txt", self.fake)
        leaked = self.commit()
        (self.repo / "fixture.zip").unlink()
        self.commit()
        self.git("checkout", "-q", "--detach", self.base)
        result, findings = self.scan("--all")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(
            any(f["Commit"] == leaked and "!payload.txt" in f["File"] for f in findings)
        )

    def test_invalid_revision_fails_closed(self):
        result, findings = self.scan("--base", "missing", "--head", self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(findings, [])

    def test_scanner_failure_does_not_print_its_output(self):
        broken = Path(self.temp.name) / "broken-scanner"
        broken.write_text(f"#!/bin/sh\nprintf '%s' '{self.fake}' >&2\nexit 2\n")
        broken.chmod(0o700)
        result, findings = self.scan("--all", binary=broken)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(findings, [])

    def test_error_diagnostic_fails_even_if_scanner_returns_zero(self):
        broken = Path(self.temp.name) / "partial-scanner"
        broken.write_text(
            "#!/bin/sh\n"
            'while [ "$1" != "--report-path" ]; do shift; done\n'
            'printf "[]" > "$2"\n'
            f"printf '%s' '{self.fake}' >&2\nexit 0\n"
        )
        broken.chmod(0o700)
        result, findings = self.scan("--all", binary=broken)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
