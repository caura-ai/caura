"""Enrich again the memories whose enrichment fell back to the heuristic.

When every LLM provider fails, enrichment stores ``fake_enrich``'s keyword
heuristic instead: a type and weight from keywords, the first ten words as the
title, the first 200 characters as the summary, and no PII verdict, validity
dates or atomic facts. Nothing on the row says so — the worker drops the
heuristic's ``llm_ms=0`` rather than store it — so the operator brings the list,
taken from the logs of the failure::

    python -m core_api.maintenance.replay_enrichment IDS_FILE            # dry run
    python -m core_api.maintenance.replay_enrichment IDS_FILE --apply

``IDS_FILE`` is a JSON array of ``{"tenant_id": ..., "memory_id": ...}``.

Each memory is enriched the way a fast write is (``_schedule_enrich_or_inline``):
an ``ENRICH_REQUESTED`` for the worker in deferred mode, the background task
inline. What follows is a fresh write's too — the governance verdict, the
atomic-fact fan-out, contradiction detection. What the writer set stays set
(``replay_pins``). In deferred mode, both the worker's PATCH
(``expected_content``) and the ENRICHED consumer refuse a row whose text was
edited after it was read here.

In deferred mode each row is marked ``enrichment_pending`` before its request
is published, as an edit's is, and the worker clears the mark when it lands. A
rerun of the same list while the worker is behind therefore reports those rows
``pending`` and skips them, instead of sending each a second request.

The dry run reads every row and reports what ``--apply`` would do. It enriches
nothing, and neither mode prints a memory's content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from common.constants import LIVE_MEMORY_STATUSES
from common.enrichment.service import fake_enrich
from core_api.clients.storage_client import get_storage_client
from core_api.services.release_replay import release_pins
from core_api.services.system_metadata import caller_owned_keys, extract_system_metadata

logger = logging.getLogger(__name__)

# Outcomes, in the order the summary reports them.
REPLAY = "replay"
MISSING = "missing"
NOT_LIVE = "not_live"
PENDING = "pending"
ENRICHED = "enriched"
NO_CONTENT = "no_content"
ERROR = "error"
OUTCOMES = (REPLAY, MISSING, NOT_LIVE, PENDING, ENRICHED, NO_CONTENT, ERROR)

# A run that cannot enrich five memories in a row has a cause that will fail the
# rest the same way; stopping leaves the remainder for a rerun.
_MAX_CONSECUTIVE_FAILURES = 5


def _pending(value: bool) -> dict:
    """The flag in both homes: a merge cannot delete a key, and the read view
    prefers ``_system`` (C25), so setting or clearing one alone leaves the other
    answering."""
    return {"metadata_patch": {"enrichment_pending": value, "_system": {"enrichment_pending": value}}}


def _publishes_to_worker() -> bool:
    """Deferred mode: the request is queued, and the row waits for the worker.
    Inline, ``_schedule_enrich_or_inline`` returns once enrichment has run."""
    from core_api.config import settings

    return not settings.inline_enrichment


@dataclass(frozen=True)
class Target:
    tenant_id: str
    memory_id: str


def load_targets(path: Path) -> list[Target]:
    """The memories to replay, de-duplicated in file order.

    Raises ``ValueError`` on anything but an array of objects with a string
    ``tenant_id`` and a UUID ``memory_id`` — a half-read list would replay a
    subset with nothing to say which.
    """
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError("expected a JSON array of {tenant_id, memory_id}")
    targets: dict[Target, None] = {}
    for i, entry in enumerate(entries):
        tenant_id = entry.get("tenant_id") if isinstance(entry, dict) else None
        memory_id = entry.get("memory_id") if isinstance(entry, dict) else None
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(memory_id, str):
            raise ValueError(f"entry {i}: needs a string tenant_id and memory_id")
        targets[Target(tenant_id, str(UUID(memory_id)))] = None
    return list(targets)


def replay_pins(memory: dict, system: dict) -> list[str]:
    """The enrichment fields the replay keeps: ``release_pins``, but a title the
    heuristic wrote goes.

    ``release_pins`` keeps any title because on a held row no enrichment has
    run, so a title can only be the writer's. Here the heuristic has run, and
    the worker writes enrichment's title over the writer's, so a title that is
    still ``fake_enrich``'s is enrichment's to replace. Any other title was set
    after it, by an edit, and is kept.
    """
    pins = set(release_pins(memory, system))
    title = memory.get("title")
    if title and title == fake_enrich(memory.get("content") or "").title:
        pins.discard("title")
    return sorted(pins)


def classify(memory: dict | None, system: dict) -> str:
    """What the replay does with one row, before it does anything."""
    if memory is None:
        return MISSING
    if memory.get("status") not in LIVE_MEMORY_STATUSES:
        return NOT_LIVE
    if system.get("enrichment_pending"):
        # Already queued; a second request would race the first.
        return PENDING
    llm_ms = system.get("llm_ms")
    if isinstance(llm_ms, (int, float)) and llm_ms > 0:
        # A real LLM run has reached the row since — an edit re-enriches.
        return ENRICHED
    if not (memory.get("content") or "").strip():
        return NO_CONTENT
    return REPLAY


def _created_at(memory: dict) -> datetime | None:
    """The write's own date, so "next Friday" resolves as it did when written."""
    value = memory.get("created_at")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


