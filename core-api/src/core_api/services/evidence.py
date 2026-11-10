"""Evidence contract v1: the provenance columns a write carries to storage.

See ``docs/plans/evidence-contract-v1.md``. ``observed_at``, ``trace_id`` and
``tool_call_id`` describe where a memory came from. They are the writer's, except
``trace_id``, which the server stamps when the writer sent none
(``session_context.server_trace_id``). ``verification_status`` is server-owned
(no write body may name it); the server derives it here from the row's own
evidence (EC-13): ``tool_observed`` when it carries a ``tool_call_id`` or a real
``source_uri`` (``common.models.memory.is_real_source_uri``), else
``unverified``.

One helper for every payload that inserts a memory row, so a row derived from
another (an auto-chunk child, an atomic fact) carries its parent's provenance
the way it already carries ``source_uri`` and ``run_id``. A derived row is
given the source and tool call its parent was written with, so it starts in
the state its parent started in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models.memory import DEFAULT_VERIFICATION_STATUS, automatic_verification_status
from core_api.schemas import MemoryOut, RecallEvidence, SearchHitOut

EVIDENCE_WRITE_FIELDS = ("observed_at", "trace_id", "tool_call_id")


def evidence_payload(source: Any) -> dict[str, Any]:
    """The evidence columns of ``source`` as a storage insert payload.

    ``source`` is a write body (``MemoryCreate``, ``BulkMemoryItem``) or a stored
    row as storage returns it. Its ``source_uri`` is read too, for
    ``verification_status``: the parent's, on a derived row. A ``datetime`` goes
    out as ISO text, as the other timestamp fields of these payloads do, since
    the payload is sent as JSON.
    """
    if isinstance(source, dict):
        values = {f: source.get(f) for f in EVIDENCE_WRITE_FIELDS}
        source_uri = source.get("source_uri")
    else:
        values = {f: getattr(source, f, None) for f in EVIDENCE_WRITE_FIELDS}
        source_uri = getattr(source, "source_uri", None)
    observed_at = values["observed_at"]
    if isinstance(observed_at, datetime):
        values["observed_at"] = observed_at.isoformat()
    values["verification_status"] = automatic_verification_status(source_uri, values["tool_call_id"])
    return values


# ── Recall side (EC-10 / EC-12) ──

#: ``source_uri`` ingest stamps on pasted content. A marker, not a source.
SOURCE_URI_PLACEHOLDER = "text-input"

#: Upper bound on the ``why`` phrase, so it stays a label and not a sentence.
WHY_MAX_CHARS = 40

#: Earlier recalls a row needs before ``why`` calls it "often recalled". Any
#: recall lifts ``recall_boost`` above 1.0 (one recall is ~1.009 at the default
#: cap), so the boost alone would tag every hit of a repeated query.
OFTEN_RECALLED_MIN_COUNT = 3

# Strategies whose rows are picked by something other than the score factors.
_ENTITY_LOOKUP = "entity_lookup"
_RECENT_STRATEGIES = ("recent_context", "temporal")


def _source(memory: Any) -> str | None:
    tool_call_id = getattr(memory, "tool_call_id", None)
    if tool_call_id:
        return f"tool:{tool_call_id}"
    source_uri = getattr(memory, "source_uri", None)
    if source_uri and source_uri != SOURCE_URI_PLACEHOLDER:
        return source_uri
    return None


def why_phrase(
    score_parts: Any,
    *,
    strategy: str | None = None,
    injected: bool = False,
    recall_count: int | None = None,
) -> str:
    """Why a row was returned, in a few words, from what ranking already computed.

    Deterministic and cheap: the signals that matched (vector, keyword, entity)
    plus at most one qualifier that fits in ``WHY_MAX_CHARS``. No LLM.
    "often recalled" needs the recall boost applied AND ``recall_count`` (the
    row's count before this search) at ``OFTEN_RECALLED_MIN_COUNT`` or more.
    """
    if injected:
        return "newest version of a recalled fact"
    if score_parts is None:
        # Unscored: the entity short-circuit, or a path that did not score.
        if strategy == _ENTITY_LOOKUP:
            return "entity match"
        if strategy in _RECENT_STRATEGIES:
            return "recent"
        return "matched"

    def part(name: str) -> float | None:
        return getattr(score_parts, name, None)

    signals = []
    if (part("vec_sim") or 0.0) > 0.0:
        signals.append("vector")
    if (part("fts_score") or 0.0) > 0.0:
        signals.append("keyword")
    if (part("entity_boost") or 1.0) > 1.0 or strategy == _ENTITY_LOOKUP:
        signals.append("entity")
    phrase = " + ".join(signals) + " match" if signals else "matched"

    qualifiers = []
    if part("rerank") is not None:
        qualifiers.append("reranked")
    if (part("temporal_boost") or 1.0) > 1.0:
        qualifiers.append("in time range")
    elif strategy in _RECENT_STRATEGIES:
        qualifiers.append("recent")
    # EC-19: present only while the tenant's verified boost is on.
    if (part("verification_boost") or 1.0) > 1.0:
        qualifiers.append("verified")
    if (part("recall_boost") or 1.0) > 1.0 and (recall_count or 0) >= OFTEN_RECALLED_MIN_COUNT:
        qualifiers.append("often recalled")
    for q in qualifiers:
        if len(phrase) + 2 + len(q) <= WHY_MAX_CHARS:
            return f"{phrase}, {q}"
    return phrase


def recall_evidence(
    memory: Any,
    *,
    score_parts: Any = None,
    strategy: str | None = None,
    injected: bool = False,
    detail: bool = False,
) -> RecallEvidence:
    """The ``evidence`` object of one search hit; ``detail`` adds the full set."""
    evidence = RecallEvidence(
        source=_source(memory),
        trace=getattr(memory, "trace_id", None),
        verification=getattr(memory, "verification_status", None) or DEFAULT_VERIFICATION_STATUS,
        why=why_phrase(
            score_parts,
            strategy=strategy,
            injected=injected,
            recall_count=getattr(memory, "recall_count", None),
        ),
    )
    if detail:
        evidence.observed_at = getattr(memory, "observed_at", None)
        evidence.run_id = getattr(memory, "run_id", None)
        evidence.tool_call_id = getattr(memory, "tool_call_id", None)
        evidence.source_uri = getattr(memory, "source_uri", None)
        evidence.verified_by = getattr(memory, "verified_by", None)
        evidence.verified_at = getattr(memory, "verified_at", None)
        evidence.strategy = strategy
    return evidence


def to_search_hit(
    out: MemoryOut,
    *,
    strategy: str | None = None,
    detail: bool = False,
) -> SearchHitOut:
    """``out`` as a search hit, carrying its evidence."""
    return SearchHitOut(
        **{name: getattr(out, name) for name in MemoryOut.model_fields},
        evidence=recall_evidence(
            out,
            score_parts=out.score_parts,
            strategy=strategy,
            injected=out.injected,
            detail=detail,
        ),
    )
