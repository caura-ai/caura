"""Official Python client for Caura (formerly MemClaw) — governed shared
memory for AI agent fleets.

The 2026-08 rename is additive: ``Caura`` is the canonical client class and
``MemClaw`` remains a permanent alias, mirroring the MCP tool rename
convention. The separate legacy import package and the two legacy
package-forwarder distributions that once depended on this one were retired
2026-09; no transition is owed to pre-rename installs.
"""

from __future__ import annotations

from .client import DEFAULT_BASE_URL, Caura, MemClaw  # legacy-name-ok: rule 3 permanent class alias
from .exceptions import (
    AuthError,
    CauraAPIError,
    CauraError,
    MemClawAPIError,  # legacy-name-ok: rule 3 permanent exception alias
    MemClawError,  # legacy-name-ok: rule 3 permanent exception alias
    NotFoundError,
)
from .models import Memory, RecallResult

__all__ = [
    "Caura",
    "MemClaw",  # legacy-name-ok: rule 3 permanent class alias
    "Memory",
    "RecallResult",
    "CauraError",
    "CauraAPIError",
    "MemClawError",  # legacy-name-ok: rule 3 permanent exception alias
    "MemClawAPIError",  # legacy-name-ok: rule 3 permanent exception alias
    "AuthError",
    "NotFoundError",
    "DEFAULT_BASE_URL",
]

__version__ = "1.0.2"
