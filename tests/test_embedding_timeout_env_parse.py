"""oss-0902-l-11 — ``OPENAI_REQUEST_TIMEOUT_SECONDS`` parsing and freshness.

Two defects in ``common/embedding/constants.py``, both on the one env var
the credential bridge also carries:

1. The value was parsed with bare ``float(os.environ.get(...))`` at module
   import. ``OPENAI_REQUEST_TIMEOUT_SECONDS=25s`` crashed the importing
   process at import time with ``ValueError: could not convert string to
   float: '25s'`` — a traceback naming neither the env var nor the fix,
   before structured logging exists to report it. core-worker takes the
   naked crash (no pydantic-settings layer validates the var first there);
   every sibling knob in the module already used ``read_float_env``, whose
   docstring names exactly this failure mode.

2. The parse ran once at import and the provider froze the result — but
   core-api's ``bridge_credentials_to_environ()`` (CAURA-595) writes the
   ``.env``-loaded value into ``os.environ`` during lifespan startup,
   after ``core_api.app``'s module-level imports have already pulled in
   ``common.embedding.constants`` (via ``core_api.constants`` and the
   route modules). A ``.env``-configured timeout therefore reached the
   LLM client — whose registry re-reads the env at construction, with a
   comment naming the bridge — but was silently ignored by the embedding
   client, despite the constant's own comment promising "a single tunable
   controls both the LLM and the embedding paths".
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import common.embedding.constants as embedding_constants
from common.embedding.providers.openai import OpenAIEmbeddingProvider

pytestmark = pytest.mark.unit


def _exec_constants_fresh() -> ModuleType:
    """Execute ``common/embedding/constants.py`` as a NEW module object.

    Import-time behaviour can only be exercised by running the module body
    again. A fresh, unregistered module — not ``importlib.reload`` — leaves
    the real module, and every ``from``-import snapshot other modules hold,
    untouched.
    """
    path = Path(embedding_constants.__file__)
    spec = importlib.util.spec_from_file_location("_fresh_embedding_constants", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestImportTimeParse:
    """Sub-claim (a): a bad env value must not crash the import."""

    @pytest.mark.parametrize("bad", ["25s", "", "twenty-five"])
    def test_garbage_value_falls_back_and_names_the_var(self, bad, capsys):
        """Pre-fix this raised ``ValueError`` out of the module body."""
        with patch.dict(os.environ, {"OPENAI_REQUEST_TIMEOUT_SECONDS": bad}):
            mod = _exec_constants_fresh()
        assert mod.OPENAI_REQUEST_TIMEOUT_SECONDS == 25.0
        # The operator-facing contract: the warning names the knob.
        assert "OPENAI_REQUEST_TIMEOUT_SECONDS" in capsys.readouterr().err

    def test_valid_value_is_parsed(self):
        with patch.dict(os.environ, {"OPENAI_REQUEST_TIMEOUT_SECONDS": "7.5"}):
            mod = _exec_constants_fresh()
        assert mod.OPENAI_REQUEST_TIMEOUT_SECONDS == 7.5

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("OPENAI_REQUEST_TIMEOUT_SECONDS", raising=False)
        mod = _exec_constants_fresh()
        assert mod.OPENAI_REQUEST_TIMEOUT_SECONDS == 25.0


def _construct_with_mocked_sdk() -> MagicMock:
    """Build a provider over a mocked ``openai.AsyncOpenAI``; return the ctor
    mock so tests can assert against the ``timeout=`` kwarg it received."""
    fake_client = MagicMock()
    fake_client.embeddings.create = AsyncMock()
    ctor = MagicMock(return_value=fake_client)
    with patch("common.embedding.providers.openai.openai.AsyncOpenAI", ctor):
        OpenAIEmbeddingProvider(api_key="sk-fake")
    return ctor


class TestConstructionTimeRead:
    """Sub-claim (b): the client must honour the env var as it stands at
    provider construction, not as it stood at module import."""

    def test_env_set_after_import_reaches_the_client(self, monkeypatch):
        """The credential-bridge scenario. ``bridge_credentials_to_environ()``
        runs in core-api's lifespan startup — after import froze the module
        constant — and the provider is constructed later still, lazily on
        the first embed call. Setting the env var here, after import, is
        exactly that ordering; pre-fix the client kept the frozen value."""
        monkeypatch.setenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "3.5")
        timeout = _construct_with_mocked_sdk().call_args.kwargs["timeout"]
        assert timeout.read == 3.5
        assert timeout.write == 3.5
        # Pool tracks the request budget when not explicitly decoupled.
        assert timeout.pool == 3.5

    def test_env_unset_falls_back_to_module_binding(self, monkeypatch):
        """The documented test idiom — patch the module-level binding — must
        keep working, and the all-defaults case must behave exactly as it
        did before the construction-time read existed."""
        monkeypatch.delenv("OPENAI_REQUEST_TIMEOUT_SECONDS", raising=False)
        monkeypatch.setattr(
            "common.embedding.providers.openai.OPENAI_REQUEST_TIMEOUT_SECONDS", 9.0
        )
        timeout = _construct_with_mocked_sdk().call_args.kwargs["timeout"]
        assert timeout.read == 9.0

    def test_garbage_env_at_construction_falls_back_with_warning(
        self, monkeypatch, capsys
    ):
        """A bad value arriving via the bridge must not take the first embed
        call down — same fallback-and-warn contract as at import."""
        monkeypatch.setenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "fast")
        monkeypatch.setattr(
            "common.embedding.providers.openai.OPENAI_REQUEST_TIMEOUT_SECONDS", 11.0
        )
        timeout = _construct_with_mocked_sdk().call_args.kwargs["timeout"]
        assert timeout.read == 11.0
        assert "OPENAI_REQUEST_TIMEOUT_SECONDS" in capsys.readouterr().err
