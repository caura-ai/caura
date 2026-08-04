"""Remote reranker — HTTP call to a separate ranking service (sidecar).

Mirrors ``OpenAIEmbeddingProvider`` + ``OPENAI_EMBEDDING_BASE_URL``: the model
lives in its own service (a self-hosted TEI reranker, typically GPU bge, or a
hosted Cohere-style ``/rerank``) and core-api sends it the query + candidate
texts over HTTP. This keeps torch out of the core-api image — the deployment
pattern the stack already uses for the TEI *embedder*.

Contract (TEI-native ``/rerank``, the primary target since the infra runs TEI):

    POST {RANK_BASE_URL}/rerank
    { "query": "<query>", "texts": ["<candidate 0>", "<candidate 1>", ...] }
    -> [ {"index": 1, "score": 0.91}, {"index": 0, "score": 0.42}, ... ]

The response is a ranked list of ``{index, score}`` (index = position in the
input ``texts``). We also accept a Cohere-style wrapper
(``{"results": [{"index", "relevance_score"}]}``) so a Cohere-compatible
endpoint drops in without an adapter. Scores are re-projected back to INPUT
order — the caller (RerankResults) owns the sort.
"""

from __future__ import annotations

import itertools
import logging

import httpx

from common.ranking.constants import RANK_TIMEOUT_SECONDS
from common.ranking.errors import PermanentRankError
from common.ranking.protocols import RankCandidate

logger = logging.getLogger(__name__)

# 4xx statuses that ARE worth retrying: a request timeout and rate limiting are
# both about *when* the call happened, not what it contained. Every other 4xx
# says the request itself is unacceptable and will be rejected identically next
# time. 5xx stays transient (it falls through to raise_for_status below).
#
# Deliberately NOT the same policy as ``common/http_retry.RETRYABLE_STATUS_CODES``
# ({502,503,504}, and 4xx never retried). That module serves the storage
# clients, where a 429 means "back off, you are hammering a shared database".
# Here the callee is a single-tenant sidecar doing one idempotent read per
# search, so its 408/429 are load signals worth one more attempt inside the
# rerank deadline. If the two policies ever need to agree, reconcile them
# explicitly rather than assuming one was a typo.
_RETRYABLE_CLIENT_STATUSES = frozenset({408, 429})

# Response bodies land in an error log, so cap them. Volume is the obvious
# reason (a misrouted base URL can return a full HTML page), but the
# load-bearing one is disclosure: a 4xx body from an arbitrary sidecar or an
# intervening proxy may echo the request back, and the request carries the
# user's stored memory text. This cap bounds how much of that a rerank
# misconfiguration can write into logs that may ship off-box. Raise it only
# with that in mind. (``common/llm/providers/_shape_error.py`` caps at 1024 for
# the same job; smaller here because a rerank body is a short JSON error.)
_ERROR_BODY_LIMIT = 300

# Opaque per-instance identity for ``dedup_scope``. A counter beats deriving the
# scope from the backend config twice over: no credential ever enters the scope
# string (hashing one is a fast-hash-of-a-secret, which CodeQL rightly flags),
# and the scope is a fixed shape that cannot collide under the prefix matching
# ``_clear_permanent_for`` does. See ``dedup_scope`` for the tradeoff.
_instance_seq = itertools.count()


