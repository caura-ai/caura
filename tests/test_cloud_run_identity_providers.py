"""Self-hosted embedding and rerank backends on Cloud Run get an identity token.

A TEI or rerank service on Cloud Run without public invocation needs each
caller to carry an ID token whose audience is the service's URL. The
operator lists those services' origins in ``CLOUD_RUN_ID_TOKEN_ORIGINS``; a
request to a listed origin carries a token from the metadata server in
``X-Serverless-Authorization``, and every other request is unchanged. The
``Authorization`` header stays the backend's own (the OpenAI-style key, or
the reranker's bearer).

The tests run each provider against a mock transport with google.auth's
fetch stubbed, so they need neither a metadata server nor a backend.
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Any

import httpx
import httpx2
import pytest
from google.oauth2 import id_token as google_id_token

from common.embedding.providers.openai import OpenAIEmbeddingProvider
from common.ranking.protocols import RankCandidate
from common.ranking.providers.remote import RemoteRanker

pytestmark = pytest.mark.unit

TEI = "https://prod-tei-abc123-uc.a.run.app"
RERANK = "https://staging-rerank-abc123-uc.a.run.app"
HEADER = "X-Serverless-Authorization"


@pytest.fixture(autouse=True)
def _clear_token_cache() -> None:
    try:
        identity = importlib.import_module("common.cloud_run_identity")
    except ModuleNotFoundError:
        return
    identity._cache.clear()
    identity._failure_cache.clear()


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    audiences: list[str] = []

    def fake_fetch(_request: object, audience: str) -> str:
        audiences.append(audience)
        return f"token-for-{audience}"

    monkeypatch.setattr(google_id_token, "fetch_id_token", fake_fetch)
    return audiences


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Route both providers' HTTP clients to one recording handler."""
    seen: dict[str, Any] = {"requests": [], "status": 200}

    def embeddings(request: Any) -> Any:
        seen["requests"].append(request)
        body = {
            "object": "list",
            "model": "BAAI/bge-m3",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.0] * 1024}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
        return httpx2.Response(seen["status"], json=body)

    def rerank(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        return httpx.Response(seen["status"], json=[{"index": 0, "score": 0.5}])

    # Subclasses rather than factories: the OpenAI SDK checks that its
    # http_client is an AsyncClient.
    class Httpx2Client(httpx2.AsyncClient):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx2.MockTransport(embeddings)
            super().__init__(*args, **kwargs)

    class HttpxClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(rerank)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", Httpx2Client)
    monkeypatch.setattr(httpx, "AsyncClient", HttpxClient)
    return seen


def _embedder(base_url: str = f"{TEI}/v1") -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(
        api_key="tei-no-auth",
        model="BAAI/bge-m3",
        base_url=base_url,
        send_dimensions=False,
    )


async def _embed(provider: OpenAIEmbeddingProvider) -> None:
    # A refused call still went out; the tests look at what it carried.
    with contextlib.suppress(Exception):
        await provider.embed("hello")


async def _rank(ranker: RemoteRanker) -> None:
    with contextlib.suppress(Exception):
        await ranker.rank("q", [RankCandidate(id="1", content="a", similarity=0.5)])


async def test_embedder_sends_a_token_to_a_listed_origin(
    backend: dict[str, Any], fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOUD_RUN_ID_TOKEN_ORIGINS", TEI)

    await _embed(_embedder())

    request = backend["requests"][0]
    assert request.headers.get(HEADER) == f"Bearer token-for-{TEI}"
    # The backend's own credential is untouched.
    assert request.headers.get("Authorization") == "Bearer tei-no-auth"
    # The audience is the origin: base_url's /v1 path is not part of it.
    assert fetches == [TEI]


async def test_reranker_sends_a_token_to_a_listed_origin(
    backend: dict[str, Any], fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOUD_RUN_ID_TOKEN_ORIGINS", f"{TEI}, {RERANK}")

    await _rank(RemoteRanker(base_url=RERANK, api_key="rerank-key"))

    request = backend["requests"][0]
    assert request.headers.get(HEADER) == f"Bearer token-for-{RERANK}"
    assert request.headers.get("Authorization") == "Bearer rerank-key"
    assert fetches == [RERANK]


async def test_listed_origins_are_normalised(
    backend: dict[str, Any], fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # An operator may paste the service URL with a trailing slash or a path.
    monkeypatch.setenv("CLOUD_RUN_ID_TOKEN_ORIGINS", f"{TEI}/v1/")

    await _embed(_embedder())

    assert backend["requests"][0].headers.get(HEADER) == f"Bearer token-for-{TEI}"
    assert fetches == [TEI]


async def test_unlisted_origin_gets_no_token(
    backend: dict[str, Any], fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOUD_RUN_ID_TOKEN_ORIGINS", RERANK)

    await _embed(_embedder())

    assert HEADER not in backend["requests"][0].headers
    assert fetches == []


async def test_no_list_means_no_token(
    backend: dict[str, Any], fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLOUD_RUN_ID_TOKEN_ORIGINS", raising=False)

    await _embed(_embedder())
    await _rank(RemoteRanker(base_url=RERANK))

    assert all(HEADER not in r.headers for r in backend["requests"])
    assert fetches == []


async def test_token_is_cached_and_a_401_drops_it(
    backend: dict[str, Any], fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOUD_RUN_ID_TOKEN_ORIGINS", RERANK)
    ranker = RemoteRanker(base_url=RERANK)

    await _rank(ranker)
    await _rank(ranker)
    assert fetches == [RERANK]

    backend["status"] = 401
    await _rank(ranker)
    backend["status"] = 200
    await _rank(ranker)

    # The token Cloud Run refused is fetched anew for the next call.
    assert fetches == [RERANK, RERANK]


async def test_failed_fetch_sends_the_call_without_a_token(
    backend: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOUD_RUN_ID_TOKEN_ORIGINS", RERANK)
    attempts: list[str] = []

    def failing_fetch(_request: object, audience: str) -> str:
        attempts.append(audience)
        raise RuntimeError("metadata server unreachable")

    monkeypatch.setattr(google_id_token, "fetch_id_token", failing_fetch)
    ranker = RemoteRanker(base_url=RERANK)

    await _rank(ranker)
    await _rank(ranker)

    # Held briefly rather than retried on every call.
    assert attempts == [RERANK]
    assert len(backend["requests"]) == 2
    assert all(HEADER not in r.headers for r in backend["requests"])


async def test_missing_google_auth_is_named_in_an_error(
    backend: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("CLOUD_RUN_ID_TOKEN_ORIGINS", RERANK)
    identity = importlib.import_module("common.cloud_run_identity")

    def no_google_auth(_audience: str) -> str:
        raise ImportError("No module named 'google'")

    monkeypatch.setattr(identity, "_fetch_blocking", no_google_auth)

    with caplog.at_level("ERROR", logger="common.cloud_run_identity"):
        await _rank(RemoteRanker(base_url=RERANK))

    assert HEADER not in backend["requests"][0].headers
    assert any(
        "google-auth is not installed" in r.getMessage() and r.levelname == "ERROR"
        for r in caplog.records
    )
