"""`caura-bus setup`: server-verified identity, private config, runtime wiring, idempotency.

Every test runs with a temporary HOME and a fake `claude` binary, so no real
runtime configuration is read or written.
"""

import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
from caura_bus_cli import setup as setup_mod
from caura_bus_cli.main import app
from caura_bus_core import Bus
from typer.testing import CliRunner

KEY = "mc_testkey_0123456789abcdefSECRET"
URL = "https://caura.test"
IDENTITY = {"agent_id": "qa-alice", "tenant_id": "tenant-1", "fleet_id": None}
TEMPLATE = (
    Path(__file__).resolve().parents[3] / "docs" / "agent-collaboration" / "PEER_AGENT_CLAUDE_template.md"
)


class Server:
    def __init__(self, identity=None, describe_status=200):
        self.identity = identity or dict(IDENTITY)
        self.describe_status = describe_status
        self.calls: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.headers.get("X-API-Key") != KEY:
            return httpx.Response(401, json={"detail": "invalid key"})
        if request.url.path == "/api/v1/bus/identity":
            return httpx.Response(200, json=self.identity)
        if request.url.path == "/api/v1/bus/agents/me/description":
            if self.describe_status != 200:
                return httpx.Response(self.describe_status, json={"detail": "Not Found"})
            return httpx.Response(200, json={**self.identity, "description": body["description"]})
        return httpx.Response(404, json={"detail": "Not Found"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    capture = tmp_path / "claude-calls.jsonl"
    claude = fakebin / "claude"
    claude.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        f"with open({str(capture)!r},'a') as f:\n"
        " f.write(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd()})+'\\n')\n"
        "sys.exit(int(os.environ.get('FAKE_CLAUDE_EXIT','0')) if sys.argv[1:3]==['mcp','add'] else 0)\n"
    )
    claude.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CAURA_API_KEY", raising=False)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{Path(sys.executable).parent}")
    server = Server()
    transport = httpx.MockTransport(server.handler)

    class MockedBus(Bus):
        def __init__(self, config, *, api_key=None, transport_=None, **_):
            super().__init__(config, api_key=api_key, transport=transport)

    monkeypatch.setattr(setup_mod, "Bus", MockedBus)

    class Env:
        pass

    e = Env()
    e.home, e.project, e.capture, e.server, e.fakebin = home, project, capture, server, fakebin
    e.config = home / ".config" / "caura-bus" / "agents" / "caura.test" / "tenant-1" / "qa-alice.toml"
    e.codex = home / ".codex" / "config.toml"
    return e


def invoke(*args, key=KEY):
    argv = ["setup", "--url", URL, *args]
    if key is not None:
        argv += ["--key", key]
    return CliRunner().invoke(app, argv)


def calls(capture: Path) -> list[dict]:
    return [json.loads(line) for line in capture.read_text().splitlines()] if capture.exists() else []


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_claude_setup_verifies_writes_private_config_and_wires_local_mcp(env):
    (env.project / "CLAUDE.md").write_text("# Team rules\n\nKeep tests green.\n")
    result = invoke("--runtime", "claude", "--dir", str(env.project))
    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    assert setup_mod.mask(KEY) in result.output
    assert "agent qa-alice, tenant tenant-1" in result.output
    assert "caura-bus doctor" in result.output and "Next steps" in result.output

    assert mode(env.config) == 0o600
    config = tomllib.loads(env.config.read_text())
    assert config["api_url"] == URL and config["peers"] == ["*"]
    assert config["agent"] == {"agent_id": "qa-alice", "tenant_id": "tenant-1"}
    assert "allow_insecure_http" not in config
    assert KEY not in env.config.read_text()

    remove, add = calls(env.capture)
    assert remove["argv"] == ["mcp", "remove", "--scope", "local", "caura-bus"]
    assert add["cwd"] == str(env.project.resolve()) == remove["cwd"]
    mcp = add["argv"][add["argv"].index("--") + 1]
    assert add["argv"][:5] == ["mcp", "add", "--scope", "local", "caura-bus"]
    assert f"CAURA_API_KEY={KEY}" in add["argv"]
    assert f"CAURA_BUS_AGENT_CONFIG={env.config}" in add["argv"]
    assert Path(mcp).is_absolute() and Path(mcp).name == "caura-bus-mcp"

    text = (env.project / "CLAUDE.md").read_text()
    assert text.startswith("# Team rules\n\nKeep tests green.\n\n" + setup_mod.MARK_START)
    assert TEMPLATE.read_text().strip() in text
    assert text.rstrip().endswith(setup_mod.MARK_END)


