"""Interviewer Phase 1 — server-side interview worker.

Consumes a node's buffered event window (submitted by the OpenClaw plugin
via ``POST /api/v1/interview/submit``), synthesizes a structured reflective
report with the tenant's LLM, and writes the report's sections as typed
memories through the existing idempotent bulk-write path.

Design notes (see ``docs/plans/interviewer-phase1-decisions.md``):

- **Mask before the LLM.** Events are PII/secret-masked on receipt with the
  shared deterministic library (``common.governance``) BEFORE any LLM sees
  them — the write-pipeline ``GovernanceScanContent`` gate only protects
  *persistence*, and this worker runs the LLM pre-persistence. The bulk
  write then re-runs the gate on the report items (defense in depth).
- **Chunked map-reduce.** Events are split into char-budgeted chunks; each
  chunk yields a mini-report (map); mini-reports merge into the final
  report (reduce). Single-chunk windows skip the reduce. The plugin-side
  submit cap plus the cursor-driven catch-up loop bound total volume.
- **Watermark is forward-only** and advances only AFTER the bulk write
  commits (crash anywhere → retry with the same deterministic attempt id
  → ``duplicate_attempt`` dedup → then the watermark advances).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from common.governance import mask, scan
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings as app_settings
from core_api.constants import (
    INTERVIEW_CHUNK_MAX_CHARS,
    INTERVIEW_EVENT_MAX_CHARS,
    INTERVIEW_MAX_ITEMS_PER_SECTION,
    INTERVIEW_MAX_KEYSTONES_IN_PROMPT,
    INTERVIEW_TEMPERATURE,
    MAX_CONTENT_LENGTH,
    NODE_OFFLINE_SECONDS,
)
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem, BulkMemoryResponse
from core_api.services.memory_service import create_memories_bulk
from core_api.services.organization_settings import get_settings_for_display, resolve_config
from core_api.services.tenants import list_tenants_with_interviewer_enabled

logger = logging.getLogger(__name__)

WATERMARK_COLLECTION = "interview_watermarks"
# Durable async-submit job queue (#665): one doc per (node, window),
# holding the MASKED event window until synthesis commits.
JOBS_COLLECTION = "interview_jobs"


class InterviewJobPermanentlyFailedError(RuntimeError):
    """Raised when enqueue_interview_job is called for a job already parked
    as failed_permanent — the route maps it to 409 so permanence is visible
    as a distinct status instead of blending into transient 500s."""


# A job stuck in "processing" longer than this is presumed dead (the
# fire-and-forget task hard-crashed with the process between the
# "processing" write and any terminal write) and is re-swept like a
# pending job. Generous vs. the worst-case synthesis (multi-chunk
# map-reduce with LLM fallback chains) so a slow-but-alive run is not
# double-processed — and even if it were, re-processing is idempotent via
# the deterministic bulk attempt id.
INTERVIEW_JOB_STALE_PROCESSING_SECONDS = 600
# Bounded fan-out for the scheduler sweep's job drain (#667): jobs
# synthesize concurrently (each is an LLM map-reduce plus storage writes,
# so a strictly sequential drain of a large backlog could outlive the
# tick), capped so a backlog can't stampede the LLM provider or exhaust
# the storage connection pool.
INTERVIEW_SWEEP_CONCURRENCY = 5
# ONE shared gate for every synthesis entry point (route fire-and-forget AND
# the scheduler sweep): two independent Semaphore(N) instances would allow
# 2N concurrent LLM map-reduces whenever the sweep overlaps in-flight route
# tasks — exactly the quota/pool exhaustion the cap exists to prevent.
synthesis_sem = asyncio.Semaphore(INTERVIEW_SWEEP_CONCURRENCY)

# Report section → memory_type. The section label is preserved in
# ``metadata.category`` so the original interview framing survives the
# mapping onto the fixed memory-type enum.
REPORT_SECTIONS: dict[str, str] = {
    "worked_on": "episode",
    "decisions": "decision",
    "outcomes": "outcome",
    "blockers": "task",
    "open_questions": "fact",
    "preferences_learned": "preference",
}


def interview_attempt_id(node_id: str, cursor_from: int, cursor_to: int) -> str:
    """Deterministic bulk-attempt id for one (node, window) interview.

    Computed server-side — never trusted from the client — so any retry of
    the same window (plugin resubmit, command re-delivery, worker crash
    re-run) resolves to ``duplicate_attempt`` instead of duplicate rows.
    Matches ``_BULK_ATTEMPT_ID_PATTERN`` (``^[A-Za-z0-9._:\\-]{1,128}$``).
    """
    digest = hashlib.sha1(f"{node_id}:{cursor_from}:{cursor_to}".encode()).hexdigest()
    return f"interview:{digest[:40]}"


def interview_job_doc_id(node_id: str, cursor_from: int, cursor_to: int) -> str:
    """Deterministic job-doc id for one (node, window) async submit (#665).

    Derived from the same identity as the bulk attempt id, so a duplicate
    submit of the same window upserts the SAME job doc instead of queueing
    a second synthesis.
    """
    return f"job_{interview_attempt_id(node_id, cursor_from, cursor_to)}"


def watermark_doc_id(node_id: str) -> str:
    """Phase 1 keys the watermark per NODE (see decisions doc Q2):
    OpenClaw session keys are not guaranteed stable (legacy-compat paths
    mint synthetic per-instance ids), so per-session cursors would
    fragment. Session ids still travel per-event for report grouping.
    """
    return f"wm_{hashlib.sha1(node_id.encode()).hexdigest()[:40]}"


# ── Masking ──


def mask_events(events: list[dict]) -> tuple[list[dict], int]:
    """Deterministically mask PII/secrets in event fields pre-LLM.

    Covers every field that reaches the prompt via ``_serialize_events``:
    ``content``, ``tool``, and ``outcome``. All categories are scanned
    unconditionally: this runs before the tenant-configurable persistence
    gate, and a masked token in the interview prompt is always acceptable
    while a leaked secret is not. Returns the masked copies and the total
    finding count (audit/log).
    """

    def _mask_field(text: str | None, max_len: int) -> tuple[str, int]:
        val = (text or "")[:max_len]
        findings = scan(val)
        return (mask(val, findings) if findings else val), len(findings)

    masked: list[dict] = []
    total = 0
    for ev in events:
        masked_content, n1 = _mask_field(ev.get("content"), INTERVIEW_EVENT_MAX_CHARS)
        masked_tool, n2 = _mask_field(ev.get("tool"), 200)
        masked_outcome, n3 = _mask_field(ev.get("outcome"), 200)
        total += n1 + n2 + n3
        update: dict = {"content": masked_content}
        if ev.get("tool") is not None:
            update["tool"] = masked_tool
        if ev.get("outcome") is not None:
            update["outcome"] = masked_outcome
        masked.append({**ev, **update})
    return masked, total


# ── Chunking (map side) ──


def chunk_events(events: list[dict]) -> list[list[dict]]:
    """Split the window into char-budgeted chunks for the map phase."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for ev in events:
        # Budget every field _serialize_events puts on the prompt line:
        # content, tool (" tool="), outcome (" outcome="), plus the
        # seq/ts/session/role envelope.
        ev_len = (
            len(ev.get("content") or "")
            + (len(ev.get("tool") or "") + 6 if ev.get("tool") else 0)
            + (len(ev.get("outcome") or "") + 9 if ev.get("outcome") else 0)
            + 80
        )
        if current and size + ev_len > INTERVIEW_CHUNK_MAX_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(ev)
        size += ev_len
    if current:
        chunks.append(current)
    return chunks


def _serialize_events(events: list[dict]) -> str:
    lines = []
    for ev in events:
        session = ev.get("session_id") or "-"
        tool = f" tool={ev['tool']}" if ev.get("tool") else ""
        outcome = f" outcome={ev['outcome']}" if ev.get("outcome") else ""
        lines.append(
            f"[{ev.get('seq')}] {ev.get('ts')} (session {session}) "
            f"{ev.get('role')}/{ev.get('kind')}{tool}{outcome}: {ev.get('content')}"
        )
    return "\n".join(lines)


_REPORT_SCHEMA_INSTRUCTION = """Respond with ONLY a JSON object of this exact shape (empty arrays allowed):
{
  "worked_on":           [{"summary": "...", "ts_start": "ISO8601 or null", "ts_end": "ISO8601 or null", "session_id": "... or null"}],
  "decisions":           [{"summary": "...", "rationale": "... or null", "ts": "ISO8601 or null"}],
  "outcomes":            [{"summary": "...", "result": "success|failure|partial", "ts": "ISO8601 or null"}],
  "blockers":            [{"summary": "...", "ts": "ISO8601 or null"}],
  "open_questions":      [{"summary": "...", "ts": "ISO8601 or null"}],
  "preferences_learned": [{"summary": "...", "ts": "ISO8601 or null"}]
}
Rules: every item must be standalone and concrete. Preserve exact figures VERBATIM — metrics,
benchmark scores, thresholds, counts, durations, version numbers, and file/table/branch names —
never round or generalize them (write "recall@10 0.87 vs 0.83, index -33%", not "better recall").
Keep each decision whole: capture its conditions, sequencing, and alternatives ("migrate to X after
the Y fix ships; keep Z as fallback"), not just the headline choice. Take ts values from the event
timestamps you actually used — never invent times. Do not restate the same fact in two sections.
Skip routine/mechanical steps; report only what a teammate would need to know."""


def build_prompt(
    events: list[dict],
    *,
    agent_id: str,
    keystone_lines: list[str],
    chunk_index: int,
    chunk_count: int,
) -> str:
    """Build the interview prompt for one chunk (the single shared prompt —
    Phase 4 relocates its execution into the broker, it does not fork it)."""
    part = f" (part {chunk_index + 1} of {chunk_count})" if chunk_count > 1 else ""
    keystones = ""
    if keystone_lines:
        keystones = (
            "\nMandatory governance rules for this scope — obey them in what you"
            " include or exclude:\n" + "\n".join(keystone_lines) + "\n"
        )
    return (
        f"You are the Interviewer: you turn an AI agent's raw activity trail into the team's"
        f" durable memory. Below is the recent activity window{part} of agent `{agent_id}`."
        f" Synthesize a structured reflective report of the key activities that kept it busy.\n"
        f"{keystones}\n"
        f"ACTIVITY TRAIL:\n{_serialize_events(events)}\n\n"
        f"{_REPORT_SCHEMA_INSTRUCTION}"
    )


# ── LLM (map) ──


def _empty_report() -> dict[str, list]:
    return {section: [] for section in REPORT_SECTIONS}


def _fake_report(events: list[dict]) -> dict:
    """Deterministic no-LLM fallback (fake provider / total LLM outage):
    a single episode summarizing the window so the cursor can still
    advance — an empty report would silently drop the window's history."""
    if not events:
        return _empty_report()
    report = _empty_report()
    report["worked_on"] = [
        {
            "summary": f"Activity window of {len(events)} events (LLM unavailable; unsynthesized).",
            "ts_start": events[0].get("ts"),
            "ts_end": events[-1].get("ts"),
            "session_id": None,
        }
    ]
    return report


async def _interview_chunk(prompt: str, config, events: list[dict]) -> dict:
    """Run one map-phase LLM call through the tenant's fallback chain."""
    from core_api.providers._retry import call_with_fallback

    async def _do_interview(llm) -> dict:
        return await llm.complete_json(prompt, temperature=INTERVIEW_TEMPERATURE)

    return await call_with_fallback(
        primary_provider_name=config.enrichment_provider,
        call_fn=_do_interview,
        fake_fn=lambda: _fake_report(events),
        tenant_config=config,
        service_label="interview",
        model_override=config.enrichment_model,
    )


# ── Reduce ──


def merge_reports(reports: list[dict]) -> dict:
    """Merge mini-reports: concatenate sections, drop exact-duplicate
    summaries, cap per section (keeps total under BULK_MAX_ITEMS)."""
    merged = _empty_report()
    seen: set[tuple[str, str]] = set()
    for report in reports:
        if not isinstance(report, dict):
            continue
        for section in REPORT_SECTIONS:
            for item in report.get(section) or []:
                if not isinstance(item, dict):
                    continue
                summary = (item.get("summary") or "").strip()
                if not summary:
                    continue
                key = (section, summary.lower())
                if key in seen:
                    continue
                seen.add(key)
                if len(merged[section]) < INTERVIEW_MAX_ITEMS_PER_SECTION:
                    merged[section].append(item)
    return merged


# ── Report → bulk items ──


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # LLM-emitted timestamps may omit the offset; treat naive as UTC so
        # downstream aware/naive comparisons and timestamptz storage don't
        # break.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def report_to_items(report: dict, *, node_id: str, command_id: str | None) -> list[BulkMemoryItem]:
    """Map report sections onto typed memories.

    ``ts_valid_start`` carries the SOURCE event time (not report time) so
    batching does not corrupt freshness ranking — decisions doc / C2/C3.
    """
    items: list[BulkMemoryItem] = []
    for section, memory_type in REPORT_SECTIONS.items():
        for entry in report.get(section) or []:
            summary = (entry.get("summary") or "").strip()
            if not summary:
                continue
            content = summary
            if section == "decisions" and entry.get("rationale"):
                content = f"{summary} — rationale: {entry['rationale']}"
            elif section == "outcomes" and entry.get("result"):
                content = f"[{entry['result']}] {summary}"
            metadata: dict[str, Any] = {
                "source": "interviewer",
                "category": section,
                "node_id": node_id,
                "written_by": "interviewer",
            }
            if command_id:
                metadata["command_id"] = command_id
            if entry.get("session_id"):
                metadata["session_id"] = entry["session_id"]
            if section == "outcomes" and entry.get("result"):
                metadata["outcome_result"] = entry["result"]
            items.append(
                BulkMemoryItem(
                    memory_type=memory_type,
                    content=content[:MAX_CONTENT_LENGTH],
                    metadata=metadata,
                    ts_valid_start=_parse_ts(entry.get("ts") or entry.get("ts_start")),
                    ts_valid_end=_parse_ts(entry.get("ts_end")),
                )
            )
    return items


# ── Keystones ──


async def _keystone_lines(tenant_id: str, fleet_id: str | None, agent_id: str) -> list[str]:
    """Merged governance rules for the interview prompt (best-effort)."""
    try:
        sc = get_storage_client()
        rows, _truncated = await sc.list_keystones(
            tenant_id,
            fleet_id=fleet_id,
            # Mirror routes/keystones.py: agent scope only makes sense
            # under a fleet scope.
            agent_id=agent_id if fleet_id else None,
        )
    except Exception:
        logger.warning("interview: keystone fetch failed; proceeding without", exc_info=True)
        return []
    lines = []
    for row in rows[:INTERVIEW_MAX_KEYSTONES_IN_PROMPT]:
        title = row.get("title") or row.get("doc_id") or ""
        content = (row.get("content") or "")[:200]
        lines.append(f"- {title}: {content}")
    return lines


# ── Watermark ──


async def _read_watermark_seq(sc, tenant_id: str, doc_id: str) -> int:
    doc = await sc.get_document(tenant_id, WATERMARK_COLLECTION, doc_id, read=False)
    if doc and isinstance(doc.get("data"), dict):
        try:
            return int(doc["data"].get("last_seq", -1))
        except (TypeError, ValueError):
            return -1
    return -1


# Bounded verify-and-repair passes for the read-max-write loop below.
_WATERMARK_WRITE_ATTEMPTS = 3


async def advance_watermark(
    tenant_id: str,
    *,
    node_id: str,
    agent_id: str,
    cursor_to: int,
    command_id: str | None,
) -> int:
    """Forward-only watermark advance. Returns the effective cursor.

    A stale retry (its ``cursor_to`` at or behind the stored cursor) is a
    no-op — the bulk write already deduplicated its rows, and regressing
    the cursor would re-open a consumed range.

    Concurrency contract: the doc store has no compare-and-set, so a bare
    read-check-write would be a TOCTOU race. Two mitigations, in order of
    load-bearing-ness:

    1. **Writers are serialized by design.** The scheduler keeps at most
       ONE pending ``interview_request`` per node and the plugin processes
       commands sequentially, so two in-flight submits for the same node
       only arise from a pathological zombie retry (e.g. a network-delayed
       resubmit of an old window landing after a newer window committed).
    2. **Verify-and-repair.** The write is max-preserving (re-reads and
       writes ``max(stored, cursor_to)``), then verifies the stored value
       and re-runs the pass if a concurrent smaller write clobbered it
       (bounded attempts).

    And the invariant is self-healing even if both miss: a regressed
    cursor only makes the next scheduler tick re-issue an already-consumed
    window, whose rows dedup via the deterministic bulk attempt id and
    whose completion re-advances the cursor — wasted work, never data
    corruption. If the doc store ever grows a conditional upsert /
    GREATEST semantics, this loop collapses to one call.
    """
    sc = get_storage_client()
    doc_id = watermark_doc_id(node_id)
    effective = cursor_to
    for _attempt in range(_WATERMARK_WRITE_ATTEMPTS):
        existing_seq = await _read_watermark_seq(sc, tenant_id, doc_id)
        if cursor_to <= existing_seq:
            return existing_seq
        await sc.upsert_document(
            {
                "tenant_id": tenant_id,
                "collection": WATERMARK_COLLECTION,
                "doc_id": doc_id,
                "data": {
                    "node_id": node_id,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "last_seq": cursor_to,
                    "last_interview_at": datetime.now(UTC).isoformat(),
                    "last_command_id": command_id,
                },
            }
        )
        # Verify: if a concurrent (smaller) writer landed between our write
        # and this read, repair on the next pass.
        stored = await _read_watermark_seq(sc, tenant_id, doc_id)
        if stored >= cursor_to:
            return stored
        logger.warning(
            "interview watermark: concurrent write regressed cursor (tenant=%s node=%s "
            "stored=%d want=%d); repairing",
            tenant_id,
            node_id,
            stored,
            cursor_to,
        )
    # Verify-and-repair attempts exhausted: report what is actually stored
    # rather than optimistically claiming ``cursor_to`` landed.
    try:
        final_stored = await _read_watermark_seq(sc, tenant_id, doc_id)
        return final_stored if final_stored >= 0 else effective
    except Exception:
        return effective


# ── Orchestrator ──


async def _synthesize_and_write(
    *,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str,
    node_id: str,
    command_id: str | None,
    cursor_from: int,
    cursor_to: int,
    masked_events: list[dict],
    advance_watermark_after: bool,
) -> dict:
    """Synthesis half of the interview: map → reduce → bulk (→ watermark).

    Shared by the legacy inline path (``run_interview``, which advances the
    watermark here, after the bulk write) and the async job processor
    (#665), which passes ``advance_watermark_after=False`` because the
    watermark already advanced at accept time — then the returned status
    dicts carry ``watermark: None``. Events MUST already be masked.
    """
    config = await resolve_config(tenant_id)
    keystones = await _keystone_lines(tenant_id, fleet_id, agent_id)

    chunks = chunk_events(masked_events)
    mini_reports = []
    for index, chunk in enumerate(chunks):
        prompt = build_prompt(
            chunk,
            agent_id=agent_id,
            keystone_lines=keystones,
            chunk_index=index,
            chunk_count=len(chunks),
        )
        mini_reports.append(await _interview_chunk(prompt, config, chunk))
    report = mini_reports[0] if len(mini_reports) == 1 else merge_reports(mini_reports)
    # Single-chunk reports still need shape normalization + caps.
    report = merge_reports([report])

    items = report_to_items(report, node_id=node_id, command_id=command_id)
    if not items:
        # Nothing report-worthy in the window (e.g. pure noise). Still
        # advance the cursor: the window was consumed, not lost.
        watermark = None
        if advance_watermark_after:
            watermark = await advance_watermark(
                tenant_id,
                node_id=node_id,
                agent_id=agent_id,
                cursor_to=cursor_to,
                command_id=command_id,
            )
        return {"status": "committed", "watermark": watermark, "memories_written": 0, "errors": 0}

    bulk: BulkMemoryResponse = await create_memories_bulk(
        BulkMemoryCreate(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,  # subject = the WORKER; interviewer is in metadata.written_by
            items=items,
            visibility="scope_team",
        ),
        bulk_attempt_id=interview_attempt_id(node_id, cursor_from, cursor_to),
    )

    errors = sum(1 for r in bulk.results if r.status == "error")
    written = sum(1 for r in bulk.results if r.status == "created")
    if errors and errors == len(bulk.results):
        # Total failure: do NOT advance the watermark — the window must be
        # re-interviewed (retry keeps the same attempt id, so any rows
        # that did land dedup as duplicate_attempt).
        return {"status": "failed", "watermark": None, "memories_written": 0, "errors": errors}

    watermark = None
    if advance_watermark_after:
        watermark = await advance_watermark(
            tenant_id,
            node_id=node_id,
            agent_id=agent_id,
            cursor_to=cursor_to,
            command_id=command_id,
        )
    return {
        "status": "partial" if errors else "committed",
        "watermark": watermark,
        "memories_written": written,
        "errors": errors,
    }


async def run_interview(
    *,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str,
    node_id: str,
    command_id: str | None,
    cursor_from: int,
    cursor_to: int,
    events: list[dict],
) -> dict:
    """The full window interview: mask → map → reduce → bulk → watermark.

    Legacy inline path (``interview_async_submit=False``): synthesis runs on
    the request and the watermark advances only after the bulk write.
    """
    started = datetime.now(UTC)

    masked, finding_count = mask_events(events)
    if finding_count:
        logger.info(
            "%s interview: masked %d PII/secret findings pre-LLM (tenant=%s node=%s)",
            started.isoformat(),
            finding_count,
            tenant_id,
            node_id,
        )

    return await _synthesize_and_write(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        node_id=node_id,
        command_id=command_id,
        cursor_from=cursor_from,
        cursor_to=cursor_to,
        masked_events=masked,
        advance_watermark_after=True,
    )


# ── Async submit job queue (#665) ──


async def enqueue_interview_job(
    *,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str,
    node_id: str,
    command_id: str | None,
    cursor_from: int,
    cursor_to: int,
    events: list[dict],
) -> str:
    """Persist one (node, window) as a durable ``interview_jobs`` doc.

    Masks FIRST — the job doc must never store unmasked PII/secrets (it
    outlives the request and is read back by the sweep). Idempotent: a
    duplicate submit of the same window resolves to the SAME doc id; the
    existing doc's status decides what happens (see the status ladder
    below). Returns the job doc id.
    """
    now = datetime.now(UTC)
    masked, finding_count = mask_events(events)
    if finding_count:
        logger.info(
            "%s interview: masked %d PII/secret findings pre-enqueue (tenant=%s node=%s)",
            now.isoformat(),
            finding_count,
            tenant_id,
            node_id,
        )
    doc_id = interview_job_doc_id(node_id, cursor_from, cursor_to)
    sc = get_storage_client()
    # Status ladder for a duplicate submit of the same window:
    #   - "done": the window was already synthesized — skip the upsert
    #     entirely (rewriting it would re-open a consumed window).
    #   - "processing": a concurrent processor owns the job — skip the
    #     upsert; downgrading to "pending" would hand the same window to a
    #     second processor (re-synthesis dedups via the deterministic bulk
    #     attempt id, but the race is pure wasted LLM work).
    #   - missing / "pending" / "failed_permanent": (re-)enqueue as
    #     "pending", PRESERVING attempts — resetting the retry budget
    #     would let a plugin resubmit cycle a failing job past
    #     interview_job_max_attempts forever.
    prior_attempts = 0
    prior_status: str | None = None
    try:
        existing = await sc.get_document(tenant_id, JOBS_COLLECTION, doc_id, read=False)
        if isinstance(existing, dict):
            prior_data = existing.get("data") or {}
            prior_status = prior_data.get("status")
            try:
                prior_attempts = int(prior_data.get("attempts", 0))
            except (TypeError, ValueError):
                prior_attempts = 0
    except Exception as exc:
        # Never silent: a reset attempts counter (or a missed "done")
        # bypasses interview_job_max_attempts / re-opens a consumed window.
        logger.warning(
            "interview job: could not read prior job state (tenant=%s doc=%s): %s "
            "— failing the request so the plugin retries the window",
            tenant_id,
            doc_id,
            exc,
        )
        # The doc's status is UNKNOWN — upserting "pending" here could
        # overwrite a "done"/"processing" job (re-opening a consumed window
        # / double-processing it) and reset its retry budget. But returning
        # success is worse: on a first-time submit no doc exists, so the
        # route would advance the watermark and the plugin would prune the
        # window — losing it permanently. Re-raise instead: the route 500s,
        # the plugin keeps its buffer and resubmits next tick.
        raise
    if prior_status in ("processing", "done"):
        logger.debug(
            "interview job: skipping enqueue over %s job (tenant=%s doc=%s)",
            prior_status,
            tenant_id,
            doc_id,
        )
        return doc_id
    if prior_status == "failed_permanent":
        # Returning doc_id here would let the route advance the watermark and
        # answer 200 — the plugin would prune a window the server has PARKED
        # and will never re-synthesize: silent, permanent loss. Raising makes
        # the route 500, the plugin keeps its buffer, and the failure stays
        # visible every tick until an operator intervenes (the masked events
        # remain in the job doc for recovery). Re-opening the job here is NOT
        # an option: this racy read-check-write path would hand the window a
        # fresh attempts budget (see the non-atomicity note below).
        logger.warning(
            "interview job: job permanently failed, cannot re-enqueue "
            "(tenant=%s doc=%s) — returning error so the plugin retries",
            tenant_id,
            doc_id,
        )
        raise InterviewJobPermanentlyFailedError(
            f"interview job {doc_id} is permanently failed and cannot be re-enqueued"
        )
    # NOTE: this read-check-write is NOT atomic — core-storage's document
    # upsert is a plain replace (``ON CONFLICT DO UPDATE SET data = :data``,
    # no conditional variant), so a processor can flip the doc to
    # "processing"/"done" between our read above and this write. Worst
    # case: a plugin resubmit in a multi-worker deployment can write a stale
    # prior_attempts over a higher value a concurrent processor committed,
    # allowing the job to exceed interview_job_max_attempts by up to
    # (number of workers - 1) attempts before every concurrent snapshot
    # sees the high value; a just-consumed window can also briefly re-open
    # as "pending". A conditional upsert (UPDATE ... WHERE attempts <=
    # :prior_attempts) in core-storage would close this; for now the
    # overshoot is bounded and re-synthesis is idempotent via the
    # deterministic bulk attempt id (wasted LLM work, never duplicate rows).
    # Double-check (not a CAS — see the non-atomicity NOTE above): re-read
    # immediately before the write to narrow the window in which a concurrent
    # processor's "processing"/"done" (or a park) gets overwritten. A failed
    # re-check raises — same invariant as the initial read: enqueue may only
    # return normally when the window is durably owned server-side.
    try:
        recheck = await sc.get_document(tenant_id, JOBS_COLLECTION, doc_id, read=False)
        if isinstance(recheck, dict):
            recheck_status = (recheck.get("data") or {}).get("status")
            if recheck_status in ("processing", "done"):
                logger.debug(
                    "interview job: skipping enqueue, status changed to %s during check (tenant=%s doc=%s)",
                    recheck_status,
                    tenant_id,
                    doc_id,
                )
                return doc_id
            if recheck_status == "failed_permanent":
                logger.warning(
                    "interview job: job permanently failed on re-check, cannot re-enqueue (tenant=%s doc=%s)",
                    tenant_id,
                    doc_id,
                )
                raise InterviewJobPermanentlyFailedError(
                    f"interview job {doc_id} is permanently failed and cannot be re-enqueued"
                )
            # Adopt the freshest attempts: a concurrent processor may have
            # incremented between the two reads — writing the stale
            # first-read value back would silently reset the retry budget
            # (the exact overwrite the double-check exists to narrow).
            try:
                prior_attempts = int((recheck.get("data") or {}).get("attempts", prior_attempts))
            except (TypeError, ValueError):
                pass
    except InterviewJobPermanentlyFailedError:
        raise
    except Exception as exc:
        logger.warning(
            "interview job: re-check read failed (tenant=%s doc=%s): %s — failing request",
            tenant_id,
            doc_id,
            exc,
        )
        raise
    await sc.upsert_document(
        {
            "tenant_id": tenant_id,
            "collection": JOBS_COLLECTION,
            "doc_id": doc_id,
            "data": {
                "status": "pending",
                "attempts": prior_attempts,
                "fleet_id": fleet_id,
                "agent_id": agent_id,
                "node_id": node_id,
                "command_id": command_id,
                "cursor_from": cursor_from,
                "cursor_to": cursor_to,
                "events": masked,
                "enqueued_at": now.isoformat(),
            },
        }
    )
    return doc_id


async def process_interview_job(
    tenant_id: str, doc_id: str, *, allow_stale_processing: bool = False
) -> dict | None:
    """Synthesize one persisted interview job. NEVER raises (#665).

    Lifecycle: ``pending`` → ``processing`` (attempts+1) → ``done`` on
    success; back to ``pending`` on failure so the scheduler sweep retries
    it (re-synthesis is idempotent via the deterministic bulk attempt id);
    parked as ``failed_permanent`` once attempts reach
    ``interview_job_max_attempts``. Returns the synthesis result dict, or
    None when there was nothing to do (missing/done/owned doc, parked, or
    the attempt raised).

    A ``processing`` doc is OWNED by whichever run wrote that status (the
    route's fire-and-forget task, or a prior sweep) — a second processor
    entering anyway would double-synthesize the window and its
    finally-reset could flip the owner's subsequent "done" back to
    "pending" (#667). So "processing" is a skip by default; only the
    scheduler sweep may reclaim one, by passing
    ``allow_stale_processing=True`` for docs whose owner is presumed dead
    (see ``_job_processing_is_stale`` — the guard re-checks staleness
    itself so the flag can never reclaim a fresh run).
    """
    sc = get_storage_client()
    try:
        doc = await sc.get_document(tenant_id, JOBS_COLLECTION, doc_id, read=False)
    except Exception:
        logger.exception("interview job: fetch failed (tenant=%s doc=%s)", tenant_id, doc_id)
        return None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict) or data.get("status") in ("done", "failed_permanent"):
        # Terminal states are no-ops: re-processing a parked job would
        # recount it as jobs_parked every sweep instead of jobs_skipped.
        return None
    if data.get("status") == "processing" and not (
        allow_stale_processing and _job_processing_is_stale(doc, datetime.now(UTC))
    ):
        return None

    # Fallback base for _set_state when its fresh re-fetch can't produce
    # one: the last merged payload this run wrote. Starts as the fetched
    # snapshot; after the "processing" transition it carries the
    # INCREMENTED attempts, so the status-only writes below ("done", the
    # finally pending-reset) can't regress the counter to the
    # pre-increment closure value when they hit the fallback path.
    last_written = data

    async def _set_state(
        state: dict, *, increment_attempts: bool = False, skip_if_terminal: bool = False
    ) -> bool:
        # Returns True when the write landed, False when skip_if_terminal
        # suppressed it — callers that must NOT proceed after a suppressed
        # transition (the "processing" claim) branch on this instead of
        # inferring from last_written, which can't distinguish a skip on
        # the stale-reclaim path (its initial status is already "processing").
        # upsert_document REPLACES the whole ``data`` payload server-side
        # (``ON CONFLICT DO UPDATE SET data = :data`` — see
        # core-storage-api ``document_upsert``), it does NOT merge keys. So
        # writing only the transitioned keys would destroy the events
        # payload, and writing the stale closure ``data`` snapshot would
        # resurrect keys a concurrent writer (e.g. a duplicate enqueue)
        # changed since our fetch. Re-fetch the CURRENT doc and merge the
        # transition keys onto that fresh snapshot instead.
        nonlocal last_written
        base = last_written
        fresh = await sc.get_document(tenant_id, JOBS_COLLECTION, doc_id, read=False)
        if isinstance(fresh, dict) and isinstance(fresh.get("data"), dict):
            base = fresh["data"]
        if skip_if_terminal and base.get("status") in ("done", "failed_permanent"):
            # A CONCURRENT run finished (or parked) the same window while
            # this one was in flight — overwriting "done" would re-open a
            # consumed window, and overwriting "failed_permanent" would
            # re-enter the retry loop past the attempts cap (#667). The
            # check runs on the SAME snapshot the merge uses, collapsing
            # the old pre-check-then-write shape's two fetch-write gaps
            # into one (round 6). On the fallback path (fresh fetch
            # unusable → base is this run's own last-written snapshot) the
            # check is best-effort only: our own snapshot can't show a
            # concurrent terminal write, so a "done" landing while storage
            # reads are failing may still be overwritten — the irreducible
            # plain-replace race; re-synthesis stays idempotent via the
            # deterministic bulk attempt id.
            return False
        merged = {**base, **state}
        if increment_attempts:
            # Increment from the FRESH base, not the caller's stale local
            # snapshot: two concurrent processors would otherwise both read
            # attempts=N and both write N+1, undercounting attempts and
            # letting a job exceed interview_job_max_attempts.
            try:
                merged["attempts"] = int(base.get("attempts", 0)) + 1
            except (TypeError, ValueError):
                merged["attempts"] = 1
        await sc.upsert_document(
            {
                "tenant_id": tenant_id,
                "collection": JOBS_COLLECTION,
                "doc_id": doc_id,
                "data": merged,
            }
        )
        last_written = merged
        return True

    try:
        attempts = int(data.get("attempts", 0))
    except (TypeError, ValueError):
        attempts = 0
    try:
        if attempts >= app_settings.interview_job_max_attempts:
            logger.warning(
                "interview job: attempts exhausted, parking as failed_permanent "
                "(tenant=%s doc=%s attempts=%d)",
                tenant_id,
                doc_id,
                attempts,
            )
            # skip_if_terminal: a stale-processing reclaim can race the
            # original (slow-but-alive) run — if that run wrote "done" just
            # before this park, overwriting it would falsely discard a
            # completed synthesis. Same rationale as the finally reset.
            parked = await _set_state({"status": "failed_permanent"}, skip_if_terminal=True)
            if not parked:
                # A concurrent run finished the job between our fetch and
                # this park — it owns the outcome; reporting parked here
                # would miscount a completed window as failed_permanent.
                return None
            return {"status": "failed_permanent"}
        # Local mirror for logging and the park-check above; the WRITTEN
        # counter comes from increment_attempts (fresh-base increment).
        attempts += 1
        # processing_started_at drives the sweep's stale-"processing"
        # recovery (INTERVIEW_JOB_STALE_PROCESSING_SECONDS).
        claimed = await _set_state(
            {
                "status": "processing",
                "processing_started_at": datetime.now(UTC).isoformat(),
            },
            increment_attempts=True,
            skip_if_terminal=True,
        )
        if not claimed:
            # A concurrent run finished (or parked) the job between our
            # initial fetch and this claim — let it own the result rather
            # than overwriting "done" with "processing" and re-synthesizing
            # (whose failure path could later park a correctly-completed
            # window as failed_permanent).
            return None
        # Use the actually-stored count (last_written carries the fresh-base
        # increment) rather than the stale initial snapshot for logging and
        # the park check on later paths.
        attempts = last_written.get("attempts", attempts)
    except Exception:
        logger.exception("interview job: state write failed (tenant=%s doc=%s)", tenant_id, doc_id)
        return None

    # Every exit below without a committed "done" write MUST converge the
    # job back to "pending" — synthesis failure, done-write failure, and
    # cancellation alike — else it strands in "processing" until the
    # (slower) stale-processing sweep. The finally block owns that
    # convergence; ``wrote_done`` records the one exit that must not.
    result: dict | None = None
    wrote_done = False
    try:
        try:
            result = await _synthesize_and_write(
                tenant_id=tenant_id,
                fleet_id=data.get("fleet_id"),
                agent_id=str(data.get("agent_id") or ""),
                node_id=str(data.get("node_id") or ""),
                command_id=data.get("command_id"),
                cursor_from=int(data.get("cursor_from") or 0),
                cursor_to=int(data.get("cursor_to") or 0),
                masked_events=data.get("events") or [],
                # The watermark already advanced at accept time (route).
                advance_watermark_after=False,
            )
        except Exception:
            logger.exception(
                "interview job: synthesis failed (tenant=%s doc=%s attempt=%d)",
                tenant_id,
                doc_id,
                attempts,
            )
            # Sentinel (not None): the job WAS claimed and attempts WAS
            # incremented before this failure — the sweep must count it as
            # retried, while None stays reserved for true no-ops (missing/
            # done doc, concurrent-ownership skip) where nothing happened.
            result = {"status": "failed_transient"}
        # "partial" is deliberately NOT terminal here: in the async path the
        # watermark advanced at accept and the plugin pruned — marking a
        # partial bulk write "done" would permanently lose the failed rows.
        # Falling through to the pending reset lets the sweep re-drive it;
        # re-synthesis is idempotent (already-written rows dedup as
        # duplicate_attempt, failed rows get a fresh attempt).
        if result and result.get("status") not in ("failed", "failed_transient", "partial"):
            try:
                # No "attempts" key: the "processing" transition already
                # stamped the fresh-base increment; re-writing the local
                # mirror here could clobber a concurrent run's newer count
                # (#667). _set_state's merge base carries the stored value.
                await _set_state(
                    {
                        "status": "done",
                        "memories_written": result.get("memories_written", 0),
                        "completed_at": datetime.now(UTC).isoformat(),
                    }
                )
                wrote_done = True
            except Exception:
                logger.exception(
                    "interview job: done-state write failed (tenant=%s doc=%s)", tenant_id, doc_id
                )
    finally:
        # Runs for plain returns, ordinary Exceptions (already swallowed
        # above — this function never raises for them), AND CancelledError
        # (which re-raises on its own once this block ends, so cancellation
        # still propagates to the asyncio runtime). Back to pending: the
        # next sweep retries; the deterministic bulk attempt id makes the
        # re-synthesis idempotent. The reset write itself gets one extra
        # best-effort attempt — a single transient storage blip must not
        # strand the job in "processing".
        if not wrote_done:
            for reset_try in (1, 2):
                try:
                    # skip_if_terminal: between our failure and this reset a
                    # CONCURRENT run may have finished the same window —
                    # _set_state backs off from a terminal status using the
                    # SAME fresh snapshot it merges onto (one fetch-write
                    # gap; round 6 collapsed the old separate pre-check +
                    # write shape). Still not atomic (plain-replace upsert),
                    # but the residual race is one gap, not two. No
                    # "attempts" key on the reset either: the fresh merge
                    # base already carries the stored count.
                    await _set_state({"status": "pending"}, skip_if_terminal=True)
                    break
                # BaseException: a CancelledError-mid-teardown reset attempt
                # must not mask the ORIGINAL in-flight exception (which the
                # finally re-raises on its own once this block ends).
                except BaseException:
                    logger.warning(
                        "interview job: pending-reset write failed (tenant=%s doc=%s try=%d)",
                        tenant_id,
                        doc_id,
                        reset_try,
                        exc_info=True,
                    )
    return result


