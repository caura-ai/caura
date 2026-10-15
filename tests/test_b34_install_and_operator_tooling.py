"""Install and operator tooling does what its contract says (audit batch B34).

Each test names the finding it pins:

- L-55: the dev stack loaded the tracked ``.env.test`` last, so every
  ``docker compose -f docker-compose.dev.yml up`` ran with ``TESTING=1`` and
  ``IS_STANDALONE=false``, and a developer's ``.env``/``.env.local`` could not
  change either.
- L-59: ``backup-db.sh`` read only the legacy ``ALLOYDB_*`` names, falling
  back to a role and database the shipped stack no longer creates, so against
  that stack (``POSTGRES_*``, ``caura``/``caura``) ``pg_dump`` failed and no
  backup was taken.
- L-61: ``mcp_tool_smoke.py`` seeded memories through a hardcoded
  ``http://localhost`` whatever ``--url`` named, and an unreachable host
  crashed it with a traceback; a failed seed went unnoticed.
- L-62: ``trust_matrix_e2e.py`` printed ``MISMATCHES FOUND`` and exited 0.
- L-71: the generated plugin installer wrote caret ranges that had drifted to
  TypeScript 5, fetched no lockfile and ran ``npm install ... || true``.
  Eldad chose (2026-10-11) to install from the lockfile CI builds with.
- L-126: the installer's fallback ``ROOT_FILES`` lacked
  ``skills/caura/SKILL.md``.
- L-144: ``backfill_embeddings``' Usage block, and the local-embedder re-run
  hint, ran the backfill without the provider settings, so it always refused.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"b34_{name}", ROOT / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- L-55: the dev stack loads no test settings ------------------------------


def test_l55_the_dev_stack_loads_no_test_settings():
    compose = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text())
    for name, service in compose["services"].items():
        chain = [
            entry if isinstance(entry, str) else entry["path"]
            for entry in service.get("env_file", [])
        ]
        assert ".env.test" not in chain, f"{name} loads .env.test"
        # ``.env`` and ``.env.local`` are the developer's own; the tracked
        # files in the chain must not switch the testing routes on.
        for env_file in set(chain) - {".env", ".env.local"}:
            settings = (ROOT / env_file).read_text().splitlines()
            assert "TESTING=1" not in settings, f"{name}: {env_file} sets TESTING=1"


# --- L-59: backup-db.sh connects to the stack it ships with ------------------

_PG_DUMP = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$PG_DUMP_ARGS"
printf '%s' "$PGPASSWORD" > "$PG_DUMP_ARGS.password"
while [ $# -gt 0 ]; do
  if [ "$1" = -f ]; then : > "$2"; fi
  shift
done
"""


