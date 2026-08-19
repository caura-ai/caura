"""Unit tests for the Caura client — fully mocked via httpx.MockTransport, no network."""

from __future__ import annotations

import json

import httpx
import pytest

from caura_client import (
    AuthError,
    Caura,
    CauraAPIError,
    Memory,
    NotFoundError,
    RecallResult,
)


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return Caura(
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


def test_search_raises_on_missing_items_key():
    def handler(request):
        return httpx.Response(200, json={"error": "quota exceeded"})

    with pytest.raises(CauraAPIError) as exc:
        make_client(handler).search("q")
    assert exc.value.status_code == 200
    assert str(exc.value) == '[200] search response missing "items" list'


def test_search_raises_on_items_not_a_list():
    def handler(request):
        return httpx.Response(200, json={"items": "not-a-list"})

    with pytest.raises(CauraAPIError) as exc:
        make_client(handler).search("q")
    assert exc.value.status_code == 200
    assert str(exc.value) == '[200] search response "items" must be a list'


def test_search_raises_on_non_dict_body():
    def handler(request):
        return httpx.Response(200, json=["not", "a", "dict"])

    with pytest.raises(CauraAPIError) as exc:
        make_client(handler).search("q")
    assert exc.value.status_code == 200
    assert str(exc.value) == "[200] search response must be a JSON object"


# The exact top-level key set ``POST /api/v1/recall`` returns, per
# ``core_api.services.recall_service.summarize_memories`` — and pinned server-side
# by ``tests/test_c4_recall_items_alias.py::_EXPECTED_TOP_LEVEL_KEYS``. Note what
# is NOT here: ``supporting_memories``. H-01 was this SDK reading that invented
# key, with a hand-written fixture that mocked it, so the suite passed while every
# live recall() returned no memories.
def _live_recall_body(memories):
    return {
        "query": "q",
        "summary": "S",
        "memory_count": len(memories),
        "memories": memories,
        "items": memories,  # server aliases the identical list
        "recall_ms": 12,
    }


def test_recall_returns_summary_and_memories():
    def handler(request):
        assert request.url.path == "/api/v1/recall"
        return httpx.Response(200, json=_live_recall_body([{"id": "m1", "content": "a"}]))

    result = make_client(handler).recall("q")
    assert isinstance(result, RecallResult)
    assert result.summary == "S"
    assert result.supporting_memories[0].id == "m1"


def test_recall_accepts_the_items_alias_alone():
    """Older/other server shapes may send only ``items``; both name the same list."""

    def handler(request):
        return httpx.Response(
            200, json={"summary": "S", "items": [{"id": "m2", "content": "b"}]}
        )

    result = make_client(handler).recall("q")
    assert [m.id for m in result.supporting_memories] == ["m2"]


def test_recall_with_no_memories_is_empty_not_an_error():
    def handler(request):
        return httpx.Response(200, json=_live_recall_body([]))

    result = make_client(handler).recall("q")
    assert result.supporting_memories == []
    assert result.summary == "S"


def test_recall_ignores_the_key_the_server_never_sends():
    """Guard against the regression: a body carrying ONLY the invented key must
    yield no memories, so nobody "fixes" this by reinstating it."""

    def handler(request):
        return httpx.Response(
            200, json={"summary": "S", "supporting_memories": [{"id": "ghost"}]}
        )

    result = make_client(handler).recall("q")
    assert result.supporting_memories == []


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

    with pytest.raises(CauraAPIError):
        make_client(handler).recall("q")


def test_context_manager():
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    with make_client(handler) as mc:
        assert mc.health()["status"] == "ok"


def test_requires_api_key_and_tenant():
    with pytest.raises(ValueError):
        Caura("", tenant_id="t")
    with pytest.raises(ValueError):
        Caura("k", tenant_id="")
