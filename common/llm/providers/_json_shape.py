"""Shared JSON-shape coercion for ``complete_json`` responses.

gemini-3.1-flash-lite (prod, 2026-09-03→06) sometimes wraps a valid
response object in a one-element array — ~3.8% of enrichments arrived
as ``[ {..valid enrichment..} ]`` and the CAURA-651 shape guard was
discarding good answers into the fake fallback (~170/day). A singleton
list holding one dict is unambiguous: unwrap it. Every other non-dict
shape (empty or multi-element lists, scalar elements, bare scalars) is
still garbage and stays with the caller's ShapeError.

Shared between the Gemini-backed providers (``vertex.py`` ADC mode,
``gemini.py`` API-key mode) — same rationale as ``_truncation.py``: one
implementation, so the heuristic cannot silently drift between them.

Deliberately schema-agnostic: this layer serves every ``complete_json``
consumer (enrichment, extraction, dedup, contradiction, …) and knows
nothing about their expected keys. A wrong-but-dict payload that slips
through lands in the caller's own field validation (e.g.
``_validate_enrichment``) exactly as an un-wrapped wrong dict would —
the guard here only exists to stop bare non-dict shapes from surfacing
as ``AttributeError`` (CAURA-651).
"""

from __future__ import annotations

import logging


def unwrap_singleton_array(
    parsed: object,
    *,
    provider: str,
    model: str,
    log: logging.Logger,
) -> object:
    """Return the inner dict of a one-element list, else *parsed* unchanged.

    Never raises — shape rejection stays the caller's job so the typed
    per-provider ShapeError (and its monitoring attributes) is preserved.
    """
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        log.info("%s (%s) returned a singleton JSON array; unwrapped", provider, model)
        return parsed[0]
    return parsed
