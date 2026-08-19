"""Enrichment constants — moved from ``core_api.constants`` (CAURA-595).

The MEMORY_TYPES / MEMORY_STATUSES tuples and ``DEFAULT_MEMORY_TYPE`` /
``DEFAULT_MEMORY_WEIGHT`` defaults are read by the enrichment service
to validate LLM output. core-api's ``constants.py`` keeps re-exports
for back-compat — adding a new value here should be paired with the
matching pattern + UI updates in core-api.
"""

from __future__ import annotations

from enum import Enum


class MemoryType(str, Enum):
    """Typed enum for the memory-type vocabulary.

    Inheriting from ``str`` keeps full backward compatibility with the
    string-based call sites: ``MemoryType.FACT == "fact"`` is True,
    dict lookup with the enum hashes the same as the literal, JSON
    serialisation emits the bare string, and SQLAlchemy reads of a
    ``Text`` column coerce cleanly via Pydantic.
    """

    FACT = "fact"
    EPISODE = "episode"
    DECISION = "decision"
    PREFERENCE = "preference"
    TASK = "task"
    SEMANTIC = "semantic"
    INTENTION = "intention"
    PLAN = "plan"
    COMMITMENT = "commitment"
    ACTION = "action"
    OUTCOME = "outcome"
    CANCELLATION = "cancellation"
    RULE = "rule"
    INSIGHT = "insight"


