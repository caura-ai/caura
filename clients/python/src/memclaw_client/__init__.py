"""Official Python client for MemClaw — governed shared memory for AI agent fleets."""

from __future__ import annotations

from ._version import __version__
from .client import DEFAULT_BASE_URL, MemClaw
from .exceptions import AuthError, MemClawAPIError, MemClawError, NotFoundError
from .models import Memory, RecallResult

__all__ = [
    "MemClaw",
    "Memory",
    "RecallResult",
    "MemClawError",
    "MemClawAPIError",
    "AuthError",
    "NotFoundError",
    "DEFAULT_BASE_URL",
    "__version__",
]
