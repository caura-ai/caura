"""The registry must reuse ``LocalEmbedding`` instances across calls.

The loaded ``SentenceTransformer`` is cached on the instance
(``self._model``), and ``get_embedding_provider`` runs on every
embed/search request. A fresh instance per call therefore reloaded the
model from disk on every request — seconds of latency per embed, and one
extra copy of the weights in RAM per in-flight request. Follow-up to C38,
which made the model configurable but kept per-call construction.

These tests never touch sentence-transformers: ``LocalEmbedding.__init__``
is lazy (the import and load happen in ``_ensure_model``), which is also
why identity — not equality — is the thing to assert.
"""

import os
from unittest.mock import patch

import pytest

from common.embedding import _registry

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_cache():
    """Module-level cache — keep entries from leaking between tests."""
    with patch.dict(_registry._local_provider_cache, clear=True):
        yield


def test_same_model_returns_the_same_instance():
    first = _registry.get_embedding_provider("local")
    second = _registry.get_embedding_provider("local")
    assert first is second


def test_env_override_is_cached_too():
    with patch.dict(os.environ, {"LOCAL_EMBEDDING_MODEL": "custom/model-x"}):
        first = _registry.get_embedding_provider("local")
        second = _registry.get_embedding_provider("local")
    assert first is second
    assert first.model == "custom/model-x"


def test_distinct_models_get_distinct_instances():
    """Keyed by model name, not a process-wide singleton: a cached default
    must not be handed to a caller whose env now names another model."""
    default = _registry.get_embedding_provider("local")
    with patch.dict(os.environ, {"LOCAL_EMBEDDING_MODEL": "custom/model-x"}):
        other = _registry.get_embedding_provider("local")
    assert default is not other
    assert default.model == _registry.DEFAULT_LOCAL_EMBEDDING_MODEL
    assert other.model == "custom/model-x"
