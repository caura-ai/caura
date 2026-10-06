"""Real loopback transport with a synthetic upstream; never calls Anthropic."""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/claude_review_proxy.py"


@pytest.fixture
def proxy(monkeypatch, request):
    spec = importlib.util.spec_from_file_location("review_proxy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    upstream_type = getattr(request, "param", "text/event-stream")

    class Upstream:
        def __init__(self, host, timeout):
            assert host == "api.anthropic.com"
            assert timeout == 120

        def request(self, method, path, body, headers):
            calls.append((method, path, body, headers))

        def getresponse(self):
            response = io.BytesIO(b"event: message\ndata: synthetic\n\n")
            response.status = 200
            response.getheader = lambda *_: upstream_type
            return response

        def close(self):
            pass

    monkeypatch.setattr(module.http.client, "HTTPSConnection", Upstream)
    server = module.ReviewServer(
        "synthetic-upstream-only", "synthetic-local-token", "test"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "path", ["/v1/messages?beta=true", "/v1/messages/count_tokens"]
)
def test_proxy_substitutes_key_only_for_fixed_https_upstream(proxy, path):
    server, calls = proxy
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    body = b'{"model":"test","max_tokens":1024}'
    try:
        client.request(
            "POST",
            path,
            body=body,
            headers={
                "x-api-key": "synthetic-local-token",
                "anthropic-beta": "test-beta",
                "Authorization": "never-forward-this",
                "Host": "attacker.invalid",
            },
        )
        response = client.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream"
        assert response.read() == b"event: message\ndata: synthetic\n\n"
    finally:
        client.close()
    method, actual_path, actual_body, headers = calls[0]
    assert (method, actual_path, actual_body) == ("POST", path, body)
    assert headers["x-api-key"] == "synthetic-upstream-only"
    assert headers["anthropic-beta"] == "test-beta"
    assert "Authorization" not in headers and "Host" not in headers


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("application/json; charset=utf-8", "application/json"),
        ("Text/Event-Stream; charset=utf-8", "text/event-stream"),
        ("text/html", "application/json"),
        ("text/event-stream\r\nX-Injected: yes", "application/json"),
        ("text/event-stream; charset=utf-8\r\nX-Injected: yes", "text/event-stream"),
        ("application/json\r\n\r\nInjected body", "application/json"),
    ],
    indirect=["proxy"],
)
def test_proxy_emits_only_constant_response_content_types(proxy, expected):
    server, _ = proxy
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request(
            "POST",
            "/v1/messages",
            body=b'{"model":"test","max_tokens":1024}',
            headers={"x-api-key": "synthetic-local-token"},
        )
        response = client.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == expected
        assert response.getheader("X-Injected") is None
        assert response.read() == b"event: message\ndata: synthetic\n\n"
    finally:
        client.close()


@pytest.mark.parametrize(
    ("path", "token", "status"),
    [
        ("/v1/messages", "wrong-token", 403),
        ("/v1/other", "synthetic-local-token", 404),
        ("https://attacker.invalid/v1/messages", "synthetic-local-token", 404),
    ],
)
def test_proxy_rejects_wrong_token_or_destination(proxy, path, token, status):
    server, calls = proxy
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request("POST", path, body=b"{}", headers={"x-api-key": token})
        assert client.getresponse().status == status
    finally:
        client.close()
    assert not calls


def _post(server, body, path="/v1/messages"):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request(
            "POST",
            path,
            body=body,
            headers={"x-api-key": "synthetic-local-token"},
        )
        response = client.getresponse()
        response.read()
        return response.status
    finally:
        client.close()


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"not json", 400),
        (b"[]", 403),
        (b'{"model":"other","max_tokens":1}', 403),
        (b'{"max_tokens":1}', 403),
        (b'{"model":"test"}', 400),
        (b'{"model":"test","max_tokens":true}', 400),
        (b'{"model":"test","max_tokens":"1024"}', 400),
        (b'{"model":"test","max_tokens":0}', 400),
        (b'{"model":"test","max_tokens":65537}', 400),
    ],
)
def test_disallowed_payload_never_reaches_provider(proxy, body, status):
    server, calls = proxy
    assert _post(server, body) == status
    assert not calls


def test_count_tokens_needs_the_selected_model_but_no_output_budget(proxy):
    server, calls = proxy
    path = "/v1/messages/count_tokens"
    assert _post(server, b'{"model":"other"}', path) == 403
    assert _post(server, b'{"model":"test"}', path) == 200
    assert len(calls) == 1


def test_request_budget_is_shared_by_messages_and_count_tokens(proxy):
    server, calls = proxy
    server.remaining_requests = 2
    assert _post(server, b'{"model":"test"}', "/v1/messages/count_tokens") == 200
    body = b'{"model":"test","max_tokens":65536}'
    assert _post(server, body) == 200
    assert _post(server, body) == 429
    assert len(calls) == 2


def test_cumulative_bytes_are_charged_even_for_rejected_payloads(proxy):
    server, calls = proxy
    body = json.dumps({"model": "other", "max_tokens": 1}).encode()
    server.remaining_bytes = len(body)
    assert _post(server, body) == 403
    assert server.remaining_bytes == 0
    assert _post(server, b'{"model":"test","max_tokens":1}') == 429
    assert not calls


def test_expired_proxy_rejects_requests_without_calling_provider(proxy):
    server, calls = proxy
    server.deadline = 0
    assert _post(server, b'{"model":"test","max_tokens":1}') == 403
    assert not calls


def test_concurrent_reservations_cannot_overspend_the_budget(proxy):
    server, _ = proxy
    server.remaining_requests = 3
    server.remaining_bytes = 30
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(server.reserve, [10] * 32))
    assert results.count(None) == 3
    assert results.count(429) == 29
    assert server.remaining_requests == server.remaining_bytes == 0
