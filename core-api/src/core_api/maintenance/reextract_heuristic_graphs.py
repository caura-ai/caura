"""Extract again the entity graphs the regex heuristic made.

When every provider fails, entity extraction stores ``_fake_extract``'s regex
graph instead of the model's: capitalised phrases as entities, every one
``mentioned``, no subject and no relations. From 2026-10-10 to 2026-11-08 Vertex
refused about half of all extraction prompts with a 400 on the seed
(caura#2367). Nothing recorded a 400, so ``background_task_log`` cannot list
those memories, and the hourly re-run sweep never saw them.

Their graphs show it. This walks each tenant's memories created in a window,
newest first, reads their entity links, and extracts again the ones whose graph
has the heuristic's shape::

    python -m core_api.maintenance.reextract_heuristic_graphs --since 2026-10-10T23:00:00Z     # dry run
    python -m core_api.maintenance.reextract_heuristic_graphs --since ... --tenant T --apply

A memory with no subject is extracted again when every link it has is
``mentioned`` (``heuristic``) or it has none (``empty``). A model graph names a
subject, so the first is the heuristic's, or now and then a model graph that
named none. The second is also what a model finds in text that names nothing;
``--skip-empty`` leaves those. Every other memory has the model's graph and is
left alone.

Each runs as ``POST /admin/memories/{id}/re-extract`` does
(``extraction_rerun._rerun``): the row is read again, its links, relations and
orphaned entities are reset, and ``process_entity_extraction`` runs, inline
here. What follows any extraction follows this one: the subject write-back, and
contradiction detection, which may mark older memories outdated. The links are
read again afterwards. A graph that is still all ``mentioned`` means the
providers failed again, and five such failures or errors in a row stop the run.
A rerun finds only what is still left: a memory extracted again has the model's
graph, unless it came back empty.

Only memories created in the window are found. The listing pages by
``created_at`` and carries no edit time, so a memory written before ``--since``
and edited after it is missed. An earlier ``--since`` reaches it, and the graph
test still leaves every model graph alone.

Nor memories written in the last 15 minutes, whatever ``--until`` says. Their
first extraction may still be running, and until it ends they have no links, so
they would read ``empty``. Extracting them now would run beside the worker's
extraction and reset what it is writing. An older memory whose edit is being
extracted reads ``empty`` for the same reason; a run that reaches it then
extracts it a second time.

The dry run extracts nothing. Neither mode prints a memory's content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import zlib
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

from common.constants import LIVE_MEMORY_STATUSES
from core_api.clients.storage_client import get_storage_client
from core_api.services.entity_extraction import EXTRACTION_PROMPT
from core_api.services.entity_extraction_worker import process_entity_extraction
from core_api.tasks import wait_for_tracked_tasks

logger = logging.getLogger(__name__)

# What the scan finds, then what --apply did, in the order the summary reports them.
MODEL = "model"
HEURISTIC = "heuristic"
EMPTY = "empty"
NOT_LIVE = "not_live"
NO_CONTENT = "no_content"
EXTRACTED = "extracted"
HEURISTIC_AGAIN = "heuristic_again"
EMPTY_AGAIN = "empty_again"
GONE = "gone"
ERROR = "error"
# Of the memories to extract again, how many have a prompt whose seed is past
# int32, the ones Vertex refused before caura#2367. Rebuilt from the row as it is
# now, so a type enrichment changed after the write makes it approximate.
SEED_PAST_INT32 = "seed_past_int32"
OUTCOMES = (
    MODEL,
    HEURISTIC,
    EMPTY,
    NOT_LIVE,
    NO_CONTENT,
    EXTRACTED,
    HEURISTIC_AGAIN,
    EMPTY_AGAIN,
    GONE,
    ERROR,
    SEED_PAST_INT32,
)

# A run whose extractions keep failing has a cause that will fail the rest the
# same way; stopping leaves the remainder for a rerun.
_MAX_CONSECUTIVE_FAILURES = 5
_FAILURES = (HEURISTIC_AGAIN, ERROR)
_PAGE = 500
_INT32_MAX = 2**31 - 1
# The scan stops this long before now. With failing providers the worker waits
# up to (60 + 300) s plus jitter before it settles for the heuristic
# (``ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S``), so a memory's first
# extraction can take about 7.5 minutes; this is twice that.
_IN_FLIGHT_GRACE = timedelta(minutes=15)


def graph_shape(subject_entity_id: object, links: list[dict]) -> str:
    """``MODEL``, ``HEURISTIC`` or ``EMPTY``, from a memory's subject and links."""
    if subject_entity_id or any(link.get("role") != "mentioned" for link in links):
        return MODEL
    return HEURISTIC if links else EMPTY


