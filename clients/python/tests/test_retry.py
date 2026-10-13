"""Opt-in retry tests use an in-memory HTTP transport; no network or sleeps."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import httpx
import pytest

from caura_client import Caura, RateLimitError, TransportError


def client(handler, **kwargs):
    return Caura("key", tenant_id="team", base_url="https://api.example.test",
                 transport=httpx.MockTransport(handler), **kwargs)


def test_no_retries_by_default():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    with pytest.raises(Exception):
        client(handler).health()
    assert len(calls) == 1


def test_safe_get_retries_transient_failure():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(503 if len(seen) < 3 else 200, json={"ok": True})
    with patch("caura_client.client.time.sleep") as sleep:
        response = client(handler, retries=2, retry_backoff=.2).health()
    assert response["ok"] is True
    assert len(seen) == 3
    assert [c.args[0] for c in sleep.call_args_list] == [.2, .4]


def test_read_only_post_search_retries():
    attempts = []
    def handler(request):
        attempts.append(request)
        return (httpx.Response(429) if len(attempts) == 1
                else httpx.Response(200, json={"items": []}))
    with patch("caura_client.client.time.sleep"):
        assert client(handler, retries=1).search("query") == []
    assert len(attempts) == 2


def test_mutating_post_is_never_retried_even_when_opted_in():
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(503)
    with patch("caura_client.client.time.sleep") as sleep:
        with pytest.raises(Exception):
            client(handler, retries=3).write("do not duplicate")
    assert len(attempts) == 1
    sleep.assert_not_called()


def test_unauthorized_status_is_not_retried():
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(401)
    with patch("caura_client.client.time.sleep") as sleep:
        with pytest.raises(Exception):
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
        return httpx.Response(429 if len(seen) == 1 else 200,
                              headers={"Retry-After": "3"},
                              json={"ok": True})
    with patch("caura_client.client.time.sleep") as sleep:
        client(handler, retries=1).health()
    sleep.assert_called_once_with(3.0)
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 0 <= Caura._retry_after_seconds(date) <= 30
    assert Caura._retry_after_seconds("invalid") is None


@pytest.mark.parametrize("options", [
    {"retries": -1}, {"retries": 11}, {"retries": 1.5}, {"retries": True},
    {"retry_backoff": -1.0}, {"retry_backoff": float("nan")},
])
def test_invalid_retry_configuration(options):
    with pytest.raises(ValueError):
        client(lambda _: httpx.Response(200), **options)
