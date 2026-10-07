"""Backfill ``entities.search_vector`` for migration 057 (aliases in the tsvector).

057 changes the trigger so an entity's aliases are indexed whenever its name or
attributes are written. Rows written before it keep a name-only vector: they
still match their names, they just cannot be found by an alias yet. This walks
the table once and rebuilds them.

    python -m core_storage_api.scripts.backfill_057_entity_search_vector --dry-run
    python -m core_storage_api.scripts.backfill_057_entity_search_vector
    python -m core_storage_api.scripts.backfill_057_entity_search_vector --revert

The shape of ``backfill_034_search_vector``, for the same reasons: it lives in
the image so a Cloud Run job can reach it, takes its connection from the
service's own settings via ``get_engine()``, and runs OUT OF BAND after the
storage deploy is healthy, never inside the migration, which is what blocked the
staging deploy on 2026-08-08 when 034 tried it.

Safe to re-run, safe to interrupt, and safe to run while serving: each batch is
its own transaction, and ``search_vector`` is not in the trigger's ``UPDATE OF``
list, so writing it cannot recurse. An interrupted run logs the id to resume
from.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import sqlalchemy as sa

logger = logging.getLogger("backfill_057")

# Must stay byte-identical to migration 057's ``_vector("e.")`` and
# ``_name_only("e.")``, or this writes vectors the trigger never produces.
# Pinned by ``tests/test_backfill_057_entity_search_vector.py``.
_VECTOR = (
    "to_tsvector('english', coalesce(e.canonical_name, '') || ' ' || coalesce(("
    "SELECT string_agg(alias, ' ') FROM json_array_elements_text("
    "CASE WHEN json_typeof(e.attributes->'_aliases') = 'array' THEN e.attributes->'_aliases' "
    "ELSE CAST('[]' AS json) END"
    ") AS a(alias)), ''))"
)
_NAME_ONLY = "to_tsvector('english', coalesce(e.canonical_name, ''))"


async def _run(batch: int, from_id: str | None, dry_run: bool, revert: bool) -> int:
    from core_storage_api.database.init import get_engine

    target = _NAME_ONLY if revert else _VECTOR
    engine = get_engine()

    async with engine.connect() as conn:
        total = (await conn.execute(sa.text("SELECT count(*) FROM entities"))).scalar()
        logger.info("%s rows; mode=%s", total, "revert" if revert else "forward")

        if dry_run:
            # Opt-in whole-table predicate pass: one to_tsvector per row.
            stale = (
                await conn.execute(
                    sa.text(
                        f"SELECT count(*) FROM entities e WHERE e.search_vector IS DISTINCT FROM {target}"
                    )
                )
            ).scalar()
            logger.info("%s need rebuilding", stale)
            return 0

    last, done = from_id, 0
    while True:
        # Windows come from the primary key alone, so choosing the next slice
        # never re-evaluates to_tsvector over rows already handled.
        async with engine.begin() as conn:
            ids = (
                await conn.execute(
                    sa.text(
                        "SELECT id FROM entities "
                        "WHERE (CAST(:last AS uuid) IS NULL OR id > CAST(:last AS uuid)) "
                        "ORDER BY id LIMIT :batch"
                    ),
                    {"last": last, "batch": batch},
                )
            ).fetchall()
            if not ids:
                break
            hi = ids[-1][0]

            # The predicate applies inside the window, so a row without aliases,
            # whose vector 057 does not change, costs a comparison and no write.
            result = await conn.execute(
                sa.text(
                    f"UPDATE entities e SET search_vector = {target} "
                    f"WHERE e.id > COALESCE(CAST(:last AS uuid), "
                    f"'00000000-0000-0000-0000-000000000000'::uuid) "
                    f"AND e.id <= CAST(:hi AS uuid) "
                    f"AND e.search_vector IS DISTINCT FROM {target}"
                ),
                {"last": last, "hi": str(hi)},
            )
            done += result.rowcount
            last = str(hi)
        logger.info("... %s rewritten, cursor %s", done, last)

    logger.info("done: %s rewritten", done)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="core_storage_api.scripts.backfill_057_entity_search_vector")
    p.add_argument("--batch", type=int, default=5000)
    p.add_argument("--from-id", default=None, help="resume cursor logged by an interrupted run")
    p.add_argument("--dry-run", action="store_true", help="count what needs rebuilding, change nothing")
    p.add_argument(
        "--revert",
        action="store_true",
        help="rewrite rows back to the name-only vector, after downgrading 057",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    try:
        sys.exit(asyncio.run(_run(args.batch, args.from_id, args.dry_run, args.revert)))
    except KeyboardInterrupt:
        logger.error("interrupted — re-run with --from-id <the last cursor logged above>")
        sys.exit(130)


if __name__ == "__main__":
    main()
