"""Private lease ownership. A tool result never contains a lease token."""

import asyncio
import logging
import secrets
from contextlib import suppress

import httpx
from caura_bus_core.bus import PlatformError
from caura_bus_core.consult import PresentedResponses
from caura_bus_core.protocol import Claim

log = logging.getLogger(__name__)

ALREADY_PRESENTED = (
    "This response was already shown in this session (collect or recent). Do not act on it "
    "again; ack this delivery_id."
)


class DeliverySession:
    def __init__(self, bus):
        self.bus = bus
        self.session_id = secrets.token_urlsafe(32)
        self.current: Claim | None = None
        self.lock = asyncio.Lock()
        self.renewal: asyncio.Task | None = None
        self.reply_keys: set[tuple[str, str]] = set()
        # (delivery_id, idempotency_key) replies sent, even if their result was lost.
        self.reply_attempts: set[tuple[str, str]] = set()
        self.reply_deliveries: dict[str, tuple[str, str, str]] = {}
        # Responses already shown via collect/recent(reply_to) or an earlier wait.
        self.presented = PresentedResponses()
        # Called with a delivery ID once this session finishes it (consultation scopes).
        self.on_finished = lambda _delivery_id: None
        # Notices returned while refreshing a stale claim; the next wait hands them over.
        self.notices: list = []
        # Human decisions whose resume_context the model has already been shown.
        self.surfaced: set[str] = set()

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
            # Notices held from a guard refresh must not wait behind a long poll.
            result = await self.bus.wait_result(self.session_id, 0 if self.notices else timeout)
            claim = await self._adopt(result)
            if claim and claim.resume_context:
                self.surfaced.add(str(claim.resume_context.get("intervention_id")))
            notices, self.notices = [*self.notices, *result.get("notices", [])], []
            delivery = self.public(claim)
            if claim and claim.envelope.kind == "response" and not self.presented.add(claim.envelope.id):
                # Already in this conversation: show attribution, not the body again,
                # and leave the queued delivery for a normal ACK.
                delivery["envelope"]["body"] = None
                delivery["already_presented"] = True
                delivery["note"] = ALREADY_PRESENTED
            return {"delivery": delivery, "notices": notices}

    async def _adopt(self, result):
        claim = Claim.model_validate(result["delivery"]) if result["delivery"] else None
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
        return claim

    async def _observe_pause(self, claim):
        token = claim.lease_token
        claim.lease_token = None
        if token:
            result = await self.bus.delivery_action(claim.delivery_id, "caura-stopped", token)
            claim.intervention = result["intervention"]

    async def guard(self, delivery_id=None, replay=False):
        """Fence operations on paused or lost work before they reach Caura.

        ``delivery_id`` names the delivery the caller targets. ``replay`` marks an
        idempotent retry of a completion this session already sent, which Caura may
        answer with its stored result even after the delivery left this session.
        """
        claim = self.current
        if not claim:
            return
        if claim.state == "paused" and not claim.lease_token:
            # A human may have resolved the pause since it was observed. The local
            # copy cannot tell, so ask Caura instead of fencing every later call.
            claim = await self._refresh(claim, delivery_id, replay)
            if claim is None:
                return
        elif claim.lease_token:
            try:
                result = await self.bus.delivery_action(claim.delivery_id, "observe", claim.lease_token)
            except PlatformError as exc:
                if exc.status not in {404, 409} or isinstance(exc.detail, dict):
                    raise
                # The lease was lost (expiry, platform rebuild). Reclaim before reusing
                # an in-hand answer; the old token is never retried.
                claim = await self._refresh(claim, delivery_id, replay)
                if claim is None:
                    return
            else:
                claim = Claim.model_validate(result["delivery"])
                self.current = claim
        if claim.state == "paused":
            await self._observe_pause(claim)
            raise PlatformError(409, {"state": "paused", "delivery": self.public(claim)})
        if claim.state in {"acked", "cancelled"}:
            self.current = None
            self.on_finished(claim.delivery_id)

    async def _refresh(self, stale, delivery_id, replay=False):
        """Re-read a paused or lease-lost claim through this session's authenticated wait.

        Caura decides: a still-paused delivery stays paused, a resumed or expired one
        is leased to this session with a fresh private token, a rejected, cancelled or
        completed one never returns, and a live lease held by another session is
        never taken over.
        """
        async with self.lock:
            if self.current is stale:
                result = await self.bus.wait_result(self.session_id, 0)
                self.notices.extend(result.get("notices", []))
                claim = await self._adopt(result)
            else:
                claim = self.current  # A concurrent call already refreshed it.
        same = claim is not None and claim.delivery_id == stale.delivery_id
        if delivery_id == stale.delivery_id and not same and not replay:
            raise PlatformError(
                409,
                {
                    "state": "unavailable",
                    "delivery_id": stale.delivery_id,
                    "detail": "Caura no longer offers this delivery to this session (completed, "
                    "rejected, cancelled or reassigned). Do not continue or replay its work; "
                    "call peer wait.",
                },
            )
        if same and claim.state == "leased" and delivery_id == claim.delivery_id and self._unseen(claim):
            self.surfaced.add(str(claim.resume_context.get("intervention_id")))
            raise PlatformError(
                409,
                {
                    "state": "resumed",
                    "delivery": self.public(claim),
                    "detail": "A human resumed this delivery with instructions. Follow "
                    "resume_context.instructions, then retry.",
                },
            )
        return claim

    def _unseen(self, claim):
        context = claim.resume_context or {}
        if not context or str(context.get("intervention_id")) in self.surfaced:
            return False
        return context.get("action") != "approve" or bool(str(context.get("instructions") or "").strip())

    def token(self, delivery_id):
        if self.current and self.current.delivery_id == delivery_id:
            return self.current.lease_token
        return None

    def completed(self, delivery_id):
        if self.current and self.current.delivery_id == delivery_id:
            self.current = None
        self.on_finished(delivery_id)

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
