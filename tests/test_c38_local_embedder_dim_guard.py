"""C38 — EMBEDDING_PROVIDER=local was dead on arrival.

``_registry`` constructed ``LocalEmbedding()`` with no arguments, hard-coding
``BAAI/bge-base-en-v1.5`` (768-dim) while ``memories.embedding`` is
``vector(VECTOR_DIM)`` = 1024. No configuration could change it, so that
provider could never have worked against this schema.

Worse, the width mismatch only logged a warning and carried on — the failure
surfaced much later as a Postgres dimension error at INSERT, naming neither the
model nor the setting responsible.

Found while looking for an embedder fallback after the OpenAI account ran out of
credits. NOT the path staging/prod use (those run TEI/bge-m3 through the
OpenAI-compatible client), so this was a dormant provider, not a live outage.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from common.constants import VECTOR_DIM
from common.embedding import _registry
from common.embedding.providers.local import LocalEmbedding

pytestmark = pytest.mark.unit


async def _load(provider, dim):
    model = MagicMock()
    model.get_sentence_embedding_dimension.return_value = dim
    st = MagicMock(return_value=model)
    with patch.dict(
        "sys.modules", {"sentence_transformers": MagicMock(SentenceTransformer=st)}
    ):
        await provider._ensure_model()


def test_default_model_matches_the_schema_width():
    """The regression guard that matters: the default must be storable. A
    768-dim default against a 1024-dim column is exactly what shipped."""
    assert _registry.DEFAULT_LOCAL_EMBEDDING_MODEL == "BAAI/bge-large-en-v1.5"


async def test_mismatched_model_refuses_at_load_and_names_the_fix():
    p = LocalEmbedding("some/768-dim-model")
    with pytest.raises(ValueError) as e:
        await _load(p, 768)
    msg = str(e.value)
    assert "768" in msg and str(VECTOR_DIM) in msg
    assert "LOCAL_EMBEDDING_MODEL" in msg  # names the knob, not just the symptom
    assert "bge-large" in msg  # and a model that actually works


async def test_matching_model_loads():
    p = LocalEmbedding("BAAI/bge-large-en-v1.5")
    await _load(p, VECTOR_DIM)
    assert p._model is not None


def test_registry_honours_the_env_override():
    with patch.dict(os.environ, {"LOCAL_EMBEDDING_MODEL": "custom/model-x"}):
        assert _registry.get_embedding_provider("local").model == "custom/model-x"


def test_empty_env_value_falls_back_rather_than_loading_a_nameless_model():
    """``os.environ.get`` returning "" would otherwise load a model named ''."""
    with patch.dict(os.environ, {"LOCAL_EMBEDDING_MODEL": ""}):
        assert (
            _registry.get_embedding_provider("local").model
            == _registry.DEFAULT_LOCAL_EMBEDDING_MODEL
        )


def test_core_api_setting_and_registry_default_agree():
    """pydantic-settings maps the field to LOCAL_EMBEDDING_MODEL; if the two
    defaults drift, core-api documents one model and loads another."""
    from core_api.config import Settings

    assert Settings().local_embedding_model == _registry.DEFAULT_LOCAL_EMBEDDING_MODEL
