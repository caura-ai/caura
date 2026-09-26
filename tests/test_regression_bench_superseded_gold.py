"""reg-d15 — the regression bench's recall@20 counted SUPERSEDED gold as missed.

The A63 signature (three hits in the 2026-08-27 sessions, worst in #1025:
temporal-reasoning recall -8.0pp while its accuracy rose +14.3pp): the
contradiction detector retires the older side of an update (status
``outdated``/``conflicted`` + a ``supersedes_id`` chain from the newer row),
present-state search excludes retired rows by design, and the bench's recall
metric counted gold ``source_uri``s against retrieved ``source_uri``s with no
status awareness and no successor credit — so CORRECT supersession read as a
recall regression.

These tests pin the supersession-aware scorer in
``benchmark/regression_sample/compare_to_baseline.py``:

* a correctly-superseded gold whose successor IS retrieved no longer flags a
  regression (the key test — cannot pass before the fix: the scorer module
  did not exist, and its ``legacy`` mode IS the pre-fix counting, asserted
  here to still reproduce the false FAIL);
* a genuinely missing gold still flags;
* a superseded gold whose successor is NOT retrieved still counts as a miss
  in BOTH modes — over-firing that buries information stays visible;
* ``--recall-mode legacy`` reproduces the historical numbers for goldens
  captured before the fix.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = (
    Path(__file__).parent.parent
    / "benchmark"
    / "regression_sample"
    / "compare_to_baseline.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("compare_to_baseline", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: the script uses ``from __future__ import
    # annotations`` + dataclasses, and on 3.14 ``@dataclass`` resolves string
    # annotations through ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ctb = _load()

GOLD_OLD = "longmemeval://sess-1/turn-0"  # e.g. "started reading The Nightingale"
GOLD_NEW = "longmemeval://sess-2/turn-0"  # e.g. "finished The Nightingale"


def _mem(
    mid: str, uri: str | None, status: str = "active", supersedes: str | None = None
):
    return {"id": mid, "source_uri": uri, "status": status, "supersedes_id": supersedes}


def _question(recalled, stored, golds=(GOLD_OLD, GOLD_NEW), answer=True, qid="q1"):
    return {
        "question_id": qid,
        "question_type": "temporal-reasoning",
        "model_answer": answer,
        "answer_source_uris": list(golds),
        "recalled_memories": recalled,
        "all_stored_memories": stored,
    }


# Baseline run: both golds retrieved, nothing superseded — recall 1.0.
BASELINE_Q = _question(
    recalled=[_mem("m-old", GOLD_OLD), _mem("m-new", GOLD_NEW)],
    stored=[_mem("m-old", GOLD_OLD), _mem("m-new", GOLD_NEW)],
)

# Candidate run: the detector (correctly) retired the older gold; search
# excludes it; the NEWER gold — which supersedes it — is retrieved.
SUPERSEDED_RUN_Q = _question(
    recalled=[_mem("m-new", GOLD_NEW, supersedes="m-old"), _mem("m-x", "other://1")],
    stored=[
        _mem("m-old", GOLD_OLD, status="conflicted"),
        _mem("m-new", GOLD_NEW, supersedes="m-old"),
    ],
)


class TestScoreQuestion:
    def test_correctly_superseded_gold_credited_via_retrieved_successor(self):
        s = ctb.score_question(SUPERSEDED_RUN_Q, "supersession-aware")
        assert s.recall == 1.0
        outcomes = {g.uri: g.outcome for g in s.golds}
        assert outcomes[GOLD_OLD] == ctb.GOLD_SUPERSEDED_CREDITED
        assert outcomes[GOLD_NEW] == ctb.GOLD_RETRIEVED
        credited = next(g for g in s.golds if g.uri == GOLD_OLD)
        assert credited.superseding_id == "m-new"

    def test_legacy_mode_reproduces_the_old_counting(self):
        # Pre-fix semantics: the superseded gold is a plain miss → 1/2.
        s = ctb.score_question(SUPERSEDED_RUN_Q, "legacy")
        assert s.recall == 0.5
        assert {g.outcome for g in s.golds} == {ctb.GOLD_RETRIEVED, ctb.GOLD_MISSING}

    def test_genuinely_missing_gold_is_a_miss_in_both_modes(self):
        q = _question(
            recalled=[_mem("m-new", GOLD_NEW)],
            stored=[
                _mem("m-old", GOLD_OLD),
                _mem("m-new", GOLD_NEW),
            ],  # active, unretrieved
        )
        assert ctb.score_question(q, "supersession-aware").recall == 0.5
        assert ctb.score_question(q, "legacy").recall == 0.5

    def test_superseded_gold_without_retrieved_successor_stays_a_miss(self):
        # Over-firing shape: the gold was retired but nothing retrieved
        # carries its lineage — the damage must keep hurting recall.
        q = _question(
            recalled=[_mem("m-x", "other://1")],
            stored=[
                _mem("m-old", GOLD_OLD, status="conflicted"),
                _mem("m-new", GOLD_NEW, supersedes="m-old"),
            ],
            golds=(GOLD_OLD,),
        )
        s = ctb.score_question(q, "supersession-aware")
        assert s.recall == 0.0
        assert s.golds[0].outcome == ctb.GOLD_SUPERSEDED_LOST

    def test_chain_credit_is_transitive_and_cycle_safe(self):
        # C supersedes B supersedes A(gold); only C retrieved.
        q = _question(
            recalled=[_mem("m-c", "other://c", supersedes="m-b")],
            stored=[
                _mem("m-a", GOLD_OLD, status="outdated"),
                _mem("m-b", "other://b", status="conflicted", supersedes="m-a"),
                _mem("m-c", "other://c", supersedes="m-b"),
            ],
            golds=(GOLD_OLD,),
        )
        assert ctb.score_question(q, "supersession-aware").recall == 1.0
        # A corrupt cycle must terminate, not hang: A→B→A.
        q_cycle = _question(
            recalled=[_mem("m-b", "other://b", supersedes="m-a")],
            stored=[
                _mem("m-a", GOLD_OLD, status="outdated", supersedes="m-b"),
                _mem("m-b", "other://b", supersedes="m-a"),
            ],
            golds=(GOLD_OLD,),
        )
        assert ctb.score_question(q_cycle, "supersession-aware").recall == 1.0

    def test_active_copy_blocks_supersession_credit(self):
        # Re-ingestion left an ACTIVE copy of the gold uri unretrieved: that
        # is a plain retrieval miss, not a supersession to credit.
        q = _question(
            recalled=[_mem("m-new", GOLD_NEW, supersedes="m-old")],
            stored=[
                _mem("m-old", GOLD_OLD, status="conflicted"),
                _mem("m-old2", GOLD_OLD, status="active"),
                _mem("m-new", GOLD_NEW, supersedes="m-old"),
            ],
            golds=(GOLD_OLD,),
        )
        s = ctb.score_question(q, "supersession-aware")
        assert s.recall == 0.0
        assert s.golds[0].outcome == ctb.GOLD_MISSING

    def test_artifact_without_stored_statuses_degrades_to_legacy(self):
        q = dict(SUPERSEDED_RUN_Q, all_stored_memories=[])
        s = ctb.score_question(q, "supersession-aware")
        assert s.degraded is True
        assert s.recall == 0.5  # legacy counting

    def test_question_without_golds_scores_none(self):
        q = _question(recalled=[], stored=[], golds=())
        assert ctb.score_question(q, "supersession-aware").recall is None


class TestCompare:
    """End-to-end: the A63 false-FAIL reproduces in legacy mode and only there."""

    RUN = {"temporal-reasoning": [SUPERSEDED_RUN_Q]}
    BASE = {"temporal-reasoning": [BASELINE_Q]}

    def test_correct_supersession_no_longer_flags_a_regression(self):
        report = ctb.compare(self.RUN, self.BASE, "supersession-aware", 0.05, 0.02)
        assert report["breaches"] == []
        cat = report["categories"]["temporal-reasoning"]
        assert cat["run"]["recall"] == 1.0
        # The over-firing instrument still reports the supersession event.
        assert cat["run"]["gold_superseded"] == 1
        assert cat["run"]["gold_superseded_credited"] == 1
        assert cat["run"]["gold_superseded_lost"] == 0

    def test_legacy_mode_reproduces_the_a63_false_fail(self):
        report = ctb.compare(self.RUN, self.BASE, "legacy", 0.05, 0.02)
        assert report["breaches"] == ["temporal-reasoning"]
        assert "recall" in report["categories"]["temporal-reasoning"]["flags"]

    def test_genuine_regression_still_fails_in_aware_mode(self):
        run = {
            "temporal-reasoning": [
                _question(
                    recalled=[_mem("m-x", "other://1")],
                    stored=[_mem("m-old", GOLD_OLD), _mem("m-new", GOLD_NEW)],
                )
            ]
        }
        report = ctb.compare(run, self.BASE, "supersession-aware", 0.05, 0.02)
        assert report["breaches"] == ["temporal-reasoning"]

    def test_superseded_and_lost_still_fails_in_aware_mode(self):
        run = {
            "temporal-reasoning": [
                _question(
                    recalled=[_mem("m-x", "other://1")],
                    stored=[
                        _mem("m-old", GOLD_OLD, status="conflicted"),
                        _mem("m-new", GOLD_NEW, status="conflicted"),
                    ],
                )
            ]
        }
        report = ctb.compare(run, self.BASE, "supersession-aware", 0.05, 0.02)
        assert report["breaches"] == ["temporal-reasoning"]
        cat = report["categories"]["temporal-reasoning"]["run"]
        assert cat["gold_superseded_lost"] == 2

    def test_render_mentions_audit_note(self):
        report = ctb.compare(self.RUN, self.BASE, "supersession-aware", 0.05, 0.02)
        text = ctb.render(report, 0.05, 0.02)
        assert "does NOT certify the supersession was correct" in text
        assert "PASS" in text


class TestCli:
    """main() over real runner-shaped files: exit code 0 aware, 1 legacy."""

    @staticmethod
    def _write(tmp_path, name: str, questions: list[dict]) -> str:
        import json

        f = tmp_path / name
        f.write_text(
            json.dumps(
                {
                    "benchmark_metadata": {
                        "question_type": "temporal-reasoning",
                        "top_k": 20,
                    },
                    "results": questions,
                }
            )
        )
        return str(f)

    def test_exit_codes_across_modes(self, tmp_path, capsys):
        run = self._write(tmp_path, "run.json", [SUPERSEDED_RUN_Q])
        base = self._write(tmp_path, "base.json", [BASELINE_Q])
        assert ctb.main(["--run", run, "--baseline", base]) == 0
        assert (
            ctb.main(["--run", run, "--baseline", base, "--recall-mode", "legacy"]) == 1
        )
        out = capsys.readouterr().out
        assert "PASS" in out and "FAIL" in out