def test_rerun_is_idempotent(env):
    assert invoke("--runtime", "claude", "--dir", str(env.project)).exit_code == 0
    first_config = env.config.read_text()
    first_md = (env.project / "CLAUDE.md").read_text()
    result = invoke("--runtime", "claude", "--dir", str(env.project))
    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output
    assert env.config.read_text() == first_config
    assert (env.project / "CLAUDE.md").read_text() == first_md
    assert first_md.count(setup_mod.MARK_START) == 1
    # Each run replaces the previous MCP entry instead of adding a second one.
    assert [c["argv"][1] for c in calls(env.capture)] == ["remove", "add", "remove", "add"]


def test_identity_comes_from_server_and_mismatch_changes_nothing(env):
    result = invoke("--runtime", "claude", "--dir", str(env.project), "--agent-id", "qa-bob")
    assert result.exit_code == 1
    assert "belongs to agent 'qa-alice', not 'qa-bob'" in result.output
    assert KEY not in result.output
    assert not env.config.exists() and not calls(env.capture)
    assert not (env.project / "CLAUDE.md").exists()


def test_rejected_key_is_reported_masked(env):
    result = invoke("--runtime", "claude", "--dir", str(env.project), key="mc_wrong_key_0123456789abcd")
    assert result.exit_code == 1
    assert "rejected key mc_wro…abcd (401)" in result.output
    assert "mc_wrong_key_0123456789abcd" not in result.output
    assert not env.config.exists()


def test_key_from_environment(env, monkeypatch):
    monkeypatch.setenv("CAURA_API_KEY", KEY)
    result = invoke("--runtime", "codex", "--dir", str(env.project), key=None)
    assert result.exit_code == 0, result.output
    assert KEY not in result.output


@pytest.mark.parametrize(
    ("url", "ok", "insecure"),
    [
        ("http://localhost:8000", True, True),
        ("http://127.0.0.1:55003/", True, True),
        ("https://caura.example", True, False),
        ("http://caura.example", False, None),
        ("https://caura.example/api/v1", False, None),
        ("ftp://caura.example", False, None),
    ],
)
def test_url_policy(url, ok, insecure):
    if ok:
        assert setup_mod.check_url(url) == (url.rstrip("/"), insecure)
    else:
        with pytest.raises(setup_mod.SetupError):
            setup_mod.check_url(url)


def test_localhost_config_allows_http(env, monkeypatch):
    result = CliRunner().invoke(
        app,
        [
            "setup",
            "--runtime",
            "codex",
            "--url",
            "http://127.0.0.1:55003",
            "--key",
            KEY,
            "--dir",
            str(env.project),
        ],
    )
    assert result.exit_code == 0, result.output
    (path,) = (env.home / ".config" / "caura-bus" / "agents").rglob("*.toml")
    assert path.parent.parent.name == "127.0.0.1_55003"
    assert tomllib.loads(path.read_text())["allow_insecure_http"] is True


def test_dry_run_writes_nothing(env):
    env.codex.parent.mkdir()
    env.codex.write_text('model = "x"\n')
    for runtime in ("claude", "codex"):
        result = invoke("--runtime", runtime, "--dir", str(env.project), "--dry-run", "--description", "d")
        assert result.exit_code == 0, result.output
        assert "[dry-run] would" in result.output and "nothing was written" in result.output
        assert KEY not in result.output and setup_mod.mask(KEY) in result.output
    assert not env.config.exists() and not calls(env.capture)
    assert not (env.project / "CLAUDE.md").exists() and not (env.project / "AGENTS.md").exists()
    assert env.codex.read_text() == 'model = "x"\n'
    assert [c[1] for c in env.server.calls] == ["/api/v1/bus/identity"] * 2


