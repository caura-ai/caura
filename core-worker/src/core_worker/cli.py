"""Operator CLI for core-worker tasks.

Usage::

    python -m core_worker.cli backfill-embeddings \
        [--tenant-id ID] [--batch-size N] [--max-inflight N] [--dry-run]

The backfill subcommand drives the existing ``handle_embed_request``
consumer: it scans memories whose ``embedding IS NULL`` (after migration
``012_vector_dim_1024``) and publishes one ``EMBED_REQUESTED`` event per
row. See ``core_worker.backfill`` for the design notes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from common.events.factory import get_event_bus
from common.events.inprocess import InProcessEventBus
from core_worker.backfill import run_embedding_backfill
from core_worker.clients.storage_client import close_storage_client


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="core_worker.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser(
        "backfill-embeddings",
        help="Re-embed memories with NULL embeddings (post-migration-012 recovery).",
    )
    bf.add_argument(
        "--tenant-id",
        required=True,
        help=(
            "Required. Scope the backfill to a single tenant. The "
            "storage-API endpoint refuses un-scoped calls since the OSS "
            "API has no auth middleware. For whole-deployment cutovers, "
            "iterate the tenant list externally and invoke this command "
            "once per tenant — also the documented prod-cutover pattern."
        ),
    )
    bf.add_argument("--batch-size", type=int, default=500)
    bf.add_argument("--max-inflight", type=int, default=100)
    bf.add_argument("--dry-run", action="store_true")
    bf.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    if args.cmd == "backfill-embeddings":
        # Refuse before doing any work if the bus cannot reach a consumer.
        # The backfill's whole output is published ``EMBED_REQUESTED`` events;
        # this process registers no handlers, so on the in-process bus every
        # one of them is delivered to zero subscribers and dropped — and the
        # run still prints ``published=N``, which reads as success. That is the
        # same silent-loss shape as the 2026-06-11 incident below, where a
        # backfill reported 16 published events and none reached the topic.
        #
        # ``--dry-run`` is exempt: it publishes nothing by definition, so it
        # stays usable for counting rows without any bus configured. That
        # exemption is why ``get_event_bus()`` sits INSIDE the condition and
        # after the ``not args.dry_run`` term — it raises on a half-configured
        # pubsub environment (missing GCP_PROJECT_ID or subscription prefix),
        # and the only other call is in the ``finally`` below, deliberately
        # wrapped. Resolving it eagerly would turn a dry run that used to exit
        # 0 into an uncaught traceback.
        if not args.dry_run and isinstance(get_event_bus(), InProcessEventBus):
            print(
                "refusing to run: EVENT_BUS_BACKEND resolves to the in-process bus, "
                "which has no subscriber in this process — every published event "
                "would be dropped and the run would still report published=N. "
                "Set EVENT_BUS_BACKEND=pubsub (plus GCP_PROJECT_ID and "
                "EVENT_BUS_SUBSCRIPTION_PREFIX) in the ENVIRONMENT, or pass "
                "--dry-run to count rows without publishing.",
                file=sys.stderr,
            )
            return 2
        try:
            report = await run_embedding_backfill(
                tenant_id=args.tenant_id,
                batch_size=args.batch_size,
                max_inflight=args.max_inflight,
                dry_run=args.dry_run,
            )
        finally:
            # Drain the event bus BEFORE exiting: publish() is
            # fire-and-forget into the Pub/Sub client's batch queue, and
            # only bus.stop() (→ PublisherClient.stop()) commits the
            # outstanding batches. Without this, a short-lived CLI run
            # exits with its final batch un-transmitted — the backfill
            # reports published=N while zero events reach the topic
            # (observed in prod 2026-06-11: all 16 events of a tenant
            # backfill silently lost).
            #
            # Guarded: get_event_bus() itself can raise (RuntimeError on
            # missing pubsub env vars, ValueError on unknown backend) if
            # the singleton was never constructed during the run — a
            # raise here inside the finally would mask the backfill's
            # original exception, hiding the real failure.
            try:
                await get_event_bus().stop()
            except Exception:
                logging.getLogger(__name__).exception("event bus stop failed; continuing teardown")
            # Close the singleton httpx client so the event-loop exits
            # cleanly. Mirrors the FastAPI lifespan shutdown.
            await close_storage_client()
        print(
            f"backfill {'dry-run ' if args.dry_run else ''}done: "
            f"scanned={report.scanned} published={report.published} "
            f"elapsed={report.elapsed_s:.1f}s"
        )
        return 0
    return 1


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