def classify(memory: dict, links: list[dict]) -> str:
    """What the scan makes of one row."""
    if memory.get("status") not in LIVE_MEMORY_STATUSES:
        return NOT_LIVE
    if not (memory.get("content") or "").strip():
        return NO_CONTENT
    return graph_shape(memory.get("subject_entity_id"), links)


def seed_past_int32(memory: dict) -> bool:
    prompt = EXTRACTION_PROMPT.format(
        memory_type=memory.get("memory_type") or "fact", content=memory.get("content") or ""
    )
    return zlib.crc32(prompt.encode("utf-8")) > _INT32_MAX


def _created_at(memory: dict) -> datetime:
    return datetime.fromisoformat(memory["created_at"])


async def _pages(sc, tenant_id: str, since: datetime, until: datetime) -> AsyncIterator[list[dict]]:
    """The tenant's undeleted memories created in [since, until), a page at a time, newest first."""
    cursor: dict = {}
    while True:
        rows = await sc.admin_list_memories(
            {"tenant_id": tenant_id, "sort": "created_at", "order": "desc", "limit": _PAGE, **cursor}
        )
        page = [r for r in rows if since <= _created_at(r) < until]
        if page:
            yield page
        if len(rows) < _PAGE or _created_at(rows[-1]) < since:
            return
        cursor = {"cursor_ts": rows[-1]["created_at"], "cursor_id": rows[-1]["id"]}


async def _scan(
    sc, tenant_id: str, *, since: datetime, until: datetime, skip_empty: bool, counts: Counter
) -> list[str]:
    """The ids of the tenant's memories to extract again, newest first."""
    wanted = (HEURISTIC,) if skip_empty else (HEURISTIC, EMPTY)
    targets: list[str] = []
    async for page in _pages(sc, tenant_id, since, until):
        links = await sc.get_entity_links_for_memories([str(m["id"]) for m in page], tenant_id)
        for memory in page:
            outcome = classify(memory, links.get(str(memory["id"]), []))
            counts[outcome] += 1
            if outcome in wanted:
                targets.append(str(memory["id"]))
                counts[SEED_PAST_INT32] += seed_past_int32(memory)
                print(f"{tenant_id} {memory['id']} {outcome}", flush=True)
    return targets


async def reextract(sc, tenant_id: str, memory_id: str) -> str:
    """Reset one memory's extraction, run it again, and say what came of it."""
    # Read again, as ``extraction_rerun.live_memory`` does: a held row reads as
    # gone, and its release runs extraction itself.
    memory = await sc.get_memory(memory_id, tenant_id, read=False)
    if memory is None or memory.get("deleted_at") is not None:
        return GONE
    await sc.reset_entity_artifacts(tenant_id, memory_id)
    await process_entity_extraction(
        UUID(memory_id),
        tenant_id,
        memory.get("fleet_id"),
        memory.get("agent_id") or "",
        memory.get("content") or "",
        memory.get("memory_type") or "fact",
    )
    # It records its own failures and never raises, so the graph is the answer.
    # The subject is written back from a subject link, so the links alone tell.
    links = (await sc.get_entity_links_for_memories([memory_id], tenant_id)).get(memory_id, [])
    return {MODEL: EXTRACTED, HEURISTIC: HEURISTIC_AGAIN, EMPTY: EMPTY_AGAIN}[graph_shape(None, links)]


async def _reextract_all(
    sc, tenant_id: str, memory_ids: list[str], *, per_second: float, concurrency: int, counts: Counter
) -> bool:
    """Extract each again, ``per_second`` started a second, ``concurrency`` at once.

    Returns whether it stopped on failures in a row.
    """
    slots = asyncio.Semaphore(concurrency)
    running: set[asyncio.Task] = set()
    failures_in_a_row = 0

    async def _one(memory_id: str) -> None:
        nonlocal failures_in_a_row
        try:
            outcome = await reextract(sc, tenant_id, memory_id)
        except Exception:
            logger.exception("could not extract %s again", memory_id)
            outcome = ERROR
        counts[outcome] += 1
        failures_in_a_row = failures_in_a_row + 1 if outcome in _FAILURES else 0
        print(f"{tenant_id} {memory_id} {outcome}", flush=True)
        slots.release()

    for memory_id in memory_ids:
        await slots.acquire()
        # Checked once a slot is free: the extraction that freed it is counted.
        if failures_in_a_row >= _MAX_CONSECUTIVE_FAILURES:
            slots.release()
            break
        # Each extraction starts contradiction detection in a task of its own;
        # starting more extractions than those finish would only queue them.
        await wait_for_tracked_tasks(at_most=concurrency)
        task = asyncio.create_task(_one(memory_id))
        running.add(task)
        task.add_done_callback(running.discard)
        await asyncio.sleep(1 / per_second)
    await asyncio.gather(*running)
    if failures_in_a_row >= _MAX_CONSECUTIVE_FAILURES:
        logger.error("stopping after %d failures in a row", failures_in_a_row)
        return True
    return False


