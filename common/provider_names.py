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


# Single source of truth for the embedding-provider default. Two resolution
# paths read ``EMBEDDING_PROVIDER``: core-api's pydantic ``Settings``
# (tenant-aware callers, via ``ResolvedConfig``) and
# ``common.embedding._service._resolve_provider_name`` (tenant-config-less
# callers — doc/skill writes, MCP doc ops, entity embeddings, the storage
# backfill CLI). They historically carried different fallbacks ("openai" vs
# "fake"), so with the env var unset the same process embedded memories with
# a real provider while silently persisting fake vectors everywhere else.
# Both defaults MUST come from here. Lives in this leaf module (not
# ``common.embedding.constants``) so ``core_api.config`` can import it
# without pulling the whole embedding package at settings-import time.
DEFAULT_EMBEDDING_PROVIDER: str = ProviderName.OPENAI.value
