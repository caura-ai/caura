"""First-stage blend A/B on LoCoMo — does rescaling ``fts_score`` retrieve better?

The companion to ``benchmark_rerank_locomo.py``, one stage earlier. That harness
holds a cosine-selected pool fixed and asks whether a reranker orders it better,
so recall is constant by construction. This one varies the FIRST-STAGE score, so
which turns are retrieved at all is the thing that moves — and recall is therefore
the headline metric rather than an invariant.

What is under test is the blend in ``postgres_service._execute_scored_search``:

    similarity = (1 - fts_weight) * vec_sim + fts_weight * fts_score

``fts_score`` today is ``ts_rank_cd / (1 + ts_rank_cd)``. Because
``memories.search_vector`` is built by migration 001 as a bare
``to_tsvector('english', content)`` with no ``setweight``, every lexeme carries
weight D = 0.1, so a typical single-occurrence match scores ~0.0909 while cosine
on the same corpus runs far higher. The nominal ``FTS_WEIGHT`` therefore does not
survive the difference in dynamic range. See
``HANDOFF-2026-08-04-fts-score-scale-normalisation.md``.

    corpus      one LoCoMo conversation's dialogue turns
    baseline    blend using today's fts_score = r/(1+r)
    treatment   blend using (k*r)/(1+k*r)  — --k scales before saturation
    truth       the QA item's ``evidence`` turn ids (binary relevance)
    ranking     the WHOLE corpus, both arms — no fixed pool

Both arms use identical embeddings and identical ``ts_rank_cd`` values; the only
difference is the scalar map applied to the rank. ``--k 1`` makes the treatment
mathematically identical to the baseline, which the harness asserts produces
exactly zero delta — a self-check that the plumbing isn't inventing differences.

Unlike the rerank harness this needs a **Postgres**, because ``ts_rank_cd`` is
what it is measuring and reimplementing Postgres FTS in Python would measure
something else. Any empty database works; the script creates a temp table per
conversation and never touches application tables.

    python scripts/benchmark_blend_locomo.py \\
        --dataset locomo10.json \\
        --embed-url http://localhost:8080 \\
        --pg-dsn postgresql://memclaw:changeme@localhost:5433/memclaw

Caveat to carry into any conclusion: this measures the blend in isolation. The
real first-stage additionally applies freshness decay, weight blending and recall
boost, and then a candidate LIMIT. Those are held out on purpose — they are
row-age dependent and would add variance unrelated to the scale question — so this
is not the end-to-end pipeline delta.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _locomo_bench import (  # noqa: E402  (path shim above must run first)
    CATEGORIES,
    embed,
    load_locomo,
    mrr,
    ndcg,
    paired_stats,
    recall,
    unit,
)

# Recall first: it is the question this harness exists to answer, and the one the
# ranking change can get wrong. The ordering metrics come along because a change
# that holds recall flat while ordering worse is still a regression.
METRICS = ("recall@5", "recall@10", "recall@20", "ndcg@10", "mrr")


def _score(rels: list[float], n_relevant: int) -> dict[str, float]:
    return {
        "recall@5": recall(rels, 5, n_relevant),
        "recall@10": recall(rels, 10, n_relevant),
        "recall@20": recall(rels, 20, n_relevant),
        "ndcg@10": ndcg(rels, 10, n_relevant),
        "mrr": mrr(rels),
    }


def _saturate(raw: float, k: float) -> float:
    """``(k*r)/(1+k*r)`` — today's formula is this with k = 1."""
    scaled = k * raw
    return scaled / (1.0 + scaled)


# ------------------------------------------------------------------ postgres fts


