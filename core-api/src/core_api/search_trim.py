"""Head-slice trim that reserves result slots for FTS-only rows (#687).

Lives at the package root, importing only :mod:`core_api.constants`, so both the
pipeline post-filter step and the legacy search path can use it without an import
cycle. One implementation on purpose: the two paths trim independently, and when
this logic was duplicated a boundary bug had to be fixed in both copies.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core_api.constants import FTS_ONLY_RESERVED_RESULTS


def trim_reserving_fts_only(
    rows: list[Any],
    top_k: int,
    is_fts_only: Callable[[Any], bool],
) -> list[Any]:
    """Trim ``rows`` to ``top_k``, keeping one FTS-only row if any exist.

    #687: exempting FTS-only rows from the cosine gate is not enough to make them
    reachable. They score on ``fts_score`` alone — single digits of a percent for
    one term — so with enough embedded rows above them a plain head slice drops
    them, having already survived storage's candidate LIMIT only because that
    reserves slots for them too.

    The reservation is deliberately minimal: it promotes at most
    ``FTS_ONLY_RESERVED_RESULTS`` rows, displacing the same number from the tail
    of the head — the weakest results — and only when the head contains none
    already. It never reorders anything. The population is transient: a row is
    FTS-only just until the deferred embed backfill lands, after which it competes
    on cosine normally.

    ``is_fts_only`` is passed in because the two callers hold different row
    shapes: the pipeline has objects (attribute access), the legacy path dicts.

    Never consumes the whole head. A ``top_k=1`` caller (valid input —
    ``schemas.py`` allows ``ge=1``) asked for their single best match, and
    answering with only an FTS-pending stub in its place is worse than not
    surfacing the stub at all. #687's contract is that such a row is
    *discoverable*, which storage's candidate reservation still provides; it was
    never that the row outranks the best result.
    """
    head = rows[:top_k]
    if FTS_ONLY_RESERVED_RESULTS <= 0 or top_k <= 0:
        return head
    if any(is_fts_only(r) for r in head):
        return head
    # The ``- 1`` is what stops the promotion taking every slot: it keeps
    # ``top_k - len(promoted)`` in the slice below at 1 or more, so at least one
    # row that earned its place on score always survives.
    budget = min(FTS_ONLY_RESERVED_RESULTS, top_k - 1)
    if budget <= 0:
        return head
    promoted = [r for r in rows[top_k:] if is_fts_only(r)][:budget]
    if not promoted:
        return head
    return head[: top_k - len(promoted)] + promoted
