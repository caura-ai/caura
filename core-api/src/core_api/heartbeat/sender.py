"""The heartbeat loop, its single HTTP call and the boot log lines.

Cadence: first send at boot + 5 minutes plus up to 60 seconds of jitter, then
every 24 hours ± 60 minutes, computed per cycle. One attempt per cycle with a
5 second total timeout; a failure is logged once at WARNING, recorded as
``last_error`` for ``GET /telemetry``, and the loop waits for the next cycle.
No retry, no backoff queue, no persistence of unsent payloads.

One beat per container, whatever the worker count. The Docker image runs
uvicorn with ``--workers 2`` and every worker runs the lifespan, so the
workers elect a leader through :mod:`core_api.heartbeat.state` (a lock file
in a per-container directory): the leader runs the send loop, followers only
count clients and flush their counter to a file the leader sums at send
time. If the leader dies a follower takes the lock on its next slow tick and
resumes from the leader's published ``next_send_at``. When the state
directory is unusable every worker runs its own loop, as before, and the boot
log says so at WARNING.

Separate containers (replicas) still each send; the collector keeps one
heartbeat per deployment per 20 hours and drops the rest.

The loop is a ``track_task`` like ``UsageMeter`` and is cancelled by
``cancel_all_tasks``; it never blocks startup or shutdown.

The request carries ``Authorization: Bearer <deployment_token>`` and
``User-Agent: caura-server/<version>`` and nothing else that identifies it.
Plain ``http://`` to anything but localhost is an off row of the policy, so
the sender never sees such a URL; the guard here is defence in depth.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from core_api.constants import VERSION
from core_api.heartbeat import clients, identity
from core_api.heartbeat.payload import Counts, build_payload, collect_counts, normalise_version
from core_api.heartbeat.policy import (
    DISABLE_HINT,
    REASON_INVALID_ENDPOINT_URL,
    Decision,
    check_endpoint_url,
    evaluate,
)
from core_api.heartbeat.state import (
    FLUSH_INTERVAL_SECONDS,
    LEADER_RETRY_SECONDS,
    STATUS_KEYS,
    SharedState,
)

if TYPE_CHECKING:
    from core_api.config import Settings

logger = logging.getLogger("core_api.heartbeat")

FIRST_SEND_DELAY_SECONDS = 5 * 60
FIRST_SEND_JITTER_SECONDS = 60
INTERVAL_SECONDS = 24 * 60 * 60
INTERVAL_JITTER_SECONDS = 60 * 60
REQUEST_TIMEOUT_SECONDS = 5.0
ACCEPTED_STATUS = 202

# ``last_error`` is a short operator-facing string; cap it and strip anything
# that looks like a URL so a credential embedded in an override can never
# surface through ``GET /telemetry`` or the log.
_ERROR_MAX_LEN = 200
_URL_IN_TEXT = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")

DOCS_URL = "https://github.com/caura-ai/caura/blob/main/docs/telemetry.md"
INSPECT_PATH = "/api/v1/telemetry"

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _endpoint_host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or url


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime(_ISO) if dt else None


def _from_iso(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, _ISO).replace(tzinfo=UTC)
    except ValueError:
        return None


def describe_error(exc: BaseException) -> str:
    """``"ConnectError: [Errno 61] Connection refused"``, URL-free and short."""
    text = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    text = _URL_IN_TEXT.sub("<url>", text)
    return text[:_ERROR_MAX_LEN]


class HeartbeatSender:
    """Owns the loop state that ``GET /telemetry`` reports.

    ``shared`` is the container-wide state (``None`` = single-process mode,
    the behaviour of every worker before leader election existed). The
    ``first_delay`` / ``interval`` / ``flush_interval`` / ``leader_retry``
    overrides exist for tests; production uses the module constants.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        version: str = VERSION,
        rng: random.Random | None = None,
        shared: SharedState | None = None,
        first_delay: float | None = None,
        interval: float | None = None,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        leader_retry: float = LEADER_RETRY_SECONDS,
    ) -> None:
        self._settings = settings
        self._version = normalise_version(version)
        self._rng = rng or random.Random()
        self._shared = shared
        self._first_delay_override = first_delay
        self._interval_override = interval
        self._flush_interval = flush_interval
        self._leader_retry = leader_retry
        self._task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self.deployment_id: str | None = None
        self.last_attempt_at: datetime | None = None
        self.last_sent_at: datetime | None = None
        self.last_status: int | None = None
        self.last_error: str | None = None
        self.next_send_at: datetime | None = None

    # -- role ---------------------------------------------------------------

    @property
    def shared(self) -> SharedState | None:
        return self._shared

    @property
    def role(self) -> str:
        """``single`` (no coordination), ``leader`` or ``follower``."""
        if self._shared is None:
            return "single"
        return "leader" if self._shared.is_leader else "follower"

    @property
    def is_leader(self) -> bool:
        return self.role != "follower"

    # -- scheduling ---------------------------------------------------------

    @property
    def endpoint(self) -> str:
        return self._settings.caura_telemetry_url

    def _first_delay(self) -> float:
        if self._first_delay_override is not None:
            return self._first_delay_override
        return FIRST_SEND_DELAY_SECONDS + self._rng.uniform(0, FIRST_SEND_JITTER_SECONDS)

    def _next_delay(self) -> float:
        if self._interval_override is not None:
            return self._interval_override
        return INTERVAL_SECONDS + self._rng.uniform(-INTERVAL_JITTER_SECONDS, INTERVAL_JITTER_SECONDS)

    def _resume_delay(self) -> float:
        """Delay for a follower that just took over: honour the old leader's schedule.

        Falls back to a fresh first delay when the previous leader published
        nothing usable. Never longer than one full interval.
        """
        stored = self._shared.read_status() if self._shared is not None else None
        planned = _from_iso(stored.get("next_send_at")) if stored else None
        if planned is None:
            return self._first_delay()
        remaining = (planned - datetime.now(UTC)).total_seconds()
        return min(max(remaining, 0.0), self._next_delay())

    def start(self) -> asyncio.Task:
        """Enable the counter and start the tasks for this worker's role.

        Only called when the policy says on. Single mode starts the send loop.
        With shared state, the worker that wins ``leader.lock`` starts the send
        loop; every worker starts the sync tick (counter flush, and for
        followers the lock retry). Returns the task that ``GET`` should see as
        "the heartbeat runs here": the loop for a sender, the tick otherwise.
        """
        from core_api.tasks import track_task

        clients.enable()
        if self._shared is None:
            self._task = track_task(self._run())
            return self._task
        if self._shared.try_acquire_leader():
            self._task = track_task(self._run())
        self._sync_task = track_task(self._sync())
        return self._task or self._sync_task

    async def _run(self, initial_delay: float | None = None) -> None:
        delay = self._first_delay() if initial_delay is None else initial_delay
        while True:
            self.next_send_at = datetime.now(UTC) + timedelta(seconds=delay)
            self._publish_status()
            await asyncio.sleep(delay)
            try:
                await self.send_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # ``send_once`` records delivery errors itself; this catches
                # anything unexpected (a storage-client bug, a build error) so
                # one bad cycle cannot end the loop.
                self.last_error = describe_error(exc)
                logger.warning("[telemetry] heartbeat cycle failed: %s", self.last_error, exc_info=True)
                self._publish_status()
            delay = self._next_delay()

    async def _sync(self) -> None:
        """Slow tick in every coordinated worker: flush counts, followers retry the lock."""
        assert self._shared is not None
        last_retry = time.monotonic()
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                self._shared.flush()
                if self._task is not None or self._shared.is_leader:
                    continue
                if time.monotonic() - last_retry < self._leader_retry:
                    continue
                last_retry = time.monotonic()
                if self._shared.try_acquire_leader():
                    from core_api.tasks import track_task

                    logger.info(
                        "[telemetry] worker pid %d took over the heartbeat (previous leader is gone).",
                        os.getpid(),
                    )
                    self._task = track_task(self._run(initial_delay=self._resume_delay()))
        except asyncio.CancelledError:
            # Shutdown: the counts this worker holds would otherwise die with
            # it. The lock is released explicitly rather than left to process
            # exit so a follower's retry succeeds as soon as we are gone.
            self._shared.flush()
            self._shared.release_leader()
            raise

    # -- payload ------------------------------------------------------------

    def _standalone_tenant(self) -> str | None:
        if not self._settings.is_standalone:
            return None
        try:
            from core_api.standalone import get_standalone_tenant_id

            return get_standalone_tenant_id()
        except RuntimeError:
            return None

    def client_counts(self) -> dict[str, int]:
        """The ``clients_24h`` source: summed over every worker when coordinated."""
        if self._shared is None:
            return clients.snapshot()
        return self._shared.aggregate()

    async def _read_storage(self) -> tuple[identity.DeploymentIdentity, Counts]:
        """Identity and counts from storage; the one place the sender does I/O besides the POST."""
        from core_api.clients.storage_client import get_storage_client

        sc = get_storage_client()
        ident = await identity.load_or_create(sc)
        counts = await collect_counts(sc, standalone_tenant_id=self._standalone_tenant())
        return ident, counts

    async def build(self) -> tuple[identity.DeploymentIdentity, dict[str, Any]]:
        """Resolve identity, read counts and assemble the payload for one send."""
        ident, counts = await self._read_storage()
        self.deployment_id = ident.deployment_id
        payload = build_payload(
            settings=self._settings,
            deployment_id=ident.deployment_id,
            counts=counts,
            client_counts=self.client_counts(),
            version=self._version,
        )
        return ident, payload

    async def preview(self) -> dict[str, Any]:
        """Exactly what the next send would contain (for ``GET /telemetry``)."""
        _ident, payload = await self.build()
        return payload

    # -- status ------------------------------------------------------------

    def _own_status(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "last_attempt_at": _iso(self.last_attempt_at),
            "last_sent_at": _iso(self.last_sent_at),
            "last_status": self.last_status,
            "last_error": self.last_error,
            "next_send_at": _iso(self.next_send_at),
        }

    def _publish_status(self) -> None:
        if self._shared is not None and self._shared.is_leader:
            self._shared.write_status(self._own_status())

    def status(self) -> dict[str, Any]:
        """The loop fields ``GET /telemetry`` reports, identical from every worker.

        A follower answers from the leader's ``state.json``; a leader or a
        single-process sender answers from memory.
        """
        if self._shared is not None and not self._shared.is_leader:
            stored = self._shared.read_status()
            if stored is not None:
                return {key: stored.get(key) for key in STATUS_KEYS}
        return self._own_status()

    # -- delivery -----------------------------------------------------------

    def _fail(self, error: str, *, exc_info: bool = False) -> None:
        self.last_error = error
        logger.warning("[telemetry] heartbeat not delivered: %s", error, exc_info=exc_info)
        self._publish_status()

    async def send_once(self) -> int | None:
        """One attempt. Returns the HTTP status, or ``None`` when nothing was sent."""
        url = self.endpoint
        self.last_attempt_at = datetime.now(UTC)
        try:
            check_endpoint_url(url)
        except ValueError as exc:
            self._fail(f"invalid endpoint: {exc}")
            return None
        try:
            ident, payload = await self.build()
        except Exception as exc:
            self._fail(f"payload build failed: {describe_error(exc)}", exc_info=True)
            return None
        headers = {
            "Authorization": f"Bearer {ident.deployment_token}",
            "User-Agent": f"caura-server/{self._version}",
            "Content-Type": "application/json",
        }
        status: int | None = None
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http:
                resp = await http.post(url, json=payload, headers=headers)
            status = resp.status_code
        except Exception as exc:
            self._fail(f"delivery failed: {describe_error(exc)}")
            return None
        self.last_sent_at = datetime.now(UTC)
        self.last_status = status
        if status == ACCEPTED_STATUS:
            # The counter covers "since the last accepted send"; a rejected
            # beat keeps its counts for the next cycle.
            self.last_error = None
            if self._shared is not None:
                self._shared.commit_send()
            else:
                clients.reset()
        else:
            self.last_error = f"collector answered {status}"
            logger.warning("[telemetry] %s", self.last_error)
        self._publish_status()
        return status


