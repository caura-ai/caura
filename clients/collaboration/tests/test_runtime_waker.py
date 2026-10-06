import asyncio
import contextlib
import json
import shlex
from datetime import datetime
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
    snapshot = {"pending": True, "wait_generation": 4, "drain_generation": 0}
    assert await state.notify(snapshot, queue)
    assert datetime.fromisoformat(state.load()["last_wake_at"]).tzinfo is not None
    assert not await state.notify(snapshot, queue)
    assert not await runtime.WakeState(state.path).notify(snapshot, queue)
    assert not await state.notify({**snapshot, "wait_generation": 5}, queue)
    assert await state.notify({**snapshot, "wait_generation": 6, "drain_generation": 5}, queue)
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    assert (
        records
        == [[["queue", "--thread", "a session; $(no shell)", "--message", runtime.WAKE_TEXT], None]] * 2
    )
    assert not await state.notify({"pending": False, "wait_generation": 6}, queue)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_ambiguous_runtime_failure_is_not_retried_inside_backoff(tmp_path):
    state = runtime.WakeState(tmp_path / "state.json", clock=Clock())
    calls = 0

    async def failed():
        nonlocal calls
        calls += 1
        raise TimeoutError()

    snapshot = {"pending": True, "wait_generation": 0, "drain_generation": 0}
    with pytest.raises(TimeoutError):
        await state.notify(snapshot, failed)
    assert not await state.notify(snapshot, failed)
    assert calls == 1
    assert "last_wake_at" not in state.load()


@pytest.mark.parametrize("failure", [RuntimeError("Codex queue failed"), TimeoutError(), FileNotFoundError()])
async def test_failed_queue_is_retried_on_a_later_snapshot_and_delivered_once(tmp_path, failure):
    clock = Clock()
    state = runtime.WakeState(tmp_path / "state.json", clock=clock)
    queued, failing = [], [True]

    async def emit(message=runtime.WAKE_TEXT):
        if failing[0]:
            raise failure
        queued.append(message)

    snapshot = {"pending": True, "wait_generation": 3, "drain_generation": 2}
    with pytest.raises(type(failure)):
        await state.notify(snapshot, emit)
    # The burst is not recorded as woken, so the failure cannot strand it.
    assert "drain_generation" not in state.load() and "last_wake_at" not in state.load()
    failing[0] = False
    assert not await state.notify(snapshot, emit)  # Still inside the backoff.
    clock.now += runtime.WAKE_RETRY_BASE_SECONDS
    assert await state.notify(snapshot, emit)
    assert queued == [runtime.WAKE_TEXT]
    assert "wake_attempt" not in state.load() and state.load()["drain_generation"] == 2
    clock.now += runtime.WAKE_RETRY_MAX_SECONDS
    assert not await state.notify(snapshot, emit)
    assert not await state.notify({**snapshot, "wait_generation": 9}, emit)
    assert queued == [runtime.WAKE_TEXT]


async def test_repeated_queue_failures_back_off_exponentially_without_a_storm(tmp_path):
    clock = Clock()
    state = runtime.WakeState(tmp_path / "state.json", clock=clock)
    calls = []

    async def failed():
        calls.append(clock.now)
        raise RuntimeError("Codex queue failed")

    snapshot = {"pending": True, "wait_generation": 0, "drain_generation": 0}
    started = clock.now
    for _ in range(4000):  # One snapshot per second for over an hour.
        with contextlib.suppress(RuntimeError):
            await state.notify(snapshot, failed)
        clock.now += 1
    gaps = [later - earlier for earlier, later in zip(calls, calls[1:], strict=False)]
    assert gaps[:6] == [5, 10, 20, 40, 80, 160]
    assert set(gaps[6:]) == {runtime.WAKE_RETRY_MAX_SECONDS}
    assert len(calls) <= 7 + (clock.now - started) / runtime.WAKE_RETRY_MAX_SECONDS
    # A new burst while the runtime is still down shares the same backoff.
    clock.now = calls[-1] + 1
    assert not await state.notify({**snapshot, "wait_generation": 1, "drain_generation": 1}, failed)


