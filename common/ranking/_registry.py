"""Rank-provider factory. Resolves a concrete provider from a name.

Env-driven (no service-config dependency), mirroring
``common/embedding/_registry.py``. Unlike embeddings there is no
platform-tier singleton — ranking runs only on the core-api search path.

The ``local`` cross-encoder is cached per model name: the provider holds
a lazily-loaded model (hundreds of MB), so reconstructing it per request
would reload the model every call. ``noop`` / ``fake`` are stateless and
built fresh.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict

from common.provider_names import ProviderName
from common.ranking.constants import RANK_API_KEY, RANK_BASE_URL, RANK_MODEL
from common.ranking.protocols import RankProvider
from common.ranking.providers.fake import FakeRanker
from common.ranking.providers.local import LocalCrossEncoderRanker
from common.ranking.providers.noop import NoopRanker
from common.ranking.providers.remote import RemoteRanker

logger = logging.getLogger(__name__)

# Cache the in-process cross-encoder per model name so the (large) model
# loads once per process rather than on every search. Keyed on model name
# so a per-tenant ``rank_model`` override gets its own cached instance.
_local_ranker_cache: dict[str, LocalCrossEncoderRanker] = {}

# LRU-bounded cache of RemoteRanker instances keyed on the full client config
# tuple. Each holds a long-lived httpx pool, so without the cache every search
# would build a fresh pool + pay a connect to the sidecar. Mirrors the OpenAI
# embedding-provider cache: ``move_to_end`` on hit, evict-oldest + background
# ``aclose()`` on overflow so a rotated URL/key doesn't leak its pool.
_REMOTE_CACHE_MAX = 32
_remote_ranker_cache: OrderedDict[tuple[str, str, str], RemoteRanker] = OrderedDict()
_background_tasks: set[asyncio.Task[None]] = set()


def _get_or_create_remote_ranker(
    base_url: str, api_key: str, model: str
) -> RemoteRanker:
    key = (base_url, api_key, model)
    cached = _remote_ranker_cache.get(key)
    if cached is not None:
        _remote_ranker_cache.move_to_end(key)
        return cached
    ranker = RemoteRanker(base_url=base_url, api_key=api_key, model=model)
    _remote_ranker_cache[key] = ranker
    if len(_remote_ranker_cache) > _REMOTE_CACHE_MAX:
        _, evicted = _remote_ranker_cache.popitem(last=False)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(evicted.aclose())
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except RuntimeError:
            pass
    return ranker


def get_rank_provider(name: str, tenant_config: object | None = None) -> RankProvider:
    """Construct a rank provider by name.

    Parameters
    ----------
    name:
        ``"noop"`` (default), ``"local"`` (in-process MiniLM), ``"remote"``
        (HTTP ``/rerank`` sidecar), or ``"fake"`` (tests).
    tenant_config:
        Optional ``ResolvedConfig``-shaped object for per-tenant overrides
        (``rank_model`` / ``rank_base_url`` / ``rank_api_key``). Duck-typed
        via ``getattr`` — may be ``None``.

    Raises
    ------
    ValueError
        If the provider name is unknown, or ``remote`` is selected with no
        base URL configured (RANK_BASE_URL / tenant ``rank_base_url``).
    """
    if name == ProviderName.NONE or name == "noop":
        return NoopRanker()

    if name == ProviderName.FAKE:
        return FakeRanker()

    def _tc(attr: str):
        return getattr(tenant_config, attr, None) if tenant_config is not None else None

    if name == ProviderName.LOCAL:
        model = _tc("rank_model") or RANK_MODEL
        cached = _local_ranker_cache.get(model)
        if cached is None:
            cached = LocalCrossEncoderRanker(model_name=model)
            _local_ranker_cache[model] = cached
        return cached

    if name == "remote":
        base_url = _tc("rank_base_url") or RANK_BASE_URL
        if not base_url:
            raise ValueError(
                "rank provider 'remote' requires a base URL "
                "(set RANK_BASE_URL or tenant rank_base_url)"
            )
        api_key = (
            _tc("rank_api_key") or RANK_API_KEY or os.environ.get("RANK_API_KEY", "")
        )
        model = _tc("rank_model") or RANK_MODEL
        return _get_or_create_remote_ranker(base_url, api_key, model)

    raise ValueError(f"Unknown rank provider: {name}")
