import asyncio
import json
import shlex
from pathlib import Path

import httpx
import pytest
from caura_bus_cli import runtime
from caura_bus_cli.hooks import install_hooks
from caura_bus_cli.main import app
from caura_bus_core import AgentConfig, PlatformError
from typer.testing import CliRunner


def config():
    return AgentConfig(api_url="https://caura.test", agent={"agent_id": "a", "tenant_id": "t"})


async def test_real_fake_queue_captures_fixed_argv_no_credential_and_coalesces(tmp_path, monkeypatch):
    capture = tmp_path / "capture.jsonl"
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "with open(os.environ['WAKE_TEST_CAPTURE'],'a') as f:\n"
        " f.write(json.dumps([sys.argv[1:],os.environ.get('CAURA_API_KEY')])+'\\n')\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("WAKE_TEST_CAPTURE", str(capture))
    monkeypatch.setenv("CAURA_API_KEY", "must-not-reach-runtime")
    state = runtime.WakeState(tmp_path / "state.json")
    queue = runtime.CodexQueue("a session; $(no shell)", str(fake))
    snapshot = {"pending": True, "wait_generation": 4}
    assert await state.notify(snapshot, queue)
    assert not await state.notify(snapshot, queue)
    assert not await runtime.WakeState(state.path).notify(snapshot, queue)
    assert await state.notify({**snapshot, "wait_generation": 5}, queue)
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    assert (
        records
        == [[["queue", "--thread", "a session; $(no shell)", "--message", runtime.WAKE_TEXT], None]] * 2
    )
    assert not await state.notify({"pending": False, "wait_generation": 6}, queue)


async def test_ambiguous_runtime_failure_does_not_queue_again(tmp_path):
    state = runtime.WakeState(tmp_path / "state.json")
    calls = 0

    async def failed():
        nonlocal calls
        calls += 1
        raise TimeoutError()

    snapshot = {"pending": True, "wait_generation": 0}
    with pytest.raises(TimeoutError):
        await state.notify(snapshot, failed)
    assert not await state.notify(snapshot, failed)
    assert calls == 1


async def test_retry_exponential_backoff_and_immediate_revocation():
    attempts, delays = [], []

    async def operation():
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("offline")
        if len(attempts) < 4:
            raise PlatformError(504, "gateway")
        raise PlatformError(401, "revoked")

    async def sleep(seconds):
        delays.append(seconds)

    with pytest.raises(PlatformError) as exc:
        await runtime.retry(operation, sleep)
    assert exc.value.status == 401
    assert delays == [0.5, 1, 2] and len(attempts) == 4


def test_hooks_merge_idempotently_preserve_settings_and_use_project_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("caura_bus_cli.hooks.shutil.which", lambda _: "/a path/caura-bus")
    project = tmp_path / "project"
    settings = project / ".claude/settings.local.json"
    settings.parent.mkdir(parents=True)
    existing = {
        "permissions": {"deny": ["Bash(rm *)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo existing"}]}]},
    }
    settings.write_text(json.dumps(existing))
    before_global = Path.home() / ".claude/settings.json"
    global_bytes = before_global.read_bytes() if before_global.exists() else None
    for _ in range(2):
        result = install_hooks(project=project, config=tmp_path / "agent config.toml", listen_seconds=37)
    assert result["path"] == str(settings)
    saved = json.loads(settings.read_text())
    assert saved["permissions"] == existing["permissions"]
    assert len(saved["hooks"]["Stop"]) == 2
    stop = saved["hooks"]["Stop"][1]["hooks"][0]
    assert stop["timeout"] == 47
    assert shlex.split(stop["command"])[-6:] == [
        "--hook",
        "Stop",
        "--wait",
        "37",
        "--idle-listen-seconds",
        "5",
    ]
    assert not (project / ".claude/settings.json").exists()
    assert "CAURA_API_KEY" not in settings.read_text()
    assert (before_global.read_bytes() if before_global.exists() else None) == global_bytes


