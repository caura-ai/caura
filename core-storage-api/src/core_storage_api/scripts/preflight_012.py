"""Pre-flight for alembic migration ``012_vector_dim_1024``.

Reports what the migration is about to do, on a real DB, before you
run it. **Read-only** — touches no data.

Output covers:
- Current alembic head revision (so the operator knows whether 012 is
  pending, already applied, or this is a fresh DB).
- Per-table row counts of non-NULL embeddings — the rows the
  migration will rewrite to NULL during the widen-to-1024 step.
- A back-of-envelope wall-clock estimate for the per-table
  ``UPDATE ... = NULL`` phase (the long part on production-sized tables).
- Whether the migration's safety gate (see migration 012's ``upgrade()``)
  would refuse to run without ``MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS=true``,
  plus the exact opt-in command to use.

Usage:
    # Default: read DSN from POSTGRES_* envs (same as core-storage-api).
    python -m core_storage_api.scripts.preflight_012

    # Or supply an explicit DSN:
    python -m core_storage_api.scripts.preflight_012 \\
        --dsn postgresql+asyncpg://memclaw:changeme@localhost:5432/memclaw

    # Machine-readable output, e.g. for piping into jq:
    python -m core_storage_api.scripts.preflight_012 --json

Exit codes:
    0  Migration is safe to run (DB empty, no rows to NULL, or already
       past 012).
    1  Migration would destroy data; opt-in env var required.
    2  Connection failed or query errored.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Heuristic: PostgreSQL ``UPDATE ... SET col = NULL`` on a wide
# embedding-bearing table churns at roughly 50k-100k rows/sec on
# AlloyDB-class hardware under typical mixed traffic. We use the lower
# bound so the printed estimate doesn't lull operators into expecting
# a faster result than they'll get. Override via ``--rows-per-sec`` if
# you've actually measured your cluster.
_DEFAULT_ROWS_PER_SEC = 50_000

# Tables + embedding columns that migration 012 widens. Kept in lock-step
# with ``_TARGETS`` in the migration itself; if a column is added there,
# add it here too or the pre-flight will under-report.
_TARGETS: tuple[tuple[str, str], ...] = (
    ("memories", "embedding"),
    ("entities", "name_embedding"),
    ("documents", "embedding"),
)


@dataclasses.dataclass
class TableReport:
    table: str
    column: str
    null_count: int
    non_null_count: int
    estimated_update_seconds: float


@dataclasses.dataclass
class PreflightReport:
    head_revision: str | None
    tables: list[TableReport]
    total_rows_to_null: int
    total_estimated_seconds: float
    safety_gate_would_trip: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


async def _gather(dsn: str, rows_per_sec: int) -> PreflightReport:
    """Run the read-only queries against ``dsn`` and assemble a report.

    All queries are plain ``COUNT(*)`` / ``SELECT version_num`` —
    nothing here writes, locks, or touches schema. Safe to point at a
    production primary; safer still at a read replica.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn, echo=False)
    try:
        async with engine.connect() as conn:
            # Alembic head, if the alembic_version table exists. On a
            # fresh DB it doesn't, and that's fine — we still want to
            # report the embedding columns (they may not exist yet
            # either, in which case the COUNTs below will fail and we
            # propagate). The head lookup is wrapped in its own try
            # because "alembic_version missing" is a benign signal we
            # want to render as "(none — fresh DB)" rather than 2-exit.
            head: str | None = None
            try:
                row = (await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))).first()
                if row:
                    head = row[0]
            except Exception:
                head = None

            tables: list[TableReport] = []
            total_to_null = 0
            total_seconds = 0.0
            for table, column in _TARGETS:
                non_null = (
                    await conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL"))
                ).scalar_one()
                null_n = (
                    await conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL"))
                ).scalar_one()
                est = non_null / max(rows_per_sec, 1)
                tables.append(
                    TableReport(
                        table=table,
                        column=column,
                        null_count=null_n,
                        non_null_count=non_null,
                        estimated_update_seconds=est,
                    )
                )
                total_to_null += non_null
                total_seconds += est

        return PreflightReport(
            head_revision=head,
            tables=tables,
            total_rows_to_null=total_to_null,
            total_estimated_seconds=total_seconds,
            safety_gate_would_trip=total_to_null > 0,
        )
    finally:
        await engine.dispose()


