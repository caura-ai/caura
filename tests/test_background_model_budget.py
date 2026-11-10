"""Background calls reserve tenant credits before every provider attempt.

The reservation boundary is stubbed; the real retry/fallback code runs. Storage
atomicity is covered separately in the storage service's integration suite.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.llm import retry
from common.usage import (
    UsageScope,
    current_scope,
    get_usage_sink,
    set_usage_sink,
    usage_scope,
)
from common.usage import scope as usage_context

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@dataclass(frozen=True)
class _BackgroundScope(UsageScope):
    """Explicit background attribution, leaving ordinary scopes foreground."""

    background: bool = True


@contextmanager
def _background(tenant="tenant-a", agent="agent-a"):
    token = usage_context._scope.set(
        _BackgroundScope(
            tenant_id=tenant, agent_id=agent, operation="entity-extraction"
        )
    )
    try:
        yield
    finally:
        usage_context._scope.reset(token)


@pytest.fixture
def reserve(monkeypatch):
    """One shared authority for API and worker callers, not a usage sink.

    create-on-old-source makes the red result a provider-call assertion rather
    than an import/collection error. The fix wires this boundary into retry.
    """
    fn = AsyncMock(return_value={"allowed": False, "retry_after_seconds": 60})
    monkeypatch.setattr(retry, "reserve_background_attempt", fn, raising=False)
    return fn


async def _outcome(fn):
    try:
        return await fn()
    except Exception as exc:
        return exc


@pytest.mark.parametrize("outcome", ["timeout", "error", "malformed", "cancel"])
async def test_unknown_admission_releases_the_attempt_before_deferring(
    monkeypatch, outcome
):
    entered = asyncio.Event()

    async def reserve(**_kw):
        entered.set()
        if outcome == "cancel":
            await asyncio.Event().wait()
        if outcome == "malformed":
            return {"allowed": "unknown"}
        raise (
            TimeoutError() if outcome == "timeout" else RuntimeError("body to withhold")
        )

    release = AsyncMock(return_value={"released": True})
    provider = AsyncMock()
    monkeypatch.setattr(
        retry, "reserve_background_attempt", AsyncMock(side_effect=reserve)
    )
    monkeypatch.setattr(retry, "release_background_attempt", release)
    with _background():
        task = asyncio.create_task(
            retry.call_with_retry(provider, "test", max_attempts=1)
        )
        await entered.wait()
        if outcome == "cancel":
            task.cancel()
        [result] = await asyncio.gather(task, return_exceptions=True)
    if outcome == "cancel":
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert type(result).__name__ == "BackgroundWorkDeferred"
    provider.assert_not_awaited()
    release.assert_awaited_once()
    assert (
        release.call_args.kwargs["attempt_id"]
        == retry.reserve_background_attempt.call_args.kwargs["attempt_id"]
    )
    assert release.call_args.kwargs["tenant_id"] == "tenant-a"


@pytest.mark.parametrize(
    "operation", ["entity-extraction", "contradiction", "enrichment", "interview"]
)
async def test_an_exhausted_background_budget_never_calls_the_provider(
    reserve, operation, caplog
):
    provider = AsyncMock(return_value="answer")
    with caplog.at_level(logging.INFO), _background():
        with usage_scope(operation=operation):
            outcome = await _outcome(
                lambda: retry.call_with_retry(provider, operation, max_attempts=1)
            )

    assert provider.await_count == 0, "an exhausted tenant still spent a provider call"
    assert reserve.await_count == 1
    assert type(outcome).__name__ == "BackgroundWorkDeferred"
    assert any(getattr(r, "reason", None) == "budget_exhausted" for r in caplog.records)


async def test_each_retry_needs_another_credit(reserve):
    reserve.side_effect = [
        {"allowed": True, "retry_after_seconds": 0},
        {"allowed": False, "retry_after_seconds": 60},
    ]
    provider = AsyncMock(
        side_effect=[RuntimeError("temporary provider failure"), "answer"]
    )
    with _background():
        outcome = await _outcome(
            lambda: retry.call_with_retry(
                provider, "entity-extraction", max_attempts=2, base_delay=0
            )
        )

    assert provider.await_count == 1, "retry bypassed the tenant's last credit"
    assert reserve.await_count == 2
    assert type(outcome).__name__ == "BackgroundWorkDeferred"


async def test_a_fallback_cannot_bypass_deferral_or_turn_it_into_a_heuristic(reserve):
    reserve.side_effect = [
        {"allowed": True, "retry_after_seconds": 0},
        {"allowed": False, "retry_after_seconds": 60},
    ]
    providers = {
        "openai": SimpleNamespace(
            is_fake=False, provider_name="openai", model="primary"
        ),
        "vertex": SimpleNamespace(
            is_fake=False, provider_name="vertex", model="fallback"
        ),
    }
    calls = []

    async def call(provider):
        calls.append(provider.model)
        if provider.model == "primary":
            raise RuntimeError("temporary provider failure")
        return "fallback answer"

    heuristic = []

    def fake():
        heuristic.append(True)
        return "heuristic"

    config = SimpleNamespace(resolve_fallback=lambda: ("vertex", "fallback"))
    with _background():
        outcome = await _outcome(
            lambda: retry.call_with_fallback(
                "openai",
                call,
                fake,
                config,
                provider_factory=lambda name, *_a, **_kw: providers[name],
                service_label="entity-extraction",
                max_attempts=1,
            )
        )

    assert calls == ["primary"], "fallback spent a call after tenant exhaustion"
    assert heuristic == [], "budget deferral must remain retryable work"
    assert type(outcome).__name__ == "BackgroundWorkDeferred"


@pytest.mark.parametrize("tenant", [None, ""])
async def test_background_work_without_a_tenant_fails_closed(reserve, tenant):
    provider = AsyncMock(return_value="answer")
    with _background(tenant):
        outcome = await _outcome(
            lambda: retry.call_with_retry(provider, "enrichment", max_attempts=1)
        )
    assert provider.await_count == 0, "unattributed background work reached a provider"
    assert reserve.await_count == 0
    assert type(outcome).__name__ == "BackgroundWorkDeferred"


async def test_reservation_outage_fails_closed_and_logs_no_upstream_payload(
    reserve, caplog
):
    sentinel = "synthetic upstream body must stay out of logs"
    reserve.side_effect = RuntimeError(sentinel)
    provider = AsyncMock(return_value="answer")
    with caplog.at_level(logging.INFO), _background():
        outcome = await _outcome(
            lambda: retry.call_with_retry(provider, "enrichment", max_attempts=2)
        )

    assert provider.await_count == 0, (
        "storage outage turned the budget into unlimited calls"
    )
    assert type(outcome).__name__ == "BackgroundWorkDeferred"
    assert any(
        getattr(r, "reason", None) == "budget_unavailable" for r in caplog.records
    )
    assert sentinel not in caplog.text
    assert all(
        r.exc_info is None
        for r in caplog.records
        if getattr(r, "reason", None) == "budget_unavailable"
    )


async def test_optional_usage_metering_cannot_disable_enforcement(reserve):
    provider = AsyncMock(return_value="answer")
    previous = get_usage_sink()
    set_usage_sink(None)
    try:
        with _background():
            await _outcome(
                lambda: retry.call_with_retry(provider, "enrichment", max_attempts=1)
            )
    finally:
        set_usage_sink(previous)
    assert provider.await_count == 0, (
        "turning off the optional meter disabled the budget"
    )


async def test_credentials_and_service_callers_share_one_tenant_budget(reserve):
    used = {}

    async def reservation(*_a, **_kw):
        tenant = current_scope().tenant_id
        before = used.get(tenant, 0)
        used[tenant] = before + 1
        return {"allowed": before < 2, "retry_after_seconds": 60}

    reserve.side_effect = reservation
    calls = []

    async def one(tenant, agent, operation):
        async def provider():
            calls.append((tenant, operation))
            return "answer"

        with _background(tenant, agent), usage_scope(operation=operation):
            await _outcome(
                lambda: retry.call_with_retry(provider, operation, max_attempts=1)
            )

    await asyncio.gather(
        *(
            one(tenant, f"credential-{n}", operation)
            for tenant in ("tenant-a", "tenant-b")
            for n, operation in enumerate(["entity-extraction", "enrichment"] * 3)
        )
    )
    assert sum(t == "tenant-a" for t, _ in calls) == 2
    assert sum(t == "tenant-b" for t, _ in calls) == 2


async def test_foreground_calls_do_not_consult_the_background_budget(reserve):
    reserve.side_effect = RuntimeError("background authority unavailable")
    provider = AsyncMock(return_value="answer")
    with usage_scope(tenant_id="tenant-a", operation="write"):
        assert (
            await retry.call_with_retry(provider, "enrichment", max_attempts=1)
            == "answer"
        )
    assert provider.await_count == 1
    reserve.assert_not_awaited()


async def test_distinct_provider_attempts_do_not_reuse_a_reservation_id(reserve):
    reserve.return_value = {"allowed": True, "retry_after_seconds": 0}
    provider = AsyncMock(
        side_effect=[RuntimeError("temporary provider failure"), "answer"]
    )
    with _background():
        assert (
            await retry.call_with_retry(
                provider, "entity-extraction", max_attempts=2, base_delay=0
            )
            == "answer"
        )
    assert reserve.await_count == 2, "each outbound attempt needs its own reservation"
    ids = [call.kwargs["attempt_id"] for call in reserve.await_args_list]
    assert len(set(ids)) == 2, "reusing an id would replay a free credit"


async def test_cancelling_a_reservation_starts_no_provider_call(reserve):
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(*_a, **_kw):
        entered.set()
        await release.wait()
        return {"allowed": True, "retry_after_seconds": 0}

    reserve.side_effect = waiting
    provider = AsyncMock(return_value="answer")
    with _background():
        task = asyncio.create_task(
            retry.call_with_retry(provider, "enrichment", max_attempts=1)
        )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        observed = entered.is_set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert observed, "background work did not attempt its reservation"
    assert provider.await_count == 0
