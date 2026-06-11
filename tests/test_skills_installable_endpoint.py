"""Tests for ``POST /api/v1/skills/installable``.

The agent-harness install surface the OpenClaw plugin reconciler pulls
from. It applies the SAME active-only + opt-in gate the MCP pull surface
applies (PR #315), server-side, so push (reconciler → disk) and pull
(``memclaw_doc``) agree on what an agent may see.

Asserted contract:
  - opted-in tenant → storage query forced to ``where={"status":"active"}``;
  - non-opted-in tenant → ``where={}`` (byte-identical legacy reconcile,
    preserving the merge-day no-op invariant);
  - settings-lookup failure → 503 (fail closed), storage never queried;
  - the caller cannot widen the filter (the request model has no ``where``).
"""

from __future__ import annotations

import pytest

from tests.conftest import get_test_auth


def _areturn(value):
    async def _fn(*_args, **_kwargs):
        return value

    return _fn


class _CapturingStorage:
    """Fake storage client recording the params the route passes."""

    def __init__(self, captured: dict, rows: list | None = None):
        self._captured = captured
        self._rows = rows or []

    async def query_documents(self, params):
        self._captured.update(params)
        return self._rows


async def test_installable_forces_active_only_when_opted_in(client, monkeypatch):
    tenant_id, headers = get_test_auth()
    monkeypatch.setattr(
        "core_api.routes.documents.get_raw_settings",
        _areturn({"skills_factory": {"enabled": True}}),
    )
    captured: dict = {}
    monkeypatch.setattr(
        "core_api.routes.documents.get_storage_client",
        lambda: _CapturingStorage(captured),
    )
    resp = await client.post(
        "/api/v1/skills/installable",
        json={"tenant_id": tenant_id, "fleet_id": "fleet-a"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert captured["where"] == {"status": "active"}
    assert captured["collection"] == "skills"
    assert captured["fleet_id"] == "fleet-a"


async def test_installable_no_status_filter_when_not_opted_in(client, monkeypatch):
    # Non-opted-in tenant: byte-identical to the legacy reconcile
    # (where={}), so a legacy skill that predates Skill Factory and lacks
    # a status field is NOT silently dropped.
    tenant_id, headers = get_test_auth()
    monkeypatch.setattr(
        "core_api.routes.documents.get_raw_settings",
        _areturn({}),  # no skills_factory block → not opted in
    )
    captured: dict = {}
    monkeypatch.setattr(
        "core_api.routes.documents.get_storage_client",
        lambda: _CapturingStorage(captured),
    )
    resp = await client.post(
        "/api/v1/skills/installable",
        json={"tenant_id": tenant_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert captured["where"] == {}


async def test_installable_fails_closed_on_settings_error(client, monkeypatch):
    # A settings-lookup failure must 503 (fail closed) and never reach
    # storage — so the reconciler (fail-safe on non-2xx) adds nothing and
    # can't push a non-active skill to disk during an outage.
    tenant_id, headers = get_test_auth()

    async def _boom(*_a, **_k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr("core_api.routes.documents.get_raw_settings", _boom)
    queried = {"called": False}

    class _NeverStorage:
        async def query_documents(self, params):  # noqa: ARG002
            queried["called"] = True
            return []

    monkeypatch.setattr(
        "core_api.routes.documents.get_storage_client", lambda: _NeverStorage()
    )
    resp = await client.post(
        "/api/v1/skills/installable",
        json={"tenant_id": tenant_id},
        headers=headers,
    )
    assert resp.status_code == 503, resp.text
    assert queried["called"] is False


async def test_installable_rejects_caller_where(client, monkeypatch):
    # Defense in depth: the request model has no ``where`` field, so a
    # caller-supplied one is ignored — it can never widen the filter to
    # pull non-active skills. (Pydantic ignores the unknown field; the
    # server still forces its own ``where``.)
    tenant_id, headers = get_test_auth()
    monkeypatch.setattr(
        "core_api.routes.documents.get_raw_settings",
        _areturn({"skills_factory": {"enabled": True}}),
    )
    captured: dict = {}
    monkeypatch.setattr(
        "core_api.routes.documents.get_storage_client",
        lambda: _CapturingStorage(captured),
    )
    resp = await client.post(
        "/api/v1/skills/installable",
        json={"tenant_id": tenant_id, "where": {"status": "staged"}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    # Server-decided filter wins; the caller's "staged" is discarded.
    assert captured["where"] == {"status": "active"}
