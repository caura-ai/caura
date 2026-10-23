"""Opt-in retry tests use an in-memory HTTP transport; no network or sleeps."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import httpx
import pytest

from caura_client import AuthError, Caura, CauraAPIError, RateLimitError, TransportError


def client(handler, **kwargs):
    return Caura(
        "key",
        tenant_id="team",
        base_url="https://api.example.test",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_no_retries_by_default():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(CauraAPIError):
        client(handler).health()
    assert len(calls) == 1


def test_safe_get_retries_transient_failure():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(503 if len(seen) < 3 else 200, json={"ok": True})

    with patch("caura_client.client.time.sleep") as sleep:
        response = client(handler, retries=2, retry_backoff=0.2).health()
    assert response["ok"] is True
    assert len(seen) == 3
    assert [c.args[0] for c in sleep.call_args_list] == [0.2, 0.4]


def test_read_only_post_search_retries():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(429) if len(attempts) == 1 else httpx.Response(200, json={"items": []})

    with patch("caura_client.client.time.sleep"):
        assert client(handler, retries=1).search("query") == []
    assert len(attempts) == 2


def test_mutating_post_is_never_retried_even_when_opted_in():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503)

    with patch("caura_client.client.time.sleep") as sleep, pytest.raises(CauraAPIError):
        client(handler, retries=3).write("do not duplicate")
    assert len(attempts) == 1
    sleep.assert_not_called()


def test_unauthorized_status_is_not_retried():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(401)

    with patch("caura_client.client.time.sleep") as sleep, pytest.raises(AuthError):
        client(handler, retries=3).health()
    assert len(attempts) == 1
    sleep.assert_not_called()


def test_transport_errors_retry_only_on_read_operations():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={"ok": True})

    with patch("caura_client.client.time.sleep"):
        assert client(handler, retries=1).health()["ok"] is True
    assert len(seen) == 2


def test_retry_after_seconds_and_date():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(429 if len(seen) == 1 else 200, headers={"Retry-After": "3"}, json={"ok": True})

    with patch("caura_client.client.time.sleep") as sleep:
        client(handler, retries=1).health()
    sleep.assert_called_once_with(3.0)
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 0 <= Caura._retry_after_seconds(date) <= 30
    assert Caura._retry_after_seconds("invalid") is None


@pytest.mark.parametrize(
    "options",
    [
        {"retries": -1},
        {"retries": 11},
        {"retries": 1.5},
        {"retries": True},
        {"retry_backoff": -1.0},
        {"retry_backoff": float("nan")},
        {"retry_backoff": "bad"},
        {"max_retry_delay": 0},
        {"max_retry_delay": -1},
        {"max_retry_delay": float("inf")},
        {"max_retry_delay": True},
    ],
)
def test_invalid_retry_configuration(options):
    with pytest.raises(ValueError):
        client(lambda _: httpx.Response(200), **options)


@pytest.mark.parametrize(
    "path, operation",
    [
        ("/api/v1/search", "search"),
        ("/api/v1/recall", "recall"),
    ],
)
@pytest.mark.parametrize("status", [502, 503, 504])
def test_metered_post_never_retries_possible_completed_work(path, operation, status):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(status)

    sdk = client(handler, retries=4, retry_backoff=0.1)
    with patch("caura_client.client.time.sleep") as sleep:
        response = sdk._request("POST", path, json={})
    assert response.status_code == status
    assert len(attempts) == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ReadError])
def test_metered_post_does_not_retry_after_request_was_sent(exception):
    attempts = []

    def handler(request):
        attempts.append(request)
        raise exception("response lost", request=request)

    with patch("caura_client.client.time.sleep") as sleep, pytest.raises(TransportError):
        client(handler, retries=4)._request("POST", "/api/v1/recall", json={})
    assert len(attempts) == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ConnectTimeout])
def test_metered_post_retries_only_connection_failures(exception):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise exception("connection not established", request=request)
        return httpx.Response(200, json={"items": []})

    with patch("caura_client.client.time.sleep") as sleep:
        response = client(handler, retries=1)._request("POST", "/api/v1/search", json={})
    assert response.status_code == 200
    assert len(attempts) == 2
    sleep.assert_called_once()


@pytest.mark.parametrize("header", ["86400", "Wed, 21 Oct 2099 07:28:00 GMT"])
def test_huge_retry_after_returns_response_without_waiting(header):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": header})

    with patch("caura_client.client.time.sleep") as sleep, pytest.raises(RateLimitError) as error:
        client(handler, retries=5, max_retry_delay=60).health()
    assert len(attempts) == 1
    assert error.value.retry_after is not None and error.value.retry_after > 60
    sleep.assert_not_called()


def test_exponential_backoff_is_capped():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503 if len(attempts) == 1 else 200, json={"ok": True})

    with patch("caura_client.client.time.sleep") as sleep:
        assert client(handler, retries=1, retry_backoff=100, max_retry_delay=2).health() == {"ok": True}
    sleep.assert_called_once_with(2)


@pytest.mark.parametrize("header", ["nan", "inf", "-5", "not a date"])
def test_shared_retry_after_parser_ignores_invalid_values(header):
    assert Caura._retry_after_seconds(header) is None
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": header})

    with patch("caura_client.client.time.sleep") as sleep, pytest.raises(RateLimitError) as error:
        client(handler, retries=1, retry_backoff=0.1).health()
    assert error.value.retry_after is None
    assert len(attempts) == 2
    sleep.assert_called_once_with(0.1)