class RemoteRanker:
    """Reranker backed by a separate HTTP ``/rerank`` service."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout: float = RANK_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._instance_id = next(_instance_seq)
        headers = {}
        if api_key:
            # Bearer for Cohere / token-gated TEI; harmless for open TEI.
            headers["Authorization"] = f"Bearer {api_key}"
        # Long-lived pooled client (one rerank call per search). Explicit
        # limits + timeout so a hung sidecar can't ride httpx's 600s default;
        # the service layer's wait_for is the hard outer deadline, this is the
        # transport-level guard.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    @property
    def provider_name(self) -> str:
        return "remote"

    @property
    def dedup_scope(self) -> str:
        """Namespace for permanent-fault log dedup — see ``PermanentRankError``.

        Per INSTANCE, which stands in for per backend config: the registry
        caches one ranker per ``(base_url, api_key, model)``, so live instances
        are distinct backends. That covers the collisions a URL-only scope
        would miss — same URL with a per-tenant ``rank_model``, or same URL and
        model behind different ``rank_api_key`` credentials — without putting
        any of those values, least of all the credential, into the string. The
        URL still reaches the operator: it is in the message.

        The instance is a proxy for the config, not a permanent name for it:
        ``_remote_ranker_cache`` is LRU-bounded (32), so a process cycling
        through more backends than that can evict and later rebuild the ranker
        for the same triple, and the rebuilt one gets a fresh scope. A
        still-broken backend then re-reports once. That is deliberate — it errs
        toward saying too much about a real fault rather than too little, the
        same direction ``_PERMANENT_LOGGED_MAX`` overflow errs in. Deriving the
        scope from the config instead would hold across eviction, but only by
        reintroducing a value-derived key over tenant-supplied strings, which
        is what the credential and delimiter problems came from.
        """
        return f"remote:{self._instance_id}"

    @property
    def model(self) -> str:
        return self._model or "remote"

    async def aclose(self) -> None:
        """Close the httpx pool cleanly (called on registry cache eviction)."""
        await self._client.aclose()

    def _reject_detail(self, resp: httpx.Response, candidate_count: int) -> str:
        """Build the operator-facing message for a permanently rejected call.

        Carries what it takes to act without opening the sidecar's own logs:
        the endpoint, the status, how many candidates we sent, and the
        service's own words.
        """
        detail = (
            f"rerank sidecar rejected the request: HTTP {resp.status_code} from "
            f"{self._base_url}/rerank with {candidate_count} candidate(s). "
            f"Response: {resp.text[:_ERROR_BODY_LIMIT]}"
        )
        if resp.status_code == 413:
            # By far the most common way to land here, and the fix is not
            # guessable from "413": the pool we send is RANK_CANDIDATE_LIMIT
            # (50 by default) but a TEI reranker admits only 32 per request
            # unless --max-client-batch-size is raised.
            detail += (
                " A 413 here usually means the sidecar's max client batch size "
                "is below RANK_CANDIDATE_LIMIT — raise it (TEI: "
                "--max-client-batch-size) or lower RANK_CANDIDATE_LIMIT."
            )
        return detail

    async def rank(self, query: str, candidates: list[RankCandidate]) -> list[float]:
        if not candidates:
            return []
        body: dict = {"query": query, "texts": [c.content for c in candidates]}
        resp = await self._client.post("/rerank", json=body)
        if resp.is_client_error and resp.status_code not in _RETRYABLE_CLIENT_STATUSES:
            raise PermanentRankError(
                self._reject_detail(resp, len(candidates)),
                # Stable + backend-scoped; the detail embeds a response body
                # and must not reach the dedup key.
                key=f"{self.dedup_scope}|{resp.status_code}",
            )
        # Everything else (5xx, 408, 429) keeps the original behaviour: raise,
        # and let the service layer's retry budget decide.
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            # A 200 carrying a non-JSON body (an HTML error page from a
            # misrouted base URL, a proxy interstitial) is the same
            # wrong-endpoint fault as the not-a-ranked-list case below, and
            # equally unfixable by retrying.
            raise PermanentRankError(
                f"rerank response from {self._base_url}/rerank was not JSON "
                f"({exc}) — does RANK_BASE_URL point at a reranker? "
                f"Response: {resp.text[:_ERROR_BODY_LIMIT]}",
                key=f"{self.dedup_scope}|invalid-json",
            ) from exc

        # TEI returns a bare list; Cohere-style wraps it in {"results": [...]}.
        results = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(results, list):
            # A 200 that isn't a ranked list means this endpoint does not speak
            # /rerank at all — almost always RANK_BASE_URL pointing somewhere
            # else. Retrying re-fetches the same wrong shape.
            raise PermanentRankError(
                f"rerank response from {self._base_url}/rerank was not a ranked "
                f"list but {type(results).__name__} — does RANK_BASE_URL point "
                f"at a reranker? Response: {resp.text[:_ERROR_BODY_LIMIT]}",
                key=f"{self.dedup_scope}|response-not-a-list",
            )

        # Re-project the ranked results back onto the INPUT order. Missing
        # indices (a partial response) keep 0.0 — the sort then places them
        # last, which is the safe degradation for a candidate the service
        # dropped rather than crashing the search.
        scores = [0.0] * len(candidates)
        for item in results:
            idx = item["index"]
            if not (0 <= idx < len(candidates)):
                continue
            score = item.get("score")
            if score is None:
                score = item.get("relevance_score")
            scores[idx] = float(score)
        return scores