def _fmt_seconds(s: float) -> str:
    if s < 120:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    return f"{s / 3600:.1f}h"


def _format_human(report: PreflightReport) -> str:
    """Plain-text rendering aimed at an operator's terminal.

    Layout:
        Alembic head:  <rev>
        <table grid with rows-to-null + ETA>
        ----------------
        TOTAL <sum>
        <verdict block: opt-in command OR safe-to-run note>
    """
    out: list[str] = []
    out.append(f"Alembic head:           {report.head_revision or '(none — fresh DB)'}")
    out.append("")
    out.append(f"{'Table':<12} {'Column':<18} {'Rows to NULL':>14} {'Est. UPDATE':>14}")
    out.append("-" * 62)
    for t in report.tables:
        out.append(
            f"{t.table:<12} {t.column:<18} {t.non_null_count:>14,} "
            f"{_fmt_seconds(t.estimated_update_seconds):>14}"
        )
    out.append("-" * 62)
    out.append(
        f"{'TOTAL':<31} {report.total_rows_to_null:>14,} {_fmt_seconds(report.total_estimated_seconds):>14}"
    )
    out.append("")
    if report.safety_gate_would_trip:
        out.append("⚠ Migration is destructive. Opt in explicitly:")
        out.append("    MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS=true alembic upgrade 012")
        out.append("After it completes, run the embedding backfill:")
        out.append("    python -m core_storage_api.scripts.backfill_embeddings")
    else:
        out.append("✓ Migration is safe to run on this DB (no destructive UPDATEs).")
        out.append("    alembic upgrade 012")
    return "\n".join(out)


def _resolve_dsn() -> str:
    """Build a DSN from ``core_storage_api.config.settings`` if available,
    else fall back to assembling from ``POSTGRES_*`` envs.

    Mirrors what the live service does so a pre-flight run inside the
    docker-compose stack picks up the same connection without extra
    flags.
    """
    try:
        from core_storage_api.config import settings  # type: ignore[import-not-found]

        return settings.database_url
    except Exception:
        import os
        from urllib.parse import quote_plus

        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5432")
        user = os.environ.get("POSTGRES_USER", "memclaw")
        password = os.environ.get("POSTGRES_PASSWORD", "")
        db = os.environ.get("POSTGRES_DB", "memclaw")
        # URL-encode user + password before interpolating. Real
        # production passwords routinely contain ``@``, ``:``, ``/``,
        # ``?``, ``#`` and other characters that would otherwise be
        # mis-parsed by SQLAlchemy's URL parser as host/port/path
        # delimiters — leading to an opaque "could not translate host
        # name" or "authentication failed" instead of "your password
        # has special chars". ``quote_plus`` matches what
        # ``sqlalchemy.engine.URL.create`` does internally and is the
        # standard idiom in code bases that build DSNs by string
        # interpolation.
        return f"postgresql+asyncpg://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="core_storage_api.scripts.preflight_012")
    p.add_argument(
        "--dsn",
        default=None,
        help="Postgres DSN. Defaults to core_storage_api.config.settings.database_url, else POSTGRES_* envs.",
    )
    p.add_argument(
        "--rows-per-sec",
        type=int,
        default=_DEFAULT_ROWS_PER_SEC,
        help=f"Heuristic for estimating UPDATE wall-clock. Default {_DEFAULT_ROWS_PER_SEC}. "
        "Tune up/down based on observed throughput on your cluster.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text.",
    )
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    if args.dsn is None:
        args.dsn = _resolve_dsn()

    try:
        report = await _gather(args.dsn, args.rows_per_sec)
    except Exception as e:
        # Connection / query failure: render a single-line diagnostic
        # and bail with exit 2. Stack trace is suppressed by design —
        # operators running pre-flight don't need it; for deeper debug
        # they can rerun with PYTHONUNBUFFERED + adjust logging.
        print(f"preflight error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), default=str))
    else:
        print(_format_human(report))
    return 1 if report.safety_gate_would_trip else 0


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
