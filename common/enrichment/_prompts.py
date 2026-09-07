"""Enrichment prompt templates — moved from
``core_api.services.memory_enrichment`` (CAURA-595).

The prompt is the source of truth for the LLM's output schema; tests
exercise it directly (see ``tests/services/test_memory_enrichment.py``)
and ``EnrichmentResult`` mirrors it field-for-field.

The ``memory_type`` vocabulary list and per-type bullets are rendered
from :data:`common.enrichment.constants.MEMORY_TYPE_DESCRIPTIONS` at
import time, so adding a type there propagates here automatically.

CAURA-719 — ``tags`` and ``status`` are no longer asked of the LLM, so
``EnrichmentResult`` is deliberately WIDER than this prompt:

* ``tags`` — the platform had no reader. Nothing filters, queries, or
  full-text-indexes them (the FTS vector is ``title || content``, see
  migration 034), so 95% of rows carried generated tags that only ever
  travelled back out again. They stay a CALLER-owned key (C25
  ``CALLER_OWNABLE_KEYS``): a caller who supplies ``metadata["tags"]``
  keeps them untouched, and the validator below still normalises those.
* ``status`` — measured constant. All 48,374 rows of the eToro corpus
  carried ``active``; the classifier never once chose ``pending`` or
  ``confirmed``, so the field cost tokens to reproduce the schema
  default. It is a LIFECYCLE field owned by explicit setters:
  ``caura_manage``'s transition op, the contradiction detector
  (``outdated`` / ``conflicted``), the crystallizer (``archived``), and
  the delete path (``deleted``). Letting the classifier also assign it
  was a live hazard: ~11 query paths filter the literal string
  ``'active'`` rather than ``LIVE_MEMORY_STATUSES``, so a memory the LLM
  labelled ``confirmed`` went invisible to insights — observed in
  production and worked around by pinning ``status="active"`` in both
  ``doc_memory.py`` and ``insights_service.py``.

Both fields keep their schema entry, default, and validator so
historical rows, caller-supplied values, and the deferred worker's
patch path all continue to deserialise unchanged.

CAURA-720 — ``retrieval_hint`` joins them, and ``weight`` gains its
missing band:

* ``retrieval_hint`` (and the per-fact copy inside ``atomic_facts``) is
  no longer asked for. Its only purpose was to augment the embedding —
  writes embedded ``"[Retrieval hint]: …\\n\\n<content>"`` while queries
  embedded raw text — and CAURA-222 DISABLED that after the asymmetry
  capped recall: identical content↔query scored cosine ~0.69 instead of
  ~1.0 across dedup, entity-lookup, and search ranking. The
  ``compose_embedding_text`` helper was deleted as dead code, and both
  the hot path and the background re-embed now embed raw ``content``.
  The per-fact hint was equally inert: ``memory_service``'s fan-out
  embeds raw ``fact_content`` for the same CAURA-222 reason.
  What remains reads it: ``scripts/backfill_embeddings.py`` selects on a
  non-empty ``metadata.retrieval_hint`` to find rows whose stored vector
  still holds the old prefixed text, and preserves the value as
  "auditability ground". That selector is unaffected — stored hints on
  historical rows are untouched, and rows written from here on simply
  stop matching it, which they should, since they were never prefixed.
  ``MemoryEnriched.retrieval_hint`` and the worker's routing entry stay
  for the same reason the two fields above do.
* ``weight`` — the field is declared ``0.0-1.0`` but the rubric only
  described ``0.3-1.0``, leaving 14.3% of the eToro corpus (``0.1`` at
  9.3%, ``0.2`` at 4.9%) in a range the prompt never defined. That range
  is not spare: FOUR consumers read ``weight < 0.3`` as "low salience"
  — insights patterns, insights stale, ``CRYSTALLIZER_STALE_MAX_WEIGHT``,
  and ``LIFECYCLE_STALE_ARCHIVE_WEIGHT`` — so the band is documented
  rather than clamped. Clamping the floor to 0.3 would make ``< 0.3``
  unsatisfiable and silently starve all four. The band is worded by its
  CONSEQUENCE (auto-archival) so the model's choice is informed by what
  the system does with the number.
  The JSON template's ``"weight": 0.0`` moves to ``0.5``: ``0.0`` sat
  outside every band the rubric described, and 32 rows landed on exactly
  that value — below even ``EVOLVE_WEIGHT_FLOOR`` (0.05), so evolve
  cannot have produced them. ``0.5`` is the mid-range neutral and sits
  inside a described band; that is the whole claim.
  Deliberately NOT claimed: that it matches "the" default weight. There
  are TWO, on two different paths, and they do not agree:
    * this path (the LLM answered) falls back to a hardcoded ``0.7`` —
      in ``_validate_enrichment`` and again as ``EnrichmentResult.weight``'s
      pydantic default;
    * ``DEFAULT_MEMORY_WEIGHT`` (0.5) applies only when NO enrichment
      value exists at all — ``merge_enrichment_fields`` / ``memory_service``
      reach for it when ``weight is None``, which #1207 records as
      ``weight_source="default"``.
  So the exemplar is not tied to either constant, and no test should pin
  it to one: a change to ``DEFAULT_MEMORY_WEIGHT`` must not drag the
  prompt with it. Unifying the 0.7 fallback onto ``DEFAULT_MEMORY_WEIGHT``
  would make the two paths agree, but that is a behaviour change to
  ranking (weight is 15% of base score) and belongs in its own PR.
"""

