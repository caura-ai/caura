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

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from common.governance import mask, scan
from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    INTERVIEW_CHUNK_MAX_CHARS,
    INTERVIEW_EVENT_MAX_CHARS,
    INTERVIEW_MAX_ITEMS_PER_SECTION,
    INTERVIEW_MAX_KEYSTONES_IN_PROMPT,
    INTERVIEW_TEMPERATURE,
    MAX_CONTENT_LENGTH,
)
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem, BulkMemoryResponse
from core_api.services.memory_service import create_memories_bulk
from core_api.services.organization_settings import resolve_config

logger = logging.getLogger(__name__)

WATERMARK_COLLECTION = "interview_watermarks"

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


def watermark_doc_id(node_id: str) -> str:
    """Phase 1 keys the watermark per NODE (see decisions doc Q2):
    OpenClaw session keys are not guaranteed stable (legacy-compat paths
    mint synthetic per-instance ids), so per-session cursors would
    fragment. Session ids still travel per-event for report grouping.
    """
    return f"wm_{hashlib.sha1(node_id.encode()).hexdigest()[:40]}"


# ── Masking ──


def mask_events(events: list[dict]) -> tuple[list[dict], int]:
    """Deterministically mask PII/secrets in event content pre-LLM.

    All categories are scanned unconditionally: this runs before the
    tenant-configurable persistence gate, and a masked token in the
    interview prompt is always acceptable while a leaked secret is not.
    Returns the masked copies and the total finding count (audit/log).
    """
    masked: list[dict] = []
    total = 0
    for ev in events:
        content = (ev.get("content") or "")[:INTERVIEW_EVENT_MAX_CHARS]
        findings = scan(content)
        if findings:
            total += len(findings)
            content = mask(content, findings)
        masked.append({**ev, "content": content})
    return masked, total


# ── Chunking (map side) ──


def chunk_events(events: list[dict]) -> list[list[dict]]:
    """Split the window into char-budgeted chunks for the map phase."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for ev in events:
        ev_len = len(ev.get("content") or "") + 64  # + envelope overhead
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
Rules: every item must be standalone and concrete (names, paths, numbers). Take ts values from the
event timestamps you actually used — never invent times. Do not restate the same fact in two sections.
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
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
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
    """
    sc = get_storage_client()
    doc_id = watermark_doc_id(node_id)
    existing = await sc.get_document(tenant_id, WATERMARK_COLLECTION, doc_id, read=False)
    existing_seq = -1
    if existing and isinstance(existing.get("data"), dict):
        try:
            existing_seq = int(existing["data"].get("last_seq", -1))
        except (TypeError, ValueError):
            existing_seq = -1
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
    return cursor_to


# ── Orchestrator ──


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
    """The full window interview: mask → map → reduce → bulk → watermark."""
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

    config = await resolve_config(tenant_id)
    keystones = await _keystone_lines(tenant_id, fleet_id, agent_id)

    chunks = chunk_events(masked)
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
