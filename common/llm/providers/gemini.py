"""Gemini LLM provider (Google Developer API, API-key auth).

Wraps the ``google-genai`` SDK in Gemini Developer API mode — key-auth,
no GCP project or ADC required. This is the tenant-facing way to use
Gemini models; the Vertex code path is reserved for platform-tier
singletons configured by enterprise operators.

The SDK is synchronous, so all calls are wrapped in ``asyncio.to_thread()``
to avoid blocking the event loop (same pattern as ``VertexLLMProvider``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from common.llm.providers._shape_error import ProviderResponseShapeError
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)


# CAURA-651: same hazard as VertexResponseShapeError — Gemini's
# ``response_mime_type='application/json'`` doesn't fully constrain
# the top-level shape, so a list (or other non-dict) can leak through
# and cause downstream ``.get(...)`` to raise bare AttributeError.
class GeminiResponseShapeError(ProviderResponseShapeError):
    def __init__(self, content: str, parsed_type: str) -> None:
        super().__init__("Gemini", content, parsed_type)

    def __reduce__(self) -> tuple:
        # See VertexResponseShapeError.__reduce__ for rationale.
        return (type(self), (self.args[1], self.args[2]))


class GeminiLLMProvider:
    """LLM provider using the Gemini Developer API with an API key."""

    def __init__(
        self,
        api_key: str,
        model: str,
    ) -> None:
        # Imported lazily so `google-genai` remains an optional runtime
        # dependency (same pattern as VertexLLMProvider with vertexai).
        from google import genai

        self._api_key = api_key
        self._model = model
        # Build the SDK client once — reuse the underlying HTTP session
        # across calls instead of reconstructing it per request.
        self._client = genai.Client(api_key=self._api_key)

    @property
    def provider_name(self) -> str:
        return ProviderName.GEMINI

    @property
    def model(self) -> str:
        return self._model

    def _complete_json_sync(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Synchronous JSON completion via google-genai."""
        from google.genai import types

        t0 = time.perf_counter()
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=temperature,
            ),
        )
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("Gemini complete_json (%s) took %dms", self._model, llm_ms)
        try:
            text = response.text or ""
        except ValueError as exc:
            raise ValueError(
                f"Gemini model {self._model} returned no usable content (possible safety block): {exc}"
            ) from exc
        if not text:
            raise ValueError(f"Gemini returned empty content for model {self._model}")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise GeminiResponseShapeError(text, type(parsed).__name__)
        return parsed

    def _complete_text_sync(
        self,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> str:
        """Synchronous text completion via google-genai."""
        from google.genai import types

        t0 = time.perf_counter()
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("Gemini complete_text (%s) took %dms", self._model, llm_ms)
        try:
            return response.text or ""
        except ValueError as exc:
            raise ValueError(
                f"Gemini model {self._model} returned no usable content (possible safety block): {exc}"
            ) from exc

    async def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        seed: int | None = None,
        response_schema: dict | None = None,
    ) -> dict:
        """Async wrapper around the synchronous Gemini JSON completion.

        ``seed`` / ``response_schema`` are accepted-and-ignored (OpenAI
        structured-output kwargs). Rejecting them made every
        ``complete_json(..., seed=..., response_schema=...)`` caller —
        notably entity extraction — raise ``TypeError``, exhaust its
        retries, and silently degrade to the regex fallback on any
        Gemini-configured tenant (audit C1).
        """
        return await asyncio.to_thread(
            self._complete_json_sync, prompt, temperature=temperature
        )

    async def complete_text(
        self,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> str:
        """Async wrapper around the synchronous Gemini text completion."""
        return await asyncio.to_thread(
            self._complete_text_sync,
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