def test_missing_claude_prints_exact_command_without_key(env, monkeypatch):
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    result = invoke("--runtime", "claude", "--dir", str(env.project))
    assert result.exit_code == 1
    assert KEY not in result.output
    assert '"CAURA_API_KEY=${CAURA_API_KEY:?export your agent key first}"' in result.output
    assert "claude mcp add --scope local caura-bus" in result.output
    assert env.config.exists() and (env.project / "CLAUDE.md").exists()


def test_failing_claude_add_is_reported_and_masked(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "3")
    result = invoke("--runtime", "claude", "--dir", str(env.project))
    assert result.exit_code == 1
    assert "`claude mcp add` failed (3)" in result.output and KEY not in result.output


def test_codex_updates_only_its_table_keeps_backup_and_is_idempotent(env):
    env.codex.parent.mkdir()
    original = (
        'model = "gpt-5"\n\n'
        "[mcp_servers.other]\n"
        'command = "other-mcp"\n\n'
        "[mcp_servers.caura-bus]\n"
        'command = "/old/caura-bus-mcp"\n'
        'env = { CAURA_API_KEY = "old", CAURA_BUS_AGENT_CONFIG = "/old.toml" }\n'
        "startup_timeout_sec = 30 # keep me\n\n"
        "[mcp_servers.caura-bus.tools.peer]\n"
        'approval_mode = "approve"\n\n'
        '[projects."/x"]\n'
        'trust_level = "trusted"\n'
    )
    env.codex.write_text(original)
    result = invoke("--runtime", "codex", "--dir", str(env.project))
    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    data = tomllib.loads(env.codex.read_text())
    entry = data["mcp_servers"]["caura-bus"]
    assert Path(entry["command"]).name == "caura-bus-mcp" and Path(entry["command"]).is_absolute()
    assert entry["env"] == {"CAURA_API_KEY": KEY, "CAURA_BUS_AGENT_CONFIG": str(env.config)}
    assert entry["startup_timeout_sec"] == 30 and entry["tools"] == {"peer": {"approval_mode": "approve"}}
    assert data["mcp_servers"]["other"] == {"command": "other-mcp"}
    assert data["model"] == "gpt-5" and data["projects"] == {"/x": {"trust_level": "trusted"}}
    assert "# keep me" in env.codex.read_text()
    backup = env.codex.with_name("config.toml.caura-bus-setup.bak")
    assert backup.read_text() == original
    assert mode(env.codex) == 0o600
    assert (env.project / "AGENTS.md").read_text().startswith(setup_mod.MARK_START)

    updated = env.codex.read_text()
    again = invoke("--runtime", "codex", "--dir", str(env.project))
    assert again.exit_code == 0 and "already current" in again.output
    assert env.codex.read_text() == updated
    assert not calls(env.capture)


def test_codex_creates_config_when_absent(env):
    result = invoke("--runtime", "codex", "--dir", str(env.project))
    assert result.exit_code == 0, result.output
    assert set(tomllib.loads(env.codex.read_text())["mcp_servers"]) == {"caura-bus"}
    assert not env.codex.with_name("config.toml.caura-bus-setup.bak").exists()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("model = [\n", "not valid TOML"),
        ('[mcp_servers]\ncaura-bus = { command = "x" }\n', "without a [mcp_servers.caura-bus] table header"),
        ('[mcp_servers.caura-bus]\nargs = [\n  "a",\n]\n', "would produce invalid TOML"),
    ],
)
def test_codex_refuses_unsafe_edits(env, text, message):
    env.codex.parent.mkdir()
    env.codex.write_text(text)
    result = invoke("--runtime", "codex", "--dir", str(env.project))
    assert result.exit_code == 1
    assert message in result.output
    assert env.codex.read_text() == text


def test_description_registered_and_unsupported_server_is_a_warning(env):
    result = invoke("--runtime", "codex", "--dir", str(env.project), "--description", "Owns release codes")
    assert result.exit_code == 0, result.output
    assert "description registered" in result.output
    assert ("PUT", "/api/v1/bus/agents/me/description", {"description": "Owns release codes"}) in (
        env.server.calls
    )
    env.server.describe_status = 404
    result = invoke("--runtime", "codex", "--dir", str(env.project), "--description", "x")
    assert result.exit_code == 0, result.output
    assert "does not support descriptions yet" in result.output


