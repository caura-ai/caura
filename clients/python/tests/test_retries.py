"""Opt-in retry-with-backoff for transient failures (429/502/503/504)."""

from __future__ import annotations

import time

import httpx
import pytest

from caura_client import Caura, CauraAPIError, NotFoundError, RateLimitError, TransportError


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return Caura(
        "mc_test",
        tenant_id="t1",
        base_url="https://example.test",
        transport=transport,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Record backoff delays without actually pausing the test run."""
    delays = []
    monkeypatch.setattr(time, "sleep", lambda seconds: delays.append(seconds))
    return delays


def test_default_retries_is_zero_and_preserves_old_behavior(no_real_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("boom")

    with pytest.raises(TransportError):
        make_client(handler).search("q")
    assert len(calls) == 1
    assert no_real_sleep == []


@pytest.mark.parametrize(
    "method, args, kwargs",
    [
        ("search", ("q",), {}),
        ("recall", ("q",), {}),
        ("health", (), {}),
        ("get_document", ("doc-1",), {"collection": "interviews"}),
    ],
)
def test_read_methods_retry_transport_errors_then_succeed(no_real_sleep, method, args, kwargs):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("boom")
        if method in ("search", "recall"):
            return httpx.Response(200, json={"items": [], "summary": "S"})
        return httpx.Response(200, json={"status": "ok"})

    client = make_client(handler, retries=2, retry_backoff=0.1)
    getattr(client, method)(*args, **kwargs)
    assert len(calls) == 3
    assert no_real_sleep == [0.1, 0.2]


def test_read_method_exhausts_retries_and_raises_transport_error(no_real_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("boom")

    client = make_client(handler, retries=2, retry_backoff=0.1)
    with pytest.raises(TransportError):
        client.search("q")
    assert len(calls) == 3
    assert no_real_sleep == [0.1, 0.2]


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_retryable_status_codes_are_retried_then_succeed(no_real_sleep, status):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, json={"detail": "transient"})
        return httpx.Response(200, json={"items": []})

    client = make_client(handler, retries=1, retry_backoff=0.1)
    client.search("q")
    assert len(calls) == 2
    assert no_real_sleep == [0.1]


def test_retryable_status_exhausts_retries_and_raises_mapped_error(no_real_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"detail": "down"})

    client = make_client(handler, retries=2, retry_backoff=0.1)
    with pytest.raises(CauraAPIError) as exc:
        client.search("q")
    assert exc.value.status_code == 503
    assert len(calls) == 3
    assert no_real_sleep == [0.1, 0.2]


def test_other_4xx_is_never_retried(no_real_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404, json={"detail": "nope"})

    client = make_client(handler, retries=3, retry_backoff=0.1)
    with pytest.raises(NotFoundError):
        client.search("q")
    assert len(calls) == 1
    assert no_real_sleep == []


def test_retry_after_header_overrides_exponential_backoff(no_real_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2.5"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"items": []})

    client = make_client(handler, retries=1, retry_backoff=0.1)
    client.search("q")
    assert no_real_sleep == [2.5]


def test_retry_after_still_raises_rate_limit_error_once_exhausted(no_real_sleep):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"detail": "slow down"})

    client = make_client(handler, retries=1, retry_backoff=0.1)
    with pytest.raises(RateLimitError) as exc:
        client.search("q")
    assert exc.value.retry_after == 1.0
    assert no_real_sleep == [1.0]


@pytest.mark.parametrize(
    "method, args, kwargs",
    [
        ("write", ("hello",), {}),
        (
            "submit_interview",
            (),
            {"node_id": "n1", "agent_id": "a1", "cursor_from": 0, "cursor_to": 1, "events": []},
        ),
    ],
)
def test_write_operations_are_never_retried_even_when_configured(no_real_sleep, method, args, kwargs):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("boom")

    client = make_client(handler, retries=5, retry_backoff=0.1)
    with pytest.raises(TransportError):
        getattr(client, method)(*args, **kwargs)
    assert len(calls) == 1
    assert no_real_sleep == []


def test_negative_retries_rejected():
    with pytest.raises(ValueError):
        Caura("mc_test", tenant_id="t1", retries=-1)


def test_negative_retry_backoff_rejected():
    with pytest.raises(ValueError):
        Caura("mc_test", tenant_id="t1", retry_backoff=-0.1)
