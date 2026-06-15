"""Tests for the ingestion-boundary governance gate (eToro).

Two layers, both deterministic (no HTTP/settings round-trip):
  * Step-level tests for ``GovernanceScanContent`` (deterministic) and
    ``GovernanceDecision`` (LLM-signal) — exercise mask/drop/flag/keep-private/
    business/fail-closed on a real ResolvedConfig, with audit-emit capture.
  * Pipeline-wiring tests — assert the steps are actually composed into the
    fast/strong/enrichment/STM write pipelines at the right positions, so a
    real write runs them. (A full HTTP E2E was dropped: it depended on
    settings-cache propagation across the test/app process boundary, which is
    flaky under the full-suite harness; the behaviour is covered here + by the
    pattern/config tests, and validated end-to-end on staging.)
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.write.governance_decision import GovernanceDecision
from core_api.pipeline.steps.write.governance_scan_content import GovernanceScanContent
from core_api.services.organization_settings import ResolvedConfig

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def emitted(monkeypatch):
    """Capture governance audit emissions in the step modules so unit tests can
    assert the right action was recorded — without needing the async audit
    queue / storage. Returns the list of captured kwargs."""
    calls: list[dict] = []

    async def _record(*args, **kwargs):
        calls.append(kwargs)

    for mod in (
        "core_api.pipeline.steps.write.governance_scan_content",
        "core_api.pipeline.steps.write.governance_decision",
    ):
        monkeypatch.setattr(f"{mod}.emit_governance_audit", _record)
    return calls


def _data(content: str, **kw):
    return SimpleNamespace(
        content=content,
        metadata=kw.get("metadata"),
        tenant_id="t1",
        agent_id="a1",
        fleet_id=None,
        persist=True,
        visibility=kw.get("visibility"),
    )


def _cfg(*, pii: dict | None = None, nb: dict | None = None) -> ResolvedConfig:
    gov: dict = {}
    if pii is not None:
        gov["pii"] = pii
    if nb is not None:
        gov["non_business"] = nb
    return ResolvedConfig({"governance": gov})


def _ctx(data, *, cfg, enrichment=None, mode="strong") -> PipelineContext:
    d = {
        "input": data,
        "resolved_write_mode": mode,
        "memory_fields": {"metadata": data.metadata or {}},
    }
    if enrichment is not None:
        d["enrichment"] = enrichment
    return PipelineContext(db=None, data=d, tenant_config=cfg)


def _enr(
    *, contains_pii=False, pii_types=None, business_relevance="business", llm_ms=5
):
    return SimpleNamespace(
        contains_pii=contains_pii,
        pii_types=pii_types or [],
        business_relevance=business_relevance,
        llm_ms=llm_ms,
    )


# ── GovernanceScanContent (deterministic) ────────────────────────────


async def test_scan_skips_when_disabled():
    data = _data("card 4111 1111 1111 1111")
    res = await GovernanceScanContent().execute(_ctx(data, cfg=_cfg()))
    assert res.outcome == StepOutcome.SKIPPED
    assert "4111 1111 1111 1111" in data.content  # untouched


async def test_scan_masks_content_in_place(emitted):
    data = _data("reach me@example.com any time")
    await GovernanceScanContent().execute(
        _ctx(data, cfg=_cfg(pii={"enabled": True, "action": "mask"}))
    )
    assert "me@example.com" not in data.content
    assert "«EMAIL»" in data.content
    assert any(c["action"] == "pii_mask" for c in emitted)  # audit emitted


async def test_scan_drop_raises_422(emitted):
    data = _data("card 4111 1111 1111 1111")
    with pytest.raises(HTTPException) as exc:
        await GovernanceScanContent().execute(
            _ctx(data, cfg=_cfg(pii={"enabled": True, "action": "drop"}))
        )
    assert exc.value.status_code == 422
    assert any(c["action"] == "pii_drop" for c in emitted)  # audited before reject


async def test_scan_flag_sets_metadata_without_masking(emitted):
    data = _data("reach me@example.com")
    await GovernanceScanContent().execute(
        _ctx(data, cfg=_cfg(pii={"enabled": True, "action": "flag"}))
    )
    assert data.metadata["contains_pii"] is True
    assert "email" in data.metadata["pii_types"]
    assert "me@example.com" in data.content  # flag does not redact
    assert any(c["action"] == "pii_flag" for c in emitted)


async def test_scan_category_toggle_limits_scope():
    data = _data("email me@example.com card 4111 1111 1111 1111")
    cfg = _cfg(pii={"enabled": True, "action": "mask", "categories": {"email": True}})
    await GovernanceScanContent().execute(_ctx(data, cfg=cfg))
    assert "«EMAIL»" in data.content
    assert "4111 1111 1111 1111" in data.content  # credit_card not in scope


# ── GovernanceDecision (LLM signal) ──────────────────────────────────


async def test_decision_skips_when_disabled():
    res = await GovernanceDecision().execute(
        _ctx(_data("hi"), cfg=_cfg(), enrichment=_enr())
    )
    assert res.outcome == StepOutcome.SKIPPED


async def test_decision_personal_keep_private(emitted):
    data = _data("planning my vacation")
    cfg = _cfg(nb={"enabled": True, "disposition": "keep_private"})
    await GovernanceDecision().execute(
        _ctx(data, cfg=cfg, enrichment=_enr(business_relevance="personal"))
    )
    assert data.visibility == "scope_agent"
    assert any(c["action"] == "nonbusiness_keep_private" for c in emitted)


async def test_decision_personal_drop_raises_422(emitted):
    data = _data("vacation plans")
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    with pytest.raises(HTTPException) as exc:
        await GovernanceDecision().execute(
            _ctx(data, cfg=cfg, enrichment=_enr(business_relevance="personal"))
        )
    assert exc.value.status_code == 422
    assert any(c["action"] == "nonbusiness_drop" for c in emitted)


async def test_decision_business_flows_through():
    data = _data("quarterly revenue report")
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    await GovernanceDecision().execute(
        _ctx(data, cfg=cfg, enrichment=_enr(business_relevance="business"))
    )
    assert data.visibility is None  # business content stored normally


async def test_decision_pii_drop_on_llm_signal(emitted):
    data = _data("free-form health detail the patterns miss")
    cfg = _cfg(pii={"enabled": True, "action": "drop"})
    with pytest.raises(HTTPException) as exc:
        await GovernanceDecision().execute(
            _ctx(
                data, cfg=cfg, enrichment=_enr(contains_pii=True, pii_types=["health"])
            )
        )
    assert exc.value.status_code == 422
    assert any(c["action"] == "pii_drop" for c in emitted)


async def test_decision_pii_mask_config_flags_and_records_intent(emitted):
    # mask-configured, strong mode: the LLM signal has no span offsets, so the
    # row is flagged (a free-form match can't be redacted). The audit keeps the
    # truthful pii_flag verb but records the configured intent so it stays
    # distinguishable from a genuine flag policy — without claiming a redaction.
    data = _data("free-form health detail the patterns miss")
    cfg = _cfg(pii={"enabled": True, "action": "mask"})
    await GovernanceDecision().execute(
        _ctx(data, cfg=cfg, enrichment=_enr(contains_pii=True, pii_types=["health"]))
    )
    flag = next(c for c in emitted if c["action"] == "pii_flag")
    assert flag["detail"]["configured_action"] == "pii_mask"


async def test_decision_pii_flag_config_records_no_configured_action(emitted):
    data = _data("free-form health detail the patterns miss")
    cfg = _cfg(pii={"enabled": True, "action": "flag"})
    await GovernanceDecision().execute(
        _ctx(data, cfg=cfg, enrichment=_enr(contains_pii=True, pii_types=["health"]))
    )
    flag = next(c for c in emitted if c["action"] == "pii_flag")
    assert "configured_action" not in flag["detail"]


async def test_decision_writes_metadata_back_when_fields_absent(emitted):
    # Fallback path: memory_fields carries no metadata AND data.metadata is None.
    # The step must attach its working dict to data.metadata, else the flags it
    # writes are lost (mirrors GovernanceScanContent's write-back).
    data = _data("free-form health detail the patterns miss")
    cfg = _cfg(pii={"enabled": True, "action": "flag"})
    ctx = _ctx(data, cfg=cfg, enrichment=_enr(contains_pii=True, pii_types=["health"]))
    ctx.data["memory_fields"] = {}  # no "metadata" key → fallback branch
    assert data.metadata is None
    await GovernanceDecision().execute(ctx)
    assert data.metadata is not None
    assert data.metadata.get("contains_pii") is True


async def test_decision_fail_closed_no_destructive_action_when_uncertain():
    # llm_ms=0 → heuristic fallback (LLM unavailable). Even with a configured
    # destructive disposition, no drop/keep-private on the absent signal — the
    # deterministic step is the high-risk backstop. Uncertainty is recorded.
    data = _data("vacation plans")
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    meta: dict = {}
    ctx = _ctx(data, cfg=cfg, enrichment=_enr(business_relevance="personal", llm_ms=0))
    ctx.data["memory_fields"] = {"metadata": meta}
    await GovernanceDecision().execute(ctx)
    assert meta.get("governance_llm_uncertain") is True
    assert data.visibility is None  # not dropped, not kept-private


# ── Pipeline wiring (deterministic) ──────────────────────────────────
#
# The step-level tests above prove the gate's decision logic on a real
# ResolvedConfig; these assert the steps are actually WIRED into the write
# compositions (so a real write runs them) and at the right positions. This is
# the deterministic stand-in for a full HTTP E2E — the latter proved flaky in
# CI because it depends on settings-cache propagation across the test/app
# process boundary (the gate behaviour itself is fully covered above).


def _step_names(pipeline) -> list[str]:
    return [s.name for s in pipeline._steps]


async def test_governance_scan_wired_into_all_ltm_compositions():
    from core_api.pipeline.compositions.write import (
        build_enrichment_pipeline,
        build_fast_write_pipeline,
        build_strong_write_pipeline,
    )

    for build in (
        build_fast_write_pipeline,
        build_strong_write_pipeline,
        build_enrichment_pipeline,
    ):
        names = _step_names(build())
        assert "governance_scan_content" in names, build.__name__
        # Deterministic mask requires the scan to run BEFORE the content hash.
        assert names.index("governance_scan_content") < names.index(
            "compute_content_hash"
        ), build.__name__


async def test_governance_scan_wired_into_stm():
    from core_api.pipeline.compositions.write import build_stm_write_pipeline

    assert "governance_scan_content" in _step_names(build_stm_write_pipeline())


async def test_governance_decision_wired_into_strong_only():
    from core_api.pipeline.compositions.write import (
        build_fast_write_pipeline,
        build_strong_write_pipeline,
    )

    strong = _step_names(build_strong_write_pipeline())
    assert "governance_decision" in strong
    # Must run after enrichment is merged, before the row is written.
    assert strong.index("merge_enrichment_fields") < strong.index("governance_decision")
    assert strong.index("governance_decision") < strong.index("write_memory_row")
    # Fast mode defers enrichment, so the LLM-signal step is NOT in it
    # (the fast path applies the signal via post-write remediation instead).
    assert "governance_decision" not in _step_names(build_fast_write_pipeline())
