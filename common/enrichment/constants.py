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
    "fact": "durable knowledge, statements of truth, technical details",
    "episode": "events that happened, deployments, meetings, incidents",
    "decision": (
        "choices made with reasoning, architecture decisions, approvals. "
        'Look for: "decided to", "chose", "going with", "agreed to", '
        '"approved", "selected", "opted for", "settled on"'
    ),
    "preference": "user/org preferences, likes, dislikes, style choices",
    "task": "work items, assignments, things to do",
    "semantic": "conceptual/definitional knowledge, taxonomy entries",
    "intention": "stated goals or aims not yet acted on",
    "plan": "structured sequences of steps to achieve a goal",
    "commitment": (
        "promises or obligations made to others. "
        'Look for: "committed to", "promised", "guaranteed", '
        '"agreed to deliver", "pledged"'
    ),
    "action": (
        "concrete step the actor (first-person / agent itself) took or is taking. "
        'Past-tense first-person framing: "I deployed", "filed", "merged", '
        '"sent". Distinct from "episode" (an observed event tied to time, '
        "regardless of who performed it)."
    ),
    "outcome": "results of actions, tasks, or plans",
    "cancellation": "explicit record that something was cancelled or abandoned",
    "rule": (
        "prescriptive directive, policy, or constraint. "
        'Look for: "always", "never", "must", "do not", "whenever", '
        '"policy", "guideline"'
    ),
    "insight": (
        "novel finding, learned lesson, or pattern observed across memories — "
        "persist results of reflection/analysis steps here so future runs build on them"
    ),
}

MEMORY_TYPES = tuple(t.value for t in MemoryType)

# Import-time guard: enum and description dict must agree, otherwise
# the prompt renderer will silently emit a bullet without a heading or
# a heading without a description. Catching here gives a loud import
# error rather than mysterious LLM behaviour.
assert set(MEMORY_TYPES) == set(MEMORY_TYPE_DESCRIPTIONS.keys()), (
    "MemoryType enum and MEMORY_TYPE_DESCRIPTIONS keys are out of sync: "
    f"enum-only={set(MEMORY_TYPES) - set(MEMORY_TYPE_DESCRIPTIONS)}, "
    f"dict-only={set(MEMORY_TYPE_DESCRIPTIONS) - set(MEMORY_TYPES)}"
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
