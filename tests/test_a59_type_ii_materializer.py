"""A59 — Type-II state materializer, shadow phase.

Contract under test:
- bundles are SUBJECT-LOCAL (the A58 spike's cross-subject targeting cannot
  recur by construction) and exclude non-live rows;
- candidate selection needs >=2 live memories and change since the watermark;
- deterministic gates reject unknown ids, unsupported claims, support that is
  not LATER than the stale row, empty replacements and low confidence;
- a NULL predicate does not disqualify a proposal (predicates are sparse — the
  exact reason A58's rdf route found nothing);
- proposals carry a stable idempotency key;
- SHADOW PURITY: no storage client is touched and no exception escapes.
"""

import pytest
from core_api.services import type_ii_materializer as tm

pytestmark = pytest.mark.unit


def _m(mid, subject, created, content="c", status="active", predicate=None):
    return {
        "id": mid,
        "subject_entity_id": subject,
        "created_at": created,
        "content": content,
        "status": status,
        "predicate": predicate,
        "scope": None,
    }


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    async def complete_json(self, prompt, **kw):
        return self.payload


def _cwf(payload, seen=None):
    async def _call(**kw):
        if seen is not None:
            seen.append(kw["service_label"])
        return await kw["call_fn"](_FakeLLM(payload))

    return _call


def test_bundles_are_subject_local_and_live_only():
    rows = [
        _m("a1", "S1", "2026-01-01"),
        _m("a2", "S1", "2026-02-01"),
        _m("b1", "S2", "2026-01-05"),
        _m("dead", "S1", "2026-03-01", status="outdated"),
        _m("nosub", None, "2026-01-01"),
    ]
    b = tm.group_subjects(rows)
    assert set(b) == {"S1", "S2"}
    assert [m["id"] for m in b["S1"]] == ["a1", "a2"]  # oldest first, no outdated


def test_candidates_need_two_memories_and_recent_change():
    b = {
        "S1": [_m("a", "S1", "2026-01-01"), _m("b", "S1", "2026-05-01")],
        "S2": [_m("c", "S2", "2026-01-01")],  # single fact
        "S3": [
            _m("d", "S3", "2026-01-01"),
            _m("e", "S3", "2026-01-02"),
        ],  # stale, unchanged
    }
    assert tm.select_candidates(b, since="2026-04-01") == ["S1"]
    assert set(tm.select_candidates(b, since=None)) == {"S1", "S3"}


def test_validation_rejects_bad_proposals():
    rows = [_m("old", "S1", "2026-01-01"), _m("new", "S1", "2026-06-01")]
    base = {
        "stale_memory_id": "old",
        "supporting_memory_ids": ["new"],
        "replacement_content": "currently unable to X",
        "confidence": 0.9,
    }
    assert tm.validate_proposal(base, "S1", rows)[0] is not None

    cases = {
        "stale_id_not_in_bundle": {**base, "stale_memory_id": "ghost"},
        "no_support_in_bundle": {**base, "supporting_memory_ids": ["ghost"]},
        # support must POSTDATE the stale row — otherwise an old fact can bury a fresh one
        "support_not_later_than_stale": {
            **base,
            "stale_memory_id": "new",
            "supporting_memory_ids": ["old"],
        },
        "empty_replacement": {**base, "replacement_content": "   "},
        "below_confidence_floor": {**base, "confidence": 0.1},
    }
    for expected, payload in cases.items():
        accepted, reason = tm.validate_proposal(payload, "S1", rows)
        assert accepted is None and reason == expected, expected


def test_null_predicate_does_not_disqualify():
    """Predicates are sparsely populated in production — a proposal must not be
    dropped for lacking one (this is what made A58's rdf route find nothing)."""
    rows = [
        _m("old", "S1", "2026-01-01", predicate=None),
        _m("new", "S1", "2026-06-01"),
    ]
    accepted, _ = tm.validate_proposal(
        {
            "stale_memory_id": "old",
            "supporting_memory_ids": ["new"],
            "replacement_content": "x",
            "confidence": 0.9,
        },
        "S1",
        rows,
    )
    assert accepted is not None and accepted["predicate"] is None


def test_proposal_key_is_stable_and_distinct():
    assert tm.proposal_key("S1", "m1") == tm.proposal_key("S1", "m1")
    assert tm.proposal_key("S1", "m1") != tm.proposal_key("S1", "m2")


