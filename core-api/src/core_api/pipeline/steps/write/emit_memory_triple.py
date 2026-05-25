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
# CAURA-126 — phrase coverage expansion. Patterns are organised by
# cluster; new clusters added 2026-05 to close the dead-code gap that
# left ~90% of ``SINGLE_VALUE_PREDICATES`` without natural-language
# coverage (the "release_date" bug report).
#
# Authoring rules to keep cross-cluster ambiguity low:
#  - Prefer phrases that name the field explicitly ("has release
#    date") over verbal forms ("released on") that grab too much
#    context after the match.
#  - One canonical predicate per semantic concept — emit the noun
#    form (``release_date``) even when the allowlist also has the
#    verbal form (``released_as``). Otherwise two memories carrying
#    the same fact in different prose split across predicate columns
#    and RDF detection silently misses them.
#  - Use ``(?:current\s+|latest\s+|running\s+)?`` prefixes to fold
#    qualifier variants into one row rather than adding separate
#    patterns that would overlap.
#  - Combine ``(?:has|has\s+a)\s+X\s+of`` and ``X\s+is`` shapes per
#    predicate so most natural orderings are covered.
#
# Adding a phrase whose target predicate is NOT in
# ``SINGLE_VALUE_PREDICATES`` is enforced impossible by the parity
# test in tests/test_emit_memory_triple.py::TestAllowlistParity.
_PHRASE_TO_PREDICATE: Final[list[tuple[re.Pattern[str], str]]] = [
    # -- Original CAURA-123 cluster: location & singular roles --
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
    # -- CAURA-126 Tier 1: dates & temporal deadlines --
    (re.compile(r"\bhas\s+release\s+date\b", re.IGNORECASE), "release_date"),
    (re.compile(r"\brelease\s+date\s+is\b", re.IGNORECASE), "release_date"),
    (re.compile(r"\bhas\s+launch\s+date\b", re.IGNORECASE), "launch_date"),
    (re.compile(r"\blaunch\s+date\s+is\b", re.IGNORECASE), "launch_date"),
    (re.compile(r"\bhas\s+go[- ]live\s+date\b", re.IGNORECASE), "go_live_date"),
    (re.compile(r"\bgo[- ]live\s+date\s+is\b", re.IGNORECASE), "go_live_date"),
    (re.compile(r"\bhas\s+target\s+date\b", re.IGNORECASE), "target_date"),
    (re.compile(r"\btarget\s+date\s+is\b", re.IGNORECASE), "target_date"),
    (re.compile(r"\bhas\s+due\s+date\b", re.IGNORECASE), "due_date"),
    (re.compile(r"\bdue\s+date\s+is\b", re.IGNORECASE), "due_date"),
    (re.compile(r"\bis\s+due\s+on\b", re.IGNORECASE), "due_date"),
    (re.compile(r"\bis\s+due\s+by\b", re.IGNORECASE), "due_date"),
    (re.compile(r"\bhas\s+start\s+date\b", re.IGNORECASE), "start_date"),
    (re.compile(r"\bstart\s+date\s+is\b", re.IGNORECASE), "start_date"),
    (re.compile(r"\bhas\s+end\s+date\b", re.IGNORECASE), "end_date"),
    (re.compile(r"\bend\s+date\s+is\b", re.IGNORECASE), "end_date"),
    (re.compile(r"\bexpires\s+on\b", re.IGNORECASE), "expiry_date"),
    (re.compile(r"\bexpiry\s+date\s+is\b", re.IGNORECASE), "expiry_date"),
    (re.compile(r"\bexpiration\s+date\s+is\b", re.IGNORECASE), "expiry_date"),
    (re.compile(r"\bhas\s+deadline\b", re.IGNORECASE), "deadline"),
    (re.compile(r"\bdeadline\s+is\b", re.IGNORECASE), "deadline"),
    (re.compile(r"\beta\s+is\b", re.IGNORECASE), "eta"),
    (re.compile(r"\bhas\s+eta\s+of\b", re.IGNORECASE), "eta"),
    (re.compile(r"\bis\s+scheduled\s+for\b", re.IGNORECASE), "scheduled_for"),
    (re.compile(r"\bis\s+rescheduled\s+to\b", re.IGNORECASE), "rescheduled_to"),
    (re.compile(r"\bwas\s+born\s+on\b", re.IGNORECASE), "birthdate"),
    (re.compile(r"\bdate\s+of\s+birth\s+is\b", re.IGNORECASE), "birthdate"),
    # -- CAURA-126 Tier 2: status, state, role, priority, project --
    (re.compile(r"\b(?:current\s+)?status\s+is\b", re.IGNORECASE), "status"),
    (re.compile(r"\bis\s+in\s+(?:the\s+)?status\b", re.IGNORECASE), "status"),
    (re.compile(r"\b(?:current\s+)?phase\s+is\b", re.IGNORECASE), "phase"),
    (re.compile(r"\bis\s+in\s+(?:the\s+)?phase\b", re.IGNORECASE), "phase"),
    (re.compile(r"\b(?:current\s+)?state\s+is\b", re.IGNORECASE), "state"),
    (re.compile(r"\b(?:current\s+)?mode\s+is\b", re.IGNORECASE), "mode"),
    (re.compile(r"\bis\s+in\s+(?:the\s+)?mode\b", re.IGNORECASE), "mode"),
    (re.compile(r"\b(?:current\s+)?priority\s+is\b", re.IGNORECASE), "priority"),
    (re.compile(r"\bhas\s+priority\b", re.IGNORECASE), "priority"),
    (re.compile(r"\bseverity\s+is\b", re.IGNORECASE), "severity"),
    (re.compile(r"\bhas\s+severity\b", re.IGNORECASE), "severity"),
    (re.compile(r"\b(?:current\s+)?role\s+is\b", re.IGNORECASE), "role"),
    (re.compile(r"\bhas\s+role\b", re.IGNORECASE), "role"),
    (re.compile(r"\b(?:job\s+)?title\s+is\b", re.IGNORECASE), "title"),
    (re.compile(r"\bhas\s+title\b", re.IGNORECASE), "title"),
    (re.compile(r"\bsprint\s+is\b", re.IGNORECASE), "sprint"),
    (re.compile(r"\bis\s+in\s+sprint\b", re.IGNORECASE), "sprint"),
    (re.compile(r"\bmilestone\s+is\b", re.IGNORECASE), "milestone"),
    (re.compile(r"\bhas\s+milestone\b", re.IGNORECASE), "milestone"),
    (re.compile(r"\bepic\s+is\b", re.IGNORECASE), "epic"),
    (re.compile(r"\bis\s+in\s+epic\b", re.IGNORECASE), "epic"),
    # -- CAURA-126 Tier 3: money, metrics, scores, versioning --
    (re.compile(r"\bis\s+priced\s+at\b", re.IGNORECASE), "price"),
    (re.compile(r"\bprice\s+is\b", re.IGNORECASE), "price"),
    (re.compile(r"\bhas\s+(?:a\s+)?price\s+of\b", re.IGNORECASE), "price"),
    (re.compile(r"\bcost\s+is\b", re.IGNORECASE), "cost"),
    (re.compile(r"\bhas\s+(?:a\s+)?cost\s+of\b", re.IGNORECASE), "cost"),
    (re.compile(r"\bsalary\s+is\b", re.IGNORECASE), "salary"),
    (re.compile(r"\bhas\s+(?:a\s+)?salary\s+of\b", re.IGNORECASE), "salary"),
    (re.compile(r"\bbudget\s+is\b", re.IGNORECASE), "budget"),
    (re.compile(r"\bhas\s+(?:a\s+)?budget\s+of\b", re.IGNORECASE), "budget"),
    (re.compile(r"\b(?:annual\s+|monthly\s+)?revenue\s+is\b", re.IGNORECASE), "revenue"),
    (re.compile(r"\bhas\s+revenue\s+of\b", re.IGNORECASE), "revenue"),
    (re.compile(r"\bis\s+valued\s+at\b", re.IGNORECASE), "valuation"),
    (re.compile(r"\bvaluation\s+is\b", re.IGNORECASE), "valuation"),
    (re.compile(r"\bhas\s+valuation\s+of\b", re.IGNORECASE), "valuation"),
    (re.compile(r"\b(?:total\s+)?funding\s+is\b", re.IGNORECASE), "funding"),
    # ``score`` excludes compound ``X_score`` qualifiers so that
    # "confidence score is 0.9" routes to the more specific
    # ``confidence_score`` predicate below rather than colliding with
    # plain ``score`` (which would mark both single matches as
    # ambiguous and skip).
    (
        re.compile(
            r"(?<!confidence\s)(?<!sentiment\s)(?<!risk\s)"
            r"(?<!quality\s)(?<!health\s)(?<!potential\s)(?<!f1\s)"
            r"\b(?:overall\s+|total\s+)?score\s+is\b",
            re.IGNORECASE,
        ),
        "score",
    ),
    (
        re.compile(
            r"(?<!confidence\s)(?<!sentiment\s)(?<!risk\s)"
            r"(?<!quality\s)(?<!health\s)(?<!potential\s)(?<!f1\s)"
            r"\bhas\s+score\s+of\b",
            re.IGNORECASE,
        ),
        "score",
    ),
    (re.compile(r"\brating\s+is\b", re.IGNORECASE), "rating"),
    (re.compile(r"\bhas\s+rating\s+of\b", re.IGNORECASE), "rating"),
    (re.compile(r"\brank\s+is\b", re.IGNORECASE), "rank"),
    (re.compile(r"\bis\s+ranked\b", re.IGNORECASE), "rank"),
    # The negative lookahead documents intent — ``confidence is`` must
    # not fire when the immediate continuation is ``score is`` (the
    # compound predicate). The plain pattern already wouldn't match
    # "confidence score is" (no ``is`` directly after ``confidence``),
    # but pinning the constraint explicitly catches a future edit
    # that loosens ``\s+`` to ``\s*`` or similar.
    (re.compile(r"\bconfidence\s+(?!score\s+is\b)is\b", re.IGNORECASE), "confidence"),
    (re.compile(r"\bconfidence\s+score\s+is\b", re.IGNORECASE), "confidence_score"),
    # Compound ``X_score`` predicates — sibling rows to the bare
    # ``score`` patterns above, all in ``SINGLE_VALUE_PREDICATES``.
    # The bare ``score`` patterns exclude these prefixes via
    # negative lookbehinds; that gate is only meaningful if each
    # compound has its own row to take the match.
    (re.compile(r"\bpotential\s+score\s+is\b", re.IGNORECASE), "potential_score"),
    (re.compile(r"\brisk\s+score\s+is\b", re.IGNORECASE), "risk_score"),
    (re.compile(r"\bquality\s+score\s+is\b", re.IGNORECASE), "quality_score"),
    (re.compile(r"\bhealth\s+score\s+is\b", re.IGNORECASE), "health_score"),
    (re.compile(r"\bsentiment\s+score\s+is\b", re.IGNORECASE), "sentiment_score"),
    (re.compile(r"\bf1\s+score\s+is\b", re.IGNORECASE), "f1_score"),
    (re.compile(r"\b(?:current\s+|latest\s+|running\s+)?version\s+is\b", re.IGNORECASE), "current_version"),
    (re.compile(r"\bis\s+on\s+version\b", re.IGNORECASE), "current_version"),
    # -- CAURA-126 Tier 4: infra, contact, hierarchy, license/plan --
    (re.compile(r"\bhostname\s+is\b", re.IGNORECASE), "hostname"),
    (re.compile(r"\bhas\s+hostname\b", re.IGNORECASE), "hostname"),
    (re.compile(r"\bcluster\s+is\b", re.IGNORECASE), "cluster"),
    (re.compile(r"\bis\s+in\s+cluster\b", re.IGNORECASE), "cluster"),
    (re.compile(r"\bnamespace\s+is\b", re.IGNORECASE), "namespace"),
    (re.compile(r"\bis\s+in\s+namespace\b", re.IGNORECASE), "namespace"),
    (re.compile(r"\bzone\s+is\b", re.IGNORECASE), "zone"),
    (re.compile(r"\bis\s+in\s+(?:availability\s+)?zone\b", re.IGNORECASE), "zone"),
    (re.compile(r"\bregion\s+is\b", re.IGNORECASE), "region"),
    (re.compile(r"\bis\s+in\s+(?:the\s+)?region\b", re.IGNORECASE), "region"),
    (re.compile(r"\bcountry\s+is\b", re.IGNORECASE), "country"),
    (re.compile(r"\bcity\s+is\b", re.IGNORECASE), "city"),
    (re.compile(r"\bemail\s+(?:address\s+)?is\b", re.IGNORECASE), "email"),
    (re.compile(r"\bhas\s+email\b", re.IGNORECASE), "email"),
    (re.compile(r"\bphone\s+(?:number\s+)?is\b", re.IGNORECASE), "phone"),
    (re.compile(r"\bhas\s+phone\b", re.IGNORECASE), "phone"),
    (re.compile(r"\bwebsite\s+is\b", re.IGNORECASE), "website"),
    (re.compile(r"\bhas\s+website\b", re.IGNORECASE), "website"),
    (re.compile(r"\bis\s+led\s+by\b", re.IGNORECASE), "led_by"),
    (re.compile(r"\bis\s+headed\s+by\b", re.IGNORECASE), "headed_by"),
    (re.compile(r"\bis\s+maintained\s+by\b", re.IGNORECASE), "maintained_by"),
    (re.compile(r"\bis\s+supervised\s+by\b", re.IGNORECASE), "supervised_by"),
    (re.compile(r"\bis\s+licensed\s+under\b", re.IGNORECASE), "license"),
    (re.compile(r"\blicense\s+is\b", re.IGNORECASE), "license"),
    (re.compile(r"\bis\s+on\s+(?:the\s+)?(?:subscription\s+)?plan\b", re.IGNORECASE), "subscription_plan"),
    (re.compile(r"\bsubscription\s+plan\s+is\b", re.IGNORECASE), "subscription_plan"),
    (re.compile(r"\btier\s+is\b", re.IGNORECASE), "tier"),
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

            # Normalise whitespace to single ASCII spaces. The score
            # lookbehinds (``(?<!confidence\s)`` etc.) are fixed-width
            # at one space character, so content like "confidence
            # \tscore is 0.9" (tab between words) would otherwise
            # bypass the lookbehind and incorrectly route to the
            # bare ``score`` predicate. Done once at the top so all
            # subsequent regex searches see canonical whitespace.
            content = re.sub(r"\s+", " ", data.content or "")
            matches: list[tuple[re.Match[str], str]] = []
            for pat, predicate in _PHRASE_TO_PREDICATE:
                m = pat.search(content)
                if m:
                    matches.append((m, predicate))
            # Ambiguity is on the *predicate*, not the raw number of
            # pattern hits. Two patterns mapping to the same canonical
            # predicate (e.g. ``\bhas\s+release\s+date\b`` and
            # ``\brelease\s+date\s+is\b`` both firing on
            # "has release date is 2027") should resolve to a single
            # emission — the predicate is unique, only the phrasing
            # overlaps. Splitting that into ``ambiguous_predicate``
            # used to silently drop the entire row.
            unique_predicates = {pred for _, pred in matches}
            if len(unique_predicates) != 1:
                return StepResult(
                    outcome=StepOutcome.SKIPPED,
                    detail={"reason": "no_predicate_match" if not matches else "ambiguous_predicate"},
                )
            # When the same predicate is hit by multiple phrase patterns
            # (e.g. ``\bhas\s+release\s+date\b`` and
            # ``\brelease\s+date\s+is\b`` both fire on "has release
            # date is 2027"), pick the match whose ``end()`` is
            # furthest right. That's the anchor closest to the actual
            # object — using ``matches[0]`` (table order) would leave
            # interstitial words like "is" in the tail and pollute
            # ``object_value``.
            match, predicate = max(matches, key=lambda m_p: m_p[0].end())

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
