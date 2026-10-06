"""Measure what landing a ``title`` does to Postgres FTS rank and ordering.

Migration 034 put ``title`` into ``memories.search_vector`` at the same weight as
``content``, and widened the trigger to ``UPDATE OF content, title`` so that a
title-only write rebuilds the row's vector. On the default write path enrichment
is deferred, so the title lands on a background worker after the row exists — the
row's ``search_vector`` therefore changes once, after write, with no schema or
code change involved.

This script measures that one transition on an existing corpus, read-only. For
every row it builds both vectors itself rather than trusting the stored one, so
the answer does not depend on whether the 034 backfill has been run here:

    before  to_tsvector('english', coalesce(content, ''))
    after   to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))

and scores both with the production expression,
``ts_rank_cd(vector, plainto_tsquery('english', query))`` — see
``postgres_service._keyword_rank``.

The question is NOT whether ranks move. Adding matching text to a tsvector can
only raise a rank, so of course they move. The question is whether they move
UNIFORMLY: a constant factor applied to every row reorders nothing and cannot
have changed a benchmark score. So the headline numbers are the spread of the
per-row ratio and the top-50 order agreement, not the mean delta.

Usage::

    python benchmark/c01_title_fts_rank_movement.py --dsn postgresql://... --tenant dev-9ff0ca

Read-only: the connection is opened with ``default_transaction_read_only``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import statistics
from dataclasses import dataclass, field

import asyncpg

# ``before`` and ``after`` as the trigger would build them. Kept as literal SQL
# fragments in ONE place: the whole measurement is meaningless if these drift
# from migration 034's ``_vector()`` / ``_content_only()``.
SV_BEFORE = "to_tsvector('english', coalesce(content, ''))"
SV_AFTER = "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))"

# Production scores with ts_rank_cd + plainto_tsquery (postgres_service.py:2803).
RANK = "ts_rank_cd"

# A query needs terms a user would actually type. Stopwords and short tokens are
# dropped by the 'english' dictionary anyway; filtering here just keeps the
# generated queries from degenerating into empty tsqueries.
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{4,}")
# Chat-transcript filler. "assistant"/"user" are not stopwords in general, but
# every other row in this corpus opens with one, so a query built from them
# matches nothing in particular.
_STOP = frozenset(
    [
        "about", "above", "after", "again", "against", "because", "before", "being",
        "below", "between", "both", "could", "does", "doing", "during", "each",
        "further", "having", "their", "there", "these", "those", "through", "under",
        "until", "where", "which", "while", "would", "should", "assistant", "user",
    ]
)  # fmt: skip

TOP_K = 50
# Below this, an ordering statistic is noise: one swap in a 2-row pool is tau=-1.
MIN_POOL = 10


@dataclass
class QueryResult:
    query: str
    n_before: int = 0
    n_after: int = 0
    n_new: int = 0  # matched only once the title was there
    n_lost: int = 0  # matched before but not after — must be 0
    ratios: list[float] = field(default_factory=list)  # per-row after/before
    overlap_50: int = 0
    overlap_10: int = 0
    eff_k: int = (
        0  # min(TOP_K, pool size) -- overlap_50 out of 50 is meaningless below it
    )
    tau: float | None = None
    tau_n: int = 0
    top1_changed: bool = False
    displacement: list[int] = field(
        default_factory=list
    )  # |rank move| of before-top-K rows


def kendall_tau_b(a: list[float], b: list[float]) -> float | None:
    """Rank correlation between two scorings of the same items, ties included.

    Written out rather than pulled from scipy: scipy is not in this repo's venv
    and one O(n^2) pass over 50 items is not worth a dependency.
    """
    n = len(a)
    if n < 2:
        return None
    concordant = discordant = ties_a = ties_b = 0
    for i in range(n):
        for j in range(i + 1, n):
            da = a[i] - a[j]
            db = b[i] - b[j]
            if da == 0 and db == 0:
                continue
            if da == 0:
                ties_a += 1
            elif db == 0:
                ties_b += 1
            elif (da > 0) == (db > 0):
                concordant += 1
            else:
                discordant += 1
    denom = (
        (concordant + discordant + ties_a) * (concordant + discordant + ties_b)
    ) ** 0.5
    if denom == 0:
        return None
    return (concordant - discordant) / denom


def _terms(text: str, limit: int) -> list[str]:
    seen: list[str] = []
    for m in _WORD.finditer(text or ""):
        w = m.group(0).lower()
        if w in _STOP or w in seen:
            continue
        seen.append(w)
        if len(seen) == limit:
            break
    return seen


async def _sample_queries(
    conn: asyncpg.Connection, tenant: str, source: str, n: int, seed: str, terms: int
) -> list[str]:
    """Draw queries from the corpus itself, deterministically.

    ``source='content'`` is the honest case — a user asks about what the memory
    says, with no knowledge that a title exists. ``source='title'`` is the
    friendly case for the hypothesis. Both are reported; reading only the second
    would be the same mistake that sank pm-0918-c-04.
    """
    col = "content" if source == "content" else "title"
    rows = await conn.fetch(
        f"""
        SELECT {col} AS text FROM memories
        WHERE deleted_at IS NULL AND tenant_id = $1 AND {col} IS NOT NULL
        ORDER BY md5(id::text || $2) LIMIT $3
        """,  # noqa: S608 -- {col} is a 2-value whitelist; user input is $1/$2/$3
        tenant,
        seed,
        n * 3,
    )
    out: list[str] = []
    for r in rows:
        q = " ".join(_terms(r["text"], terms))
        if len(q.split()) >= min(2, terms) and q not in out:
            out.append(q)
        if len(out) == n:
            break
    return out


async def measure(conn: asyncpg.Connection, tenant: str, query: str) -> QueryResult:
    rows = await conn.fetch(
        f"""
        WITH q AS (SELECT plainto_tsquery('english', $2) AS tq),
        pop AS (
            SELECT id, {SV_BEFORE} AS sv_before, {SV_AFTER} AS sv_after
            FROM memories
            WHERE deleted_at IS NULL AND tenant_id = $1
        )
        SELECT pop.id,
               {RANK}(sv_before, q.tq) AS r_before,
               {RANK}(sv_after, q.tq) AS r_after,
               sv_before @@ q.tq AS m_before,
               sv_after @@ q.tq AS m_after
        FROM pop, q
        WHERE sv_before @@ q.tq OR sv_after @@ q.tq
        """,  # noqa: S608 -- SV_BEFORE/SV_AFTER are module constants; query text is $2
        tenant,
        query,
    )
    res = QueryResult(query=query)
    before = [(r["r_before"], str(r["id"])) for r in rows if r["m_before"]]
    after = [(r["r_after"], str(r["id"])) for r in rows if r["m_after"]]
    res.n_before, res.n_after = len(before), len(after)
    before_ids = {i for _, i in before}
    after_ids = {i for _, i in after}
    res.n_new = len(after_ids - before_ids)
    res.n_lost = len(before_ids - after_ids)

    for r in rows:
        if r["m_before"] and r["m_after"] and r["r_before"] > 0:
            res.ratios.append(r["r_after"] / r["r_before"])

    # Ties broken by id so the two orderings differ only where the ranks do.
    before.sort(key=lambda t: (-t[0], t[1]))
    after.sort(key=lambda t: (-t[0], t[1]))
    top_b = [i for _, i in before[:TOP_K]]
    top_a = [i for _, i in after[:TOP_K]]
    res.overlap_50 = len(set(top_b) & set(top_a))
    res.overlap_10 = len(set(top_b[:10]) & set(top_a[:10]))
    res.eff_k = min(TOP_K, len(top_b), len(top_a))
    res.top1_changed = bool(top_b and top_a and top_b[0] != top_a[0])

    # Tau over the rows the before-ranking would have returned, comparing the
    # order they were in to the order they end up in. Rows missing from the
    # after-ordering cannot happen (n_lost is asserted 0 by the caller).
    pos_after = {i: k for k, i in enumerate(i for _, i in after)}
    common = [i for i in top_b if i in pos_after]
    if len(common) >= 2:
        res.tau = kendall_tau_b(
            [float(k) for k in range(len(common))],
            [float(pos_after[i]) for i in common],
        )
        res.tau_n = len(common)
    res.displacement = [
        abs(pos_after[i] - k) for k, i in enumerate(top_b) if i in pos_after
    ]
    return res


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = min(len(s) - 1, max(0, round(p * (len(s) - 1))))
    return s[k]


def report(label: str, results: list[QueryResult]) -> None:
    usable = [r for r in results if r.n_before or r.n_after]
    print(f"\n### {label}  ({len(usable)} queries with any match)")
    if not usable:
        print("  no matches — nothing to report")
        return

    lost = sum(r.n_lost for r in usable)
    print(
        f"  rows matching before / after : {sum(r.n_before for r in usable)} / {sum(r.n_after for r in usable)}"
    )
    print(
        f"  newly eligible (title-only)  : {sum(r.n_new for r in usable)}  (lost: {lost})"
    )

    all_ratios = [x for r in usable for x in r.ratios]
    unchanged = sum(1 for x in all_ratios if abs(x - 1.0) < 1e-9)
    print(
        f"\n  per-row rank ratio after/before, over {len(all_ratios)} rows matching BOTH ways:"
    )
    print(
        f"    unchanged (ratio==1) : {unchanged}  ({100.0 * unchanged / len(all_ratios):.1f}%)"
        if all_ratios
        else "    (none)"
    )
    if all_ratios:
        for p in (0.0, 0.25, 0.5, 0.75, 0.95, 1.0):
            print(f"    p{int(p * 100):<3d}                : {_pct(all_ratios, p):.4f}")
        moved = [x for x in all_ratios if abs(x - 1.0) >= 1e-9]
        if len(moved) >= 2:
            print(
                f"    stdev of ratio       : {statistics.pstdev(all_ratios):.4f}"
                f"   (over movers only: {statistics.pstdev(moved):.4f}, n={len(moved)})"
            )
            print(
                f"    distinct ratio values: {len({round(x, 6) for x in moved})}"
                "   -- >1 is necessary for a reorder, NOT sufficient:"
                " a spread that misses its neighbours still preserves order."
                " The tau below is the answer."
            )

    sizes = sorted(r.n_after for r in usable)
    print(
        f"\n  candidate-pool size per query: p50 {_pct([float(x) for x in sizes], 0.5):.0f},"
        f" p90 {_pct([float(x) for x in sizes], 0.9):.0f}, max {sizes[-1]}"
    )

    # Overlap out of 50 says nothing when the pool is smaller than 50, so it is
    # always reported against the effective K. MIN_POOL keeps the ordering stats
    # off two-row result sets, where a single swap reads as tau = -1.
    for min_pool in (2, MIN_POOL):
        ranked = [r for r in usable if r.n_before >= min_pool and r.n_after >= min_pool]
        if not ranked:
            print(f"\n  ordering (pool >= {min_pool}): no qualifying queries")
            continue
        rates = [r.overlap_50 / r.eff_k for r in ranked if r.eff_k]
        r10 = [r for r in ranked if min(r.n_before, r.n_after) >= 10]
        taus = [(r.tau, r.tau_n) for r in ranked if r.tau is not None]
        disp = [d for r in ranked for d in r.displacement]
        print(f"\n  ordering, {len(ranked)} queries with pool >= {min_pool}:")
        print(
            f"    top-K agreement (K = min(50, pool)) : {statistics.mean(rates):.3f}"
            f"   ({sum(1 for x in rates if x < 1.0)} queries below 1.0)"
        )
        if r10:
            o10 = [r.overlap_10 / 10 for r in r10]
            print(
                f"    top-10 agreement, pools >= 10      : {statistics.mean(o10):.3f}   (n={len(r10)})"
            )
        if taus:
            wmean = sum(t * n for t, n in taus) / sum(n for _, n in taus)
            print(
                f"    Kendall tau-b, size-weighted       : {wmean:.4f}"
                f"   (unweighted {statistics.mean(t for t, _ in taus):.4f},"
                f" below 1.0: {sum(1 for t, _ in taus if t < 0.9999)}/{len(taus)})"
            )
        if disp:
            print(
                f"    |rank displacement| of top-K rows  : mean {statistics.mean(disp):.2f},"
                f" p95 {_pct([float(x) for x in disp], 0.95):.0f}, max {max(disp)}"
            )
        print(
            f"    top-1 result changed               : {sum(1 for r in ranked if r.top1_changed)} / {len(ranked)}"
        )


async def corpus_health(conn: asyncpg.Connection, tenant: str | None) -> None:
    where = "WHERE deleted_at IS NULL" + (" AND tenant_id = $1" if tenant else "")
    args = [tenant] if tenant else []
    row = await conn.fetchrow(
        f"""
        SELECT count(*) AS live,
               count(title) AS titled,
               count(*) FILTER (WHERE search_vector IS DISTINCT FROM {SV_AFTER}) AS stale_vector
        FROM memories {where}
        """,  # noqa: S608 -- SV_AFTER is a module constant; tenant is $1
        *args,
    )
    print(f"\n### corpus ({tenant or 'ALL TENANTS'})")
    print(f"  live rows            : {row['live']}")
    print(
        f"  with a title         : {row['titled']}"
        f"  ({100.0 * row['titled'] / row['live']:.1f}%)"
        if row["live"]
        else ""
    )
    print(
        f"  stored search_vector != title-inclusive vector: {row['stale_vector']}"
        "   (034 backfill not run / pre-034 rows; the measurement does not depend on this)"
    )

    # Did titles land after the row existed? ``memories`` has no updated_at, but
    # the create audit records the title as it was at insert time.
    audit = await conn.fetchrow(
        f"""
        WITH c AS (
            SELECT resource_id, detail->>'title' AS title_at_create
            FROM audit_log
            WHERE resource_type = 'memory' AND action = 'create' AND resource_id IS NOT NULL
        )
        SELECT count(*) AS paired,
               count(*) FILTER (WHERE c.title_at_create IS NULL) AS null_at_create,
               count(*) FILTER (WHERE c.title_at_create IS NULL AND m.title IS NOT NULL) AS landed_later
        FROM c JOIN memories m ON m.id = c.resource_id
        {"AND m.tenant_id = $1" if tenant else ""}
        """,  # noqa: S608 -- SV_AFTER is a module constant; tenant is $1
        *args,
    )
    print(
        f"  create-audit paired  : {audit['paired']}"
        f" (untitled at insert: {audit['null_at_create']},"
        f" titled later: {audit['landed_later']})"
    )


# The trigger demonstration, on a temp table that borrows the real trigger
# function and fire condition. Nothing in the shared corpus is touched: the
# table is ON COMMIT DROP inside a rolled-back transaction.
PROBE_SQL = """
CREATE TEMP TABLE c01_probe (id int, title text, content text, search_vector tsvector) ON COMMIT DROP;
CREATE TRIGGER c01_probe_trg BEFORE INSERT OR UPDATE OF content, title ON c01_probe
    FOR EACH ROW EXECUTE FUNCTION memories_search_vector_update();
