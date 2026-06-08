"""Embedding retry with graceful degradation tests.

Unit tests validate:
  - Embedding retry constants are sensible
  - Retry exhaustion returns None instead of raising
  - Success on second attempt returns valid embedding
  - Fake provider never returns None (no retry needed)
"""

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
    import logging

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
    import logging

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
