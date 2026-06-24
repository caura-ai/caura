"""Regression tests for audit S6 / C1 / M15 / M16.

- S6: ``ExecuteScoredSearch`` forwarded ``readable_tenant_ids`` only when
  ``len(readable) > 1``, while the entity-lookup short-circuit uses
  ``readable != [tenant_id]`` — a cross-tenant credential whose readable
  set is exactly ``["other-tenant"]`` was silently narrowed to home-tenant
  reads on the scored-search path only.
- C1: Gemini / Vertex / Fake ``complete_json`` rejected the OpenAI
  structured-output kwargs (``seed`` / ``response_schema``) with
  ``TypeError`` — entity extraction silently degraded to the regex
  fallback for every non-OpenAI deployment.
- M15: lifecycle payloads used ``extra="forbid"`` — an additive publisher
  field silently dropped archive/purge/forge requests during a rolling
  deploy.
- M16: ``payload_cls(**event.payload)`` raised ``TypeError`` (escaping
  the ``except ValidationError``) on non-dict payloads → nack /
  redeliver-forever.
"""

from __future__ import annotations

import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# S6 — scored-search cross-tenant widening
# ---------------------------------------------------------------------------


def _search_ctx(tenant_id: str, readable: list[str] | None):
    from core_api.pipeline.context import PipelineContext

    data = {
        "tenant_id": tenant_id,
        "query": "q",
        "embedding": [0.0] * 8,
        "search_params": {"top_k": 5},
        "temporal_window": None,
        "boosted_memory_ids": [],
        "memory_boost_factor": {},
        "readable_tenant_ids": readable,
        # Skip the per-tenant slot context manager (already-acquired path).
        "_storage_slot_acquired": True,
    }
    return PipelineContext(data=data)


async def _run_scored_search(monkeypatch, tenant_id: str, readable: list[str] | None) -> dict:
    from core_api.pipeline.steps.search import execute_scored_search as mod

    sc = MagicMock()
    sc.scored_search = AsyncMock(return_value=[])
    monkeypatch.setattr(mod, "get_storage_client", lambda: sc)

    await mod.ExecuteScoredSearch().execute(_search_ctx(tenant_id, readable))
    return sc.scored_search.await_args.args[0]


async def test_single_foreign_tenant_readable_set_is_forwarded(monkeypatch):
    """readable == ["other-tenant"] (home not included) must widen —
    this is the exact shape ``len > 1`` silently narrowed."""
    sent = await _run_scored_search(monkeypatch, "home", ["other-tenant"])
    assert sent["readable_tenant_ids"] == ["other-tenant"]


async def test_multi_tenant_readable_set_still_forwarded(monkeypatch):
    sent = await _run_scored_search(monkeypatch, "home", ["home", "other"])
    assert sent["readable_tenant_ids"] == ["home", "other"]


async def test_home_only_readable_set_stays_single_tenant(monkeypatch):
    sent = await _run_scored_search(monkeypatch, "home", ["home"])
    assert "readable_tenant_ids" not in sent


async def test_absent_readable_set_stays_single_tenant(monkeypatch):
    sent = await _run_scored_search(monkeypatch, "home", None)
    assert "readable_tenant_ids" not in sent


# ---------------------------------------------------------------------------
# C1 — non-OpenAI providers accept the structured-output kwargs
# ---------------------------------------------------------------------------


def test_all_providers_accept_structured_output_kwargs():
    """Every provider's complete_json must accept seed / response_schema —
    a TypeError here is swallowed by call_with_fallback's retry loop and
    silently lands on the fake/regex fallback."""
    from common.llm.providers.fake import FakeLLMProvider
    from common.llm.providers.gemini import GeminiLLMProvider
    from common.llm.providers.openai import OpenAILLMProvider
    from common.llm.providers.vertex import VertexLLMProvider

    for provider_cls in (FakeLLMProvider, GeminiLLMProvider, OpenAILLMProvider, VertexLLMProvider):
        params = inspect.signature(provider_cls.complete_json).parameters
        assert "seed" in params, f"{provider_cls.__name__}.complete_json lacks seed"
        assert "response_schema" in params, (
            f"{provider_cls.__name__}.complete_json lacks response_schema"
        )


async def test_fake_provider_ignores_structured_output_kwargs():
    from common.llm.providers.fake import FakeLLMProvider

    out = await FakeLLMProvider().complete_json(
        "p", seed=123, response_schema={"type": "object"}
    )
    assert out == {}


# ---------------------------------------------------------------------------
# M15 — lifecycle payloads tolerate additive fields (rolling deploy)
# ---------------------------------------------------------------------------


def test_lifecycle_payload_ignores_unknown_fields():
    from common.events.lifecycle_archive_request import LifecycleArchiveRequest
    from common.events.lifecycle_purge_request import LifecyclePurgeRequest

    req = LifecycleArchiveRequest.model_validate(
        {
            "audit_id": 1,
            "org_id": "t",
            "triggered_by": "test",
            "added_by_newer_publisher": "x",
        }
    )
    assert req.audit_id == 1

    purge = LifecyclePurgeRequest.model_validate(
        {
            "audit_id": 2,
            "org_id": "t",
            "triggered_by": "test",
            "retention_days": 7,
            "future_field": True,
        }
    )
    assert purge.retention_days == 7


# ---------------------------------------------------------------------------
# M16 — non-dict payload is dropped, not redelivered forever
# ---------------------------------------------------------------------------


async def test_non_dict_lifecycle_payload_is_dropped():
    from common.events.base import Event
    from common.events.lifecycle_archive_request import LifecycleArchiveRequest
    from common.events.lifecycle_handlers import _run_action

    adapter = MagicMock()
    op = AsyncMock()
    # ``model_construct`` bypasses Event's own payload: dict validation to
    # simulate a malformed wire payload reaching the handler.
    event = Event.model_construct(
        event_id=uuid.uuid4(),
        event_type="memclaw.lifecycle.archive-expired-requested",
        payload=["not", "a", "dict"],
    )
    # Must return cleanly (drop) — the kwargs-splat raised TypeError which
    # escaped the handler and nacked the delivery.
    await _run_action(
        event,
        adapter=adapter,
        payload_cls=LifecycleArchiveRequest,
        run_op=op,
        stats_key="archived",
        action="archive-expired",
    )
    op.assert_not_awaited()
