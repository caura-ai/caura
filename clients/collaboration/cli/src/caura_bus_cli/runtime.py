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
from caura_bus_core import RESYNC_EVENT, Bus, PlatformError
from caura_bus_core.collaboration import Presence

WAKE_TEXT = (
    "Caura: check inbox. Call peer wait, handle each delivery, and repeat until delivery is null. "
    "Read notices too. Stop if paused. Acknowledge with peer progress; send one reply with the result."
)
OVERDUE_TEXT = "Caura: a request you sent is overdue. Call peer wait."
WAKE_EVENTS = {
    "request.overdue",
    "request.unanswered",
    "request.nudge",
    "request.cancelled",
    "message.available",
    "delivery.available",
    # Retention removed history after our cursor: reconcile from REST state.
    RESYNC_EVENT,
    "human.decided",
    "delivery.interrupt",
    "delivery.acked",
    "delivery.leased",
    "delivery.resumed",
    "delivery.cancelled",
}


class WakeNotStarted(RuntimeError):
    """The runtime process was never created, so retry cannot duplicate a prompt."""


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


# A failed or interrupted native queue is retried on a later snapshot, spaced
# by a capped exponential backoff that survives restarts (wall-clock based).
WAKE_RETRY_BASE_SECONDS = 5.0
WAKE_RETRY_MAX_SECONDS = 300.0


def wake_retry_delay(attempts):
    return min(WAKE_RETRY_BASE_SECONDS * 2 ** min(attempts - 1, 16), WAKE_RETRY_MAX_SECONDS)


class WakeState:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock

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
            # The burst is recorded as woken only after the runtime confirms the
            # queue. Before handing control to it, persist the attempt with its
            # backoff deadline: a failed, timed-out or crashed queue is retried
            # on a later snapshot (also after a restart), but never in a storm.
            attempt = saved.get("wake_attempt") or {}
            now = self.clock()
            if now < attempt.get("retry_at", 0):
                return False
            attempts = attempt.get("attempts", 0) + 1
            self.save(
                {
                    **saved,
                    "wake_attempt": {"attempts": attempts, "retry_at": now + wake_retry_delay(attempts)},
                }
            )
            if snapshot.get("wake_reason") == "request_overdue":
                await emit(OVERDUE_TEXT)
            else:
                await emit()
            # Inventory records only confirmed queue delivery or emitted hook
            # output; a runtime failure never becomes a successful wake.
            current = {k: v for k, v in saved.items() if k != "wake_attempt"}
            self.save(
                {
                    **current,
                    marker: generation,
                    "recovery_key": recovery or saved.get("recovery_key"),
                    "last_wake_at": datetime.now(UTC).isoformat(),
                }
            )
            return True


LATEST_THREAD = "latest"


def codex_home():
    base = os.environ.get("CODEX_HOME", "").strip()
    return Path(base) if base else Path.home() / ".codex"


def latest_codex_thread(directory, home=None):
    """The newest Codex session started in ``directory``, from its rollout metadata.

    Codex writes a session's rollout once its first turn starts, so a session
    that has not yet received a message has no thread to queue into.
    """
    directory = Path(directory).resolve()
    sessions = (Path(home) if home else codex_home()) / "sessions"
    rollouts = sorted(sessions.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for rollout in rollouts:
        try:
            with rollout.open() as stream:
                meta = json.loads(stream.readline())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or meta.get("type") != "session_meta":
            continue
        payload = meta.get("payload") or {}
        if not payload.get("id") or not payload.get("cwd"):
            continue
        if Path(payload["cwd"]).resolve() == directory:
            return payload["id"]
    return None


class CodexQueue:
    def __init__(self, thread, executable="codex", directory=None):
        # ``latest`` follows the newest session in ``directory`` and is resolved
        # at every wake, so the waker can start before (or outlive) a session.
        self.thread = thread
        self.executable = executable
        self.directory = Path(directory or Path.cwd())

    def resolve(self):
        if self.thread != LATEST_THREAD:
            return self.thread
        thread = latest_codex_thread(self.directory)
        if thread is None:
            raise WakeNotStarted(
                f"no Codex session found for {self.directory}; start Codex there and send it a first message"
            )
        return thread

    async def __call__(self, message=WAKE_TEXT):
        env = {k: v for k, v in os.environ.items() if k not in {"CAURA_API_KEY", "CAURA_BUS_AGENT_CONFIG"}}
        thread = self.resolve()
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "queue",
                "--thread",
                thread,
                "--message",
                message,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except OSError as exc:
            raise WakeNotStarted("Codex queue could not start; fix the runtime and retry") from exc
        try:
            await asyncio.wait_for(process.wait(), 15)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("Codex queue failed; inspect the runtime before clearing wake state")
        print(f"Caura: native Codex wake queued for thread {thread}.", file=sys.stderr, flush=True)


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
            else "Claude Code background hook; wakes the open session between turns"
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
            try:
                await state.notify(snapshot, emit)
            except Exception as exc:
                # The attempt and its backoff are persisted; a later snapshot
                # retries. Losing the waker here would strand the wake.
                print(f"Caura: native wake failed, will retry: {exc!r}", file=sys.stderr, flush=True)
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


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def listen(config, state, wait, watch_pid=None, poll=5.0):
    """Background Claude Code listener (an ``asyncRewake`` hook).

    Runs the event-driven waker until the inbox has a new burst to announce,
    then returns the wake text; the hook exits 2 and Claude Code starts a turn
    with it, even when the session is idle. Returns ``None`` without waking when
    another listener already covers this agent on this host, when ``wait``
    expires, or when the watched Claude Code process has exited.
    """
    message = None
    woke = asyncio.Event()

    async def emit(text=WAKE_TEXT):
        nonlocal message
        message = text
        woke.set()

    guard = contextlib.ExitStack()
    try:
        guard.enter_context(lock(state.path.with_suffix(".process.lock")))
    except RuntimeError:
        return None  # one listener per agent and host; the armed one wakes us
    loop = asyncio.get_running_loop()
    parent = os.getppid()
    waker = asyncio.create_task(run_waker(config, "claude-code", state, emit))
    try:
        deadline = loop.time() + wait
        while not woke.is_set() and not waker.done():
            if watch_pid is not None and not process_alive(watch_pid):
                break
            if watch_pid is None and os.getppid() != parent:
                break  # reparented: the session that armed us is gone
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(min(poll, remaining)):
                    await woke.wait()
        if waker.done() and not woke.is_set():
            waker.result()  # surface revocation or a stopped event stream
    finally:
        if not waker.done():
            waker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waker
        guard.close()
    return message


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
                            # Match the waker: a held lease is busy, not ready.
                            status = "busy" if snapshot.get("active") else "ready"
                            await bus.advertise(profile.model_copy(update={"status": status}))
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


def load_key(key_file=None):
    """Make the agent key available: the environment wins, then a private key file.

    Hooks and wakers are started by the runtime, not by the shell that holds the
    key, so `setup --hooks` stores it in a 0600 file next to the agent config.
    Returns False when no key is available (the caller treats that as opt-out).
    """
    if os.environ.get("CAURA_API_KEY", "").strip():
        return True
    if key_file is None or not Path(key_file).is_file():
        return False
    path = Path(key_file)
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{path} must be private to you (chmod 600 {path})")
    key = path.read_text().strip()
    if not key:
        return False
    os.environ["CAURA_API_KEY"] = key
    return True
