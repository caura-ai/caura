"""Shared post-processing helpers for scored search results.

Lives at the package root, importing only :mod:`core_api.constants`, so both the
pipeline post-filter step and the legacy search path can use it without an import
cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core_api.constants import FTS_RESERVED_RESULTS, INCLUDE_DERIVED_DEFAULT


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


# pm-0918-c-03. The marker set that identifies an atomic-fact fan-out child.
#
# READ THIS BEFORE SIMPLIFYING THE PREDICATE BELOW TO A ``parent_memory_id``
# CHECK. It looks like the same thing and it is not.
#
# Exactly two writers put ``parent_memory_id`` into child metadata, and they
# produce rows with opposite properties (both in ``services/memory_service.py``):
#
#   source="atomic_fact_fanout"  A70's fan-out. One claim extracted FROM a
#                                parent that keeps its full text and stays
#                                retrievable on its own. Redundant by
#                                construction — this is the population the
#                                caller is asking to be rid of.
#
#   source="auto_chunk"          The >2,000-character chunker. These ARE the
#                                only sub-document vectors the store has. The
#                                parent row does carry the whole document in
#                                ``content``, but it carries ONE embedding over
#                                all of it, which is the entire reason chunking
#                                exists. Dropping these does not remove
#                                duplicates; it removes the only vector that can
#                                match a specific passage of a long document.
#
# A ``parent_memory_id IS NOT NULL`` filter catches both. The regression that
# causes does not look like this code: it looks like "long documents stopped
# being findable", days later, with nothing pointing here.
#
# The other half is not optional either, and for a different reason.
# ``source`` is DELIBERATELY absent from ``PLATFORM_ONLY_KEYS``
# (``services/system_metadata.py`` says so, and why: ingest writes it too, in
# caller-adjacent item metadata). So a caller can put any value it likes in
# ``source``, including this one. ``parent_memory_id`` IS reserved and is
# stripped from caller input, so requiring BOTH halves makes the predicate
# unforgeable — a caller cannot hide its own rows from the default search by
# stamping a source on them.
#
# Not used: ``source_uri IS NULL``. Fan-out children happen to have no
# ``source_uri`` (their payload has no such key) while auto-chunk children
# inherit the parent's, so it correlates — but it is incidental. On the
# development corpus 19,622 of 55,621 ORDINARY rows also have it null; a filter
# using it would delete a third of a normal store.
_FANOUT_SOURCE = "atomic_fact_fanout"
_PARENT_KEY = "parent_memory_id"
_SYSTEM_NAMESPACE = "_system"


def is_derived_fanout_row(metadata: Any) -> bool:
    """True for an atomic-fact fan-out child, false for everything else.

    Takes the row's raw metadata rather than the row, because the two search
    paths hold different shapes — the pipeline has ORM objects, the legacy path
    dicts — exactly as ``trim_reserving_fts_matches`` takes ``is_fts_match`` for
    the same reason.

    Both writers store their markers at the TOP level of ``metadata``: neither
    goes through ``set_system_value``, so nothing lands in ``_system`` today.
    The namespace is checked as a fallback anyway, because
    ``extract_system_metadata`` merges the two on read — so a row written the
    other way would be a fan-out child to every consumer while being invisible
    to a top-level-only probe here, and that divergence would be silent.
    """
    if not isinstance(metadata, dict):
        # Rows with null or scalar metadata cannot be derived: neither writer
        # can produce one, since both build a dict literal. Non-dict is a
        # perfectly ordinary row, not a parse failure to flag.
        return False
    nested = metadata.get(_SYSTEM_NAMESPACE)
    if not isinstance(nested, dict):
        nested = {}
    has_parent = metadata.get(_PARENT_KEY) is not None or nested.get(_PARENT_KEY) is not None
    source = nested.get("source", metadata.get("source"))
    return has_parent and source == _FANOUT_SOURCE


def resolve_include_derived(request_value: bool | None, tenant_config: Any) -> bool:
    """Three-layer resolution: request flag > tenant setting > global default.

    Lives here rather than in either caller so the pipeline and legacy search
    paths cannot answer the question differently. That is not a hypothetical
    concern in this file's neighbourhood: two ranking features shipped applying
    to the pipeline path alone because each search path registered its knobs
    separately, which also meant the documented ``_USE_PIPELINE_SEARCH = False``
    rollback lever silently reverted them.

    ``None`` from either layer means "not set", NOT "false" — which is why the
    request field is ``bool | None`` and the tenant default is ``None`` in
    ``DEFAULT_SETTINGS``. A caller that explicitly sends ``true`` must be able to
    override a tenant that has turned derived rows off, and a tri-state is the
    only shape that can tell that apart from not asking.

    ``getattr`` with a default because ``tenant_config`` is optional on this
    path and older config objects and test doubles predate the property —
    matching how ``strict_fleet_scoping`` is read in the pipeline ctx builder.
    """
    if request_value is not None:
        return bool(request_value)
    resolved = getattr(tenant_config, "search_include_derived", None)
    if resolved is not None:
        return bool(resolved)
    return INCLUDE_DERIVED_DEFAULT