def test_existing_block_is_replaced_in_place():
    old = f"intro\n\n{setup_mod.MARK_HEADER}\nstale text\n{setup_mod.MARK_END}\n\n## Mine\nkeep\n"
    updated, action = setup_mod.merge_instructions(old)
    assert action == "updated"
    assert updated.startswith("intro\n\n" + setup_mod.MARK_START)
    assert updated.endswith("\n## Mine\nkeep\n") and "stale text" not in updated
    assert setup_mod.merge_instructions(updated) == (updated, "unchanged")
    with pytest.raises(setup_mod.SetupError):
        setup_mod.merge_instructions(f"{setup_mod.MARK_HEADER}\nno end\n")


def test_rerun_preserves_local_peer_allow_list_and_consultation(tmp_path):
    existing = tmp_path / "a.toml"
    existing.write_text(
        'api_url = "https://old"\npeers = ["bob"]\n[agent]\nagent_id = "x"\ntenant_id = "y"\n'
        "[consultation]\nmax_requests = 2\ndeadline_seconds = 60\n"
    )
    text = setup_mod.render_config(URL, False, IDENTITY, existing)
    data = tomllib.loads(text)
    assert data["peers"] == ["bob"] and data["api_url"] == URL
    assert data["consultation"] == {"max_requests": 2, "deadline_seconds": 60}
    assert data["agent"] == {"agent_id": "qa-alice", "tenant_id": "tenant-1"}


def test_packaged_peer_instructions_match_docs_template():
    # The CLI ships its own copy (docs/ is not in the wheel). Re-copy on template changes:
    #   cp docs/agent-collaboration/PEER_AGENT_CLAUDE_template.md \
    #      clients/collaboration/cli/src/caura_bus_cli/peer_instructions.md
    assert setup_mod.peer_template() == TEMPLATE.read_text()


def test_mask_never_reveals_short_keys():
    assert setup_mod.mask("short") == "****"
    assert setup_mod.mask(KEY) == "mc_tes…CRET"


def test_setup_without_hooks_installs_no_hooks_and_stores_no_key(env):
    assert invoke("--runtime", "claude", "--dir", str(env.project)).exit_code == 0
    assert not (env.project / ".claude" / "settings.local.json").exists()
    assert not env.config.with_suffix(".key").exists()


def test_claude_setup_hooks_stores_private_key_file_and_installs_wake_hooks(env):
    result = invoke("--runtime", "claude", "--dir", str(env.project), "--hooks")
    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    key_file = env.config.with_suffix(".key")
    assert mode(key_file) == 0o600 and key_file.read_text().strip() == KEY
    settings = env.project / ".claude" / "settings.local.json"
    text = settings.read_text()
    assert KEY not in text  # hooks reference the key file, never the key
    hooks = json.loads(text)["hooks"]
    assert set(hooks) == {"SessionStart", "Stop", "UserPromptSubmit"}
    for event in ("SessionStart", "Stop"):
        (hook,) = hooks[event][0]["hooks"]
        assert hook["asyncRewake"] is True
        assert f"--key-file {key_file}" in hook["command"] and f"--config {env.config}" in hook["command"]
    assert invoke("--runtime", "claude", "--dir", str(env.project), "--hooks").exit_code == 0
    assert json.loads(settings.read_text())["hooks"] == hooks  # idempotent


def test_codex_setup_hooks_prints_the_waker_command(env):
    result = invoke("--runtime", "codex", "--dir", str(env.project), "--hooks")
    assert result.exit_code == 0, result.output
    key_file = env.config.with_suffix(".key")
    assert mode(key_file) == 0o600
    assert "caura-bus wake --runtime codex --thread latest" in result.output
    assert f"--key-file {key_file}" in result.output and KEY not in result.output
    assert not (env.project / ".claude").exists()


def test_dry_run_hooks_writes_nothing(env):
    result = invoke("--runtime", "claude", "--dir", str(env.project), "--hooks", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "wake hooks" in result.output
    assert not env.config.with_suffix(".key").exists()
    assert not (env.project / ".claude").exists()