async def test_restart_after_failure_retries_and_restart_after_success_does_not(tmp_path):
    clock = Clock()
    path = tmp_path / "state.json"
    queued = []

    async def failed():
        raise RuntimeError("Codex queue failed")

    async def emit(message=runtime.WAKE_TEXT):
        queued.append(message)

    snapshot = {"pending": True, "wait_generation": 1, "drain_generation": 0, "recovery_key": "resume:case-1"}
    with pytest.raises(RuntimeError):
        await runtime.WakeState(path, clock=clock).notify(snapshot, failed)
    # A restarted waker honours the persisted backoff, then retries.
    assert not await runtime.WakeState(path, clock=clock).notify(snapshot, emit)
    clock.now += runtime.WAKE_RETRY_BASE_SECONDS
    assert await runtime.WakeState(path, clock=clock).notify(snapshot, emit)
    assert runtime.WakeState(path).load()["recovery_key"] == "resume:case-1"
    # A restart after the confirmed queue never re-wakes the same burst/recovery.
    clock.now += runtime.WAKE_RETRY_MAX_SECONDS
    assert not await runtime.WakeState(path, clock=clock).notify(snapshot, emit)
    assert queued == [runtime.WAKE_TEXT]


async def test_crash_while_queueing_is_retried_after_restart(tmp_path):
    clock = Clock()
    path = tmp_path / "state.json"
    queued = []

    async def crashed():
        raise asyncio.CancelledError()

    async def emit(message=runtime.WAKE_TEXT):
        queued.append(message)

    snapshot = {"pending": True, "wait_generation": 0, "drain_generation": 0}
    with pytest.raises(asyncio.CancelledError):
        await runtime.WakeState(path, clock=clock).notify(snapshot, crashed)
    assert not await runtime.WakeState(path, clock=clock).notify(snapshot, emit)
    clock.now += runtime.WAKE_RETRY_BASE_SECONDS
    assert await runtime.WakeState(path, clock=clock).notify(snapshot, emit)
    assert queued == [runtime.WAKE_TEXT]


async def test_real_fake_codex_queue_failure_is_retried_until_queued(tmp_path, monkeypatch):
    capture, fake, fail = tmp_path / "capture.txt", tmp_path / "codex", tmp_path / "fail"
    fake.write_text(
        "#!/usr/bin/env python3\nimport os,sys\n"
        "open(os.environ['WAKE_TEST_CAPTURE'],'a').write('call\\n')\n"
        "sys.exit(1 if os.path.exists(os.environ['WAKE_TEST_FAIL']) else 0)\n"
    )
    fake.chmod(0o700)
    fail.touch()
    monkeypatch.setenv("WAKE_TEST_CAPTURE", str(capture))
    monkeypatch.setenv("WAKE_TEST_FAIL", str(fail))
    clock = Clock()
    state = runtime.WakeState(tmp_path / "state.json", clock=clock)
    queue = runtime.CodexQueue("thread", str(fake))
    snapshot = {"pending": True, "wait_generation": 4, "drain_generation": 0}
    with pytest.raises(RuntimeError):
        await state.notify(snapshot, queue)
    fail.unlink()
    clock.now += runtime.WAKE_RETRY_BASE_SECONDS
    assert await state.notify(snapshot, queue)
    clock.now += runtime.WAKE_RETRY_MAX_SECONDS
    assert not await runtime.WakeState(state.path, clock=clock).notify(snapshot, queue)
    assert capture.read_text().splitlines() == ["call", "call"]


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
        self.snapshots = [
            {"pending": False, "active": False, "wait_generation": 0, "drain_generation": 0, "cursor": 2}
        ]
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
    bus.snapshots.append(
        {"pending": True, "active": False, "wait_generation": 0, "drain_generation": 0, "cursor": 3}
    )
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    state = runtime.WakeState(tmp_path / "state.json")
    result = await runtime.receive(config(), state, "Stop", wait=2)
    assert json.loads(result) == {"decision": "block", "reason": runtime.WAKE_TEXT}
    assert "remaining" in capsys.readouterr().err
    assert [p.status for p in bus.profiles] == ["ready", "offline"]
    assert bus.closed
    assert await runtime.receive(config(), state, "Stop", wait=2) == ""
    bus.snapshots[0]["wait_generation"] += 1
    assert await runtime.receive(config(), state, "UserPromptSubmit") == ""
    bus.snapshots[0]["drain_generation"] += 1
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


