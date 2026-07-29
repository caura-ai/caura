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

import logging

from common.provider_names import ProviderName
from common.ranking.constants import RANK_MODEL
from common.ranking.protocols import RankProvider
from common.ranking.providers.fake import FakeRanker
from common.ranking.providers.local import LocalCrossEncoderRanker
from common.ranking.providers.noop import NoopRanker

logger = logging.getLogger(__name__)

# Cache the in-process cross-encoder per model name so the (large) model
# loads once per process rather than on every search. Keyed on model name
# so a per-tenant ``rank_model`` override gets its own cached instance.
_local_ranker_cache: dict[str, LocalCrossEncoderRanker] = {}


def get_rank_provider(name: str, tenant_config: object | None = None) -> RankProvider:
    """Construct a rank provider by name.

    Parameters
    ----------
    name:
        ``"noop"`` (default), ``"local"`` (in-process MiniLM), or
        ``"fake"`` (tests). ``"remote"`` is reserved for a future HTTP
        sidecar provider.
    tenant_config:
        Optional ``ResolvedConfig``-shaped object for per-tenant overrides
        (``rank_model``). Duck-typed via ``getattr`` — may be ``None``.

    Raises
    ------
    ValueError
        If the provider name is unknown.
    """
    if name == ProviderName.NONE or name == "noop":
        return NoopRanker()

    if name == ProviderName.FAKE:
        return FakeRanker()

    if name == ProviderName.LOCAL:
        model = (
            getattr(tenant_config, "rank_model", None)
            if tenant_config is not None
            else None
        ) or RANK_MODEL
        cached = _local_ranker_cache.get(model)
        if cached is None:
            cached = LocalCrossEncoderRanker(model_name=model)
            _local_ranker_cache[model] = cached
        return cached

    raise ValueError(f"Unknown rank provider: {name}")
