"""`caura-bus join` prefers the Caura broker (`caura agent connect`) when it is installed.

Reuses the mocked dashboard/bus environment of test_join_command and adds a
fake `caura` broker that records how it was called.
"""

import json
import sys
from pathlib import Path

import pytest
from caura_bus_cli import broker as broker_mod
from caura_bus_cli.main import app
from test_join_command import EMAIL, PASSWORD, URL, env  # noqa: F401  (env is a fixture)
from typer.testing import CliRunner

USAGE = """Usage of agent connect:
  -agent string
  -bootstrap-key-file string
  -description string
  -project string
  -runtime string
  -thread string
  -url string
"""


def invoke(*args, runtime="claude", password=PASSWORD, input=None):
    argv = ["join", "--url", URL, "--email", EMAIL, *args]
    if runtime:
        argv += ["--runtime", runtime]
    extra = {"CAURA_PASSWORD": password} if password else None
    return CliRunner().invoke(app, argv, env=extra, input=input)


def install_broker(env, *, usage=USAGE, exit_code=0):  # noqa: F811
    """A fake `caura` next to the fake `claude`; real calls go to broker-calls.jsonl."""
    fakebin = env.tmp / "bin"
    calls = env.tmp / "broker-calls.jsonl"
    script = fakebin / "caura"
    script.write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        f"if sys.argv[1:] == ['agent', 'connect', '--help']:\n"
        f"    sys.stderr.write({usage!r}); sys.exit(0)\n"
        f"with open({str(calls)!r}, 'a') as f:\n"
        " f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o700)
    return calls


def broker_calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_without_broker_join_is_unchanged(env):  # noqa: F811
    result = invoke("--dir", str(env.project))
    assert result.exit_code == 0, result.output
    assert "Caura broker" not in result.output
    assert env.dashboard.minted()


def test_broker_flag_without_broker_fails_before_sign_in(env):  # noqa: F811
    result = invoke("--dir", str(env.project), "--broker")
    assert result.exit_code == 1
    assert "needs the Caura broker" in result.output
    assert not env.dashboard.calls


def test_noninteractive_prints_equivalent_command_and_continues(env):  # noqa: F811
    calls = install_broker(env)
    result = invoke("--dir", str(env.project), "--name", "Alice Claude!", "--description", "Owns billing")
    assert result.exit_code == 0, result.output
    assert "The equivalent command is:" in result.output
    expected = (
        f"agent connect --runtime claude-code --agent alice-claude --project {env.project.resolve()} "
        f"--url {URL} --description 'Owns billing'"
    )
    assert expected in result.output
    assert "Continuing with caura-bus join" in result.output
    assert not broker_calls(calls)
    assert env.dashboard.minted()  # join itself still ran


def test_interactive_yes_runs_the_broker_instead(env, monkeypatch):  # noqa: F811
    calls = install_broker(env, exit_code=0)
    monkeypatch.setattr(broker_mod, "interactive", lambda: True)
    # No password: the broker signs in through the browser.
    result = invoke("--dir", str(env.project), "--name", "rev", input="y\n", password=None)
    assert result.exit_code == 0, result.output
    (argv,) = broker_calls(calls)
    assert argv == [
        "agent",
        "connect",
        "--runtime",
        "claude-code",
        "--agent",
        "rev",
        "--project",
        str(env.project.resolve()),
        "--url",
        URL,
    ]
    assert not env.dashboard.calls


def test_interactive_no_continues_with_join(env, monkeypatch):  # noqa: F811
    calls = install_broker(env)
    monkeypatch.setattr(broker_mod, "interactive", lambda: True)
    result = invoke("--dir", str(env.project), input="n\n")
    assert result.exit_code == 0, result.output
    assert not broker_calls(calls)
    assert env.dashboard.minted()


def test_broker_flag_runs_without_asking_and_passes_its_exit_code(env):  # noqa: F811
    calls = install_broker(env, exit_code=3)
    result = invoke("--dir", str(env.project), "--broker", password=None)
    assert result.exit_code == 3
    assert len(broker_calls(calls)) == 1
    assert not env.dashboard.calls


