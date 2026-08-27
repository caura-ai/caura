"""A58 — Path D basis-invalidation shadow: gating, guardrails, and purity.

The contract under test:
- fires only with a resolved subject_entity_id (no subject → no LLM spend);
- the bridge's menu is a HARD gate (off-menu predicates dropped — the
  ``is_valid_bucket_track`` analog) with a confidence floor;
- at most MAX_JUDGED candidates ever reach the judge;
- unknown judge decisions normalize to NO_OP;
- SHADOW PURITY: the only storage call is the read (find_rdf_conflicts);
  nothing is ever written, and no exception escapes the entry point.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from core_api.services.contradiction import basis_invalidation as bi

pytestmark = pytest.mark.unit


def _mem(
    subject="ent-1",
    predicate="commutes_by",
    content="tore ACL, no weight-bearing for six weeks",
):
    return {
        "id": "m-new",
        "subject_entity_id": subject,
        "predicate": predicate,
        "content": content,
    }


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    async def complete_json(self, prompt, **kw):
        return self.payload


def _direct_call_with_fallback(payload):
    """Replace call_with_fallback with 'just run call_fn against a fake llm'."""

    async def _cwf(
        *,
        primary_provider_name,
        call_fn,
        fake_fn,
        tenant_config,
        service_label,
        model_attr,
        timeout,
    ):
        return await call_fn(_FakeLLM(payload))

    return _cwf


async def test_no_subject_skips_without_llm(monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(bi, "call_with_fallback", called)
    out = await bi.run_basis_shadow(_mem(subject=None), "t", None)
    assert out["skipped_reason"] == "no_subject_entity_id"
    assert out["fired"] is False
    called.assert_not_awaited()


async def test_bridge_menu_is_a_hard_gate(monkeypatch):
    payload = {
        "affected": [
            {
                "predicate": "commute_vibes",
                "broken_basis": "x",
                "confidence": 0.9,
            },  # off-menu
            {
                "predicate": "current_role",
                "broken_basis": "low",
                "confidence": 0.2,
            },  # below floor
            {
                "predicate": "current_status",
                "broken_basis": "injury blocks it",
                "confidence": 0.8,
            },
        ]
    }
    monkeypatch.setattr(bi, "call_with_fallback", _direct_call_with_fallback(payload))
    sc = MagicMock()
    sc.find_rdf_conflicts = AsyncMock(return_value=[])
    monkeypatch.setattr(bi, "get_storage_client", lambda: sc)
    out = await bi.run_basis_shadow(_mem(), "t", None)
    assert [b["predicate"] for b in out["bridged"]] == ["current_status"]


async def test_judge_cap_and_shadow_purity(monkeypatch):
    bridge_payload = {
        "affected": [
            {"predicate": "current_status", "broken_basis": "b1", "confidence": 0.9},
            {"predicate": "current_role", "broken_basis": "b2", "confidence": 0.8},
        ]
    }
    judge_payload = {
        "decision": "INDIRECT_INVALIDATE",
        "reason": "basis broken",
        "confidence": 0.9,
    }
    calls = {"n": 0}

    async def _cwf(
        *,
        primary_provider_name,
        call_fn,
        fake_fn,
        tenant_config,
        service_label,
        model_attr,
        timeout,
    ):
        calls["n"] += 1
        payload = bridge_payload if service_label == "basis_bridge" else judge_payload
        return await call_fn(_FakeLLM(payload))

    monkeypatch.setattr(bi, "call_with_fallback", _cwf)
    sc = MagicMock()
    # three active candidates per predicate — more than MAX_JUDGED total
    sc.find_rdf_conflicts = AsyncMock(
        return_value=[
            {"id": f"old-{i}", "status": "active", "content": f"old fact {i}"}
            for i in range(3)
        ]
    )
    monkeypatch.setattr(bi, "get_storage_client", lambda: sc)

    out = await bi.run_basis_shadow(_mem(), "t", "f1")
    assert len(out["verdicts"]) == bi.MAX_JUDGED
    assert calls["n"] == 1 + bi.MAX_JUDGED  # 1 bridge + capped judges
    # purity: ONLY the read endpoint was touched on storage
    used = [
        name
        for name, m in vars(sc).items()
        if isinstance(m, AsyncMock) and m.await_count
    ]
    assert used == ["find_rdf_conflicts"]
    assert all(v["decision"] == "INDIRECT_INVALIDATE" for v in out["verdicts"])


async def test_retired_candidates_are_not_rejudged(monkeypatch):
    monkeypatch.setattr(
        bi,
        "call_with_fallback",
        _direct_call_with_fallback(
            {
                "affected": [
                    {
                        "predicate": "current_status",
                        "broken_basis": "b",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
    )
    sc = MagicMock()
    sc.find_rdf_conflicts = AsyncMock(
        return_value=[{"id": "old-1", "status": "outdated", "content": "x"}]
    )
    monkeypatch.setattr(bi, "get_storage_client", lambda: sc)
    out = await bi.run_basis_shadow(_mem(), "t", None)
    assert out["verdicts"] == []


async def test_unknown_decision_normalizes_to_no_op(monkeypatch):
    async def _cwf(
        *,
        primary_provider_name,
        call_fn,
        fake_fn,
        tenant_config,
        service_label,
        model_attr,
        timeout,
    ):
        payload = (
            {
                "affected": [
                    {
                        "predicate": "current_status",
                        "broken_basis": "b",
                        "confidence": 0.9,
                    }
                ]
            }
            if service_label == "basis_bridge"
            else {"decision": "OBLITERATE", "reason": "?", "confidence": 1.0}
        )
        return await call_fn(_FakeLLM(payload))

    monkeypatch.setattr(bi, "call_with_fallback", _cwf)
    sc = MagicMock()
    sc.find_rdf_conflicts = AsyncMock(
        return_value=[{"id": "old-1", "status": "active", "content": "x"}]
    )
    monkeypatch.setattr(bi, "get_storage_client", lambda: sc)
    out = await bi.run_basis_shadow(_mem(), "t", None)
    assert out["verdicts"][0]["decision"] == "NO_OP"


async def test_errors_never_escape(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(bi, "call_with_fallback", _boom)
    out = await bi.run_basis_shadow(_mem(), "t", None)
    assert out["skipped_reason"] == "error"
    assert out["verdicts"] == []


def test_detector_hook_respects_flag():
    """Default off + the hook is actually wired in the detector source."""
    from core_api.services import contradiction_detector as cd

    assert cd.settings.basis_invalidation_shadow is False  # default off
    from pathlib import Path

    src = Path(cd.__file__).read_text()
    assert "basis_invalidation_shadow" in src
    assert "run_basis_shadow" in src


async def test_overlap_fallback_when_rdf_route_is_empty(monkeypatch):
    """Predicates are sparse on real rows — when the rdf route returns
    nothing, the judge runs on the caller's entity-overlap candidates and the
    summary carries route=overlap."""

    async def _cwf(
        *,
        primary_provider_name,
        call_fn,
        fake_fn,
        tenant_config,
        service_label,
        model_attr,
        timeout,
    ):
        payload = (
            {
                "affected": [
                    {
                        "predicate": "current_status",
                        "broken_basis": "injury",
                        "confidence": 0.9,
                    }
                ]
            }
            if service_label == "basis_bridge"
            else {
                "decision": "SET_UNKNOWN_CURRENT",
                "reason": "basis broken, no replacement",
                "confidence": 0.8,
            }
        )
        return await call_fn(_FakeLLM(payload))

    monkeypatch.setattr(bi, "call_with_fallback", _cwf)
    sc = MagicMock()
    sc.find_rdf_conflicts = AsyncMock(return_value=[])  # rdf route empty
    monkeypatch.setattr(bi, "get_storage_client", lambda: sc)
    overlap = [
        {
            "id": "m-new",
            "status": "active",
            "content": "self",
        },  # the new row — must be excluded
        {"id": "old-bike", "status": "active", "content": "commutes by bicycle"},
        {"id": "old-dead", "status": "outdated", "content": "retired"},
    ]
    out = await bi.run_basis_shadow(_mem(), "t", None, overlap_candidates=overlap)
    assert out["route"] == "overlap"
    assert [v["target_memory_id"] for v in out["verdicts"]] == ["old-bike"]
    assert out["verdicts"][0]["decision"] == "SET_UNKNOWN_CURRENT"
