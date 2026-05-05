"""CAURA-651: every provider that does ``json.loads`` on an LLM
response must raise a typed error when the parsed value is not a
``dict``. Without this guard, the downstream consumer in
``common.enrichment.service`` calls ``raw.get(...)`` and surfaces a
bare ``AttributeError``, which ``call_with_fallback`` then silently
downgrades to ``FakeLLMProvider`` keyword-fragment metadata.

Tests target the parse-and-shape-check logic in isolation rather than
spinning up the full SDK path; the bug class lives purely in the
post-response branch and the SDK init wants live cloud creds.
"""

from __future__ import annotations

import json

import pytest

from common.enrichment.service import _validate_enrichment
from common.llm.providers._shape_error import ProviderResponseShapeError
from common.llm.providers.gemini import GeminiResponseShapeError
from common.llm.providers.openai import OpenAIResponseShapeError
from common.llm.providers.vertex import VertexResponseShapeError


def _shape_check(error_cls: type, response_text: str) -> dict:
    parsed = json.loads(response_text)
    if not isinstance(parsed, dict):
        raise error_cls(response_text, type(parsed).__name__)
    return parsed


@pytest.mark.unit
@pytest.mark.parametrize(
    "error_cls,provider_name",
    [
        (VertexResponseShapeError, "Vertex"),
        (GeminiResponseShapeError, "Gemini"),
        (OpenAIResponseShapeError, "OpenAI"),
    ],
)
class TestProviderResponseShape:
    def test_dict_passes_through(self, error_cls, provider_name):
        out = _shape_check(error_cls, '{"memory_type": "fact"}')
        assert out == {"memory_type": "fact"}

    def test_list_raises(self, error_cls, provider_name):
        with pytest.raises(error_cls, match="list"):
            _shape_check(error_cls, '["a", "b"]')

    def test_string_raises(self, error_cls, provider_name):
        with pytest.raises(error_cls, match="str"):
            _shape_check(error_cls, '"plain string"')

    def test_message_includes_provider_name(self, error_cls, provider_name):
        with pytest.raises(error_cls) as exc:
            _shape_check(error_cls, "[1, 2]")
        assert provider_name in str(exc.value)

    def test_attributes_exposed_for_monitoring(self, error_cls, provider_name):
        """Per json.JSONDecodeError convention: structured fields on the
        exception, not just embedded in the message."""
        with pytest.raises(error_cls) as exc:
            _shape_check(error_cls, "[1, 2, 3]")
        assert exc.value.parsed_type == "list"
        assert exc.value.content == "[1, 2, 3]"
        # ``provider`` is what monitoring / fallback code reads to tag
        # metrics or route alerts without scraping the message string.
        assert exc.value.provider == provider_name

    def test_content_truncated_to_1kib(self, error_cls, provider_name):
        long = '"' + "x" * 5000 + '"'
        with pytest.raises(error_cls) as exc:
            _shape_check(error_cls, long)
        assert len(exc.value.content) <= 1024
        # Truncation must also apply to ``self.args`` — not just to
        # the display attribute — so a megabyte-scale aberrant
        # response can't be retained for the exception's lifetime nor
        # serialised across pytest-xdist / multiprocessing pickle
        # boundaries in full.
        assert len(exc.value.args[1]) <= 1024

    def test_subclasses_value_error(self, error_cls, provider_name):
        """A single ``except ValueError`` should catch all provider
        shape errors, matching the ``_validate_enrichment`` fallback."""
        assert issubclass(error_cls, ValueError)

    def test_subclasses_provider_response_shape_error(self, error_cls, provider_name):
        """Monitoring code should catch all three with one
        ``except ProviderResponseShapeError`` clause."""
        assert issubclass(error_cls, ProviderResponseShapeError)

    def test_pickle_preserves_truncated_label(self, error_cls, provider_name):
        """A ``_was_truncated`` flag would not survive pickle (the
        receiving worker re-runs ``__init__`` on the already-clipped
        1 KiB slice, recomputing the flag as False). Detection from
        ``len(self.content)`` survives — verify the round-trip still
        renders ``(truncated)``."""
        import pickle

        long = '"' + "x" * 5000 + '"'
        with pytest.raises(error_cls) as exc:
            _shape_check(error_cls, long)
        assert "(truncated)" in str(exc.value)
        restored = pickle.loads(pickle.dumps(exc.value))
        assert "(truncated)" in str(restored)

    def test_pickle_round_trip(self, error_cls, provider_name):
        """pytest-xdist + any multiprocessing pool serialise exception
        results across process boundaries. Without the ``__reduce__``
        overrides, default reconstruction calls ``cls(*self.args)`` —
        wrong arity for both the 3-arg base and the 2-arg subclasses
        — and the test/worker run crashes with TypeError instead of a
        clean failure report."""
        import pickle

        with pytest.raises(error_cls) as exc:
            _shape_check(error_cls, '["x"]')
        restored = pickle.loads(pickle.dumps(exc.value))
        assert isinstance(restored, error_cls)
        assert restored.provider == provider_name
        assert restored.parsed_type == "list"
        assert restored.content == '["x"]'

    def test_label_says_truncated_only_when_actually_truncated(
        self, error_cls, provider_name
    ):
        # Short content (well under 1 KiB cap) — label must NOT say
        # truncated.
        with pytest.raises(error_cls) as short_exc:
            _shape_check(error_cls, '"short"')
        assert "(truncated)" not in str(short_exc.value)
        # Long content (over the 1 KiB cap) — label must say truncated.
        long = '"' + "x" * 5000 + '"'
        with pytest.raises(error_cls) as long_exc:
            _shape_check(error_cls, long)
        assert "(truncated)" in str(long_exc.value)


@pytest.mark.unit
def test_shape_error_publicly_re_exported():
    """``ProviderResponseShapeError`` lives in a private impl module
    but should be importable from the package root so monitoring code
    doesn't have to couple to the internal path."""
    from common.llm.providers import ProviderResponseShapeError as Public
    from common.llm.providers._shape_error import ProviderResponseShapeError as Private

    assert Public is Private


@pytest.mark.unit
class TestEnrichmentValidatorRejectsNonDict:
    def test_list_raises_value_error(self):
        with pytest.raises(ValueError, match="enrichment LLM returned a JSON list"):
            _validate_enrichment(["a", "b"], llm_ms=10)  # type: ignore[arg-type]

    def test_string_raises_value_error(self):
        with pytest.raises(ValueError, match="enrichment LLM returned a JSON str"):
            _validate_enrichment("not a dict", llm_ms=10)  # type: ignore[arg-type]

    def test_none_raises_value_error(self):
        with pytest.raises(ValueError, match="NoneType"):
            _validate_enrichment(None, llm_ms=10)  # type: ignore[arg-type]
