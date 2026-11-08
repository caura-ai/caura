"""core-operations presents a Cloud Run ID token on every core-api call.

When core-api runs on Cloud Run without public invocation, a caller must
carry an ID token whose audience is core-api's URL. core-operations sends
it as ``X-Serverless-Authorization``, so the ``X-API-Key`` admin credential
and core-api's own ``Authorization`` handling are left untouched.

The tests drive each tick against an ``httpx.MockTransport`` and stub
google.auth's token fetch, so they need neither a metadata server nor a
running core-api.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from google.oauth2 import id_token as google_id_token

from core_operations import lease, tasks
from core_operations.config import settings

CLOUD_RUN_URL = "https://core-api-abc123-uc.a.run.app"


def _ticks() -> list[tuple[str, Callable[[], Awaitable[Any]]]]:
    """Every code path that POSTs to core-api, one tick per path."""
    return [
        ("fanout", tasks.run_archive_expired_tick),
        ("reconcile", tasks.run_lifecycle_reconcile_tick),
        ("rerun sweep", tasks.run_enrichment_rerun_tick),
        ("embedding coverage", tasks.run_embedding_coverage_tick),
        ("agent digest", tasks.run_agent_digest_tick),
        ("interviewer schedule", tasks.run_interviewer_schedule_tick),
        ("tick lease", lambda: lease.claim_tick_lease("archive_expired", 60.0)),
    ]


@pytest.fixture(autouse=True)
def _clear_token_cache() -> None:
    try:
        identity = importlib.import_module("core_operations.identity_token")
    except ModuleNotFoundError:
        return
    identity._cache.clear()
    identity._failure_cache.clear()


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub google.auth's fetch; record each audience it was asked for."""
    audiences: list[str] = []

    def fake_fetch(_request: object, audience: str) -> str:
        audiences.append(audience)
        return f"token-for-{audience}"

    monkeypatch.setattr(google_id_token, "fetch_id_token", fake_fetch)
    return audiences


@pytest.fixture
def core_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Route every httpx.AsyncClient the ticks open to a mock core-api."""
    seen: dict[str, Any] = {"requests": [], "status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        return httpx.Response(seen["status"], json={})

    real_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(settings, "core_api_url", CLOUD_RUN_URL)
    monkeypatch.setattr(settings, "core_api_admin_api_key", "admin-key")
    return seen


@pytest.mark.parametrize(("name", "tick"), _ticks(), ids=[n for n, _ in _ticks()])
async def test_every_core_api_call_carries_an_id_token(
    name: str,
    tick: Callable[[], Awaitable[Any]],
    core_api: dict[str, Any],
    fetches: list[str],
) -> None:
    await tick()

    assert core_api["requests"], f"{name} made no core-api request"
    for request in core_api["requests"]:
        assert request.headers.get("X-Serverless-Authorization") == f"Bearer token-for-{CLOUD_RUN_URL}"
        # The admin credential still travels as before, and the ID token
        # leaves core-api's own Authorization header alone.
        assert request.headers.get("X-API-Key") == "admin-key"
        assert "Authorization" not in request.headers
    assert set(fetches) == {CLOUD_RUN_URL}


async def test_token_is_cached_across_ticks(core_api: dict[str, Any], fetches: list[str]) -> None:
    await tasks.run_archive_expired_tick()
    await tasks.run_archive_stale_tick()

    assert len(core_api["requests"]) == 2
    assert fetches == [CLOUD_RUN_URL]


async def test_a_401_drops_the_cached_token(core_api: dict[str, Any], fetches: list[str]) -> None:
    core_api["status"] = 401
    await tasks.run_archive_expired_tick()
    core_api["status"] = 200
    await tasks.run_archive_expired_tick()

    # The token Cloud Run refused is not reused: the second tick fetches anew.
    assert fetches == [CLOUD_RUN_URL, CLOUD_RUN_URL]
    assert all(r.headers.get("X-Serverless-Authorization") for r in core_api["requests"])


async def test_audience_is_the_origin_of_core_api_url(
    core_api: dict[str, Any], fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cloud Run matches the token's audience against the service URL, which
    # has no path; a CORE_API_URL with a prefix or trailing slash must not
    # leak into the audience.
    monkeypatch.setattr(settings, "core_api_url", f"{CLOUD_RUN_URL}/prefix/")

    await tasks.run_archive_expired_tick()

    assert fetches == [CLOUD_RUN_URL]
    assert core_api["requests"][0].url.path.startswith("/prefix/api/v1/")


async def test_plain_http_core_api_gets_no_token(
    core_api: dict[str, Any], fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A compose or on-prem core-api on the internal network: there is no
    # Cloud Run front end to satisfy and no metadata server to ask.
    monkeypatch.setattr(settings, "core_api_url", "http://oss-core-api:8000")

    await tasks.run_archive_expired_tick()

    assert fetches == []
    assert "X-Serverless-Authorization" not in core_api["requests"][0].headers


async def test_failed_fetch_sends_the_call_without_a_token(
    core_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def failing_fetch(_request: object, audience: str) -> str:
        attempts.append(audience)
        raise RuntimeError("metadata server unreachable")

    monkeypatch.setattr(google_id_token, "fetch_id_token", failing_fetch)

    await tasks.run_archive_expired_tick()
    await tasks.run_archive_stale_tick()

    # The tick still runs (core-api answers 401 or 403 and the tick logs it),
    # and the failure is held briefly rather than retried on every call.
    assert len(core_api["requests"]) == 2
    assert attempts == [CLOUD_RUN_URL]
    assert all("X-Serverless-Authorization" not in r.headers for r in core_api["requests"])
