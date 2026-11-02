"""Executable contracts for dependency gates, image references and documentation assets."""

import importlib
import json
import re
import runpy
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
ENTERPRISE = (ROOT / "platform-collaboration-api").exists()


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/security-scan.yml").read_text())


def test_npm_locks_have_a_blocking_scheduled_audit():
    config = workflow()
    assert "schedule" in config.get("on", config.get(True, {}))
    job = config["jobs"].get("npm-audit")
    assert job, "no npm dependency gate"
    assert not job.get("continue-on-error", False)
    assert not job.get("if"), "unchanged locks must also be audited on the weekly run"
    steps = job["steps"]
    audit = next(
        (s for s in steps if "audit_npm_dependencies.py" in s.get("run", "")), None
    )
    assert audit and not audit.get("continue-on-error", False)
    assert config["permissions"] == {"contents": "read"}


def test_npm_exception_policy_is_committed_and_scoped():
    path = ROOT / ".github/security-scan/npm-exceptions.json"
    assert path.is_file(), "accepted advisories need a reviewable policy file"
    entries = json.loads(path.read_text())
    assert isinstance(entries, list)
    for entry in entries:
        assert set(entry) == {"lockfile", "advisory", "reason", "expires"}
        assert entry["reason"].strip()
        assert "*" not in entry["lockfile"]
        assert re.fullmatch(r"GHSA-[a-z0-9-]+", entry["advisory"])


def test_sensitive_paths_have_explicit_codeowners():
    path = ROOT / "CODEOWNERS"
    assert path.is_file(), "no repository ownership policy"
    rules = [
        line.split()
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    required = {
        "/.github/workflows/",
        "/terraform/",
        "/gateway/",
        "/common/license/",
        "/CODEOWNERS",
    }
    required |= (
        {"/platform-auth-api/"} if ENTERPRISE else {"/core-api/src/core_api/routes/"}
    )
    for prefix in required:
        matches = [r for r in rules if r[0] == prefix]
        assert matches and len(matches[-1]) > 1, f"no explicit owner for {prefix}"


def test_external_container_references_are_digest_pinned():
    paths = sorted(ROOT.glob("*/Dockerfile"))
    if ENTERPRISE:
        paths.append(ROOT / "frontend/site/Dockerfile")
    assert paths
    for path in paths:
        aliases = set()
        for line in path.read_text().splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0].upper() == "FROM":
                source = next(p for p in parts[1:] if not p.startswith("--"))
                internal = source in aliases or source == "dd-apm-${DD_APM_ENABLED}"
                if not internal:
                    assert re.search(r"@sha256:[a-f0-9]{64}$", source), (
                        f"{path.parent.name}: {source}"
                    )
                    if source.startswith("nginx:"):
                        assert re.match(r"nginx:\d+\.\d+", source), source
                if "AS" in [p.upper() for p in parts]:
                    aliases.add(parts[-1])
            if parts[0].upper() == "COPY":
                for source in re.findall(r"--from=([^\s]+)", line):
                    if "/" in source or ":" in source:
                        assert re.search(r"@sha256:[a-f0-9]{64}$", source), source


@pytest.mark.skipif(not ENTERPRISE, reason="enterprise deployment overlays")
def test_container_update_coverage_includes_the_overlays_and_forwarder():
    entries = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
    docker = next(entry for entry in entries if entry["package-ecosystem"] == "docker")
    assert set(docker["directories"]) >= {
        "/core-api",
        "/core-storage-api",
        "/scrunch-agent-traffic",
    }


@pytest.mark.skipif(not ENTERPRISE, reason="enterprise site")
def test_unused_diagram_component_and_registration_are_removed():
    manifest = json.loads((ROOT / "frontend/site/package.json").read_text())
    assert "mermaid" not in manifest.get("dependencies", {})
    assert not (ROOT / "frontend/site/components/mdx/mermaid.tsx").exists()
    assert "Mermaid" not in (ROOT / "frontend/site/components/mdx.tsx").read_text()
    lock = json.loads((ROOT / "frontend/site/package-lock.json").read_text())
    assert "node_modules/mermaid" not in lock["packages"]
    purifier = lock["packages"].get("node_modules/dompurify")
    if purifier:
        assert tuple(map(int, purifier["version"].split("."))) >= (3, 4, 17)


