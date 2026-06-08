#!/usr/bin/env python3
"""``memclawctl forge dry-run`` — Phase 1 manual harness (SF-106).

Invokes the Forge resident pipeline against a live MemClaw deployment
for one tenant + fleet and reports the run summary. Candidates always
land with ``status='candidate'`` and are NEVER promoted; the operator
runs this to:

  * Smoke the end-to-end signal → cluster → fingerprint → distill →
    candidate write path.
  * Generate input for the eval harness (SF-105) when measuring
    precision / recall on a real fleet snapshot.
  * Pre-flight a tenant before flipping ``skills_factory.enabled``.

Usage::

    python scripts/forge_dry_run.py \\
        --tenant <tenant_id> \\
        --fleet  <fleet_id> \\
        --window-days 14

The script reads MemClaw connection params from the standard env
(``DATABASE_URL``, ``OPENAI_API_KEY``) — see ``core_api.config``.

This is NOT a production-grade scheduler. The real Forge run flows
through the ``memclaw.lifecycle.forge-distill-requested`` event
(SF-007); the scheduled-tick worker handler lands in Phase 1's
final wiring step alongside the public lifecycle endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

# Imported at top so the fake-LLM fallback (when ``common.llm`` is
# absent) can stamp ``schema_version`` correctly without re-importing
# the distill_prompt module on every fake-LLM call. Cheap import —
# distill_prompt only pulls in stdlib re/json/dataclasses + the
# package's own SignalKind enum.
from core_api.services.forge.distill_prompt import DISTILL_SCHEMA_VERSION

logger = logging.getLogger("forge_dry_run")


# ── CLI ────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="forge_dry_run",
        description="Run one Forge tick against a tenant + fleet window.",
    )
    p.add_argument("--tenant", required=True, help="Tenant id (org_id).")
    p.add_argument("--fleet", default=None, help="Fleet id; omit for tenant-wide scan.")
    p.add_argument(
        "--window-days",
        type=int,
        default=14,
        help="Trailing days to include (default: 14, matching forge.freshness_window_days).",
    )
    p.add_argument(
        "--min-cluster-size",
        type=int,
        default=3,
        help="Override forge.min_cluster_size (default: 3 — demo value).",
    )
    p.add_argument(
        "--min-distinct-agents",
        type=int,
        default=3,
        help="Override forge.min_distinct_agents (default: 3).",
    )
    p.add_argument(
        "--max-writes-per-run",
        type=int,
        default=20,
        help="Cap candidates written per run (default: 20).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the run summary as JSON (default: human-readable).",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="DEBUG-level logs.",
    )
    return p


# ── Runtime wiring ────────────────────────────────────────────────


async def _wire_llm_fn():
    """Resolve a working LLM callable from the project's existing
    provider plumbing. Falls back to a fake-LLM for environments
    without ``common.llm`` available (the run still produces
    candidates — they just have placeholder content the operator
    can use to smoke the rest of the pipeline)."""
    # Lazy import — keeps `--help` working in environments missing
    # the heavy DB/LLM deps. ImportError is the explicit fallback
    # signal: ``common.llm`` not installed (e.g. a packaging variant
    # without the LLM provider chain).
    try:
        from common.llm import (  # type: ignore[import-not-found]
            LLMRequest,
            call_with_fallback,
        )
    except ImportError:
        logger.warning(
            "forge_dry_run: common.llm not importable; falling back to "
            "FAKE-LLM mode — candidates will carry placeholder content. "
            "Install the LLM provider chain (or run from the core-api "
            "package) for real distillation."
        )
        return _fake_llm_fn

    async def llm_fn(prompt: str) -> str:
        # Tenant-resolution and provider chain are handled inside
        # call_with_fallback. We pass the prompt as a single user
        # message; the prompt itself carries the system framing.
        request = LLMRequest(messages=[{"role": "user", "content": prompt}])
        response = await call_with_fallback(request, expecting="json")
        return response.text

    return llm_fn


# Counter for the fake-LLM path so each cluster gets a unique slug
# (the route's slug regex + per-tenant doc_id uniqueness would
# otherwise collide on the second cluster).
_FAKE_LLM_COUNTER = {"n": 0}


async def _fake_llm_fn(_prompt: str) -> str:
    """Deterministic placeholder LLM. Returns a valid JSON response
    that ``parse_distill_response`` will accept. Used when
    ``common.llm`` is not importable (no LLM provider chain
    available) so an operator can still smoke the
    extraction → cluster → fingerprint → write pipeline without
    needing API keys."""
    _FAKE_LLM_COUNTER["n"] += 1
    n = _FAKE_LLM_COUNTER["n"]
    payload = {
        "schema_version": DISTILL_SCHEMA_VERSION,
        "kind": "create",
        "goal_phrase": f"forge dry-run cluster {n} placeholder goal",
        "domain": "general",
        "step_skeleton": [
            "observe cluster shape",
            "review fake placeholder",
            "swap fake llm for real",
        ],
        "name": f"Forge Dry-Run Placeholder {n}",
        "slug": f"forge-dry-run-placeholder-{n}",
        "description": (
            f"Placeholder skill from fake-LLM run #{n}; no real distillation occurred."
        ),
        "summary": (
            "FAKE-LLM placeholder content. Wire common.llm into the run to "
            "produce real skill content from the cluster's session-traces."
        ),
        "content": (
            f"## When to use\nThis is fake-LLM output (run #{n}).\n\n"
            "## Steps\n1. Observe placeholder.\n2. Switch to a real LLM provider.\n"
        ),
        "tags": ["forge", "dry-run", "fake-llm"],
        "evidence": "FAKE-LLM mode — no actual evidence aggregated; placeholder only.",
        "goal": "Smoke the Forge pipeline without an LLM provider chain.",
    }
    return json.dumps(payload)


async def _wire_memory_fetcher(db):
    """Return an async fetcher that loads memory content by id from
    the live database."""
    from sqlalchemy import text

    async def fetcher(memory_ids: list[str]) -> dict[str, str]:
        if not memory_ids:
            return {}
        rows = (
            await db.execute(
                # Cast the PARAMETER (text[] → uuid[]), not the column.
                # ``WHERE id::text = ANY(:ids)`` wraps the column in
                # a function call and disables any btree index on
                # ``memories.id``. Casting the bind variable instead
                # keeps the index usable — same pattern as the bulk
                # entity-id lookup in session_trace._query_entity_ids_for_memories.
                text(
                    "SELECT id::text AS id, content FROM memories "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": list(memory_ids)},
            )
        ).fetchall()
        # NULL-safe: memories.content is nullable, but downstream
        # ``_distill_cluster`` slices the value as a string. Returning
        # None would TypeError and get swallowed into the io_error bucket.
        return {row.id: row.content if row.content is not None else "" for row in rows}

    return fetcher


async def _wire_poison_checker(db, tenant_id: str, fleet_id: str | None):
    """Async fp → bool against the forge_rejected_fingerprints
    table (migration 020). Honors the per-row cooloff_days."""
    from sqlalchemy import text

    async def checker(fp: str) -> bool:
        row = (
            await db.execute(
                text(
                    """
                    SELECT 1 FROM forge_rejected_fingerprints
                    WHERE tenant_id = :tenant_id
                      AND cluster_fingerprint = :fp
                      AND (fleet_id IS NULL OR fleet_id = :fleet_id)
                      AND rejected_at + (interval '1 day' * cooloff_days) > now()
                    LIMIT 1
                    """
                ),
                {"tenant_id": tenant_id, "fleet_id": fleet_id, "fp": fp},
            )
        ).fetchone()
        return row is not None

    return checker


async def _wire_candidate_writer(db):
    """Persist a Forge-generated candidate via the SF-002
    ``memclaw_doc`` write path so all 7 adjustments + Sentinel scan
    + audit fire as they would for an external write."""
    # We use the underlying storage client directly here because the
    # Forge worker is internal (not an HTTP caller); the SF-002
    # validator's checks that pertain to internal callers are
    # already satisfied by the doc shape Forge builds.
    from core_api.clients.storage_client import get_storage_client

    sc = get_storage_client()

    async def writer(candidate_doc: dict) -> None:
        await sc.upsert_document(candidate_doc)

    return writer


async def _wire_status_checker(db):
    """No-overwrite guard wiring. Returns the current ``data.status``
    of an existing doc, or ``None`` if no doc with that id exists.
    Forge skips persistence when the target slug already exists
    with a non-``candidate`` status (operator-curated state)."""
    from core_api.clients.storage_client import get_storage_client

    sc = get_storage_client()

    async def checker(tenant_id: str, collection: str, doc_id: str) -> str | None:
        doc = await sc.get_document(
            tenant_id=tenant_id, collection=collection, doc_id=doc_id
        )
        if not doc:
            return None
        data = doc.get("data") or {}
        return data.get("status")

    return checker


# ── Entry point ───────────────────────────────────────────────────


async def _run(args: argparse.Namespace) -> int:
    # Lazy imports for `--help` ergonomics on dependency-light envs.
    from core_api.db.session import async_session
    from core_api.services.forge.forge_service import (
        ForgeConfig,
        run_forge_distill,
    )

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=args.window_days)

    # Audit handle for this tick — surfaced on every candidate's
    # origin.run_id so an operator can trace an Inbox card back to
    # the Forge invocation that minted it.
    run_label = f"forge-dry-run-{args.tenant}-{window_end.strftime('%Y%m%dT%H%M')}"

    async with async_session() as db:
        llm_fn = await _wire_llm_fn()
        memory_fetcher = await _wire_memory_fetcher(db)
        poison_checker = await _wire_poison_checker(db, args.tenant, args.fleet)
        candidate_writer = await _wire_candidate_writer(db)
        status_checker = await _wire_status_checker(db)

        cfg = ForgeConfig(
            min_cluster_size=args.min_cluster_size,
            min_distinct_agents=args.min_distinct_agents,
            max_writes_per_run=args.max_writes_per_run,
        )

        result = await run_forge_distill(
            db,
            tenant_id=args.tenant,
            fleet_id=args.fleet,
            window_start=window_start,
            window_end=window_end,
            run_label=run_label,
            llm_fn=llm_fn,
            memory_fetcher=memory_fetcher,
            poison_checker=poison_checker,
            candidate_writer=candidate_writer,
            status_checker=status_checker,
            config=cfg,
        )
        # Commit any session_trace upserts + candidate writes.
        await db.commit()

    if args.json:
        payload = {
            "run_label": result.run_label,
            "tenant_id": result.tenant_id,
            "fleet_id": result.fleet_id,
            "window_start": result.window_start.isoformat(),
            "window_end": result.window_end.isoformat(),
            "started_at": result.started_at.isoformat(),
            "total_traces": result.total_traces,
            "labeled_traces": result.labeled_traces,
            "clusters_total": result.clusters_total,
            "clusters_eligible": result.clusters_eligible,
            "candidates_written": result.candidates_written,
            "candidates_skipped_poisoned": result.candidates_skipped_poisoned,
            "candidates_skipped_sentinel": result.candidates_skipped_sentinel,
            "candidates_skipped_distill_error": result.candidates_skipped_distill_error,
            "candidates_skipped_io_error": result.candidates_skipped_io_error,
            "candidates_skipped_existing": result.candidates_skipped_existing,
            "candidate_doc_ids": result.candidate_doc_ids,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"forge_dry_run · run_label={result.run_label} "
            f"tenant={result.tenant_id} fleet={result.fleet_id or '<none>'} "
            f"window=[{result.window_start.isoformat()} → {result.window_end.isoformat()}]"
        )
        print(
            f"  traces:      total={result.total_traces} labeled={result.labeled_traces}"
        )
        print(
            f"  clusters:    total={result.clusters_total} eligible={result.clusters_eligible}"
        )
        print(
            f"  candidates:  written={result.candidates_written} "
            f"poisoned={result.candidates_skipped_poisoned} "
            f"sentinel={result.candidates_skipped_sentinel} "
            f"distill_errors={result.candidates_skipped_distill_error} "
            f"io_errors={result.candidates_skipped_io_error} "
            f"existing={result.candidates_skipped_existing}"
        )
        if result.candidate_doc_ids:
            print("  doc_ids:")
            for slug in result.candidate_doc_ids:
                print(f"    - {slug}")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level CLI handler
        logger.error("forge_dry_run failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
