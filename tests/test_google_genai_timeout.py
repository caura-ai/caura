"""The Vertex and Gemini providers bound every HTTP request.

google-genai has no default timeout, and the providers call it through
``asyncio.to_thread``. Without a bound, one stalled upstream call held a
worker's enrich loop and a core-api executor thread indefinitely.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

pytest.importorskip("google.genai")

from google import genai
from google.genai import types

from common.llm.constants import GOOGLE_GENAI_REQUEST_TIMEOUT_SECONDS

pytestmark = pytest.mark.unit

EXPECTED_MS = int(GOOGLE_GENAI_REQUEST_TIMEOUT_SECONDS * 1000)


def _timeout_of(client) -> object:
    return client._api_client._http_options.timeout


def test_gemini_client_has_a_request_timeout():
    from common.llm.providers.gemini import GeminiLLMProvider

    provider = GeminiLLMProvider(api_key="test-key", model="gemini-test")
    assert _timeout_of(provider._client) == EXPECTED_MS


@pytest.mark.parametrize("location", ["us-central1", "global"])
def test_vertex_client_has_a_request_timeout(location, monkeypatch):
    from common.llm.providers import vertex

    monkeypatch.setattr(genai, "Client", _CapturingClient)
    provider = vertex.VertexLLMProvider(
        project_id="p", location=location, model="gemini-test"
    )
    client = provider._get_client()
    options = client.kwargs["http_options"]
    assert options.timeout == EXPECTED_MS
    if location in vertex._MULTI_REGION_LOCATIONS:
        assert (
            options.base_url == vertex._BARE_VERTEX_BASE_URL
        )  # still routed to the bare host


class _CapturingClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_the_sdk_honours_the_timeout_against_a_silent_server():
    """A server that accepts and never answers must not hang the call."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    held: list[socket.socket] = []

    def _accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)

    threading.Thread(target=_accept, daemon=True).start()
    client = genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(
            timeout=1000, base_url=f"http://127.0.0.1:{port}"
        ),
    )
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        client.models.generate_content(model="gemini-test", contents="hi")
    elapsed = time.monotonic() - started
    srv.close()
    for c in held:
        c.close()
    assert elapsed < 10, f"call took {elapsed:.1f}s with a 1s timeout"
