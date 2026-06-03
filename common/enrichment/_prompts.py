"""Enrichment prompt templates — moved from
``core_api.services.memory_enrichment`` (CAURA-595).

The prompt is the source of truth for the LLM's output schema; tests
exercise it directly (see ``tests/services/test_memory_enrichment.py``)
and ``EnrichmentResult`` mirrors it field-for-field.

The ``memory_type`` vocabulary list and per-type bullets are rendered
from :data:`common.enrichment.constants.MEMORY_TYPE_DESCRIPTIONS` at
import time, so adding a type there propagates here automatically.
"""

from __future__ import annotations

from common.enrichment.constants import (
    MEMORY_TYPE_DESCRIPTIONS,
    MEMORY_TYPES,
    SERVER_RESERVED_MEMORY_TYPES,
)

# The auto-classifier is only ever offered the agent-writable types. The
# server-reserved types (insight/outcome/rule) are authored exclusively by
# internal flows (insights_service / evolve_service) with the type set
# explicitly, so they never travel through this prompt. Offering them here
# is what let agent writes leak into the insight space (CAURA-699).
_CLASSIFIABLE_TYPES = tuple(
    t for t in MEMORY_TYPES if t not in SERVER_RESERVED_MEMORY_TYPES
)
_TYPES_INLINE = ", ".join(f'"{name}"' for name in _CLASSIFIABLE_TYPES)
_TYPE_BULLETS = "\n".join(
    f"   - {name}: {MEMORY_TYPE_DESCRIPTIONS[name]}" for name in _CLASSIFIABLE_TYPES
)

ENRICHMENT_PROMPT = (
    """\
You are a memory classifier for a business agent memory system.

Analyze the following memory content and return a JSON object with these fields:

1. "memory_type": one of """
    + _TYPES_INLINE
    + "\n"
    + _TYPE_BULLETS
    + """

   Action vs episode (common confusion):
   - action — actor's own deed, first-person past tense (subject=agent).
   - episode — observed event tied to time/place (subject=event,
     often third-person).
   Pairs:
     "I deployed v2.3 to production"                 -> action
     "The v2.3 deployment succeeded at 14:00"        -> episode
     "Merged the auth refactor PR"                   -> action
     "Production outage between 14:00 and 14:30"     -> episode

2. "weight": float 0.0-1.0 indicating importance
   - 0.9-1.0: critical decisions, key facts with evidence, high-impact events
   - 0.7-0.8: solid facts, meaningful events, clear preferences
   - 0.5-0.6: routine observations, minor events, uncertain information
   - 0.3-0.4: trivial, speculative, or low-confidence information

3. "title": short label (max 80 chars) summarizing the memory for display in lists

4. "summary": 1-2 sentence condensed version capturing the key information

5. "tags": array of 1-5 lowercase keyword tags for search and filtering
   - Use SINGULAR form: "meeting" not "meetings", "decision" not "decisions",
     "deployment" not "deployments". Stable singular keys join cleanly across memories.
   - Multi-word tags use HYPHEN separator (kebab-case): "code-review" not "code review"
     or "code_review" or "code reviews". Example tag set:
     ["deployment", "post-mortem", "decision", "team-meeting"].
   - At most 5 tags. Fewer is better — pick the highest-signal terms.

6. "status": one of "active", "pending", "confirmed" (optional, default "active")
   - active: default for most memories — current and valid
   - pending: for tasks/plans/commitments not yet confirmed or started
   - confirmed: for verified facts or completed commitments

7. "ts_valid_start": ISO 8601 datetime string (optional, null if not applicable)
   - The earliest time this memory is valid/relevant
   - Extract from phrases like "starting March 1", "from next Monday", "after the meeting"
   - For events/meetings: the event start time
   - Today's date is {today}

8. "ts_valid_end": ISO 8601 datetime string (optional, null if not applicable)
   - The latest time this memory is valid/relevant
   - Set ONLY when the content explicitly bounds the validity interval:
       * deadlines ("deadline March 30", "by Friday", "due tomorrow")
       * explicit end dates ("until end of Q1", "expires 2024-06-01", "contract runs through December")
       * time-limited facts where the content names the end ("subscription until Jan 2024")
   - DO NOT set ts_valid_end for memory_type "episode". Episodes are things that
     happened; they do not expire. The event is a permanent historical fact
     regardless of when the query is asked.
   - DO NOT infer ts_valid_end from relative time modifiers on the event itself
     like "this month", "last week", "yesterday". Those describe when the event
     happened (ts_valid_start territory), not how long the memory stays valid.
   - When in doubt, leave ts_valid_end as null. A missing end date is a feature,
     not an omission — it means "no known expiry".

9. "contains_pii": boolean (default false)
   - true if the content contains personally identifiable information
   - PII includes: email addresses, phone numbers, physical addresses, SSN/ID numbers, credit card numbers, dates of birth, full names paired with sensitive data
   - Do NOT flag generic first names, job titles, or company names alone

10. "pii_types": array of strings (optional, empty if no PII)
    - Types of PII detected, e.g. ["email", "phone", "address", "ssn", "credit_card", "date_of_birth"]

11. "retrieval_hint": short clause (max ~15 words, may be empty) capturing the
    memory's SEMANTIC ESSENCE in vocabulary a reader would use when asking
    about it LATER. Used to augment the embedding so queries that reference
    the significance/category of the memory can find it, not just ones that
    share surface vocabulary with the content.
    - Focus on the WHY-THIS-IS-NOTEWORTHY: milestones, decisions, changes,
      categories, roles, events — not a restatement of the content.
    - Examples:
      content "I signed a contract with my first client today"
        → "business milestone: signed first client, first paying customer"
      content "I learned about Petra at a History Museum lecture this month"
        → "museum visit, History Museum lecture, learning about Petra"
      content "We decided to go with Postgres over MongoDB"
        → "database technology decision: chose Postgres over MongoDB"
      content "Ran the nightly deploy at 03:00"
        → "deployment event, nightly release"
    - Leave as an empty string "" if the content is already highly query-aligned
      (common nouns + verbs of the thing itself) and no extra framing helps.

12. "atomic_facts": OPTIONAL — null in almost all cases. Populate only when
    the content carries 2+ DISTINCT atomic claims that would be searched by
    DIFFERENT query vocabulary. Each entry becomes its own child memory with
    its own embedding.
    - Rule of thumb: two concepts that are semantically UNRELATED and would
      be retrieved by disjoint queries.
      Examples of multi-fact content:
        "I'm looking for gift ideas for my sister-in-law… by the way, my friend
         Rachel got engaged last month on May 15th"
         → 2 facts: (gift planning for sister-in-law) + (Rachel engagement date)
        "Our anniversary is July 22. Also, the kitchen faucet started leaking."
         → 2 facts: (anniversary date) + (kitchen faucet leak)
    - DO NOT fan out single-topic content that just has multiple sentences
      about the same subject (e.g. several details about one project).
    - Each fact object has:
        "content"        : self-contained claim (include names, dates, values)
        "suggested_type" : same vocabulary as field 1
        "retrieval_hint" : same guidance as field 11 — short, query-aligned
    - When in doubt, leave atomic_facts as null.

Return ONLY valid JSON (no markdown fences):
{{"memory_type": "...", "weight": 0.0, "title": "...", "summary": "...", "tags": ["..."], "status": "active", "ts_valid_start": null, "ts_valid_end": null, "contains_pii": false, "pii_types": [], "retrieval_hint": "", "atomic_facts": null}}

Content:
{content}
"""
)
