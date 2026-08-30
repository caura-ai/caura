"""Type-II state materialization — subject-local, batch, SHADOW phase (A59).

A stored fact can stop being a safe current default because a LATER,
non-contradicting fact broke its practical basis ("commutes by bicycle",
later "tore ACL, no weight-bearing six weeks"). Both are true; nothing
contradicts; the bike fact must stop governing answers.

Two approaches were measured before this one (see
``benchmark/a57-recall-experiments-findings.md``):

* read-side retrieval tricks — recency injection useless, query expansion
  net-flat. The oracle says coverage is the ceiling (27%->80%), so the fix
  has to put the right row in front of the reader.
* a per-WRITE bridge + invalidation judge (A58, shipped in shadow) — flagged
  2 of 15 known cases and sometimes targeted another subject's memory,
  because predicates are sparse and its fallback pool was not subject-local.

This module takes the third route, designed batch-first: once per night, per
SUBJECT, one LLM call over that subject's own live memories. The corpus shape
is what makes it cheap — subjects carry ~1.4 memories on average and only
~23% carry two or more, so the candidate set is a small minority of a small
minority (changed subjects only).

The output is deliberately NOT a profile blob: it is a predicate-specific
successor row that names the current state in the stale row's own vocabulary.
That reuses retrieval machinery that already exists — successor injection and
the A34 ranking contract — instead of adding a read path.

**This phase writes nothing.** It emits a report section with auditable
samples so the proposal precision can be scored by hand before any status
moves. That is the direct lesson of the A58 spike, whose verdicts were
logged in a shape nobody could score.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from core_api.config import settings
from core_api.providers._retry import call_with_fallback

logger = logging.getLogger(__name__)

# A subject needs at least this many live memories to be worth a call — a
# lone fact has nothing to invalidate it.
MIN_SUBJECT_MEMORIES = 2
# Hard ceiling on subjects per run, so one enormous tenant cannot turn a
# nightly sweep into an unbounded spend.
MAX_SUBJECTS_PER_RUN = 200
# Bundle cap: subjects are small in practice; this only bounds pathological ones.
MAX_MEMORIES_PER_SUBJECT = 25
MIN_CONFIDENCE = 0.6
# Proposals kept in the report for human scoring. Small on purpose: the point
# is an auditable sample, not a data dump.
MAX_SAMPLES = 20

_LIVE_STATUSES = {"active", "confirmed"}

TYPE_II_PROMPT = """\
Below are memories about ONE subject, oldest first, each with an id and date.

{bundle}

A memory is a STALE DEFAULT when a LATER memory here broke the practical basis \
that made it safe to rely on as the subject's CURRENT state — even though both \
statements remain true and neither contradicts the other. Practical basis \
includes: access, availability, continuity, feasibility, responsibility, \
arrangement, recurring coordination, institutional or status dependency.

Examples of the shape: "commutes by bicycle" + later "cannot bear weight for \
six weeks" -> the commute default is stale. "loves the local coffee shop" + \
later "moved abroad" -> the coffee-shop default is stale.

NOT stale: a fact that is merely older, merely related, or about a different \
aspect that still holds. Most subjects have NOTHING stale — an empty list is \
the common, correct answer.

For each stale default, write the replacement the store should hold instead: \
state what IS currently true, in the vocabulary of the stale memory, so a \
question phrased like the stale memory still finds it. If the replacement \
value is unknown, say so explicitly rather than inventing one.

