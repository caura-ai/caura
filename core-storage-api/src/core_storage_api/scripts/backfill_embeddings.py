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
command — already-embedded rows are skipped naturally. ``--repair-provenance``
is idempotent for the same reason: its own write flips
``embedded_content_hash`` out of the selector. ``--rewrite-hint-prefixed`` is
the exception, and says so at the point of use.

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

    # Repair rows that carry a vector but record nothing about the text it
    # came from. NO OTHER SWEEP CAN REACH THESE: every other backfill,
    # core-worker's included, selects ``embedding IS NULL`` and these rows
    # have one. Defaults to rows created after provenance existed, which is
    # the population that should always be empty.
    python -m core_storage_api.scripts.backfill_embeddings \\
        --repair-provenance --dry-run

    # Widen to rows predating migration 037: tens of thousands of them, one
    # provider call each, undetermined rather than known-damaged. Size it
    # with --dry-run before spending it.
    python -m core_storage_api.scripts.backfill_embeddings \\
        --repair-provenance --include-legacy --dry-run

Scope:
- ``memories.embedding`` — re-embedded from ``memories.content``, and
  ``memories.embedded_content_hash`` stamped in the same statement with the
  ``content_hash`` read alongside that content. Without the stamp this sweep
  moved rows out of ``embedding IS NULL`` — the population both backfills
  scan — and into ``embedding IS NOT NULL AND embedded_content_hash IS
  NULL``, which nothing scans, so the row silently left every repair path
  and the staleness detector at once.
- ``entities.name_embedding`` — re-embedded from ``entities.canonical_name``.
  No provenance stamp: migration 037 added ``embedded_content_hash`` to
  ``memories`` only, and ``entities`` carries no content hash to record.
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
import enum
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from typing import NamedTuple

logger = logging.getLogger(__name__)


# Stop the loop if too many consecutive embed calls return None.
# ``get_embedding`` returns None only after exhausting its retry budget,
# so a streak of Nones means the provider is meaningfully degraded
# rather than blipping. Better to halt and let the operator escalate
# than to spend the next hour writing nothing.
_MAX_CONSECUTIVE_NONES = 20


class _ScanMode(enum.StrEnum):
    """Which population a run targets. Exactly one, by construction.

    A pair of booleans once the third mode arrived, and that admits a state
    ("rewrite the hint prefix AND repair provenance") with no meaning and no
    branch. The values double as the log and CLI spelling, which is what the
    mode was already being rendered as by hand.
    """

    #: Rows with no vector. The post-migration-012 recovery path, and the
    #: catch-all for any other source of missing embeddings.
    NULL_EMBEDDING = "null-embedding"
    #: Rows embedded under the pre-CAURA-222 hint-prefixed write path.
    REWRITE_HINT_PREFIXED = "rewrite-hint-prefixed"
    #: Rows carrying a vector that records nothing about the text it came
    #: from. Invisible to every other sweep precisely because they HAVE an
    #: embedding, which is what makes this mode the only way to reach them.
    REPAIR_PROVENANCE = "repair-provenance"


class _Provenance(NamedTuple):
    """Which columns carry a table's embedding provenance.

    A pair rather than two optional fields on ``_TableSpec``, so "one set and
    the other not" cannot be written down. Half a pair silently disables
    stamping, which is the exact failure this records against — a sweep that
    writes vectors and attests nothing. Unrepresentable beats validated.
    """

    source_hash_column: str  # read alongside the content
    stamp_column: str  # the value read is written back here


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
    # Which text each stored vector was built from. ``None`` for tables that
    # cannot record it: migration 037 added ``embedded_content_hash`` to
    # ``memories`` alone, and ``entities`` has neither that column nor a
    # content hash to attest — emitting the assignment there would fail with
    # an undefined-column error on every write.
    provenance: _Provenance | None = None