# -- process-wide wiring ------------------------------------------------------

_sender: HeartbeatSender | None = None
_decision: Decision | None = None


def get_sender() -> HeartbeatSender | None:
    """The running sender, or ``None`` when the policy said off (or before boot)."""
    return _sender


def get_decision() -> Decision | None:
    return _decision


def _reset_for_tests() -> None:
    global _sender, _decision
    if _sender is not None and _sender.shared is not None:
        _sender.shared.release_leader()
    _sender = None
    _decision = None
    clients.disable()


def log_boot_line(decision: Decision, settings: Settings, *, role: str = "single") -> None:
    """The lines printed on every start, through the normal logger.

    ON and OFF are INFO, except an OFF caused by a bad collector URL, which is
    an operator mistake and goes out at WARNING with the rule. A follower
    worker prints its own line instead of a second ON line, so a container
    log shows one ON line per container.
    """
    if not decision.enabled:
        if decision.reason == REASON_INVALID_ENDPOINT_URL:
            logger.warning(
                "[telemetry] anonymous heartbeat OFF (%s): refusing to send to %r. "
                "CAURA_TELEMETRY_URL must be https:// (plain http is allowed for localhost only).",
                decision.reason,
                settings.caura_telemetry_url,
            )
        else:
            logger.info("[telemetry] anonymous heartbeat OFF (%s).", decision.reason)
        return
    if role == "follower":
        logger.info(
            "[telemetry] anonymous heartbeat follower: worker pid %d counts clients only; "
            "another worker of this container sends the one ping a day.",
            os.getpid(),
        )
        return
    logger.info(
        "[telemetry] anonymous heartbeat ON: one ping a day to %s. Disable: %s. Inspect: GET %s. What is sent: %s",
        _endpoint_host(settings.caura_telemetry_url),
        DISABLE_HINT,
        INSPECT_PATH,
        DOCS_URL,
    )


