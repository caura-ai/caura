"""Async client. No Redis access, anonymous mode, or fallback transport."""

from __future__ import annotations

import asyncio
import json
import secrets
from time import monotonic

import httpx

from .agent import AgentConfig
from .collaboration import Checkpoint, Presence
from .config import require_api_key
from .protocol import Claim, Receipt, SendMessage
from .retry import Backoff, transient_status


class HumanRequired(RuntimeError):
    def __init__(self, intervention):
        self.intervention = intervention
        super().__init__("Caura requires human input before this work can continue")


class PlatformError(RuntimeError):
    def __init__(self, status: int, detail: object):
        self.status = status
        self.detail = detail
        super().__init__(f"Caura returned {status}: {detail}")


class Bus:
    def __init__(
        self,
        config: AgentConfig,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if config.api_url.startswith("http:") and not config.allow_insecure_http:
            raise ValueError("HTTPS is required; allow_insecure_http is only for isolated local demos")
        self.config = config
        self._notice_session: str | None = None
        self._notice_receipt: str | None = None
        self._http = httpx.AsyncClient(
            base_url=config.api_url + "/api/v1/bus/",
            headers={"X-API-Key": api_key or require_api_key()},
            timeout=httpx.Timeout(20, connect=5),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self):
        try:
            await self.connect()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def connect(self):
        identity = await self.request("GET", "identity")
        expected = self.config.agent
        if (identity["agent_id"], identity["tenant_id"]) != (expected.agent_id, expected.tenant_id):
            raise PlatformError(403, "credential does not match configured agent and tenant")
        return identity

    async def close(self):
        await self._http.aclose()

    async def request(self, method: str, path: str, *, retry_safe: bool = False, **kwargs):
        # Snapshot proof of the previous response; retries retain exactly the
        # same acknowledgement even if another concurrent call receives notices.
        headers = dict(kwargs.pop("headers", {}))
        if self._notice_session and self._notice_receipt:
            headers["X-Caura-Session-ID"] = self._notice_session
            headers["X-Caura-Notice-Receipt"] = self._notice_receipt
        kwargs["headers"] = headers
        # Only operations with durable idempotency retry ambiguous responses.
        for attempt in range(3):
            try:
                response = await self._http.request(method, path, **kwargs)
            except httpx.TransportError:
                if not retry_safe or attempt == 2:
                    raise
            else:
                if response.is_success:
                    return response.json()
                if not transient_status(response.status_code) or not retry_safe or attempt == 2:
                    try:
                        detail = response.json().get("detail", "request rejected")
                    except ValueError:
                        detail = "gateway rejected request"
                    raise PlatformError(response.status_code, detail)
            await asyncio.sleep(0.25 * (2**attempt) + secrets.randbelow(100) / 1000)
        raise AssertionError("unreachable")

    async def send(self, message: SendMessage, *, idempotency_key: str) -> Receipt:
        if (
            message.kind != "response"
            and "*" not in self.config.peers
            and not set(message.to) <= set(self.config.peers)
        ):
            raise ValueError("recipient is outside the local peer allow-list")
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("idempotency_key must contain 1–128 characters")
        result = await self.request(
            "POST",
            "messages",
            json=message.model_dump(),
            headers={"Idempotency-Key": idempotency_key},
            retry_safe=True,
        )
        return Receipt.model_validate(result)

    async def claim(self, lease_seconds: int = 30) -> Claim | None:
        data = await self.request("POST", "inbox/claim", json={"lease_seconds": lease_seconds})
        return Claim.model_validate(data["delivery"]) if data["delivery"] else None

    async def wait(self, session_id: str, timeout: float = 50) -> Claim | None:
        result = await self.wait_result(session_id, timeout)
        return Claim.model_validate(result["delivery"]) if result["delivery"] else None

    async def wait_result(self, session_id: str, timeout: float = 50) -> dict:
        # Short HTTP polls fit the Caura gateway's 25-second upstream timeout and
        # revalidate credentials; the model still sees one bounded MCP wait.
        deadline = monotonic() + timeout
        failures = 0
        responded = False
        while True:
            remaining = max(0, deadline - monotonic())
            poll = min(20, remaining)
            try:
                async with asyncio.timeout(remaining + 1 if timeout else 5):
                    data = await self.request(
                        "POST",
                        "inbox/wait",
                        json={"session_id": session_id, "timeout": poll},
                        timeout=httpx.Timeout(min(poll + 5, remaining + 1) if timeout else 5, connect=5),
                    )
                responded = True
                failures = 0
                if data.get("notice_receipt"):
                    self._notice_session = session_id
                    self._notice_receipt = data["notice_receipt"]
                if data["delivery"] or data.get("notices"):
                    return data
            except PlatformError as exc:
                if exc.status < 500:
                    raise
                failures += 1
            except (httpx.TransportError, TimeoutError):
                failures += 1
            remaining = deadline - monotonic()
            if remaining <= 0 or not timeout:
                if responded:
                    return {"delivery": None, "notices": []}
                raise PlatformError(503, "Caura remained unavailable during wait; retry wait")
            if failures:
                await asyncio.sleep(min(remaining, 0.25 * 2 ** min(failures - 1, 4)))

    async def inbox_state(self):
        return await self.request("GET", "inbox/state")

    async def delivery_action(self, delivery_id: str, action: str, token=None, **params):
        return await self.request(
            "POST",
            f"deliveries/{delivery_id}/{action}",
            json={"lease_token": token, **params},
            retry_safe=action in {"ack", "progress"},
        )

    async def reply(
        self,
        delivery_id: str,
        *,
        token=None,
        idempotency_key: str,
        body: str,
        reply_to=None,
        ack=True,
    ):
        return await self.request(
            "POST",
            f"deliveries/{delivery_id}/reply",
            json={"lease_token": token, "body": body, "reply_to": reply_to, "ack": ack},
            headers={"Idempotency-Key": idempotency_key},
            retry_safe=True,
        )

    async def settle(self, claim: Claim, action: str):
        if action not in {"ack", "nack", "renew", "paused"}:
            raise ValueError("unknown delivery action")
        return await self.request(
            "POST",
            f"deliveries/{claim.delivery_id}/{action}",
            json={"lease_token": claim.lease_token},
            retry_safe=action == "ack",
        )

    async def agents(self, fleet_id: str | None = None):
        return await self.request("GET", "agents", params={"fleet_id": fleet_id} if fleet_id else {})

    async def recent(
        self,
        *,
        thread_id: str | None = None,
        peer_agent_id: str | None = None,
        limit: int = 20,
        before: str | None = None,
    ):
        params = {
            "limit": limit,
            "thread_id": thread_id,
            "peer_agent_id": peer_agent_id,
            "before": before,
        }
        return await self.request(
            "GET", "messages", params={k: v for k, v in params.items() if v is not None}
        )

    async def threads(self):
        return await self.request("GET", "threads")

    async def requests(self, *, state=None, limit=20):
        return await self.request(
            "GET",
            "requests",
            params={k: v for k, v in {"state": state, "limit": limit}.items() if v is not None},
        )

    async def status(self, message_id: str):
        return await self.request("GET", f"messages/{message_id}")

    async def memory_context(self, *, message_id: str | None = None, delivery_id: str | None = None):
        from .protocol import MemoryContextRequest

        params = MemoryContextRequest(message_id=message_id, delivery_id=delivery_id)
        return await self.request("GET", "memory-context", params=params.model_dump(exclude_none=True))

    async def advertise(self, profile: Presence):
        return await self.request("PUT", "presence", json=profile.model_dump(), retry_safe=True)

    async def discover(self, *, capability: str | None = None, available_only=True, fleet_id=None):
        params = {"capability": capability, "available_only": available_only, "fleet_id": fleet_id}
        return await self.request(
            "GET", "discover", params={k: v for k, v in params.items() if v is not None}
        )

    async def checkpoint(self, checkpoint: Checkpoint):
        result = await self.request("POST", "checkpoints", json=checkpoint.model_dump(), retry_safe=True)
        if result["decision"] == "human_required":
            raise HumanRequired(result["intervention"])
        return result

    async def escalate(self, delivery_id: str, reason: str):
        return await self.request(
            "POST", "interventions", json={"delivery_id": delivery_id, "reason": reason}
        )

    async def events(self, after=0):
        """Resume a live stream by durable cursor; reconnect revalidates credentials."""
        cursor = after
        backoff = Backoff(maximum=15)
        while True:
            try:
                async with self._http.stream(
                    "GET",
                    "events",
                    params={"after": cursor},
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    if response.status_code != 200:
                        if not transient_status(response.status_code):
                            raise PlatformError(response.status_code, "live event access failed")
                    else:
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                event = json.loads(line[6:])
                                cursor = event["seq"]
                                backoff.reset()
                                yield event
            except httpx.TransportError:
                pass
            # This also bounds reconnects when an upstream closes an empty
            # stream successfully, without supplying an event or heartbeat.
            await backoff.sleep()
