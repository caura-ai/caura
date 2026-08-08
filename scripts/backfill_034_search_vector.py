"""Backfill ``memories.search_vector`` for migration 034 (title in the tsvector).

034 changes the trigger so new and updated rows index their title. Rows written
before it keep a content-only vector: they score exactly as they always did —
content lexemes and their weight are unchanged — they just have not gained title
search yet. This walks the table once and rebuilds them.

RUN IT OUT OF BAND, after the storage deploy is healthy. It deliberately does NOT
live in the migration. The first version of 034 backfilled during the alembic
startup hook and blocked the staging deploy on 2026-08-08: every instance boots,
serialises on the migration advisory lock, and Cloud Run's startup probe kills the
container at 240 s, so the leader never finishes. Committed batches did not save
it — the cursor lives in process memory, so each restart resumed from the top and
re-evaluated one ``to_tsvector`` per row across a growing prefix of already-done
rows, turning a linear job quadratic across restarts.

    python scripts/backfill_034_search_vector.py \\
        --dsn postgresql://user:pw@host:5432/db          # or $DATABASE_URL
    python scripts/backfill_034_search_vector.py --from-id <uuid>   # resume
    python scripts/backfill_034_search_vector.py --dry-run          # count only
    python scripts/backfill_034_search_vector.py --revert           # after a 034 downgrade

Safe to re-run, safe to interrupt, and safe to run while serving: each batch is
its own transaction, and the trigger is not in the ``UPDATE OF`` list so writing
``search_vector`` cannot recurse. Interrupting prints the id to resume from.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

# Must stay byte-identical to migration 034's ``_vector()``. If these diverge the
# backfill writes vectors the trigger would not have written, and the difference
# is invisible until a search misses.
_VECTOR = "to_tsvector('english', coalesce(m.title, '') || ' ' || coalesce(m.content, ''))"
# 001's expression, for ``--revert``. Migration 034's ``downgrade()`` restores the
# content-only TRIGGER but deliberately touches no data — reverting rows is the
# same volume of work as the forward pass, so doing it in the migration would
# reintroduce the startup-probe failure this split exists to fix. Without this
# mode a downgrade leaves a split corpus: rows already rebuilt keep matching on
# title terms while rows written after the downgrade do not.
_CONTENT_ONLY = "to_tsvector('english', coalesce(m.content, ''))"


async def _run(dsn: str, batch: int, from_id: str | None, dry_run: bool, revert: bool) -> int:
    target = _CONTENT_ONLY if revert else _VECTOR
    conn = await asyncpg.connect(dsn)
    try:
        total = await conn.fetchval("SELECT count(*) FROM memories")
        print(f"{total} rows; mode={'revert' if revert else 'forward'}", flush=True)
        if dry_run:
            # The only whole-table predicate pass, and it is opt-in: it costs a
            # to_tsvector per row, comparable to the rebuild itself on a large
            # table. A real run learns the same thing from its per-batch tallies.
            stale = await conn.fetchval(
                f"SELECT count(*) FROM memories m WHERE m.search_vector IS DISTINCT FROM {target}"
            )
            print(f"{stale} need rebuilding", flush=True)
            return 0

        last, done = from_id, 0
        while True:
            # Window bounds come from the PRIMARY KEY index alone — no predicate —
            # so picking the next slice is an index scan rather than a scan that
            # re-evaluates ``to_tsvector`` over rows already handled. That is the
            # part the in-migration version got wrong.
            ids = await conn.fetch(
                "SELECT id FROM memories WHERE ($1::uuid IS NULL OR id > $1::uuid) "
                "ORDER BY id LIMIT $2",
                last,
                batch,
            )
            if not ids:
                break
            hi = ids[-1]["id"]

            # The predicate applies INSIDE the window, so untitled rows — whose
            # vector is unchanged by 034 — cost a comparison and no write.
            tag = await conn.execute(
                f"""
                UPDATE memories m SET search_vector = {target}
                 WHERE m.id > COALESCE($1::uuid, '00000000-0000-0000-0000-000000000000'::uuid)
                   AND m.id <= $2::uuid
                   AND m.search_vector IS DISTINCT FROM {target}
                """,
                last,
                hi,
            )
            done += int(tag.split()[-1])
            last = hi
            print(f"  ... {done} rebuilt, cursor {last}", flush=True)

        # The loop visited every id, so ``done`` is the answer — no second
        # whole-table pass. Rows written while it ran were handled by the trigger.
        print(f"done: {done} rewritten", flush=True)
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--from-id", default=None, help="resume cursor printed by an interrupted run")
    ap.add_argument("--dry-run", action="store_true", help="count what needs rebuilding, change nothing")
    ap.add_argument(
        "--revert",
        action="store_true",
        help="rewrite rows back to the content-only vector, after downgrading 034",
    )
    args = ap.parse_args()

    if not args.dsn:
        print("need --dsn or DATABASE_URL", file=sys.stderr)
        return 2
    # asyncpg speaks the bare protocol; strip the SQLAlchemy driver suffix so the
    # service's own DATABASE_URL can be pasted in unchanged.
    dsn = args.dsn.replace("postgresql+asyncpg://", "postgresql://")

    try:
        return asyncio.run(_run(dsn, args.batch, args.from_id, args.dry_run, args.revert))
    except KeyboardInterrupt:
        print("\ninterrupted — re-run with --from-id <the last cursor printed above>", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
