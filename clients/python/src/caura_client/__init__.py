"""Official Python client for Caura (formerly MemClaw) — governed shared
memory for AI agent fleets.

The 2026-08 rename is additive: ``Caura`` is the canonical client class and
``MemClaw`` remains a permanent alias, mirroring the MCP tool rename
convention. The separate legacy import package and the two legacy
package-forwarder distributions that once depended on this one were retired
2026-09; no transition is owed to pre-rename installs.
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

__version__ = "1.0.1"
