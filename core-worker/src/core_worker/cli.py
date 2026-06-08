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
        try:
            report = await run_embedding_backfill(
                tenant_id=args.tenant_id,
                batch_size=args.batch_size,
                max_inflight=args.max_inflight,
                dry_run=args.dry_run,
            )
        finally:
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
