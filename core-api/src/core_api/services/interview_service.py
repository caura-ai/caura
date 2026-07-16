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
    NODE_OFFLINE_SECONDS,
)
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem, BulkMemoryResponse
from core_api.services.memory_service import create_memories_bulk
from core_api.services.organization_settings import get_settings_for_display, resolve_config
from core_api.services.tenants import list_tenants_with_interviewer_enabled

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
    tenants = await list_tenants_with_interviewer_enabled()
    summary = {
        "tenants": len(tenants),
        "nodes_considered": 0,
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
    return summary
