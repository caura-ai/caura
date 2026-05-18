"""EmitMemoryTriple — populate (subject_entity_id, predicate, object_value)
from the incoming request using deterministic heuristics so the RDF
contradiction path in ``contradiction_detector.py`` can fire instead of
falling through to the LLM (CAURA-123).

Contract:
- This step never raises on a parse miss. Any failure → SKIPPED, with
  the reason logged at DEBUG. The downstream LLM contradiction path
  remains unchanged and continues to handle anything we skip.
- This step never overwrites caller-supplied triple fields.
- This step issues no LLM calls and no DB writes; it mutates only
  ``ctx.data["input"]`` (the in-memory MemoryCreate) so that
  ``WriteMemoryRow`` (line 60-62) persists the populated columns.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Final

from common.constants import SINGLE_VALUE_PREDICATES
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


# Phrase → predicate table. Only entries whose predicate appears in
# ``SINGLE_VALUE_PREDICATES`` will ever fire; the parity test in
# ``tests/test_emit_memory_triple.py::TestAllowlistParity`` enforces this invariant.
#
# Patterns are case-insensitive, deliberately narrow, and applied as
# ``re.search`` so they can match within longer sentences. The matched
# phrase is the split point: text before is ignored (we already have
# the subject from entity_links), text after is normalized as the
# object value.
_PHRASE_TO_PREDICATE: Final[list[tuple[re.Pattern[str], str]]] = [
    (re.compile(r"\blives\s+in\b", re.IGNORECASE), "lives_in"),
    (re.compile(r"\bis\s+located\s+in\b", re.IGNORECASE), "located_in"),
    (re.compile(r"\bis\s+based\s+in\b", re.IGNORECASE), "based_in"),
    (re.compile(r"\bis\s+headquartered\s+in\b", re.IGNORECASE), "headquartered_in"),
    (re.compile(r"\breports\s+to\b", re.IGNORECASE), "reports_to"),
    (re.compile(r"\bis\s+managed\s+by\b", re.IGNORECASE), "managed_by"),
    (re.compile(r"\bis\s+owned\s+by\b", re.IGNORECASE), "owned_by"),
    (re.compile(r"\bis\s+assigned\s+to\b", re.IGNORECASE), "assigned_to"),
    (re.compile(r"\bis\s+employed\s+by\b", re.IGNORECASE), "employed_by"),
    (re.compile(r"\bis\s+the\s+ceo\s+of\b", re.IGNORECASE), "ceo_of"),
    (re.compile(r"\bis\s+the\s+cto\s+of\b", re.IGNORECASE), "cto_of"),
    (re.compile(r"\bis\s+the\s+cfo\s+of\b", re.IGNORECASE), "cfo_of"),
    (re.compile(r"\bis\s+renamed\s+to\b", re.IGNORECASE), "renamed_to"),
]

_LEADING_ARTICLES = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def _normalize_object(raw: str) -> str | None:
    """Trim, strip trailing terminal punctuation, drop leading article. Empty → None."""
    s = raw.strip().rstrip(".!?,;")
    s = _LEADING_ARTICLES.sub("", s).strip()
    return s.lower() if s else None


class EmitMemoryTriple:
    @property
    def name(self) -> str:
        return "emit_memory_triple"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        t0 = time.perf_counter()
        tenant_config = ctx.tenant_config
        if tenant_config is not None and not getattr(tenant_config, "triple_emission_enabled", True):
            return StepResult(outcome=StepOutcome.SKIPPED, detail={"reason": "flag_off"})

        data = ctx.data["input"]

        # Never overwrite caller-supplied triples — they may come from
        # an upstream system that already knows the canonical predicate.
        # Any partial supply (e.g., only subject_entity_id) is also
        # treated as "caller is in control" — otherwise our heuristic
        # would silently overwrite the supplied field with a different
        # value derived from entity_links.
        if data.subject_entity_id is not None or data.predicate is not None or data.object_value is not None:
            return StepResult(outcome=StepOutcome.SKIPPED, detail={"reason": "already_set"})

        try:
            subject_links = [
                link for link in (data.entity_links or []) if (link.role or "").lower() == "subject"
            ]
            if len(subject_links) != 1:
                return StepResult(
                    outcome=StepOutcome.SKIPPED,
                    detail={"reason": "no_subject" if not subject_links else "ambiguous_subject"},
                )
            subject_entity_id = subject_links[0].entity_id

            content = data.content or ""
            matches: list[tuple[re.Match[str], str]] = []
            for pat, predicate in _PHRASE_TO_PREDICATE:
                m = pat.search(content)
                if m:
                    matches.append((m, predicate))
            if len(matches) != 1:
                return StepResult(
                    outcome=StepOutcome.SKIPPED,
                    detail={"reason": "no_predicate_match" if not matches else "ambiguous_predicate"},
                )
            match, predicate = matches[0]

            # Defensive: the allowlist parity test guards this set, but
            # belt-and-braces — never emit a predicate the detector
            # won't recognize.
            if predicate not in SINGLE_VALUE_PREDICATES:
                return StepResult(
                    outcome=StepOutcome.SKIPPED, detail={"reason": "predicate_not_in_allowlist"}
                )

            # Bound the object to the current sentence — without this,
            # a follow-up clause like "Ran lives in NYC. He also …"
            # would swallow the rest of the content into object_value.
            tail = content[match.end() :]
            # Match real sentence boundaries — `!`, `?`, or `.` that
            # is (a) preceded by 3+ word characters and (b) followed
            # by whitespace+capital or end-of-string. The lookbehind
            # filters out short abbreviations (Dr., Sr., Mr., Inc.)
            # that would otherwise be mistaken for sentence endings
            # when the next word is capitalised.
            sentence_end = re.search(r"[!?]|(?<=\w{3})\.(?=\s+[A-Z]|\s*$)", tail)
            object_value = _normalize_object(tail[: sentence_end.start()] if sentence_end else tail)
            if object_value is None:
                return StepResult(outcome=StepOutcome.SKIPPED, detail={"reason": "object_unparseable"})

            data.subject_entity_id = subject_entity_id
            data.predicate = predicate
            data.object_value = object_value

            emit_ms = round((time.perf_counter() - t0) * 1000, 1)
            fields = ctx.data.get("memory_fields")
            if isinstance(fields, dict):
                metadata = fields.get("metadata")
                if isinstance(metadata, dict):
                    metadata["triple_emission_ms"] = emit_ms
            logger.info(
                "emit_triple populated subject=%s predicate=%s ms=%s",
                str(subject_entity_id)[:8],
                predicate,
                emit_ms,
            )
            return None
        except Exception as exc:
            # Contract: never break the write pipeline. Skip + log; the
            # LLM contradiction path remains the safety net.
            logger.warning("emit_triple skipped due to unexpected error: %s", exc, exc_info=True)
            return StepResult(outcome=StepOutcome.SKIPPED, detail={"reason": "error"})
