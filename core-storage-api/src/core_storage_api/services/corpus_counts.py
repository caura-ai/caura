"""The report's corpus counts, kept for a few minutes (Prism App Review v2 N035).

``PostgresService.memory_quality_metrics(recalled_after=...)`` counts the whole
corpus search can return and how much of it was recalled in the window. Those
two counts are the only quality figures bounded by neither ``created_at`` nor
``memory_type``: each is one pass over all of a tenant's live rows, and a
dashboard reads the report on every load. A share of the corpus recalled in a
period moves slowly, so a few minutes' lag is the price of not rescanning.

Its own module rather than a class in ``postgres_service``: the tenant-scope
gate's check 4 reads that module by bare function name, and a second
``__init__`` there would make it ambiguous.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable

CORPUS_COUNT_TTL_S = 300.0


class CorpusCountCache:
    """``(corpus_total, recalled_in_window)`` per scope and window, kept for
    ``ttl_s``.

    Per process, as a replica's own read is; bounded, oldest entry out first.
    The key is built by the caller from every input the corpus filters read,
    so no two scopes share a count.
    """

    def __init__(
        self,
        ttl_s: float = CORPUS_COUNT_TTL_S,
        max_entries: int = 1_024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[tuple, tuple[float, tuple[int, int]]] = OrderedDict()

    def get(self, key: tuple) -> tuple[int, int] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, counts = entry
        if self._clock() - stored_at >= self._ttl_s:
            del self._entries[key]
            return None
        return counts

    def put(self, key: tuple, counts: tuple[int, int]) -> None:
        self._entries[key] = (self._clock(), counts)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
