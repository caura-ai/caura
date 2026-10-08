"""Idle-session wake-ups: the Claude Code background listener and Codex `--thread latest`."""

import asyncio
import json
import os
import shlex
import subprocess
import sys

import pytest
from caura_bus_cli import runtime
from caura_bus_cli.hooks import owned_command
from caura_bus_cli.main import app
from caura_bus_core import AgentConfig, PlatformError
from typer.testing import CliRunner

IDLE = {"pending": False, "active": False, "wait_generation": 0, "drain_generation": 0, "cursor": 2}
ARRIVED = {**IDLE, "pending": True, "drain_generation": 1}


def config():
    return AgentConfig(api_url="https://caura.test", agent={"agent_id": "a", "tenant_id": "t"})


class StreamBus:
    """Minimal bus for run_waker: snapshots change when an event is released."""

    def __init__(self, snapshots, events=1):
        self.snapshots = list(snapshots)
        self.release = asyncio.Event()
        self.events_to_send = events
        self.profiles = []
        self.closed = False

    async def connect(self):
        return {}

    async def inbox_state(self):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    async def advertise(self, profile):
        self.profiles.append(profile)

    async def events(self, after):
        for seq in range(self.events_to_send):
            await self.release.wait()
            yield {"seq": after + seq + 1, "event_type": "message.available"}
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


async def test_listener_wakes_once_when_work_arrives_and_records_the_burst(tmp_path, monkeypatch):
    bus = StreamBus([IDLE, ARRIVED])
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    state = runtime.WakeState(tmp_path / "state.json")
    task = asyncio.create_task(runtime.listen(config(), state, wait=30, watch_pid=os.getpid(), poll=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()  # idle inbox: keeps listening, never blocks a turn
    bus.release.set()
    assert await asyncio.wait_for(task, 5) == runtime.WAKE_TEXT
    assert state.load()["drain_generation"] == 1
    assert bus.closed and bus.profiles[-1].status == "offline"
    # The re-armed listener (next Stop) must not wake again for the same burst.
    again = StreamBus([ARRIVED])
    monkeypatch.setattr(runtime, "Bus", lambda _: again)
    assert await runtime.listen(config(), state, wait=0.1, watch_pid=os.getpid(), poll=0.01) is None


async def test_overdue_sent_request_wakes_with_the_overdue_text(tmp_path, monkeypatch):
    overdue = {**ARRIVED, "pending": False, "notices_pending": True, "wake_reason": "request_overdue"}
    monkeypatch.setattr(runtime, "Bus", lambda _: StreamBus([overdue]))
    state = runtime.WakeState(tmp_path / "state.json")
    assert await runtime.listen(config(), state, wait=5, poll=0.01) == runtime.OVERDUE_TEXT


async def test_second_listener_for_the_same_agent_exits_without_connecting(tmp_path, monkeypatch):
    first = StreamBus([IDLE])
    monkeypatch.setattr(runtime, "Bus", lambda _: first)
    state = runtime.WakeState(tmp_path / "state.json")
    armed = asyncio.create_task(runtime.listen(config(), state, wait=30, poll=0.01))
    await asyncio.sleep(0.05)

    def unexpected(_):
        pytest.fail("a duplicate listener must not open a second subscription")

    monkeypatch.setattr(runtime, "Bus", unexpected)
    assert await runtime.listen(config(), state, wait=30, poll=0.01) is None
    armed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await armed


async def test_listener_stops_when_its_claude_session_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "Bus", lambda _: StreamBus([IDLE]))
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    state = runtime.WakeState(tmp_path / "state.json")
    started = asyncio.get_running_loop().time()
    assert await runtime.listen(config(), state, wait=30, watch_pid=gone.pid, poll=0.01) is None
    assert asyncio.get_running_loop().time() - started < 5
    # The lock is released, so the next session can arm its own listener.
    monkeypatch.setattr(runtime, "Bus", lambda _: StreamBus([ARRIVED]))
    assert await runtime.listen(config(), state, wait=5, poll=0.01) == runtime.WAKE_TEXT


async def test_listener_surfaces_revocation(tmp_path, monkeypatch):
    class Revoked(StreamBus):
        async def connect(self):
            raise PlatformError(401, "revoked")

    monkeypatch.setattr(runtime, "Bus", lambda _: Revoked([IDLE]))
    with pytest.raises(PlatformError):
        await runtime.listen(config(), runtime.WakeState(tmp_path / "s.json"), wait=5, poll=0.01)


def write_config(tmp_path):
    cfg = tmp_path / "agent.toml"
    cfg.write_text('api_url = "https://caura.test"\n[agent]\nagent_id = "a"\ntenant_id = "t"\n')
    return cfg


def test_cli_rewake_exits_2_with_wake_text_and_reads_a_private_key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CAURA_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    key_file = tmp_path / "agent.key"
    key_file.write_text("file-key-123\n")
    key_file.chmod(0o600)
    seen = []

    def bus(cfg):
        seen.append(os.environ.get("CAURA_API_KEY"))
        return StreamBus([ARRIVED])

    monkeypatch.setattr(runtime, "Bus", bus)
    args = ["recv", "--brief", "--hook", "Rewake", "--wait", "5", "--config", str(write_config(tmp_path))]
    args += ["--state", str(tmp_path / "s.json"), "--key-file", str(key_file)]
    result = CliRunner().invoke(app, args, input='{"hook_event_name":"Stop"}')
    assert result.exit_code == 2, result.output
    assert result.stdout == "" and result.stderr.strip() == runtime.WAKE_TEXT
    assert seen == ["file-key-123"] and "file-key-123" not in result.output


def test_cli_key_file_must_be_private_and_missing_file_is_silent(tmp_path, monkeypatch):
    monkeypatch.delenv("CAURA_API_KEY", raising=False)
    monkeypatch.setattr(runtime, "Bus", lambda _: pytest.fail("must not connect"))
    cfg = write_config(tmp_path)
    key_file = tmp_path / "agent.key"
    args = ["recv", "--brief", "--hook", "Rewake", "--config", str(cfg), "--key-file", str(key_file)]
    absent = CliRunner().invoke(app, args)
    assert absent.exit_code == 0 and absent.output == ""
    key_file.write_text("k")
    key_file.chmod(0o644)
    loose = CliRunner().invoke(app, args)
    assert loose.exit_code == 1 and "chmod 600" in loose.stderr


def test_reinstall_replaces_the_legacy_blocking_stop_listener(tmp_path, monkeypatch):
    monkeypatch.setattr("caura_bus_cli.hooks.shutil.which", lambda _: "/bin/caura-bus")
    settings = tmp_path / ".claude/settings.local.json"
    settings.parent.mkdir()
    legacy = "/bin/caura-bus recv --brief --hook Stop --wait 600 --idle-listen-seconds 5"
    foreign = {"type": "command", "command": "echo mine"}
    settings.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [foreign, {"type": "command", "command": legacy}]}]}})
    )
    result = CliRunner().invoke(
        app,
        [
            "hooks",
            "install",
            "--runtime",
            "claude-code",
            "--dir",
            str(tmp_path),
            "--key-file",
            str(tmp_path / "k.key"),
        ],
    )
    assert result.exit_code == 0, result.output
    hooks = json.loads(settings.read_text())["hooks"]
    assert hooks["Stop"][0]["hooks"] == [foreign]
    commands = [
        g["hooks"][0]["command"] for e in hooks.values() for g in e if owned_command(g["hooks"][0]["command"])
    ]
    assert len(commands) == 3 and legacy not in commands
    assert all(str((tmp_path / "k.key").resolve()) in shlex.split(c) for c in commands)


