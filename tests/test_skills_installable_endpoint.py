"""Tests for ``POST /api/v1/skills/installable``.

The agent-harness install surface the OpenClaw plugin reconciler pulls
from. It applies the SAME active-only + opt-in gate the MCP pull surface
applies (PR #315), server-side, so push (reconciler → disk) and pull
(``caura_doc``) agree on what an agent may see.

Asserted contract:
  - every tenant, opted in to the Skill Factory or not → storage query forced
    to ``where={"status":"active"}``: a skill reaches a node's disk only once
    approved;
  - the caller cannot widen the filter (the request model has no ``where``).
"""

from __future__ import annotations

from tests.conftest import get_test_auth


class _CapturingStorage:
    """Fake storage client recording the params the route passes."""

    def __init__(self, captured: dict, rows: list | None = None):
        self._captured = captured
        self._rows = rows or []

    async def query_documents(self, params):
        self._captured.update(params)
        return self._rows


async def test_installable_forces_active_only_whatever_the_flag_says(
    client, monkeypatch
):
    # The Skill Factory flag defaults to off, and the endpoint no longer reads
    # it: a skill an agent wrote is unreviewed instructions until approved,
    # for every tenant. Legacy rows were backfilled ``active`` (migration 070)
    # and stay installed.
    tenant_id, headers = get_test_auth()
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


async def test_installable_rejects_caller_where(client, monkeypatch):
    # Defense in depth: the request model has no ``where`` field, so a
    # caller-supplied one is ignored — it can never widen the filter to
    # pull non-active skills. (Pydantic ignores the unknown field; the
    # server still forces its own ``where``.)
    tenant_id, headers = get_test_auth()
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