# LLM-facing description for each ``MemoryType``. Kept str-keyed so a
# direct lookup with either a literal string ("fact") or the enum member
# (``MemoryType.FACT``, which IS a string) both succeed. Enrichment
# prompt rendering and DX surfaces (OpenAPI descriptions, console
# tooltips) read from here.
MEMORY_TYPE_DESCRIPTIONS: dict[str, str] = {
    "fact": (
        "durable knowledge, statements of truth, technical details, "
        "reference material, taxonomies, API/schema documentation. "
        "Anything that is a stable statement of what IS. Includes "
        "agent-authored documentation and technical analyses."
    ),
    "episode": (
        "events that happened — deployments, meetings, incidents, "
        "third-person observations of things occurring. "
        "NOT first-person self-reports of work the agent completed "
        "(those are action). NOT durable technical reference or "
        "documentation (those are fact). NOT style/aesthetic guidance "
        "or preferences (those are preference)."
    ),
    "decision": (
        "the act of deliberating and selecting a choice — NOT the execution "
        "of that choice. A decision requires at least one visible marker of "
        "deliberation: (a) reasoning (\"because…\", \"for scale\"), "
        "(b) comparison between alternatives (\"X over Y\", \"instead of Z\"), "
        "or (c) group/meeting conclusion framing (\"the team concluded\", "
        "\"we agreed after discussion\"). Look for: \"decided to X because Y\", "
        "\"chose X over Y\", \"going with A instead of B\", \"settled on X after "
        "considering Y\", \"the team concluded X\". If the content is just a "
        "deed being executed (\"approved X\", \"paused Y\") without any visible "
        "deliberation, it is action, not decision."
    ),
    "preference": (
        "user/org preferences, likes, dislikes, style choices, aesthetic "
        "guidance, aspirational orientations. "
        'Look for: "prefers", "likes", "would rather", "we care about", '
        '"our style is".'
    ),
    "task": (
        "work items, assignments, standing instructions, things to do — "
        "NOT YET DONE (work planned, pending, or delegated to someone). "
        'Look for: imperative or delegating phrasing ("do X", "please '
        'handle Y", "from now on Z"), or a work item written up but not '
        "yet performed."
    ),
    "semantic": (
        "DEPRECATED (CAURA-701): semantic content is now classified as "
        "'fact'. This entry is retained for read-compat with historical "
        "rows; the classifier no longer emits this type."
    ),
    "intention": (
        "DEPRECATED (CAURA-717): intention content is now folded into "
        "'plan' (structured goals / long-range targets) or the appropriate "
        "adjacent type. This entry is retained for read-compat with "
        "historical rows; the classifier no longer emits this type."
    ),
    "plan": (
        "structured sequences of steps to achieve a goal, prioritized "
        "checklists, week-by-week / phase-by-phase breakdowns, roadmaps, "
        "and long-range organizational goals or targets (the endpoint a "
        "roadmap points at, even when the steps aren't spelled out). "
        "Any content whose shape is \"here's what we're heading toward, "
        "or the ordered set of things we'll do.\""
    ),
    "commitment": (
        "DEPRECATED (CAURA-717): commitment content is now folded into "
        "the appropriate deed/pending-work type — a confirmed promise is "
        "'action', a standing instruction is 'task', an approved config is "
        "either 'action' or 'decision' depending on visible deliberation. "
        "This entry is retained for read-compat with historical rows; the "
        "classifier no longer emits this type."
    ),
    "action": (
        "a DEED the actor completed (or is completing). First-person "
        'past-tense verbs like "deployed", "merged", "created", "sent", '
        '"filed", "completed", "confirmed", "acknowledged", "paused", '
        '"approved", "signed off", "rejected", "cancelled" (as the deed '
        "itself) with an agentic subject. The DEED being described "
        "determines this, NOT who authored the content. If the content "
        'records the execution of a choice ("approved X", "paused Y", '
        '"cancelled Z") without visible deliberation or reasoning, it is '
        'action, not decision. Distinct from "task" (work NOT YET done) '
        'and "episode" (observed event).'
    ),
    "outcome": (
        "results of actions, tasks, or plans. Server-generated by "
        "caura_evolve when an agent reports an outcome — agents should "
        "NOT set memory_type='outcome' explicitly; use the evolve tool or "
        "leave the type to auto-classification."
    ),
    "cancellation": (
        "DEPRECATED (CAURA-717): cancellation content is now folded into "
        "'action' (the cancellation-deed itself, e.g. \"Cancelled the beta "
        "rollout\") or 'decision' (a documented change of direction with "
        "visible reasoning). This entry is retained for read-compat with "
        "historical rows; the classifier no longer emits this type."
    ),
    "rule": (
        "prescriptive directive, policy, or constraint. "
        'Look for: "always", "never", "must", "do not", "whenever", '
        '"policy", "guideline". Server-generated by caura_evolve when a '
        "failure outcome yields a synthesised rule — agents should NOT "
        "set memory_type='rule' explicitly; author rules via "
        "caura_keystones_set, or leave the type to auto-classification."
    ),
    "insight": (
        "novel finding, learned lesson, or pattern observed across "
        "memories. Server-generated by caura_insights — agents should "
        "NOT set memory_type='insight' explicitly; persist reflections "
        "as type 'fact' (durable knowledge) instead, and let "
        "caura_insights crystallise patterns over your corpus."
    ),
}

MEMORY_TYPES = tuple(t.value for t in MemoryType)

# C3/C8 — Memory types the SERVER generates internally and that agents
# MUST NOT supply explicitly on the write boundary. ``outcome`` and
# ``rule`` come out of evolve_service; ``insight`` comes out of
# insights_service. Each is still a valid value at the storage layer
# (the schema enum is unchanged) — only the route + MCP boundaries
# reject explicit agent-supplied values.
#
# CAURA-699 — the LLM auto-classifier must NOT mint these from agent
# content either: the enrichment prompt omits them from the offered
# vocabulary (see ``common.enrichment._prompts``) and ``_validate_enrichment``
# demotes any reserved type that slips through to ``DEFAULT_MEMORY_TYPE``.
# The reserved types only ever reach storage via the internal flows above,
# which set ``memory_type`` explicitly and bypass the classifier path.
SERVER_RESERVED_MEMORY_TYPES: frozenset[str] = frozenset({"outcome", "rule", "insight"})

