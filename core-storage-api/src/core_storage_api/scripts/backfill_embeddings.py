"""Re-embed memories and entities whose embedding is NULL.

Companion to alembic migration 012_vector_dim_1024. After 012 NULLs every
existing 768-dim embedding (pgvector cannot widen a vector column in
place), this script walks the relevant rows and re-embeds each via the
configured embedding provider — typically the same hosted OpenAI account
the deployment was already using, just now producing 1024-dim vectors via
the SDK's ``dimensions=`` parameter.

Standalone CLI (no event bus, no core-worker required). For OSS docker-
compose deployments this is the recommended eager backfill path. For
enterprise / multi-tenant production cutovers, prefer the event-driven
backfill task in core-worker (see ``local_emb_res/specs/C-backfill-task-pr.md``).

Idempotent and restartable: the WHERE clauses filter to rows with NULL
embeddings, so a partial run can be resumed by simply re-running the
command — already-embedded rows are skipped naturally.

⚠ **Single-provider only — does NOT honour per-tenant embedding configs.**

This CLI calls ``common.embedding.get_embedding(content)`` without a
``tenant_config`` argument, which means every row — regardless of its
``tenant_id`` — is re-embedded against the **process-level** embedding
provider resolved from environment variables (``EMBEDDING_PROVIDER``,
``OPENAI_*``, ``OPENAI_EMBEDDING_*``). On a multi-tenant deployment
where individual tenants have overridden the embedding provider /
model / base_url via ``tenant_config``, this script will silently
re-embed those rows against the wrong provider, producing vectors
that are **inconsistent with the rest of the tenant's data** in the
shared embedding space. Cross-tenant search quality and per-tenant
recall will both degrade.

Threading per-tenant ``tenant_config`` here would require resolving
each row's tenant config (DB lookup or proxy call) inside the embed
loop, which conflicts with the "standalone, no service deps" design
goal of this CLI.

If your deployment uses per-tenant embedding overrides, **stop and
use the event-driven backfill task in core-worker instead**:

    docker compose run --rm core-worker \\
        python -m core_worker.cli backfill-embeddings

That path publishes ``EMBED_REQUESTED`` events and lets the regular
embed worker resolve ``tenant_config`` per row, exactly matching the
hot path. Use the ``--tenant-id`` flag below only to **scope** the
scan to one tenant — it does not change which embedding provider
runs the call.

Usage:
    # Run inside the docker-compose stack so envs are wired up correctly.
    docker compose run --rm core-storage-api \\
        python -m core_storage_api.scripts.backfill_embeddings

    # Dry-run first to estimate scope (does not call OpenAI / write DB):
    docker compose run --rm core-storage-api \\
        python -m core_storage_api.scripts.backfill_embeddings --dry-run

    # Per-tenant phasing for prod cutover safety:
    python -m core_storage_api.scripts.backfill_embeddings --tenant-id tenant-abc

    # CAURA-222 recovery: re-embed memories whose stored vector was
    # produced under the old hint-prefixed write path. Targets rows
    # with non-empty ``metadata.retrieval_hint``; entities are skipped.
    python -m core_storage_api.scripts.backfill_embeddings \\
        --rewrite-hint-prefixed --tenant-id tenant-abc --dry-run

Scope:
- ``memories.embedding`` — re-embedded from ``memories.content``.
- ``entities.name_embedding`` — re-embedded from ``entities.canonical_name``.
- ``documents.embedding`` — NOT handled. Documents store opaque JSON and
  the embed source is fixed to ``data["summary"]`` (with a back-compat
  fallback to ``data["description"]`` for ``collection="skills"``).
  Treat documents as lazy: re-write the doc (no schema change needed —
  the existing ``data["summary"]`` re-embeds on upsert) or use a custom
  script that loads the row's ``data`` and POSTs it back.

Exit codes:
    0  Backfill completed (or dry-run completed).
    1  Configuration error (missing env, DB unreachable, etc).
    2  Embedding provider returned None on too many rows in a row
       (probable degradation; surface and stop).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


# Stop the loop if too many consecutive embed calls return None.
# ``get_embedding`` returns None only after exhausting its retry budget,
# so a streak of Nones means the provider is meaningfully degraded
# rather than blipping. Better to halt and let the operator escalate
# than to spend the next hour writing nothing.
_MAX_CONSECUTIVE_NONES = 20


@dataclasses.dataclass
class _TableSpec:
    table: str
    embedding_column: str
    content_column: str
    # Whether this table soft-deletes rows via a ``deleted_at`` timestamp
    # column. When True, the scan filter excludes soft-deleted rows so we
    # don't waste embed calls (and provider quota) on tombstones — keeps
    # the scope consistent with ``postgres_service.memory_list_null_embedding_rows``
    # and the ``/null-embedding-ids`` endpoint that the event-driven
    # backfill task uses. ``entities`` does not have a ``deleted_at``
    # column.
    has_deleted_at: bool = False
    # JSONB metadata column used by the ``rewrite_hint_prefixed`` scan
    # to filter on ``->>'retrieval_hint'``. ``None`` for tables that
    # don't carry hint metadata (entities) — the hint-rewrite call site
    # asserts this is set so a misconfigured spec fails loudly rather
    # than emitting SQL against a nonexistent column.
    metadata_column: str | None = None


_TARGETS: tuple[_TableSpec, ...] = (
    _TableSpec(
        table="memories",
        embedding_column="embedding",
        content_column="content",
        has_deleted_at=True,
        metadata_column="metadata_",
    ),
    _TableSpec(
        table="entities",
        embedding_column="name_embedding",
        content_column="canonical_name",
    ),
)


@dataclasses.dataclass
class BackfillReport:
    table: str
    scanned: int
    embedded: int
    skipped_empty_content: int
    none_returns: int
    elapsed_s: float


async def _iter_rows(
    engine,
    spec: _TableSpec,
    *,
    tenant_id: str | None,
    batch_size: int,
    rewrite_hint_prefixed: bool,
) -> AsyncIterator[list[tuple[uuid.UUID, str]]]:
    """Yield batches of (id, content) for rows that need (re-)embedding.

    Two scan modes:
      - Default: rows where the embedding is NULL (post-12 backfill,
        also covers any other source of missing vectors).
      - ``rewrite_hint_prefixed=True``: rows whose stored vector was
        produced under the pre-CAURA-222 hint-prefixed write path
        (``"[Retrieval hint]: <hint>\\n\\n<content>"``). Selector is
        ``embedding IS NOT NULL AND metadata_->>'retrieval_hint'`` is
        non-empty. Only ``memories`` carries hint metadata; the
        ``entities`` table is skipped at the call site.

    Cursor-style pagination on ``id`` for stable resumability — the
    consumer's writes flip the row's match condition (NULL → non-NULL,
    or rewrite the embedding while metadata stays put), so on a re-run
    the same page-after-id may yield fewer rows but never duplicates.
    Hint-prefixed mode is the exception: a re-run after a successful
    rewrite would re-match the same rows, since metadata.retrieval_hint
    is intentionally preserved for auditability. The recommended
    operational pattern is a single forward pass per tenant followed
    by the embed-stability probe to verify; see PR description.
    """
    from sqlalchemy import text

    after: uuid.UUID | None = None
    while True:
        params: dict = {"limit": batch_size}
        sql = f"SELECT id, {spec.content_column} FROM {spec.table} WHERE "
        if rewrite_hint_prefixed:
            # Rewrite mode: target rows that were embedded with the
            # pre-CAURA-222 hint prefix. The metadata key is preserved
            # as auditability ground truth — we use it as the selector
            # for which rows need rewriting.
            if spec.metadata_column is None:
                raise ValueError(
                    f"rewrite_hint_prefixed scan requires a metadata column on "
                    f"_TableSpec.table={spec.table!r}; got metadata_column=None"
                )
            sql += (
                f"{spec.embedding_column} IS NOT NULL "
                f"AND {spec.metadata_column} ? 'retrieval_hint' "
                f"AND COALESCE({spec.metadata_column}->>'retrieval_hint', '') <> '' "
            )
        else:
            sql += f"{spec.embedding_column} IS NULL "
        # Skip soft-deleted rows on tables that have ``deleted_at`` —
        # consistent with ``memory_list_null_embedding_rows`` and the
        # event-driven backfill task; otherwise we'd burn provider
        # quota re-embedding tombstones that nothing reads.
        if spec.has_deleted_at:
            sql += "AND deleted_at IS NULL "
        if tenant_id is not None:
            sql += "AND tenant_id = :tenant_id "
            params["tenant_id"] = tenant_id
        if after is not None:
            sql += "AND id > :after "
            params["after"] = after
        sql += "ORDER BY id LIMIT :limit"
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), params)
            rows = result.all()
        if not rows:
            return
        yield [(row[0], row[1]) for row in rows]
        after = rows[-1][0]


async def _backfill_one_table(
    engine,
    spec: _TableSpec,
    *,
    tenant_id: str | None,
    batch_size: int,
    max_inflight: int,
    dry_run: bool,
    rewrite_hint_prefixed: bool,
) -> BackfillReport:
    from sqlalchemy import text

    from common.embedding import get_embedding

    sem = asyncio.Semaphore(max_inflight)
    started = time.monotonic()
    scanned = embedded = skipped_empty = none_returns = 0
    consecutive_nones = 0
    none_lock = asyncio.Lock()

    async def _embed_and_write(row_id: uuid.UUID, content: str | None) -> None:
        nonlocal embedded, skipped_empty, none_returns, consecutive_nones
        if not content:
            # Defensive: ``content`` is NOT NULL on memories but is
            # technically nullable on entities' canonical_name? No,
            # canonical_name is also NOT NULL. Still — empty string is
            # possible, and ``get_embedding("")`` is provider-dependent.
            # Skip rather than ship a degenerate vector.
            skipped_empty += 1
            return
        async with sem:
            if dry_run:
                # Count what would have been done without calling out.
                embedded += 1
                return
            vec = await get_embedding(content)
            if vec is None:
                async with none_lock:
                    none_returns += 1
                    consecutive_nones += 1
                    if consecutive_nones >= _MAX_CONSECUTIVE_NONES:
                        raise RuntimeError(
                            f"Embedding provider returned None on "
                            f"{_MAX_CONSECUTIVE_NONES} consecutive rows; "
                            "stopping. Check OPENAI_API_KEY validity, "
                            "rate-limit headroom, and the registry warnings "
                            "logged at startup."
                        )
                return
            async with none_lock:
                consecutive_nones = 0
            async with engine.connect() as conn:
                # Pass the vector as ``str(vec)`` (Python's list repr —
                # ``'[0.1, 0.2, ...]'``) and let pgvector's input parser
                # cast it on the column-type side. The CLI's deployed
                # asyncpg driver does NOT have the ``register_vector``
                # codec registered (it's only added on connections
                # created via pgvector's helper, not via SQLAlchemy's
                # default async engine factory). Without the codec,
                # asyncpg tries to serialize a ``list[float]`` directly
                # and bails with ``invalid input for query argument $1
                # ... (expected str, got list)``. The text-cast path
                # is what every other write site in this codebase
                # already uses (see ``memory_update_embedding``); this
                # CLI just needs to match.
                #
                # Explicit ``::vector`` cast on the placeholder so
                # PostgreSQL parses the string at server side rather
                # than relying on implicit-cast inference, which would
                # depend on asyncpg's chosen wire-type for the param.
                await conn.execute(
                    text(f"UPDATE {spec.table} SET {spec.embedding_column} = (:emb)::vector WHERE id = :id"),
                    {"emb": str(vec), "id": row_id},
                )
                await conn.commit()
            embedded += 1

    async for batch in _iter_rows(
        engine,
        spec,
        tenant_id=tenant_id,
        batch_size=batch_size,
        rewrite_hint_prefixed=rewrite_hint_prefixed,
    ):
        scanned += len(batch)
        await asyncio.gather(*(_embed_and_write(rid, c) for rid, c in batch))
        logger.info(
            "backfill[%s] progress: scanned=%d embedded=%d empty=%d none=%d",
            spec.table,
            scanned,
            embedded,
            skipped_empty,
            none_returns,
        )

    return BackfillReport(
        table=spec.table,
        scanned=scanned,
        embedded=embedded,
        skipped_empty_content=skipped_empty,
        none_returns=none_returns,
        elapsed_s=time.monotonic() - started,
    )


async def run_backfill(
    *,
    tenant_id: str | None,
    batch_size: int,
    max_inflight: int,
    dry_run: bool,
    only_table: str | None = None,
    rewrite_hint_prefixed: bool = False,
) -> list[BackfillReport]:
    """Walk targeted rows and (re-)embed them according to the selected scan mode.

    Two modes:

    - Default (``rewrite_hint_prefixed=False``): scan rows where
      ``embedding IS NULL`` and embed them from ``content`` /
      ``canonical_name``. This is the post-migration-012 recovery
      path for OSS docker-compose users, and also covers any other
      source of missing vectors (failed inline embeds, etc.).

    - Hint-prefixed rewrite (``rewrite_hint_prefixed=True``): scan
      rows where ``embedding IS NOT NULL`` AND
      ``metadata.retrieval_hint`` is non-empty — rows written under
      the pre-CAURA-222 hint-prefixed write path — and re-embed them
      from raw ``content`` to align with the search-side surface.
      One-off recall recovery after CAURA-222 has deployed; new
      writes already land on the raw-content surface. Entities are
      skipped in this mode (no hint metadata exists there).

    Returns one ``BackfillReport`` per table processed.

    Per-tenant embedding providers are NOT honoured — see the module
    docstring's warning. ``tenant_id`` here scopes the SQL scan, not
    the embedding-provider resolution; every row is embedded against
    the process-level provider (``EMBEDDING_PROVIDER`` env, etc.).
    Multi-tenant deployments with per-tenant overrides must use the
    event-driven backfill task in ``core-worker`` instead.
    """
    from core_storage_api.database.init import get_engine

    engine = get_engine()
    reports: list[BackfillReport] = []
    for spec in _TARGETS:
        if only_table is not None and spec.table != only_table:
            continue
        if rewrite_hint_prefixed and spec.table != "memories":
            # Only memories carry ``metadata.retrieval_hint``; skip
            # other tables silently to keep the CLI a single command.
            logger.info(
                "backfill[%s] skipped under --rewrite-hint-prefixed (no hint metadata on this table)",
                spec.table,
            )
            continue
        logger.info(
            "backfill[%s] starting (tenant=%s, batch=%d, max_inflight=%d, dry_run=%s, mode=%s)",
            spec.table,
            tenant_id,
            batch_size,
            max_inflight,
            dry_run,
            "rewrite-hint-prefixed" if rewrite_hint_prefixed else "null-embedding",
        )
        report = await _backfill_one_table(
            engine,
            spec,
            tenant_id=tenant_id,
            batch_size=batch_size,
            max_inflight=max_inflight,
            dry_run=dry_run,
            rewrite_hint_prefixed=rewrite_hint_prefixed,
        )
        reports.append(report)
        logger.info(
            "backfill[%s] done: scanned=%d embedded=%d empty=%d none=%d elapsed=%.1fs",
            spec.table,
            report.scanned,
            report.embedded,
            report.skipped_empty_content,
            report.none_returns,
            report.elapsed_s,
        )
    return reports


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="core_storage_api.scripts.backfill_embeddings")
    p.add_argument(
        "--tenant-id",
        default=None,
        help=(
            "Restrict the SQL scan to a single tenant id. NOTE: this scopes "
            "the rows scanned; it does NOT switch the embedding provider to "
            "the tenant's per-tenant config. Multi-tenant deployments with "
            "per-tenant provider overrides must use the core-worker "
            "event-driven backfill task instead — see module docstring."
        ),
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows per pagination page. Default 500.",
    )
    p.add_argument(
        "--max-inflight",
        type=int,
        default=50,
        help="Concurrent embed calls. Default 50. Tune down if hitting "
        "OpenAI rate limits, up if rate-limit headroom allows.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows that would be re-embedded; don't call the embedding provider or write to the DB.",
    )
    p.add_argument(
        "--rewrite-hint-prefixed",
        action="store_true",
        help=(
            "Re-embed memories rows that were written under the pre-CAURA-222 "
            "hint-prefixed write path (selector: embedding IS NOT NULL AND "
            "metadata.retrieval_hint is non-empty). Only the ``memories`` "
            "table is processed in this mode; entities are skipped. Use after "
            "the CAURA-222 fix has deployed to recover recall on existing "
            "rows. Combine with --tenant-id and --dry-run for a phased "
            "rollout."
        ),
    )
    p.add_argument(
        "--only-table",
        choices=[s.table for s in _TARGETS],
        default=None,
        help="Limit to a single table (memories or entities). Default: both.",
    )
    p.add_argument(
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

    # Sanity: does an embedding provider key resolve to anything?
    if not os.environ.get("OPENAI_API_KEY") and (os.environ.get("EMBEDDING_PROVIDER", "fake") in ("openai",)):
        logger.error(
            "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is unset. "
            "Set the key or change the provider before running backfill."
        )
        return 1

    # --rewrite-hint-prefixed is intentionally non-idempotent: the
    # selector keys on metadata.retrieval_hint, which the rewrite
    # preserves for auditability, so every re-run will re-match (and
    # re-embed) the same rows. Surface this loudly before a live run
    # so an operator who re-invokes the command doesn't silently burn
    # provider quota on a no-op rewrite. Dry-run is fine — no provider
    # call, no DB write.
    if args.rewrite_hint_prefixed and not args.dry_run:
        print(
            "WARNING: --rewrite-hint-prefixed is NOT idempotent. "
            "metadata.retrieval_hint is preserved as auditability ground "
            "truth, so every re-run will re-match and re-embed the same "
            "rows — burning provider quota on no-op rewrites. Intended "
            "as a single forward pass per tenant; verify scope with "
            "--dry-run first.",
            file=sys.stderr,
        )
        print("Starting in 5 s — press Ctrl-C to abort.", file=sys.stderr)
        # ``await asyncio.sleep`` rather than ``time.sleep`` so we don't
        # block the event loop. Functionally equivalent for the 5s
        # operator grace window — Ctrl-C cancels the sleep on either
        # path and the script exits before any provider call.
        await asyncio.sleep(5)

    try:
        reports = await run_backfill(
            tenant_id=args.tenant_id,
            batch_size=args.batch_size,
            max_inflight=args.max_inflight,
            dry_run=args.dry_run,
            only_table=args.only_table,
            rewrite_hint_prefixed=args.rewrite_hint_prefixed,
        )
    except RuntimeError as e:
        # Reserved for the "degraded provider" abort path (20 consecutive
        # None returns from get_embedding) — tells operator monitoring
        # this is provider-side, not local config.
        logger.error("backfill aborted: %s", e)
        return 2
    except Exception as e:
        # Anything else (DB unreachable, registry misconfig surfacing as
        # ValueError, an asyncio cancellation, etc.) — exit 1 with a
        # stack trace so the failure is debuggable but the script's
        # exit code distinguishes it from the provider-degraded case.
        logger.error(
            "backfill aborted (configuration or unexpected error): %s",
            e,
            exc_info=True,
        )
        return 1

    total_scanned = sum(r.scanned for r in reports)
    total_embedded = sum(r.embedded for r in reports)
    print(
        f"backfill {'dry-run ' if args.dry_run else ''}done: "
        f"scanned={total_scanned} embedded={total_embedded} "
        f"({len(reports)} table(s))"
    )
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
