"""Vertex AI LLM provider (Gemini via Workload Identity / ADC).

Wraps the ``google-genai`` SDK in Vertex mode (``vertexai=True``) —
project/location auth via Application Default Credentials, no API key.
Migrated off ``vertexai.generative_models`` (deprecated June 2025,
removed June 2026): the legacy SDK only speaks the v1 API, where the
Gemini 3.x catalog is not published, and its region allowlist predates
the ``us``/``eu`` multi-regions the 3.x Flash-Lite models are served
from — so it structurally cannot reach any Gemini 3 model.

Multi-region endpoint quirk: ``us``/``eu`` are served from the bare
``aiplatform.googleapis.com`` host (same as ``global``), but the SDK
builds ``{location}-aiplatform.googleapis.com`` for any non-``global``
location — and ``us-aiplatform.googleapis.com`` is rejected with
``400 Invalid hostname``. ``_MULTI_REGION_LOCATIONS`` below pins the
base URL for those locations (verified live 2026-08-30/31: both
``gemini-3.5-flash-lite`` and ``gemini-3.1-flash-lite`` 200 on
``locations/us`` via the bare host, 404 on ``global`` and on regional
``us-central1``).

The SDK is synchronous, so all calls are wrapped in
``asyncio.to_thread()`` to avoid blocking the event loop (same pattern
as ``GeminiLLMProvider``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from common.llm.constants import LLM_JSON_MAX_OUTPUT_TOKENS
from common.llm.providers._shape_error import ProviderResponseShapeError
from common.llm.providers._truncation import raise_if_truncated

logger = logging.getLogger(__name__)

# Multi-region locations served from the bare aiplatform host. Regional
# locations (us-central1, ...) and ``global`` resolve correctly via the
# SDK's own endpoint logic and must NOT be overridden.
_MULTI_REGION_LOCATIONS = frozenset({"us", "eu"})
_BARE_VERTEX_BASE_URL = "https://aiplatform.googleapis.com/"


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
    """LLM provider using Vertex AI Gemini models via google-genai."""

    def __init__(
        self,
        project_id: str,
        location: str,
        model: str,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._model = model
        # Built lazily on first call: constructing the provider must not
        # require ADC (the platform singleton is instantiated at app
        # startup and in tests, where credentials may be absent), and
        # google.auth discovery belongs on the call path with the other
        # network work. Tests inject a fake by assigning ``_client``.
        # ``Any`` because the concrete ``genai.Client`` type cannot be
        # imported at module scope (optional-SDK rule — see module
        # docstring and test_optional_provider_sdks_stay_optional).
        self._client: Any = None
        # ``complete_*`` run in ``asyncio.to_thread`` workers, so
        # concurrent first calls on a shared instance (the platform
        # singleton) race ``_get_client`` from different threads —
        # without the lock each would build (and authenticate) its own
        # client and the losers' HTTP pools would linger until GC.
        self._client_lock = threading.Lock()

    @property
    def provider_name(self) -> str:
        return "vertex"

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self):
        # Double-checked locking: the unlocked read keeps the steady
        # state lock-free; the locked re-check makes concurrent first
        # calls build exactly one client.
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    # Imported lazily so `google-genai` remains an optional
                    # runtime dependency (same pattern as GeminiLLMProvider).
                    from google import genai
                    from google.genai import types

                    client_kwargs: dict = {
                        "vertexai": True,
                        "project": self._project_id,
                        "location": self._location,
                    }
                    if self._location in _MULTI_REGION_LOCATIONS:
                        client_kwargs["http_options"] = types.HttpOptions(
                            base_url=_BARE_VERTEX_BASE_URL
                        )
                    self._client = genai.Client(**client_kwargs)
        return self._client

    def _complete_json_sync(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Synchronous JSON completion via google-genai (Vertex mode)."""
        from google.genai import types

        t0 = time.perf_counter()
        response = self._get_client().models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=temperature,
                # Runaway guard: without a ceiling, a looping generation
                # runs to the model's own output limit and comes back as
                # truncated JSON (~200KB partials seen on prod 2026-08-26).
                max_output_tokens=LLM_JSON_MAX_OUTPUT_TOKENS,
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
        raise_if_truncated(
            response,
            provider="Vertex",
            model=self._model,
            max_tokens=LLM_JSON_MAX_OUTPUT_TOKENS,
        )
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
        """Synchronous text completion via google-genai (Vertex mode)."""
        from google.genai import types

        t0 = time.perf_counter()
        response = self._get_client().models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("Vertex complete_text (%s) took %dms", self._model, llm_ms)
        try:
            return response.text or ""
        except ValueError as exc:
            raise ValueError(
                f"Vertex model {self._model} returned no usable content (possible safety block): {exc}"
            ) from exc

    async def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        seed: int | None = None,
        response_schema: dict | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        """Async wrapper around synchronous Vertex AI JSON completion.

        ``seed`` / ``response_schema`` / ``reasoning_effort`` are
        accepted-and-ignored (OpenAI structured-output / reasoning
        kwargs) — see ``GeminiProvider.complete_json`` for why rejecting
        them silently broke entity extraction (C1).
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
        """Async wrapper around synchronous Vertex AI text completion."""
        return await asyncio.to_thread(
            self._complete_text_sync,
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