async def test_event_waker_survives_queue_failure_and_retries(tmp_path, monkeypatch):
    bus = FakeBus(config())
    bus.snapshots[0]["pending"] = True
    events_seen = asyncio.Event()

    async def events(after):
        for seq in range(3, 6):
            yield {"seq": seq, "event_type": "message.available"}
            await asyncio.sleep(0.01)
        events_seen.set()
        await asyncio.Event().wait()

    bus.events = events
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    monkeypatch.setattr(runtime, "WAKE_RETRY_BASE_SECONDS", 0.0)
    calls = []

    async def emit():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Codex queue failed")

    state = runtime.WakeState(tmp_path / "state")
    task = asyncio.create_task(runtime.run_waker(config(), "codex", state, emit))
    await asyncio.wait_for(events_seen.wait(), 2)
    await asyncio.sleep(0.02)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 2
    assert state.load()["last_wake_at"] and "wake_attempt" not in state.load()


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


async def test_sender_notices_share_a_hint_until_inbox_is_drained(tmp_path):
    state = runtime.WakeState(tmp_path / "notices.json")
    emitted = []

    async def emit(message=runtime.WAKE_TEXT):
        emitted.append(message)

    snapshot = {
        "pending": False,
        "notices_pending": True,
        "wait_generation": 3,
        "drain_generation": 2,
        "notice_cursor": 8,
        "wake_reason": "request_overdue",
    }
    assert await state.notify(snapshot, emit)
    assert emitted == ["Caura: a request you sent is overdue. Call peer wait."]
    assert not await state.notify(snapshot, emit)
    assert not await state.notify({**snapshot, "notice_cursor": 9}, emit)
    assert await state.notify({**snapshot, "notice_cursor": 10, "drain_generation": 4}, emit)
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


async def test_delivery_burst_across_turns_and_restarts_queues_only_one_hint(tmp_path):
    path = tmp_path / "burst.json"
    queued = []

    async def emit(message=runtime.WAKE_TEXT):
        queued.append(message)

    # A queued native prompt may not reach the model until many waits, replies,
    # and notice arrivals have happened in the current turn.
    snapshot = {"pending": True, "active": False, "wait_generation": 0, "drain_generation": 0}
    assert await runtime.WakeState(path).notify(snapshot, emit)
    for generation in range(1, 21):
        snapshot.update(wait_generation=generation, notice_cursor=generation)
        assert not await runtime.WakeState(path).notify({**snapshot, "active": True}, emit)
        assert not await runtime.WakeState(path).notify(snapshot, emit)
    # Inbox becoming temporarily empty is not proof the native hint was read.
    assert not await runtime.WakeState(path).notify({**snapshot, "pending": False}, emit)
    assert not await runtime.WakeState(path).notify(snapshot, emit)
    assert len(queued) == 1  # At most this one prompt can cause an empty wait.
    # The model drains the inbox; later work must still wake it, even if the
    # waker never observed the intervening empty snapshot.
    snapshot.update(wait_generation=22, drain_generation=21)
    assert await runtime.WakeState(path).notify(snapshot, emit)
    assert not await runtime.WakeState(path).notify(snapshot, emit)
    assert len(queued) == 2


@pytest.mark.parametrize("key", ["resume:case-1", "retry:delivery-1:1", "retry:delivery-1:2"])
async def test_recovery_rearms_without_an_empty_wait(tmp_path, key):
    state = runtime.WakeState(tmp_path / "recovery.json")
    queued = []

    async def emit(message=runtime.WAKE_TEXT):
        queued.append(message)

    snapshot = {"pending": True, "wait_generation": 0, "drain_generation": 0}
    assert await state.notify(snapshot, emit)
    assert await state.notify({**snapshot, "recovery_key": key}, emit)
    assert not await runtime.WakeState(state.path).notify({**snapshot, "recovery_key": key}, emit)
    # Ordinary messages queued behind the recovery share the same drain.
    assert not await state.notify({**snapshot, "wait_generation": 10}, emit)
    assert len(queued) == 2


async def test_upgrade_adopts_unread_hint_but_does_not_strand_consumed_legacy_state(tmp_path):
    state = runtime.WakeState(tmp_path / "upgrade.json")
    state.save({"outstanding": 4, "notice_cursor": 7})
    queued = []

    async def emit(message=runtime.WAKE_TEXT):
        queued.append(message)

    snapshot = {"pending": True, "wait_generation": 4, "drain_generation": 0}
    assert not await state.notify(snapshot, emit)
    assert not await runtime.WakeState(state.path).notify({**snapshot, "wait_generation": 5}, emit)
    assert await state.notify({**snapshot, "wait_generation": 6, "drain_generation": 6}, emit)
    state.save({"outstanding": 4, "notice_cursor": 7})
    assert await state.notify({**snapshot, "wait_generation": 5}, emit)
    assert len(queued) == 2


def test_cli_reports_its_version_without_starting_a_runtime():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("caura-bus ")