INSERT INTO c01_probe (id, title, content) VALUES
    (1, NULL, 'We standardized on Terraform for infrastructure provisioning.'),
    (2, NULL, 'The team migrated telemetry from ClickHouse to TimescaleDB last quarter.');
"""


async def probe(conn: asyncpg.Connection) -> None:
    """Show that a title-only UPDATE rebuilds ``search_vector``, and by how much.

    Two rows, no content change, three states: no title, a title that echoes a
    query term, a title that does not. The echo case is the modal enriched row —
    an LLM title reuses the content's salient words.
    """
    q1 = "terraform provisioning"
    q2 = "telemetry timescaledb"
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(PROBE_SQL)

        async def snap(label: str) -> None:
            rows = await conn.fetch(
                f"SELECT id, {RANK}(search_vector, plainto_tsquery('english', $1)) r1,"  # noqa: S608 -- RANK is a module constant; both queries are bound $1/$2
                f"       {RANK}(search_vector, plainto_tsquery('english', $2)) r2"
                " FROM c01_probe ORDER BY id",
                q1,
                q2,
            )
            cells = "  ".join(
                f"row{r['id']}: {r['r1']:.6f} / {r['r2']:.6f}" for r in rows
            )
            print(f"    {label:<22} {cells}")

        print(
            f"\n### trigger probe — ts_rank_cd against '{q1}' / '{q2}', content never changes"
        )
        await snap("no title")
        await conn.execute(
            "UPDATE c01_probe SET title = 'Standardized Terraform for infrastructure provisioning' WHERE id = 1"
        )
        await conn.execute(
            "UPDATE c01_probe SET title = 'Telemetry migrated to TimescaleDB' WHERE id = 2"
        )
        await snap("echoing title")
        await conn.execute("UPDATE c01_probe SET title = 'Quarterly note' WHERE id = 1")
        await snap("non-echoing title")
        print(
            "    a title-only UPDATE rebuilds the vector; an echoing title raises the rank,"
        )
        print("    a non-echoing one restores it. Different rows, different factors.")
    finally:
        await tx.rollback()


# The local dev database and role literally bear this name, and the tenant
# carrying the titled corpus (dev-9ff0ca) lives in it -- pointing this at
# "caura" would make the default not work. Naming an existing on-disk
# artifact, not minting a new one.
_DEFAULT_DSN = "postgresql://memclaw:changeme@localhost:5432/memclaw"  # legacy-name-floor: live local database name


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=_DEFAULT_DSN)
    ap.add_argument(
        "--tenant",
        default="dev-9ff0ca",
        help="tenant to measure; recall is tenant-scoped",
    )
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--seed", default="c01")
    ap.add_argument(
        "--terms",
        type=int,
        default=3,
        help="terms per generated query; fewer = broader pools",
    )
    args = ap.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        # Read-only except for the probe's ON COMMIT DROP temp table, which is
        # session-local and rolled back. No statement here writes ``memories``.
        await probe(conn)
        await corpus_health(conn, None)
        await corpus_health(conn, args.tenant)
        for source in ("content", "title"):
            qs = await _sample_queries(
                conn, args.tenant, source, args.queries, args.seed, args.terms
            )
            results = [await measure(conn, args.tenant, q) for q in qs]
            if not all(r.n_lost == 0 for r in results):
                raise RuntimeError("a title removed a match — impossible, check SQL")
            report(f"{source}-derived queries (tenant {args.tenant})", results)
            digest = hashlib.sha256("|".join(qs).encode()).hexdigest()[:12]
            print(f"  query-set digest: {digest}  (seed={args.seed}, n={len(qs)})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
