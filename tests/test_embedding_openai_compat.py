"""Unit tests for the A.5 + Option B knobs on ``OpenAIEmbeddingProvider``.

These verify the contract added in the local-embedder branch — the
provider now also serves any OpenAI-compatible endpoint (TEI sidecar,
vLLM, etc.) and supports asymmetric query/document encoding for
instruction-aware models.

What's covered (no network, no real OpenAI / TEI calls):

* ``base_url`` is forwarded to the underlying OpenAI SDK.
* ``send_dimensions=False`` omits the ``dimensions`` SDK kwarg
  (TEI rejects it).
* ``send_dimensions=True`` (default) still sends ``dimensions=VECTOR_DIM``
  on the OpenAI hosted path.
* ``truncate_to_dim`` slices and L2-renormalizes the model output.
* ``embed_query`` is identical to ``embed`` when no instruction
  resolves (symmetric models like bge-m3, OpenAI text-embedding-3-small).
* ``embed_query`` prepends the Qwen-style ``Instruct: ...\\nQuery: ...``
  prefix when an instruction resolves (per-call arg or constructor
  default).
"""

from __future__ import annotations

import math
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.constants import VECTOR_DIM
from common.embedding.providers.openai import OpenAIEmbeddingProvider


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _fake_openai_response(vector: list[float]):
    """Shape an OpenAI SDK ``embeddings.create`` response object."""
    return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=vector)])


def _patched_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_vector: list[float] | None = None,
    **provider_kwargs,
) -> tuple[OpenAIEmbeddingProvider, MagicMock, AsyncMock]:
    """Build an ``OpenAIEmbeddingProvider`` whose underlying
    ``openai.AsyncOpenAI`` is fully mocked.

    Returns ``(provider, async_openai_constructor_mock, embeddings_create_mock)``
    so each test can assert against either the constructor kwargs (was
    ``base_url`` forwarded?) or the create-call kwargs (was ``dimensions``
    sent? was the input prefixed?).
    """
    if response_vector is None:
        response_vector = [0.5] * VECTOR_DIM

    embeddings_create = AsyncMock(return_value=_fake_openai_response(response_vector))
    fake_client = MagicMock()
    fake_client.embeddings.create = embeddings_create

    async_openai_ctor = MagicMock(return_value=fake_client)
    monkeypatch.setattr(
        "common.embedding.providers.openai.openai.AsyncOpenAI", async_openai_ctor
    )

    provider = OpenAIEmbeddingProvider(api_key="sk-fake", **provider_kwargs)
    return provider, async_openai_ctor, embeddings_create


