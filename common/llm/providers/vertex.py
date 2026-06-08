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

from common.llm.providers._shape_error import ProviderResponseShapeError

logger = logging.getLogger(__name__)


# CAURA-651: Gemini (via Vertex) occasionally returns a JSON array at
# the top level even with ``response_mime_type="application/json"`` set
# — typically on prompts that ask for "a list of" something where the
# model misinterprets the schema. Downstream consumers expect a dict
# and call ``.get(...)``, raising bare ``AttributeError: 'list' object
# has no attribute 'get'`` and silently falling through to the FakeLLM
# fallback. Surface this as a typed error with the actual response
# captured so log-based forensics can identify the schema-miss class.
class VertexResponseShapeError(ProviderResponseShapeError):
    def __init__(self, content: str, parsed_type: str) -> None:
        super().__init__("Vertex", content, parsed_type)

    def __reduce__(self) -> tuple:
        # Base sets ``self.args = (provider, content, parsed_type)``
        # but this subclass takes only ``(content, parsed_type)`` —
        # drop the hardcoded provider arg so pickle round-trips
        # cleanly (matters for pytest-xdist + any multiprocessing
        # exception serialisation).
        return (type(self), (self.args[1], self.args[2]))


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
        # Guard ``response.text`` access the same way Gemini does: a
        # safety-blocked response can set ``.text`` to ``None`` (or
        # raise ``ValueError`` on access), and ``json.loads(None)``
        # would surface as a bare ``TypeError`` that's harder to
        # diagnose than the structured ValueError this branch raises.
        try:
            text = response.text or ""
        except ValueError as exc:
            raise ValueError(
                f"Vertex model {self._model} returned no usable content (possible safety block): {exc}"
            ) from exc
        if not text:
            raise ValueError(f"Vertex returned empty content for model {self._model}")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            # CAURA-651: see ``VertexResponseShapeError`` above.
            raise VertexResponseShapeError(text, type(parsed).__name__)
        return parsed

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
