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
]


DEFAULT_MODEL = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}


def _build_openai_caller(model: str):
    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: openai SDK not installed. Run: pip install openai", file=sys.stderr)
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
            return {"_raw": "", "_parse_error": True, "_provider_error": "empty content"}
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


def run_once(caller, provider: str, model: str, *, run_id: int) -> tuple[int, int]:
    """Run all cases once. Returns (passed, failed)."""
    passed = 0
    failed = 0
    print(f"\n{'=' * 78}\nRUN {run_id}  provider={provider}  model={model}\n{'=' * 78}")
    for case in CASES:
        t0 = time.perf_counter()
        raw = call_model(caller, case.new_content, case.old_content)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        verdict = _parse_contradiction_response(raw)
        ok = verdict == case.expected
        passed += int(ok)
        failed += int(not ok)

        status = "PASS" if ok else "FAIL"
        print(f"\n[{status}] {case.label}  ({latency_ms} ms)")
        print(f"  note          : {case.note}")
        print(f"  new (A)       : {case.new_content}")
        print(f"  old (B)       : {case.old_content}")
        print(f"  expected      : contradiction={case.expected}")
        print(f"  model JSON    : {json.dumps(raw, ensure_ascii=False)}")
        print(f"  parser verdict: {verdict}")
        if not ok:
            print(f"  >>> MISMATCH: expected {case.expected}, got {verdict}")
    print(f"\nRun {run_id} summary: {passed}/{passed + failed} passed")
    return passed, failed


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
    for run_id in range(1, args.runs + 1):
        p, f = run_once(caller, args.provider, model, run_id=run_id)
        total_pass += p
        total_fail += f

    print(f"\n{'=' * 78}")
    print(
        f"OVERALL: {total_pass}/{total_pass + total_fail} passed across "
        f"{args.runs} run(s), {len(CASES)} cases each  "
        f"[provider={args.provider} model={model}]"
    )
    print(f"{'=' * 78}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