class FakeBus:
    def __init__(self, cfg):
        self.config = cfg
        self.snapshots = [{"pending": False, "active": False, "wait_generation": 0, "cursor": 2}]
        self.profiles = []
        self.closed = False

    async def connect(self):
        return {}

    async def inbox_state(self):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    async def advertise(self, profile):
        self.profiles.append(profile)

    async def close(self):
        self.closed = True


async def test_stop_listener_arrival_and_hook_coalescing(tmp_path, monkeypatch, capsys):
    bus = FakeBus(config())
    bus.snapshots.append({"pending": True, "active": False, "wait_generation": 0, "cursor": 3})
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    state = runtime.WakeState(tmp_path / "state.json")
    result = await runtime.receive(config(), state, "Stop", wait=2)
    assert json.loads(result) == {"decision": "block", "reason": runtime.WAKE_TEXT}
    assert "remaining" in capsys.readouterr().err
    assert [p.status for p in bus.profiles] == ["ready", "offline"]
    assert bus.closed
    assert await runtime.receive(config(), state, "Stop", wait=2) == ""
    bus.snapshots[0]["wait_generation"] += 1
    submitted = json.loads(await runtime.receive(config(), state, "UserPromptSubmit"))
    assert submitted["hookSpecificOutput"]["additionalContext"] == runtime.WAKE_TEXT


async def test_stop_listener_expires_and_leaves_offline(tmp_path, monkeypatch):
    bus = FakeBus(config())
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    result = await runtime.receive(config(), runtime.WakeState(tmp_path / "state"), "Stop", 0.02)
    assert result == "" and bus.closed and bus.profiles[-1].status == "offline"


async def test_event_waker_queues_once_and_stops_on_revocation(tmp_path, monkeypatch):
    bus = FakeBus(config())
    release = asyncio.Event()
    bus.snapshots[0]["pending"] = True

    async def events(after):
        assert after == 2
        yield {"seq": 3, "event_type": "message.available"}
        yield {"seq": 4, "event_type": "delivery.interrupt"}
        await release.wait()
        raise PlatformError(403, "revoked")

    bus.events = events
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    sent = []

    async def emit():
        sent.append(runtime.WAKE_TEXT)

    task = asyncio.create_task(
        runtime.run_waker(config(), "codex", runtime.WakeState(tmp_path / "state"), emit)
    )
    await asyncio.sleep(0.02)
    release.set()
    with pytest.raises(PlatformError) as exc:
        await task
    assert exc.value.status == 403 and bus.closed
    assert sent == [runtime.WAKE_TEXT]
    assert bus.profiles[0].status == "ready"
    assert all(not p.supports_interrupt for p in bus.profiles)


async def test_waker_shutdown_advertises_offline(tmp_path, monkeypatch):
    bus = FakeBus(config())

    async def events(after):
        await asyncio.Event().wait()
        yield

    bus.events = events
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    task = asyncio.create_task(runtime.run_waker(config(), "codex", runtime.WakeState(tmp_path / "state")))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.profiles[-1].status == "offline" and bus.closed


def test_cli_rejects_unsupported_runtime_and_missing_thread():
    runner = CliRunner()
    assert runner.invoke(app, ["wake", "--runtime", "codex"]).exit_code == 1
    result = runner.invoke(app, ["wake", "--runtime", "cursor"])
    assert result.exit_code == 1 and "unsupported" in result.output
    assert runner.invoke(app, ["hooks", "install", "--runtime", "cursor"]).exit_code == 1


