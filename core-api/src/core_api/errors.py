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
