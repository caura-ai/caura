"""Embedding retry with graceful degradation tests.

Unit tests validate:
  - Embedding retry constants are sensible
  - Retry exhaustion returns None instead of raising
  - Success on second attempt returns valid embedding
  - Fake provider never returns None (no retry needed)
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from core_api.constants import (
    EMBEDDING_REEMBED_BATCH_SIZE,
    EMBEDDING_REEMBED_DELAY_S,
    EMBEDDING_RETRY_ATTEMPTS,
    EMBEDDING_RETRY_DELAY_S,
    VECTOR_DIM,
)


# ---------------------------------------------------------------------------
# Unit tests: constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmbeddingRetryConstants:
    """Verify embedding retry constants are sensible."""

    def test_retry_attempts_positive(self):
        assert EMBEDDING_RETRY_ATTEMPTS >= 1

    def test_retry_attempts_bounded(self):
        assert EMBEDDING_RETRY_ATTEMPTS <= 5

    def test_retry_delay_positive(self):
        assert EMBEDDING_RETRY_DELAY_S > 0

    def test_retry_delay_bounded(self):
        worst = sum(
            EMBEDDING_RETRY_DELAY_S * (i + 1) for i in range(EMBEDDING_RETRY_ATTEMPTS)
        )
        assert worst <= 30.0

    def test_reembed_delay_positive(self):
        assert EMBEDDING_REEMBED_DELAY_S > 0

    def test_reembed_batch_size_positive(self):
        assert EMBEDDING_REEMBED_BATCH_SIZE >= 1


# ---------------------------------------------------------------------------
# Unit tests: retry exhaustion returns None
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_exhaustion_returns_none():
    """When all retry attempts fail, get_embedding returns None."""
    from common.embedding import get_embedding

    mock_provider = AsyncMock()
    mock_provider.embed = AsyncMock(side_effect=RuntimeError("provider down"))
    with (
        patch(
            "common.embedding._service.get_embedding_provider",
            return_value=mock_provider,
        ),
        patch("common.embedding._service.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await get_embedding("hello world")

    assert result is None
    assert mock_provider.embed.call_count == EMBEDDING_RETRY_ATTEMPTS


# ---------------------------------------------------------------------------
# Unit tests: success on second attempt
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_on_second_attempt():
    """get_embedding returns a valid embedding when the second attempt succeeds."""
    from common.embedding import get_embedding

    fake_vec = [0.1] * VECTOR_DIM
    mock_provider = AsyncMock()
    mock_provider.embed = AsyncMock(side_effect=[RuntimeError("transient"), fake_vec])
    with (
        patch(
            "common.embedding._service.get_embedding_provider",
            return_value=mock_provider,
        ),
        patch("common.embedding._service.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await get_embedding("hello world")

    assert result == fake_vec
    assert mock_provider.embed.call_count == 2


# ---------------------------------------------------------------------------
# Unit tests: fake provider never returns None
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_provider_never_returns_none():
    """The fake embedding provider always succeeds (deterministic hash)."""
    from common.embedding import get_embedding

    # CAURA-594 extraction: common/embedding/_service reads EMBEDDING_PROVIDER
    # from os.environ when no tenant_config is passed. Force "fake" via the
    # env var rather than patching a now-nonexistent module attribute.
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("EMBEDDING_PROVIDER", "fake")
    try:
        result = await get_embedding("any text")
    finally:
        monkeypatch_env.undo()

    assert result is not None
    assert isinstance(result, list)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# Provider-misconfiguration degradation: ValueError from registry → None
# ---------------------------------------------------------------------------


def _reset_misconfig_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe the module-level ``_misconfiguration_logged`` set so a test
    sees a fresh "first-failure" path. Module-scoped state — without
    this, test ordering would mask the once-per-provider dedup logic."""
    import common.embedding._service as service_mod

    monkeypatch.setattr(service_mod, "_misconfiguration_logged", set())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_embedding_returns_none_on_registry_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the registry raises ``ValueError`` at provider construction
    (env-var misconfig), ``get_embedding`` must map it to the documented
    ``None`` degradation contract — write paths persist
    ``embedding=NULL`` for backfill instead of crashing the request
    handler. Logged at ERROR (once per provider) so the misconfig is
    still visible."""
    from common.embedding import get_embedding

    _reset_misconfig_dedup(monkeypatch)

    def _explode(*_a, **_k):
        raise ValueError(
            "OPENAI_EMBEDDING_BASE_URL=... is set but SEND_DIMENSIONS is true"
        )

    monkeypatch.setattr("common.embedding._service.get_embedding_provider", _explode)
    result = await get_embedding("anything")
    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_query_embedding_returns_none_on_registry_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the above for the query/search path. Search routes
    typically translate ``None`` → 503; raising would crash the worker
    instead."""
    from common.embedding import get_query_embedding

    _reset_misconfig_dedup(monkeypatch)

    def _explode(*_a, **_k):
        raise ValueError("invalid OPENAI_EMBEDDING_TRUNCATE_TO_DIM='zzz'")

    monkeypatch.setattr("common.embedding._service.get_embedding_provider", _explode)
    result = await get_query_embedding("anything")
    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_value_error_in_provider_construction_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The degradation guard logs at ERROR level on the first failure
    so operators see the misconfiguration in their logs — silent
    return-None would obscure the cause."""

    from common.embedding import get_embedding

    _reset_misconfig_dedup(monkeypatch)

    def _explode(*_a, **_k):
        raise ValueError("operator forgot OPENAI_EMBEDDING_SEND_DIMENSIONS=false")

    monkeypatch.setattr("common.embedding._service.get_embedding_provider", _explode)

    with caplog.at_level(logging.ERROR, logger="common.embedding._service"):
        await get_embedding("anything")

    assert any(
        "misconfiguration" in rec.getMessage() for rec in caplog.records
    ), "expected an ERROR log naming the misconfig"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_misconfiguration_error_logged_only_once_per_provider(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_resolve_provider_or_degrade`` runs on every embed/query
    request. Logging the ERROR unconditionally would spam the log at
    request rate. The module-level ``_misconfiguration_logged`` set
    gates the ERROR to one emit per resolved provider name per process
    — failure stats still increment on every call so the degraded
    trip-wire keeps working, but the log stays readable."""

    from common.embedding import get_embedding

    _reset_misconfig_dedup(monkeypatch)

    def _explode(*_a, **_k):
        raise ValueError("misconfig that would otherwise log every request")

    monkeypatch.setattr("common.embedding._service.get_embedding_provider", _explode)

    with caplog.at_level(logging.ERROR, logger="common.embedding._service"):
        # Five back-to-back calls simulate five incoming requests.
        for _ in range(5):
            assert await get_embedding("x") is None

    matches = [
        rec for rec in caplog.records if "misconfiguration" in rec.getMessage()
    ]
    assert len(matches) == 1, (
        f"expected exactly 1 ERROR across 5 calls; got {len(matches)} "
        f"({[r.getMessage() for r in matches]!r})"
    )
    # Sanity: the message tells the operator we'll be quiet from here on.
    assert "will not repeat" in matches[0].getMessage()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_stats_still_increment_under_misconfig_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The log dedup must NOT short-circuit ``_stats.record_failure()``
    — every misconfigured call still counts as a failure so the
    degraded-provider tripwire (3 consecutive failures fires the
    "service degraded" ERROR) keeps working."""
    import common.embedding._service as service_mod
    from common.embedding import get_embedding

    _reset_misconfig_dedup(monkeypatch)
    # Reset failure stats so this test owns the count.
    monkeypatch.setattr(service_mod, "_stats", service_mod._EmbeddingStats())

    def _explode(*_a, **_k):
        raise ValueError("dedupable misconfig")

    monkeypatch.setattr("common.embedding._service.get_embedding_provider", _explode)

    for _ in range(4):
        assert await get_embedding("x") is None

    # All four calls bumped failure stats even though only the first
    # one logged.
    assert service_mod._stats.failures == 4
    assert service_mod._stats.consecutive_failures == 4


def _bulk_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``get_embeddings_batch`` at a provider whose bulk call always
    fails and whose single-embed call always succeeds — the shape of a
    provider-side batch-size cap that the per-item fallback rides out.

    Subclasses the real fake so the stand-in still satisfies the
    ``EmbeddingProvider`` protocol (``provider_name`` / ``model``) rather
    than being a shape that could not exist in production.
    """
    import common.embedding._service as service_mod
    from common.embedding.providers.fake import FakeEmbeddingProvider

    class _CappedProvider(FakeEmbeddingProvider):
        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError(
                f"batch size {len(texts)} > maximum allowed batch size 32"
            )

    monkeypatch.setattr(service_mod, "_stats", service_mod._EmbeddingStats())
    monkeypatch.setattr(
        "common.embedding._service.get_embedding_provider",
        lambda *_a, **_k: _CappedProvider(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_failure_reports_even_while_single_embeds_succeed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bulk-only outage must still report while other paths succeed.

    This is the regression test for the bug that hid for 30+ days: prod
    TEI rejected 100% of bulk embeds on a batch-size cap while single
    embeds kept working, and ``consecutive_failures`` — shared by every
    call path through the process-wide ``_stats`` — was reset by each
    successful query embed, so the degraded-provider ERROR never fired.

    Interleaving a success after every failure is the whole point: with
    only ``consecutive_failures`` to go on, the streak never reaches the
    reporting threshold and NOTHING is logged here.
    """
    import common.embedding._service as service_mod
    from common.embedding import get_embedding, get_embeddings_batch

    _bulk_provider(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="common.embedding._service"):
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await get_embeddings_batch(["a"] * 50)
            # A healthy single embed between every bulk failure — exactly
            # what search traffic does to the shared streak.
            assert await get_embedding("q") is not None

    assert service_mod._stats.consecutive_failures == 0, (
        "precondition: the interleaved successes must clear the shared "
        "streak, otherwise this test is not exercising the masking"
    )

    matches = [
        rec
        for rec in caplog.records
        if "Bulk embedding failing" in rec.getMessage()
    ]
    assert len(matches) == 1, (
        f"expected exactly one bulk-failure report, got {len(matches)}"
    )
    # The batch size is the datum that names this class of bug outright.
    assert "batch=50" in matches[0].getMessage()
    assert matches[0].levelno == logging.WARNING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_failure_report_is_rate_limited_not_per_call(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log volume must not track the traffic curve.

    A batch-size cap fails on EVERY bulk write, so an unconditional
    warning would put log volume on request rate. Reporting at 3 and then
    every 10th (13, 23, …) keeps a sustained fault bounded.
    """
    from common.embedding import get_embeddings_batch

    _bulk_provider(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="common.embedding._service"):
        for _ in range(13):
            with pytest.raises(RuntimeError):
                await get_embeddings_batch(["a"] * 50)

    matches = [
        rec
        for rec in caplog.records
        if "Bulk embedding failing" in rec.getMessage()
    ]
    # 13 consecutive failures, reports at streak 3 and streak 13.
    assert len(matches) == 2, (
        f"expected reports at streak 3 and 13, got {len(matches)}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_failure_counted_when_caller_deadline_cancels_us(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller's timeout must still count as a bulk failure.

    Every caller wraps ``get_embeddings_batch`` in its own deadline
    (``BULK_STRONG_EMBED_TIMEOUT_SECONDS`` is 8s) and enforces it by
    CANCELLING us, so what arrives is ``CancelledError`` — a
    ``BaseException``, which ``except Exception`` does not catch. That
    silently skipped the streak for the slow-provider outage the streak
    exists to name: one provider request may burn
    ``OPENAI_REQUEST_TIMEOUT_SECONDS`` (25s), already past the 8s budget.
    """
    import asyncio

    import common.embedding._service as service_mod
    from common.embedding import get_embeddings_batch
    from common.embedding.providers.fake import FakeEmbeddingProvider

    class _HangingProvider(FakeEmbeddingProvider):
        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    monkeypatch.setattr(service_mod, "_stats", service_mod._EmbeddingStats())
    monkeypatch.setattr(
        "common.embedding._service.get_embedding_provider",
        lambda *_a, **_k: _HangingProvider(),
    )

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await get_embeddings_batch(["a"] * 50)

    assert service_mod._stats.consecutive_bulk_failures == 1, (
        "a deadline-cancelled bulk call must advance the bulk streak"
    )
    assert service_mod._stats.failures == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_success_rearms_the_bulk_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bulk success clears the bulk streak, so a later regression
    re-reports from 3 instead of staying silent forever."""
    import common.embedding._service as service_mod
    from common.embedding import get_embeddings_batch

    _bulk_provider(monkeypatch)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await get_embeddings_batch(["a"] * 50)
    # Provider recovers.
    class _HealthyProvider:
        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] for _ in texts]

    monkeypatch.setattr(
        "common.embedding._service.get_embedding_provider",
        lambda *_a, **_k: _HealthyProvider(),
    )
    assert len(await get_embeddings_batch(["a", "b"])) == 2
    assert service_mod._stats.consecutive_bulk_failures == 0