# CAURA-701 — Memory types kept in the enum for read-compatibility with
# historical rows, but no longer offered to the LLM classifier because
# the taxonomy analysis showed they were indistinguishable from another
# type in practice. The write-time enrichment prompt filters these out
# (see ``common.enrichment._prompts``); ``_validate_enrichment`` demotes
# any deprecated value the LLM emits to ``DEFAULT_MEMORY_TYPE`` so the
# stored label matches the intended merger.
#
# ``semantic`` — 84% of prod-semantic rows were re-classified as ``fact``
# by an independent evaluator (Opus 4.8) and every remaining case had
# ``fact`` and ``semantic`` flagged as interchangeable. Merged into
# ``fact`` at write-time; historical rows keep their ``semantic`` label.
#
# CAURA-717 — ``intention``, ``commitment``, and ``cancellation`` folded
# into the appropriate adjacent types after a full 5,251-row Opus 4.8
# labeling of the eToro pool with the V2.2 taxonomy showed the three
# together accounted for <2% of the corpus and their content already
# fit cleanly into 'plan'/'preference' (intention), 'action'/'task'/
# 'decision' (commitment), and 'action'/'decision' (cancellation). Kept
# in the enum for read-compat with historical rows; the classifier no
# longer offers them and ``_validate_enrichment`` demotes any residual
# LLM-emitted value to ``DEFAULT_MEMORY_TYPE`` (``fact``) at the write
# boundary. Note the demotion default is a safety net — the retrained
# V2.2 prompt is the primary mechanism that steers each case into its
# semantically correct successor type.
CLASSIFIER_DEPRECATED_MEMORY_TYPES: frozenset[str] = frozenset(
    {"semantic", "intention", "commitment", "cancellation"}
)

# Import-time guard: enum and description dict must agree, otherwise
# the prompt renderer will silently emit a bullet without a heading or
# a heading without a description. Catching here gives a loud import
# error rather than mysterious LLM behaviour.
assert set(MEMORY_TYPES) == set(MEMORY_TYPE_DESCRIPTIONS.keys()), (
    "MemoryType enum and MEMORY_TYPE_DESCRIPTIONS keys are out of sync: "
    f"enum-only={set(MEMORY_TYPES) - set(MEMORY_TYPE_DESCRIPTIONS)}, "
    f"dict-only={set(MEMORY_TYPE_DESCRIPTIONS) - set(MEMORY_TYPES)}"
)

# C3/C8 — every reserved type must exist in the enum / description set;
# otherwise the boundary check below is referencing a phantom slug.
assert SERVER_RESERVED_MEMORY_TYPES.issubset(set(MEMORY_TYPES)), (
    "SERVER_RESERVED_MEMORY_TYPES references unknown memory types: "
    f"{SERVER_RESERVED_MEMORY_TYPES - set(MEMORY_TYPES)}"
)

# CAURA-701 — deprecated types must exist in the enum (that's the whole
# point — read-compat); they must NOT overlap with reserved types because
# the classifier filter subtracts both sets independently.
assert CLASSIFIER_DEPRECATED_MEMORY_TYPES.issubset(set(MEMORY_TYPES)), (
    "CLASSIFIER_DEPRECATED_MEMORY_TYPES references unknown memory types: "
    f"{CLASSIFIER_DEPRECATED_MEMORY_TYPES - set(MEMORY_TYPES)}"
)
assert not (CLASSIFIER_DEPRECATED_MEMORY_TYPES & SERVER_RESERVED_MEMORY_TYPES), (
    "Deprecated and reserved sets must not overlap: "
    f"{CLASSIFIER_DEPRECATED_MEMORY_TYPES & SERVER_RESERVED_MEMORY_TYPES}"
)

DEFAULT_MEMORY_TYPE = MemoryType.FACT.value

# Status vocabulary — the enrichment LLM may downgrade a write to e.g.
# ``cancelled`` or ``conflicted`` based on the prompt's classification
# rules.
MEMORY_STATUSES = (
    "active",
    "pending",
    "confirmed",
    "cancelled",
    "outdated",
    "conflicted",
    "archived",
    "deleted",
)

# Default weight assigned to memories whose enrichment didn't return
# a salience score. 0.5 = neutral; the recall ranker uses this as the
# tie-breaking baseline.
DEFAULT_MEMORY_WEIGHT = 0.5
