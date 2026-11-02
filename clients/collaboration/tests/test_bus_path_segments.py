"""Identifiers stay inside their intended HTTP path or query field."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from caura_bus_core import AgentConfig, Bus
from caura_bus_mcp.server import Ack, Collect, Human, Status
from pydantic import ValidationError


@asynccontextmanager
async def client(*args, **kwargs):
    bus = Bus(*args, **kwargs)
    try:
        yield bus
    finally:
        await bus.close()


@pytest.mark.parametrize("identifier", ["a/b?after=0#tail", "../../memories?limit=5", "space and %2F"])
@pytest.mark.parametrize("operation", ["action", "reply", "settle", "status"])
async def test_identifiers_are_one_wire_segment(identifier, operation):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={})

    config = AgentConfig(agent={"agent_id": "a", "tenant_id": "t"}, peers=["b"], api_url="https://caura.test")
    async with client(config, api_key="test-key", transport=httpx.MockTransport(respond)) as bus:
        if operation == "action":
            await bus.delivery_action(identifier, "ack")
        elif operation == "reply":
            await bus.reply(identifier, idempotency_key="one", body="reply")
        elif operation == "settle":
            await bus.settle(SimpleNamespace(delivery_id=identifier, lease_token="lease"), "ack")
        else:
            await bus.status(identifier)
    (request,) = seen
    segment = quote(identifier, safe="")
    suffix = (
        f"messages/{segment}"
        if operation == "status"
        else f"deliveries/{segment}/{'reply' if operation == 'reply' else 'ack'}"
    )
    assert request.url.raw_path.decode() == "/api/v1/bus/" + suffix
    assert not request.url.query and not request.url.fragment


@pytest.mark.parametrize("identifier", [".", ".."])
async def test_dot_only_identifiers_are_rejected(identifier):
    config = AgentConfig(agent={"agent_id": "a", "tenant_id": "t"}, peers=["b"], api_url="https://caura.test")
    async with client(
        config, api_key="test-key", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as bus:
        with pytest.raises(ValueError):
            await bus.status(identifier)
        with pytest.raises(ValueError):
            await bus.delivery_action(identifier, "ack")


@pytest.mark.parametrize("identifier", ["../x", "x?q=1", "x#tail", ".", "..", "white space"])
@pytest.mark.parametrize(
    "model,field,extra",
    [
        (Ack, "delivery_id", {}),
        (Human, "delivery_id", {"reason": "help"}),
        (Status, "message_id", {}),
        (Collect, "message_id", {}),
    ],
)
def test_mcp_ids_have_a_bounded_alphabet(identifier, model, field, extra):
    with pytest.raises(ValidationError):
        model(**{field: identifier}, **extra)


async def test_memory_context_keeps_its_reference_in_the_query():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={})

    config = AgentConfig(agent={"agent_id": "a", "tenant_id": "t"}, peers=["b"], api_url="https://caura.test")
    async with client(config, api_key="test-key", transport=httpx.MockTransport(respond)) as bus:
        await bus.memory_context(message_id="a/b?c#d")
    assert seen[0].url.path == "/api/v1/bus/memory-context"
    assert dict(seen[0].url.params) == {"message_id": "a/b?c#d"}


@pytest.mark.parametrize("identifier", ["d_123", "msg:123", "1a2b-3c4d", ".versioned-id", "x.y"])
def test_mcp_ids_keep_supported_identifier_characters(identifier):
    assert Ack(delivery_id=identifier).delivery_id == identifier
    assert Status(message_id=identifier).message_id == identifier
