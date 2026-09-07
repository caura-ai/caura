#!/usr/bin/env python3
"""Ask a live Caura deployment why entity retrieval did or did not fire.

Why this exists
---------------
A benchmark run reported the ENTITY_LOOKUP strategy firing on **0 of 589
queries** and the finding was written up as "the entity/graph subsystem
contributed to 0 of 589 queries". Those are different claims, and the second
one was never measured. Two reasons:

1. ``retrieval_strategy`` reports only the ENTITY_LOOKUP **short-circuit**,
   which by design fires solely when the entity-linked pool fills ``top_k`` on
   its own (``classify_query.py``: ``len(filtered_rows) >= top_k``). The
   *boost* — entity signal nudging scores inside ordinary semantic search — is
   a separate mechanism that reports no strategy of its own.
2. The boost's effect is meant to be visible per row, in the same response,
   as ``diagnostic.all_candidates[].entity_boost``. The probe that produced
   the zero read eight summary scalars and discarded ``all_candidates``
   entirely — but that turns out not to have cost it anything yet, because
   storage does not currently serialize that factor at all (see the note in
   ``probe()``). So the boost question is still open, and this script says so
   explicitly instead of scoring it as a zero.

So this script reads BOTH mechanisms, plus the CAURA-722 fields that say why
the short-circuit declined when it did.

What each column means
----------------------
``strategy``          the short-circuit: ``entity_lookup`` = fired.
``matches``           entities the query's tokens matched in entity FTS.
                      ``-`` means the FTS never ran (entity retrieval off, no
                      entity-shaped tokens, or a swallowed failure) — which is
                      a different answer from ``0``.
``declined``          the over-broad refusal (> ENTITY_LOOKUP_MAX_MATCHES).
                      This is the ONLY decline that also suppresses the boost.
``boosted``           rows in the candidate set carrying a non-null, non-1.0
                      ``entity_boost`` — i.e. the boost actually moved them.
                      This is the column that WOULD answer "did entity
                      retrieval contribute?", and it reads 0 with a loud
                      warning until storage serializes the factor.

Reading the verdict
-------------------
    declined=True                      -> entity contributed NOTHING here
                                          (boost suppressed by design)
    strategy=entity_lookup             -> entity answered the query outright
    boosted>0                          -> entity contributed via the boost,
                                          even though strategy says
                                          "semantic_search"
    matches=0, boosted=0               -> nothing matched; an extraction /
                                          linking question
    matches=-                          -> the query never asked; not evidence
                                          about the entity index at all

Read-only and side-effect free: the only call is ``POST /search`` with
``diagnostic: true``, which the API documents as inspection rather than use
(it does not bump ``recall_count`` — see ``SearchDiagnostic``). Nothing writes,
deletes, ingests or purges.

Usage
-----
    # against a local dev stack (no auth in standalone mode)
    python scripts/entity_retrieval_probe.py \
        --base-url http://localhost:18000/api/v1 --tenant-id my-tenant \
        --query "What did Sarah say about the Q3 roadmap?"

    # a file of queries, one per line, against a real deployment
    CAURA_API_KEY=mc_... python scripts/entity_retrieval_probe.py \
        --base-url https://caura.ai/api/v1 --tenant-id t \
        --agent-id amb-persona-0 --queries-file queries.txt --top-k 20

    # queries straight out of an AMB run
    python scripts/entity_retrieval_probe.py --base-url ... --tenant-id t \
        --amb-run outputs/personamem/caura/rag/32k.json.gz --limit 100

``--json`` prints one JSON object per query instead of the table, for piping.

Note on ``--top-k``: the short-circuit needs the entity pool to fill it
entirely, so a large ``top_k`` against a small store makes ``entity_lookup``
effectively unreachable. If every row says ``semantic_search``, re-run with a
small ``--top-k`` before concluding anything about the entity index.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

MAX_QUERY_LENGTH = 5_000


def _load_amb_queries(path: Path) -> list[str]:
    """Pull the retrieval queries out of an AMB results file (.json or .json.gz)."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for r in data.get("results", []):
        meta = r.get("meta") or {}
        # AMB sends ``meta.retrieval_query`` to the provider when present; the
        # bare ``query`` is the question put to the answer model.
        out.append(meta.get("retrieval_query") or r.get("query") or "")
    return [q for q in out if q]


