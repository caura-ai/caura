"""Retry-After on retryable responses bounds how soon the client comes back."""

import httpx
import pytest

from caura_bus_core import AgentConfig, Bus, PlatformError


def config():
    return AgentConfig(
        agent={"agent_id": "a", "tenant_id": "t"},
        peers=["b"],
        api_url="https://caura.test",
    )


async def _run(monkeypatch, responses):
    calls = []
    sleeps = []

    async def handle(request):
        calls.append(request)
        status, headers = responses[min(len(calls) - 1, len(responses) - 1)]
        if status == 200:
            return httpx.Response(200, json=[])
        return httpx.Response(status, headers=headers, json={"detail": "busy"})

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("caura_bus_core.bus.asyncio.sleep", sleep)
    monkeypatch.setattr("caura_bus_core.bus.secrets.randbelow", lambda n: 0)
    bus = Bus(config(), api_key="key", transport=httpx.MockTransport(handle))
    try:
        result = await bus.request("GET", "agents", retry_safe=True)
    finally:
        await bus.close()
    return result, calls, sleeps


async def test_retry_after_is_honoured(monkeypatch):
    result, calls, sleeps = await _run(monkeypatch, [(503, {"Retry-After": "2"}), (200, {})])
    assert result == [] and len(calls) == 2
    assert sleeps == [2.0]


async def test_retry_after_is_capped_at_five_seconds(monkeypatch):
    _, calls, sleeps = await _run(monkeypatch, [(429, {"Retry-After": "30"}), (200, {})])
    assert len(calls) == 2 and sleeps == [5.0]


async def test_absent_or_invalid_header_keeps_backoff(monkeypatch):
    _, _, sleeps = await _run(monkeypatch, [(503, {}), (503, {"Retry-After": "soon"}), (200, {})])
    assert sleeps == [0.25, 0.5]


async def test_short_hint_never_shortens_backoff(monkeypatch):
    _, _, sleeps = await _run(monkeypatch, [(503, {"Retry-After": "0"}), (200, {})])
    assert sleeps == [0.25]


async def test_non_retryable_status_ignores_header(monkeypatch):
    with pytest.raises(PlatformError) as exc:
        await _run(monkeypatch, [(400, {"Retry-After": "2"})])
    assert exc.value.status == 400
