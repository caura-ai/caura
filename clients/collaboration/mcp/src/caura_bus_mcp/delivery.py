"""Private lease ownership. A tool result never contains a lease token."""

import asyncio
import logging
import secrets
from contextlib import suppress

import httpx
from caura_bus_core.bus import PlatformError
from caura_bus_core.protocol import Claim

log = logging.getLogger(__name__)


class DeliverySession:
    def __init__(self, bus):
        self.bus = bus
        self.session_id = secrets.token_urlsafe(32)
        self.current: Claim | None = None
        self.lock = asyncio.Lock()
        self.renewal: asyncio.Task | None = None
        self.reply_deliveries: dict[str, tuple[str, str, str]] = {}

    @staticmethod
    def public(claim):
        return claim.model_dump(by_alias=True, exclude={"lease_token", "event_cursor"}) if claim else None

    async def close(self):
        if self.renewal:
            self.renewal.cancel()
            with suppress(asyncio.CancelledError):
                await self.renewal
        # Disconnect is not completion; server lease expiry provides recovery.

    async def wait(self, timeout=50):
        async with self.lock:
            claim = await self.bus.wait(self.session_id, timeout)
            self.current = claim
            if claim:
                self.reply_deliveries[claim.envelope.id] = (
                    claim.delivery_id,
                    claim.envelope.from_,
                    claim.envelope.thread_id,
                )
                if claim.state == "paused":
                    await self._observe_pause(claim)
                elif claim.state == "leased" and (not self.renewal or self.renewal.done()):
                    self.renewal = asyncio.create_task(self._renew())
            return {"delivery": self.public(claim)}

    async def _observe_pause(self, claim):
        token = claim.lease_token
        claim.lease_token = None
        if token:
            result = await self.bus.delivery_action(claim.delivery_id, "caura-stopped", token)
            claim.intervention = result["intervention"]

    async def guard(self):
        claim = self.current
        if not claim:
            return
        if claim.lease_token:
            result = await self.bus.delivery_action(claim.delivery_id, "observe", claim.lease_token)
            claim = Claim.model_validate(result["delivery"])
            self.current = claim
        if claim.state == "paused":
            await self._observe_pause(claim)
            raise PlatformError(409, {"state": "paused", "delivery": self.public(claim)})
        if claim.state in {"acked", "cancelled"}:
            self.current = None

    def token(self, delivery_id):
        if self.current and self.current.delivery_id == delivery_id:
            return self.current.lease_token
        return None

    def completed(self, delivery_id):
        if self.current and self.current.delivery_id == delivery_id:
            self.current = None

    async def _renew(self):
        while True:
            await asyncio.sleep(2)
            async with self.lock:
                claim = self.current
                if not claim or claim.state != "leased" or not claim.lease_token:
                    return
                try:
                    result = await self.bus.delivery_action(claim.delivery_id, "renew", claim.lease_token)
                    claim.lease_expires_at = result["lease_expires_at"]
                except PlatformError as exc:
                    log.debug("Lease renewal stopped or deferred after platform status %s", exc.status)
                    if exc.status == 409:
                        # Surface pause at the next model call. Background activity is not
                        # runtime stoppage and never manufactures that confirmation.
                        return
                    if exc.status in {401, 403, 404}:
                        return
                except httpx.TransportError:
                    log.debug("Lease renewal transport failure; retrying within platform fence")
                    # The platform fences recovery if a network failure outlasts the lease.
                    continue
