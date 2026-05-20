"""CAURA-125 wet test — direction-invariance of contradiction detection.

The gap (A6 in the consolidated gap list): the same two statements can
produce different contradiction verdicts depending on which one is
presented as the "new" memory. The audit measured this at dir_inv =
0.795 (41 / 200 pairs flipped verdict under A↔B swap).

This script isolates the LLM-side component of that asymmetry by
calling the real ``CONTRADICTION_PROMPT`` twice for each fixture —
once with (A as new, B as old) and once with (B as new, A as old) —
and asserting that the parser verdict is the same in both directions.

A fixture is "direction-invariant" iff both orders produce the same
``_parse_contradiction_response`` verdict. A failure means the LLM
(or our parser layer) saw the two statements as semantically
asymmetric — i.e., the model's reasoning depended on which side
carried the "Statement A (NEW)" framing.

This wet test does NOT exercise the candidate-set filter at the
detection layer (``_candidate_is_older`` in
``contradiction_detector.py:42``). That asymmetry is covered by the
unit tests in ``tests/test_contradiction_direction_invariance.py``
(landing in a later commit of this PR). The two together quantify
the gap end-to-end.

Two flavours of fixture:

  - **genuine_conflict (8)**: same subject, mutually exclusive claims.
    Direction-invariance means BOTH orders produce ``contradicts=True``.
  - **non_conflict (7)**: one of the seven shapes from CAURA-124 that
    Gate 2 must classify as non-conflict. Direction-invariance means
    BOTH orders produce ``contradicts=False``.

Usage:
    export OPENAI_API_KEY=sk-...
    export GEMINI_API_KEY=...  # or GOOGLE_API_KEY=...

    # Single provider
    python scripts/wet_test_direction_invariance.py --provider openai
    python scripts/wet_test_direction_invariance.py --provider gemini

    # With JSONL artifact for before/after comparison
    python scripts/wet_test_direction_invariance.py --provider openai \\
        --json-out scripts/wet_test_baselines/caura-125-baseline.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass

# Make core-api, core-storage-api, core-worker, and the repo root importable
# without installing the packages. Mirrors pytest.ini's pythonpath.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for sub in ("core-api/src", "core-storage-api/src", "core-worker/src", "."):
    sys.path.insert(0, os.path.join(ROOT, sub))

from core_api.services.contradiction_detector import (  # noqa: E402
    CONTRADICTION_PROMPT,
    _parse_contradiction_response,
)


@dataclass(frozen=True)
class Pair:
    """A single direction-invariance probe.

    The ``a`` and ``b`` content strings are presented to the model
    once as (new=a, old=b) and once as (new=b, old=a). The fixture
    passes when both calls produce the same parser verdict equal to
    ``expected_contradicts``.
    """

    label: str
    a: str
    b: str
    expected_contradicts: bool
    shape: str  # "genuine_conflict" or one of the CAURA-124 non_conflict_reasons
    note: str


PAIRS: list[Pair] = [
    # ----- Genuine conflicts (BOTH orders must flag) -----
    Pair(
        label="ship_date_slip",
        a="Project Falcon ships in Q4 2026.",
        b="Project Falcon ships in Q2 2026.",
        expected_contradicts=True,
        shape="genuine_conflict",
        note="Same project, two future ship dates — true conflict.",
    ),
    Pair(
        label="address_change",
        a="Alice lives in Haifa.",
        b="Alice lives in Tel Aviv.",
        expected_contradicts=True,
        shape="genuine_conflict",
        note="Single-value predicate (lives_in) flipped.",
    ),
    Pair(
        label="role_swap_ceo",
        a="The CEO of Acme is Alice.",
        b="The CEO of Acme is Bob.",
        expected_contradicts=True,
        shape="genuine_conflict",
        note="``ceo_of`` is single-valued — Alice and Bob can't both hold it.",
    ),
    Pair(
        label="status_flip",
        a="Project Atlas is launched.",
        b="Project Atlas has not been launched yet.",
        expected_contradicts=True,
        shape="genuine_conflict",
        note="Polar negation of the same state claim.",
    ),
    Pair(
        label="numeric_mismatch_budget",
        a="Acme's Q2 budget is $500k.",
        b="Acme's Q2 budget is $1.2M.",
        expected_contradicts=True,
        shape="genuine_conflict",
        note="Same period, two non-overlapping budget figures.",
    ),
    Pair(
        label="version_conflict",
        a="The running version of memclaw is 2.3.1.",
        b="The running version of memclaw is 2.4.0.",
        expected_contradicts=True,
        shape="genuine_conflict",
        note="``running_version`` is single-valued.",
    ),
    Pair(
        label="location_conflict_hq",
        a="Globex is headquartered in Berlin.",
        b="Globex is headquartered in Munich.",
        expected_contradicts=True,
        shape="genuine_conflict",
        note="``headquartered_in`` is single-valued.",
    ),
    Pair(
        label="boolean_conflict_public_private",
        a="Initech is publicly traded.",
        b="Initech is privately held.",
        expected_contradicts=True,
        shape="genuine_conflict",
        note="Public vs private are mutually exclusive states.",
    ),
    # ----- Non-conflicts (BOTH orders must NOT flag) -----
    # One per CAURA-124 shape; same fixture text as the post-fix wet
    # test for the prompt gate, so the two harnesses cross-reference.
    Pair(
        label="nc_temporal_supersession",
        a="The Atlas feature shipped on 2026-09-12.",
        b="The Atlas feature is planned for Q3 2026.",
        expected_contradicts=False,
        shape="temporal_supersession",
        note="Plan-then-ship sequence; both true sequentially.",
    ),
    Pair(
        label="nc_list_valued",
        a="Project Atlas supports French.",
        b="Project Atlas supports English.",
        expected_contradicts=False,
        shape="list_valued_predicate",
        note="``supports`` is list-valued.",
    ),
    Pair(
        label="nc_refinement",
        a="Acme is headquartered in Munich, Germany.",
        b="Acme is headquartered in Europe.",
        expected_contradicts=False,
        shape="refinement",
        note="Coarse → fine geographic granularity.",
    ),
    Pair(
        label="nc_scope_mismatch",
        a="Acme's Europe division is profitable.",
        b="Acme is not profitable.",
        expected_contradicts=False,
        shape="scope_mismatch",
        note="Whole vs part — parent loses, one division profits.",
    ),
    Pair(
        label="nc_same_name_distinct_subject",
        a="Today's standup (2026-03-10) ran 45 minutes over.",
        b="Today's standup (2026-03-09) got cancelled.",
        expected_contradicts=False,
        shape="same_name_distinct_subject",
        note="Different dates — distinct meeting instances.",
    ),
    Pair(
        label="nc_conditional_unrealized",
        a="We're shipping in Q1 2027.",
        b="If we hire 10 engineers in Q3, we will ship by Q4 2026.",
        expected_contradicts=False,
        shape="conditional_unrealized",
        note="Realised state vs unmet conditional.",
    ),
    Pair(
        label="nc_event_restatement",
        a="Acme is acquiring Globex.",
        b="Acme acquired Globex last March.",
        expected_contradicts=False,
        shape="event_restatement",
        note="Same deal at different points in time; tense/aspect only.",
    ),
]


DEFAULT_MODEL = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}


def _build_openai_caller(model: str):
    try:
        from openai import OpenAI
    except ImportError:
        print(
            "ERROR: openai SDK not installed. Run: pip install openai", file=sys.stderr
        )
        sys.exit(2)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: set OPENAI_API_KEY in your environment", file=sys.stderr)
        sys.exit(2)
    client = OpenAI(api_key=api_key)

    def _call(prompt: str) -> dict:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"_raw": content, "_parse_error": True}

    return _call


def _build_gemini_caller(model: str):
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print(
            "ERROR: google-genai SDK not installed. Run: pip install google-genai",
            file=sys.stderr,
        )
        sys.exit(2)
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print(
            "ERROR: set GEMINI_API_KEY (or GOOGLE_API_KEY) in your environment",
            file=sys.stderr,
        )
        sys.exit(2)
    client = genai.Client(api_key=api_key)

    def _call(prompt: str) -> dict:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        try:
            text = response.text or ""
        except ValueError as exc:
            return {"_raw": "", "_parse_error": True, "_provider_error": str(exc)}
        if not text:
            return {
                "_raw": "",
                "_parse_error": True,
                "_provider_error": "empty content",
            }
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text, "_parse_error": True}
        if not isinstance(parsed, dict):
            return {"_raw": text, "_parse_error": True, "_shape": type(parsed).__name__}
        return parsed

    return _call


def call_model(caller, new_content: str, old_content: str) -> dict:
    """Call the real LLM with the actual CONTRADICTION_PROMPT in JSON mode.

    Mirrors the production call site's [:500] truncation so the wet
    test exercises the exact prompt shape production sends.
    """
    prompt = CONTRADICTION_PROMPT.format(
        new_content=new_content[:500],
        old_content=old_content[:500],
    )
    return caller(prompt)


def run_once(caller, provider: str, model: str, *, run_id: int) -> dict:
    """Run every pair forward AND reverse. Return per-shape rollup."""
    print(f"\n{'=' * 78}\nRUN {run_id}  provider={provider}  model={model}\n{'=' * 78}")

    cases_out: list[dict] = []
    by_shape: dict[str, dict[str, int]] = {}
    dir_inv_pass = 0
    dir_inv_fail = 0
    verdict_pass = 0  # both directions land on expected value
    for pair in PAIRS:
        t0 = time.perf_counter()
        raw_ab = call_model(caller, pair.a, pair.b)
        verdict_ab = _parse_contradiction_response(raw_ab)
        raw_ba = call_model(caller, pair.b, pair.a)
        verdict_ba = _parse_contradiction_response(raw_ba)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        # Direction-invariance: both orders agree, regardless of whether
        # they agree with ``expected``. This is the main metric A6 targets.
        dir_inv_ok = verdict_ab == verdict_ba

        # Verdict correctness: both orders land on the expected value.
        # Strictly stronger than dir_inv_ok — a pair that's invariantly
        # WRONG (both directions say True when expected False) counts
        # as dir_inv_ok=True but verdict_ok=False.
        verdict_ok = (
            verdict_ab == pair.expected_contradicts
            and verdict_ba == pair.expected_contradicts
        )

        dir_inv_pass += int(dir_inv_ok)
        dir_inv_fail += int(not dir_inv_ok)
        verdict_pass += int(verdict_ok)

        shape_bucket = by_shape.setdefault(
            pair.shape, {"dir_inv_pass": 0, "dir_inv_fail": 0}
        )
        shape_bucket["dir_inv_pass" if dir_inv_ok else "dir_inv_fail"] += 1

        status = "OK " if dir_inv_ok else "ASYM"
        print(f"\n[{status}] {pair.label}  shape={pair.shape}  ({latency_ms} ms)")
        print(f"  note          : {pair.note}")
        print(f"  expected      : contradicts={pair.expected_contradicts}")
        print(
            f"  forward  (A,B): verdict={verdict_ab}  raw={json.dumps(raw_ab, ensure_ascii=False)}"
        )
        print(
            f"  reverse  (B,A): verdict={verdict_ba}  raw={json.dumps(raw_ba, ensure_ascii=False)}"
        )
        if not dir_inv_ok:
            print(f"  >>> ASYMMETRY: forward={verdict_ab} reverse={verdict_ba}")
        elif not verdict_ok:
            print(
                f"  >>> INVARIANTLY WRONG: both directions={verdict_ab}, expected={pair.expected_contradicts}"
            )

        cases_out.append(
            {
                "label": pair.label,
                "shape": pair.shape,
                "expected_contradicts": pair.expected_contradicts,
                "raw_forward": raw_ab,
                "raw_reverse": raw_ba,
                "verdict_forward": verdict_ab,
                "verdict_reverse": verdict_ba,
                "dir_inv_ok": dir_inv_ok,
                "verdict_ok": verdict_ok,
                "latency_ms": latency_ms,
            }
        )

    print(f"\nRun {run_id} summary:")
    print(f"  direction-invariance: {dir_inv_pass}/{dir_inv_pass + dir_inv_fail}")
    print(f"  verdict correctness : {verdict_pass}/{dir_inv_pass + dir_inv_fail}")
    print(f"\nPer-shape direction-invariance ({provider}/{model}):")
    for shape in sorted(by_shape):
        b = by_shape[shape]
        total = b["dir_inv_pass"] + b["dir_inv_fail"]
        pct = (100.0 * b["dir_inv_pass"] / total) if total else 0.0
        print(f"  {shape:<32} {b['dir_inv_pass']:>2}/{total:<2}  ({pct:5.1f}%)")
    return {
        "provider": provider,
        "model": model,
        "run_id": run_id,
        "dir_inv_pass": dir_inv_pass,
        "dir_inv_fail": dir_inv_fail,
        "verdict_pass": verdict_pass,
        "by_shape": by_shape,
        "cases": cases_out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--provider",
        choices=("openai", "gemini"),
        default="openai",
        help="LLM provider (default: openai)",
    )
    ap.add_argument(
        "--model",
        default=None,
        help="Model id; default depends on --provider "
        f"({DEFAULT_MODEL['openai']} for openai, {DEFAULT_MODEL['gemini']} for gemini)",
    )
    ap.add_argument("--runs", type=int, default=1, help="Repeat all cases N times")
    ap.add_argument(
        "--json-out",
        default=None,
        help="If set, write the structured run summary to this path (JSONL).",
    )
    args = ap.parse_args()

    model = args.model or DEFAULT_MODEL[args.provider]
    if args.provider == "openai":
        caller = _build_openai_caller(model)
    elif args.provider == "gemini":
        caller = _build_gemini_caller(model)
    else:  # unreachable due to argparse choices
        raise ValueError(f"unknown provider: {args.provider}")

    summaries: list[dict] = []
    total_di_pass = 0
    total_di_fail = 0
    total_verdict_pass = 0
    for run_id in range(1, args.runs + 1):
        s = run_once(caller, args.provider, model, run_id=run_id)
        total_di_pass += s["dir_inv_pass"]
        total_di_fail += s["dir_inv_fail"]
        total_verdict_pass += s["verdict_pass"]
        summaries.append(s)

    print(f"\n{'=' * 78}")
    print(
        f"OVERALL  dir_inv: {total_di_pass}/{total_di_pass + total_di_fail}"
        f"  verdict: {total_verdict_pass}/{total_di_pass + total_di_fail}"
        f"  across {args.runs} run(s) × {len(PAIRS)} pairs"
        f"  [provider={args.provider} model={model}]"
    )
    print(f"{'=' * 78}")

    if args.json_out:
        if os.path.exists(args.json_out) and os.path.getsize(args.json_out) > 0:
            print(
                f"WARNING: appending to existing non-empty file {args.json_out}; "
                f"re-running will create duplicate records — use a fresh path "
                f"if that is not intended.",
                file=sys.stderr,
            )
        with open(args.json_out, "a") as f:
            for s in summaries:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"Wrote {len(summaries)} run summary record(s) to {args.json_out}")

    # Exit code reflects direction-invariance — pass/fail of the audit.
    return 0 if total_di_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
