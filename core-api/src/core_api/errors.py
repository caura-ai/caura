"""Canonical error contract shared by REST and MCP surfaces.

Both surfaces should return errors in the shape::

    {
      "error": {
        "code": "<UPPER_SNAKE>",
        "message": "<human-readable>",
        "details": { ... optional ... }
      }
    }

REST additionally keeps the ``detail`` top-level field for back-compat
with existing clients that read ``response.json()["detail"]``. The
``error`` field is the canonical surface; ``detail`` is the deprecated
mirror.

This module is import-safe: pure data, no side effects, no FastAPI
imports — so it can be used by MCP tools, REST routes, the storage
client, or anywhere else without dragging in a request stack.
"""

from __future__ import annotations

# Mapping from HTTP status code → canonical error code. Used when callers
# raise ``HTTPException(status_code=N, detail="...")`` without supplying an
# explicit code. The handler in ``app.py`` derives the code from the
# status code via this table; if a status isn't listed, it falls back to
# ``HTTP_<status>`` (e.g. ``HTTP_418``).
STATUS_TO_CODE: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    402: "PAYMENT_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    408: "REQUEST_TIMEOUT",
    409: "CONFLICT",
    410: "GONE",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "INVALID_ARGUMENTS",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    502: "UPSTREAM_ERROR",
    503: "UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}


def code_for_status(status: int) -> str:
    """Return the canonical error code for an HTTP status, or HTTP_<status> if unknown."""
    return STATUS_TO_CODE.get(status, f"HTTP_{status}")


def make_error_payload(
    code: str,
    message: str,
    details: dict | None = None,
) -> dict:
    """Return the canonical error envelope.

    Use this from both REST and MCP error sites so the on-the-wire shape
    is identical. ``details`` is included only when non-empty.
    """
    payload: dict = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    return payload


# ── Codes for the auth boundary (C32 / API-05) ────────────────────────────
#
# Why these exist at all. ``code_for_status`` maps every 403 to ``FORBIDDEN``
# and every 401 to ``UNAUTHORIZED``, so the eight distinct reasons a request can
# be refused at this boundary arrived at the caller as one word. An agent that
# gets FORBIDDEN on a write cannot tell "this key is read-only" from "your org
# is over its plan limit" from "this action needs an admin" — and the only move
# left is to guess. One did: an agent was refused a write with a tenant key and
# concluded that tenant keys cannot write, which is false, and then stopped
# trying. A wrong general rule learned from a specific refusal is worse than no
# answer, because the agent stops asking.
#
# Each code names one reason and each message says what would work instead.
# ``MISSING_AGENT_ID`` in ``mcp_server`` is the shape being copied: what
# happened, why, and the concrete way out.
AUTH_READ_ONLY_KEY = "READ_ONLY_CREDENTIAL"
AUTH_DEMO_SANDBOX = "DEMO_SANDBOX_READ_ONLY"
AUTH_PLAN_LIMIT = "PLAN_LIMIT_READ_ONLY"
AUTH_ADMIN_REQUIRED = "ADMIN_REQUIRED"
AUTH_ORG_ADMIN_REQUIRED = "ORG_ADMIN_REQUIRED"
AUTH_MISSING_API_KEY = "MISSING_API_KEY"
AUTH_INVALID_API_KEY = "INVALID_API_KEY"
AUTH_MISSING_TENANT_CONTEXT = "MISSING_TENANT_CONTEXT"
AUTH_GATEWAY_ONLY = "GATEWAY_ONLY"
AUTH_AGENT_CREDENTIAL_FORBIDDEN = "AGENT_CREDENTIAL_FORBIDDEN"
AUTH_AGENT_IDENTITY_MISMATCH = "AGENT_IDENTITY_MISMATCH"
AUTH_TENANT_REQUIRED = "TENANT_REQUIRED"
AUTH_TENANT_MISMATCH = "TENANT_MISMATCH"
AUTH_TENANT_NOT_READABLE = "TENANT_NOT_READABLE"
AUTH_CROSS_TENANT_REQUIRED = "CROSS_TENANT_READ_REQUIRED"
AUTH_ORG_SUSPENDED = "ORGANIZATION_SUSPENDED"

# C32 — the reasons the remaining 33 refusal sites actually give. Added rather
# than folded into the codes above because each one leaves the caller a
# DIFFERENT next move, which is the whole test for whether a code earns its
# place: "register this agent" and "raise this agent's trust_level" are both
# FORBIDDEN and are not the same instruction.
AUTH_AGENT_NOT_REGISTERED = "AGENT_NOT_REGISTERED"
AUTH_AGENT_TRUST_TOO_LOW = "AGENT_TRUST_TOO_LOW"
AUTH_TARGET_AGENT_RESTRICTED = "TARGET_AGENT_RESTRICTED"
AUTH_FLEET_SCOPE_FORBIDDEN = "FLEET_SCOPE_FORBIDDEN"
AUTH_FEATURE_DISABLED = "FEATURE_DISABLED"

# Three codes whose VALUES are inherited, not chosen. ``skills_inbox`` and
# ``stm`` already shipped a machine-readable token welded to the front of the
# prose — ``"TENANT_MISMATCH — this credential is not scoped…"`` — with a
# comment at the site stating that clients branch on the prefix and not on the
# message. That is a live contract, so moving it into ``error.code`` must keep
# the string byte-for-byte; a "tidier" spelling here would be a silent breaking
# change for every caller already parsing it. ``TENANT_MISMATCH`` needed no new
# constant — ``AUTH_TENANT_MISMATCH`` above already carries that exact value.
AUTH_UNAUTHENTICATED = "UNAUTHENTICATED"
AUTH_SKILLS_FACTORY_DISABLED = "SKILLS_FACTORY_DISABLED"
AUTH_SKILLS_INBOX_FORBIDDEN = "SKILLS_INBOX_FORBIDDEN"


def coded_detail(code: str, message: str, **details: object) -> dict:
    """An ``HTTPException`` detail that keeps its own error code.

    ``app.http_exception_handler`` recognises this shape and emits
    ``{"detail": message, "error": {"code", "message", "details"}}`` — so the
    top-level ``detail`` a client already reads is unchanged and stays a plain
    string, while the code and any structured context ride alongside.

    Use this instead of a bare string wherever the caller's next action depends
    on WHICH refusal this is. A bare string is not wrong, it is just
    indistinguishable: it collapses into the status-derived code above.
    """
    return {"code": code, "message": message, "details": dict(details)}
