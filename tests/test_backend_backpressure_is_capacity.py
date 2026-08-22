"""A backend at capacity is capacity, and one embed is one request.

Two faults, one root: nothing in the stack could tell "the shared backend
is full" apart from "the backend is broken".

  1. A 429 was retried. It is the ONE signal a single process can have
     about aggregate demand — the concurrency cap is per process, so
     ``cap x instances`` is what reaches the backend and no instance can
     observe that number, but a 429 can only fire once the other
     instances have taken the capacity. Retrying it answers "you are
     full" with more load.
  2. It was retried more times than anyone wrote down. The OpenAI SDK
     applies ``DEFAULT_MAX_RETRIES = 2`` beneath a service layer that
     retries ``EMBEDDING_RETRY_ATTEMPTS`` times, so one logical embed
     could reach the backend six times — invisibly, because SDK retries
     happen below the gate: no slot, no log line, one call in every stat.

Both were latent rather than firing: TEI served 345,802 requests in the 7
days to 2026-08-21 with zero 429s and zero 5xx. The tests below are the
line that keeps them latent, and that keeps the 429 usable as the
aggregate-demand signal an aggregate cap would have to be built on.

The narrowness is deliberate and is tested in both directions: 429 is
capacity, 503 is a fault. A 503 is equally what a real outage looks like,
so classifying it as capacity would silence the degraded-provider ERROR
during one.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from common.embedding import _service as svc
from common.embedding.constants import EMBEDDING_PROVIDER_MAX_RETRIES
from common.embedding.providers.openai import OpenAIEmbeddingProvider


def _status_error(code: int) -> Exception:
    """A real SDK error for *code*, not a stand-in.

    Constructed from the actual ``openai`` types so the duck-typed
    classifier is checked against the shape it will meet in production
    rather than against a local mock that happens to agree with it.
    """
    response = httpx.Response(
        code, request=httpx.Request("POST", "http://tei/v1/embeddings")
    )
    cls = openai.RateLimitError if code == 429 else openai.APIStatusError
    return cls("backend said no", response=response, body=None)


@pytest.fixture
def provider(monkeypatch):
    """A provider whose every call raises, with the call count visible."""
    prov = MagicMock()
    prov.provider_name = "spy"
    prov.model = "spy-model"
    prov.embed = AsyncMock()
    prov.embed_batch = AsyncMock()

    async def _resolve(_tenant_config, _context):
        return prov

    monkeypatch.setattr(svc, "_resolve_provider_or_degrade", _resolve)
    monkeypatch.setattr(svc, "get_embedding_provider", lambda *_a, **_k: prov)
    # The retry path sleeps ``delay x attempt`` between attempts; the
    # control tests below deliberately exhaust the budget.
    monkeypatch.setattr(svc, "EMBEDDING_RETRY_DELAY_S", 0.0)
    return prov


@pytest.fixture
def stats(monkeypatch):
    """Intercept the shared stats object the degraded-provider ERROR reads."""
    s = svc._EmbeddingStats(label="spy")
    monkeypatch.setattr(svc, "_stats_for", lambda *_a, **_k: s)
    return s


@pytest.mark.unit
class TestBusyBackendIsNotRetried:
    @pytest.mark.asyncio
    async def test_429_costs_exactly_one_request(self, provider, stats):
        """The whole point: one refusal, one round trip.

        Before this change the same call reached the backend twice here,
        and up to six times in a deployed process — the SDK's own two
        retries multiply each of these attempts.
        """
        provider.embed.side_effect = _status_error(429)

        assert await svc.get_embedding("hello", background=True) is None

        assert provider.embed.await_count == 1

    @pytest.mark.asyncio
    async def test_the_interactive_path_also_stops_at_one(self, provider, stats):
        """The query path is the one with nothing behind it.

        A write's ``None`` persists as ``embedding=NULL`` for the backfill
        and a batch's fans out to EMBED_REQUESTED, so both recover on
        their own. A query embed has no queue at all — the search just
        fails with a 503. It still must not retry into a full backend, and
        the shared log line must not tell an operator the work was
        deferred when for this caller nothing is.
        """
        provider.embed.side_effect = _status_error(429)

        assert await svc.get_query_embedding("hello") is None

        assert provider.embed.await_count == 1
        assert stats.failures == 0

    @pytest.mark.asyncio
    async def test_429_does_not_blame_the_backend(self, provider, stats):
        """A backend shedding load correctly must not read as degraded.

        The streak drives "Embedding service degraded [<backend>]", which
        points an operator at the service. During a 429 that service is
        doing exactly what it should.
        """
        provider.embed.side_effect = _status_error(429)

        for _ in range(5):
            assert await svc.get_embedding("hello", background=True) is None

        assert stats.consecutive_failures == 0
        assert stats.failures == 0

    @pytest.mark.asyncio
    async def test_an_earlier_real_failure_still_counts(self, provider, stats):
        """A genuine fault before the 429 must survive it.

        A backend both erroring and full is what an outage looks like,
        and returning early must not discard the evidence — the same
        mixed case ``EmbeddingGateTimeout`` handles.

        Passes with or without the new arm, deliberately: before it, the
        429 was itself counted as the failure, so the number came out
        right for the wrong reason. It is here to pin that the early
        return did not silently drop it — the exact regression the same
        fix introduced when it was made one layer up.
        """
        provider.embed.side_effect = [_status_error(500), _status_error(429)]

        assert await svc.get_embedding("hello", background=True) is None

        assert provider.embed.await_count == 2
        assert stats.failures == 1


@pytest.mark.unit
class TestOnlyA429CountsAsCapacity:
    @pytest.mark.asyncio
    async def test_500_is_still_retried_and_counted(self, provider, stats):
        """The control. Without it, "not retried" could just mean broken."""
        provider.embed.side_effect = _status_error(500)

        assert await svc.get_embedding("hello", background=True) is None

        assert provider.embed.await_count == svc.EMBEDDING_RETRY_ATTEMPTS
        assert stats.failures == 1

    @pytest.mark.asyncio
    async def test_503_is_a_fault_not_capacity(self, provider, stats):
        """A 503 is ambiguous, so it keeps the loud treatment.

        Cloud Run returns 429 for "no instance available" and TEI returns
        it when its queue is full — both unambiguously "later". A 503 is
        equally a real outage, and misreading one as capacity would
        suppress the signal that names it.
        """
        provider.embed.side_effect = _status_error(503)

        assert await svc.get_embedding("hello", background=True) is None

        assert provider.embed.await_count == svc.EMBEDDING_RETRY_ATTEMPTS
        assert stats.failures == 1

    def test_classifier_reads_both_exception_shapes(self):
        """``status_code`` sits in two places across the two libraries.

        ``openai.APIStatusError`` exposes it directly; ``httpx`` puts it
        on ``.response``. The classifier is duck-typed to avoid importing
        a provider SDK into the service layer, so both are checked here
        rather than assumed.
        """
        request = httpx.Request("POST", "http://tei/v1/embeddings")
        assert svc._is_backend_busy(_status_error(429))
        assert svc._is_backend_busy(
            httpx.HTTPStatusError(
                "busy", request=request, response=httpx.Response(429, request=request)
            )
        )
        assert not svc._is_backend_busy(_status_error(503))
        assert not svc._is_backend_busy(ValueError("nothing to do with HTTP"))


@pytest.mark.unit
class TestBatchPath:
    @pytest.mark.asyncio
    async def test_429_raises_without_advancing_the_bulk_streak(self, provider, stats):
        """Raising is what routes the batch to its durable per-item path.

        ``_reembed_batch_via_provider`` fans a failed batch out through
        the inline/deferred router, so in deferred mode a refused batch
        becomes N EMBED_REQUESTED messages that drain at core-worker's
        sequential rate. That IS the demand smoothing — so the exception
        must reach the caller, while still not counting against the
        bulk-failure streak.
        """
        provider.embed_batch.side_effect = _status_error(429)

        with pytest.raises(svc.EmbeddingBackendBusy):
            await svc.get_embeddings_batch(["a", "b"], background=True)

        assert provider.embed_batch.await_count == 1
        assert stats.failures == 0
        assert stats.consecutive_bulk_failures == 0


@pytest.mark.unit
class TestSdkRetriesArePinned:
    @pytest.mark.asyncio
    async def test_client_does_not_retry_underneath_us(self):
        """Guard the multiplier, not just today's behaviour.

        This is a library default, so it comes back on its own the moment
        the constructor stops passing the value — an upgrade, a refactor,
        a copied kwargs dict. Nothing else in the stack can see it: SDK
        retries take no gate slot and emit no log.
        """
        provider = OpenAIEmbeddingProvider(api_key="test-key")
        try:
            assert provider._client.max_retries == EMBEDDING_PROVIDER_MAX_RETRIES
            assert provider._client.max_retries == 0, (
                "one logical embed must be one request; the SDK's default 2 puts "
                "a silent 3x under every service-layer retry"
            )
        finally:
            await provider.aclose()
