"""Official Python client for Caura — governed shared memory for AI agent
fleets.
"""

from __future__ import annotations

# The MemClaw/MemClawError/MemClawAPIError class-level aliases from the  # legacy-name-ok: records the retirement of the class-alias surface
# 2026-08 rename were retired 2026-09, the same treatment already given to
# the separate legacy import package and the two legacy package-forwarder
# distributions that once depended on this one -- no transition is owed to
# pre-rename installs.

from .client import DEFAULT_BASE_URL, Caura
from .exceptions import (
    AuthError,
    CauraAPIError,
    CauraError,
    NotFoundError,
)
from .models import Memory, RecallResult, Stats

__all__ = [
    "Caura",
    "Memory",
    "RecallResult",
    "Stats",
    "CauraError",
    "CauraAPIError",
    "AuthError",
    "NotFoundError",
    "DEFAULT_BASE_URL",
]

__version__ = "1.0.2"
