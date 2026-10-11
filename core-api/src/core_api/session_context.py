"""The agent session a request belongs to (value metering, docs/plans/value-metering.md).

Every KPI that says "per session" reads this: a memory is reused once per new
session that recalls it, and its origin time runs from its session's start or
previous write.

Where it comes from:

* MCP. The server is stateless (``stateless_http=True``), so the SDK issues no
  session. ``MCPAuthMiddleware`` issues one instead — a label, not server
  state — on the response to a request that carried none, and clients send it
  back as ``Mcp-Session-Id`` on every later call, as the spec has them do. In
  stateless mode the SDK does not validate the header, so nothing else moves.
* REST and SDKs. ``X-Caura-Session-Id``, read by ``SessionHeaderMiddleware``.
* The broker. Stamps ``metadata.session_id`` on what it writes, which a write
  keeps over this (``memory_service``).

A label is attribution, not authority: it scopes nothing and grants nothing.
"""

from __future__ import annotations

import contextvars
import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MCP_SESSION_HEADER = b"mcp-session-id"
REST_SESSION_HEADER = b"x-caura-session-id"

# Visible ASCII, bounded: it lands in metadata and an index key.
_VALID = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")

_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("caura_session_id", default=None)
_session_source: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "caura_session_source", default=None
)


def current_session_id() -> str | None:
    return _session_id.get()


def current_session_source() -> str | None:
    """``mcp`` or ``rest``; None outside a session."""
    return _session_source.get()


def valid_session_id(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if _VALID.match(value) else None


def set_session(session_id: str | None, source: str | None) -> None:
    _session_id.set(session_id)
    _session_source.set(source if session_id else None)


def new_mcp_session_id() -> str:
    return f"mcp-{uuid.uuid4().hex}"


async def run_with_mcp_session(scope: Scope, receive: Receive, send: Send, app: ASGIApp) -> None:
    """Read the MCP session label, or issue one on the response if the request had none."""
    headers = dict(scope.get("headers", []))
    sent = valid_session_id(headers.get(MCP_SESSION_HEADER, b"").decode(errors="ignore"))
    session = sent or new_mcp_session_id()
    set_session(session, "mcp")
    if sent:
        await app(scope, receive, send)
        return

    async def send_with_session(message: Message) -> None:
        if message["type"] == "http.response.start":
            existing = [k for k, _ in message.get("headers", [])]
            if MCP_SESSION_HEADER not in existing:
                message = {
                    **message,
                    "headers": [*message.get("headers", []), (MCP_SESSION_HEADER, session.encode())],
                }
        await send(message)

    await app(scope, receive, send_with_session)


class SessionHeaderMiddleware:
    """REST: take the session from ``X-Caura-Session-Id``, and clear it otherwise.

    Cleared on every request, like the MCP identity vars, so a reused task
    cannot carry a previous request's session into this one.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            session = valid_session_id(headers.get(REST_SESSION_HEADER, b"").decode(errors="ignore"))
            set_session(session, "rest")
        await self.app(scope, receive, send)