def _job_processing_is_stale(row: dict, now: datetime) -> bool:
    """True when a ``processing`` job doc's run is presumed dead.

    Stale = ``processing_started_at`` older than
    ``INTERVIEW_JOB_STALE_PROCESSING_SECONDS`` — the owning task hard-
    crashed between the "processing" write and any terminal write. A
    missing/unparseable timestamp (docs written before it existed) also
    counts as stale, else those docs would strand in "processing" forever.
    """
    data = row.get("data") if isinstance(row, dict) else None
    if not isinstance(data, dict):
        return False
    started = data.get("processing_started_at")
    if not started or not isinstance(started, str):
        return True
    try:
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return True
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    return (now - started_dt).total_seconds() >= INTERVIEW_JOB_STALE_PROCESSING_SECONDS


async def process_pending_interview_jobs(limit_per_tenant: int = 20) -> dict:
    """Drain ``pending`` (and stale ``processing``) interview jobs across
    opted-in tenants (#665).

    The durable retry path behind the submit route's fire-and-forget task:
    the scheduler sweep calls this, so a job whose immediate processing
    died with the process (or failed transiently) still completes. Jobs
    stranded in ``processing`` by a hard crash mid-run are recovered once
    stale (see ``_job_processing_is_stale``); re-processing them is
    idempotent via the deterministic bulk attempt id, so racing a
    slow-but-alive run wastes work but never duplicates rows. Jobs run
    concurrently, bounded by ``INTERVIEW_SWEEP_CONCURRENCY`` (round 6).
    Returns a bounded counts summary.
    """
    sc = get_storage_client()
    now = datetime.now(UTC)
    # read=False → primary, same reason as the watermark read below: this call
    # selects which tenants are swept at all, so replica lag drops a tenant
    # from the tick entirely rather than merely returning it a stale field.
    tenants = await list_tenants_with_interviewer_enabled(read=False)
    summary = {
        "tenants": len(tenants),
        # Tenants dropped from this tick by a storage error below. ``jobs_sweep_ok``
        # only covers this function raising as a whole; the per-tenant handler
        # catches and continues, so without this counter a sweep that failed for
        # some tenants returns the same numbers as one where those tenants had
        # nothing to do — the exact confusion #1019 removed one level up, and the
        # one that lets a single tenant's interviewer stall indefinitely behind a
        # summary that reads healthy.
        "tenants_failed": 0,
        "jobs_processed": 0,
        "jobs_done": 0,
        "jobs_retried": 0,
        "jobs_parked": 0,
        "jobs_skipped": 0,
    }
    # Pass 1: collect (tenant, doc, allow_stale) triples across tenants —
    # per-tenant limits and stale filtering identical to the old
    # sequential drain.
    jobs: list[tuple[str, str, bool]] = []
    for tenant_id in tenants:
        try:
            # Storage ``where`` is JSONB scalar equality on ``data->>key``
            # (see skills_inbox), so pending-only filtering happens DB-side.
            docs = await sc.query_documents(
                {
                    "tenant_id": tenant_id,
                    "collection": JOBS_COLLECTION,
                    "where": {"status": "pending"},
                    "order_by": "created_at",
                    "order": "asc",
                    "limit": limit_per_tenant,
                }
            )
            # ``where`` can't express "older than", so fetch processing
            # docs by scalar equality and apply the staleness cutoff
            # client-side.
            processing = await sc.query_documents(
                {
                    "tenant_id": tenant_id,
                    "collection": JOBS_COLLECTION,
                    "where": {"status": "processing"},
                    "order_by": "created_at",
                    "order": "asc",
                    # over-fetch; stale filter is client-side
                    "limit": limit_per_tenant * 4,
                }
            )
        except Exception:
            logger.exception("interview jobs: pending query failed (tenant=%s)", tenant_id)
            summary["tenants_failed"] += 1
            continue
        stale = [row for row in processing or [] if _job_processing_is_stale(row, now)]
        # allow_stale_processing only for the stale rows: the sweep is the
        # ONE caller allowed to reclaim a stale "processing" doc (the
        # fire-and-forget task owns fresh ones — see process_interview_job).
        stale_ids = {str(row.get("doc_id") or "") for row in stale}
        for row in list(docs or []) + stale:
            doc_id = str(row.get("doc_id") or "")
            if not doc_id:
                continue
            jobs.append((tenant_id, doc_id, doc_id in stale_ids))

    # Pass 2: bounded fan-out.
    semaphore = synthesis_sem  # shared with the route's fire-and-forget path

    async def _run_one(tenant_id: str, doc_id: str, allow_stale: bool) -> dict | None:
        async with semaphore:
            return await process_interview_job(tenant_id, doc_id, allow_stale_processing=allow_stale)

    # process_interview_job NEVER raises ordinary exceptions (every path
    # inside it is wrapped — see its docstring), so a bare gather can only
    # propagate BaseException (cancellation), which SHOULD abort the sweep.
    results = await asyncio.gather(
        *(_run_one(tenant_id, doc_id, allow_stale) for tenant_id, doc_id, allow_stale in jobs)
    )
    # Per-result counting is order-insensitive — summary semantics are
    # identical to the old sequential loop.
    for result in results:
        summary["jobs_processed"] += 1
        if result and result.get("status") == "committed":
            summary["jobs_done"] += 1
        elif result and result.get("status") == "failed_permanent":
            summary["jobs_parked"] += 1
        elif result is None:
            # No-op: missing/done doc, or a concurrent run claimed the job
            # between the sweep's fetch and our claim — nothing was retried.
            summary["jobs_skipped"] += 1
        else:
            summary["jobs_retried"] += 1
    return summary


