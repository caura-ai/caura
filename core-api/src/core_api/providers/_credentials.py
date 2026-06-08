"""Back-compat re-export shim for :mod:`common.llm._credentials`
(CAURA-595).

Implementation moved to ``common/llm/_credentials.py`` so core-worker
can resolve credentials without importing core-api. This shim keeps
existing call sites (``core_api.providers._registry``, tests) working
unchanged.

The pydantic-settings ``.env`` fallback for local dev is handled by
:func:`core_api.config.bridge_credentials_to_environ`, called from
the FastAPI lifespan at startup. That function copies any
``settings.X`` credential values into ``os.environ`` so the common
helpers below — which read ``os.environ`` directly so core-worker
stays settings-free — see ``.env``-loaded values too.
"""

from __future__ import annotations

from common.llm._credentials import (
    has_credentials,
    resolve_gemini_config,
    resolve_openai_compatible,
)

__all__ = [
    "has_credentials",
    "resolve_gemini_config",
    "resolve_openai_compatible",
]
