"""The client refuses to send the API key over plain HTTP to another machine (L-66).

``Caura`` sent ``X-API-Key`` to whatever ``base_url`` it was given, so a dropped
``s`` or a copied internal ``http://`` URL put the key on the network in clear on
every call. The OpenClaw plugin already refuses that unless
``CAURA_ALLOW_INSECURE_HTTP`` is set; the SDK now applies the same rule: https,
or plain http to a loopback host, or an explicit opt-in.
"""

from __future__ import annotations

import httpx
import pytest

from caura_client import Caura
from caura_client.interviewer.installer import render_env_file


@pytest.fixture(autouse=True)
def _no_env_opt_in(monkeypatch):
    monkeypatch.delenv("CAURA_ALLOW_INSECURE_HTTP", raising=False)


def _client(base_url, **kwargs):
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    return Caura(
        "mc_test",
        tenant_id="t1",
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_plain_http_to_a_remote_host_is_refused():
    with pytest.raises(ValueError, match=r"caura\.example") as exc:
        _client("http://caura.example")
    assert "allow_insecure_http" in str(exc.value)
    assert "CAURA_ALLOW_INSECURE_HTTP" in str(exc.value)


@pytest.mark.parametrize("scheme", ["ftp", "ws", "file"])
def test_a_scheme_other_than_http_or_https_is_refused(scheme):
    with pytest.raises(ValueError, match="https://"):
        _client(f"{scheme}://caura.example")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://caura.example",
        "http://localhost:8000",
        "http://LOCALHOST:8000",
        "http://api.localhost",
        "http://127.0.0.1:8000",
        "http://127.8.9.10",
        "http://[::1]:8000",
    ],
)
def test_https_and_loopback_http_are_allowed(base_url):
    assert _client(base_url).health() == {"status": "ok"}


def test_the_opt_in_allows_plain_http():
    assert _client("http://caura.example", allow_insecure_http=True).health() == {"status": "ok"}


@pytest.mark.parametrize("value", ["true", "1"])
def test_the_env_opt_in_allows_plain_http(monkeypatch, value):
    monkeypatch.setenv("CAURA_ALLOW_INSECURE_HTTP", value)
    assert _client("http://caura.example").health() == {"status": "ok"}


@pytest.mark.parametrize("value", ["", "false", "0", "yes"])
def test_other_env_values_do_not_opt_in(monkeypatch, value):
    monkeypatch.setenv("CAURA_ALLOW_INSECURE_HTTP", value)
    with pytest.raises(ValueError):
        _client("http://caura.example")


def test_an_explicit_refusal_beats_the_env_opt_in(monkeypatch):
    monkeypatch.setenv("CAURA_ALLOW_INSECURE_HTTP", "true")
    with pytest.raises(ValueError):
        _client("http://caura.example", allow_insecure_http=False)


def test_the_scheduled_interviewer_keeps_the_opt_in():
    """cron does not inherit the shell, so the opt-in must reach the env file."""
    out = render_env_file({"CAURA_ALLOW_INSECURE_HTTP": "true"})
    assert "export CAURA_ALLOW_INSECURE_HTTP='true'" in out