async def replay(targets: list[Target], *, apply: bool, per_second: float) -> Counter:
    """Read each target, report its outcome, and with ``apply`` enrich it."""
    from core_api.services.memory_service import _schedule_enrich_or_inline
    from core_api.services.organization_settings import resolve_config

    sc = get_storage_client()
    deferred = _publishes_to_worker()
    outcomes: Counter = Counter()
    pinned: Counter = Counter()
    consecutive_failures = 0
    for target in targets:
        try:
            memory = await sc.get_memory(target.memory_id, target.tenant_id, read=False)
            system = extract_system_metadata((memory or {}).get("metadata_")) or {}
            outcome = classify(memory, system)
        except Exception:
            logger.exception("could not read %s", target.memory_id)
            outcome = ERROR
        if outcome != REPLAY:
            outcomes[outcome] += 1
            print(f"{target.tenant_id} {target.memory_id} {outcome}", flush=True)
            continue

        assert memory is not None  # classify() returned REPLAY
        pins = replay_pins(memory, system)
        pinned.update(pins)
        if not apply:
            outcomes[REPLAY] += 1
            print(f"{target.tenant_id} {target.memory_id} {REPLAY} pins={','.join(pins)}", flush=True)
            continue

        marked = False
        try:
            if deferred:
                # Before the publish, as an edit marks its row: marked after it,
                # a fast worker's clear could land first and leave the row
                # pending for good.
                if await sc.update_memory(target.memory_id, target.tenant_id, _pending(True)) is None:
                    raise LookupError("storage refused the pending mark")
                marked = True
            await _schedule_enrich_or_inline(
                UUID(target.memory_id),
                memory["content"],
                target.tenant_id,
                memory.get("fleet_id"),
                memory.get("agent_id") or "",
                await resolve_config(target.tenant_id),
                agent_provided_fields=pins,
                caller_owned_metadata_keys=sorted(caller_owned_keys(memory.get("metadata_"))) or None,
                reference_datetime=_created_at(memory),
                # As for a fast write: no ``GovernanceDecision`` ran on a
                # verdict, because the heuristic gave none.
                run_governance_remediation=True,
            )
        except Exception:
            logger.exception("could not enrich %s", target.memory_id)
            if marked:
                # No request left, so nothing will clear the mark but this.
                try:
                    await sc.update_memory(target.memory_id, target.tenant_id, _pending(False))
                except Exception:
                    logger.exception("%s is left marked pending; clear it by hand", target.memory_id)
            outcomes[ERROR] += 1
            consecutive_failures += 1
            print(f"{target.tenant_id} {target.memory_id} {ERROR}", flush=True)
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.error("stopping after %d failures in a row", consecutive_failures)
                break
            continue
        consecutive_failures = 0
        outcomes[REPLAY] += 1
        print(f"{target.tenant_id} {target.memory_id} {REPLAY} pins={','.join(pins)}", flush=True)
        await asyncio.sleep(1 / per_second)

    summary = {name: outcomes[name] for name in OUTCOMES}
    print(json.dumps({"apply": apply, "targets": len(targets), "outcomes": summary, "pinned": dict(pinned)}))
    return outcomes


async def _run(args: argparse.Namespace) -> int:
    from core_api.config import bridge_credentials_to_environ
    from core_api.providers._platform import init_platform_providers

    # The same startup core-api runs: inline mode enriches in this process.
    bridge_credentials_to_environ()
    init_platform_providers()

    from common.events import get_event_bus

    try:
        outcomes = await replay(load_targets(args.ids_file), apply=args.apply, per_second=args.per_second)
    finally:
        # Flushes what the publisher still holds: ``publish`` returns before
        # Pub/Sub has the message, and the process exits right after this.
        await get_event_bus().stop()
        await get_storage_client().close()
    return 1 if outcomes[ERROR] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core_api.maintenance.replay_enrichment",
        description="Enrich again memories whose enrichment fell back to the heuristic.",
    )
    parser.add_argument("ids_file", type=Path, help="JSON array of {tenant_id, memory_id}")
    parser.add_argument("--apply", action="store_true", help="enrich; without it, a dry run")
    parser.add_argument(
        "--per-second",
        type=float,
        default=1.0,
        help="enrichments started per second with --apply (default 1)",
    )
    args = parser.parse_args(argv)
    if args.per_second <= 0:
        parser.error("--per-second must be positive")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