async def _extraction_on(tenant_id: str) -> bool:
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(tenant_id)
    # With the heuristic, or nothing, configured, extracting again makes the same graph.
    heuristic_only = config.entity_extraction_provider in ("fake", "none")
    return bool(config.entity_extraction_enabled) and not heuristic_only


async def run(
    tenants: list[str],
    *,
    since: datetime,
    until: datetime | None,
    apply: bool,
    per_second: float,
    concurrency: int,
    skip_empty: bool,
) -> Counter:
    """Scan each tenant, report what it found, and with ``apply`` extract it again."""
    sc = get_storage_client()
    latest = datetime.now(UTC) - _IN_FLIGHT_GRACE
    window_end = latest if until is None else min(until, latest)
    total: Counter = Counter()
    try:
        for tenant_id in tenants:
            if not await _extraction_on(tenant_id):
                print(json.dumps({"tenant_id": tenant_id, "skipped": "extraction_off"}), flush=True)
                continue
            counts: Counter = Counter()
            targets = await _scan(
                sc, tenant_id, since=since, until=window_end, skip_empty=skip_empty, counts=counts
            )
            stopped = apply and await _reextract_all(
                sc, tenant_id, targets, per_second=per_second, concurrency=concurrency, counts=counts
            )
            print(json.dumps({"tenant_id": tenant_id, "apply": apply, "outcomes": dict(counts)}), flush=True)
            total.update(counts)
            if stopped:
                break
    finally:
        # Contradiction detection may still be running for the last ones;
        # exiting now would lose it.
        await wait_for_tracked_tasks()
    summary = {
        "apply": apply,
        "since": since.isoformat(),
        "until": window_end.isoformat(),
        "tenants": len(tenants),
        "outcomes": {k: total[k] for k in OUTCOMES},
    }
    print(json.dumps(summary))
    return total


def _timestamp(value: str) -> datetime:
    """An ISO 8601 time; one without a zone is UTC."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _run(args: argparse.Namespace) -> int:
    from core_api.config import bridge_credentials_to_environ
    from core_api.providers._platform import init_platform_providers

    # The same startup core-api runs: extraction calls the providers in this process.
    bridge_credentials_to_environ()
    init_platform_providers()

    from common.events import get_event_bus

    try:
        tenants = args.tenant or await get_storage_client().list_active_tenants()
        outcomes = await run(
            tenants,
            since=args.since,
            until=args.until,
            apply=args.apply,
            per_second=args.per_second,
            concurrency=args.concurrency,
            skip_empty=args.skip_empty,
        )
    finally:
        # Flushes what the publisher still holds: ``publish`` returns before
        # Pub/Sub has the message, and the process exits right after this.
        await get_event_bus().stop()
        await get_storage_client().close()
    return 1 if outcomes[ERROR] or outcomes[HEURISTIC_AGAIN] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core_api.maintenance.reextract_heuristic_graphs",
        description="Extract again the entity graphs the regex heuristic made.",
    )
    parser.add_argument(
        "--since",
        type=_timestamp,
        required=True,
        help="earliest created_at to scan (ISO 8601; UTC if no zone)",
    )
    parser.add_argument(
        "--until",
        type=_timestamp,
        help="created_at to stop before (default, and at the latest: 15 minutes ago)",
    )
    parser.add_argument(
        "--tenant", action="append", help="a tenant to scan; repeat for more (default: every active tenant)"
    )
    parser.add_argument("--apply", action="store_true", help="extract again; without it, a dry run")
    parser.add_argument(
        "--per-second",
        type=float,
        default=2.0,
        help="extractions started per second with --apply (default 2)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="extractions at once with --apply (default 4)"
    )
    parser.add_argument("--skip-empty", action="store_true", help="leave memories with no graph at all")
    args = parser.parse_args(argv)
    if args.per_second <= 0:
        parser.error("--per-second must be positive")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.until is not None and args.until <= args.since:
        parser.error("--until must be after --since")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