# ---------------------------------------------------------------------------
# A.5 — base_url forwarding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_base_url_forwarded_to_async_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """``base_url`` is passed through to ``openai.AsyncOpenAI`` so a TEI
    sidecar (``http://tei:80/v1``) becomes a drop-in OpenAI replacement."""
    _, async_openai_ctor, _ = _patched_provider(
        monkeypatch, base_url="http://tei:80/v1"
    )
    kwargs = async_openai_ctor.call_args.kwargs
    assert kwargs.get("base_url") == "http://tei:80/v1"


@pytest.mark.unit
def test_base_url_omitted_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``base_url`` is not given, the kwarg must not be sent
    (would override the SDK's hosted-OpenAI default)."""
    _, async_openai_ctor, _ = _patched_provider(monkeypatch)
    assert "base_url" not in async_openai_ctor.call_args.kwargs


# ---------------------------------------------------------------------------
# A.5 — send_dimensions toggle
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_dimensions_false_omits_dimensions_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TEI rejects ``dimensions=`` — when pointed at a TEI sidecar,
    ``send_dimensions=False`` must drop the kwarg from every embed call."""
    provider, _, create = _patched_provider(monkeypatch, send_dimensions=False)
    await provider.embed("hello")
    assert "dimensions" not in create.call_args.kwargs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_dimensions_true_sends_vector_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default path (hosted OpenAI) still sends the ``dimensions``
    kwarg pinned to the schema's ``VECTOR_DIM``."""
    provider, _, create = _patched_provider(monkeypatch)  # default True
    await provider.embed("hello")
    assert create.call_args.kwargs.get("dimensions") == VECTOR_DIM


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_batch_respects_send_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``embed_batch`` honours the same toggle as ``embed`` —
    important so a TEI sidecar serves both ingest and search paths."""
    provider, _, create = _patched_provider(monkeypatch, send_dimensions=False)
    create.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(index=0, embedding=[0.1] * VECTOR_DIM),
            SimpleNamespace(index=1, embedding=[0.2] * VECTOR_DIM),
        ]
    )
    await provider.embed_batch(["a", "b"])
    assert "dimensions" not in create.call_args.kwargs


# ---------------------------------------------------------------------------
# Option B — Matryoshka truncation + L2 renormalization
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_truncate_to_dim_slices_and_renormalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vectors trained with MRL (Qwen3-Embedding, jina-v3,
    snowflake-arctic-l) stay coherent under truncation, but the
    truncated vector is no longer unit-norm. The provider must slice
    AND L2-renormalize so cosine similarity at the pgvector layer
    stays correct.

    ``truncate_to_dim`` must equal ``VECTOR_DIM`` (the only
    operationally valid value — any smaller value would produce
    vectors pgvector rejects at write time). We simulate a wider
    model by producing a ``2 * VECTOR_DIM`` response vector so the
    truncation has something to slice off."""
    raw = list(range(1, 2 * VECTOR_DIM + 1))  # native dim 2*VECTOR_DIM
    provider, _, _ = _patched_provider(
        monkeypatch,
        response_vector=raw,
        truncate_to_dim=VECTOR_DIM,
        send_dimensions=False,
    )
    out = await provider.embed("anything")

    assert len(out) == VECTOR_DIM
    norm = math.sqrt(sum(x * x for x in out))
    assert math.isclose(norm, 1.0, abs_tol=1e-9)
    # Verify proportional structure preserved: the slice is [1, 2, ..., VECTOR_DIM]
    # → renormalised; relative ratios survive.
    sum_sq = sum(v * v for v in range(1, VECTOR_DIM + 1))
    expected_norm = math.sqrt(sum_sq)
    for i, got in enumerate(out, start=1):
        assert math.isclose(got, i / expected_norm, abs_tol=1e-9)


@pytest.mark.unit
def test_openai_provider_init_rejects_truncate_below_vector_dim() -> None:
    """Defence-in-depth (the registry already validates this, but
    direct construction shouldn't bypass it). Constructing the provider
    with ``truncate_to_dim != VECTOR_DIM`` must raise ``ValueError``
    immediately rather than silently producing schema-incompatible
    vectors that pgvector rejects far from the construction site."""
    with pytest.raises(ValueError, match="must equal VECTOR_DIM"):
        OpenAIEmbeddingProvider(api_key="sk-test", truncate_to_dim=512)


@pytest.mark.unit
def test_openai_provider_init_rejects_truncate_above_vector_dim() -> None:
    """Mirror of the below-VECTOR_DIM case — anything other than exactly
    ``VECTOR_DIM`` is invalid."""
    with pytest.raises(ValueError, match="must equal VECTOR_DIM"):
        OpenAIEmbeddingProvider(api_key="sk-test", truncate_to_dim=VECTOR_DIM + 1)


@pytest.mark.unit
def test_openai_provider_init_accepts_truncate_equal_to_vector_dim() -> None:
    """Exact-equality is the only operationally valid value: truncating
    a wider model's native output (e.g. Qwen3-4B at 2560) down to the
    schema dimension."""
    p = OpenAIEmbeddingProvider(api_key="sk-test", truncate_to_dim=VECTOR_DIM)
    assert p is not None


@pytest.mark.unit
def test_openai_provider_init_accepts_truncate_unset() -> None:
    """``truncate_to_dim=None`` (the default) is the symmetric path —
    no validation, no truncation, vectors pass through unchanged."""
    p = OpenAIEmbeddingProvider(api_key="sk-test")
    assert p is not None
    p2 = OpenAIEmbeddingProvider(api_key="sk-test", truncate_to_dim=None)
    assert p2 is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_truncate_to_dim_unset_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No truncation knob → vector is returned untouched
    (no surprise renormalisation on the OpenAI hosted path)."""
    raw = [0.3, 0.4, 0.0, 0.0]  # not unit-norm
    provider, _, _ = _patched_provider(
        monkeypatch, response_vector=raw, send_dimensions=False
    )
    out = await provider.embed("hi")
    assert out == raw


@pytest.mark.unit
@pytest.mark.asyncio
async def test_truncate_to_dim_raises_on_undersized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the configured model returns FEWER dimensions than
    ``truncate_to_dim``, ``_postprocess`` must raise instead of
    silently passing through. An undersized vector would make every
    pgvector insert fail with ``expected N dimensions, not M`` —
    failing fast in the provider attributes the error to the
    model+truncate config rather than surfacing as a far-downstream
    write error.

    Use a half-sized response (VECTOR_DIM // 2) against
    ``truncate_to_dim=VECTOR_DIM`` to simulate a misconfigured model
    (e.g. operator pointed ``OPENAI_EMBEDDING_MODEL`` at a 512-d
    legacy model while ``truncate_to_dim`` expected a wider one)."""
    half = VECTOR_DIM // 2
    raw = list(range(1, half + 1))
    provider, _, _ = _patched_provider(
        monkeypatch,
        response_vector=raw,
        truncate_to_dim=VECTOR_DIM,
        send_dimensions=False,
    )
    with pytest.raises(ValueError) as ei:
        await provider.embed("any")
    msg = str(ei.value)
    assert str(half) in msg
    assert str(VECTOR_DIM) in msg
    assert "must produce at least" in msg


# ---------------------------------------------------------------------------
# Option B — embed_query (asymmetric, instruction-aware)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_query_no_instruction_passes_text_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric models (bge-m3, gte-en-v1.5, OpenAI
    text-embedding-3-small) get no prefix — ``embed_query`` is
    equivalent to ``embed`` when no instruction resolves."""
    provider, _, create = _patched_provider(monkeypatch, send_dimensions=False)
    await provider.embed_query("what is the capital of France?")
    assert create.call_args.kwargs["input"] == "what is the capital of France?"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_query_per_call_instruction_prepended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-call ``instruction`` arg overrides the constructor default
    and produces the Qwen-style ``Instruct: ...\\nQuery: ...`` prefix."""
    provider, _, create = _patched_provider(monkeypatch, send_dimensions=False)
    await provider.embed_query("paris", instruction="Retrieve relevant memories")
    expected = "Instruct: Retrieve relevant memories\nQuery: paris"
    assert create.call_args.kwargs["input"] == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_query_constructor_default_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``query_instruction=`` set at construction is the fallback
    for any caller that doesn't override per-call. This is the
    process-wide default plumbed via ``EMBEDDING_QUERY_INSTRUCTION``."""
    provider, _, create = _patched_provider(
        monkeypatch,
        send_dimensions=False,
        query_instruction="Default task",
    )
    await provider.embed_query("query body")
    assert create.call_args.kwargs["input"] == "Instruct: Default task\nQuery: query body"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_unaffected_by_query_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``embed`` (the document/ingest path) NEVER prepends the
    instruction — that asymmetry is the whole point of Option B."""
    provider, _, create = _patched_provider(
        monkeypatch,
        send_dimensions=False,
        query_instruction="Should not appear",
    )
    await provider.embed("a document chunk")
    assert create.call_args.kwargs["input"] == "a document chunk"


# ---------------------------------------------------------------------------
# Registry-side validation (env-driven knobs)
# ---------------------------------------------------------------------------


def _reset_registry_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe the registry's process-level OpenAI provider LRU so a test
    sees a fresh cache-miss path. Without this, a populated cache from
    a previous test would silently skip the validation / misconfig
    branches a test is asserting against."""
    import common.embedding._registry as registry_mod

    monkeypatch.setattr(registry_mod, "_openai_provider_cache", OrderedDict())


@pytest.mark.unit
def test_registry_rejects_truncate_to_dim_above_schema_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OPENAI_EMBEDDING_TRUNCATE_TO_DIM`` greater than ``VECTOR_DIM`` is
    nonsensical — pgvector would reject the wider vector at write time.
    The registry catches this misconfiguration up-front with a clear
    ``ValueError`` instead of letting embed calls 4xx in production."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_TRUNCATE_TO_DIM", str(VECTOR_DIM + 256))

    with pytest.raises(ValueError, match="must equal VECTOR_DIM"):
        get_embedding_provider("openai")


@pytest.mark.unit
def test_registry_rejects_truncate_to_dim_below_schema_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value < ``VECTOR_DIM`` (e.g. 512) passes the naive ``> VECTOR_DIM``
    check but produces vectors pgvector would reject because they don't
    match the schema column dimension. The exact-equality check catches
    this too."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_TRUNCATE_TO_DIM", str(VECTOR_DIM - 256))

    with pytest.raises(ValueError, match="must equal VECTOR_DIM"):
        get_embedding_provider("openai")


@pytest.mark.unit
def test_registry_accepts_truncate_to_dim_equal_to_schema_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only operationally valid value is exactly ``VECTOR_DIM``:
    truncating a wider model's native output (e.g. Qwen3-4B at 2560)
    down to the 1024-dim schema."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_TRUNCATE_TO_DIM", str(VECTOR_DIM))

    # Should not raise.
    p = get_embedding_provider("openai")
    assert p is not None


@pytest.mark.unit
def test_registry_validation_skipped_on_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot-path optimisation (Fix 1): cache lookup runs *before*
    truncate-dim validation. Once a provider is cached, subsequent calls
    with the same env return the cached instance without going through
    the validation path again.

    Proof: pre-populate the cache slot for a known config tuple with a
    sentinel. The next ``get_embedding_provider`` call with that same
    config must return the sentinel — meaning the validation +
    construction path was never re-entered."""
    import common.embedding._registry as registry_mod
    from common.embedding._registry import get_embedding_provider
    from common.embedding.constants import OPENAI_EMBEDDING_MODEL

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", raising=False)
    monkeypatch.delenv("EMBEDDING_QUERY_INSTRUCTION", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_TRUNCATE_TO_DIM", raising=False)

    # Build the exact cache_key the registry will compute for the env
    # above and pre-populate it with a sentinel. If validation ran on
    # cache hit, the registry would never reach the ``return cached``
    # branch — it'd fall through to construction and overwrite us.
    sentinel = object()
    cache_key = (
        "sk-test",
        OPENAI_EMBEDDING_MODEL,
        None,  # base_url
        True,  # send_dimensions (default)
        None,  # query_instruction
        None,  # truncate_to_dim
    )
    registry_mod._openai_provider_cache[cache_key] = sentinel  # type: ignore[assignment]

    p = get_embedding_provider("openai")
    assert p is sentinel, (
        "cache hit must return without re-running validation/construction"
    )


@pytest.mark.unit
def test_registry_raises_when_base_url_set_but_send_dimensions_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TEI / vLLM / most OpenAI-compatible self-hosted endpoints reject
    the ``dimensions=`` SDK kwarg. Setting ``OPENAI_EMBEDDING_BASE_URL``
    without flipping ``OPENAI_EMBEDDING_SEND_DIMENSIONS=false`` would
    cause every embed call to 4xx, retries to exhaust, and writes to
    silently persist with ``embedding=NULL``. Failing fast at provider
    construction (raise) is strictly better than logging a warning and
    falling through."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_BASE_URL", "http://tei:80/v1")
    # Default for SEND_DIMENSIONS is "true" — leave unset.
    monkeypatch.delenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", raising=False)

    with pytest.raises(ValueError) as ei:
        get_embedding_provider("openai")
    msg = str(ei.value)
    assert "OPENAI_EMBEDDING_BASE_URL" in msg
    assert "SEND_DIMENSIONS" in msg
    assert "TEI" in msg or "vLLM" in msg


@pytest.mark.unit
def test_registry_misconfig_raises_on_every_call_no_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed construction must NOT be cached as a sentinel. Every
    call with the misconfigured env raises freshly — operators see the
    error on every request until they fix it, not just the first. (The
    cache is only populated on successful construction.)"""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_BASE_URL", "http://tei:80/v1")
    monkeypatch.delenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", raising=False)

    for _ in range(3):
        with pytest.raises(ValueError):
            get_embedding_provider("openai")


@pytest.mark.unit
def test_registry_raises_inverse_no_base_url_with_send_dims_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted OpenAI (no ``base_url``) + ``SEND_DIMENSIONS=false`` is a
    write-time time bomb: OpenAI returns the model's native dim (1536
    for text-embedding-3-small) and pgvector rejects every insert. The
    registry must raise at construction so the operator catches the
    misconfiguration before the first failed write — not log a warning
    and let writes silently persist NULL."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", "false")

    with pytest.raises(ValueError) as ei:
        get_embedding_provider("openai")
    msg = str(ei.value)
    assert "SEND_DIMENSIONS=false" in msg
    assert "BASE_URL is unset" in msg
    # Message names a concrete native-dim example so the error is
    # actionable for an operator who hasn't memorised every model's
    # native dim.
    assert "1536" in msg or "text-embedding-3-small" in msg


@pytest.mark.unit
def test_registry_accepts_correct_tei_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``base_url`` set + ``SEND_DIMENSIONS=false`` is the correct TEI
    configuration: no raise, provider constructs cleanly."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_BASE_URL", "http://tei:80/v1")
    monkeypatch.setenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", "false")

    p = get_embedding_provider("openai")
    assert p is not None


@pytest.mark.unit
def test_registry_accepts_default_hosted_openai_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default hosted OpenAI: no base_url, send_dimensions left at its
    default (true). The happy path — no raise, provider constructs
    cleanly."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", raising=False)

    p = get_embedding_provider("openai")
    assert p is not None


@pytest.mark.unit
def test_registry_truncate_to_dim_non_integer_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer value in ``OPENAI_EMBEDDING_TRUNCATE_TO_DIM`` must
    raise a ``ValueError`` whose message names the env var and the
    offending raw value, not the bare CPython
    ``"invalid literal for int() with base 10: '...'"``. Operators
    grep startup logs for ``OPENAI_EMBEDDING_*`` when something's
    wrong; the parse error should be findable that way."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_TRUNCATE_TO_DIM", "ten-twenty-four")

    with pytest.raises(
        ValueError,
        match=r"OPENAI_EMBEDDING_TRUNCATE_TO_DIM=.*must be an integer",
    ):
        get_embedding_provider("openai")


@pytest.mark.unit
@pytest.mark.parametrize("val", ["yes", "1", "trun", "0", "TRU"])
def test_registry_rejects_non_canonical_send_dimensions_value(
    monkeypatch: pytest.MonkeyPatch,
    val: str,
) -> None:
    """Strict bool parsing for ``OPENAI_EMBEDDING_SEND_DIMENSIONS``: only
    ``"true"`` / ``"false"`` (case-insensitive) accepted. A typo or a
    different truthy syntax (``yes``, ``1``) must raise instead of
    silently coercing to a default — which historically biased toward
    ``true`` and that's the wrong default direction for a flag the
    registry now hard-fails on."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", val)

    with pytest.raises(
        ValueError,
        match=r"OPENAI_EMBEDDING_SEND_DIMENSIONS=.*must be 'true' or 'false'",
    ):
        get_embedding_provider("openai")


