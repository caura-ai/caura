"""A small stateful stand-in for Caura pull delivery, enough to exercise MCP lease recovery.

It mirrors the platform rules the MCP client relies on: wait serves one delivery per
recipient, returns a live lease only to the session that owns it, reissues a token
when it reclaims pending work, and every lease operation checks the current token.
Human decisions reset the delivery to pending (approve/redirect) or cancel it (reject).
"""

import json
import secrets

import httpx
from caura_bus_core import AgentConfig, Bus
from caura_bus_core.envelope import Envelope
from caura_bus_mcp.server import AppContext


class Platform:
    def __init__(self):
        self.request = Envelope.new(from_="architect", to=["developer"], body="Review", kind="request")
        self.state = "pending"
        self.token: str | None = None
        self.session: str | None = None
        self.live = False
        self.attempt = 0
        self.resume_context: dict | None = None
        self.intervention: dict | None = None
        self.calls: list[tuple[str, dict]] = []
        self.replies: dict[str, dict] = {}
        self.notices: list[dict] = []

    # Human and infrastructure controls.
    def pause(self):
        self.state = "paused"
        self.intervention = {"id": "case-1", "state": "pending", "stop_status": "pause_requested"}

    def decide(self, action, instructions=""):
        context = {"intervention_id": "case-1", "action": action, "instructions": instructions}
        self.intervention = None
        self.token = self.session = None
        self.live = False
        self.attempt = 0
        if action == "reject":
            self.state = "cancelled"
        else:
            self.state, self.resume_context = "pending", context

    def expire(self):
        """The lease lapsed (for example during a rebuild); the sweeper returns it to pending."""
        self.state, self.token, self.session, self.live = "pending", None, None, False

    def view(self, session_id=None):
        return {
            "delivery_id": "d1",
            "lease_token": self.token if session_id and session_id == self.session else None,
            "lease_expires_at": "2026-10-06T00:00:30+00:00",
            "attempt": self.attempt,
            "state": self.state,
            "envelope": self.request.model_dump(by_alias=True),
            "resume_context": self.resume_context,
            "intervention": self.intervention if self.state == "paused" else None,
        }

    def _active(self, token):
        if self.state == "paused":
            return httpx.Response(409, json={"detail": {"state": "paused", "delivery_id": "d1"}})
        if not token or token != self.token:
            return httpx.Response(409, json={"detail": "stale delivery lease"})
        if self.state != "leased" or not self.live:
            return httpx.Response(409, json={"detail": "delivery lease expired or no longer active"})
        return None

    def wait(self, session_id):
        notices, self.notices = self.notices, []
        if self.state == "paused":
            return {"delivery": self.view(session_id), "notices": notices}
        if self.state == "leased" and self.live:
            return {
                "delivery": self.view(session_id) if session_id == self.session else None,
                "notices": notices,
            }
        if self.state in {"pending", "leased"}:
            self.state, self.live, self.session = "leased", True, session_id
            self.token = secrets.token_urlsafe(16)
            self.attempt += 1
            return {"delivery": self.view(session_id), "notices": notices}
        return {"delivery": None, "notices": notices}

    async def handle(self, request):
        path = request.url.path.removeprefix("/api/v1/bus")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        if path == "/inbox/wait":
            return httpx.Response(200, json=self.wait(body["session_id"]))
        if path.startswith("/deliveries/d1/"):
            action = path.rsplit("/", 1)[1]
            token = body.get("lease_token")
            if action == "observe":
                if not token or token != self.token:
                    return httpx.Response(409, json={"detail": "stale delivery lease"})
                return httpx.Response(200, json={"delivery": self.view(self.session)})
            if action == "caura-stopped":
                if self.state != "paused":
                    return httpx.Response(409, json={"detail": "delivery is not paused"})
                if not token or token != self.token:
                    return httpx.Response(409, json={"detail": "stale delivery lease"})
                self.token = None
                self.intervention["stop_status"] = "caura_stop_confirmed"
                return httpx.Response(200, json={"status": "paused", "intervention": self.intervention})
            if action == "reply":
                key = request.headers["Idempotency-Key"]
                if key in self.replies and self.state == "acked":
                    return httpx.Response(202, json={**self.replies[key], "duplicate": True})
            if rejected := self._active(token):
                return rejected
            if action == "renew":
                return httpx.Response(200, json={"lease_expires_at": "2026-10-06T00:01:00+00:00"})
            if action == "progress":
                return httpx.Response(200, json={"status": "leased", "extension_count": 1})
            if action == "reply":
                receipt = {
                    "message_id": "reply-1",
                    "thread_id": self.request.thread_id,
                    "recipients": ["architect"],
                }
                self.replies[key] = receipt
                if body.get("ack", True):
                    self.state, self.token, self.live = "acked", None, False
                return httpx.Response(202, json=receipt)
            if action == "ack":
                self.state, self.token, self.live = "acked", None, False
                return httpx.Response(200, json={"status": "acked"})
        if path == "/discover":
            return httpx.Response(200, json=[{"agent_id": "architect"}])
        return httpx.Response(404, json={"detail": "unexpected " + path})

    def actions(self, name):
        return [body for path, body in self.calls if path == "/deliveries/d1/" + name]


def session(platform):
    config = AgentConfig(
        api_url="https://caura.test",
        agent={"agent_id": "developer", "tenant_id": "tenant"},
        peers=["architect"],
    )
    bus = Bus(config, api_key="test-key", transport=httpx.MockTransport(platform.handle))
    return AppContext(config, bus)


async def close(app):
    await app.delivery.close()
    await app.bus.close()
