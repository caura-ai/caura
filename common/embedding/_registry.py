"""Embedding-provider factory.

Resolves a concrete provider instance from the canonical name +
optional tenant config. Tier order:

    Tenant key  →  Platform singleton  →  FakeEmbeddingProvider

Env-driven (no service-config dependency) so both core-api (tenant-aware)
and core-worker (platform-only) can use it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict

from common.constants import VECTOR_DIM
from common.embedding.constants import OPENAI_EMBEDDING_MODEL
from common.embedding.protocols import EmbeddingProvider
from common.embedding.providers.fake import FakeEmbeddingProvider
from common.embedding.providers.local import LocalEmbedding
from common.embedding.providers.openai import OpenAIEmbeddingProvider
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)

# Cache OpenAI provider instances by (api_key, model). Each instance
# holds a long-lived ``AsyncOpenAI`` (and therefore a long-lived
# ``httpx.AsyncClient`` connection pool); without the cache, every
# ``get_embedding`` call from core-api would build a fresh pool and
# pay a TLS handshake to api.openai.com on every request. Multi-tenant
# safe: keying on ``api_key`` keeps tenant-A's client from being
# reused for tenant-B's request. The platform-tier singleton has its
# own caching (managed in ``_platform.py``) — we don't double-cache it.
#
# LRU-bounded so a rotated/revoked tenant key eventually gets evicted
# instead of pinning a 401-returning client forever. ``OrderedDict``
# semantics: ``move_to_end`` on hit, ``popitem(last=False)`` on miss
# when full. 256 is well above any realistic tenant count for a single
# process; raise if that proves wrong.
#
# Eviction must explicitly close the evicted provider's httpx pool
# (CAURA-627). The OpenAI SDK now gets a user-provided ``http_client``
# (set in ``OpenAIEmbeddingProvider.__init__`` so we control pool
# sizing); per the SDK contract, user-provided clients are NOT closed
# on SDK teardown — caller owns cleanup. ``_get_or_create_openai_provider``
# below schedules ``aclose()`` on the evicted instance via
# ``asyncio.create_task`` so the pool drains in the background instead
# of leaking ``ResourceWarning`` and held connections under long-lived
# processes that rotate keys past the cache cap.
_OPENAI_CACHE_MAX = 256
_openai_provider_cache: OrderedDict[
    tuple[str, str, str | None, bool, str | None, int | None],
    OpenAIEmbeddingProvider,
] = OrderedDict()

# Strong references to in-flight ``aclose()`` cleanup tasks so the GC
# doesn't reap them mid-execution. ``asyncio.create_task`` returns a
# task that's kept alive only by user references; without this set the
# task could be collected before its coroutine finishes draining the
# httpx pool. The ``add_done_callback`` removes the task from the set
# once it completes so this doesn't grow unboundedly.
_background_tasks: set[asyncio.Task[None]] = set()


def _get_or_create_openai_provider(
    api_key: str,
    model: str,
    base_url: str | None,
    send_dimensions: bool,
    query_instruction: str | None,
    truncate_to_dim: int | None,
) -> OpenAIEmbeddingProvider:
    """LRU-bounded ``OpenAIEmbeddingProvider`` lookup keyed on the full client
    config tuple.

    Cache key includes ``base_url`` / ``send_dimensions`` / ``query_instruction``
    / ``truncate_to_dim`` so the same api_key can host multiple simultaneous
    providers — e.g. real OpenAI for one tenant and a local TEI sidecar with
    a Qwen3 instruction prefix for another — without the second silently
    aliasing to the first cached client.

    Not strictly async-safe across concurrent cache misses for the same
    key — two coroutines can both observe a miss before either inserts.
    The race is harmless: the later insert overwrites the earlier with
    a functionally identical client (same key tuple, no per-call state),
    and the earlier instance gets dropped on the next GC. Not worth an
    asyncio.Lock for the cost of a duplicated TLS handshake on the
    first concurrent miss.
    """
    cache_key = (
        api_key,
        model,
        base_url,
        send_dimensions,
        query_instruction,
        truncate_to_dim,
    )
    cached = _openai_provider_cache.get(cache_key)
    if cached is not None:
        _openai_provider_cache.move_to_end(cache_key)
        return cached
    provider = OpenAIEmbeddingProvider(
        api_key=api_key,
        model=model,
        base_url=base_url,
        send_dimensions=send_dimensions,
        query_instruction=query_instruction,
        truncate_to_dim=truncate_to_dim,
    )
    _openai_provider_cache[cache_key] = provider
    if len(_openai_provider_cache) > _OPENAI_CACHE_MAX:
        _, evicted = _openai_provider_cache.popitem(last=False)
        # Schedule cleanup of the evicted provider's httpx pool. The
        # SDK won't do it for us (we pass an explicit ``http_client``).
        # Best-effort — bare ``except RuntimeError`` covers the rare
        # case where the registry is exercised outside a running event
        # loop (e.g. early startup); GC will reclaim the connections
        # eventually but with the ``ResourceWarning`` we'd see in
        # asyncio debug mode.
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(evicted.aclose())
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except RuntimeError:
            pass
    return provider


def _resolve_openai_api_key(tenant_config: object | None) -> str:
    """Tenant override first, then ``OPENAI_API_KEY`` env. Empty string if neither.

    Tenant config is duck-typed (loose ``getattr`` protocol) so callers
    can pass any object exposing an ``openai_api_key`` attribute (or
    none at all).
    """
    if tenant_config is not None:
        key = getattr(tenant_config, "openai_api_key", None)
        if key:
            return key
    return os.environ.get("OPENAI_API_KEY", "")


def get_embedding_provider(
    name: str,
    tenant_config: object | None = None,
) -> EmbeddingProvider:
    """Construct an embedding provider by name.

    Parameters
    ----------
    name:
        Provider identifier: ``"openai"``, ``"local"``, or ``"fake"``.
    tenant_config:
        Optional ``ResolvedConfig``-shaped object for per-tenant overrides
        (``openai_api_key``, ``embedding_model``). Can be ``None`` for
        platform-only callers (e.g. core-worker).

    Raises
    ------
    ValueError
        If the provider name is unknown, or if the OpenAI-compatible
        env var combination would guarantee 100% failed embed calls
        (``base_url`` set with ``send_dimensions=true``, or
        ``base_url`` unset with ``send_dimensions=false``), or if
        ``OPENAI_EMBEDDING_TRUNCATE_TO_DIM`` is set to anything other
        than ``VECTOR_DIM``, or if it is not parseable as an integer.
    """
    if name == ProviderName.FAKE:
        return FakeEmbeddingProvider()

    if name == ProviderName.OPENAI:
        api_key = _resolve_openai_api_key(tenant_config)
        if not api_key:
            # Lazy import — avoids a circular at module-load time and
            # keeps platform setup off the cold-start critical path
            # for callers that don't need it.
            from common.embedding._platform import get_platform_embedding

            platform = get_platform_embedding()
            if platform is not None:
                logger.info(
                    "No tenant key for OpenAI embedding, using platform embedding (%s)",
                    platform.model,
                )
                return platform
            logger.warning(
                "No API key for OpenAI embedding provider, returning FakeEmbeddingProvider",
            )
            return FakeEmbeddingProvider()
        embed_model = (
            getattr(tenant_config, "embedding_model", None)
            if tenant_config is not None
            else None
        ) or OPENAI_EMBEDDING_MODEL
        base_url = os.environ.get("OPENAI_EMBEDDING_BASE_URL") or None
        # Strict bool parsing: only ``"true"`` / ``"false"`` (case-insensitive)
        # accepted. The previous ``!= "false"`` check silently treated typos
        # ("trun", "yes", "1") and accidental values as true, which is the
        # wrong default direction for a flag that controls a config the
        # registry now hard-fails on (any base_url + send_dimensions=true
        # raises). Operators get a clear error on the parse rather than
        # downstream on the misconfig.
        _send_dim_raw = os.environ.get(
            "OPENAI_EMBEDDING_SEND_DIMENSIONS", "true"
        ).lower()
        if _send_dim_raw not in ("true", "false"):
            raise ValueError(
                f"OPENAI_EMBEDDING_SEND_DIMENSIONS={_send_dim_raw!r} "
                "must be 'true' or 'false'."
            )
        send_dimensions = _send_dim_raw == "true"
        # Option B: instruction-aware query encoding. Default applied to all
        # /api/v1/search calls; documents (writes) embed unmodified text.
        query_instruction = os.environ.get("EMBEDDING_QUERY_INSTRUCTION") or None
        truncate_raw = os.environ.get("OPENAI_EMBEDDING_TRUNCATE_TO_DIM")
        # Surface the env var name in the parse error. Bare ``int(...)``
        # raises ``ValueError: invalid literal for int() with base 10:
        # 'foo'`` — which is true but doesn't tell the operator which
        # knob they fat-fingered. Re-raise with the variable name and
        # the offending value.
        try:
            truncate_to_dim = int(truncate_raw) if truncate_raw else None
        except ValueError:
            raise ValueError(
                f"OPENAI_EMBEDDING_TRUNCATE_TO_DIM={truncate_raw!r} must be an integer"
            ) from None

        # Hot-path note. ``get_embedding_provider`` runs on every embed
        # and search request, so we want the steady-state cache hit to
        # be as cheap as possible — but the cache key itself includes
        # the *parsed* env values (``send_dimensions: bool``,
        # ``truncate_to_dim: int | None``), so the env lookups + strict
        # bool/int parsing above ALWAYS run, regardless of cache hit
        # or miss. They're O(microseconds) (a couple of ``os.environ``
        # reads + a string compare + at most one ``int()``) and have
        # been measured as below the noise floor of the embed call
        # itself, but they're not free. The expensive structural
        # validation — the ``base_url ⊕ send_dimensions`` raise and the
        # ``truncate_to_dim`` vs ``VECTOR_DIM`` raise below — is what
        # the cache lookup actually skips. On a steady-state cache hit
        # nothing about the env-driven config has changed since the
        # cached provider was constructed, so we don't need to re-run
        # those structural checks. (Operators changing env vars
        # mid-process would need to restart anyway — env parses don't
        # invalidate cache entries.)
        #
        # ``_get_or_create_openai_provider`` re-does the same dict
        # lookup on a miss — single O(1) and not worth a
        # signature-breaking refactor.
        cache_key = (
            api_key,
            embed_model,
            base_url,
            send_dimensions,
            query_instruction,
            truncate_to_dim,
        )
        cached = _openai_provider_cache.get(cache_key)
        if cached is not None:
            _openai_provider_cache.move_to_end(cache_key)
            return cached

        # ── Cache miss — first time we see this config tuple. ─────────
        # Both misconfiguration shapes below GUARANTEE that every embed
        # call will fail (4xx from the provider, or schema-dim mismatch
        # at the pgvector layer). A warning + fall-through would mean:
        # provider gets constructed, every request 4xx's, retries
        # exhaust, ``get_embedding`` returns None, writes persist with
        # ``embedding=NULL``, search silently broken. That's strictly
        # worse than failing fast at construction time — the operator
        # sees the misconfiguration on the first request rather than
        # debugging mysteriously empty search results.
        #
        # Forward misconfiguration: TEI / vLLM and most OpenAI-compatible
        # self-hosted endpoints reject the ``dimensions=`` SDK kwarg
        # outright. Hosted ``api.openai.com`` is the only target that
        # accepts (and requires) it.
        if base_url and send_dimensions:
            raise ValueError(
                f"OPENAI_EMBEDDING_BASE_URL={base_url!r} is set but "
                "OPENAI_EMBEDDING_SEND_DIMENSIONS is true. Most "
                "OpenAI-compatible self-hosted endpoints (TEI, vLLM, ...) "
                "reject the ``dimensions=`` kwarg. Set "
                "OPENAI_EMBEDDING_SEND_DIMENSIONS=false for TEI/vLLM, or "
                "remove OPENAI_EMBEDDING_BASE_URL to use hosted OpenAI."
            )

        # Inverse misconfiguration: hosted OpenAI (no ``base_url``) with
        # ``send_dimensions=false``. Hosted ``api.openai.com`` honours
        # ``dimensions=`` to truncate the model's native output down to
        # the schema dim; omitting it returns the full native dim
        # (1536 for ``text-embedding-3-small``, 3072 for
        # ``text-embedding-3-large``), which pgvector rejects on every
        # write with ``expected N dimensions, not M``.
        if not base_url and not send_dimensions:
            raise ValueError(
                "OPENAI_EMBEDDING_SEND_DIMENSIONS=false but "
                "OPENAI_EMBEDDING_BASE_URL is unset (hosted OpenAI). "
                f"Hosted OpenAI returns model {embed_model}'s native dim "
                "without the ``dimensions=`` kwarg (e.g. 1536 for "
                "text-embedding-3-small), which pgvector rejects at "
                "write time. Set OPENAI_EMBEDDING_SEND_DIMENSIONS=true "
                "(or unset it) for the hosted OpenAI path, or set "
                "OPENAI_EMBEDDING_BASE_URL to point at a self-hosted "
                "endpoint that produces VECTOR_DIM-sized vectors natively."
            )

        # Matryoshka truncation: lets us run instruction-aware models with
        # native dim > VECTOR_DIM (Qwen3-Embedding-4B is 2560-d, 8B is
        # 4096-d) against the 1024-d schema. Qwen3-Embedding-0.6B is
        # natively 1024-d and does NOT need this knob — leave unset.
        # ``OpenAIEmbeddingProvider._postprocess`` slices and
        # L2-renormalizes so cosine sim correctness is preserved.
        #
        # Only valid value is exactly ``VECTOR_DIM``. Anything smaller
        # produces vectors pgvector rejects at write time (column type
        # is ``vector(VECTOR_DIM)``); anything larger would not fit
        # the schema column either. The knob exists to truncate a
        # *wider* model's native output down to the schema dimension —
        # that's the only meaningful operation. Catch the misuse here
        # instead of letting writes 4xx in production.
        if truncate_to_dim is not None and truncate_to_dim != VECTOR_DIM:
            raise ValueError(
                f"OPENAI_EMBEDDING_TRUNCATE_TO_DIM={truncate_to_dim} must equal "
                f"VECTOR_DIM={VECTOR_DIM}; this knob is only for truncating a "
                "wider model's native output to the schema dimension."
            )
        return _get_or_create_openai_provider(
            api_key,
            embed_model,
            base_url,
            send_dimensions,
            query_instruction,
            truncate_to_dim,
        )

    if name == ProviderName.LOCAL:
        return LocalEmbedding()

    raise ValueError(f"Unknown embedding provider: {name}")