async def _ts_rank_cd(dsn: str, texts: list[str], queries: list[str]) -> list[list[float]]:
    """Return ``ts_rank_cd`` per (query, doc), using real Postgres FTS.

    Mirrors production exactly: ``to_tsvector('english', content)`` with no
    ``setweight`` (migration 001's trigger) and ``plainto_tsquery('english', q)``
    (``_execute_scored_search``). Non-matching pairs come back 0.0, which is what
    the SQL ``ts_rank_cd`` yields and therefore what the blend sees.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE TEMP TABLE bench_turns (i int, tsv tsvector)")
        await conn.executemany(
            "INSERT INTO bench_turns (i, tsv) VALUES ($1, to_tsvector('english', $2))",
            list(enumerate(texts)),
        )
        # One statement per query, all docs at once. Every doc comes back whether
        # it matched or not: `ts_rank_cd` scores a non-match as 0 rather than
        # omitting it, and `ORDER BY t.i` holds the rows in `texts` order — so the
        # caller gets a genuine 0.0 per miss instead of a gap it has to
        # reconstruct. (`ts_rank_cd` returns NULL only for a NULL tsvector, which
        # `to_tsvector` of a non-null text cannot produce, so there is nothing to
        # COALESCE away.)
        out = []
        for q in queries:
            rows = await conn.fetch(
                """
                SELECT t.i, ts_rank_cd(t.tsv, plainto_tsquery('english', $1)) AS r
                FROM bench_turns t
                ORDER BY t.i
                """,
                q,
            )
            out.append([float(r["r"]) for r in rows])
        return out
    finally:
        await conn.close()


# ------------------------------------------------------------------------- run


def run_conversation(conv: dict, args: argparse.Namespace) -> list[dict]:
    texts = [d["text"] for d in conv["docs"]]
    ids = [d["id"] for d in conv["docs"]]
    queries = [q["q"] for q in conv["queries"]]
    if not queries:
        return []

    dvecs = [
        unit(v)
        for v in embed(texts, args.embed_url, args.embed_model, args.embed_api_key, args.embed_batch)
    ]
    qvecs = [
        unit(v)
        for v in embed(queries, args.embed_url, args.embed_model, args.embed_api_key, args.embed_batch)
    ]
    raw_ranks = asyncio.run(_ts_rank_cd(args.pg_dsn, texts, queries))

    w = args.fts_weight
    rows = []
    for q, qvec, raws in zip(conv["queries"], qvecs, raw_ranks):
        expected = set(q["expected"])
        n_relevant = len(expected)
        # vec_sim is cosine on unit vectors, matching `1 - cosine_distance` for
        # the embedded rows this corpus is entirely made of.
        vsims = [sum(a * b for a, b in zip(qvec, d)) for d in dvecs]

        def ranked(k: float) -> list[float]:
            scored = [
                ((1.0 - w) * v + w * _saturate(r, k), i) for i, (v, r) in enumerate(zip(vsims, raws))
            ]
            # Descending by blended similarity; index breaks ties so both arms
            # order equal-scoring docs identically and a tie can't masquerade as
            # a win for either.
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [1.0 if ids[i] in expected else 0.0 for _, i in scored]

        base_rels = ranked(1.0)
        treat_rels = ranked(args.k)
        # Full-corpus ranking, so both arms see every relevant doc somewhere.
        # If that stops holding, recall@k is no longer comparable.
        assert sum(base_rels) == sum(treat_rels) == n_relevant, (
            "a relevant turn is missing from the ranking — the corpus is not whole"
        )

        rows.append(
            {
                "category": q["cat"],
                "baseline": _score(base_rels, n_relevant),
                "treatment": _score(treat_rels, n_relevant),
                "fts_hit": any(r > 0 for r in raws),
            }
        )
    return rows


def report(label: str, rows: list[dict], args: argparse.Namespace) -> dict | None:
    if not rows:
        print(f"\n{label}: nothing scorable")
        return None

    hits = sum(1 for r in rows if r["fts_hit"])
    print(f"\n=== {label} — n={len(rows)} · fts matched on {hits}/{len(rows)} queries ===")
    print(
        f"  {'metric':10s} {'k=1 (today)':>12s} {f'k={args.k:g}':>10s} {'delta':>9s} "
        f"{'95% CI':>20s} {'better/worse/tie':>18s} {'sign p':>8s}"
    )

    rng = random.Random(args.seed)
    out = {}
    for m in METRICS:
        deltas = [r["treatment"][m] - r["baseline"][m] for r in rows]
        base = statistics.fmean(r["baseline"][m] for r in rows)
        treat = statistics.fmean(r["treatment"][m] for r in rows)
        st = paired_stats(deltas, rng)
        out[m] = {"baseline": base, "treatment": treat, "delta": treat - base, **st}
        lo, hi = st["ci"]
        print(
            f"  {m:10s} {base:12.4f} {treat:10.4f} {treat - base:+9.4f} "
            f"[{lo:+.4f},{hi:+.4f}] {st['better']:>6d}/{st['worse']}/{st['ties']:<7d} "
            f"{st['sign_p']:8.4f}{'  *' if st['significant'] else ''}"
        )

    if args.k == 1.0:
        # The treatment formula IS the baseline at k=1, so any non-zero delta
        # means the harness is producing a difference the change cannot cause.
        for m in METRICS:
            assert abs(out[m]["delta"]) < 1e-12, (
                f"k=1 must reproduce the baseline exactly; {m} moved by {out[m]['delta']}"
            )
        print("  self-check: k=1 reproduced the baseline exactly on every metric")
    return {"n": len(rows), "fts_hits": hits, "metrics": out}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dataset", required=True, help="path to locomo10.json")
    ap.add_argument("--embed-url", required=True, help="base URL exposing /v1/embeddings")
    ap.add_argument(
        "--pg-dsn",
        default="postgresql://memclaw:changeme@localhost:5433/memclaw",
        help="any empty Postgres; used only for ts_rank_cd via a TEMP table",
    )
    ap.add_argument("--embed-model", default="BAAI/bge-m3")
    ap.add_argument("--embed-api-key", default="")
    ap.add_argument("--embed-batch", type=int, default=32)
    ap.add_argument(
        "--k",
        type=float,
        default=6.0,
        help="scale applied to ts_rank_cd before saturation. 1 = today's formula "
        "(and asserts a zero delta). ~6 puts a single-term match near the cosine "
        "range, making the effective FTS weight match the nominal one.",
    )
    ap.add_argument(
        "--fts-weight",
        type=float,
        default=0.3,
        help="FTS_WEIGHT from core_api.constants (default matches production)",
    )
    ap.add_argument("--limit-conversations", type=int, default=0, help="0 = all")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--json-out", default="", help="write the full result as JSON")
    args = ap.parse_args()

    if not 0.0 <= args.fts_weight <= 1.0:
        raise SystemExit("--fts-weight must be in [0, 1]")
    if args.k <= 0:
        raise SystemExit("--k must be > 0")

    convs = load_locomo(args.dataset)
    if args.limit_conversations:
        convs = convs[: args.limit_conversations]

    all_rows: list[dict] = []
    for conv in convs:
        rows = run_conversation(conv, args)
        all_rows.extend(rows)
        print(f"  {conv['sample_id']}: {len(rows)} queries scored")

    result = {"overall": report("overall", all_rows, args)}

    # The subgroup that can actually move. Where plainto_tsquery matches no turn,
    # fts_score is 0 in BOTH arms and the rescale is a mathematical no-op — those
    # queries only dilute the overall delta toward zero. Reporting them separately
    # keeps "small effect" and "small effect on the queries it applies to" from
    # being confused, which is the difference that decides whether to ship.
    matched = [r for r in all_rows if r["fts_hit"]]
    if matched and len(matched) < len(all_rows):
        result["fts-matched-only"] = report("fts-matched-only", matched, args)

    for cat in CATEGORIES.values():
        subset = [r for r in all_rows if r["category"] == cat]
        if subset:
            result[cat] = report(cat, subset, args)

    if args.json_out:
        payload = {
            "k": args.k,
            "fts_weight": args.fts_weight,
            "embed_model": args.embed_model,
            "seed": args.seed,
            "results": result,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
