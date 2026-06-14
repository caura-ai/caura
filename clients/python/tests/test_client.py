"""Unit tests for the MemClaw client — fully mocked via httpx.MockTransport, no network."""

from __future__ import annotations

import json

import httpx
import pytest

from memclaw_client import (
    AuthError,
    MemClaw,
    MemClawAPIError,
    Memory,
    NotFoundError,
    RecallResult,
)


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return MemClaw(
        "mc_test",
        tenant_id="t1",
        base_url="https://example.test",
        transport=transport,
        **kwargs,
    )


def test_write_returns_memory():
    def handler(request):
        assert request.url.path == "/api/v1/memories"
        assert request.headers["X-API-Key"] == "mc_test"
        body = json.loads(request.content)
        assert body == {"tenant_id": "t1", "content": "hello", "agent_id": "a1"}
        return httpx.Response(201, json={"id": "m1", "content": "hello", "title": "Hi", "agent_id": "a1"})

    mem = make_client(handler, agent_id="a1").write("hello")
    assert isinstance(mem, Memory)
    assert mem.id == "m1"
    assert mem.title == "Hi"
    assert mem.raw["agent_id"] == "a1"


def test_write_per_call_agent_overrides_default():
    def handler(request):
        assert json.loads(request.content)["agent_id"] == "override"
        return httpx.Response(201, json={"id": "m1", "content": "x"})

    make_client(handler, agent_id="default").write("x", agent_id="override")


def test_search_returns_list():
    def handler(request):
        assert request.url.path == "/api/v1/search"
        body = json.loads(request.content)
        assert body["query"] == "q"
        assert body["top_k"] == 3
        return httpx.Response(200, json={"items": [{"id": "m1", "content": "a"}, {"id": "m2", "content": "b"}]})

    results = make_client(handler).search("q", top_k=3)
    assert [m.id for m in results] == ["m1", "m2"]


def test_recall_returns_summary():
    def handler(request):
        assert request.url.path == "/api/v1/recall"
        return httpx.Response(200, json={"summary": "S", "supporting_memories": [{"id": "m1", "content": "a"}]})

    result = make_client(handler).recall("q")
    assert isinstance(result, RecallResult)
    assert result.summary == "S"
    assert result.supporting_memories[0].id == "m1"


def test_health():
    def handler(request):
        assert request.url.path == "/api/v1/health"
        return httpx.Response(200, json={"status": "ok"})

    assert make_client(handler).health()["status"] == "ok"


def test_auth_error_parses_envelope():
    def handler(request):
        return httpx.Response(403, json={"error": {"message": "cross-fleet", "details": {"x": 1}}})

    with pytest.raises(AuthError) as exc:
        make_client(handler).write("x")
    assert exc.value.status_code == 403
    assert exc.value.details == {"x": 1}


def test_not_found_error():
    def handler(request):
        return httpx.Response(404, json={"detail": "nope"})

    with pytest.raises(NotFoundError):
        make_client(handler).search("q")


def test_generic_api_error():
    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(MemClawAPIError):
        make_client(handler).recall("q")


def test_context_manager():
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    with make_client(handler) as mc:
        assert mc.health()["status"] == "ok"


def test_requires_api_key_and_tenant():
    with pytest.raises(ValueError):
        MemClaw("", tenant_id="t")
    with pytest.raises(ValueError):
        MemClaw("k", tenant_id="")
