"""Blank text must never reach an embedding backend.

Regression cover for 2026-08-18 17:00-18:59Z. ``prod-memclaw-tei`` returned
274 x ``413 Input validation error: `inputs` cannot be empty`` in one week,
272 of them inside that window, because nothing checked the text before
sending it. Three separate faults compounded:

  1. The request was sent at all — a blank string cannot be embedded by any
     backend, so the round trip was always wasted.
  2. It was RETRIED. The error is deterministic, so the second attempt could
     only fail the same way; 272 requests for ~136 logical calls.
  3. The resulting ``None`` was reported to users as "Embedding service
     unavailable" (138 times), sending operators after a backend that was
     answering in ~7 ms.

The guard returns ``None``, which is the contract callers already handle —
what changes is that it costs no backend request, no retry, and the log and
the user-facing error both name the real cause.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from common.embedding import _service as svc


@pytest.fixture
def spy_provider(monkeypatch):
    """A provider that records whether it was ever asked to embed anything."""
    provider = MagicMock()
    provider.provider_name = "spy"
    provider.model = "spy-model"
    provider.embed = AsyncMock(return_value=[0.1] * 8)
    provider.embed_query = AsyncMock(return_value=[0.2] * 8)

    async def _resolve(_tenant_config, _context):
        return provider

    monkeypatch.setattr(svc, "_resolve_provider_or_degrade", _resolve)
    return provider


# Every value a caller can realistically hand us that has nothing to encode.
BLANKS = ["", " ", "   ", "\t", "\n", " \n\t "]


@pytest.mark.unit
class TestBlankTextIsNotSent:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", BLANKS)
    async def test_get_embedding_sends_nothing(self, blank, spy_provider):
        assert await svc.get_embedding(blank, background=False) is None
        spy_provider.embed.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", BLANKS)
    async def test_get_query_embedding_sends_nothing(self, blank, spy_provider):
        assert await svc.get_query_embedding(blank) is None
        spy_provider.embed.assert_not_awaited()
        spy_provider.embed_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_real_text_still_embeds(self, spy_provider):
        """The control: the guard must not swallow work that has content."""
        assert await svc.get_embedding("hello world", background=False) == [0.1] * 8
        spy_provider.embed.assert_awaited_once_with("hello world")

    @pytest.mark.asyncio
    async def test_blank_does_not_blame_the_provider(self, spy_provider, monkeypatch):
        """It must not advance the streak behind "Embedding service degraded".

        Same reasoning as a gate timeout: the backend never saw the call, so
        counting it as a provider failure points the operator at the wrong
        component — which is exactly what happened for the 2026-08-18 window.
        """
        stats = svc._EmbeddingStats(label="spy")
        monkeypatch.setattr(svc, "_stats_for", lambda *_a, **_k: stats)

        for _ in range(5):
            assert await svc.get_embedding("", background=True) is None

        assert stats.consecutive_failures == 0
        assert stats.failures == 0

    def test_is_blank_text_agrees_with_the_backend(self):
        """Whitespace is blank too — TEI rejects a lone space identically."""
        for blank in BLANKS:
            assert svc.is_blank_text(blank), f"{blank!r} should count as blank"
        for real in ("a", " a ", "0", "hello world"):
            assert not svc.is_blank_text(real), f"{real!r} should not count as blank"
