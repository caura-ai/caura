"""Exceptions raised by the Caura client."""

from __future__ import annotations

from typing import Any


class CauraError(Exception):
    """Base class for all Caura client errors."""


class CauraAPIError(CauraError):
    """Raised when the Caura API returns a non-success status code.

    The structured ``error`` envelope (``{"error": {"code", "message", "details"}}``)
    is parsed when present; otherwise the raw body is used as the message.
    """

    def __init__(self, status_code: int, message: str, *, details: Any = None) -> None:
        self.status_code = status_code
        self.details = details
        super().__init__(f"[{status_code}] {message}")


class AuthError(CauraAPIError):
    """Raised on 401/403 — bad or insufficiently-scoped credential."""


class NotFoundError(CauraAPIError):
    """Raised on 404."""
