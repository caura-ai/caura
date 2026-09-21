import json

import httpx
import pytest
from caura_bus_core import AgentConfig, Bus, PlatformError, SendMessage
from pydantic import ValidationError


def config(**kwargs):
    return AgentConfig(
        agent={"agent_id": "a", "tenant_id": "t"},
        peers=["b"],
        api_url="https://caura.test",
        **kwargs,
    )


def test_obsolete_transport_config_fails():
    with pytest.raises(ValidationError):
        config(redis_url="redis://localhost")


@pytest.mark.parametrize(
    "url",
    [
        "redis://localhost",
        "https://user:secret@caura.test",
        "https://caura.test/api/v1",
        "https://caura.test?key=secret",
    ],
)
def test_invalid_api_urls_rejected(url):
    with pytest.raises(ValidationError):
        AgentConfig(agent={"agent_id": "a", "tenant_id": "t"}, api_url=url)


def test_insecure_http_requires_explicit_local_opt_in():
    cfg = config().model_copy(update={"api_url": "http://localhost"})
    with pytest.raises(ValueError):
        Bus(cfg, api_key="key")


async def test_identity_mismatch_fails_closed():
    async def handle(request):
        return httpx.Response(200, json={"tenant_id": "other", "agent_id": "a"})

    with pytest.raises(PlatformError, match="does not match"):
        async with Bus(config(), api_key="key", transport=httpx.MockTransport(handle)):
            pytest.fail("wrong identity connected")


async def test_ambiguous_send_reuses_key_and_body():
    requests = []

    async def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(
            202, json={"message_id": "m", "thread_id": "t", "recipients": ["b"], "duplicate": True}
        )

    bus = Bus(config(), api_key="key", transport=httpx.MockTransport(handle))
    try:
        assert (await bus.send(SendMessage(to=["b"], body="hi"), idempotency_key="stable")).duplicate
        assert len(requests) == 2
        assert requests[0].content == requests[1].content
        assert all(r.headers["Idempotency-Key"] == "stable" for r in requests)
        assert all(r.headers["X-API-Key"] == "key" for r in requests)
    finally:
        await bus.close()


async def test_claim_is_not_retried_after_uncertain_response():
    requests = []

    async def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout("lost")

    bus = Bus(config(), api_key="key", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(httpx.ReadTimeout):
            await bus.claim()
        assert len(requests) == 1
    finally:
        await bus.close()


async def test_mcp_wait_spans_gateway_safe_http_polls_with_one_session(monkeypatch):
    elapsed = 0
    requests = []

    async def handle(request):
        nonlocal elapsed
        body = json.loads(request.content)
        requests.append(body)
        elapsed += body["timeout"]
        return httpx.Response(200, json={"delivery": None})

    monkeypatch.setattr("caura_bus_core.bus.monotonic", lambda: elapsed)
    bus = Bus(config(), api_key="key", transport=httpx.MockTransport(handle))
    try:
        assert await bus.wait("stable-session") is None
        assert [r["timeout"] for r in requests] == [20, 20, 10]
        assert {r["session_id"] for r in requests} == {"stable-session"}
    finally:
        await bus.close()


async def test_wait_absorbs_transient_5xx_and_transport_failures_within_budget(monkeypatch):
    elapsed = 0
    requests = []

    async def handle(request):
        nonlocal elapsed
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(504, text="<html>old gateway timeout</html>")
        if len(requests) == 2:
            raise httpx.ConnectError("deploying")
        elapsed += body["timeout"]
        return httpx.Response(200, json={"delivery": None})

    async def backoff(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr("caura_bus_core.bus.monotonic", lambda: elapsed)
    monkeypatch.setattr("caura_bus_core.bus.asyncio.sleep", backoff)
    bus = Bus(config(), api_key="key", transport=httpx.MockTransport(handle))
    try:
        assert await bus.wait("same-session", 5) is None
        assert elapsed == 5 and len(requests) == 3
        assert {r["session_id"] for r in requests} == {"same-session"}
    finally:
        await bus.close()


async def test_wait_unavailable_budget_and_revocation_fail_explicitly(monkeypatch):
    elapsed = 0
    status = 503
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": "unavailable"})

    async def backoff(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr("caura_bus_core.bus.monotonic", lambda: elapsed)
    monkeypatch.setattr("caura_bus_core.bus.asyncio.sleep", backoff)
    bus = Bus(config(), api_key="key", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(PlatformError, match="remained unavailable"):
            await bus.wait("same-session", 1)
        assert elapsed == 1
        requests.clear()
        status = 403
        with pytest.raises(PlatformError) as exc:
            await bus.wait("same-session", 50)
        assert exc.value.status == 403 and len(requests) == 1
    finally:
        await bus.close()


async def test_auth_rejection_not_retried_and_redirect_not_followed():
    for status in [401, 403, 307]:
        requests = []

        async def handle(request, requests=requests, status=status):
            requests.append(request)
            return httpx.Response(
                status, json={"detail": "rejected"}, headers={"location": "https://untrusted.test"}
            )

        bus = Bus(config(), api_key="key", transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(PlatformError):
                await bus.send(SendMessage(to=["b"], body="hi"), idempotency_key="key")
            assert len(requests) == 1
        finally:
            await bus.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"to": ["b"], "body": "hi", "from": "spoof"},
        {"to": ["b"], "body": "hi", "tenant_id": "other"},
        {"to": ["b"], "body": "hi", "kind": "response"},
        {"to": [], "body": "hi"},
        {"to": ["b", "b"], "body": "hi"},
        {"to": ["*"], "body": "hi"},
        {"to": ["b"], "body": "x" * 65537},
    ],
)
def test_invalid_send_shapes_rejected(payload):
    with pytest.raises(ValidationError):
        SendMessage.model_validate(payload)
