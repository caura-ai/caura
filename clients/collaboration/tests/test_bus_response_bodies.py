"""Unexpected response bodies surface as PlatformError, never as parser errors."""

import json

import httpx
import pytest
from caura_bus_core import AgentConfig, Bus, PlatformError


def config():
    return AgentConfig(
        agent={"agent_id": "a", "tenant_id": "t"},
        peers=["b"],
        api_url="https://caura.test",
    )


async def _request(handler):
    bus = Bus(config(), api_key="key", transport=httpx.MockTransport(handler))
    try:
        return await bus.request("GET", "agents")
    finally:
        await bus.close()


def _body(status, content):
    return lambda request: httpx.Response(
        status, content=content, headers={"content-type": "application/json"}
    )


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize("content", [b"<html>captive portal login</html>", b""], ids=["html", "empty"])
async def test_success_without_json_body_raises_platform_error(status, content):
    """A 2xx served as HTML (captive portal, proxy) must not leak JSONDecodeError."""

    with pytest.raises(PlatformError) as exc:
        await _request(_body(status, content))
    assert exc.value.status == status
    assert exc.value.detail == "response was not valid JSON"
    assert isinstance(exc.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    "content",
    [b"[]", b'"busy"', b"503", b"null", b"true"],
    ids=["list", "string", "number", "null", "boolean"],
)
async def test_error_with_non_object_json_body_keeps_its_status(content):
    """A non-object error body must still reach the caller as PlatformError."""

    with pytest.raises(PlatformError) as exc:
        await _request(_body(400, content))
    assert exc.value.status == 400
    assert exc.value.detail == "request rejected"


@pytest.mark.parametrize("content", [b"<html>bad gateway</html>", b""], ids=["html", "empty"])
async def test_error_without_json_body_keeps_gateway_message(content):
    """An unparseable error body keeps the message callers already see."""

    with pytest.raises(PlatformError) as exc:
        await _request(_body(502, content))
    assert exc.value.status == 502
    assert exc.value.detail == "gateway rejected request"


async def test_error_object_body_still_reports_detail():
    """The documented ``{"detail": ...}`` path keeps working."""

    with pytest.raises(PlatformError) as exc:
        await _request(lambda request: httpx.Response(409, json={"detail": "already claimed"}))
    assert exc.value.status == 409
    assert exc.value.detail == "already claimed"
