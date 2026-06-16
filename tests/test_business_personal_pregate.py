"""Tests for the fast business/personal pre-gate (BusinessPersonalPregate).

Layers, all deterministic (no HTTP/settings round-trip):
  * Step-level: enablement gating, drop/flow-through, min_confidence, and that
    the pre-gate uses its OWN provider independent of the enrichment provider
    (the green-local/red-CI lesson — the signal must survive
    ``enrichment_provider=none``). The classifier is monkeypatched at the step
    level so the decision logic is exercised without a live model.
  * Classifier: the ``provider=none`` fail-open path, exercised directly.
  * Settings: default-off + validation of the new pregate keys.
  * Pipeline wiring: the step is composed into strong + fast at the right spot.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.write import business_personal_pregate as pregate_mod
from core_api.pipeline.steps.write.business_personal_pregate import (
    BusinessPersonalPregate,
)
from core_api.services.business_classifier import (
    BusinessClassification,
    classify_business_personal,
)
from core_api.services.organization_settings import (
    ResolvedConfig,
    _validate_governance_enums,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def emitted(monkeypatch):
    """Capture governance audit emissions in the step module."""
    calls: list[dict] = []

    async def _record(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(f"{pregate_mod.__name__}.emit_governance_audit", _record)
    return calls


def _data(content="some content", **kw):
    return SimpleNamespace(
        content=content,
        metadata=kw.get("metadata"),
        tenant_id="t1",
        agent_id="a1",
        fleet_id=None,
        persist=True,
        visibility=kw.get("visibility"),
    )


def _cfg(*, nb=None, enrichment_provider=None) -> ResolvedConfig:
    ts: dict = {}
    if nb is not None:
        ts["governance"] = {"non_business": nb}
    if enrichment_provider is not None:
        ts["enrichment"] = {"provider": enrichment_provider}
    return ResolvedConfig(ts)


def _ctx(data, *, cfg, mode="strong") -> PipelineContext:
    return PipelineContext(
        db=None,
        data={"input": data, "resolved_write_mode": mode},
        tenant_config=cfg,
    )


def _patch_classifier(monkeypatch, *, relevance="personal", confidence=0.95):
    """Replace the step's classifier with a canned result; capture call kwargs."""
    captured: dict = {}

    async def _fake(content, tenant_config=None, *, provider, model):
        captured.update(content=content, provider=provider, model=model)
        return BusinessClassification(
            business_relevance=relevance, confidence=confidence, llm_ms=3
        )

    monkeypatch.setattr(pregate_mod, "classify_business_personal", _fake)
    return captured


# ── enablement gating ────────────────────────────────────────────────


async def test_pregate_skips_when_pregate_disabled():
    cfg = _cfg(
        nb={"enabled": True, "disposition": "drop", "pregate": {"enabled": False}}
    )
    res = await BusinessPersonalPregate().execute(_ctx(_data(), cfg=cfg))
    assert res.outcome == StepOutcome.SKIPPED


async def test_pregate_skips_when_non_business_disabled():
    cfg = _cfg(
        nb={"enabled": False, "disposition": "drop", "pregate": {"enabled": True}}
    )
    res = await BusinessPersonalPregate().execute(_ctx(_data(), cfg=cfg))
    assert res.outcome == StepOutcome.SKIPPED


async def test_pregate_skips_when_disposition_not_drop():
    # keep_private/store persist the row → left to the post-enrichment decision.
    cfg = _cfg(
        nb={
            "enabled": True,
            "disposition": "keep_private",
            "pregate": {"enabled": True},
        }
    )
    res = await BusinessPersonalPregate().execute(_ctx(_data(), cfg=cfg))
    assert res.outcome == StepOutcome.SKIPPED


# ── decision logic ───────────────────────────────────────────────────


async def test_pregate_personal_drop_raises_422(monkeypatch, emitted):
    _patch_classifier(monkeypatch, relevance="personal", confidence=0.9)
    cfg = _cfg(
        nb={
            "enabled": True,
            "disposition": "drop",
            "pregate": {"enabled": True, "provider": "openai"},
        }
    )
    with pytest.raises(HTTPException) as exc:
        await BusinessPersonalPregate().execute(_ctx(_data("my vacation"), cfg=cfg))
    assert exc.value.status_code == 422
    drop = next(c for c in emitted if c["action"] == "nonbusiness_pregate_drop")
    assert drop["detail"]["source"] == "llm_pregate"
    assert drop["detail"]["confidence"] == 0.9


async def test_pregate_business_flows_through(monkeypatch, emitted):
    _patch_classifier(monkeypatch, relevance="business", confidence=0.9)
    cfg = _cfg(
        nb={
            "enabled": True,
            "disposition": "drop",
            "pregate": {"enabled": True, "provider": "openai"},
        }
    )
    res = await BusinessPersonalPregate().execute(_ctx(_data("q3 revenue"), cfg=cfg))
    assert res is None
    assert emitted == []