def _backup(tmp_path: Path, **env: str) -> tuple[dict[str, str], str]:
    """Run backup-db.sh against a recording ``pg_dump``, in an empty project
    with no ``.env``, and an environment holding only ``env``."""
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "backup-db.sh"
    shutil.copy(ROOT / "scripts" / "backup-db.sh", script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pg_dump").write_text(_PG_DUMP)
    (bin_dir / "pg_dump").chmod(0o755)
    args = tmp_path / "pg_dump.args"
    base = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    subprocess.run(
        ["bash", str(script)],
        env={**base, "PG_DUMP_ARGS": str(args), **env},
        check=True,
        capture_output=True,
    )
    argv = args.read_text().splitlines()
    connection = {flag: argv[argv.index(flag) + 1] for flag in ("-h", "-p", "-U", "-d")}
    return connection, Path(f"{args}.password").read_text()


def test_l59_backup_reads_the_stacks_postgres_settings(tmp_path):
    connection, password = _backup(
        tmp_path,
        POSTGRES_HOST="db.internal",
        POSTGRES_PORT="5433",
        POSTGRES_USER="caura",
        POSTGRES_PASSWORD="from-env",
        POSTGRES_DB="caura",
    )

    assert connection == {
        "-h": "db.internal",
        "-p": "5433",
        "-U": "caura",
        "-d": "caura",
    }
    assert password == "from-env"


def test_l59_backup_defaults_to_the_shipped_role_and_database(tmp_path):
    connection, _ = _backup(tmp_path)

    assert connection["-U"] == "caura"
    assert connection["-d"] == "caura"


def test_l59_backup_still_reads_the_legacy_names(tmp_path):
    """The control: a hosted environment that sets only ALLOYDB_* keeps working."""
    connection, password = _backup(
        tmp_path,
        ALLOYDB_HOST="10.0.0.5",
        ALLOYDB_USER="svc",
        ALLOYDB_DATABASE="prod",
        ALLOYDB_PASSWORD="legacy",
    )

    assert connection == {"-h": "10.0.0.5", "-p": "5432", "-U": "svc", "-d": "prod"}
    assert password == "legacy"


# --- L-61: mcp_tool_smoke seeds the stack it tests ---------------------------


@pytest.fixture
def smoke(monkeypatch):
    module = _load_script("mcp_tool_smoke")
    monkeypatch.setattr(module, "CORE_API", "http://core.example:8000", raising=False)
    monkeypatch.setattr(module, "TENANT", "b34-tenant", raising=False)
    return module


def _seed_with(smoke, monkeypatch, answer):
    urls: list[str] = []

    def http(method, url, body=None, headers=None):
        urls.append(url)
        return answer(method, url)

    monkeypatch.setattr(smoke, "http", http)
    return urls


def test_l61_every_seed_goes_to_the_url_under_test(smoke, monkeypatch):
    def ok(method, url):
        return (200, {"items": [{"id": "m-1"}]}) if method == "GET" else (201, {})

    urls = _seed_with(smoke, monkeypatch, ok)

    smoke.seed_fleet_and_memories()

    assert urls
    assert {urlsplit(url)[:2] for url in urls} == {("http", "core.example:8000")}, urls


def test_l61_a_failed_seed_stops_the_run(smoke, monkeypatch):
    def memory_seed_fails(method, url):
        return (500, {"detail": "boom"}) if "/memories" in url else (201, {})

    _seed_with(smoke, monkeypatch, memory_seed_fails)

    with pytest.raises(SystemExit) as stopped:
        smoke.seed_fleet_and_memories()
    assert stopped.value.code not in (0, None)


def _duplicate(code: str) -> dict:
    return {"detail": "Duplicate memory exists", "error": {"code": code}}


def test_l61_a_rerun_finds_its_memories_already_seeded(smoke, monkeypatch):
    """The seed content is fixed, so a second run against the same tenant is
    answered 409 DUPLICATE_MEMORY: the memory is there, which is all seeding
    needs."""

    def rerun(method, url):
        if method == "POST" and url.endswith("/api/v1/memories"):
            return 409, _duplicate("DUPLICATE_MEMORY")
        return (200, {"items": [{"id": "m-1"}]}) if method == "GET" else (200, {})

    _seed_with(smoke, monkeypatch, rerun)

    assert smoke.seed_fleet_and_memories()["memory_id"] == "m-1"


def test_l61_another_conflict_still_stops_the_run(smoke, monkeypatch):
    def conflict(method, url):
        if method == "POST" and url.endswith("/api/v1/memories"):
            return 409, _duplicate("CONFLICT")
        return 200, {}

    _seed_with(smoke, monkeypatch, conflict)

    with pytest.raises(SystemExit):
        smoke.seed_fleet_and_memories()


def test_l61_an_unreachable_host_is_a_status_not_a_traceback(smoke, monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(smoke.urllib.request, "urlopen", refuse)

    status, body = smoke.http("GET", "http://core.example:8000/healthz")

    assert status == 0
    assert "connection refused" in json.dumps(body)


# --- L-62: a trust-ladder mismatch fails the run -----------------------------


@pytest.mark.parametrize(
    ("grid", "fleetless", "liveness", "code"),
    [
        (False, True, True, 1),
        (True, False, True, 1),
        (True, True, False, 1),
        (True, True, True, 0),
    ],
)
def test_l62_the_exit_code_reports_the_verdict(
    monkeypatch, tmp_path, grid, fleetless, liveness, code
):
    matrix = _load_script("trust_matrix_e2e")
    env_file = tmp_path / "e2e.env"
    env_file.write_text("KEY=b34-key\nTENANT_ID=b34-tenant\n")
    monkeypatch.setattr(matrix, "provision", lambda: {})
    monkeypatch.setattr(matrix, "run_grid", lambda ids: grid)
    monkeypatch.setattr(matrix, "run_fleetless", lambda: fleetless)
    monkeypatch.setattr(matrix, "run_read_liveness", lambda ids: liveness)

    assert matrix.main(["--env-file", str(env_file)]) == code


# --- L-71 and L-126: the installer builds what CI built -----------------------


@pytest.fixture
def install_script(monkeypatch):
    from core_api.config import settings
    from core_api.routes.plugin import _generate_install_script

    monkeypatch.setattr(
        settings, "installer_allowed_api_urls", "https://caura.example.com"
    )
    return _generate_install_script(
        api_url="https://caura.example.com",
        api_key="sk-test-key-1234",
        fleet_id="b34-fleet",
        tenant_id="",
        node_name="b34-node",
        tls_bootstrap="verify",
    )


def _heredoc(script: str, target: str, marker: str) -> str:
    pattern = (
        rf'cat > "\$PLUGIN_DIR/{re.escape(target)}" '
        rf"<< '?{marker}'?\n(.*?)\n{marker}\n"
    )
    match = re.search(pattern, script, re.DOTALL)
    assert match, f"no {target} heredoc in the install script"
    return match.group(1)


def test_l71_the_installer_declares_the_plugins_own_toolchain(install_script):
    written = json.loads(_heredoc(install_script, "package.json", "PACKAGE_EOF"))
    plugin = json.loads((PLUGIN / "package.json").read_text())

    assert written["devDependencies"] == plugin["devDependencies"]
    # TypeScript 7 includes no @types unless ``types`` names them; the
    # installer compiles with the configuration CI compiles with.
    tsconfig = json.loads(_heredoc(install_script, "tsconfig.json", "TSCONFIG_EOF"))
    assert tsconfig == json.loads((PLUGIN / "tsconfig.json").read_text())


def test_l71_the_installer_installs_from_the_lockfile_or_stops(install_script):
    assert "plugin-source?file=package-lock.json" in install_script
    installs = [
        line.strip()
        for line in install_script.splitlines()
        if re.match(r"\s*npm (ci|install)\b", line)
    ]
    assert installs, "the installer installs nothing"
    assert all(line.startswith("npm ci") for line in installs), installs
    # TypeScript 7's compiler ships as platform-specific optional packages.
    assert not [line for line in installs if "optional" in line], installs
    assert not [line for line in installs if "|| true" in line], installs


@pytest.fixture
def lockfile() -> str:
    return (PLUGIN / "package-lock.json").read_text()


async def test_l71_the_lockfile_is_served_but_not_deployed(lockfile):
    """Served to the installer, and kept out of the deploy manifest and its
    hash: a lockfile bump must not redeploy every node."""
    from core_api.routes import plugin as plugin_mod

    served = await plugin_mod.plugin_source(file="package-lock.json")

    assert served == lockfile
    assert "package-lock.json" not in plugin_mod._plugin_root_files
    manifest = await plugin_mod.plugin_manifest()
    assert "package-lock.json" not in json.dumps(manifest)


def test_l126_the_fallback_root_files_are_the_served_ones(install_script):
    from core_api.routes.plugin import _plugin_root_files

    match = re.search(r'ROOT_FILES="([^"]+)"', install_script)
    assert match, "no ROOT_FILES fallback in the install script"
    assert set(match.group(1).split()) == _plugin_root_files


# --- L-144: the backfill is run with the provider settings -------------------


def test_l144_every_documented_backfill_run_loads_the_env():
    from core_storage_api.scripts import backfill_embeddings

    usage = backfill_embeddings.__doc__ or ""
    runs = [
        line
        for line in usage.splitlines()
        if "docker compose run" in line and "core-storage-api" in line
    ]
    assert runs, "the Usage block shows no docker compose run"
    assert all("--env-from-file .env" in line for line in runs), runs

    hint = (ROOT / "docs" / "local-embedder.md").read_text()
    rerun = hint[hint.index("re-run") :][:300]
    assert "--env-from-file .env" in rerun, rerun
