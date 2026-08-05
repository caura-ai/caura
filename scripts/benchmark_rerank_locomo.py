"""
Rerank A/B on LoCoMo — does the cross-encoder order results better than
first-stage similarity alone?

Isolates the reranker so that ORDER is the only variable:

    corpus     one LoCoMo conversation's dialogue turns (~600 of them)
    baseline   embedding cosine, top --pool          (what first-stage ranks by)
    treatment  that same pool, reordered by POST {rerank-url}/rerank
    truth      the QA item's ``evidence`` turn ids   (binary relevance)

Both arms score the SAME pool, so recall@pool is identical by construction —
asserted at runtime. Any difference is ordering, which is all a reranker does.

Retrieval is per-conversation, matching LoCoMo's setup: each conversation is
one agent's memory and a question only ever concerns its own conversation.
Pooling all ten would measure a task the product never performs.

LoCoMo category 5 (adversarial / unanswerable) is excluded: there is no
evidence turn to retrieve, so those items measure answer abstention rather
than ranking. Categories 1-4 (multi-hop, temporal, open-domain, single-hop)
are kept and reported separately.

Reads the dataset from disk and talks to two HTTP endpoints; no cloud
dependencies and stdlib only, so it runs against ``docker compose`` as easily
as against a deployed stack.

    # dataset: https://github.com/snap-research/locomo -> data/locomo10.json
    python scripts/benchmark_rerank_locomo.py \\
        --dataset locomo10.json \\
        --embed-url http://localhost:8080 \\
        --rerank-url http://localhost:8081

``--embed-url`` speaks the OpenAI-compatible ``/v1/embeddings`` route (TEI, or
OpenAI itself with --embed-api-key). ``--rerank-url`` speaks TEI-native
``/rerank``; a Cohere-shaped ``{"results": [...]}`` body is accepted too.

Caveat worth carrying into any conclusion: the baseline here is pure cosine,
while the real first-stage additionally applies freshness decay, weight
blending and recall boost. This measures the reranker in isolation, which is
the point, but it is not the end-to-end pipeline delta.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

# LoCoMo's own category numbering. 5 is deliberately absent — see module docstring.
#
# DON'T "correct" this to 1:single-hop, 2:multi-hop, 3:temporal, 4:open-domain.
# That ordering is widely repeated in write-ups but does NOT match locomo10.json.
# Verified against the published file — each is unmistakable from the questions:
#   1  several items carry TWO evidence turns ("Where did Caroline move from
#      4 years ago?") -> multi-hop
#   2  every question is "When did ...", every answer a date              -> temporal
#   3  "Would Caroline likely have Dr. Seuss books on her bookshelf?" —
#      needs world knowledge beyond the transcript                    -> open-domain
#   4  the largest bucket (841) and the simplest: one evidence turn, direct
#      lookup ("What did the charity race raise awareness for?")       -> single-hop
#   5  every answer is literally None                                  -> adversarial
# Re-check by sampling qa items per category value, not by consulting a summary.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _locomo_bench import (  # noqa: E402  (path shim above must run first)
    _post,
    embed,
    load_locomo,
    mrr,
    ndcg,
    paired_stats,
    unit,
)

METRICS = ("ndcg@5", "ndcg@10", "mrr", "p@5")


# --------------------------------------------------------------------------- io


def rerank(query: str, texts: list[str], url: str, api_key: str) -> list[float]:
    """Return a score per input text, in INPUT order."""
    r = _post(f"{url}/rerank", {"query": query, "texts": texts}, api_key)
    items = r.get("results", r) if isinstance(r, dict) else r
    scores = [0.0] * len(texts)
    for it in items:
        s = it.get("score")
        if s is None:
            s = it.get("relevance_score")
        scores[it["index"]] = float(s)
    return scores


# ---------------------------------------------------------------------- dataset


# ---------------------------------------------------------------------- metrics


def _score(rels: list[float], n_relevant: int) -> dict[str, float]:
    return {
        "ndcg@5": ndcg(rels, 5, n_relevant),
        "ndcg@10": ndcg(rels, 10, n_relevant),
        "mrr": mrr(rels),
        "p@5": sum(rels[:5]) / 5,
    }


def _cosine_ranked(qvec: list[float], dvecs: list[list[float]], pool: int) -> list[int]:
    sims = ((sum(a * b for a, b in zip(qvec, d)), i) for i, d in enumerate(dvecs))
    return [i for _, i in sorted(sims, reverse=True)[:pool]]


# ------------------------------------------------------------------------- run


def run_conversation(conv: dict, args: argparse.Namespace) -> list[dict]:
    texts = [d["text"] for d in conv["docs"]]
    ids = [d["id"] for d in conv["docs"]]
    dvecs = [
        unit(v)
        for v in embed(
            texts,
            args.embed_url,
            args.embed_model,
            args.embed_api_key,
            args.embed_batch,
        )
    ]
    qvecs = [
        unit(v)
        for v in embed(
            [q["q"] for q in conv["queries"]],
            args.embed_url,
            args.embed_model,
            args.embed_api_key,
            args.embed_batch,
        )
    ]

    rows = []
    for q, qvec in zip(conv["queries"], qvecs):
        expected = set(q["expected"])
        pool = _cosine_ranked(qvec, dvecs, args.pool)
        n_relevant = sum(1 for i in pool if ids[i] in expected)
        if not n_relevant:
            # First-stage retrieved nothing relevant into the pool. A reranker
            # cannot invent it, so scoring this would add a shared zero to both
            # arms and dilute the comparison. Counted and reported instead.
            rows.append({"scored": False})
            continue

        base_rels = [1.0 if ids[i] in expected else 0.0 for i in pool]
        scores = rerank(
            q["q"], [texts[i] for i in pool], args.rerank_url, args.rerank_api_key
        )
        order = sorted(range(len(pool)), key=lambda j: -scores[j])
        rr_rels = [1.0 if ids[pool[j]] in expected else 0.0 for j in order]
        assert sum(base_rels) == sum(rr_rels), (
            "pool changed — this is no longer an order-only test"
        )

        rows.append(
            {
                "scored": True,
                "category": q["cat"],
                "baseline": _score(base_rels, n_relevant),
                "reranked": _score(rr_rels, n_relevant),
                "top1_changed": pool[0] != pool[order[0]],
            }
        )
    return rows


def report(label: str, rows: list[dict], pool: int, seed: int) -> dict | None:
    scored = [r for r in rows if r["scored"]]
    missed = len(rows) - len(scored)
    if not scored:
        print(f"\n{label}: nothing scorable ({missed} skipped)")
        return None

    head = f"\n=== {label} — n={len(scored)}"
    if missed:
        head += f" ({missed} skipped: no evidence turn reached the top-{pool} pool)"
    print(head + " ===")
    print(
        f"  {'metric':9s} {'first-stage':>11s} {'reranked':>9s} {'delta':>9s} "
        f"{'95% CI':>20s} {'better/worse/tie':>18s} {'sign p':>8s}"
    )

    rng = random.Random(seed)
    out = {}
    for m in METRICS:
        deltas = [r["reranked"][m] - r["baseline"][m] for r in scored]
        base = statistics.fmean(r["baseline"][m] for r in scored)
        rr = statistics.fmean(r["reranked"][m] for r in scored)
        st = paired_stats(deltas, rng)
        lo, hi = st["ci"]
        out[m] = {
            "baseline": base,
            "reranked": rr,
            "delta": rr - base,
            "ci": [lo, hi],
            "better": st["better"],
            "worse": st["worse"],
            "sign_p": st["sign_p"],
        }
        mark = "" if not st["significant"] else "  *"
        print(
            f"  {m:9s} {base:11.4f} {rr:9.4f} {rr - base:+9.4f} "
            f"[{lo:+.4f},{hi:+.4f}] {st['better']:>6d}/{st['worse']}/{st['ties']:<7d} "
            f"{st['sign_p']:8.4f}{mark}"
        )
    print(
        f"  top-1 changed on {sum(1 for r in scored if r['top1_changed'])}/{len(scored)}"
    )
    return {"n": len(scored), "skipped": missed, "metrics": out}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dataset", required=True, help="path to locomo10.json")
    ap.add_argument(
        "--embed-url", required=True, help="base URL exposing /v1/embeddings"
    )
    ap.add_argument("--rerank-url", required=True, help="base URL exposing /rerank")
    ap.add_argument("--embed-model", default="BAAI/bge-m3")
    ap.add_argument(
        "--embed-api-key", default="", help="only if the embedder needs one"
    )
    ap.add_argument(
        "--rerank-api-key", default="", help="only if the reranker needs one"
    )
    ap.add_argument(
        "--pool",
        type=int,
        default=20,
        help="candidates retrieved then reordered (default: 20, mirrors "
        "top_k * SEARCH_OVERFETCH_FACTOR at top_k=10)",
    )
    ap.add_argument(
        "--embed-batch",
        type=int,
        default=32,
        help="texts per embedding request; keep <= the embedder's client batch cap",
    )
    ap.add_argument(
        "--conversations", type=int, default=0, help="limit conversations (0 = all)"
    )
    ap.add_argument(
        "--seed", type=int, default=4242, help="bootstrap seed, for reproducibility"
    )
    ap.add_argument(
        "--json", default="", help="also write the full result as JSON here"
    )
    args = ap.parse_args()

    samples = load_locomo(args.dataset)
    if args.conversations:
        samples = samples[: args.conversations]
    print(f"pool={args.pool} · embedder={args.embed_url} · reranker={args.rerank_url}")

    started = time.time()
    rows: list[dict] = []
    for i, conv in enumerate(samples, 1):
        conv_rows = run_conversation(conv, args)
        scored = [r for r in conv_rows if r["scored"]]
        delta = (
            statistics.fmean(
                r["reranked"]["ndcg@5"] - r["baseline"]["ndcg@5"] for r in scored
            )
            if scored
            else 0.0
        )
        print(
            f"  [{i}/{len(samples)}] {conv['sample_id']}: {len(conv['docs'])} turns · "
            f"{len(scored)}/{len(conv_rows)} scorable · nDCG@5 {delta:+.4f}",
            flush=True,
        )
        rows += conv_rows

    result = {
        "overall": report("OVERALL", rows, args.pool, args.seed),
        "by_category": {},
    }
    for cat in ("single-hop", "multi-hop", "temporal", "open-domain"):
        subset = [r for r in rows if r["scored"] and r["category"] == cat]
        result["by_category"][cat] = report(
            f"CATEGORY: {cat}", subset, args.pool, args.seed
        )
    print(f"\ndone in {time.time() - started:.0f}s. '*' marks a 95% CI excluding zero.")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    sys.exit(main())
