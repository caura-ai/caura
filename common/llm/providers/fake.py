"""Fake/deterministic LLM provider for testing — moved from
``core_api.providers.fake_provider`` (CAURA-595).

Requires no external API keys and always succeeds. Useful for tests,
development, and as the last-resort fallback when all real providers
are unavailable.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class FakeLLMProvider:
    """LLM provider that returns empty/default responses without any API calls."""

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake"

    @property
    def is_fake(self) -> bool:
        return True

    async def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Return an empty JSON object."""
        return {}

    async def complete_text(
        self,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> str:
        """Return an empty string."""
        return ""