@pytest.mark.unit
@pytest.mark.parametrize("val", ["true", "TRUE", "True", "false", "FALSE", "False"])
def test_registry_accepts_case_insensitive_canonical_send_dimensions(
    monkeypatch: pytest.MonkeyPatch,
    val: str,
) -> None:
    """Mirror of the rejection test: the canonical bool spellings (with
    case variation) all parse cleanly. Tied with appropriate base_url
    config so neither misconfig branch trips."""
    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", val)
    if val.lower() == "false":
        # Pair with base_url so the "no base_url + send_dims=false"
        # branch doesn't fire.
        monkeypatch.setenv("OPENAI_EMBEDDING_BASE_URL", "http://tei:80/v1")
    else:
        monkeypatch.delenv("OPENAI_EMBEDDING_BASE_URL", raising=False)

    p = get_embedding_provider("openai")
    assert p is not None


@pytest.mark.unit
def test_registry_no_warning_when_send_dimensions_false(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mirror case: ``base_url`` set + ``SEND_DIMENSIONS=false`` is
    the correct TEI configuration and must NOT trigger the warning."""
    import logging

    from common.embedding._registry import get_embedding_provider

    _reset_registry_state(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_BASE_URL", "http://tei:80/v1")
    monkeypatch.setenv("OPENAI_EMBEDDING_SEND_DIMENSIONS", "false")

    with caplog.at_level(logging.WARNING, logger="common.embedding._registry"):
        get_embedding_provider("openai")

    assert not any(
        "SEND_DIMENSIONS" in rec.getMessage() for rec in caplog.records
    ), "no SEND_DIMENSIONS warning expected when correctly set to false"


# ---------------------------------------------------------------------------
# Split protocols: EmbeddingProvider vs InstructionAwareEmbedder
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_symmetric_providers_do_not_implement_instruction_aware() -> None:
    """``Fake``, ``Local``, and ``Vertex`` are symmetric encoders by design
    — they conform to :class:`EmbeddingProvider` but deliberately not to
    :class:`InstructionAwareEmbedder`. The query path detects that and
    falls back to :meth:`embed`."""
    from common.embedding.protocols import (
        EmbeddingProvider,
        InstructionAwareEmbedder,
    )
    from common.embedding.providers.fake import FakeEmbeddingProvider

    p = FakeEmbeddingProvider()
    assert isinstance(p, EmbeddingProvider)
    assert not isinstance(p, InstructionAwareEmbedder)
    assert not hasattr(p, "embed_query")


@pytest.mark.unit
def test_openai_provider_implements_instruction_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OpenAIEmbeddingProvider`` implements both protocols — it owns the
    ``Instruct: ...\\nQuery: ...`` prefix logic when an instruction is
    configured."""
    from common.embedding.protocols import (
        EmbeddingProvider,
        InstructionAwareEmbedder,
    )

    p, _, _ = _patched_provider(monkeypatch)
    assert isinstance(p, EmbeddingProvider)
    assert isinstance(p, InstructionAwareEmbedder)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_query_embedding_falls_back_for_symmetric_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_query_embedding`` ``hasattr``-checks
    :class:`InstructionAwareEmbedder` and routes symmetric providers to
    :meth:`embed`. Verified end-to-end through the service layer with a
    fake provider that lacks ``embed_query`` — ``instruction`` is silently
    ignored, output equals ``embed(text)``."""
    from common.embedding._service import get_query_embedding
    from common.embedding.providers.fake import FakeEmbeddingProvider

    fake = FakeEmbeddingProvider()
    monkeypatch.setattr(
        "common.embedding._service.get_embedding_provider",
        lambda *_a, **_k: fake,
    )

    expected = await fake.embed("paris")
    got_no_instr = await get_query_embedding("paris")
    got_with_instr = await get_query_embedding("paris", instruction="should be ignored")
    assert got_no_instr == expected
    assert got_with_instr == expected