def open_shared_state(settings: Settings) -> SharedState | None:
    """The container-wide state for ``settings``, or ``None`` with a WARNING when unusable."""
    directory = getattr(settings, "caura_telemetry_state_dir", "") or ""
    if not directory:
        logger.info("[telemetry] CAURA_TELEMETRY_STATE_DIR is empty; each worker sends its own heartbeat.")
        return None
    shared = SharedState.open(directory)
    if shared is None:
        logger.warning(
            "[telemetry] state directory %r is not writable; falling back to one heartbeat per worker "
            "(a container with several workers will send several). Set CAURA_TELEMETRY_STATE_DIR "
            "to a writable directory.",
            directory,
        )
    return shared


def install(settings: Settings) -> Decision:
    """Evaluate the policy, elect a role, log the boot line, start the tasks.

    Called from the lifespan right after the UsageMeter starts. When the
    policy says off this creates no task, builds no HTTP client, resolves no
    DNS, touches no state directory and enables no counter.
    """
    global _sender, _decision
    decision = evaluate(settings)
    _decision = decision
    if not decision.enabled:
        log_boot_line(decision, settings)
        return decision
    sender = HeartbeatSender(settings, shared=open_shared_state(settings))
    sender.start()
    _sender = sender
    log_boot_line(decision, settings, role=sender.role)
    return decision
