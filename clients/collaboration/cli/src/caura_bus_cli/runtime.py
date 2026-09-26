"""Native wake prompts; consumption and lease ownership remain exclusively in MCP."""

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from caura_bus_core import Bus, PlatformError
from caura_bus_core.collaboration import Presence

WAKE_TEXT = (
    "Caura: check inbox. Call peer wait, handle each delivery, and repeat until delivery is null. "
    "Read notices too. Stop if paused."
)
OVERDUE_TEXT = "Caura: a request you sent is overdue. Call peer wait."
WAKE_EVENTS = {
    "request.overdue",
    "request.unanswered",
    "request.nudge",
    "request.cancelled",
    "message.available",
    "delivery.available",
    "human.decided",
    "delivery.interrupt",
    "delivery.acked",
    "delivery.leased",
    "delivery.resumed",
    "delivery.cancelled",
}


def state_path(config):
    identity = json.dumps([config.api_url, config.agent.tenant_id, config.agent.agent_id])
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return root / "caura-bus" / (digest + ".json")


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another Caura waker or hook is using this agent") from None
        yield
    finally:
        os.close(fd)


class WakeState:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, data):
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def health(self, status):
        with lock(self.path.with_suffix(".lock")):
            self.save({**self.load(), "health": status, "health_at": datetime.now(UTC).isoformat()})

    async def notify(self, snapshot, emit):
        with lock(self.path.with_suffix(".lock")):
            saved = self.load()
            # Repeated waits on a lease and additional notices belong to the
            # same burst. Only a drained inbox rearms its ordinary wake hint.
            # Keep the old-server path for rolling upgrades.
            durable = "drain_generation" in snapshot
            generation = snapshot.get("drain_generation", snapshot["wait_generation"])
            marker = "drain_generation" if durable else "outstanding"
            recovery = snapshot.get("recovery_key")
            if not (snapshot["pending"] or snapshot.get("notices_pending")):
                return False
            if durable and marker not in saved and saved.get("outstanding") == snapshot["wait_generation"]:
                # An older waker may already have queued a prompt. Adopt it,
                # rather than queueing another merely because we upgraded.
                saved = {**saved, marker: generation, "recovery_key": recovery}
                self.save(saved)
                return False
            same_burst = saved.get(marker) == generation
            recovering = recovery is not None and recovery != saved.get("recovery_key")
            if same_burst and not recovering:
                return False
            # Persist before handing control to a runtime. A crash/ambiguous
            # queue failure must not enqueue duplicate prompts on restart.
            current = {**saved, marker: generation, "recovery_key": recovery or saved.get("recovery_key")}
            self.save(current)
            if snapshot.get("wake_reason") == "request_overdue":
                await emit(OVERDUE_TEXT)
            else:
                await emit()
            # Inventory records only confirmed queue delivery or emitted hook
            # output; an ambiguous runtime failure never becomes a successful wake.
            self.save({**current, "last_wake_at": datetime.now(UTC).isoformat()})
            return True


class CodexQueue:
    def __init__(self, thread, executable="codex"):
        self.thread = thread
        self.executable = executable

    async def __call__(self, message=WAKE_TEXT):
        env = {k: v for k, v in os.environ.items() if k not in {"CAURA_API_KEY", "CAURA_BUS_AGENT_CONFIG"}}
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "queue",
            "--thread",
            self.thread,
            "--message",
            message,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            await asyncio.wait_for(process.wait(), 15)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("Codex queue failed; inspect the runtime before clearing wake state")
        print("Caura: native Codex wake queued.", file=sys.stderr, flush=True)


def transient(exc):
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, PlatformError) and (exc.status >= 500 or exc.status == 429)
    )


async def retry(operation, sleep=asyncio.sleep):
    failures = 0
    while True:
        try:
            return await operation()
        except (PlatformError, httpx.TransportError) as exc:
            if not transient(exc):
                raise
            await sleep(min(0.5 * 2 ** min(failures, 5), 15))
            failures += 1