def rollout(home, name, thread, cwd, mtime):
    path = home / "sessions" / "2026" / "10" / "08" / f"rollout-{name}-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": thread, "cwd": str(cwd)}}
    path.write_text(json.dumps(meta) + "\n{}\n")
    os.utime(path, (mtime, mtime))
    return path


def test_latest_codex_thread_is_the_newest_session_in_the_directory(tmp_path):
    home, project, other = tmp_path / "codex", tmp_path / "project", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    assert runtime.latest_codex_thread(project, home) is None
    rollout(home, "a", "older", project, 100)
    rollout(home, "b", "newer", project, 200)
    rollout(home, "c", "elsewhere", other, 300)
    (home / "sessions" / "rollout-broken-x.jsonl").write_text("not json\n")
    assert runtime.latest_codex_thread(project, home) == "newer"


async def test_codex_queue_latest_resolves_at_wake_time(tmp_path, monkeypatch):
    home, project = tmp_path / "codex", tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    capture = tmp_path / "argv.json"
    fake = tmp_path / "codex-bin"
    fake.write_text(
        f"#!{sys.executable}\nimport json,sys\nopen({str(capture)!r},'w').write(json.dumps(sys.argv[1:]))\n"
    )
    fake.chmod(0o700)
    queue = runtime.CodexQueue(runtime.LATEST_THREAD, str(fake), directory=project)
    with pytest.raises(runtime.WakeNotStarted):
        await queue()  # retried later with backoff, like a missing runtime
    rollout(home, "a", "thread-1", project, 100)
    await queue()
    assert json.loads(capture.read_text()) == [
        "queue",
        "--thread",
        "thread-1",
        "--message",
        runtime.WAKE_TEXT,
    ]


def test_codex_wake_needs_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("CAURA_API_KEY", raising=False)
    result = CliRunner().invoke(
        app, ["wake", "--runtime", "codex", "--thread", "latest", "--config", str(write_config(tmp_path))]
    )
    assert result.exit_code == 1 and "--key-file" in result.stderr
