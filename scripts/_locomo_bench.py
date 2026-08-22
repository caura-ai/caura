"""Shared pieces for the LoCoMo benchmark harnesses.

Extracted from ``benchmark_rerank_locomo.py`` when the first-stage blend harness
(``benchmark_blend_locomo.py``) needed the same dataset loader, embedding client
and paired statistics. The two harnesses measure different stages and keep their
own docstrings, arms and metrics; only the parts that are genuinely stage-agnostic
live here.

Stdlib only, deliberately — the rerank harness runs against ``docker compose``
with no installed dependencies, and this must not take that away from it.
"""

from __future__ import annotations

import ast
import collections
import json
import math
import statistics
import urllib.error
import urllib.request

CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop"}


# ------------------------------------------------------------------------ http


def _post(url: str, body: dict, api_key: str = "", timeout: float = 180.0) -> object:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:  # surface the body — it says what's wrong
        raise SystemExit(f"POST {url} -> {exc.code}: {exc.read()[:400]!r}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"POST {url} unreachable: {exc.reason}") from exc


def embed(
    texts: list[str], url: str, model: str, api_key: str, batch: int
) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        r = _post(
            f"{url}/v1/embeddings",
            {"model": model, "input": texts[i : i + batch]},
            api_key,
        )
        out.extend(d["embedding"] for d in r["data"])
    return out


# --------------------------------------------------------------------- dataset


def load_locomo(path: str) -> list[dict]:
    """Flatten LoCoMo into {sample_id, docs[{id,text}], queries[{q,expected,cat}]}."""
    with open(path) as fh:
        raw = json.load(fh)
    samples, skipped = [], collections.Counter()
    for sample in raw:
        turns = []
        for value in sample["conversation"].values():
            if not isinstance(value, list):
                continue  # session_N_date_time and the speaker names
            for t in value:
                if isinstance(t, dict) and t.get("dia_id") and t.get("text"):
                    # keep the speaker: who said it is part of what makes a turn
                    # the right answer to "when did X do Y".
                    turns.append(
                        {
                            "id": t["dia_id"],
                            "text": f"{t.get('speaker', '?')}: {t['text']}",
                        }
                    )
        ids = {t["id"] for t in turns}

        queries = []
        for qa in sample["qa"]:
            try:
                cat = int(qa.get("category"))
            except (TypeError, ValueError):
                cat = None
            if cat not in CATEGORIES:
                skipped[f"category-{cat}"] += 1
                continue
            evidence = qa.get("evidence")
            if isinstance(evidence, str):
                # the published file stores this as a str repr of a list
                try:
                    evidence = ast.literal_eval(evidence)
                except (ValueError, SyntaxError):
                    evidence = None
            evidence = [e for e in (evidence or []) if e in ids]
            if not evidence:
                skipped["unresolvable-evidence"] += 1
                continue
            queries.append(
                {"q": qa["question"], "expected": evidence, "cat": CATEGORIES[cat]}
            )
        samples.append(
            {"sample_id": sample["sample_id"], "docs": turns, "queries": queries}
        )

    print(
        f"loaded {len(samples)} conversations · "
        f"{sum(len(s['docs']) for s in samples)} turns · "
        f"{sum(len(s['queries']) for s in samples)} scorable questions"
    )
    if skipped:
        print(f"  excluded: {dict(skipped)}")
    return samples


# --------------------------------------------------------------------- metrics


def _dcg(rels: list[float]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def ndcg(rels: list[float], k: int, n_relevant: int) -> float:
    ideal = _dcg([1.0] * min(n_relevant, k))
    return _dcg(rels[:k]) / ideal if ideal else 0.0


def mrr(rels: list[float]) -> float:
    for i, r in enumerate(rels):
        if r:
            return 1.0 / (i + 1)
    return 0.0


def recall(rels: list[float], k: int, n_relevant: int) -> float:
    """Share of ALL relevant docs that made the top ``k``.

    Only meaningful when the ranking covers the whole corpus. A harness that
    scores a fixed candidate pool holds this constant by construction, so it
    reports ordering metrics instead.
    """
    return sum(rels[:k]) / n_relevant if n_relevant else 0.0


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


# ------------------------------------------------------------------ statistics


def paired_stats(deltas: list[float], rng) -> dict:
    """Paired bootstrap CI + exact two-sided sign test over per-query deltas.

    Paired because both arms answer the SAME queries: the per-query difference
    removes query difficulty, which is the dominant source of variance here and
    would otherwise swamp the effect being measured.
    """
    boots = sorted(
        statistics.fmean([deltas[rng.randrange(len(deltas))] for _ in range(len(deltas))])
        for _ in range(4000)
    )
    lo, hi = boots[100], boots[3900]
    better = sum(1 for d in deltas if d > 1e-9)
    worse = sum(1 for d in deltas if d < -1e-9)
    n = better + worse
    p = (
        min(1.0, 2 * sum(math.comb(n, i) for i in range(min(better, worse) + 1)) / 2**n)
        if n
        else 1.0
    )
    return {
        "ci": [lo, hi],
        "better": better,
        "worse": worse,
        "ties": len(deltas) - n,
        "sign_p": p,
        # A CI straddling zero means the sample can't distinguish the arms —
        # report it as such rather than quoting the point estimate alone.
        "significant": not (lo <= 0 <= hi),
    }
