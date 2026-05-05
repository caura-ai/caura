"""Vertex AI LLM provider.

Wraps the Google Cloud Vertex AI SDK. Since the SDK is synchronous,
all calls are wrapped in ``asyncio.to_thread()`` to avoid blocking
the event loop.

CAURA-333: ``VertexEmbeddingProvider`` was removed (broken — never passed
``output_dimensionality`` to the SDK, so writes failed against pgvector's
1024-dim column). Only the LLM-side provider remains.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)


class VertexLLMProvider:
    """LLM provider using Vertex AI Generative Models (Gemini).

    Matches the existing ``_vertex_enrich_sync`` and
    ``_vertex_contradiction_check_sync`` patterns from the codebase.
    """

    def __init__(
        self,
        project_id: str,
        location: str,
        model: str,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._model = model

    @property
    def provider_name(self) -> str:
        return "vertex"

    @property
    def model(self) -> str:
        return self._model

    def _complete_json_sync(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Synchronous JSON completion via Vertex AI GenerativeModel."""
        from google.cloud import aiplatform
        from vertexai.generative_models import GenerationConfig, GenerativeModel

        aiplatform.init(project=self._project_id, location=self._location)
        model = GenerativeModel(self._model)

        t0 = time.perf_counter()
        response = model.generate_content(
            prompt,
            generation_config=GenerationConfig(
                response_mime_type="application/json",
                temperature=temperature,
            ),
        )
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("Vertex complete_json (%s) took %dms", self._model, llm_ms)
        return json.loads(response.text)

    def _complete_text_sync(
        self,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> str:
        """Synchronous text completion via Vertex AI GenerativeModel."""
        from google.cloud import aiplatform
        from vertexai.generative_models import GenerationConfig, GenerativeModel

        aiplatform.init(project=self._project_id, location=self._location)
        model = GenerativeModel(self._model)

        t0 = time.perf_counter()
        response = model.generate_content(
            prompt,
            generation_config=GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("Vertex complete_text (%s) took %dms", self._model, llm_ms)
        return response.text or ""

    async def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Async wrapper around synchronous Vertex AI JSON completion."""
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
        """Async wrapper around synchronous Vertex AI text completion."""
        return await asyncio.to_thread(
            self._complete_text_sync,
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
