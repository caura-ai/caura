"""Ranking failure classification.

Every rerank failure degrades to first-stage order — that is the component's
whole posture and it does not change here. What this adds is the distinction
between the two *reasons* a rerank can fail, because they want opposite
handling:

* **Transient** (timeout, 429, 5xx, connection reset): the next attempt might
  succeed. Retry it, log at ``warning``. This is the default for anything not
  explicitly classified, so a new failure mode never silently stops retrying.
* **Permanent** (:class:`PermanentRankError`): a configuration-class fault that
  will fail *identically* on every subsequent call until a human changes
  something — a batch cap below the candidate limit, a base URL pointing at a
  service that doesn't speak ``/rerank``, a rejected API key. Retrying spends
  the turn's latency budget to reach the same failure, so the service layer
  stops immediately and logs at ``error`` with the provider's detail.

The split matters because the two are indistinguishable from the outside: both
serve first-stage order and neither breaks recall, so a misconfiguration looks
exactly like load. Providers raise the specific type; the service layer stays
transport-agnostic and only reacts to it.
"""

from __future__ import annotations


class PermanentRankError(Exception):
    """A rerank failure that will recur until the configuration changes.

    Raise this instead of a generic exception when the provider can tell the
    fault is not worth retrying. The message is surfaced verbatim in the
    service layer's ``error`` log, so it should carry everything an operator
    needs to act on without opening the provider's own logs.

    ``key`` identifies the *condition* for log-deduplication. It is required —
    there is no default, because every plausible default is wrong: the message
    embeds a response body and varies per request, which would defeat the
    dedup silently rather than loudly. Two properties it must have:

    * **Stable** across occurrences of the same fault, or the service layer
      logs a fresh ERROR every search and volume tracks traffic.
    * **Scoped to the failing backend**, via the provider's
      :attr:`dedup_scope`. One process can hold several remote rankers at once
      (``common/ranking/_registry.py`` caches them per
      ``(base_url, api_key, model)`` so per-tenant ``rank_base_url`` overrides
      each get their own). A bare ``"remote:413"`` would let one tenant's
      logged fault suppress a different tenant's unrelated one.

    Build it as ``f"{provider.dedup_scope}|{condition}"`` — see
    :class:`~common.ranking.providers.remote.RemoteRanker`.
    """

    def __init__(self, message: str, *, key: str) -> None:
        super().__init__(message)
        self.key = key
