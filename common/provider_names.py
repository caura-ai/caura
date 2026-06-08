"""Canonical provider identifiers shared across services.

Internal comparison sites should use these enum members instead of
string literals (``provider == ProviderName.OPENAI`` rather than
``provider == "openai"``) to eliminate a whole class of typo bugs.

``StrEnum`` means members ARE strings — external callers can keep
passing literals ("openai", "gemini", ...) via env vars or JSON and
equality still works both ways.
"""

from __future__ import annotations

from enum import StrEnum


class ProviderName(StrEnum):
    """Canonical names for LLM / embedding providers.

    Values are the wire-format strings used in env vars, settings JSON,
    and logs. Treat this enum as the source of truth — add new members
    here rather than sprinkling new string literals across the codebase.
    """

    # Tenant-facing LLM providers
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    GEMINI = "gemini"

    # Platform-tier only (not valid as a tenant-facing provider name)
    VERTEX = "vertex"

    # Embedding-only
    LOCAL = "local"

    # Sentinels
    FAKE = "fake"
    NONE = "none"