async def test_shadow_run_emits_auditable_samples_and_writes_nothing(monkeypatch):
    rows = [
        _m("bike", "S1", "2026-01-01", content="commutes by bicycle daily"),
        _m("acl", "S1", "2026-06-01", content="tore ACL, no weight-bearing six weeks"),
    ]
    payload = {
        "stale": [
            {
                "stale_memory_id": "bike",
                "supporting_memory_ids": ["acl"],
                "replacement_content": "cannot currently commute by bicycle; mode unknown for six weeks",
                "broken_basis": "weight-bearing use of the leg",
                "confidence": 0.85,
            }
        ]
    }
    seen: list[str] = []
    monkeypatch.setattr(tm, "call_with_fallback", _cwf(payload, seen))
    out = await tm.run_shadow(rows, "t1")
    assert out["mode"] == "shadow"
    assert out["subjects_called"] == 1 and out["accepted"] == 1 and out["rejected"] == 0
    s = out["samples"][0]
    # the sample must be scorable by hand: stale text, support text, replacement
    assert s["stale_content"].startswith("commutes by bicycle")
    assert s["supporting_content"][0].startswith("tore ACL")
    assert "cannot currently commute" in s["replacement_content"]
    assert s["key"] == tm.proposal_key("S1", "bike")
    assert seen == ["type_ii_materializer"]


async def test_shadow_run_skips_single_memory_subjects(monkeypatch):
    called = []
    monkeypatch.setattr(tm, "call_with_fallback", _cwf({"stale": []}, called))
    out = await tm.run_shadow([_m("only", "S1", "2026-01-01")], "t1")
    assert out["subjects_scanned"] == 1 and out["subjects_called"] == 0
    assert called == []  # no LLM spend on a subject with nothing to invalidate


async def test_shadow_run_survives_llm_failure(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(tm, "call_with_fallback", _boom)
    rows = [_m("a", "S1", "2026-01-01"), _m("b", "S1", "2026-06-01")]
    out = await tm.run_shadow(rows, "t1")
    assert out["accepted"] == 0 and out["subjects_called"] == 0
    assert "error" not in out  # per-subject failure is contained, run still reports


def test_flag_defaults_off_and_phase_is_wired():
    from pathlib import Path

    from core_api.config import settings
    from core_api.services import crystallizer_service as cs

    assert settings.type_ii_materializer_shadow is False
    src = Path(cs.__file__).read_text()
    assert "type_ii_materializer_shadow" in src
    # nested under hygiene on purpose: analysis_reports has fixed columns, so
    # a new top-level report key never reaches storage or the API
    assert 'hygiene["type_ii_staleness"] = type_ii' in src


def test_transcript_turns_are_excluded_from_bundles():
    """Raw dialogue turns are not state claims. Measured: 9 of 13 accepted
    proposals in the first real sweep came from consecutive turns, and
    memory_type does NOT separate them (false positives were typed
    fact/preference exactly like the true ones) — the content shape does."""
    rows = [
        _m(
            "t1",
            "S1",
            "2026-01-01",
            content="User: I'm looking for book recommendations",
        ),
        _m(
            "t2",
            "S1",
            "2026-02-01",
            content="Assistant: Here are some historical fiction picks",
        ),
        _m("real", "S1", "2026-03-01", content="Robin commutes by bicycle every day"),
    ]
    b = tm.group_subjects(rows)
    assert [m["id"] for m in b["S1"]] == ["real"]


def test_restatement_is_rejected():
    """The dangerous failure: proposing the stale claim as its own successor
    would enshrine the error with a fresh timestamp."""
    rows = [
        _m(
            "old",
            "S1",
            "2026-01-01",
            content="Robin has been based in Seattle for the last few years",
        ),
        _m(
            "new",
            "S1",
            "2026-06-01",
            content="Robin switched their license and voter registration after the move",
        ),
    ]
    accepted, reason = tm.validate_proposal(
        {
            "stale_memory_id": "old",
            "supporting_memory_ids": ["new"],
            # restates the stale claim instead of retiring it
            "replacement_content": "Robin has been based in Seattle for the last few years, and completed the paperwork",
            "confidence": 0.9,
        },
        "S1",
        rows,
    )
    assert accepted is None and reason == "replacement_restates_stale"


def test_genuine_replacement_still_passes():
    rows = [
        _m(
            "old",
            "S1",
            "2026-01-01",
            content="Robin sees friends twice a week and keeps those nights open",
        ),
        _m("new", "S1", "2026-06-01", content="Robin started a night-shift rotation"),
    ]
    accepted, _ = tm.validate_proposal(
        {
            "stale_memory_id": "old",
            "supporting_memory_ids": ["new"],
            "replacement_content": "Robin is no longer free two evenings a week; the night-shift rotation leaves only one",
            "confidence": 0.8,
        },
        "S1",
        rows,
    )
    assert accepted is not None
