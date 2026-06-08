"""LLM-provider Protocol — moved from ``core_api.protocols`` (CAURA-595).

Async LLM provider interface for structured-JSON + free-text completions.
The same Protocol is consumed by ``common.enrichment.service`` (via
``get_llm_provider`` factory) and by core-api code paths that pre-date
the extraction (which keep working via the re-export at
``core_api.protocols.LLMProvider``).

Mirrors the embedding-provider extraction (CAURA-594 Step B): keep the
shared Protocol in ``common`` so both core-api and core-worker can import
it without the worker pulling in core_api as a dependency.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Async LLM provider for structured (JSON) and free-text completions."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Send a prompt and return a parsed JSON dict.

        Implementations are responsible for enforcing JSON output using
        whatever mechanism the underlying API supports (e.g., OpenAI
        ``response_format``, Vertex ``response_mime_type``, or prompt
        engineering for APIs without native JSON mode).

        Parameters that are not supported by a provider (e.g.,
        ``temperature``) may be silently ignored by the implementation.
        """
        ...

    async def complete_text(
        self,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> str:
        """Send a prompt and return raw text.

        Parameters that are not supported by a provider (e.g.,
        ``temperature``, ``max_tokens``) may be silently ignored by the
        implementation.
        """
        ...