async def run_waker(config, runtime, state, emit=None):
    """Events trigger prompt checks; periodic reconciliation handles lease expiry."""
    bus = Bus(config)
    profile = Presence(
        session_id="waker-" + runtime,
        display_name=config.agent.agent_id,
        description=(
            "Native Codex queue; delivery at a turn boundary"
            if runtime == "codex"
            else "Claude Code Stop/UserPromptSubmit hooks; boundary checks only"
        ),
        capabilities=["pull-delivery"],
        supports_interrupt=False,
    )
    changed = asyncio.Event()
    revoked = False

    async def consume(cursor):
        async for event in bus.events(after=cursor):
            if event["event_type"] in WAKE_EVENTS:
                changed.set()

    async def checked(operation):
        try:
            return await operation()
        except (PlatformError, httpx.TransportError):
            state.health("error")
            raise

    async def reconcile():
        snapshot = await retry(lambda: checked(bus.inbox_state))
        await retry(
            lambda: checked(
                lambda: bus.advertise(
                    profile.model_copy(update={"status": "busy" if snapshot["active"] else "ready"})
                )
            )
        )
        if emit is not None:
            await state.notify(snapshot, emit)
        state.health("healthy")
        return snapshot

    events_task = None
    try:
        state.health("starting")
        await retry(lambda: checked(bus.connect))
        snapshot = await reconcile()
        events_task = asyncio.create_task(consume(snapshot["cursor"]))
        while True:
            changed_task = asyncio.create_task(changed.wait())
            try:
                done, _ = await asyncio.wait(
                    {events_task, changed_task},
                    timeout=10,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if events_task in done:
                    await events_task
                    raise RuntimeError("Caura event stream stopped")
            finally:
                changed_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await changed_task
            changed.clear()
            await reconcile()
    except PlatformError as exc:
        revoked = exc.status in {401, 403}
        state.health("error")
        raise
    finally:
        if events_task is not None:
            events_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, PlatformError):
                await events_task
        if not revoked:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await bus.advertise(profile.model_copy(update={"status": "offline"}))
        await bus.close()


async def supervise(config, runtime, state, emit=None):
    # Only one event subscriber per agent on this host. Hint checks also use
    # a short-lived lock so concurrent callers cannot queue duplicate prompts.
    with lock(state.path.with_suffix(".process.lock")):
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            await run_waker(config, runtime, state, emit)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)


async def receive(config, state, hook=None, wait=0, idle_listen_seconds=5):
    output = []

    async def emit(message=WAKE_TEXT):
        if hook == "Stop":
            output.append(json.dumps({"decision": "block", "reason": message}))
        elif hook == "UserPromptSubmit":
            output.append(
                json.dumps({"hookSpecificOutput": {"hookEventName": hook, "additionalContext": message}})
            )
        else:
            output.append(message)

    started = time.monotonic()
    deadline = started + wait
    next_status = 0
    failures = 0
    bus = Bus(config)
    profile = Presence(
        session_id="claude-stop-listener",
        display_name=config.agent.agent_id,
        description="Claude Stop hook is listening for a bounded window",
        capabilities=["pull-delivery"],
        supports_interrupt=False,
    )
    try:
        initial_budget = min(wait, idle_listen_seconds) if wait and idle_listen_seconds else 5
        async with asyncio.timeout(initial_budget) as budget:
            while True:
                try:
                    await bus.connect()
                    snapshot = await bus.inbox_state()
                    state.health("healthy")
                    if wait:
                        window = (
                            wait if snapshot.get("awaiting_reply", False) else min(wait, idle_listen_seconds)
                        )
                        deadline = started + window
                        # Shrinking the budget also bounds a slow gateway call
                        # after learning that this is an ordinary idle session.
                        budget.reschedule(
                            asyncio.get_running_loop().time() + max(0, deadline - time.monotonic())
                        )
                    if await state.notify(snapshot, emit):
                        break
                    if snapshot["pending"]:
                        # A previous hook already asked for wait; do not create
                        # an unbounded Stop continuation loop if it was ignored.
                        break
                    remaining = max(0, deadline - time.monotonic())
                    if not wait or remaining <= 0:
                        break
                    if time.monotonic() >= next_status:
                        print(
                            f"Caura: listening, {remaining:.0f}s remaining",
                            file=sys.stderr,
                            flush=True,
                        )
                        next_status = time.monotonic() + 10
                        if hook == "Stop":
                            await bus.advertise(profile)
                    failures = 0
                    await asyncio.sleep(min(1, remaining))
                except (PlatformError, httpx.TransportError) as exc:
                    state.health("error")
                    if not transient(exc) or not wait:
                        raise
                    await asyncio.sleep(min(0.5 * 2 ** min(failures, 5), 15))
                    failures += 1
    except TimeoutError:
        if not wait:
            raise
    finally:
        if hook == "Stop" and wait:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2):
                    await bus.advertise(profile.model_copy(update={"status": "offline"}))
        await bus.close()
    return output[0] if output else ""
