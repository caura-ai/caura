"""Wet test for the CAURA-111 contradiction-detection prompt fix.

Hits a real LLM with the actual ``CONTRADICTION_PROMPT`` from
``core_api.services.contradiction_detector`` and runs it over a curated
list of memory pairs covering:

  - The documented cross-subject false-positive shapes that motivated the fix
    (Sarah Johnson / David Patel, Daniel Cohen / Daniel Levi).
  - Genuine same-subject contradictions (must still fire).
  - Same-subject complementary facts (must NOT fire).
  - More-specific-version cases (must NOT fire).

For each pair the script prints:
  - Raw model JSON (so you can inspect subject_a / subject_b / same_subject /
    contradicts / reason).
  - The parser verdict from the real ``_parse_contradiction_response`` —
    proves the hard gate behaves end-to-end.
  - Whether the case matches its expected verdict.

Two providers are supported. Each uses the same JSON-output, temperature=0.0
configuration the codebase uses in production:

  OpenAI Chat Completions   — ``response_format={"type": "json_object"}``
  Gemini Developer API      — ``response_mime_type="application/json"``
                              (matches ``common.llm.providers.gemini``)

Usage:
    # OpenAI (default)
    export OPENAI_API_KEY=sk-...
    python scripts/wet_test_contradiction_prompt.py
    python scripts/wet_test_contradiction_prompt.py --model gpt-4o-mini

    # Gemini
    export GEMINI_API_KEY=...
    python scripts/wet_test_contradiction_prompt.py --provider gemini
    python scripts/wet_test_contradiction_prompt.py --provider gemini --model gemini-2.5-flash

    # Repeat all cases for variance check (temp=0 so deltas are model-side noise)
    python scripts/wet_test_contradiction_prompt.py --runs 3

The script is a standalone diagnostic tool. It does NOT replace the unit
tests in ``tests/test_contradiction_subject_gate.py`` — those exercise the
parser hard-gate deterministically. This script exercises the *prompt*
against a real model.
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

# SDKs are imported lazily inside the provider-specific call paths so that
# you only need the SDK for the provider you actually use.


@dataclass(frozen=True)
class Case:
    label: str
    new_content: str
    old_content: str
    expected: bool  # True = should be flagged as contradiction
    note: str
    # CAURA-124 — shape tag groups fixtures into the 7 within-subject
    # false-positive classes. ``existing`` covers the original CAURA-111
    # fixtures plus the genuine-contradiction TPs.
    shape: str = "existing"
    # The enum value the model is expected to emit on the
    # ``non_conflict_reason`` field once the gate ships in commit 3.
    # ``None`` means the test does not assert a specific
    # non_conflict_reason. Parser correctness for these cases is
    # verified through the ``ok`` field instead.
    expected_non_conflict_reason: str | None = None


CASES: list[Case] = [
    # ----- Documented cross-subject false positives (must NOT fire) -----
    Case(
        label="sarah_vs_david_pref_1",
        new_content="Sarah Johnson prefers iced coffee in the morning.",
        old_content="David Patel prefers hot tea in the morning.",
        expected=False,
        note="Different people, opposite-looking preferences.",
    ),
    Case(
        label="sarah_vs_david_pref_2",
        new_content="Sarah Johnson does not like working from the office.",
        old_content="David Patel likes working from the office.",
        expected=False,
        note="Different people, polar opposite predicate.",
    ),
    Case(
        label="daniel_cohen_vs_daniel_levi_role",
        new_content="Daniel Cohen joined Acme as Head of Engineering.",
        old_content="Daniel Levi left Acme as Head of Engineering last quarter.",
        expected=False,
        note="Shared first name, different individuals.",
    ),
    Case(
        label="daniel_cohen_vs_daniel_levi_location",
        new_content="Daniel Cohen relocated to Tel Aviv.",
        old_content="Daniel Levi relocated to Berlin.",
        expected=False,
        note="Shared first name, non-conflicting facts.",
    ),
    # ----- Genuine same-subject contradictions (MUST fire) -----
    Case(
        label="alice_current_address_conflict",
        new_content="Alice lives in Haifa.",
        old_content="Alice lives in Tel Aviv.",
        expected=True,
        note="Same person, two undated current-state claims — must conflict.",
    ),
    Case(
        label="acme_ship_date_slip",
        new_content="Acme's Project Falcon ships in Q4 2026.",
        old_content="Acme's Project Falcon ships in Q2 2026.",
        expected=True,
        note="Same project, ship-date slip — true contradiction.",
    ),
    # ----- Genuinely historical, non-overlapping periods (must NOT fire) -----
    Case(
        label="historical_residence_not_contradiction",
        new_content="Alice lived in Haifa from 2010 to 2014.",
        old_content="Alice lived in Tel Aviv from 2015 to 2018.",
        expected=False,
        note="Non-overlapping past periods — both historically true.",
    ),
    # ----- Same-subject complementary facts (must NOT fire) -----
    Case(
        label="alice_two_facts",
        new_content="Alice was promoted to Senior Engineer last month.",
        old_content="Alice has been on the platform team since 2024.",
        expected=False,
        note="Same person, complementary facts.",
    ),
    Case(
        label="more_specific_not_contradiction",
        new_content="Alice lives in Tel Aviv on Rothschild Boulevard.",
        old_content="Alice lives in Tel Aviv.",
        expected=False,
        note="More specific version of the same fact.",
    ),
    # ===================================================================
    # CAURA-124 — within-subject false-positive shapes (must NOT fire).
    # Each shape has 3 cases. Until the ``non_conflict_reason`` gate
    # ships (commit 3 of this PR), most of these will misfire — that is
    # the baseline this fixture set is here to measure.
    # ===================================================================
    # ----- Shape 1: temporal_supersession (planned → shipped, etc.) -----
    Case(
        label="ts_planned_shipped",
        new_content="The Atlas feature shipped on 2026-09-12.",
        old_content="The Atlas feature is planned for Q3 2026.",
        expected=False,
        note="Plan-then-ship sequence; both true sequentially.",
        shape="temporal_supersession",
        expected_non_conflict_reason="temporal_supersession",
    ),
    Case(
        label="ts_ticket_open_closed",
        new_content="Ticket OPS-4521 is closed.",
        old_content="Ticket OPS-4521 is open.",
        expected=False,
        note="State machine transition: open → closed is supersession.",
        shape="temporal_supersession",
        expected_non_conflict_reason="temporal_supersession",
    ),
    Case(
        label="ts_draft_published",
        new_content="The Q3 board memo was published last Friday.",
        old_content="The Q3 board memo is in draft.",
        expected=False,
        note="Document lifecycle: draft → published is supersession.",
        shape="temporal_supersession",
        expected_non_conflict_reason="temporal_supersession",
    ),
    # ----- Shape 2: list_valued_predicate (multi-value attributes) -----
    Case(
        label="lv_supports_languages",
        new_content="Project Atlas supports French.",
        old_content="Project Atlas supports English.",
        expected=False,
        note="``supports`` is list-valued; both can hold simultaneously.",
        shape="list_valued_predicate",
        expected_non_conflict_reason="list_valued_predicate",
    ),
    Case(
        label="lv_speaks_languages",
        new_content="Maria speaks Hebrew.",
        old_content="Maria speaks Spanish.",
        expected=False,
        note="``speaks`` is list-valued; multiple languages coexist.",
        shape="list_valued_predicate",
        expected_non_conflict_reason="list_valued_predicate",
    ),
    Case(
        label="lv_matrix_reports_to",
        new_content="Bob reports to Carol on the product side of the matrix.",
        old_content="Bob reports to Alice on the engineering side of the matrix.",
        expected=False,
        note="Matrix orgs: ``reports_to`` can be plural; both true.",
        shape="list_valued_predicate",
        expected_non_conflict_reason="list_valued_predicate",
    ),
    # ----- Shape 3: refinement (same fact, finer granularity) -----
    Case(
        label="rf_geo_coarse_fine",
        new_content="Acme is headquartered in Munich, Germany.",
        old_content="Acme is headquartered in Europe.",
        expected=False,
        note="Same fact, finer geographic granularity. Not a conflict.",
        shape="refinement",
        expected_non_conflict_reason="refinement",
    ),
    Case(
        label="rf_industry_coarse_fine",
        new_content="John works at Google as a senior engineer.",
        old_content="John works in tech.",
        expected=False,
        note="Tech → Google is refinement, not a different claim.",
        shape="refinement",
        expected_non_conflict_reason="refinement",
    ),
    Case(
        label="rf_date_coarse_fine",
        new_content="The launch is on September 15, 2026.",
        old_content="The launch is in Q3 2026.",
        expected=False,
        note="Q3 2026 → September 15, 2026 is refinement.",
        shape="refinement",
        expected_non_conflict_reason="refinement",
    ),
    # ----- Shape 4: scope_mismatch (whole/part, qualifier difference) -----
    Case(
        label="sm_whole_part_profitability",
        new_content="Acme's Europe division is profitable.",
        old_content="Acme is not profitable.",
        expected=False,
        note="Whole-vs-part: the parent loses money while one division profits.",
        shape="scope_mismatch",
        expected_non_conflict_reason="scope_mismatch",
    ),
    Case(
        label="sm_annual_vs_quarterly_revenue",
        new_content="Acme's Q2 revenue is $25M.",
        old_content="Acme's annual revenue is $100M.",
        expected=False,
        note="Different time windows of the same metric; both true.",
        shape="scope_mismatch",
        expected_non_conflict_reason="scope_mismatch",
    ),
    Case(
        label="sm_weekday_weekend_residence",
        new_content="John lives in his Vermont cabin on weekends.",
        old_content="John lives in NYC during the work week.",
        expected=False,
        note="Different temporal qualifiers; both residences coexist.",
        shape="scope_mismatch",
        expected_non_conflict_reason="scope_mismatch",
    ),
    # ----- Shape 5: same_name_distinct_subject (ambiguous reference) -----
    Case(
        label="snds_nightly_build_runs",
        new_content="The nightly build passed at 14:30 UTC.",
        old_content="The nightly build failed at 02:00 UTC.",
        expected=False,
        note="``The nightly build`` refers to two different runs of the same job.",
        shape="same_name_distinct_subject",
        expected_non_conflict_reason="same_name_distinct_subject",
    ),
    Case(
        label="snds_recurring_standup",
        new_content="Today's standup (2026-03-10) ran 45 minutes over.",
        old_content="Today's standup (2026-03-09) got cancelled.",
        expected=False,
        note="``Today's standup`` on different dates — distinct meeting instances.",
        shape="same_name_distinct_subject",
        expected_non_conflict_reason="same_name_distinct_subject",
    ),
    Case(
        label="snds_generic_first_name",
        new_content="John just started as an intern this week.",
        old_content="John was promoted to VP last quarter.",
        expected=False,
        note="``John`` is too generic — likely two different people.",
        shape="same_name_distinct_subject",
        expected_non_conflict_reason="same_name_distinct_subject",
    ),
    # ----- Shape 6: conditional_unrealized (irrealis vs factual) -----
    Case(
        label="cu_license_hypothetical",
        new_content="Atlas is closed-source under the proprietary license.",
        old_content="If we adopt the Apache 2.0 license, Atlas would be open-source.",
        expected=False,
        note="Conditional/hypothetical vs realized state; hypothetical isn't a claim.",
        shape="conditional_unrealized",
        expected_non_conflict_reason="conditional_unrealized",
    ),
    Case(
        label="cu_hiring_condition",
        new_content="We're shipping in Q1 2027.",
        old_content="If we hire 10 engineers in Q3, we will ship by Q4 2026.",
        expected=False,
        note="The hiring condition was not met; the conditional doesn't bind.",
        shape="conditional_unrealized",
        expected_non_conflict_reason="conditional_unrealized",
    ),
    Case(
        label="cu_merger_rumour",
        new_content="Acme and Globex remain independent.",
        old_content="If the merger goes through, Acme would absorb Globex.",
        expected=False,
        note="Speculative conditional vs realized state.",
        shape="conditional_unrealized",
        expected_non_conflict_reason="conditional_unrealized",
    ),
    # ----- Shape 7: event_restatement (same event, different lexicalization) -----
    Case(
        label="er_acquired_tense",
        new_content="Acme is acquiring Globex.",
        old_content="Acme acquired Globex last March.",
        expected=False,
        note="Same deal at different points in time; tense/aspect difference only.",
        shape="event_restatement",
        expected_non_conflict_reason="event_restatement",
    ),
    Case(
        label="er_hired_joined",
        new_content="John joined as Head of Marketing.",
        old_content="John was hired as Head of Marketing.",
        expected=False,
        note="Synonymous verbs for the same event.",
        shape="event_restatement",
        expected_non_conflict_reason="event_restatement",
    ),
    Case(
        label="er_deal_closed_aspect",
        new_content="The deal has been closed.",
        old_content="The deal closed yesterday.",
        expected=False,
        note="Perfect vs simple past, same closing event.",
        shape="event_restatement",
        expected_non_conflict_reason="event_restatement",
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
    """Build a caller that mirrors common/llm/providers/gemini.py:
    Developer API key auth, response_mime_type=application/json, temperature=0.0.
    """
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

    The [:500] truncation is intentional — it mirrors the production call
    site in ``_llm_contradiction_check`` (contradiction_detector.py), which
    truncates each statement to 500 chars before formatting the prompt.
    Keeping the same slice here means the wet test exercises the exact
    prompt shape production sends to the model.
    """
    prompt = CONTRADICTION_PROMPT.format(
        new_content=new_content[:500],
        old_content=old_content[:500],
    )
    return caller(prompt)


