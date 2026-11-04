"""EC-05 — explain an empty recall with the closest below-cut-off result.

A search that returns nothing reads to an agent as "nothing was ever saved",
when the usual truth is "something was saved, and it scored just under the
similarity cut-off". The pipeline already holds that candidate — ``raw_rows``
is storage's scored set before ``PostFilterResults`` applies the floor — and
until now only ``LogRecallEvent`` looked at it, on a sampled, opt-in log.

Scoping is inherited, not re-implemented: storage applies tenant, fleet,
visibility, agent, type, status, verification (``verified_only``) and date
filters in the scored query itself, so every row in ``raw_rows`` is one the
caller could read. The only cuts made
after that are the cut-off, the derived-row exclusion and the top_k trim. When
nothing survived, the trim cut nothing, so a candidate here failed the cut-off
— and the derived exclusion is re-applied below, so a row the caller opted out
of is never named. The hint is therefore "passed every filter except the
score cut-off", by construction.

No ranking change and no extra query: one pass over rows already in memory,
only on an empty result.
"""

from __future__ import annotations

import re

from core_api.search_trim import is_derived_fanout_row

# Short enough that the hint stays a hint, long enough to recognise the memory.
_PREVIEW_CHARS = 80
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")
_WHITESPACE = re.compile(r"\s+")


def _preview(memory) -> str:
    """A one-line, length-capped label for the memory: its title, else content."""
    raw = getattr(memory, "title", None) or getattr(memory, "content", None) or ""
    text = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", str(raw))).strip()
    if len(text) > _PREVIEW_CHARS:
        text = text[: _PREVIEW_CHARS - 1].rstrip() + "…"
    return text


def build_empty_hint(
    raw_rows: list | None,
    *,
    min_similarity: float | None,
    include_derived: bool = True,
) -> dict | None:
    """The closest below-cut-off candidate of an EMPTY search, or None.

    Callers invoke this only when the search returned no results. Returns None
    when there is no candidate with a real cosine to report (nothing scored,
    only unembedded rows, or no cut-off in play).
    """
    if not raw_rows or min_similarity is None:
        return None
    best = None
    best_sim = None
    for row in raw_rows:
        # A missing vector's 0.0 is a sentinel, not a measurement (L-113).
        if getattr(row, "has_embedding", True) is False:
            continue
        sim = getattr(row, "vec_sim", None)
        if sim is None:
            continue
        memory = getattr(row, "Memory", None)
        if not include_derived and is_derived_fanout_row(getattr(memory, "metadata_", None)):
            continue
        if best_sim is None or float(sim) > best_sim:
            best, best_sim = row, float(sim)
    if best is None or best_sim is None:
        return None
    memory = best.Memory
    memory_id = str(getattr(memory, "id", ""))
    title = _preview(memory)
    similarity = round(best_sim, 4)
    cutoff = round(float(min_similarity), 4)
    label = f'"{title}" ' if title else ""
    return {
        "memory_id": memory_id,
        "title": title,
        "similarity": similarity,
        "min_similarity": cutoff,
        "message": (
            f"No result cleared the similarity cut-off {cutoff:.2f}. "
            f"Closest: {label}(id {memory_id}), similarity {similarity:.2f}. "
            "If it looks relevant, read it by id."
        ),
    }
