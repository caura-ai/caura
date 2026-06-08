"""Forge service — the resident orchestrator (SF-104 part 2).

End-to-end Phase-1 pipeline:

  build session traces (SF-102)
    → filter to labeled outcomes
    → cluster by entity Jaccard overlap
    → gate clusters (volume + diversity)
    → distill each cluster via injected ``llm_fn`` (uses SF-104 prompt)
    → compute fingerprint (SF-103)
    → check poison memory (``forge_rejected_fingerprints``)
    → write skill candidate doc with status='candidate', source='forge'

Phase-1 MVP scope (per plan §15):

  * Forge runs ONLY as a manual CLI invocation (SF-106) or via
    direct call from the eval harness (SF-105).
  * Candidates ALWAYS land with ``status='candidate'``. They never
    auto-promote to ``staged`` in Phase 1 — the auto-gates that
    eventually move them are part of Phase 2.
  * No HITL surfacing — the inbox is Phase 2.

Everything that touches an external system is injected as a callable
parameter so the unit tests run hermetic:

  * ``llm_fn``        — async (prompt: str) → str (raw LLM response).
  * ``memory_fetcher`` — async (memory_ids) → dict[memory_id → content].
  * ``poison_checker`` — async (fingerprint) → bool (True = poisoned).
  * ``candidate_writer`` — async (candidate_doc) → None (persists).

The production caller wires real implementations; the eval harness
swaps in golden-output fakes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core_api.services.forge.distill_prompt import (
    ClusterPromptInputs,
    DistillParseError,
    TraceSnapshot,
    build_distill_prompt,
    parse_distill_response,
)
from core_api.services.forge.fingerprint import (
    ENTITY_TOP_K,
    ClusterFingerprintInputs,
    compute_fingerprint,
)
from core_api.services.forge.sentinel_scan import scan_skill_doc
from core_api.services.session_trace import (
    SessionTraceRow,
    build_session_traces,
)

logger = logging.getLogger(__name__)


# ── Tunables (mirrors org_settings.skills_factory.forge.*) ────────


@dataclass(frozen=True)
class ForgeConfig:
    """Resolves from ``org_settings.skills_factory.forge.*`` at call
    time. Defaults here mirror plan §12 + ``DEFAULT_SETTINGS`` in
    ``organization_settings`` — duplicated literals are
    intentional (decoupled fallback). Production callers pass an
    explicitly-resolved instance.
    """

    min_cluster_size: int = 3
    min_distinct_agents: int = 3
    freshness_window_days: int = 14
    max_writes_per_run: int = 20
    # Entity-Jaccard threshold for cluster membership. 0.4 means a
    # trace joins a cluster if it shares ≥40% of its entities with
    # the cluster's entity union.
    cluster_entity_jaccard_threshold: float = 0.4
    # Max characters of a single memory's content fed into the
    # prompt per trace (head truncation; tail is dropped).
    memory_excerpt_char_cap: int = 600
    # Sentinel size caps — mirror
    # ``org_settings.skills_factory.{body,description}_max_bytes`` so the
    # in-Forge pre-write scan respects per-tenant overrides rather than
    # falling back to Sentinel's module defaults.
    body_max_bytes: int = 40_000
    description_max_bytes: int = 160


# ── Injected callables ────────────────────────────────────────────


LlmFn = Callable[[str], Awaitable[str]]
MemoryFetcher = Callable[[list[str]], Awaitable[dict[str, str]]]
PoisonChecker = Callable[[str], Awaitable[bool]]  # (fingerprint) → True if poisoned
CandidateWriter = Callable[[dict[str, Any]], Awaitable[None]]
# Pre-write existence check: (tenant_id, collection, doc_id) → existing status
# (or None if no doc with that id exists). The Forge writer SKIPS persistence
# when the existing doc's status is anything OTHER than ``candidate`` — that
# prevents a re-mining run from clobbering an already-approved (``active``),
# already-rejected, quarantined, or otherwise human-curated skill. A re-mined
# candidate that lands on top of an existing ``candidate`` IS allowed (and
# expected — that's how Forge refines its own candidates over time).
StatusChecker = Callable[[str, str, str], Awaitable[str | None]]


# ── Run summary ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ForgeRunResult:
    """Returned per call to :func:`run_forge_distill`. The CLI
    (SF-106) and eval harness (SF-105) inspect this directly.

    ``started_at`` is REQUIRED — populated by the caller at the very
    top of :func:`run_forge_distill` before any await. The previous
    shape used ``default_factory=datetime.now`` which fired at
    dataclass-construction time (the END of the run), making
    ``started_at`` indistinguishable from ``finished_at``.
    """

    tenant_id: str
    fleet_id: str | None
    window_start: datetime
    window_end: datetime
    total_traces: int
    labeled_traces: int
    clusters_total: int
    clusters_eligible: int
    candidates_written: int
    # Cluster fingerprint matched a row in ``forge_rejected_fingerprints``
    # that's still inside its cooloff window.
    candidates_skipped_poisoned: int
    # Sentinel pre-scan returned a fatal finding (path violation,
    # hard size cap, etc.). Distinguished from ``poisoned`` so the
    # run summary can answer "is the corpus producing unsafe
    # content?" separately from "is the rejected-fingerprint memory
    # working?".
    candidates_skipped_sentinel: int
    # LLM response failed to parse or violated the Phase-1 invariants
    # (kind='create', valid slug, etc.). Operator action: re-tune the
    # prompt or model.
    candidates_skipped_distill_error: int
    # Transient I/O failures from injected callables (memory_fetcher
    # DB error, poison_checker failure, status_checker DB hiccup,
    # candidate_writer UNIQUE-violation, etc.). Operator action:
    # inspect logs for the underlying exception type. Distinct from
    # distill errors so the run summary actionably surfaces "3 parse
    # errors" vs "2 storage hiccups".
    candidates_skipped_io_error: int
    # Candidates where ``status_checker`` reported the target doc_id
    # already exists with a non-``candidate`` status (active, rejected,
    # quarantined, stale, deprecated). Forge skips the write to avoid
    # clobbering operator-curated state.
    candidates_skipped_existing: int
    started_at: datetime
    run_label: str
    candidate_doc_ids: list[str] = field(default_factory=list)


# ── Public entry point ────────────────────────────────────────────


async def run_forge_distill(
    db: Any,
    *,
    tenant_id: str,
    fleet_id: str | None,
    window_start: datetime,
    window_end: datetime,
    run_label: str,
    llm_fn: LlmFn,
    memory_fetcher: MemoryFetcher,
    poison_checker: PoisonChecker,
    candidate_writer: CandidateWriter,
    status_checker: StatusChecker | None = None,
    config: ForgeConfig | None = None,
) -> ForgeRunResult:
    """One Forge tick. Idempotent against ``session_traces`` (writes
    via :func:`build_session_traces` upsert by run/agent id).
    Idempotent against ``skills`` by virtue of fingerprint-keyed
    skipping — a re-run with the same lake state writes the same
    candidate ids back.

    ``run_label`` is the audit handle for this tick — surfaced as
    ``origin.run_id`` on every candidate doc Forge produces. The
    caller (CLI / scheduler) is responsible for choosing a value
    that uniquely identifies this run (e.g. timestamp + tenant).

    ``status_checker`` (optional) is the no-overwrite guard. When
    supplied, Forge looks up the doc at the candidate's target slug
    BEFORE writing. If something already exists with a status other
    than ``candidate`` (i.e. it's ``active``, ``rejected``,
    ``quarantined``, ``stale``, or ``deprecated``), Forge SKIPS the
    write and increments ``candidates_skipped_existing``. This stops
    a re-mining run from silently clobbering operator-approved or
    operator-rejected state. Re-mining over an existing
    ``candidate`` IS allowed — that's the legitimate refine-the-
    candidate path. None disables the check (e.g. eval harness)."""

    cfg = config or ForgeConfig()
    # Stamp the start of the run BEFORE the first await. Anything
    # later (including the build_session_traces call) will already
    # have done work; using ``datetime.now`` at dataclass-construction
    # time would have read the END of the run, not the start.
    run_started_at = datetime.now(UTC)

    # 1. Build traces (persists into session_traces via SF-102).
    traces = await build_session_traces(
        db,
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        window_start=window_start,
        window_end=window_end,
        persist=True,
    )

    # 2. Drop unlabeled traces — they don't have an outcome to
    # cluster around. Plan §6: outcome_label='unknown' is the
    # explicit "don't know" state.
    labeled = [t for t in traces if t.outcome_label != "unknown"]

    # 3. Cluster by entity Jaccard. Empty / no-entity traces fall
    # out (can't compare set overlap).
    clusters = _cluster_by_entity_overlap(
        labeled,
        jaccard_threshold=cfg.cluster_entity_jaccard_threshold,
    )

    # 4. Gate by volume + diversity.
    eligible = _gate_clusters(
        clusters,
        min_cluster_size=cfg.min_cluster_size,
        min_distinct_agents=cfg.min_distinct_agents,
    )

    # Run-level counters. Split out by failure mode so the run
    # summary is actionable — "3 parse errors" vs "1 Sentinel block"
    # vs "2 storage hiccups" each demand different operator action.
    written_ids: list[str] = []
    skipped_poisoned = 0
    skipped_sentinel = 0
    skipped_distill_error = 0
    skipped_io_error = 0
    skipped_existing = 0

    # 5. Distill each eligible cluster.
    for cluster_traces in eligible[: cfg.max_writes_per_run]:
        try:
            candidate_doc = await _distill_cluster(
                cluster_traces,
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                run_label=run_label,
                window_end=window_end,
                llm_fn=llm_fn,
                memory_fetcher=memory_fetcher,
                poison_checker=poison_checker,
                excerpt_cap=cfg.memory_excerpt_char_cap,
                body_max_bytes=cfg.body_max_bytes,
                description_max_bytes=cfg.description_max_bytes,
            )
        except _PoisonedClusterSkip:
            # Cluster fingerprint matched the cooloff'd reject memory.
            skipped_poisoned += 1
            continue
        except _SentinelFatalSkip:
            # Sentinel pre-scan blocked the cluster on a fatal finding
            # (path violation / hard size cap). Distinct from the
            # poison-memory hit so operators can tell content-shape
            # problems from cooloff hits.
            skipped_sentinel += 1
            continue
        except DistillParseError as exc:
            # LLM-shaped failure: response unparseable / kind != create
            # / malformed slug. Operator action: re-tune the prompt
            # or model.
            logger.warning("forge: skipping cluster — LLM response unparseable: %s", exc)
            skipped_distill_error += 1
            continue
        except Exception:
            # Transient I/O failures inside _distill_cluster
            # (memory_fetcher / poison_checker DB hiccup, entity-
            # lookup partial fail bubbling up). MUST NOT abort the
            # tick. Operator action: inspect logs for the underlying
            # exception type.
            logger.exception("forge: unexpected I/O exception during cluster distill — skipping cluster")
            skipped_io_error += 1
            continue

        # No-overwrite guard: if a doc with this slug already exists
        # with a non-``candidate`` status, the operator (or a prior
        # run / Sentinel / Inbox action) has staked a claim on it.
        # Re-mining must NOT silently overwrite that state.
        if status_checker is not None:
            try:
                existing_status = await status_checker(tenant_id, "skills", candidate_doc["doc_id"])
            except Exception:
                # storage-layer failure → I/O bucket, not parse-error.
                logger.exception(
                    "forge: status_checker raised for slug=%s — skipping write",
                    candidate_doc.get("data", {}).get("slug"),
                )
                skipped_io_error += 1
                continue
            if existing_status is not None and existing_status != "candidate":
                logger.info(
                    "forge: skipping write for slug=%s — existing doc has status=%s (not candidate)",
                    candidate_doc["data"]["slug"],
                    existing_status,
                )
                skipped_existing += 1
                continue

        try:
            await candidate_writer(candidate_doc)
        except Exception:
            # candidate_writer failure (UNIQUE-violation, schema
            # validator hiccup, network drop) → I/O bucket.
            logger.exception(
                "forge: candidate_writer raised for slug=%s — skipping",
                candidate_doc.get("data", {}).get("slug"),
            )
            skipped_io_error += 1
            continue
        # Append the FULL doc_id (``forge/<slug>``), not the bare slug —
        # ``candidate_doc_ids`` is the handle callers (CLI summary,
        # eval harness, Phase 2 inbox query) use to look the row up
        # in the ``documents`` table. A bare slug wouldn't match the
        # (tenant_id, collection, doc_id) primary-key shape.
        written_ids.append(candidate_doc["doc_id"])

    result = ForgeRunResult(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        window_start=window_start,
        window_end=window_end,
        total_traces=len(traces),
        labeled_traces=len(labeled),
        clusters_total=len(clusters),
        clusters_eligible=len(eligible),
        candidates_written=len(written_ids),
        candidates_skipped_poisoned=skipped_poisoned,
        candidates_skipped_sentinel=skipped_sentinel,
        candidates_skipped_distill_error=skipped_distill_error,
        candidates_skipped_io_error=skipped_io_error,
        candidates_skipped_existing=skipped_existing,
        candidate_doc_ids=written_ids,
        started_at=run_started_at,
        run_label=run_label,
    )
    logger.info(
        "forge_run: run_label=%s tenant=%s fleet=%s traces=%d labeled=%d "
        "clusters=%d eligible=%d written=%d poisoned=%d sentinel=%d "
        "distill_errors=%d io_errors=%d existing=%d",
        result.run_label,
        result.tenant_id,
        result.fleet_id,
        result.total_traces,
        result.labeled_traces,
        result.clusters_total,
        result.clusters_eligible,
        result.candidates_written,
        result.candidates_skipped_poisoned,
        result.candidates_skipped_sentinel,
        result.candidates_skipped_distill_error,
        result.candidates_skipped_io_error,
        result.candidates_skipped_existing,
    )
    return result


# ── Clustering ────────────────────────────────────────────────────


def _cluster_by_entity_overlap(
    traces: list[SessionTraceRow],
    *,
    jaccard_threshold: float,
) -> list[list[SessionTraceRow]]:
    """Greedy single-pass clustering. Each trace joins the first
    existing cluster whose entity union overlaps it by ≥
    ``jaccard_threshold``; otherwise starts its own cluster.

    Why greedy (not k-means): k-means needs k chosen upfront, and
    skill clusters are emergent — we don't know how many we want
    until we see the data. Greedy on a single pass is fast,
    deterministic-given-order, and produces tighter clusters than
    k-means when k would be very small.

    Determinism: traces are iterated in their input order — the
    caller (Forge run) gets them in (run_id, agent_id) ascending
    order from the session_trace builder. So the same lake state
    produces the same cluster assignment.

    Traces with no entities are dropped (can't cluster by entity
    overlap if there are none).
    """
    clusters: list[list[SessionTraceRow]] = []
    cluster_entity_sets: list[set[str]] = []

    for trace in traces:
        ents = set(trace.entity_ids)
        if not ents:
            continue

        placed = False
        for idx, cluster_ents in enumerate(cluster_entity_sets):
            inter = len(ents & cluster_ents)
            union = len(ents | cluster_ents)
            if union == 0:
                continue
            if inter / union >= jaccard_threshold:
                clusters[idx].append(trace)
                cluster_entity_sets[idx] |= ents
                placed = True
                break

        if not placed:
            clusters.append([trace])
            cluster_entity_sets.append(set(ents))

    return clusters


def _gate_clusters(
    clusters: list[list[SessionTraceRow]],
    *,
    min_cluster_size: int,
    min_distinct_agents: int,
) -> list[list[SessionTraceRow]]:
    """Drop clusters that don't meet the volume + diversity floors.
    Plan §5 auto-gates: 'volume ≥ N' + 'diversity ≥ M distinct
    agents'. Pre-filters before the LLM call so we don't waste
    tokens on under-evidenced clusters.
    """
    eligible: list[list[SessionTraceRow]] = []
    for cluster in clusters:
        if len(cluster) < min_cluster_size:
            continue
        distinct_agents = {t.agent_id for t in cluster}
        if len(distinct_agents) < min_distinct_agents:
            continue
        eligible.append(cluster)
    return eligible


# ── Per-cluster distill pipeline ──────────────────────────────────


class _PoisonedClusterSkip(Exception):
    """Internal sentinel — raised when the cluster's fingerprint is
    on the poison list. Caught by ``run_forge_distill``.

    Distinct from :class:`_SentinelFatalSkip` so the run summary can
    show "rejected-fingerprint poison memory hits" separately from
    "Sentinel-scanner content blocks" — different operator action
    in each case."""


class _SentinelFatalSkip(Exception):
    """Internal sentinel — raised when the Sentinel pre-scan returns
    a finding marked ``fatal=True`` (path violation, hard size cap,
    etc.). Caught by ``run_forge_distill`` and counted as
    ``candidates_skipped_sentinel`` separately from poisoned-
    fingerprint skips."""


async def _distill_cluster(
    cluster_traces: list[SessionTraceRow],
    *,
    tenant_id: str,
    fleet_id: str | None,
    run_label: str,
    window_end: datetime,
    llm_fn: LlmFn,
    memory_fetcher: MemoryFetcher,
    poison_checker: PoisonChecker,
    excerpt_cap: int,
    body_max_bytes: int = 40_000,
    description_max_bytes: int = 160,
) -> dict[str, Any]:
    """Turn one cluster into a candidate skill doc ready for the
    upsert into ``documents`` (``collection='skills'``)."""

    # Collect all member-memory ids; fetch their content for the prompt.
    all_memory_ids = sorted({mid for trace in cluster_traces for mid in trace.memory_ids})
    contents = await memory_fetcher(all_memory_ids)

    # Build per-trace snapshots.
    snapshots: list[TraceSnapshot] = []
    for trace in cluster_traces:
        excerpts: list[str] = []
        budget = excerpt_cap
        for mid in trace.memory_ids:
            if budget <= 0:
                break
            text = contents.get(mid, "")
            snippet = text[:budget]
            if snippet:
                excerpts.append(snippet)
                budget -= len(snippet)
        snapshots.append(
            TraceSnapshot(
                run_id=trace.run_id,
                agent_id=trace.agent_id,
                outcome_label=trace.outcome_label,
                memory_excerpts=excerpts,
                entity_ids=trace.entity_ids,
                started_at_iso=trace.started_at.isoformat(),
                ended_at_iso=trace.ended_at.isoformat(),
            )
        )

    cluster_inputs = ClusterPromptInputs(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        traces=snapshots,
        # Cap at ENTITY_TOP_K to bound prompt size and to mirror the
        # fingerprint's centrality cut — the LLM sees the same top-K
        # entities the fingerprint stamps, so prompt and fp agree on
        # cluster identity.
        top_entity_ids=sorted({e for trace in cluster_traces for e in trace.entity_ids})[:ENTITY_TOP_K],
        hint_domain=None,
    )

    # LLM call — single round-trip per cluster.
    prompt = build_distill_prompt(cluster_inputs)
    raw_response = await llm_fn(prompt)
    parsed = parse_distill_response(raw_response)

    # Phase 1 invariant: Forge only mints NEW candidates. ``kind='update'``
    # candidates (with hash-binding to an existing live skill) are
    # Phase-4 territory — they need the v2-diff card flow + drift
    # detection that don't ship until then. If a model decides to
    # respond with kind='update' anyway, we'd silently mint a
    # candidate that the validator would later reject for missing
    # ``data.target.target_content_hash``. Bail loud now with a
    # parse error so the cluster is skipped + counted as a distill
    # error rather than producing a malformed doc.
    if parsed.get("kind") != "create":
        raise DistillParseError(
            f"Forge distill: kind must be 'create' in Phase 1, got {parsed['kind']!r}. "
            "kind='update' candidates require hash-binding to a live target — "
            "that flow lands in Phase 4 alongside v2 diff cards."
        )

    # Compute fingerprint from LLM-extracted goal/domain/steps +
    # cluster entities (entity centralities omitted in MVP; SF-105
    # eval harness will measure whether stability is good enough
    # without them).
    fp_inputs = ClusterFingerprintInputs(
        goal_phrase=parsed["goal_phrase"],
        domain=parsed["domain"],
        entity_ids=cluster_inputs.top_entity_ids,
        step_skeleton=parsed["step_skeleton"],
        entity_centralities=None,
    )
    fingerprint = compute_fingerprint(fp_inputs)

    # Poison check — short-circuits any further write.
    if await poison_checker(fingerprint.fp):
        logger.info(
            "forge: cluster fingerprint %s is poisoned; skipping (slug=%s)",
            fingerprint.fp,
            parsed["slug"],
        )
        raise _PoisonedClusterSkip()

    # Assemble the candidate doc ready for memclaw_doc upsert.
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")

    # Forge writes go through the storage client directly (the worker is
    # internal — not an HTTP caller), so the SF-002 validator + Sentinel
    # hook in ``routes/documents.py`` are NOT exercised on this path. We
    # therefore need to stamp ``content_hash`` and ``scan`` ourselves so
    # downstream consumers (hash-binding for kind=update, Inbox-card
    # render of scan findings) see the same shape they'd see on a
    # validator-mediated write.
    content = parsed["content"]
    content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    candidate_doc: dict[str, Any] = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "collection": "skills",
        # Plan §3 §15 doc_id namespacing for Forge candidates.
        "doc_id": f"forge/{parsed['slug']}",
        "data": {
            "name": parsed["name"],
            "slug": parsed["slug"],
            "version": "v1",
            "kind": parsed["kind"],  # always "create" in Phase 1
            "source": "forge",
            "status": "candidate",  # Phase 1 candidates NEVER auto-promote
            "description": parsed["description"],
            "summary": parsed["summary"],
            "domain": parsed["domain"],
            "tags": parsed["tags"],
            "content": content,
            "content_hash": content_hash,
            # Provenance — Inbox card surfaces these in Phase 2.
            "cites": all_memory_ids,
            "goal": parsed["goal"],
            "evidence": parsed["evidence"],
            "cluster_fingerprint": fingerprint.fp,
            "origin": {
                "agent_id": "forge",
                "session_key": None,
                # Stamped from the run_label threaded through
                # run_forge_distill — the CLI / scheduler picks the
                # value (e.g. ``forge-dry-run-acme-20260607T1845``)
                # and we propagate it onto every candidate this tick
                # produces so an operator inspecting the inbox can
                # trace a card back to the Forge invocation that
                # minted it.
                "run_id": run_label,
                "message_id": None,
                # Provenance fields the Phase 2 auto-gate evaluator
                # (skill_lifecycle.evaluate_auto_gates) reads from
                # ``data.origin``. Without these the promoter would
                # fail closed on G1 (volume), G2 (diversity), and G3
                # (freshness), so no Forge candidate would ever
                # auto-promote to ``staged``.
                "cluster_size": len(cluster_traces),
                "distinct_agents": len({t.agent_id for t in cluster_traces}),
                "window_end": window_end.isoformat(),
            },
            "created_at": now_iso,
            "updated_at": now_iso,
            "telemetry": {
                "fires_total": 0,
                "fires_success": 0,
                "fires_failure": 0,
                "last_fired_at": None,
                "utilization_30d": None,
            },
        },
    }

    # Sentinel pre-scan, mirroring the SF-002 validator behavior. If
    # any fatal finding turns up (path violation, hard size cap, etc.)
    # we treat the cluster like a poisoned one — skip the write so the
    # Forge tick doesn't smuggle unsafe content past the inbox gate.
    # Non-fatal critical findings flip the doc to ``status='quarantined'``
    # before persisting, matching the route's behavior for the same
    # condition.
    scan_result = await scan_skill_doc(
        candidate_doc["data"],
        mode="pre-write",
        body_max_bytes=body_max_bytes,
        description_max_bytes=description_max_bytes,
    )
    if scan_result.any_fatal:
        logger.warning(
            "forge: skipping cluster — Sentinel returned fatal finding for slug=%s",
            parsed["slug"],
        )
        # Distinct from _PoisonedClusterSkip so the run summary
        # surfaces "Sentinel blocked content" separately from
        # "rejected-fingerprint cooloff hit" — different operator
        # responses.
        raise _SentinelFatalSkip()
    candidate_doc["data"]["scan"] = scan_result.as_doc_field()
    if scan_result.state == "quarantined":
        candidate_doc["data"]["status"] = "quarantined"
        candidate_doc["data"]["quarantined_at"] = now_iso

    return candidate_doc
