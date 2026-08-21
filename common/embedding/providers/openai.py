"""OpenAI-compatible embedding provider."""

from __future__ import annotations

import itertools
import math

import httpx
import openai

from common.constants import VECTOR_DIM
from common.embedding.constants import (
    EMBEDDING_HOSTED_MAX_BATCH,
    EMBEDDING_HTTPX_CONNECT_TIMEOUT_SECONDS,
    EMBEDDING_HTTPX_MAX_CONNECTIONS,
    EMBEDDING_HTTPX_MAX_KEEPALIVE_CONNECTIONS,
    EMBEDDING_HTTPX_POOL_TIMEOUT_SECONDS,
    EMBEDDING_REMOTE_MAX_BATCH,
    OPENAI_EMBEDDING_MODEL,
    OPENAI_REQUEST_TIMEOUT_SECONDS,
)

# Opaque per-instance identity for ``dedup_scope``. A counter beats deriving
# the scope from the backend config twice over: no credential ever enters the
# scope string (hashing one is a fast-hash-of-a-secret, which CodeQL rightly
# flags), and the scope is a fixed shape that cannot collide. Same device, same
# reasoning, as ``common/ranking/providers/remote.py``.
_instance_seq = itertools.count()


class OpenAIEmbeddingProvider:
    """Embedding provider using the OpenAI embeddings API.

    Also serves any OpenAI-compatible endpoint (TEI, vLLM, etc.) by setting
    ``base_url``. Supports asymmetric query/doc encoding for instruction-aware
    embedders (Qwen3-Embedding, e5-instruct, ...) via :meth:`embed_query` and
    the ``query_instruction`` constructor arg. Supports Matryoshka-style
    output truncation via ``truncate_to_dim`` so models with native dim
    larger than the pgvector column can be used without a schema migration.
    """

    def __init__(
        self,
        api_key: str,
        model: str = OPENAI_EMBEDDING_MODEL,
        base_url: str | None = None,
        send_dimensions: bool = True,
        query_instruction: str | None = None,
        truncate_to_dim: int | None = None,
        max_batch: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._send_dimensions = send_dimensions
        self._query_instruction = query_instruction
        self._truncate_to_dim = truncate_to_dim
        # Derived from the backend, because the right cap is a property of
        # WHICH backend this is. ``base_url`` set means a self-hosted
        # OpenAI-compatible server whose admission limit we cannot see, so
        # assume TEI's conservative default; unset means hosted OpenAI,
        # whose documented limit is 2048 and which gains nothing from being
        # split. This is the same discriminator the registry already uses
        # to decide ``send_dimensions``.
        #
        # Explicit ``max_batch`` overrides both — injectable so a test can
        # exercise chunking without a 33-text fixture. The registry never
        # passes it, so every real provider derives; that is also why it is
        # not part of the registry's cache key.
        if max_batch is None:
            max_batch = (
                EMBEDDING_REMOTE_MAX_BATCH if base_url else EMBEDDING_HOSTED_MAX_BATCH
            )
        # Same defence-in-depth argument as truncate_to_dim above, and the
        # failure it prevents is worse. The env path is already safe
        # (``read_int_env`` rejects non-positive), but direct construction
        # is not, and a non-positive cap does NOT fail cleanly:
        # ``max_batch=-1`` makes ``range(0, n, -1)`` empty, so embed_batch
        # returns ZERO embeddings for n texts with no error at all — and
        # callers zip the result onto rows positionally. Silent data loss.
        # (``max_batch=0`` raises deep in the loop instead.) Fail here.
        if max_batch < 1:
            raise ValueError(
                f"max_batch={max_batch} must be >= 1; it is the number of "
                "texts sent per provider request."
            )
        self._max_batch = max_batch
        self._instance_id = next(_instance_seq)
        # Retained for ``backend_label`` only. Not a secret, unlike the key,
        # and the single most actionable identifier for a self-hosted backend.
        self._base_url = base_url
        # Defence in depth: the registry's ``get_embedding_provider``
        # already validates this knob before constructing a provider,
        # but direct construction (a one-off script, a migration helper,
        # a test that mocks the registry) bypasses that path. Without
        # this guard, ``truncate_to_dim=512`` against a 1024-dim schema
        # silently produces 512-element vectors that pgvector rejects
        # at write time with a dim-mismatch error far from the original
        # constructor call. Fail fast at construction instead.
        if truncate_to_dim is not None and truncate_to_dim != VECTOR_DIM:
            raise ValueError(
                f"truncate_to_dim={truncate_to_dim} must equal "
                f"VECTOR_DIM={VECTOR_DIM}; this knob is only for truncating a "
                "wider model's native output to the schema dimension."
            )
        # Explicit per-call timeout — without this the SDK rides
        # httpx's default 600s read timeout, and a single hung
        # api.openai.com response would silently eat the worker's
        # entire ack budget. Mirrors the same env-driven default that
        # gates ``OpenAILLMProvider``.
        #
        # Explicit ``http_client`` with ``httpx.Limits`` sized for our
        # bulk-write fan-out (CAURA-627). Same rationale as
        # ``OpenAILLMProvider``: the SDK's default httpx pool (100 max
        # / 20 keepalive) saturates under storm load and queues other
        # tenants' embed calls at the pool layer.
        client_kwargs: dict = {
            "api_key": api_key,
            "timeout": httpx.Timeout(
                connect=EMBEDDING_HTTPX_CONNECT_TIMEOUT_SECONDS,
                # read AND write keep the full request budget — the bare float
                # this replaces set every phase to it.
                read=OPENAI_REQUEST_TIMEOUT_SECONDS,
                write=OPENAI_REQUEST_TIMEOUT_SECONDS,
                # Pool tracks the request budget unless explicitly decoupled,
                # preserving the previous behaviour for every configuration.
                pool=(
                    EMBEDDING_HTTPX_POOL_TIMEOUT_SECONDS
                    if EMBEDDING_HTTPX_POOL_TIMEOUT_SECONDS is not None
                    else OPENAI_REQUEST_TIMEOUT_SECONDS
                ),
            ),
            "http_client": httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=EMBEDDING_HTTPX_MAX_CONNECTIONS,
                    max_keepalive_connections=EMBEDDING_HTTPX_MAX_KEEPALIVE_CONNECTIONS,
                ),
            ),
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**client_kwargs)

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def dedup_scope(self) -> str:
        """Namespace for per-backend failure stats — see ``_stats_by_scope``.

        Per INSTANCE, standing in for per backend config: the registry caches
        one provider per ``(api_key, model, base_url, send_dimensions,
        query_instruction, truncate_to_dim)``, so live instances are distinct
        backends. That covers what a name+model scope misses — two tenants on
        the same model behind different keys, or two self-hosted sidecars both
        serving ``bge-m3`` at different URLs — without putting any of those
        values, least of all the credential, into the string.

        The instance is a proxy for the config, not a permanent name for it:
        the registry's cache is bounded, so a process cycling through more
        backends than that can evict and rebuild the provider for the same
        tuple, and the rebuilt one gets a fresh scope. A still-broken backend
        then restarts its streak and re-reports. Deliberate, and the same
        direction ``_STATS_SCOPE_MAX`` overflow errs in: say too much about a
        real fault rather than too little.
        """
        return f"openai:{self._instance_id}"

    @property
    def backend_label(self) -> str:
        """Human-facing identity for log lines — never the scope key.

        ``dedup_scope`` is an opaque counter on purpose, which makes it a
        correct key and a useless thing to read in an alert. This is the
        other half of the same trade ``common/ranking`` makes: keep the
        credential out of the key, and still put the URL in the message.
        """
        where = self._base_url or "hosted"
        return f"openai model={self._model} at {where}"

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        """Close the underlying httpx pool cleanly.

        Without this, ``asyncio`` debug mode emits ``ResourceWarning:
        Unclosed <httpx.AsyncClient>`` when the provider is GC'd —
        noisy in tests and a leak in long-lived processes that rotate
        client instances. Idempotent; safe to call multiple times.
        """
        await self._client.close()

    def _postprocess(self, emb: list[float]) -> list[float]:
        # Matryoshka truncation: slice to ``truncate_to_dim`` and L2-renormalize.
        # Models trained with MRL (Qwen3-Embedding, jina-v3, snowflake-arctic-l)
        # produce vectors that stay coherent under truncation, but the resulting
        # vector is no longer unit-norm — we re-normalize so cosine similarity
        # at the pgvector layer stays correct.
        # Explicit ``is not None`` rather than truthy check: a
        # hypothetical ``truncate_to_dim=0`` would slip through a
        # truthy gate as "no truncation" but is invalid configuration
        # the registry already rejects. Be unambiguous here too.
        if self._truncate_to_dim is not None and len(emb) > self._truncate_to_dim:
            # Slicing a list already returns a new list — no need to
            # wrap in ``list(...)`` (which would allocate a redundant
            # second copy of the truncated data).
            emb = emb[: self._truncate_to_dim]
            n = math.sqrt(sum(x * x for x in emb))
            if n > 0:
                emb = [x / n for x in emb]
        elif self._truncate_to_dim is not None and len(emb) < self._truncate_to_dim:
            # Undersized passthrough is silently broken: returning a
            # vector with fewer than ``truncate_to_dim`` elements would
            # produce inserts that pgvector rejects with
            # ``expected N dimensions, not M``. Fail fast here so the
            # mismatch is attributable to the model+truncate config,
            # not surfaced obliquely as a write error far downstream.
            raise ValueError(
                f"Model returned {len(emb)}-dim vector but truncate_to_dim="
                f"{self._truncate_to_dim}; the configured model must produce "
                f"at least {self._truncate_to_dim} native dimensions."
            )
        return emb

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for a single text (document/ingest path)."""
        kwargs: dict = {"model": self._model, "input": text}
        if self._send_dimensions:
            kwargs["dimensions"] = VECTOR_DIM
        response = await self._client.embeddings.create(**kwargs)
        return self._postprocess(response.data[0].embedding)

    async def _embed_batch_chunk(self, texts: list[str]) -> list[list[float]]:
        """One request's worth of texts. Caller guarantees the size cap."""
        kwargs: dict = {"model": self._model, "input": texts}
        if self._send_dimensions:
            kwargs["dimensions"] = VECTOR_DIM
        response = await self._client.embeddings.create(**kwargs)
        # OpenAI returns embeddings sorted by index
        sorted_data = sorted(response.data, key=lambda x: x.index)
        return [self._postprocess(item.embedding) for item in sorted_data]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts, splitting across requests if needed.

        Chunking is invisible in the result because each text is embedded
        INDEPENDENTLY — a vector does not depend on what shares its
        request — so N chunks return exactly what one large call would, in
        input order. (Contrast ``common/ranking``'s remote reranker, where
        the same optimisation is only safe because a cross-encoder scores
        pairs independently, and would break for a listwise model.)

        See ``EMBEDDING_REMOTE_MAX_BATCH`` for why the cap exists: without
        it, a bulk write of up to ``BULK_MAX_ITEMS`` texts is rejected
        outright by a TEI sidecar at its default cap of 32, and the
        failure hides in a per-item fallback that still produces correct
        embeddings.

        SEQUENTIAL, unlike the reranker's concurrent ``asyncio.gather``.
        Two reasons, both about not undoing work this module already did:
        ``call_embedding_gated`` holds ONE ``EMBEDDING_MAX_CONCURRENCY`` slot for
        this whole call, so fanning out inside it would multiply the real
        provider concurrency by the chunk count and defeat a cap that
        exists because of the 2026-07-27 connection-pool exhaustion. And
        the budget is wide enough not to need it: bulk callers arm 30 s
        (``BULK_EMBEDDING_TIMEOUT_SECONDS``) where the reranker had 0.5 s,
        against four ~10 ms sidecar round trips for a 100-text write.
        """
        # A pool that fits one request behaves exactly as it did before
        # chunking existed — no extra allocation, and the provider
        # exception propagates with its own traceback.
        if len(texts) <= self._max_batch:
            return await self._embed_batch_chunk(texts)

        embeddings: list[list[float]] = []
        for start in range(0, len(texts), self._max_batch):
            chunk = texts[start : start + self._max_batch]
            embeddings.extend(await self._embed_batch_chunk(chunk))
        return embeddings

    async def embed_query(
        self, text: str, instruction: str | None = None
    ) -> list[float]:
        """Generate an embedding vector for a search-side query.

        For instruction-aware models (Qwen3-Embedding, e5-instruct), prepends
        the resolved instruction in the convention these models were trained
        with: ``"Instruct: <task>\\nQuery: <text>"``. The instruction is
        resolved as: per-call *instruction* arg → constructor
        *query_instruction* → no prefix.

        For symmetric models (bge-m3, snowflake-arctic-l, gte-en-v1.5,
        OpenAI text-embedding-3-small), pass no instruction at construction
        time and this is equivalent to :meth:`embed`.
        """
        instr = instruction or self._query_instruction
        if instr:
            text = f"Instruct: {instr}\nQuery: {text}"
        return await self.embed(text)