@pytest.mark.parametrize("missing", ["default_config", "explicit_config", "env_config", "key"])
def test_recv_without_identity_is_silent_and_does_not_connect(tmp_path, monkeypatch, missing):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CAURA_BUS_AGENT_CONFIG", raising=False)
    monkeypatch.setenv("CAURA_API_KEY", "local-test-key")
    args = ["recv", "--brief", "--hook", "Stop", "--wait", "600"]
    if missing == "explicit_config":
        args.extend(["--config", str(tmp_path / "absent.toml")])
    elif missing == "env_config":
        monkeypatch.setenv("CAURA_BUS_AGENT_CONFIG", str(tmp_path / "absent.toml"))
    elif missing == "key":
        monkeypatch.setenv("CAURA_API_KEY", " ")
        (tmp_path / "caura-bus.toml").write_text(
            'api_url = "https://caura.test"\n[agent]\nagent_id = "a"\ntenant_id = "t"\n'
        )

    def unexpected(*args):
        pytest.fail("unconfigured hook must not connect or create wake state")

    monkeypatch.setattr(runtime, "Bus", unexpected)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0 and result.stdout == "" and result.stderr == ""


def test_hook_shared_settings_require_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr("caura_bus_cli.hooks.shutil.which", lambda _: "/bin/caura-bus")
    shared = tmp_path / ".claude/settings.json"
    shared.parent.mkdir()
    original = '{"permissions":{"deny":["Bash(rm *)"]}}\n'
    shared.write_text(original)
    install_hooks(project=tmp_path)
    assert shared.read_text() == original
    install_hooks(project=tmp_path, shared=True, idle_listen_seconds=2)
    settings = json.loads(shared.read_text())
    assert settings["permissions"] == json.loads(original)["permissions"]
    command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert shlex.split(command)[-2:] == ["--idle-listen-seconds", "2"]


@pytest.mark.parametrize("awaiting,expected_delay", [(False, 0.02), (True, 0.08)])
async def test_listener_uses_full_cap_only_for_unanswered_requests(
    tmp_path, monkeypatch, awaiting, expected_delay
):
    bus = FakeBus(config())
    bus.snapshots[0]["awaiting_reply"] = awaiting
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        raise TimeoutError()

    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    result = await runtime.receive(
        config(), runtime.WakeState(tmp_path / "state"), "Stop", wait=0.08, idle_listen_seconds=0.02
    )
    assert result == "" and bus.closed
    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(expected_delay, abs=0.005)
    assert bus.profiles[-1].status == "offline"


async def test_listener_shortens_window_when_request_is_answered(tmp_path, monkeypatch):
    bus = FakeBus(config())
    bus.snapshots[0]["awaiting_reply"] = True
    bus.snapshots.append({**bus.snapshots[0], "awaiting_reply": False})
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise TimeoutError()

    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    await runtime.receive(
        config(), runtime.WakeState(tmp_path / "state"), "Stop", wait=0.08, idle_listen_seconds=0.02
    )
    assert sleeps == pytest.approx([0.08, 0.02], abs=0.005)


async def test_sender_notice_wakes_with_exact_text_without_delivery_and_new_notice_is_not_coalesced(tmp_path):
    state = runtime.WakeState(tmp_path / "notices.json")
    emitted = []

    async def emit(message=runtime.WAKE_TEXT):
        emitted.append(message)

    snapshot = {
        "pending": False,
        "notices_pending": True,
        "wait_generation": 3,
        "notice_cursor": 8,
        "wake_reason": "request_overdue",
    }
    assert await state.notify(snapshot, emit)
    assert emitted == ["Caura: a request you sent is overdue. Call peer wait."]
    assert not await state.notify(snapshot, emit)
    assert await state.notify({**snapshot, "notice_cursor": 9}, emit)
    assert len(emitted) == 2


@pytest.mark.parametrize("hook", [None, "Stop", "UserPromptSubmit"])
async def test_notice_only_hook_prompts_peer_wait(tmp_path, monkeypatch, hook):
    bus = FakeBus(config())
    bus.snapshots = [
        {
            "pending": True,
            "active": False,
            "notices_pending": True,
            "notice_cursor": 1,
            "wake_reason": "request_overdue",
            "wait_generation": 0,
            "cursor": 1,
        }
    ]
    monkeypatch.setattr(runtime, "Bus", lambda cfg: bus)
    result = await runtime.receive(config(), runtime.WakeState(tmp_path / "notice.json"), hook)
    assert runtime.OVERDUE_TEXT in result
    assert bus.closed
