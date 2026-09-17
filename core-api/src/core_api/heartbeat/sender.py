"""The heartbeat loop, its single HTTP call and the boot log lines.

Cadence: first send at boot + 5 minutes plus up to 60 seconds of jitter, then
every 24 hours ± 60 minutes, computed per cycle. One attempt per cycle with a
5 second total timeout; failure is logged at debug and the loop waits for the
next cycle. No retry, no backoff queue, no persistence of unsent payloads.

The loop is a ``track_task`` like ``UsageMeter`` and is cancelled by
``cancel_all_tasks``; it never blocks startup or shutdown. Every replica runs
it with the shared ``deployment_id``; the collector keeps one heartbeat per
deployment per 20 hours and drops the rest, so there is no leader election.

The request carries ``Authorization: Bearer <deployment_token>`` and
``User-Agent: caura-server/<version>`` and nothing else that identifies it.
Plain ``http://`` is refused unless the host is localhost — the test case.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from core_api.constants import VERSION
from core_api.heartbeat import clients, identity
from core_api.heartbeat.payload import Counts, build_payload, collect_counts
from core_api.heartbeat.policy import DISABLE_HINT, Decision, evaluate

if TYPE_CHECKING:
    from core_api.config import Settings

logger = logging.getLogger("core_api.heartbeat")

FIRST_SEND_DELAY_SECONDS = 5 * 60
FIRST_SEND_JITTER_SECONDS = 60
INTERVAL_SECONDS = 24 * 60 * 60
INTERVAL_JITTER_SECONDS = 60 * 60
REQUEST_TIMEOUT_SECONDS = 5.0
ACCEPTED_STATUS = 202

DOCS_URL = "https://github.com/caura-ai/caura/blob/main/docs/telemetry.md"
INSPECT_PATH = "/api/v1/telemetry"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def check_endpoint_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is https, or http to localhost."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and (parts.hostname or "").lower() in _LOCAL_HOSTS:
        return
    raise ValueError("caura_telemetry_url must be https:// (plain http is allowed for localhost only)")


def _endpoint_host(url: str) -> str:
    return urlsplit(url).hostname or url


class HeartbeatSender:
    """Owns the loop state that ``GET /telemetry`` reports."""

    def __init__(
        self,
        settings: Settings,
        *,
        version: str = VERSION,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._version = version
        self._rng = rng or random.Random()
        self._task: asyncio.Task | None = None
        self.deployment_id: str | None = None
        self.last_sent_at: datetime | None = None
        self.last_status: int | None = None
        self.next_send_at: datetime | None = None

    # ── scheduling ──────────────────────────────────────────────────────

    @property
    def endpoint(self) -> str:
        return self._settings.caura_telemetry_url

    def _first_delay(self) -> float:
        return FIRST_SEND_DELAY_SECONDS + self._rng.uniform(0, FIRST_SEND_JITTER_SECONDS)

    def _next_delay(self) -> float:
        return INTERVAL_SECONDS + self._rng.uniform(-INTERVAL_JITTER_SECONDS, INTERVAL_JITTER_SECONDS)

    def start(self) -> asyncio.Task:
        """Create the tracked loop task. Only called when the policy says on."""
        from core_api.tasks import track_task

        clients.enable()
        self._task = track_task(self._run())
        return self._task

    async def _run(self) -> None:
        delay = self._first_delay()
        while True:
            self.next_send_at = datetime.now(UTC) + timedelta(seconds=delay)
            await asyncio.sleep(delay)
            try:
                await self.send_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # ``send_once`` swallows delivery errors itself; this catches
                # anything unexpected (a storage-client bug, a build error) so
                # one bad cycle cannot end the loop.
                logger.debug("[telemetry] heartbeat cycle failed", exc_info=True)
            delay = self._next_delay()

    # ── payload ─────────────────────────────────────────────────────────

    def _standalone_tenant(self) -> str | None:
        if not self._settings.is_standalone:
            return None
        try:
            from core_api.standalone import get_standalone_tenant_id

            return get_standalone_tenant_id()
        except RuntimeError:
            return None

    async def build(self) -> tuple[identity.DeploymentIdentity, dict[str, Any]]:
        """Resolve identity, read counts and assemble the payload for one send."""
        from core_api.clients.storage_client import get_storage_client

        sc = get_storage_client()
        ident = await identity.load_or_create(sc)
        self.deployment_id = ident.deployment_id
        counts: Counts = await collect_counts(sc, standalone_tenant_id=self._standalone_tenant())
        payload = build_payload(
            settings=self._settings,
            deployment_id=ident.deployment_id,
            counts=counts,
            version=self._version,
        )
        return ident, payload

    async def preview(self) -> dict[str, Any]:
        """Exactly what the next send would contain (for ``GET /telemetry``)."""
        _ident, payload = await self.build()
        return payload

    # ── delivery ────────────────────────────────────────────────────────

    async def send_once(self) -> int | None:
        """One attempt. Returns the HTTP status, or ``None`` when nothing was sent."""
        url = self.endpoint
        try:
            check_endpoint_url(url)
        except ValueError:
            logger.debug("[telemetry] refusing to send to %r: not https", url)
            return None
        try:
            ident, payload = await self.build()
        except Exception:
            logger.debug("[telemetry] could not build heartbeat payload", exc_info=True)
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
        except Exception:
            logger.debug("[telemetry] heartbeat delivery failed", exc_info=True)
            return None
        self.last_sent_at = datetime.now(UTC)
        self.last_status = status
        if status == ACCEPTED_STATUS:
            # The counter covers "since the last accepted send"; a rejected
            # beat keeps its counts for the next cycle.
            clients.reset()
        else:
            logger.debug("[telemetry] collector answered %s", status)
        return status


# ── process-wide wiring ────────────────────────────────────────────────

_sender: HeartbeatSender | None = None
_decision: Decision | None = None


def get_sender() -> HeartbeatSender | None:
    """The running sender, or ``None`` when the policy said off (or before boot)."""
    return _sender


def get_decision() -> Decision | None:
    return _decision


def _reset_for_tests() -> None:
    global _sender, _decision
    _sender = None
    _decision = None
    clients.disable()


def log_boot_line(decision: Decision, settings: Settings) -> None:
    """The lines printed on every start, through the normal logger at INFO."""
    if decision.enabled:
        logger.info(
            "[telemetry] anonymous heartbeat ON: one ping a day to %s. "
            "Disable: %s. Inspect: GET %s. What is sent: %s",
            _endpoint_host(settings.caura_telemetry_url),
            DISABLE_HINT,
            INSPECT_PATH,
            DOCS_URL,
        )
    else:
        logger.info("[telemetry] anonymous heartbeat OFF (%s).", decision.reason)


def install(settings: Settings) -> Decision:
    """Evaluate the policy, log the boot line, and start the loop if it says on.

    Called from the lifespan right after the UsageMeter starts. When the
    policy says off this creates no task, builds no HTTP client, resolves no
    DNS and enables no counter.
    """
    global _sender, _decision
    decision = evaluate(settings)
    _decision = decision
    log_boot_line(decision, settings)
    if decision.enabled:
        sender = HeartbeatSender(settings)
        sender.start()
        _sender = sender
    return decision