def run_once(caller, provider: str, model: str, *, run_id: int) -> dict:
    """Run all cases once. Returns a structured summary including per-shape rollup."""
    print(f"\n{'=' * 78}\nRUN {run_id}  provider={provider}  model={model}\n{'=' * 78}")

    cases_out: list[dict] = []
    by_shape: dict[str, dict[str, int]] = {}
    passed = 0
    failed = 0
    for case in CASES:
        t0 = time.perf_counter()
        raw = call_model(caller, case.new_content, case.old_content)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        verdict = _parse_contradiction_response(raw)
        ok = verdict == case.expected
        # ``reason_ok`` is diagnostic-only: the parser verdict can be
        # correct (e.g. contradicts=False) even when the model picked a
        # *different* non_conflict_reason than the fixture expected
        # (e.g. routed the case through "event_restatement" instead of
        # "list_valued_predicate"). That's an interesting signal for
        # tuning the prompt but it does NOT affect pass/fail.
        if case.expected_non_conflict_reason is None:
            # For genuine-contradiction fixtures, verify the model
            # didn't accidentally misfire Gate 2 — i.e., emit a
            # recognised non_conflict_reason for a case that should
            # land at contradicts=true. For "should not flag" fixtures
            # with no specific reason expected (out-of-scope coverage),
            # there is nothing to assert and reason_ok stays True.
            reason_ok = not case.expected or raw.get("non_conflict_reason") in (
                None,
                "none",
            )
        else:
            reason_ok = (
                raw.get("non_conflict_reason") == case.expected_non_conflict_reason
            )
        passed += int(ok)
        failed += int(not ok)
        shape_bucket = by_shape.setdefault(case.shape, {"pass": 0, "fail": 0})
        shape_bucket["pass" if ok else "fail"] += 1

        status = "PASS" if ok else "FAIL"
        print(f"\n[{status}] {case.label}  shape={case.shape}  ({latency_ms} ms)")
        print(f"  note          : {case.note}")
        print(f"  new (A)       : {case.new_content}")
        print(f"  old (B)       : {case.old_content}")
        print(
            f"  expected      : contradiction={case.expected} "
            f"non_conflict_reason={case.expected_non_conflict_reason!r}"
        )
        print(f"  model JSON    : {json.dumps(raw, ensure_ascii=False)}")
        print(f"  parser verdict: {verdict}")
        if not ok:
            print(f"  >>> MISMATCH: expected {case.expected}, got {verdict}")
        if ok and not reason_ok:
            print(
                f"  note: correct verdict but unexpected reason "
                f"(expected {case.expected_non_conflict_reason!r}, "
                f"got {raw.get('non_conflict_reason')!r})"
            )

        cases_out.append(
            {
                "label": case.label,
                "shape": case.shape,
                "expected_contradicts": case.expected,
                "expected_non_conflict_reason": case.expected_non_conflict_reason,
                "raw": raw,
                "parser_verdict": verdict,
                "ok": ok,
                "reason_ok": reason_ok,
                "latency_ms": latency_ms,
            }
        )

    print(f"\nRun {run_id} summary: {passed}/{passed + failed} passed")
    print(f"\nPer-shape ({provider}/{model}):")
    for shape in sorted(by_shape):
        b = by_shape[shape]
        total = b["pass"] + b["fail"]
        pct = (100.0 * b["pass"] / total) if total else 0.0
        print(f"  {shape:<32} {b['pass']:>2}/{total:<2}  ({pct:5.1f}%)")
    return {
        "provider": provider,
        "model": model,
        "run_id": run_id,
        "passed": passed,
        "failed": failed,
        "by_shape": by_shape,
        "cases": cases_out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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
        help="If set, write the structured run summary to this path (one JSON "
        "object per --runs iteration, appended). Used by CAURA-124 to commit "
        "baseline vs post-fix numbers.",
    )
    args = ap.parse_args()

    model = args.model or DEFAULT_MODEL[args.provider]
    if args.provider == "openai":
        caller = _build_openai_caller(model)
    elif args.provider == "gemini":
        caller = _build_gemini_caller(model)
    else:  # unreachable due to argparse choices
        raise ValueError(f"unknown provider: {args.provider}")

    total_pass = 0
    total_fail = 0
    summaries: list[dict] = []
    for run_id in range(1, args.runs + 1):
        summary = run_once(caller, args.provider, model, run_id=run_id)
        total_pass += summary["passed"]
        total_fail += summary["failed"]
        summaries.append(summary)

    print(f"\n{'=' * 78}")
    print(
        f"OVERALL: {total_pass}/{total_pass + total_fail} passed across "
        f"{args.runs} run(s), {len(CASES)} cases each  "
        f"[provider={args.provider} model={model}]"
    )
    print(f"{'=' * 78}")

    if args.json_out:
        # Append-mode so a baseline file accumulates across providers/models
        # in a single artifact. One JSON object per line (JSONL).
        # Warn if appending to a non-empty file — accumulating across
        # providers in one shot is the documented use case, but a stale
        # file from a previous invocation will create duplicate records
        # that quietly skew any per-shape rollup downstream.
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

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
