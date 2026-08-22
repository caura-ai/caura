"""Official Python client for Caura (formerly MemClaw) — governed shared
memory for AI agent fleets.

The 2026-08 rename is additive: ``Caura`` is the canonical client class and
``MemClaw`` remains a permanent alias, mirroring the MCP tool rename
(caura_* canonical, memclaw_* accepted forever). The ``memclaw_client``
import package likewise remains importable forever — see its ``__init__``.
"""

from __future__ import annotations

from .client import DEFAULT_BASE_URL, Caura, MemClaw
from .exceptions import (
    AuthError,
    CauraAPIError,
    CauraError,
    MemClawAPIError,
    MemClawError,
    NotFoundError,
)
from .models import Memory, RecallResult

__all__ = [
    "Caura",
    "MemClaw",
    "Memory",
    "RecallResult",
    "CauraError",
    "CauraAPIError",
    "MemClawError",
    "MemClawAPIError",
    "AuthError",
    "NotFoundError",
    "DEFAULT_BASE_URL",
]

__version__ = "1.0.0"
