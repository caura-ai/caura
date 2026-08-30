"""Shared post-processing helpers for scored search results.

Lives at the package root, importing only :mod:`core_api.constants`, so both the
pipeline post-filter step and the legacy search path can use it without an import
cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core_api.constants import FTS_RESERVED_RESULTS


def passes_relevance_filter(
    *,
    has_embedding: bool | None,
    vec_sim: float | None,
    min_similarity: float,
    fts_match: bool = False,
    allow_fts_global_floor_bypass: bool = False,
) -> bool:
    """Apply the relevance floor, including its two lexical exceptions.

    Storage admits an unembedded row only through full-text search and represents
    its missing cosine as ``0.0``. ``has_embedding`` distinguishes that sentinel
    from a real orthogonal vector. An embedded full-text match may bypass only
    the untuned global fallback; request, agent, and tenant floors remain strict.
    """
    return (
        has_embedding is False
        or vec_sim is None
        or float(vec_sim) >= min_similarity
        or (allow_fts_global_floor_bypass and fts_match)
    )


def trim_reserving_fts_matches(
    rows: list[Any],
    top_k: int,
    is_fts_match: Callable[[Any], bool],
) -> list[Any]:
    """Trim ``rows`` to ``top_k``, keeping one full-text match if available.

    A full-text match can rank below vector-only candidates and fall outside a
    plain head slice. Storage reserves matching candidates for the same reason;
    this final trim makes one of those candidates visible without changing the
    ordering of the remaining results. It also preserves #687's guarantee for a
    matching row whose embedding backfill has not landed yet.

    The reservation is deliberately minimal: it promotes at most
    ``FTS_RESERVED_RESULTS`` rows, displacing the same number from the tail
    of the head — the weakest results — and only when the head contains none
    already. It never reorders anything.

    ``is_fts_match`` is passed in because the two callers hold different row
    shapes: the pipeline has objects (attribute access), the legacy path dicts.

    Never consumes the whole head. A ``top_k=1`` caller (valid input —
    ``schemas.py`` allows ``ge=1``) asked for their single best match, and
    answering with only an FTS-pending stub in its place is worse than not
    surfacing the stub at all. #687's contract is that such a row is
    *discoverable*, which storage's candidate reservation still provides; it was
    never that the row outranks the best result.
    """
    head = rows[:top_k]
    if FTS_RESERVED_RESULTS <= 0 or top_k <= 0:
        return head
    if any(is_fts_match(r) for r in head):
        return head
    # The ``- 1`` is what stops the promotion taking every slot: it keeps
    # ``top_k - len(promoted)`` in the slice below at 1 or more, so at least one
    # row that earned its place on score always survives.
    budget = min(FTS_RESERVED_RESULTS, top_k - 1)
    if budget <= 0:
        return head
    promoted = [r for r in rows[top_k:] if is_fts_match(r)][:budget]
    if not promoted:
        return head
    return head[: top_k - len(promoted)] + promoted