async def test_pregate_respects_min_confidence(monkeypatch, emitted):
    # personal but below the configured confidence floor → no drop (fail-open).
    _patch_classifier(monkeypatch, relevance="personal", confidence=0.4)
    cfg = _cfg(
        nb={
            "enabled": True,
            "disposition": "drop",
            "pregate": {"enabled": True, "provider": "openai", "min_confidence": 0.8},
        }
    )
    res = await BusinessPersonalPregate().execute(
        _ctx(_data("maybe personal"), cfg=cfg)
    )
    assert res is None
    assert emitted == []


# ── provider independence + fail-open (the green-local/red-CI lesson) ──


async def test_pregate_uses_own_provider_independent_of_enrichment(monkeypatch):
    captured = _patch_classifier(monkeypatch, relevance="business")
    # Enrichment provider is none (e.g. CI), but the pre-gate has its own.
    cfg = _cfg(
        nb={
            "enabled": True,
            "disposition": "drop",
            "pregate": {"enabled": True, "provider": "openai", "model": "gpt-5.4-nano"},
        },
        enrichment_provider="none",
    )
    await BusinessPersonalPregate().execute(_ctx(_data(), cfg=cfg))
    assert captured["provider"] == "openai"  # NOT "none"
    assert captured["model"] == "gpt-5.4-nano"


async def test_pregate_falls_back_to_enrichment_provider_when_unset(monkeypatch):
    captured = _patch_classifier(monkeypatch, relevance="business")
    cfg = _cfg(
        nb={"enabled": True, "disposition": "drop", "pregate": {"enabled": True}},
        enrichment_provider="gemini",
    )
    await BusinessPersonalPregate().execute(_ctx(_data(), cfg=cfg))
    assert captured["provider"] == "gemini"


async def test_classifier_none_provider_fail_open():
    # No live model (provider=none): never block — return a "business" verdict.
    res = await classify_business_personal(
        "anything", None, provider="none", model=None
    )
    assert res.business_relevance == "business"
    assert res.llm_ms == 0


async def test_classifier_prompt_preserves_braces():
    # str.format inserts the substituted value literally; the content must NOT be
    # brace-escaped, or JSON/code/braces in it get corrupted before the LLM sees
    # them (e.g. {"a": 1} → {{"a": 1}}).
    from core_api.services.business_classifier import _build_prompt

    content = 'config = {"a": 1}; for {i} in range(3): pass'
    prompt = _build_prompt(content)
    assert content in prompt
    assert "{{" not in prompt.split("Content:")[-1]


# ── settings: default-off + validation ───────────────────────────────


async def test_pregate_default_off():
    pg = ResolvedConfig({}).governance_non_business_pregate
    assert pg.enabled is False
    assert pg.provider is None and pg.model is None and pg.min_confidence is None


async def test_pregate_settings_resolve():
    pg = ResolvedConfig(
        {
            "governance": {
                "non_business": {
                    "pregate": {
                        "enabled": True,
                        "provider": "openai",
                        "model": "gpt-5.4-nano",
                        "min_confidence": 0.7,
                    }
                }
            }
        }
    ).governance_non_business_pregate
    assert (
        pg.enabled
        and pg.provider == "openai"
        and pg.model == "gpt-5.4-nano"
        and pg.min_confidence == 0.7
    )


async def test_pregate_validation_rejects_bad_provider():
    with pytest.raises(ValueError):
        _validate_governance_enums(
            {"governance": {"non_business": {"pregate": {"provider": "bogus"}}}}
        )


async def test_pregate_validation_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        _validate_governance_enums(
            {"governance": {"non_business": {"pregate": {"min_confidence": 1.5}}}}
        )


async def test_pregate_validation_accepts_valid():
    _validate_governance_enums(
        {
            "governance": {
                "non_business": {
                    "pregate": {"provider": "gemini", "min_confidence": 0.5}
                }
            }
        }
    )


# ── pipeline wiring ──────────────────────────────────────────────────


def _names(pipeline) -> list[str]:
    return [s.name for s in pipeline._steps]


async def test_pregate_wired_into_strong_and_fast_before_enrich():
    from core_api.pipeline.compositions.write import (
        build_fast_write_pipeline,
        build_strong_write_pipeline,
    )

    for build in (build_strong_write_pipeline, build_fast_write_pipeline):
        names = _names(build())
        assert "business_personal_pregate" in names, build.__name__
        # After the deterministic scan, before the expensive enrichment.
        assert (
            names.index("governance_scan_content")
            < names.index("business_personal_pregate")
            < names.index("parallel_embed_enrich")
        ), build.__name__


async def test_pregate_not_in_stm_or_enrichment_only():
    from core_api.pipeline.compositions.write import (
        build_enrichment_pipeline,
        build_stm_write_pipeline,
    )

    assert "business_personal_pregate" not in _names(build_stm_write_pipeline())
    assert "business_personal_pregate" not in _names(build_enrichment_pipeline())