@pytest.mark.skipif(not ENTERPRISE, reason="enterprise frozen workspace")
def test_python_scanners_are_pinned_and_sources_follow_the_deployed_pin():
    requirements = ROOT / ".github/security-scan/requirements.txt"
    assert requirements.is_file(), "scanner versions float on every run"
    pins = requirements.read_text()
    for tool in ("bandit", "pip-audit"):
        assert re.search(rf"^{tool}==\d+\.\d+\.\d+$", pins, re.MULTILINE)
    job = workflow()["jobs"]["security-scan"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "scripts/checkout_collaboration_core.py" in runs
    assert "uv pip install -e" not in runs, (
        "fresh source resolution is not the shipped dependency set"
    )
    assert "platform-collaboration-api/src" in runs
    assert "scrunch-agent-traffic" in runs


@pytest.mark.skipif(not ENTERPRISE, reason="enterprise frozen workspace")
def test_audited_profiles_cover_every_shipped_extra_without_local_packages(tmp_path):
    script = ROOT / "scripts/audit_python_dependencies.py"
    assert script.is_file(), "no frozen deployment dependency audit"
    module = runpy.run_path(str(script))
    targets = module["targets"](ROOT)
    profiles = {target["name"]: target for target in targets}
    assert set(profiles) >= {
        "core-api",
        "core-storage-api",
        "platform-operations",
        "scrunch-agent-traffic",
    }
    for name, extra in (("core-api", "api"), ("core-storage-api", "storage")):
        target = profiles[name]
        assert target["package"] == "platform-collaboration-api"
        assert set(target["extras"]) >= {extra, "datadog"}
        command = module["export_command"](target, tmp_path / "requirements.txt")
        assert command[0:2] == ["uv", "export"]
        assert set(command) >= {"--frozen", "--no-dev", "--no-emit-local"}
        assert "--all-extras" not in command
    assert set(profiles["platform-operations"]["extras"]) >= {"pubsub", "datadog"}


@pytest.mark.skipif(not ENTERPRISE, reason="enterprise relock producer")
def test_relocked_dependencies_are_audited_before_upload_without_writer_credentials():
    config = yaml.safe_load(
        (ROOT / ".github/workflows/relock-python-deps.yml").read_text()
    )
    job = config["jobs"]["relock"]
    steps = job["steps"]
    upgrade = next(
        i for i, s in enumerate(steps) if "uv lock --upgrade" in s.get("run", "")
    )
    audits = [
        i
        for i, s in enumerate(steps)
        if "audit_python_dependencies.py" in s.get("run", "")
    ]
    assert audits, "a constrained vulnerable pin silently survives the weekly relock"
    upload = next(
        i for i, s in enumerate(steps) if "upload-artifact@" in s.get("uses", "")
    )
    assert upgrade < audits[0] < upload
    assert not steps[audits[0]].get("continue-on-error", False)
    assert "environment" not in job
    assert "create-github-app-token" not in str(job)


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if (tag == "script" and "src" in attributes) or (
            tag == "link" and attributes.get("rel") == "stylesheet"
        ):
            self.assets.append(attributes)


@pytest.mark.asyncio
@pytest.mark.skipif(ENTERPRISE, reason="public API documentation")
async def test_swagger_assets_use_exact_versions_and_integrity():
    from starlette.requests import Request

    app = importlib.import_module("core_api.app").app

    route = next(
        route for route in app.routes if getattr(route, "path", None) == "/api/docs"
    )
    response = await route.endpoint(Request({"type": "http", "root_path": "/prefix"}))
    parser = AssetParser()
    parser.feed(response.body.decode())
    assert len(parser.assets) == 2
    for asset in parser.assets:
        url = asset.get("src", asset.get("href"))
        assert re.search(r"swagger-ui-dist@\d+\.\d+\.\d+/", url), url
        assert re.fullmatch(r"sha384-[A-Za-z0-9+/]{64}", asset.get("integrity", ""))
        assert asset.get("crossorigin") == "anonymous"
    assert "/prefix/api/openapi.json" in response.body.decode()
    redirect = app.swagger_ui_oauth2_redirect_url
    assert redirect and any(
        getattr(route, "path", None) == redirect for route in app.routes
    )
    assert f"/prefix{redirect}" in response.body.decode()