def test_no_broker_flag_skips_the_offer(env, monkeypatch):  # noqa: F811
    calls = install_broker(env)
    monkeypatch.setattr(broker_mod, "interactive", lambda: True)
    result = invoke("--dir", str(env.project), "--no-broker")
    assert result.exit_code == 0, result.output
    assert "Caura broker" not in result.output
    assert not broker_calls(calls)


def test_codex_without_thread_is_shown_but_not_run(env, monkeypatch):  # noqa: F811
    calls = install_broker(env)
    monkeypatch.setattr(broker_mod, "interactive", lambda: True)
    result = invoke("--dir", str(env.project), runtime="codex")
    assert result.exit_code == 0, result.output
    assert f"--thread '{broker_mod.THREAD_PLACEHOLDER}'" in result.output
    assert "Codex needs its thread" in result.output
    assert not broker_calls(calls)
    result = invoke("--dir", str(env.project), "--broker", runtime="codex", password=None)
    assert result.exit_code == 1 and "cannot run the command" in result.output


def test_codex_thread_from_environment(env, monkeypatch):  # noqa: F811
    calls = install_broker(env)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    result = invoke("--dir", str(env.project), "--broker", runtime="codex", password=None)
    assert result.exit_code == 0, result.output
    (argv,) = broker_calls(calls)
    assert argv[argv.index("--runtime") + 1] == "codex" and argv[-2:] == ["--thread", "thread-123"]


def test_unrelated_caura_executable_is_ignored(env):  # noqa: F811
    calls = install_broker(env, usage="usage: caura [memory tools]\n")
    result = invoke("--dir", str(env.project))
    assert result.exit_code == 0, result.output
    assert "Caura broker" not in result.output
    assert not broker_calls(calls)


def test_plan_notes_options_the_broker_does_not_take(tmp_path):
    broker = broker_mod.Broker(path="/usr/local/bin/caura", flags={"agent", "runtime", "project", "url"})
    plan = broker_mod.connect_plan(
        broker,
        url=URL,
        runtime="claude",
        directory=tmp_path,
        name=None,
        description="x",
        tenant="other",
        hooks=False,
        env={},
    )
    assert plan.runnable
    agent = plan.argv[plan.argv.index("--agent") + 1]
    assert agent.startswith(f"claude-{broker_mod.sanitize_name(tmp_path.name)[:24].strip('-')}")
    assert "--description" not in plan.argv
    joined = " ".join(plan.notes)
    assert "cannot register a description" in joined and "--tenant" in joined and "--no-hooks" in joined
    assert PASSWORD not in " ".join(plan.argv)
    with pytest.raises(broker_mod.JoinError):
        broker_mod.connect_plan(
            broker,
            url=URL,
            runtime="claude",
            directory=tmp_path,
            name="!!!",
            description=None,
            tenant=None,
            hooks=True,
            env={},
        )


def test_plan_passes_no_memory_only_to_a_broker_that_knows_it(tmp_path):
    base = {"agent", "runtime", "project", "url"}
    common = dict(
        url=URL,
        runtime="claude",
        directory=tmp_path,
        name="a",
        description=None,
        tenant=None,
        hooks=True,
        env={},
    )
    old = broker_mod.Broker(path="/b/caura", flags=base)
    plan = broker_mod.connect_plan(old, memory=True, **common)
    assert "does not register the Caura memory MCP" in " ".join(plan.notes)
    assert "--no-memory" not in broker_mod.connect_plan(old, memory=False, **common).argv
    new = broker_mod.Broker(path="/b/caura", flags=base | {"no-memory"})
    assert "--no-memory" not in broker_mod.connect_plan(new, memory=True, **common).argv
    assert broker_mod.connect_plan(new, memory=True, **common).notes == []
    assert broker_mod.connect_plan(new, memory=False, **common).argv[-1] == "--no-memory"
