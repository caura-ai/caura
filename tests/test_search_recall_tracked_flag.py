"""``/search`` reports whether it bumped ``recall_count`` (``recall_tracked``).

TrackRecalls deliberately skips the ``recall_count`` bump for a recall that
carries no caller agent identity (gap A26, pinned by
``test_track_recalls_agentless.py``). That skip is correct, but it was also
completely invisible: a tenant-scoped key — which has ``auth.agent_id = None``
and so presents no identity unless it also sets ``filter_agent_id``, which
would additionally narrow results to that agent's own rows — saw
``recall_count`` pinned at 0 forever and ``recall_boost`` never engage, with
nothing in the response saying so. The only way to discover it was to watch a
counter never move.

``recall_tracked`` reports what the search actually did. The value comes from
TrackRecalls itself rather than being re-derived at the route, because the
route cannot see two of the three reasons a bump is skipped (an empty result
set, and the legacy search path's unconditional bump).
"""

from __future__ import annotations

import uuid

import pytest

from core_api import mcp_server
from core_api.services import memory_service
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_test_auth


@pytest.fixture
def pipeline_search(monkeypatch):
    """Pin the production (pipeline) search path for the REST cases below.

    Not defensive boilerplate: ``tests/pipeline/test_search_pipeline.py`` sets
    ``memory_service._USE_PIPELINE_SEARCH = False`` without restoring it, so
    every test that runs after it in the same session silently exercises the
    deprecated legacy path — which bumps ``recall_count`` unconditionally and
    would therefore report ``recall_tracked: true`` for the agentless case
    these tests exist to pin. Pinning the flag here makes each case assert the
    behaviour of a named path instead of whatever ran last.
    """
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)


def _tenant() -> str:
    return f"test-tenant-recall-tracked-{uuid.uuid4().hex[:8]}"


async def _write(client, headers, tenant_id, agent_id, content):
    resp = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={"tenant_id": tenant_id, "agent_id": agent_id, "content": content},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.integration
async def test_agentless_search_reports_recall_not_tracked(client, pipeline_search):
    """The finding: a tenant-scoped caller gets no bump, and now hears about it.

    The admin/tenant key used by the test auth helper has no ``agent_id``, so
    this is the exact shape of the integration that sat on a permanently
    zero ``recall_count``.
    """
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    content = f"Deploys are gated on a green staging smoke {uuid.uuid4().hex}"
    await _write(client, headers, tenant_id, "deploy-bot", content)

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={"tenant_id": tenant_id, "query": content, "top_k": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"], "the search must return the row it is asked about"
    assert body["recall_tracked"] is False


@pytest.mark.integration
async def test_agent_scoped_search_reports_recall_tracked(client, pipeline_search):
    """The contrast case: a caller identity is present, so the bump is dispatched."""
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    content = f"Rollbacks are one command and never a redeploy {uuid.uuid4().hex}"
    await _write(client, headers, tenant_id, agent_id, content)

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": content,
            "top_k": 5,
            "filter_agent_id": agent_id,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"], "the search must return the row it is asked about"
    assert body["recall_tracked"] is True


@pytest.mark.integration
async def test_diagnostic_search_reports_recall_not_tracked(client, pipeline_search):
    """D12 — a diagnostic call is inspection, not use, so it must not reinforce.

    Reported honestly even though a caller identity IS present, which is the
    case a predicate re-derived from ``filter_agent_id`` alone would get wrong.
    """
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    content = f"The oncall rotation hands over on Tuesdays {uuid.uuid4().hex}"
    await _write(client, headers, tenant_id, agent_id, content)

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": content,
            "top_k": 5,
            "filter_agent_id": agent_id,
            "diagnostic": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"], "the search must return the row it is asked about"
    assert body["recall_tracked"] is False


@pytest.mark.integration
async def test_empty_result_set_reports_recall_not_tracked(client, pipeline_search):
    """No rows means nothing was reinforced, identity or not.

    The route cannot derive this from the request, which is why the value is
    reported by TrackRecalls rather than recomputed from the caller's inputs.
    """
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": f"nothing in this empty tenant matches {uuid.uuid4().hex}",
            "top_k": 5,
            "filter_agent_id": agent_id,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["recall_tracked"] is False


@pytest.mark.integration
async def test_legacy_path_reports_its_own_unconditional_bump(client, monkeypatch):
    """The deprecated legacy path bumps with no caller-agent gate, and says so.

    ``_search_memories_legacy`` calls ``increment_recall`` for every row it
    returns, with none of TrackRecalls' gates. Reporting the pipeline's policy
    for it would be a lie to whoever flips ``_USE_PIPELINE_SEARCH`` back during
    a hotfix — the same divergence trap this file's BlankQuery handler already
    warns about. So the agentless search that reports ``false`` above reports
    ``true`` here, because that is what this path actually does.
    """
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", False)
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    content = f"Staging mirrors prod except for the mail sink {uuid.uuid4().hex}"
    await _write(client, headers, tenant_id, "deploy-bot", content)

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={"tenant_id": tenant_id, "query": content, "top_k": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"], "the search must return the row it is asked about"
    assert body["recall_tracked"] is True


# ---------------------------------------------------------------------------
# MCP parity — ``caura_recall`` reports the same field
#
# Emitted on both surfaces so that adding it does not itself open a new
# REST/MCP divergence, which is the failure mode the parity smoke exists to
# catch. These pin that MCP forwards what the pipeline reported rather than
# hardcoding a value: the same handler returns True and False depending only
# on what ``search_memories`` wrote into ``recall_ctx``.
# ---------------------------------------------------------------------------


class _MemoryStub:
    def __init__(self, mid: str):
        self.mid = mid

    def model_dump(self, mode: str = "python"):
        return {"id": self.mid}


class _FakeConfig:
    recall_boost = False
    graph_expand = False
    entity_retrieval = True


async def _fake_resolve_config(tenant_id):
    return _FakeConfig()


def _wire(monkeypatch):
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    return stub_storage_client(monkeypatch, get_agent=None)


def _search_writing(tracked: bool):
    """Stand in for ``search_memories``, filling ``recall_ctx`` as the pipeline does."""

    async def _search(*args, recall_ctx=None, **kwargs):
        if recall_ctx is not None:
            recall_ctx["recall_tracked"] = tracked
        return [_MemoryStub("m-1")]

    return _search


@pytest.mark.unit
async def test_mcp_recall_forwards_recall_tracked_true(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").side_effect = _search_writing(True)
    _wire(monkeypatch)

    payload = parse_envelope(await mcp_server.caura_recall(query="anything"))
    assert payload["recall_tracked"] is True


@pytest.mark.unit
async def test_mcp_recall_forwards_recall_tracked_false(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").side_effect = _search_writing(False)
    _wire(monkeypatch)

    payload = parse_envelope(await mcp_server.caura_recall(query="anything"))
    assert payload["recall_tracked"] is False