Return JSON only:
{{"stale": [{{"stale_memory_id": "<id from the list>", \
"supporting_memory_ids": ["<later id that broke the basis>"], \
"replacement_content": "<one or two sentences>", \
"broken_basis": "<what specifically stopped holding>", \
"confidence": <0..1>}}]}}"""


def _parse_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw))
    except (ValueError, TypeError):
        return {}


def _created(m: dict) -> str:
    return str(m.get("created_at") or "")


def proposal_key(subject_id: str, stale_id: str) -> str:
    """Deterministic idempotency key.

    A nightly sweep re-reads the same subject repeatedly; without a stable key
    a re-run would stack a second successor on the same stale row. The write
    phase checks this key before creating anything.
    """
    return hashlib.sha256(f"{subject_id}|{stale_id}".encode()).hexdigest()[:16]


def group_subjects(memories: list[dict]) -> dict[str, list[dict]]:
    """Bundle live memories by subject, oldest first.

    Subject-local by construction: a bundle never contains another subject's
    rows, which is the specific failure the A58 spike exhibited (2 of 10
    verdicts pointed at a different person). Rows without a resolved subject
    are skipped — there is no bundle to reason over.
    """
    bundles: dict[str, list[dict]] = {}
    for m in memories:
        subject = m.get("subject_entity_id")
        if not subject:
            continue
        if (m.get("status") or "active") not in _LIVE_STATUSES:
            continue
        if m.get("deleted_at"):
            continue
        bundles.setdefault(str(subject), []).append(m)
    for subject, rows in bundles.items():
        rows.sort(key=_created)
        if len(rows) > MAX_MEMORIES_PER_SUBJECT:
            # Keep the newest window: the breaking evidence is by definition
            # later than what it invalidates.
            bundles[subject] = rows[-MAX_MEMORIES_PER_SUBJECT:]
    return bundles


def select_candidates(bundles: dict[str, list[dict]], since: str | None) -> list[str]:
    """Subjects worth an LLM call: >=2 live memories AND something new since
    the last run. ``since`` is the previous run's watermark (ISO string);
    None means 'first run, consider every multi-memory subject'."""
    out = []
    for subject, rows in bundles.items():
        if len(rows) < MIN_SUBJECT_MEMORIES:
            continue
        if since and not any(_created(m) > since for m in rows):
            continue
        out.append(subject)
    # Deterministic order, newest activity first, so the per-run cap keeps the
    # most-recently-changed subjects rather than an arbitrary slice.
    out.sort(key=lambda s: _created(bundles[s][-1]), reverse=True)
    return out[:MAX_SUBJECTS_PER_RUN]


def validate_proposal(p: dict, subject_id: str, rows: list[dict]) -> tuple[dict | None, str]:
    """Deterministic gates. Returns (accepted_proposal, rejection_reason)."""
    by_id = {str(m.get("id")): m for m in rows}
    stale_id = str(p.get("stale_memory_id") or "")
    if stale_id not in by_id:
        return None, "stale_id_not_in_bundle"
    support = [str(s) for s in (p.get("supporting_memory_ids") or [])]
    support = [s for s in support if s in by_id]
    if not support:
        return None, "no_support_in_bundle"
    stale_created = _created(by_id[stale_id])
    # The basis-breaker must postdate what it invalidates — the temporal
    # causality guard. Without it the model can "invalidate" a fresh row with
    # an older one, which is how a good current fact gets buried.
    if not any(_created(by_id[s]) > stale_created for s in support):
        return None, "support_not_later_than_stale"
    replacement = str(p.get("replacement_content") or "").strip()
    if not replacement:
        return None, "empty_replacement"
    try:
        confidence = float(p.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None, "bad_confidence"
    if confidence < MIN_CONFIDENCE:
        return None, "below_confidence_floor"
    stale_row = by_id[stale_id]
    return (
        {
            "key": proposal_key(subject_id, stale_id),
            "subject_entity_id": subject_id,
            "stale_memory_id": stale_id,
            # May be None: predicates are sparsely populated in practice, and
            # the write phase keys on supersedes_id, so a missing predicate
            # must not disqualify an otherwise good proposal (the A58 spike's
            # rdf route found nothing for exactly this reason).
            "predicate": stale_row.get("predicate"),
            "scope": stale_row.get("scope"),
            "stale_content": (stale_row.get("content") or "")[:200],
            "supporting_memory_ids": support,
            "supporting_content": [(by_id[s].get("content") or "")[:200] for s in support[:2]],
            "replacement_content": replacement[:1000],
            "broken_basis": str(p.get("broken_basis") or "")[:300],
            "confidence": confidence,
        },
        "",
    )


async def _call_subject(bundle_text: str, tenant_config) -> list[dict]:
    prompt = TYPE_II_PROMPT.format(bundle=bundle_text)
    provider_name = (
        tenant_config.enrichment_provider if tenant_config else settings.entity_extraction_provider
    )

    async def _do(llm) -> list[dict]:
        raw = _parse_json(await llm.complete_json(prompt))
        items = raw.get("stale")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do,
        # An outage yields nothing — never a stand-in proposal.
        fake_fn=lambda: [],
        tenant_config=tenant_config,
        service_label="type_ii_materializer",
        model_attr="entity_extraction_model",
        timeout=20.0,
    )


def render_bundle(rows: list[dict]) -> str:
    lines = []
    for m in rows:
        date = _created(m)[:10]
        pred = m.get("predicate") or "-"
        lines.append(f"[{m.get('id')}] ({date}, predicate={pred}) {(m.get('content') or '')[:400]}")
    return "\n".join(lines)


async def run_shadow(
    memories: list[dict],
    tenant_id: str,
    tenant_config=None,
    since: str | None = None,
) -> dict:
    """Subject-local Type-II sweep in SHADOW mode.

    Writes nothing anywhere. Returns the ``type_ii_staleness`` report section:
    counts plus a bounded sample of accepted proposals (with the stale text,
    the supporting text, and the proposed replacement side by side) so a human
    can score precision without reading logs.
    """
    section: dict[str, Any] = {
        "enabled": True,
        "mode": "shadow",
        "subjects_scanned": 0,
        "subjects_called": 0,
        "proposals": 0,
        "accepted": 0,
        "rejected": 0,
        "rejections": {},
        "samples": [],
    }
    try:
        bundles = group_subjects(memories)
        section["subjects_scanned"] = len(bundles)
        candidates = select_candidates(bundles, since)
        for subject in candidates:
            rows = bundles[subject]
            try:
                raw_proposals = await _call_subject(render_bundle(rows), tenant_config)
            except Exception:
                logger.warning("type_ii subject call failed subject=%s", subject, exc_info=True)
                continue
            section["subjects_called"] += 1
            section["proposals"] += len(raw_proposals)
            for p in raw_proposals:
                accepted, reason = validate_proposal(p, subject, rows)
                if accepted is None:
                    section["rejected"] += 1
                    section["rejections"][reason] = section["rejections"].get(reason, 0) + 1
                    continue
                section["accepted"] += 1
                if len(section["samples"]) < MAX_SAMPLES:
                    section["samples"].append(accepted)
                logger.info(
                    "type_ii_shadow proposal tenant=%s subject=%s stale=%s support=%s "
                    "confidence=%.2f basis=%s",
                    tenant_id,
                    subject,
                    accepted["stale_memory_id"],
                    ",".join(accepted["supporting_memory_ids"][:2]),
                    accepted["confidence"],
                    accepted["broken_basis"][:120],
                )
        logger.info(
            "type_ii_shadow completed tenant=%s subjects_scanned=%d called=%d accepted=%d rejected=%d",
            tenant_id,
            section["subjects_scanned"],
            section["subjects_called"],
            section["accepted"],
            section["rejected"],
        )
        return section
    except Exception:
        logger.warning("type_ii_shadow failed tenant=%s", tenant_id, exc_info=True)
        section["error"] = True
        return section
