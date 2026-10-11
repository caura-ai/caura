"""`caura-bus join`: dashboard sign-in, agent key mint, in-process setup.

The dashboard (login, API credentials) and the bus (identity, description) are
both mocked transports; every test runs with a temporary HOME and a fake
`claude` binary, so no real runtime configuration is touched.
"""

import json
import logging
import os
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
from caura_bus_cli import join as join_mod
from caura_bus_cli import setup as setup_mod
from caura_bus_cli.main import app
from caura_bus_core import Bus
from typer.testing import CliRunner

URL = "https://caura.test"
EMAIL = "alice@example.com"
PASSWORD = "Pw-very-secret-123!"
ORG = "org-1"
USER = "user-alice"
TENANT = "caura-qa"


class Dashboard:
    """The platform auth + admin api-credentials surface the dashboard uses."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict]] = []
        self.creds: list[dict] = []
        self.keys: dict[str, dict] = {}  # raw key -> bus identity
        self.login_status = 200
        self.totp = None
        self.tenants = [TENANT, "personal-alice"]
        self.mint_status = 201
        self.logged_out = False

    def add(self, agent, created_by=USER, label="manual", revoked=False, tenant=TENANT):
        row = {
            "id": f"cred-{len(self.creds) + 1}",
            "kind": "agent_key",
            "home_tenant_id": tenant,
            "home_agent_id": agent,
            "created_by": created_by,
            "label": label,
            "key_prefix": f"mc_{len(self.creds) + 1:04d}",
            "created_at": f"2026-10-0{len(self.creds) + 1}T00:00:00Z",
            "revoked_at": "2026-10-09T00:00:00Z" if revoked else None,
        }
        self.creds.append(row)
        return row

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body, dict(request.headers)))
        creds = f"/api/v1/admin/orgs/{ORG}/api-credentials"
        if path == "/api/auth/user/login":
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={"detail": "Invalid credentials"})
            if body != {
                "email": EMAIL,
                "password": PASSWORD,
                **({"totp_code": body["totp_code"]} if "totp_code" in body else {}),
            }:
                return httpx.Response(401, json={"detail": "Invalid credentials"})
            if self.totp and body.get("totp_code") != self.totp:
                return httpx.Response(200, json={"ok": False, "totp_required": True})
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "user_id": USER,
                    "org_id": ORG,
                    "tenant_id": TENANT,
                    "must_change_password": False,
                },
                headers=[("set-cookie", "session_token=s1; Path=/"), ("set-cookie", "csrf_token=c1; Path=/")],
            )
        if path == "/api/auth/user/logout":
            self.logged_out = True
            return httpx.Response(200, json={"ok": True})
        if request.headers.get("cookie", "").find("session_token=s1") == -1:
            return httpx.Response(401, json={"detail": "not signed in"})
        if path == f"{creds}/tenant-options":
            return httpx.Response(200, json=[{"tenant_id": t} for t in self.tenants])
        if path == creds and request.method == "GET":
            params = request.url.params
            rows = [
                r
                for r in self.creds
                if r["home_tenant_id"] == params.get("home_tenant_id")
                and (params.get("include_revoked") == "true" or not r["revoked_at"])
            ]
            return httpx.Response(200, json={"items": rows})
        if request.method in {"POST", "DELETE"} and request.headers.get("x-csrf-token") != "c1":
            return httpx.Response(403, json={"detail": "CSRF"})
        if path == creds and request.method == "POST":
            if self.mint_status != 201:
                return httpx.Response(self.mint_status, json={"detail": "nope"})
            row = self.add(body["home_agent_id"], label=body["label"], tenant=body["home_tenant_id"])
            raw = f"mc_minted_{row['id']}_0123456789abcdef"
            self.keys[raw] = {
                "agent_id": body["home_agent_id"],
                "tenant_id": body["home_tenant_id"],
                "fleet_id": None,
            }
            return httpx.Response(201, json={**row, "raw_key": raw})
        if path.startswith(creds + "/") and request.method == "DELETE":
            cred_id = path.rsplit("/", 1)[1]
            for row in self.creds:
                if row["id"] == cred_id:
                    row["revoked_at"] = "2026-10-11T00:00:00Z"
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "Not Found"})

    def bus_handler(self, request: httpx.Request) -> httpx.Response:
        identity = self.keys.get(request.headers.get("X-API-Key", ""))
        if identity is None:
            return httpx.Response(401, json={"detail": "invalid key"})
        if request.url.path == "/api/v1/bus/identity":
            return httpx.Response(200, json=identity)
        if request.url.path == "/api/v1/bus/agents/me/description":
            return httpx.Response(
                200, json={**identity, "description": json.loads(request.content)["description"]}
            )
        return httpx.Response(404, json={"detail": "Not Found"})

    def minted(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/api-credentials")]

    def revoked(self):
        return [c[1].rsplit("/", 1)[1] for c in self.calls if c[0] == "DELETE"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "my-project"
    project.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    capture = tmp_path / "claude-calls.jsonl"
    claude = fakebin / "claude"
    claude.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        f"with open({str(capture)!r},'a') as f:\n"
        " f.write(json.dumps({'argv':sys.argv[1:],'env':{k:v for k,v in os.environ.items() if 'CAURA' in k}})+'\\n')\n"
        "sys.exit(int(os.environ.get('FAKE_CLAUDE_EXIT','0')) if sys.argv[1:3]==['mcp','add'] else 0)\n"
    )
    claude.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    for name in ("XDG_CONFIG_HOME", "CAURA_API_KEY", "CAURA_EMAIL", "CAURA_TOTP_CODE"):
        monkeypatch.delenv(name, raising=False)
    for name in ("CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CAURA_PASSWORD", raising=False)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{Path(sys.executable).parent}")
    dashboard = Dashboard()
    dash_transport = httpx.MockTransport(dashboard.handler)
    bus_transport = httpx.MockTransport(dashboard.bus_handler)
    real_session = join_mod.Session

    class MockedSession(real_session):
        def __init__(self, url, transport=None):
            super().__init__(url, dash_transport)

    class MockedBus(Bus):
        def __init__(self, config, *, api_key=None, transport=None, **_):
            super().__init__(config, api_key=api_key, transport=bus_transport)

    monkeypatch.setattr(join_mod, "Session", MockedSession)
    monkeypatch.setattr(setup_mod, "Bus", MockedBus)

    class Env:
        pass

    e = Env()
    e.home, e.project, e.capture, e.dashboard, e.tmp = home, project, capture, dashboard, tmp_path
    return e


def invoke(*args, runtime="claude", password=PASSWORD):
    argv = ["join", "--url", URL, "--email", EMAIL, *args]
    if runtime:
        argv += ["--runtime", runtime]
    # join pops CAURA_PASSWORD from the environment once read, so set it per run.
    return CliRunner().invoke(app, argv, env={"CAURA_PASSWORD": password} if password else None)


def claude_calls(capture: Path) -> list[dict]:
    return [json.loads(line) for line in capture.read_text().splitlines()] if capture.exists() else []


def all_text_under(root: Path) -> str:
    return "\n".join(p.read_text(errors="ignore") for p in root.rglob("*") if p.is_file())


def test_claude_join_signs_in_mints_and_runs_setup(env):
    result = invoke("--dir", str(env.project), "--description", "Owns the billing service")
    assert result.exit_code == 0, result.output
    d = env.dashboard
    (mint,) = d.minted()
    body = mint[2]
    assert body["kind"] == "agent_key" and body["home_tenant_id"] == TENANT
    assert body["capabilities"] == ["read", "write"]
    name = body["home_agent_id"]
    assert name.startswith("claude-my-project-") and len(name.rsplit("-", 1)[1]) == 4
    assert body["label"].startswith("caura-bus join: claude in my-project on ") and "[dir " in body["label"]
    assert mint[3]["x-csrf-token"] == "c1"
    assert d.logged_out
    raw = next(iter(d.keys))
    assert raw not in result.output and PASSWORD not in result.output

    config = env.home / ".config" / "caura-bus" / "agents" / "caura.test" / TENANT / f"{name}.toml"
    assert tomllib.loads(config.read_text())["agent"] == {"agent_id": name, "tenant_id": TENANT}
    assert config.with_suffix(".key").read_text().strip() == raw  # hooks on by default
    add = [c for c in claude_calls(env.capture) if c["argv"][:2] == ["mcp", "add"]]
    assert add and f"CAURA_API_KEY={raw}" in add[0]["argv"]
    assert (env.project / "CLAUDE.md").exists()
    assert (env.project / ".claude" / "settings.local.json").exists()
    assert f"Agent {name} is registered in workspace {TENANT}" in result.output
    assert "Restart this Claude Code session (or run /mcp)" in result.output
    assert "not loaded until the session restarts" in result.output
    assert "description registered" in result.output


def test_codex_join_writes_codex_config_and_prints_waker(env):
    result = invoke("--dir", str(env.project), runtime="codex")
    assert result.exit_code == 0, result.output
    name = env.dashboard.minted()[0][2]["home_agent_id"]
    assert name.startswith("codex-my-project-")
    codex = tomllib.loads((env.home / ".codex" / "config.toml").read_text())
    assert codex["mcp_servers"]["caura-bus"]["env"]["CAURA_API_KEY"] in env.dashboard.keys
    assert (env.project / "AGENTS.md").exists()
    assert "Restart Codex" in result.output
    assert "caura-bus wake --runtime codex --thread latest" in result.output


def test_no_hooks_skips_key_file(env):
    result = invoke("--dir", str(env.project), "--no-hooks")
    assert result.exit_code == 0, result.output
    assert not list(env.home.rglob("*.key"))
    assert not (env.project / ".claude" / "settings.local.json").exists()


def test_login_401_stops_before_anything(env):
    env.dashboard.login_status = 401
    result = invoke("--dir", str(env.project))
    assert result.exit_code == 1
    assert "sign-in failed: wrong email, password or code" in result.output
    assert PASSWORD not in result.output
    assert not env.dashboard.minted()
    assert not claude_calls(env.capture)
    assert not (env.project / "CLAUDE.md").exists()


def test_wrong_password_is_401_too(env):
    result = invoke("--dir", str(env.project), password="wrong-password-xyz")
    assert result.exit_code == 1 and "sign-in failed" in result.output
    assert "wrong-password-xyz" not in result.output


def test_password_never_in_argv_output_files_or_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    result = invoke("--dir", str(env.project), "--description", "x")
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output
    assert PASSWORD not in caplog.text
    for call in claude_calls(env.capture):
        assert PASSWORD not in json.dumps(call)
    assert PASSWORD not in all_text_under(env.home)
    assert PASSWORD not in all_text_under(env.project)
    # the password only travels in the login request body
    sent = [c for c in env.dashboard.calls if PASSWORD in json.dumps(c[2] or {})]
    assert [c[1] for c in sent] == ["/api/auth/user/login"]
    # and there is no way to pass it on the command line
    assert "--password" not in CliRunner().invoke(app, ["join", "--help"]).output


def test_no_password_and_no_terminal_fails_clearly(env, monkeypatch):

    def no_tty(label, secret=False):
        raise join_mod.JoinError(f"no terminal to ask for the {label.lower()}")

    monkeypatch.setattr("caura_bus_cli.main.tty_prompt", no_tty)
    result = invoke("--dir", str(env.project), password=None)
    assert result.exit_code == 1
    assert "CAURA_PASSWORD" in result.output
    assert not env.dashboard.calls


def test_totp_code_from_environment(env, monkeypatch):
    env.dashboard.totp = "123456"
    monkeypatch.setenv("CAURA_TOTP_CODE", "123456")
    result = invoke("--dir", str(env.project))
    assert result.exit_code == 0, result.output
    assert len([c for c in env.dashboard.calls if c[1] == "/api/auth/user/login"]) == 2


def test_name_owned_by_someone_else_is_refused(env):
    env.dashboard.add("bob-claude", created_by="user-bob")
    result = invoke("--dir", str(env.project), "--name", "bob-claude")
    assert result.exit_code == 1
    assert "created by someone else" in result.output
    assert not env.dashboard.minted()


def test_rejoin_by_name_rotates_only_join_keys_for_this_dir(env):
    assert invoke("--dir", str(env.project), "--name", "Alice Claude!").exit_code == 0
    first = env.dashboard.creds[-1]
    assert first["home_agent_id"] == "alice-claude"
    manual = env.dashboard.add("alice-claude", label="alice laptop (dashboard)")
    other_dir = env.tmp / "elsewhere"
    other_dir.mkdir()
    assert invoke("--dir", str(other_dir), "--name", "alice-claude").exit_code == 0
    elsewhere = env.dashboard.creds[-1]

    result = invoke("--dir", str(env.project), "--name", "alice-claude")
    assert result.exit_code == 0, result.output
    assert "sanitized" not in result.output
    assert "re-joined (new key)" in result.output
    assert env.dashboard.revoked() == [first["id"]]
    assert manual["revoked_at"] is None and elsewhere["revoked_at"] is None
    assert "left 2 other active key(s)" in result.output


def test_rejoin_without_name_reuses_this_dirs_agent(env):
    assert invoke("--dir", str(env.project)).exit_code == 0
    name = env.dashboard.creds[-1]["home_agent_id"]
    old = env.dashboard.creds[-1]["id"]
    result = invoke("--dir", str(env.project))
    assert result.exit_code == 0, result.output
    assert env.dashboard.creds[-1]["home_agent_id"] == name
    assert env.dashboard.revoked() == [old]
    # a different runtime in the same dir is a different agent
    assert invoke("--dir", str(env.project), runtime="codex").exit_code == 0
    assert env.dashboard.creds[-1]["home_agent_id"].startswith("codex-")


def test_setup_failure_revokes_the_new_key(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    result = invoke("--dir", str(env.project))
    assert result.exit_code == 1
    assert "the new key was revoked" in result.output
    assert env.dashboard.revoked() == [env.dashboard.creds[-1]["id"]]


def test_unknown_tenant_is_refused(env):
    result = invoke("--dir", str(env.project), "--tenant", "nope")
    assert result.exit_code == 1
    assert "not in your organization" in result.output and TENANT in result.output
    assert not env.dashboard.minted()


def test_explicit_tenant(env):
    result = invoke("--dir", str(env.project), "--tenant", "personal-alice")
    assert result.exit_code == 0, result.output
    assert env.dashboard.minted()[0][2]["home_tenant_id"] == "personal-alice"


def test_mint_forbidden_is_reported(env):
    env.dashboard.mint_status = 403
    result = invoke("--dir", str(env.project))
    assert result.exit_code == 1
    assert "minting the agent key failed: nope (HTTP 403)" in result.output


def test_auto_runtime_from_environment(env, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    result = invoke("--dir", str(env.project), runtime=None)
    assert result.exit_code == 0, result.output
    assert env.dashboard.minted()[0][2]["home_agent_id"].startswith("claude-")


def test_auto_runtime_none_detected(env):
    result = invoke("--dir", str(env.project), runtime=None)
    assert result.exit_code == 1
    assert "could not detect the runtime" in result.output
    assert not env.dashboard.calls


@pytest.mark.parametrize(
    ("env_vars", "ancestor", "expected"),
    [
        ({"CLAUDECODE": "1"}, None, "claude"),
        ({"CODEX_THREAD_ID": "t"}, None, "codex"),
        ({"CODEX_SANDBOX": "seatbelt"}, None, "codex"),
        ({"CODEX_SESSION_ID": "s"}, None, "codex"),
        ({"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}, "codex", "codex"),
        ({"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}, "claude", "claude"),
        # inherited non-markers do not count
        ({"CLAUDE_CODE_SESSION_ID": "x", "CODEX_HOME": "/h"}, None, None),
        ({"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}, None, None),
    ],
)
def test_detect_runtime(env_vars, ancestor, expected):
    if expected is None:
        with pytest.raises(join_mod.JoinError):
            join_mod.detect_runtime("auto", env_vars, lambda: ancestor)
    else:
        assert join_mod.detect_runtime("auto", env_vars, lambda: ancestor) == expected


def test_explicit_runtime_wins_over_environment():
    assert join_mod.detect_runtime("codex", {"CLAUDECODE": "1"}) == "codex"
    assert join_mod.detect_runtime("claude-code", {}) == "claude"
    with pytest.raises(join_mod.JoinError):
        join_mod.detect_runtime("cursor", {})


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Alice Claude!", "alice-claude"),
        ("a__b--c", "a-b-c"),
        ("--x--", "x"),
        ("é", ""),
        ("A" * 80, "a" * 64),
    ],
)
def test_sanitize_name(raw, clean):
    assert join_mod.sanitize_name(raw) == clean


def test_default_name_shape(tmp_path):
    name = join_mod.default_name("codex", tmp_path / "My Repo.git")
    stem, hexpart = name.rsplit("-", 1)
    assert stem == "codex-my-repo-git" and len(hexpart) == 4
    assert join_mod.default_name("claude", Path("/")).startswith("claude-agent-")
