"""Smoke tests for core-operations cron-tick fanout (CAURA-655)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from core_operations import tasks
from core_operations.config import settings


class _StubResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | str) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else ""

    def json(self) -> dict[str, Any]:
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _StubAsyncClient:
    """Minimal httpx.AsyncClient drop-in. ``post`` returns the
    response queued at construction; ``raise_on_post`` simulates a
    network error.
    """

    def __init__(
        self,
        *,
        response: _StubResponse | None = None,
        raise_on_post: Exception | None = None,
    ) -> None:
        self._response = response
        self._raise = raise_on_post
        self.calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, headers: dict | None = None) -> _StubResponse:
        self.calls.append((url, headers))
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response

    async def get(self, url: str, *, headers: dict | None = None) -> _StubResponse:
        # Same contract as ``post`` — read-only ticks (embedding-coverage) GET.
        # ``raise_on_post`` covers both verbs; the field name predates the first
        # GET caller and renaming it would churn every existing test.
        self.calls.append((url, headers))
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


@asynccontextmanager
async def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _StubResponse | None = None,
    raise_on_post: Exception | None = None,
) -> AsyncIterator[_StubAsyncClient]:
    stub = _StubAsyncClient(response=response, raise_on_post=raise_on_post)
    monkeypatch.setattr(
        tasks.httpx,
        "AsyncClient",
        lambda *a, **kw: stub,
    )
    yield stub


@pytest.mark.asyncio
async def test_archive_expired_tick_posts_to_fanout(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "archive-expired", "published": 3, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_archive_expired_tick()

    assert len(stub.calls) == 1
    url, headers = stub.calls[0]
    assert url == "http://core-api/api/v1/admin/lifecycle/fanout/archive-expired"
    assert headers == {"X-API-Key": "admin-key-xyz"}


@pytest.mark.asyncio
async def test_agent_digest_tick_posts_to_run_endpoint(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"period": "day", "orgs": 2, "completed": 2, "digests": 5})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_agent_digest_tick()

    assert len(stub.calls) == 1
    url, headers = stub.calls[0]
    assert url == "http://core-api/api/v1/admin/reports/agent-digest/run?period=day"
    assert headers == {"X-API-Key": "admin-key-xyz"}


@pytest.mark.asyncio
async def test_agent_digest_weekly_tick_posts_period_week(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"period": "week", "orgs": 1, "completed": 1, "digests": 3})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_agent_digest_weekly_tick()

    assert len(stub.calls) == 1
    url, _ = stub.calls[0]
    assert url == "http://core-api/api/v1/admin/reports/agent-digest/run?period=week"


@pytest.mark.asyncio
async def test_archive_stale_tick_hits_correct_path(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "archive-stale", "published": 0, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_archive_stale_tick()

    assert stub.calls[0][0].endswith("/admin/lifecycle/fanout/archive-stale")


@pytest.mark.asyncio
async def test_purge_soft_deleted_tick_hits_correct_path(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "purge-soft-deleted", "published": 2, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_purge_soft_deleted_tick()

    assert stub.calls[0][0].endswith("/admin/lifecycle/fanout/purge-soft-deleted")


@pytest.mark.asyncio
async def test_tick_swallows_non_2xx(monkeypatch: pytest.MonkeyPatch):
    """A non-2xx response must not raise — the scheduler retries on the
    next tick anyway, and re-raising would just produce duplicate
    stack traces in the on-call channel without changing behaviour.
    """
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(503, "upstream timeout")
    async with _patch_client(monkeypatch, response=response):
        await tasks.run_archive_expired_tick()  # no raise


@pytest.mark.asyncio
async def test_tick_swallows_network_error(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    async with _patch_client(monkeypatch, raise_on_post=httpx.ConnectError("offline")):
        await tasks.run_archive_expired_tick()  # no raise


@pytest.mark.asyncio
async def test_crystallize_tick_hits_correct_path(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "crystallize", "published": 1, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_crystallize_tick()

    assert stub.calls[0][0].endswith("/admin/lifecycle/fanout/crystallize")


@pytest.mark.asyncio
async def test_entity_link_tick_hits_correct_path(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "entity-link", "published": 2, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_entity_link_tick()

    assert stub.calls[0][0].endswith("/admin/lifecycle/fanout/entity-link")


@pytest.mark.asyncio
async def test_insights_tick_hits_correct_path(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "insights", "published": 1, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_insights_tick()

    assert stub.calls[0][0].endswith("/admin/lifecycle/fanout/insights")


@pytest.mark.asyncio
async def test_embed_backfill_tick_posts_to_fanout(monkeypatch: pytest.MonkeyPatch):
    """The sweep goes through the same per-org fanout as every other tick.

    One message per org, not one giant call: the storage endpoint behind the
    sweep refuses un-scoped requests, and per-org phasing bounds blast radius.
    """
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "embed-backfill", "published": 7, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_embed_backfill_tick()

    assert len(stub.calls) == 1
    url, headers = stub.calls[0]
    assert url == "http://core-api/api/v1/admin/lifecycle/fanout/embed-backfill"
    assert headers == {"X-API-Key": "admin-key-xyz"}


# ---------------------------------------------------------------------------
# embedding-coverage tick — the log lines ARE the metric, so assert on them
# ---------------------------------------------------------------------------


def _coverage_body(tenants: list[dict]) -> dict:
    return {
        "tenants": tenants,
        "total_active": sum(t["total_active"] for t in tenants),
        "missing_embeddings": sum(t["missing_embeddings"] for t in tenants),
        "tenants_with_missing": sum(1 for t in tenants if t["missing_embeddings"]),
    }


def _records(caplog, message: str) -> list:
    return [r for r in caplog.records if r.getMessage() == message]


@pytest.mark.asyncio
async def test_embedding_coverage_tick_gets_admin_route_and_logs(monkeypatch, caplog):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    body = _coverage_body(
        [
            {"tenant_id": "t-bad", "total_active": 100, "missing_embeddings": 40, "coverage_pct": 60.0},
            {"tenant_id": "t-ok", "total_active": 50, "missing_embeddings": 0, "coverage_pct": 100.0},
        ]
    )
    caplog.set_level("INFO", logger=tasks.logger.name)
    async with _patch_client(monkeypatch, response=_StubResponse(200, body)) as stub:
        await tasks.run_embedding_coverage_tick()

    assert len(stub.calls) == 1
    url, headers = stub.calls[0]
    assert url == "http://core-api/api/v1/admin/lifecycle/embedding-coverage"
    assert headers == {"X-API-Key": "admin-key-xyz"}

    total = _records(caplog, "embedding coverage total")
    assert len(total) == 1
    assert total[0].total_active == 150
    assert total[0].missing_embeddings == 40
    assert total[0].tenants_with_missing == 1

    # Only the tenant that actually has a gap is worth a line.
    per_tenant = _records(caplog, "embedding coverage tenant")
    assert [r.tenant_id for r in per_tenant] == ["t-bad"]
    assert per_tenant[0].missing_embeddings == 40


@pytest.mark.asyncio
async def test_embedding_coverage_tick_caps_per_tenant_lines_and_says_so(monkeypatch, caplog):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    over = tasks._COVERAGE_TENANT_LOG_CAP + 5
    body = _coverage_body(
        [
            {
                "tenant_id": f"t-{i}",
                "total_active": 10,
                "missing_embeddings": 1,
                "coverage_pct": 90.0,
            }
            for i in range(over)
        ]
    )
    caplog.set_level("INFO", logger=tasks.logger.name)
    async with _patch_client(monkeypatch, response=_StubResponse(200, body)):
        await tasks.run_embedding_coverage_tick()

    per_tenant = _records(caplog, "embedding coverage tenant")
    assert len(per_tenant) == tasks._COVERAGE_TENANT_LOG_CAP

    # The cap must announce itself — a silent truncation would read as
    # "only these tenants have gaps".
    truncated = _records(caplog, "embedding coverage per-tenant lines truncated")
    assert len(truncated) == 1
    assert truncated[0].reported == tasks._COVERAGE_TENANT_LOG_CAP
    # ``tenants_affected`` counts BOTH defects — a tenant whose rows are all
    # embedded but stale is just as worth a line as one missing embeddings.
    assert truncated[0].tenants_affected == over

    # The deployment-wide total is never truncated.
    assert _records(caplog, "embedding coverage total")[0].missing_embeddings == over


@pytest.mark.asyncio
async def test_embedding_coverage_tick_swallows_non_2xx(monkeypatch, caplog):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    caplog.set_level("INFO", logger=tasks.logger.name)
    async with _patch_client(monkeypatch, response=_StubResponse(503, "upstream down")):
        await tasks.run_embedding_coverage_tick()

    assert _records(caplog, "embedding coverage total") == []
    assert len(_records(caplog, "embedding-coverage returned non-2xx; will retry next tick")) == 1


@pytest.mark.asyncio
async def test_embedding_coverage_tick_swallows_network_error(monkeypatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    async with _patch_client(monkeypatch, raise_on_post=httpx.ConnectError("offline")):
        await tasks.run_embedding_coverage_tick()  # must not raise


@pytest.mark.asyncio
async def test_embedding_coverage_tick_swallows_non_json(monkeypatch, caplog):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    caplog.set_level("INFO", logger=tasks.logger.name)
    async with _patch_client(monkeypatch, response=_StubResponse(200, "<html>nope</html>")):
        await tasks.run_embedding_coverage_tick()

    assert _records(caplog, "embedding coverage total") == []
    assert len(_records(caplog, "embedding-coverage returned non-JSON; will retry next tick")) == 1


@pytest.mark.asyncio
async def test_embedding_coverage_tick_reports_stale_separately(monkeypatch, caplog):
    """Stale must not be folded into missing.

    The nightly sweep repairs ``missing`` and CANNOT repair ``stale`` (the
    column is non-NULL, so the sweep never sees the row). Collapsing them
    would make a rising stale count look like something already being fixed.
    """
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    body = {
        "tenants": [
            {
                "tenant_id": "t-stale",
                "total_active": 100,
                "missing_embeddings": 0,
                "stale_embeddings": 7,
                "unknown_provenance": 3,
                "coverage_pct": 100.0,
            }
        ],
        "total_active": 100,
        "missing_embeddings": 0,
        "tenants_with_missing": 0,
        "stale_embeddings": 7,
        "tenants_with_stale": 1,
        "unknown_provenance": 3,
    }
    caplog.set_level("INFO", logger=tasks.logger.name)
    async with _patch_client(monkeypatch, response=_StubResponse(200, body)):
        await tasks.run_embedding_coverage_tick()

    total = _records(caplog, "embedding coverage total")[0]
    assert total.missing_embeddings == 0
    assert total.stale_embeddings == 7
    assert total.unknown_provenance == 3

    # The tenant has ZERO missing but 7 stale — it must still get a line.
    # Filtering on missing alone would hide exactly the case this release
    # exists to surface.
    per_tenant = _records(caplog, "embedding coverage tenant")
    assert [r.tenant_id for r in per_tenant] == ["t-stale"]
    assert per_tenant[0].stale_embeddings == 7