def probe(
    client: httpx.Client,
    base: str,
    tenant_id: str,
    query: str,
    top_k: int,
    agent_id: str | None,
    fleet_ids: list[str] | None,
) -> dict:
    """One diagnostic search. Returns the entity trace, never the memories."""
    payload: dict = {
        "tenant_id": tenant_id,
        "query": query[:MAX_QUERY_LENGTH],
        "top_k": top_k,
        "diagnostic": True,
    }
    if agent_id:
        payload["filter_agent_id"] = agent_id
    if fleet_ids:
        payload["fleet_ids"] = fleet_ids

    last_err = None
    for attempt in range(4):
        try:
            r = client.post(f"{base}/search", json=payload, timeout=60.0)
        except httpx.HTTPError as exc:
            last_err = str(exc)
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(5.0 * (attempt + 1))
            continue
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}

        body = r.json()
        diag = body.get("diagnostic") or {}
        candidates = diag.get("all_candidates") or []

        # A boost of exactly 1.0 is the multiplicative identity — present but
        # inert. Counting it as "contributed" would overstate the subsystem,
        # which is the error this script exists to correct, so require a real
        # deviation.
        #
        # ``None`` is a THIRD state and must not be read as 1.0 or as 0.
        #
        # As of this writing it is also the ONLY state you will see in
        # production, and that is a defect, not a configuration: storage
        # computes ``entity_boost`` (with ``freshness``, ``recall_boost``,
        # ``temporal_boost``, ``fts_score``) inside the scored CTE, uses them
        # to build ``score``, and then the outer ``select()`` does not include
        # them — so the scored-search route never serializes them and
        # ``all_candidates`` reports five of its nine factors as null on every
        # query. See ``postgres_service.memory_scored_search``'s outer query.
        #
        # Until that is fixed, ``boosted`` cannot answer "did the boost
        # contribute?" anywhere. The script therefore reports the boost as
        # UNANSWERED rather than as "no" — reporting 0% from a response that
        # never carried the number is the exact mistake this script exists to
        # correct.
        boosts = [
            c.get("entity_boost")
            for c in candidates
            if c.get("entity_boost") is not None and c.get("entity_boost") != 1.0
        ]
        boost_reported = sum(1 for c in candidates if c.get("entity_boost") is not None)

        return {
            "query": query[:80],
            "strategy": diag.get("retrieval_strategy"),
            # CAURA-722. Absent on a deployment older than that change, which
            # is why this reports ``None`` rather than defaulting to 0 — an
            # old server must not look like "matched nothing".
            "entity_matches": diag.get("entity_matches"),
            "entity_match_declined": diag.get("entity_match_declined"),
            "candidates": len(candidates),
            "boosted": len(boosts),
            "max_boost": max(boosts) if boosts else None,
            # Did the response carry the factor at all? Distinguishes "no
            # boost" from "cannot tell".
            "boost_reported": boost_reported,
            "items": len(body.get("items") or []),
            "has_caura722_fields": "entity_matches" in diag,
        }
    return {"error": last_err or "exhausted retries"}