# ── Schedule (cron entry point) ──


def _is_due(watermark_data: dict | None, period_hours: int, now: datetime) -> bool:
    """A node is due when it has never been interviewed, or its last
    interview is at least one period old."""
    if not watermark_data:
        return True
    last = watermark_data.get("last_interview_at")
    if not last or not isinstance(last, str):
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - last_dt).total_seconds() >= period_hours * 3600


def _node_is_eligible(node: dict, now: datetime) -> bool:
    """Live, real nodes only: skip fleet-registration sentinels and nodes
    whose heartbeat has gone dark (an offline node can't answer the
    command anyway — it would just sit pending and block the next tick)."""
    name = node.get("node_name") or ""
    metadata = node.get("metadata") or {}
    if metadata.get("sentinel") or name.startswith("_fleet_"):
        return False
    hb = node.get("last_heartbeat")
    if not hb or not isinstance(hb, str):
        return False
    try:
        hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - hb_dt).total_seconds() <= NODE_OFFLINE_SECONDS


async def run_interview_schedule() -> dict:
    """Queue ``interview_request`` fleet commands for every due node of every
    opted-in tenant. The core-operations hourly tick calls this via
    ``POST /admin/interview/schedule/run``.

    Per node, at most ONE interview_request is in flight: an existing
    pending command skips the node (no stacking while a node is slow or
    briefly offline). Dueness is driven by the watermark doc's
    ``last_interview_at`` vs the tenant's ``interviewer.period_hours``, so
    the same tick cadence serves every tenant regardless of their period —
    and a backlog (submit cap reached) naturally drains because the
    watermark's ``last_seq`` advances while ``last_interview_at`` gates the
    NEXT window. Commands are queued unsigned in OSS (the plugin default is
    permissive; enterprise signing gateways sign in transit).
    """
    sc = get_storage_client()
    now = datetime.now(UTC)
    # read=False → primary, same reason as the watermark read below: this call
    # selects which tenants are swept at all, so replica lag drops a tenant
    # from the tick entirely rather than merely returning it a stale field.
    tenants = await list_tenants_with_interviewer_enabled(read=False)
    summary = {
        "tenants": len(tenants),
        # Same reason as the jobs sweep's counter: both loops below log-and-continue,
        # so a scan that failed is otherwise reported as a tenant with no due nodes.
        # ``nodes_failed`` also keeps the node counters self-consistent —
        # ``nodes_considered`` is incremented before the per-node try, so without it
        # a failing node is counted as considered and lands in no outcome bucket.
        "tenants_failed": 0,
        "nodes_considered": 0,
        "nodes_failed": 0,
        "commands_queued": 0,
        "skipped_pending": 0,
        "skipped_not_due": 0,
    }
    for tenant_id in tenants:
        settings = await get_settings_for_display(tenant_id)
        cfg = settings.get("interviewer") or {}
        period_hours = int(cfg.get("period_hours") or 12)
        template_id = cfg.get("template_id") or "default-v1"

        try:
            nodes = await sc.list_nodes(tenant_id)
            # High limit so the pending-dedup set is complete for any
            # realistic fleet size — a truncated set would re-queue nodes
            # whose pending command fell outside the page.
            pending = await sc.list_commands(
                tenant_id, status="pending", command="interview_request", limit=10_000
            )
        except Exception:
            logger.exception("interview schedule: tenant scan failed (tenant=%s)", tenant_id)
            summary["tenants_failed"] += 1
            continue
        pending_nodes = {str(c.get("node_id")) for c in pending}

        for node in nodes:
            if not _node_is_eligible(node, now):
                continue
            node_id = str(node.get("id") or "")
            if not node_id:
                continue
            summary["nodes_considered"] += 1
            if node_id in pending_nodes:
                summary["skipped_pending"] += 1
                continue
            # Per-node isolation: one node's storage failure must not abort
            # scheduling for the tenant's remaining nodes (or later tenants).
            try:
                # read=False → primary, matching the advance path: a stale
                # replica cursor would re-issue a consumed window (dedup makes
                # it harmless but wasted LLM work) or mis-time dueness.
                watermark = await sc.get_document(
                    tenant_id, WATERMARK_COLLECTION, watermark_doc_id(node_id), read=False
                )
                data = watermark.get("data") if isinstance(watermark, dict) else None
                if not _is_due(data, period_hours, now):
                    summary["skipped_not_due"] += 1
                    continue
                last_seq = -1
                if isinstance(data, dict):
                    try:
                        last_seq = int(data.get("last_seq", -1))
                    except (TypeError, ValueError):
                        last_seq = -1
                await sc.create_command(
                    {
                        "tenant_id": tenant_id,
                        "node_id": node_id,
                        "command": "interview_request",
                        "payload": {
                            # Echoed so the plugin submits with the SAME node key
                            # the watermark is stored under (it only knows its
                            # node_name locally; the watermark is keyed by the
                            # fleet-node UUID).
                            "node_id": node_id,
                            "since_seq": last_seq + 1,
                            "template_id": template_id,
                            "period_hours": period_hours,
                        },
                    }
                )
                summary["commands_queued"] += 1
            except Exception:
                logger.exception(
                    "interview schedule: node scan failed (tenant=%s node=%s)",
                    tenant_id,
                    node_id,
                )
                summary["nodes_failed"] += 1
    # #665: drain persisted async-submit jobs in the same sweep — the
    # durable retry path for jobs whose fire-and-forget processing at
    # submit time died with the process or failed transiently.
    # Zero-init unconditionally so the summary schema is identical whether
    # the sweep succeeds or raises — callers index these keys directly.
    for key in ("jobs_processed", "jobs_done", "jobs_retried", "jobs_parked", "jobs_skipped"):
        summary[key] = 0
    # The sweep's own ``tenants_failed`` is a DIFFERENT population from this
    # function's: the sweep failed to query a tenant's job queue, this one failed
    # to scan a tenant's nodes. A tick can hit either, both or neither, so they
    # are carried side by side rather than summed into one number that answers
    # neither question.
    summary["jobs_tenants_failed"] = 0
    # ``jobs_sweep_ok`` exists because the zero-init above is otherwise
    # indistinguishable from a healthy idle sweep: a total failure and "no
    # pending jobs" both answer 200 with five zeros. The hourly cron reads
    # this endpoint, so a sweep that raised on every tick would look exactly
    # like a quiet queue for as long as nobody read the logs. Keep the 200 —
    # the scheduling half of the summary above is still valid and still worth
    # returning — but say which of the two happened.
    summary["jobs_sweep_ok"] = True
    try:
        jobs_summary = await process_pending_interview_jobs()
    except Exception as exc:
        logger.exception("interview schedule: pending-jobs sweep failed")
        summary["jobs_sweep_ok"] = False
        # Type name only. The full exception is already in the log line above,
        # and this string lands in an HTTP response body — some ``__str__``
        # implementations carry hostnames, URLs or request fragments, which is
        # the same reason ``_storage_detail`` refuses to forward a raw error
        # body to a caller. The type is what a dashboard branches on; the
        # detail belongs where it already is.
        summary["jobs_sweep_error"] = type(exc).__name__
    else:
        for key in ("jobs_processed", "jobs_done", "jobs_retried", "jobs_parked", "jobs_skipped"):
            summary[key] = jobs_summary.get(key, 0)
        summary["jobs_tenants_failed"] = jobs_summary.get("tenants_failed", 0)
    return summary