from __future__ import annotations

from common.enrichment.constants import (
    CLASSIFIER_DEPRECATED_MEMORY_TYPES,
    MEMORY_TYPE_DESCRIPTIONS,
    MEMORY_TYPES,
    SERVER_RESERVED_MEMORY_TYPES,
)

# The auto-classifier is only ever offered the agent-writable, non-deprecated
# types. The server-reserved types (insight/outcome/rule) are authored
# exclusively by internal flows (insights_service / evolve_service) with the
# type set explicitly, so they never travel through this prompt — offering
# them here is what let agent writes leak into the insight space (CAURA-699).
# The deprecated types (``semantic`` from CAURA-701; ``intention``,
# ``commitment``, and ``cancellation`` from CAURA-717) remain in the enum
# for read-compat with historical rows but are hidden from the LLM so it
# merges their content into the successor types (``fact``, ``plan``,
# ``action``, ``task``, or ``decision`` depending on the case; the
# ``_validate_enrichment`` demotion falls back to ``fact`` when the LLM
# emits a deprecated slug anyway).
_CLASSIFIABLE_TYPES = tuple(
    t
    for t in MEMORY_TYPES
    if t not in SERVER_RESERVED_MEMORY_TYPES
    and t not in CLASSIFIER_DEPRECATED_MEMORY_TYPES
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

   Action vs episode vs fact — resolving the most common confusion:
   - action = the ACTOR did a DEED. Verbs of doing (deployed, merged,
     sent, completed, created, filed, staged, confirmed, approved,
     paused, cancelled) + first-person or agentic subject.
   - episode = an EVENT happened (observed, third-person framing). Not
     tied to the actor as the doer.
   - fact = a stable STATE or knowledge — durable statements of what IS,
     including documentation of a system, API, or process.

   Action vs decision — the tiebreaker:
   - action = execution of the deed itself. Includes verbs like
     "approved", "paused", "signed off", "rejected", "cancelled" when
     the content is just the deed + its object.
   - decision = the deliberation and selection itself. Requires at least
     one visible marker: reasoning ("because…", "for scale"), alternatives
     ("X over Y", "instead of Z"), or group framing ("the team concluded").
     Without these markers, an "approved X" or "paused Y" line is action,
     not decision.
   - If a line records BOTH the choice and its execution ("Approved config
     X because scaling issues"), prefer decision when reasoning/alternatives
     are visible; prefer action when it is just verb + object + parameters.

   The type is determined by what the statement DOES (deed / event /
   state / choice / pending work / structured steps / preference), not
   by who authored the content or by surface prefixes.

   Contrastive examples:
     "Deployed v2.3 to production"                        -> action     (deed completed)
     "The v2.3 deployment succeeded at 14:00"             -> episode    (third-person observed event)
     "Merged the auth refactor PR"                        -> action     (deed completed)
     "Production outage between 14:00 and 14:30"          -> episode    (event tied to time)
     "Completed full data pipeline validation"            -> action     (deed completed)
     "Confirmed in DM to Eli that I will participate"     -> action     (confirmation is the deed)
     "System uses OAuth 2.0 with refresh tokens"          -> fact       (durable state)
     "API endpoints: /users, /posts, /admin"              -> fact       (documentation)
     "The goal is $100M ARR by 2029 across LATAM"         -> plan       (specific target with a horizon — north-star endpoint)
     "Our priority this quarter is faster iteration"      -> preference (aspirational orientation, not a specific target)
     "Chose Postgres over MongoDB for scale"              -> decision   (alternatives + reasoning)
     "Going with OAuth 2.0 over SAML, our clients need mobile support" -> decision (alternatives + reasoning)
     "Team concluded we'll adopt async-first workflows"   -> decision   (group deliberation framing)
     "Approved the new auth config: cap $1.20, target $0.70" -> action  (approval-deed, no deliberation)
     "Paused the follow-up loop, will try a different approach" -> action (pause is the deed; future orientation is not deliberation)
     "Cancelled the beta rollout"                         -> action     (cancellation-deed)
     "Fix the login bug"                                  -> task       (pending work)
     "Week 1-2: 1) Share doc with Legal 2) Risk review"   -> plan       (ordered steps)
     "Team prefers concise Slack updates over long emails" -> preference (org style)

2. "weight": float 0.0-1.0 indicating importance
   - 0.9-1.0: critical decisions, key facts with evidence, high-impact events
   - 0.7-0.8: solid facts, meaningful events, clear preferences
   - 0.5-0.6: routine observations, minor events, uncertain information
   - 0.3-0.4: trivial, speculative, or low-confidence information
   - 0.0-0.2: negligible — content you would be comfortable having
     auto-archived after six months

3. "title": short label (max 80 chars) summarizing the memory for display in lists

4. "summary": 1-2 sentence condensed version capturing the key information

5. "ts_valid_start": ISO 8601 datetime string (optional, null if not applicable)
   - The earliest time this memory is valid/relevant
   - Extract from phrases like "starting March 1", "from next Monday", "after the meeting"
   - For events/meetings: the event start time
   - Today's date is {today}

6. "ts_valid_end": ISO 8601 datetime string (optional, null if not applicable)
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

7. "contains_pii": boolean (default false)
   - true if the content contains personally identifiable information
   - PII includes: email addresses, phone numbers, physical addresses, SSN/ID numbers, credit card numbers, dates of birth, full names paired with sensitive data
   - Do NOT flag generic first names, job titles, or company names alone

8. "pii_types": array of strings (optional, empty if no PII)
    - Types of PII detected, e.g. ["email", "phone", "address", "ssn", "credit_card", "date_of_birth"]

9. "atomic_facts": OPTIONAL — null in almost all cases. Populate only when
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
    - When in doubt, leave atomic_facts as null.

10. "business_relevance": one of "business" | "personal" (default "business")
    - "personal": private life unrelated to work — health, family, personal
      finance, relationships, errands, vacation planning, casual chat, idle ideas.
    - "business": work / professional / operational content (the default).
    - When unsure, choose "business" — only mark "personal" when you are
      confident the content is non-work.

Return ONLY valid JSON (no markdown fences):
{{"memory_type": "...", "weight": 0.5, "title": "...", "summary": "...", "ts_valid_start": null, "ts_valid_end": null, "contains_pii": false, "pii_types": [], "atomic_facts": null, "business_relevance": "business"}}

Content:
{content}
"""
)