_TARGETS: tuple[_TableSpec, ...] = (
    _TableSpec(
        table="memories",
        embedding_column="embedding",
        content_column="content",
        has_deleted_at=True,
        # The DATABASE column is ``metadata``. ``metadata_`` is the Python
        # attribute name on the ORM model — ``mapped_column("metadata", JSONB)``
        # renames it because ``metadata`` collides with SQLAlchemy's own
        # ``Base.metadata``. This script emits raw SQL, so it needs the
        # database's name. Carrying the ORM's spelling here meant
        # ``--rewrite-hint-prefixed`` raised UndefinedColumnError on its first
        # statement and had therefore never completed a run.
        metadata_column="metadata",
        provenance=_Provenance("content_hash", "embedded_content_hash"),
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
    mode: _ScanMode,
    include_legacy: bool = False,
) -> AsyncIterator[list[tuple[uuid.UUID, str, str | None]]]:
    """Yield batches of (id, content, content_hash) for rows that need (re-)embedding.

    ``content_hash`` is the provenance source — the hash of the text this
    row holds — and is ``None`` for tables that don't carry it (entities).
    It is read here, in the same statement as the content, on purpose: see
    ``_embed_and_write`` for why the value must travel with the text rather
    than being re-read at write time.

    Three scan modes, one selector each — see :class:`_ScanMode`.

    ``REPAIR_PROVENANCE`` requires ``content_hash IS NOT NULL``, and that term
    is load-bearing rather than tidy. The repair writes
    ``embedded_content_hash`` FROM the row's ``content_hash``, so a row without
    one is stamped NULL again and still matches the selector on the next run —
    the sweep would re-embed it forever, spending a provider call each time to
    change nothing. Every other mode is self-terminating because its own write
    flips the row out of its predicate; this is the one that has to be told.

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

    # Deferred like every other import in this module — the CLI keeps its
    # startup light and avoids importing the service layer it deliberately
    # does not depend on — but hoisted out of the pagination loop below, which
    # would otherwise re-resolve it once per page.
    #
    # Imported rather than restated: the alert in core-operations counts the
    # population this sweep repairs, and a second copy of the cutoff would let
    # the two disagree about which rows are a defect.
    from core_storage_api.services.postgres_service import PROVENANCE_REQUIRED_FROM

    after: uuid.UUID | None = None
    while True:
        params: dict = {"limit": batch_size}
        selected = f"id, {spec.content_column}"
        if spec.provenance:
            selected += f", {spec.provenance.source_hash_column}"
        sql = f"SELECT {selected} FROM {spec.table} WHERE "
        if mode is _ScanMode.REWRITE_HINT_PREFIXED:
            # Rewrite mode: target rows that were embedded with the
            # pre-CAURA-222 hint prefix. The metadata key is preserved
            # as auditability ground truth — we use it as the selector
            # for which rows need rewriting.
            if spec.metadata_column is None:
                raise ValueError(
                    f"rewrite_hint_prefixed scan requires a metadata column on "
                    f"_TableSpec.table={spec.table!r}; got metadata_column=None"
                )
            # No ``? 'retrieval_hint'`` existence test. ``?`` is jsonb-only and
            # this column is ``json`` (the migration chain creates it that way
            # regardless of the model declaring JSONB), so it raised
            # UndefinedFunctionError: operator does not exist: json ? unknown.
            # It was also redundant — the COALESCE below already excludes an
            # absent key: ``->>`` yields NULL for a missing key and for a NULL
            # metadata, which coalesces to '' and fails the ``<> ''`` test.
            # One clause, correct on both json and jsonb.
            sql += (
                f"{spec.embedding_column} IS NOT NULL "
                f"AND COALESCE({spec.metadata_column}->>'retrieval_hint', '') <> '' "
            )
        elif mode is _ScanMode.REPAIR_PROVENANCE:
            # Reachable by no other sweep: both embedding backfills and
            # core-worker's event-driven task select ``embedding IS NULL``,
            # and these rows have one. That is why 241 of them sat unrepaired
            # for a week while three repair paths ran over the same table.
            if spec.provenance is None:
                raise ValueError(
                    f"repair_provenance scan requires provenance columns on "
                    f"_TableSpec.table={spec.table!r}; got provenance=None"
                )
            sql += (
                f"{spec.embedding_column} IS NOT NULL "
                f"AND {spec.provenance.stamp_column} IS NULL "
                # Idempotency, not tidiness — see the docstring.
                f"AND {spec.provenance.source_hash_column} IS NOT NULL "
            )
            if not include_legacy:
                # Default to the population that should be empty. Older rows
                # predate provenance entirely: repairing them is a provider
                # spend decision about tens of thousands of rows, not a defect
                # being cleaned up, so it takes an explicit flag.
                sql += "AND created_at >= :provenance_cutoff "
                params["provenance_cutoff"] = PROVENANCE_REQUIRED_FROM
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
        yield [(row[0], row[1], row[2] if spec.provenance else None) for row in rows]
        after = rows[-1][0]


async def _backfill_one_table(
    engine,
    spec: _TableSpec,
    *,
    tenant_id: str | None,
    batch_size: int,
    max_inflight: int,
    dry_run: bool,
    mode: _ScanMode,
    include_legacy: bool = False,
) -> BackfillReport:
    from sqlalchemy import text

    from common.embedding import get_embedding

    sem = asyncio.Semaphore(max_inflight)
    started = time.monotonic()
    scanned = embedded = skipped_empty = none_returns = 0
    consecutive_nones = 0
    none_lock = asyncio.Lock()

    async def _embed_and_write(row_id: uuid.UUID, content: str | None, content_hash: str | None) -> None:
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
            vec = await get_embedding(content, background=True)
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
                #
                # Provenance is written in the SAME statement as the vector.
                # Writing the vector alone is what made this script a
                # producer of the very rows it exists to repair: it moved
                # them out of ``embedding IS NULL`` (which both backfills
                # scan) and into ``embedding IS NOT NULL AND
                # embedded_content_hash IS NULL``, which nothing scans at
                # all. The row left every repair path and the staleness
                # detector in one write, silently.
                #
                # ``:ch`` is the hash READ ALONGSIDE THE CONTENT, not a
                # SQL-side ``= content_hash``. That distinction is the whole
                # correctness argument. A content update landing in the
                # fetch -> embed -> write window would leave the row's
                # ``content_hash`` describing text this vector was not built
                # from, and a self-referential assignment would stamp that
                # new hash onto the old vector — recording the row as
                # freshly embedded at the exact moment it went stale. Naming
                # the value we embedded keeps the column's promise true:
                # afterwards ``embedded_content_hash != content_hash``, and
                # the staleness detector correctly picks the row up.
                # ``memory_update_embedding`` refuses the same re-read for
                # the same reason.
                #
                # A NULL ``content_hash`` writes NULL, deliberately. Unknown
                # provenance is honest; a guessed hash reads downstream as
                # verified freshness and is worse than the NULL it replaced.
                # Under ``--rewrite-hint-prefixed`` this can CLEAR an
                # existing stamp, which is also correct: that stamp
                # described the hint-prefixed vector being overwritten here,
                # so keeping it would assert freshness for a vector that no
                # longer exists.
                assignments = f"{spec.embedding_column} = (:emb)::vector"
                params: dict = {"emb": str(vec), "id": row_id}
                if spec.provenance:
                    assignments += f", {spec.provenance.stamp_column} = :ch"
                    params["ch"] = content_hash
                await conn.execute(
                    text(f"UPDATE {spec.table} SET {assignments} WHERE id = :id"),
                    params,
                )
                await conn.commit()
            embedded += 1

    async for batch in _iter_rows(
        engine,
        spec,
        tenant_id=tenant_id,
        batch_size=batch_size,
        mode=mode,
        include_legacy=include_legacy,
    ):
        scanned += len(batch)
        await asyncio.gather(*(_embed_and_write(rid, c, ch) for rid, c, ch in batch))
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
    mode: _ScanMode = _ScanMode.NULL_EMBEDDING,
    include_legacy: bool = False,
) -> list[BackfillReport]:
    """Walk targeted rows and (re-)embed them according to the selected scan mode.

    Three modes:

    - ``NULL_EMBEDDING`` (default): rows where ``embedding IS NULL``,
      embedded from ``content`` / ``canonical_name``. The
      post-migration-012 recovery path, and the catch-all for any other
      source of missing vectors.

    - ``REWRITE_HINT_PREFIXED``: rows where ``embedding IS NOT NULL`` AND
      ``metadata.retrieval_hint`` is non-empty — written under the
      pre-CAURA-222 hint-prefixed path — re-embedded from raw ``content``
      to match the search-side surface. Entities are skipped (no hint
      metadata there).

    - ``REPAIR_PROVENANCE``: rows carrying a vector that records nothing
      about the text it came from. **The only sweep that can see them.**
      The other two, and core-worker's event-driven backfill, all select
      ``embedding IS NULL``; these rows have an embedding, which is why 241
      of them sat unrepaired for a week in 2026-09 while three separate
      repair paths ran over the same table.

      The repair is a genuine re-embed, never a stamp. Writing
      ``embedded_content_hash = content_hash`` here would assert that the
      EXISTING vector describes the current text — which is exactly what
      nothing knows, because that is the state being repaired. It happened
      to be true for those 241 rows (each verified first against a
      recomputed hash) and is not true in general; a wrong hash reads
      downstream as verified freshness, worse than the NULL it replaced.
      Re-embedding makes the assertion true rather than assuming it.

      ``include_legacy`` widens the scan past ``PROVENANCE_REQUIRED_FROM``
      to rows predating provenance entirely. Off by default: those are
      undetermined rather than damaged, there are tens of thousands of
      them, and each costs a provider call.

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
        if mode is _ScanMode.REPAIR_PROVENANCE and spec.provenance is None:
            # Nothing to repair where nothing can be recorded: migration 037
            # gave ``embedded_content_hash`` to ``memories`` alone. Skipped
            # rather than raised so the CLI stays one command over both tables.
            logger.info(
                "backfill[%s] skipped under --repair-provenance (table records no provenance)",
                spec.table,
            )
            continue
        if mode is _ScanMode.REWRITE_HINT_PREFIXED and spec.table != "memories":
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
            mode.value,
        )
        report = await _backfill_one_table(
            engine,
            spec,
            tenant_id=tenant_id,
            batch_size=batch_size,
            max_inflight=max_inflight,
            dry_run=dry_run,
            mode=mode,
            include_legacy=include_legacy,
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
    modes = p.add_mutually_exclusive_group()
    modes.add_argument(
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
    modes.add_argument(
        "--repair-provenance",
        action="store_true",
        help=(
            "Re-embed memories that carry a vector but record nothing about "
            "the text it came from (embedding IS NOT NULL AND "
            "embedded_content_hash IS NULL). No other sweep can reach these "
            "rows: every other backfill selects embedding IS NULL and these "
            "have one. Entities are skipped (no provenance columns). Rows with "
            "no content_hash are skipped too — there is nothing to record for "
            "them, and including them would re-embed the same rows on every "
            "run forever. Defaults to rows created after provenance existed; "
            "see --include-legacy."
        ),
    )
    p.add_argument(
        "--include-legacy",
        action="store_true",
        help=(
            "With --repair-provenance, also scan rows predating migration 037 "
            "(2026-08-16). Those are undetermined rather than damaged, there "
            "are tens of thousands of them, and each costs a provider call to "
            "re-embed — so widening the scan is a spend decision. Size it with "
            "--dry-run first."
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
    if args.rewrite_hint_prefixed:
        mode = _ScanMode.REWRITE_HINT_PREFIXED
    elif args.repair_provenance:
        mode = _ScanMode.REPAIR_PROVENANCE
    else:
        mode = _ScanMode.NULL_EMBEDDING

    if args.include_legacy and mode is not _ScanMode.REPAIR_PROVENANCE:
        # Ignoring it silently would let an operator believe they had widened
        # a scan that never reads the flag.
        logger.error("--include-legacy only applies to --repair-provenance")
        return 1

    # Widening past migration 037 is a spend decision, not a bug fix: those
    # rows are undetermined rather than damaged, there are tens of thousands
    # of them, and each costs a provider call. Say so before spending it.
    if args.repair_provenance and args.include_legacy and not args.dry_run:
        print(
            "WARNING: --include-legacy re-embeds rows that predate provenance "
            "(migration 037, 2026-08-16). These are UNDETERMINED, not known to "
            "be damaged, and there are tens of thousands of them — one provider "
            "call each. Run with --dry-run first to size it.",
            file=sys.stderr,
        )
        print("Starting in 5 s — press Ctrl-C to abort.", file=sys.stderr)
        await asyncio.sleep(5)

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
            mode=mode,
            include_legacy=args.include_legacy,
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
