"""Backfill ``memories.search_vector`` for migration 034 (title in the tsvector).

034 changes the trigger so new and updated rows index their title. Rows written
before it keep a content-only vector: they score exactly as they always did —
content lexemes and their weight are unchanged — they just have not gained title
search yet. This walks the table once and rebuilds them.

    python -m core_storage_api.scripts.backfill_034_search_vector --dry-run
    python -m core_storage_api.scripts.backfill_034_search_vector
    python -m core_storage_api.scripts.backfill_034_search_vector --revert

Lives here, in the image, rather than in the repo's top-level ``scripts/``: that
directory is not copied into the container (see ``core-storage-api/Dockerfile``),
so a Cloud Run job could not reach it. Sibling of ``backfill_embeddings.py`` and
``preflight_012.py``, and like them it takes its connection from the service's own
settings via ``get_engine()`` rather than a DSN argument — so a job inherits the
VPC connector, service account and secret bindings of the deployment it runs in.

RUN IT OUT OF BAND, after the storage deploy is healthy. It deliberately does NOT
run inside the migration. The first version of 034 backfilled during the alembic
startup hook and blocked the staging deploy on 2026-08-08: every instance boots,
serialises on the migration advisory lock, and Cloud Run's startup probe kills the
container at 240 s, so the leader never finishes. Committed batches did not save
it — the cursor lives in process memory, so each restart resumed from the top and
re-evaluated one ``to_tsvector`` per row across a growing prefix of already-done
rows, turning a linear job quadratic across restarts.

Safe to re-run, safe to interrupt, and safe to run while serving: each batch is
its own transaction, and ``search_vector`` is not in the trigger's ``UPDATE OF``
list so writing it cannot recurse. An interrupted run prints the id to resume from.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import sqlalchemy as sa

logger = logging.getLogger("backfill_034")

# Must stay byte-identical to migration 034's ``_vector()`` / ``_content_only()``.
# If they diverge this writes vectors the trigger would never produce, and the
# difference is invisible until a search misses. Pinned by
# ``tests/test_skill_schema_v1.py::TestBackfill034MatchesMigration``.
_VECTOR = "to_tsvector('english', coalesce(m.title, '') || ' ' || coalesce(m.content, ''))"
_CONTENT_ONLY = "to_tsvector('english', coalesce(m.content, ''))"


async def _run(batch: int, from_id: str | None, dry_run: bool, revert: bool) -> int:
    from core_storage_api.database.init import get_engine

    target = _CONTENT_ONLY if revert else _VECTOR
    engine = get_engine()

    async with engine.connect() as conn:
        total = (await conn.execute(sa.text("SELECT count(*) FROM memories"))).scalar()
        logger.info("%s rows; mode=%s", total, "revert" if revert else "forward")

        if dry_run:
            # The only whole-table predicate pass, and it is opt-in: it costs a
            # to_tsvector per row, comparable to the rebuild itself on a large
            # table. A real run learns the same thing from its per-batch tallies.
            stale = (
                await conn.execute(
                    sa.text(
                        f"SELECT count(*) FROM memories m WHERE m.search_vector IS DISTINCT FROM {target}"
                    )
                )
            ).scalar()
            logger.info("%s need rebuilding", stale)
            return 0

    last, done = from_id, 0
    while True:
        # Window bounds come from the PRIMARY KEY index alone — no predicate — so
        # picking the next slice is an index scan rather than a scan that
        # re-evaluates to_tsvector over rows already handled. That is the part the
        # in-migration version got wrong.
        async with engine.begin() as conn:
            ids = (
                await conn.execute(
                    sa.text(
                        "SELECT id FROM memories "
                        "WHERE (CAST(:last AS uuid) IS NULL OR id > CAST(:last AS uuid)) "
                        "ORDER BY id LIMIT :batch"
                    ),
                    {"last": last, "batch": batch},
                )
            ).fetchall()
            if not ids:
                break
            hi = ids[-1][0]

            # The predicate applies INSIDE the window, so untitled rows — whose
            # vector 034 does not change — cost a comparison and no write.
            result = await conn.execute(
                sa.text(
                    f"UPDATE memories m SET search_vector = {target} "
                    f"WHERE m.id > COALESCE(CAST(:last AS uuid), "
                    f"'00000000-0000-0000-0000-000000000000'::uuid) "
                    f"AND m.id <= CAST(:hi AS uuid) "
                    f"AND m.search_vector IS DISTINCT FROM {target}"
                ),
                {"last": last, "hi": str(hi)},
            )
            done += result.rowcount
            last = str(hi)
        logger.info("... %s rewritten, cursor %s", done, last)

    # The loop visited every id, so ``done`` is the answer — no second whole-table
    # pass. Rows written while it ran were handled by the trigger.
    logger.info("done: %s rewritten", done)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="core_storage_api.scripts.backfill_034_search_vector")
    p.add_argument("--batch", type=int, default=5000)
    p.add_argument("--from-id", default=None, help="resume cursor printed by an interrupted run")
    p.add_argument("--dry-run", action="store_true", help="count what needs rebuilding, change nothing")
    p.add_argument(
        "--revert",
        action="store_true",
        help="rewrite rows back to the content-only vector, after downgrading 034",
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
