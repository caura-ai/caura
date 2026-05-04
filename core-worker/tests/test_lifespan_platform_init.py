"""Regression: lifespan must init BOTH platform singletons (CAURA-647).

Pre-fix the worker called only ``init_platform_embedding`` on startup,
so the LLM singleton (built from ``PLATFORM_LLM_*``) stayed ``None``
even when the env was correctly configured. Any enrich-request whose
payload resolved to ``ProviderName.OPENAI`` (the default when the
publisher omits ``enrichment_provider``) without a tenant openai key
hit ``get_llm_provider("openai")`` → no key → ``get_platform_llm()``
returned ``None`` → ``FakeLLMProvider``. The drift-recovery re-enrich
on caura-ai (post-CAURA-641) silently corrupted ~5,000 rows before the
bug surfaced.

This test pins the invariant: opening the lifespan must call the
combined ``init_platform_providers`` (which calls both
``init_platform_embedding`` and the LLM init), not the embedding-only
shim.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from core_worker.app import create_app


def test_lifespan_calls_init_platform_providers():
    """Opening the FastAPI app's lifespan must invoke
    :func:`common.llm.init_platform_providers` exactly once.

    The earlier code path called ``init_platform_embedding`` only —
    fine for embeds (the dedicated embedding singleton was built), but
    the LLM half of the platform tier was never materialised, so any
    enrichment fall-through to platform-LLM returned ``None`` and the
    enrichment service silently dropped to FakeLLMProvider.
    """
    bus = MagicMock()
    bus.start = AsyncMock(return_value=None)
    bus.stop = AsyncMock(return_value=None)
    bus.is_healthy = True

    with (
        patch("core_worker.app.init_platform_providers") as init_mock,
        patch("core_worker.app.configure_consumer"),
        patch("core_worker.app.register_consumers"),
        patch("core_worker.app.get_event_bus", return_value=bus),
        patch("core_worker.app.close_storage_client", new=AsyncMock()),
    ):
        app = create_app()
        # Use TestClient as a context manager to trigger the lifespan
        # startup/shutdown without binding to a real port.
        with TestClient(app):
            pass

        init_mock.assert_called_once_with()