def _verdict(row: dict) -> str:
    if row.get("error"):
        return "error"
    if row.get("entity_match_declined"):
        return "declined-over-broad (boost suppressed)"
    if row.get("strategy") == "entity_lookup":
        return "short-circuit fired"
    if row.get("boosted"):
        return "boost contributed"
    if row.get("entity_matches") == 0:
        return "no entity match"
    if row.get("entity_matches") is None:
        return "FTS never ran"
    # Reached only when the FTS matched something, the short-circuit did not
    # fire and it was not declined — so the boost is the open question.
    # Whether the answer is "the boost did nothing" or "the boost was not
    # reported" depends on the column coming back at all, and the two must not
    # share a line: one is a finding, the other is a broken measurement.
    if row.get("candidates") and not row.get("boost_reported"):
        return "matched; boost NOT MEASURABLE (no score factors returned)"
    return "matched, but no effect"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--base-url", required=True, help="e.g. http://localhost:18000/api/v1"
    )
    ap.add_argument("--tenant-id", required=True)
    ap.add_argument("--agent-id", default=None, help="sets filter_agent_id")
    ap.add_argument("--fleet-id", action="append", default=None, dest="fleet_ids")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--query", action="append", default=[])
    ap.add_argument("--queries-file", type=Path, default=None)
    ap.add_argument(
        "--amb-run", type=Path, default=None, help="AMB results .json/.json.gz"
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    queries: list[str] = list(args.query)
    if args.queries_file:
        queries += [
            ln.strip()
            for ln in args.queries_file.read_text().splitlines()
            if ln.strip()
        ]
    if args.amb_run:
        queries += _load_amb_queries(args.amb_run)
    if not queries:
        ap.error("give at least one of --query / --queries-file / --amb-run")
    if args.limit:
        queries = queries[: args.limit]

    headers = {}
    key = os.environ.get("CAURA_API_KEY")
    if key:
        headers["X-API-Key"] = key

    base = args.base_url.rstrip("/")
    rows = []
    with httpx.Client(headers=headers) as client:
        for i, q in enumerate(queries, 1):
            row = probe(
                client,
                base,
                args.tenant_id,
                q,
                args.top_k,
                args.agent_id,
                args.fleet_ids,
            )
            row["verdict"] = _verdict(row)
            rows.append(row)
            if args.as_json:
                print(json.dumps(row))
            elif i == 1 or i % 25 == 0:
                print(f"  ... {i}/{len(queries)}", file=sys.stderr)

    if args.as_json:
        return 0

    errors = [r for r in rows if r.get("error")]
    ok = [r for r in rows if not r.get("error")]

    if ok and not ok[0]["has_caura722_fields"]:
        print(
            "\nNOTE: this deployment does not return entity_matches — it predates\n"
            "CAURA-722. The boost columns below are still valid; the match count\n"
            "and decline reason are not available.\n"
        )

    print(
        f"\n{'strategy':<18} {'matches':>8} {'decl':>5} {'cand':>5} {'boosted':>8} {'maxb':>6}  query"
    )
    print("-" * 100)
    for r in ok[:40]:
        m = r["entity_matches"]
        mb = r["max_boost"]
        print(
            f"{r['strategy']!s:<18} {('-' if m is None else m):>8} "
            f"{('Y' if r['entity_match_declined'] else '.'):>5} {r['candidates']:>5} "
            f"{r['boosted']:>8} {(f'{mb:.3f}' if mb else '-'):>6}  {r['query']}"
        )
    if len(ok) > 40:
        print(f"... {len(ok) - 40} more (use --json for all)")

    print(
        f"\n{'=' * 60}\nVERDICT over {len(ok)} queries"
        + (f" ({len(errors)} errored)" if errors else "")
    )
    for verdict, n in Counter(r["verdict"] for r in ok).most_common():
        print(f"  {n:>5}  {verdict}")

    contributed = sum(1 for r in ok if r["boosted"] or r["strategy"] == "entity_lookup")
    # Queries where scored search ran but returned no score factors: the boost
    # is unmeasured, so they belong in neither the numerator nor a "0%" claim.
    # Deliberately narrow. A declined query, a zero-match query and one whose
    # FTS never ran are all DETERMINATE "entity contributed nothing" — folding
    # them in here would understate the denominator and flatter the result.
    # Only a query that matched, did not short-circuit and was not declined
    # leaves the boost genuinely unobserved.
    unmeasurable = sum(
        1
        for r in ok
        if r["strategy"] != "entity_lookup"
        and not r["entity_match_declined"]
        and (r["entity_matches"] or 0) > 0
        and r["candidates"]
        and not r["boost_reported"]
    )
    if ok:
        denom = len(ok) - unmeasurable
        if denom > 0:
            print(
                f"\nEntity retrieval affected the result set on {contributed}/{denom} "
                f"measurable queries ({100 * contributed / denom:.1f}%)."
            )
            print(
                "Compare against the strategy column alone, which would have reported "
                f"{sum(1 for r in ok if r['strategy'] == 'entity_lookup')}/{len(ok)}."
            )
        if unmeasurable:
            print(
                f"\n!! {unmeasurable}/{len(ok)} queries returned candidates with NO score\n"
                "   factors (entity_boost null on every row). On current storage that column\n"
                "   is never null — it is 1.0 when unboosted — so this deployment is not\n"
                "   reporting them and the boost question is UNANSWERED for those queries.\n"
                "   Do not read this as 'entity contributed nothing'; that is the error this\n"
                "   script exists to catch. Check the storage version behind this endpoint."
            )
    if errors:
        print(f"\nfirst error: {errors[0]['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
