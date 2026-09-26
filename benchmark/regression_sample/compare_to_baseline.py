#!/usr/bin/env python3
"""Compare a regression-sample run against the golden baseline (reg-d15).

Consumes the LongMemEval runner family's per-category results JSON
(``{"benchmark_metadata", "results": [...]}``, one file per question
category) for a candidate run and for the golden
baseline, recomputes accuracy and recall@k from the raw per-question
artifacts on BOTH sides, and prints the per-category delta table with
threshold flags. Exit code 1 when any threshold is breached.

Why recall is recomputed instead of trusted from ``retrieval_summary``
----------------------------------------------------------------------
The runner's recall@k counts gold ``source_uri``s against retrieved
``source_uri``s and nothing else. The product, meanwhile, deliberately
retires the older side of an update: the contradiction detector marks it
``outdated`` (RDF path) or ``conflicted`` (semantic path) and points the
newer row at it via ``supersedes_id``, and default (present-state) search
excludes those rows — see ``memory_scored_search`` in
``core-storage-api``. A gold row that the pipeline CORRECTLY superseded is
therefore counted as a miss, and every change that makes supersession fire
more (A63: #1023, #1025) reads as a recall regression while accuracy holds
or rises. This scorer makes the metric supersession-aware, so correct
supersession stops being reported as damage — while keeping REAL damage
visible (see below).

Recall modes
------------
``--recall-mode supersession-aware`` (default)
    A gold source_uri counts as satisfied when
      (a) a retrieved memory carries it (identical to the legacy metric), or
      (b) the gold row was superseded (its stored status is ``outdated`` or
          ``conflicted``) AND a retrieved memory's ``supersedes_id`` chain
          reaches the gold row's id — i.e. the information's successor made
          the cut. A superseded gold whose successor is NOT retrieved still
          counts as a MISS, so a detector that buries information without
          surfacing a replacement keeps hurting recall.
    The denominator is never reduced: dropping superseded golds from it
    would let a run score 1.0 while retrieving none of the question's
    information. "Recall" keeps meaning "fraction of the required
    information units whose lineage is present in the top-k".

``--recall-mode legacy``
    Reproduces the historical counting exactly (retrieved source_uris
    only), for comparability with goldens captured before this scorer.

Supersession visibility (the over-firing instrument)
----------------------------------------------------
Whether supersession was CORRECT is precisely what a run summary can't
decide (reg-d15's premise check: gold event-pair memories may be getting
marked conflicted by mistake). So besides recall, every report lists each
gold that was superseded — credited or lost, and by which memory — plus
per-category totals. A jump in ``gold superseded`` under unchanged data
means the detector fired more; the pair list is what to audit before
believing either the old metric's FAIL or the new metric's PASS.

Inputs degrade gracefully: a question whose artifact lacks
``all_stored_memories`` statuses can't classify unretrieved golds and is
scored legacy-style (a warning is counted and shown).

Usage::

    python benchmark/regression_sample/compare_to_baseline.py \
        --run results/run-2026-08-30 \
        --baseline results/golden-2026-08-27 \
        [--recall-mode supersession-aware|legacy] \
        [--max-recall-drop 0.05] [--max-accuracy-drop 0.02] \
        [--json compare.json]

``--run`` / ``--baseline`` each accept one or more JSON files or
directories (directories are scanned for ``*.json``).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

SUPERSEDED_STATUSES = frozenset({"outdated", "conflicted"})

# How each gold source_uri was satisfied (or not).
GOLD_RETRIEVED = "retrieved"
GOLD_SUPERSEDED_CREDITED = "superseded_credited"
GOLD_SUPERSEDED_LOST = "superseded_lost"
GOLD_MISSING = "missing"

_SATISFIED = frozenset({GOLD_RETRIEVED, GOLD_SUPERSEDED_CREDITED})


@dataclass
class GoldOutcome:
    """Per-gold classification, kept for the audit listing."""

    uri: str
    outcome: str
    # The retrieved memory whose supersedes chain reached this gold
    # (credited case), or None.
    superseding_id: str | None = None


@dataclass
class QuestionScore:
    question_id: str
    question_type: str
    recall: float | None  # None when the question has no gold set
    model_answer: bool | None
    golds: list[GoldOutcome] = field(default_factory=list)
    degraded: bool = False  # scored legacy-style for lack of status data


def _chain_reaches(
    start_id: str,
    target_ids: frozenset[str],
    supersedes_of: dict[str, str],
) -> bool:
    """Walk ``supersedes_id`` pointers from ``start_id``; True if a target is hit.

    Chains resolve newest-first in the product (C supersedes B supersedes A),
    so a retrieved C must credit a gold A. A visited-set guards against a
    corrupt cycle the same way ``load_and_serialize`` does.
    """
    seen: set[str] = set()
    cur: str | None = start_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        cur = supersedes_of.get(cur)
        if cur is not None and cur in target_ids:
            return True
    return False


def score_question(result: dict, mode: str) -> QuestionScore:
    """Recompute one question's recall from its raw artifacts.

    ``result`` is one entry of the runner's ``results`` list. Only
    ``answer_source_uris``, ``recalled_memories`` and (for the
    supersession-aware mode) ``all_stored_memories`` are consulted;
    the runner's own ``retrieval_metrics`` is deliberately ignored.
    """
    gold_uris: list[str] = list(dict.fromkeys(result.get("answer_source_uris") or []))
    retrieved: list[dict] = result.get("recalled_memories") or []
    stored: list[dict] = result.get("all_stored_memories") or []

    qid = str(result.get("question_id") or result.get("test_id") or "?")
    qtype = str(result.get("question_type") or "unknown")
    model_answer = result.get("model_answer")
    if not isinstance(model_answer, bool):
        model_answer = None

    if not gold_uris:
        return QuestionScore(qid, qtype, None, model_answer)

    retrieved_uris = {m.get("source_uri") for m in retrieved if m.get("source_uri")}

    # id -> supersedes_id pointers from every row we can see. Retrieved rows
    # carry the pointer on the wire; stored snapshots may or may not.
    supersedes_of: dict[str, str] = {}
    for m in list(stored) + list(retrieved):
        mid, sup = m.get("id"), m.get("supersedes_id")
        if mid and sup:
            supersedes_of[str(mid)] = str(sup)

    # source_uri -> stored rows (re-ingestion can duplicate a uri).
    stored_by_uri: dict[str, list[dict]] = {}
    for m in stored:
        uri = m.get("source_uri")
        if uri:
            stored_by_uri.setdefault(uri, []).append(m)

    degraded = mode != "legacy" and not stored

    golds: list[GoldOutcome] = []
    for uri in gold_uris:
        if uri in retrieved_uris:
            golds.append(GoldOutcome(uri, GOLD_RETRIEVED))
            continue
        if mode == "legacy" or degraded:
            golds.append(GoldOutcome(uri, GOLD_MISSING))
            continue

        rows = stored_by_uri.get(uri, [])
        # Superseded only when every stored copy of the uri was retired; an
        # active copy that simply wasn't retrieved is a plain miss.
        if not rows or any(r.get("status") not in SUPERSEDED_STATUSES for r in rows):
            golds.append(GoldOutcome(uri, GOLD_MISSING))
            continue

        target_ids = frozenset(str(r["id"]) for r in rows if r.get("id"))
        credit_from = next(
            (
                str(m["id"])
                for m in retrieved
                if m.get("id")
                and _chain_reaches(str(m["id"]), target_ids, supersedes_of)
            ),
            None,
        )
        if credit_from is not None:
            golds.append(GoldOutcome(uri, GOLD_SUPERSEDED_CREDITED, credit_from))
        else:
            golds.append(GoldOutcome(uri, GOLD_SUPERSEDED_LOST))

    satisfied = sum(1 for g in golds if g.outcome in _SATISFIED)
    return QuestionScore(
        qid, qtype, satisfied / len(gold_uris), model_answer, golds, degraded
    )


@dataclass
class CategoryScore:
    accuracy: float | None
    recall: float | None
    questions: int
    superseded_total: int
    superseded_credited: int
    superseded_lost: int
    degraded: int


def score_category(
    results: list[dict], mode: str
) -> tuple[CategoryScore, list[QuestionScore]]:
    scores = [score_question(r, mode) for r in results]
    recalls = [s.recall for s in scores if s.recall is not None]
    answers = [s.model_answer for s in scores if s.model_answer is not None]
    outcomes = [g.outcome for s in scores for g in s.golds]
    return (
        CategoryScore(
            accuracy=(sum(answers) / len(answers)) if answers else None,
            recall=mean(recalls) if recalls else None,
            questions=len(scores),
            superseded_total=sum(
                o in (GOLD_SUPERSEDED_CREDITED, GOLD_SUPERSEDED_LOST) for o in outcomes
            ),
            superseded_credited=outcomes.count(GOLD_SUPERSEDED_CREDITED),
            superseded_lost=outcomes.count(GOLD_SUPERSEDED_LOST),
            degraded=sum(1 for s in scores if s.degraded),
        ),
        scores,
    )


def load_categories(paths: list[Path]) -> dict[str, list[dict]]:
    """Load runner results files; return {category: [per-question dicts]}."""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        else:
            files.append(p)
    categories: dict[str, list[dict]] = {}
    for f in files:
        data = json.loads(f.read_text())
        default_type = (data.get("benchmark_metadata") or {}).get("question_type")
        for r in data.get("results") or []:
            qtype = str(r.get("question_type") or default_type or "unknown")
            r.setdefault("question_type", qtype)
            categories.setdefault(qtype, []).append(r)
    return categories


def _fmt(v: float | None, pct: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}" if pct else f"{v:.3f}"


def _fmt_delta(new: float | None, old: float | None) -> str:
    if new is None or old is None:
        return "—"
    return f"{(new - old) * 100:+.1f}pp"


def compare(
    run: dict[str, list[dict]],
    baseline: dict[str, list[dict]],
    mode: str,
    max_recall_drop: float,
    max_accuracy_drop: float,
) -> dict:
    """Score both sides with the SAME mode and build the comparison report."""
    report: dict = {"recall_mode": mode, "categories": {}, "breaches": []}
    audit_lines: list[str] = []

    for cat in sorted(set(run) | set(baseline)):
        run_cat, run_qs = score_category(run[cat], mode) if cat in run else (None, [])
        base_cat, _ = (
            score_category(baseline[cat], mode) if cat in baseline else (None, [])
        )
        entry: dict = {}
        flags: list[str] = []
        if run_cat and base_cat:
            if (
                run_cat.recall is not None
                and base_cat.recall is not None
                and base_cat.recall - run_cat.recall > max_recall_drop
            ):
                flags.append("recall")
            if (
                run_cat.accuracy is not None
                and base_cat.accuracy is not None
                and base_cat.accuracy - run_cat.accuracy > max_accuracy_drop
            ):
                flags.append("accuracy")
        elif run_cat is None or base_cat is None:
            flags.append("missing-side")
        for name, c in (("run", run_cat), ("baseline", base_cat)):
            entry[name] = (
                None
                if c is None
                else {
                    "accuracy": c.accuracy,
                    "recall": c.recall,
                    "questions": c.questions,
                    "gold_superseded": c.superseded_total,
                    "gold_superseded_credited": c.superseded_credited,
                    "gold_superseded_lost": c.superseded_lost,
                    "degraded_questions": c.degraded,
                }
            )
        entry["flags"] = flags
        report["categories"][cat] = entry
        if flags and flags != ["missing-side"]:
            report["breaches"].append(cat)

        # Audit listing: every superseded gold in the RUN, with its crediting
        # successor when there is one. This is what to read before trusting
        # the verdict either way (reg-d15).
        for q in run_qs:
            for g in q.golds:
                if g.outcome == GOLD_SUPERSEDED_CREDITED:
                    audit_lines.append(
                        f"  [{cat}] q={q.question_id} gold {g.uri} superseded; "
                        f"credited via retrieved successor {g.superseding_id}"
                    )
                elif g.outcome == GOLD_SUPERSEDED_LOST:
                    audit_lines.append(
                        f"  [{cat}] q={q.question_id} gold {g.uri} superseded; "
                        f"NO successor retrieved -> counted as a miss"
                    )
    report["_audit_lines"] = audit_lines
    return report


def render(report: dict, max_recall_drop: float, max_accuracy_drop: float) -> str:
    mode = report["recall_mode"]
    lines = [
        f"regression-sample comparison (recall mode: {mode})",
        (
            f"thresholds: recall drop > {max_recall_drop:.3f}, "
            f"accuracy drop > {max_accuracy_drop:.3f}"
        ),
        "",
        (
            f"{'category':<28} {'acc(base)':>9} {'acc(run)':>9} {'Δacc':>8} "
            f"{'rec(base)':>9} {'rec(run)':>9} {'Δrec':>8}  flags"
        ),
    ]
    for cat, entry in report["categories"].items():
        b, r = entry["baseline"], entry["run"]
        lines.append(
            f"{cat:<28} "
            f"{_fmt(b and b['accuracy'], pct=True):>9} "
            f"{_fmt(r and r['accuracy'], pct=True):>9} "
            f"{_fmt_delta(r and r['accuracy'], b and b['accuracy']):>8} "
            f"{_fmt(b and b['recall']):>9} "
            f"{_fmt(r and r['recall']):>9} "
            f"{_fmt_delta(r and r['recall'], b and b['recall']):>8}  "
            + (
                "ok"
                if not entry["flags"]
                else (
                    "⚠ missing-side"
                    if entry["flags"] == ["missing-side"]
                    else "❌ " + ",".join(entry["flags"])
                )
            )
        )
        if r and (r["gold_superseded"] or r["degraded_questions"]):
            lines.append(
                f"{'':<28}   run golds superseded: {r['gold_superseded']} "
                f"(credited {r['gold_superseded_credited']}, "
                f"lost {r['gold_superseded_lost']})"
                + (
                    f"; {r['degraded_questions']} question(s) scored legacy-style "
                    f"(no stored-memory statuses in artifact)"
                    if r["degraded_questions"]
                    else ""
                )
            )
    audit = report.get("_audit_lines") or []
    if audit:
        lines += ["", "superseded-gold audit (run):", *audit]
        lines.append(
            "  note: 'credited' means the metric no longer penalises the "
            "supersession — it does NOT certify the supersession was correct. "
            "Audit these pairs when the count moves (reg-d15)."
        )
    lines.append("")
    lines.append(
        "RESULT: "
        + ("FAIL — " + ", ".join(report["breaches"]) if report["breaches"] else "PASS")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--run", nargs="+", type=Path, required=True)
    ap.add_argument("--baseline", nargs="+", type=Path, required=True)
    ap.add_argument(
        "--recall-mode",
        choices=("supersession-aware", "legacy"),
        default="supersession-aware",
        help="legacy reproduces the historical status-blind counting",
    )
    ap.add_argument("--max-recall-drop", type=float, default=0.05)
    ap.add_argument("--max-accuracy-drop", type=float, default=0.02)
    ap.add_argument(
        "--json", type=Path, default=None, help="also write the report as JSON"
    )
    args = ap.parse_args(argv)

    report = compare(
        load_categories(args.run),
        load_categories(args.baseline),
        args.recall_mode,
        args.max_recall_drop,
        args.max_accuracy_drop,
    )
    print(render(report, args.max_recall_drop, args.max_accuracy_drop))
    if args.json:
        serializable = {k: v for k, v in report.items() if k != "_audit_lines"}
        args.json.write_text(json.dumps(serializable, indent=2) + "\n")
    return 1 if report["breaches"] else 0


if __name__ == "__main__":
    sys.exit(main())
