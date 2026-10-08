"""MCP tools answer as their REST twins do (the audit's MCP/REST parity batch).

Each test names the finding it closes. The handlers run against the shared
``mcp_env`` stubs, so these are unit tests of the MCP surface; the REST half of
L-09 sits with the other route-level checks in tests/test_route_authz_gaps.py.
"""

from __future__ import annotations

import itertools
import json
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core_api import _standalone_state, mcp_server
from core_api.config import settings
from core_api.routes import documents as documents_route
from core_api.tools import REGISTRY
from tests._mcp_test_helpers import (
    is_error_envelope,
    parse_envelope,
    stub_storage_client,
)
from tests._scoped_module import scoped
from tests.test_mcp_gateway_secret import _SHARED_KEY_FIELD, _call_middleware

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_mcp_context_vars():
    """The session shares one context: leave the middleware's vars clean."""
    yield
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)
    mcp_server._agent_id_var.set(None)
    mcp_server._via_gateway_var.set(False)
    mcp_server._org_read_only_var.set(False)


class _Out:
    def __init__(self, memory_id: str) -> None:
        self.memory_id = memory_id

    def model_dump(self, mode: str = "python") -> dict:
        return {"id": self.memory_id, "status": "created"}


def _code(out) -> str:
    return parse_envelope(out)["error"]["code"]


# --- L-101: the shared key binds no agent, so the default is refused -------------


async def test_l101_shared_key_without_an_agent_refuses_the_default(monkeypatch):
    """Outside standalone, REST 422s a write that names no agent on the shared
    CAURA_API_KEY path; MCP used to fall back to the shared ``mcp-agent``."""
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)
    headers = [(b"x-api-key", b"sh4red"), (b"x-tenant-id", b"acme")]

    app_called, _ = await _call_middleware(headers)

    assert app_called
    refusal = mcp_server._refuse_default_agent_on_gateway(mcp_server._DEFAULT_AGENT_ID)
    assert refusal is not None
    assert json.loads(refusal)["error"]["code"] == "MISSING_AGENT_ID"
    assert mcp_server._refuse_default_agent_on_gateway("alice") is None


async def test_l101_standalone_keeps_its_single_identity(monkeypatch):
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", True)
    # What init_standalone() sets at startup. Without it this test passed only
    # after another module's fixture had initialised standalone mode.
    monkeypatch.setattr(_standalone_state, "standalone_tenant_id", "default")

    app_called, _ = await _call_middleware([(b"x-api-key", b"sh4red")])

    assert app_called
    default = mcp_server._DEFAULT_AGENT_ID
    assert mcp_server._refuse_default_agent_on_gateway(default) is None


# --- L-102: a recall the fleet gate refuses is not charged -----------------------


async def test_l102_a_refused_recall_is_not_charged(mcp_env, monkeypatch):
    agent = {"fleet_id": "fleet-a", "trust_level": 1}
    monkeypatch.setattr(mcp_server, "lookup_agent", AsyncMock(return_value=agent))
    refused = AsyncMock(side_effect=HTTPException(status_code=403, detail="no"))
    monkeypatch.setattr(mcp_server, "enforce_fleet_read_many", refused)

    out = await mcp_server.caura_recall(query="q", fleet_ids=["fleet-b"])

    assert is_error_envelope(out)
    mcp_server.check_and_increment.assert_not_awaited()


# --- L-103: metadata.idempotency_key replays on MCP as on REST -------------------


async def test_l103_a_keyed_write_replays_on_retry(mcp_env, monkeypatch):
    # The clock moves on further at each reading, so the two calls answer with
    # different ``_latency_ms``, as they can by chance. The replay must match
    # the first answer apart from it.
    readings = itertools.count()
    clock = scoped(time, perf_counter=lambda: next(readings) ** 2 / 1000)
    monkeypatch.setattr(mcp_server, "time", clock)
    sc = stub_storage_client(
        monkeypatch,
        get_idempotency=None,
        claim_idempotency=(True, None),
        upsert_idempotency=None,
    )
    target = "core_api.middleware.idempotency.get_storage_client"
    monkeypatch.setattr(target, lambda: sc)
    create = mcp_env["service"]("create_memory")
    create.return_value = _Out("m-1")
    metadata = {"idempotency_key": "retry-1"}

    first = await mcp_server.caura_write(content="hello", metadata=metadata)
    recorded = sc.upsert_idempotency.await_args.kwargs
    sc.get_idempotency = AsyncMock(
        return_value={
            "request_hash": recorded["request_hash"],
            "is_pending": False,
            "response_body": recorded["response_body"],
            "status_code": recorded["status_code"],
        }
    )
    second = await mcp_server.caura_write(content="hello", metadata=metadata)

    assert create.await_count == 1
    assert json.loads(second)["_latency_ms"] != json.loads(first)["_latency_ms"]
    assert parse_envelope(second) == parse_envelope(first)


# --- L-104: reading a profile needs no write scope; reset clears it --------------


async def test_l104_a_read_only_credential_can_read_its_profile(mcp_env, monkeypatch):
    monkeypatch.setattr(mcp_server, "_get_scopes", lambda: {"read"})
    agent = {"agent_id": "alice", "search_profile": {"top_k": 7}}
    monkeypatch.setattr(mcp_server, "lookup_agent", AsyncMock(return_value=agent))

    out = await mcp_server.caura_tune(agent_id="alice")

    assert parse_envelope(out)["search_profile"] == {"top_k": 7}


async def test_l104_a_read_only_credential_still_cannot_tune(mcp_env, monkeypatch):
    monkeypatch.setattr(mcp_server, "_get_scopes", lambda: {"read"})

    out = await mcp_server.caura_tune(agent_id="alice", top_k=9)

    assert _code(out) == "FORBIDDEN"


async def test_l104_reset_clears_the_profile(mcp_env, monkeypatch):
    sc = stub_storage_client(monkeypatch, reset_search_profile=True)
    monkeypatch.setattr(mcp_server, "log_action", AsyncMock())

    out = await mcp_server.caura_tune(agent_id="alice", reset=True)

    assert parse_envelope(out)["search_profile"] == {}
    sc.reset_search_profile.assert_awaited_once()


# --- L-105: one skill-slug rule on both surfaces ---------------------------------


def test_l105_mcp_and_rest_share_the_skill_slug_rule():
    assert mcp_server._SKILL_SLUG_RE.pattern == documents_route._SKILL_SLUG_RE.pattern
    assert mcp_server._SKILL_SLUG_RE.fullmatch("agent/my-skill")


# --- L-106: every MCP write honours the plan limit -------------------------------


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (
            "caura_doc",
            {"op": "write", "collection": "notes", "doc_id": "a", "data": {"k": 1}},
        ),
        (
            "caura_keystones_set",
            {
                "op": "set",
                "doc_id": "rule-1",
                "title": "t",
                "content": "c",
                "scope": "tenant",
                "weight": "low",
            },
        ),
        ("caura_evolve", {"outcome": "x", "outcome_type": "success"}),
        ("caura_insights", {"focus": "patterns"}),
    ],
)
async def test_l106_an_over_plan_write_is_refused(mcp_env, monkeypatch, tool, kwargs):
    monkeypatch.setattr(settings, "enforce_mcp_plan_limits", True)
    mcp_server._org_read_only_var.set(True)
    stub_storage_client(monkeypatch)

    out = await getattr(mcp_server, tool)(**kwargs)

    assert _code(out) == "PLAN_LIMIT_READ_ONLY"


# --- L-107: the shrink-guard override exists on MCP ------------------------------


async def test_l107_doc_write_forwards_force(mcp_env, monkeypatch):
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 42})

    out = await mcp_server.caura_doc(
        op="write", collection="notes", doc_id="a", data={"k": 1}, force=True
    )

    assert parse_envelope(out)["action"] == "updated"
    assert sc.upsert_document_xmax.await_args.args[0]["force"] is True


# --- L-108 and L-10: doc query emits items and bounds its paging -----------------


async def test_l108_doc_query_emits_items_beside_results(mcp_env, monkeypatch):
    rows = [{"doc_id": "a", "data": {"k": 1}}]
    stub_storage_client(monkeypatch, query_documents=rows)

    payload = parse_envelope(await mcp_server.caura_doc(op="query", collection="notes"))

    assert payload["items"] == payload["results"]


async def test_l10_doc_query_bounds_limit_and_offset(mcp_env, monkeypatch):
    sc = stub_storage_client(monkeypatch, query_documents=[])

    await mcp_server.caura_doc(op="query", collection="notes", limit=0, offset=-5)

    sent = sc.query_documents.await_args.args[0]
    assert (sent["limit"], sent["offset"]) == (1, 0)


# --- L-109: caura_list says when it capped the page ------------------------------


async def test_l109_caura_list_reports_the_limit_it_applied(mcp_env, monkeypatch):
    stub_storage_client(monkeypatch, list_memories_by_filters=[])

    payload = parse_envelope(await mcp_server.caura_list(limit=200))

    assert payload["effective_limit"] == 50


# --- L-110: keystones default to the caller's home fleet -------------------------


async def test_l110_keystones_read_the_callers_home_fleet(mcp_env, monkeypatch):
    sc = stub_storage_client(
        monkeypatch,
        get_agent={"agent_id": "alice", "fleet_id": "fleet-a", "trust_level": 1},
        list_keystones=([], False),
    )

    await mcp_server.caura_keystones(agent_id="alice")

    sent = sc.list_keystones.await_args.kwargs
    assert (sent["fleet_id"], sent["agent_id"]) == ("fleet-a", "alice")


# --- L-111: a tenant key can name its caller; the standalone operator is admin ---


_KEYSTONE = {"op": "set", "doc_id": "rule-1", "title": "t", "content": "c"}


def _trust_at(level: int, seen: list[str], *, not_found: bool = False):
    async def _require_trust(tenant_id, agent_id, min_level):
        seen.append(agent_id)
        return level, not_found, None

    return _require_trust


def _keystone_storage(monkeypatch) -> None:
    stub_storage_client(
        monkeypatch, get_document=None, upsert_keystone={"doc_id": "rule-1"}
    )
    monkeypatch.setattr(mcp_server, "log_action", AsyncMock())


async def test_l111_a_tenant_key_names_its_caller(mcp_env, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(mcp_server, "_require_trust", _trust_at(3, seen))
    _keystone_storage(monkeypatch)
    mcp_server._via_gateway_var.set(True)

    out = await mcp_server.caura_keystones_set(
        **_KEYSTONE, scope="tenant", weight="low", caller_agent_id="alice"
    )

    assert not is_error_envelope(out)
    assert set(seen) == {"alice"}


async def test_l111_an_asserted_caller_is_held_to_trust_2(mcp_env, monkeypatch):
    monkeypatch.setattr(mcp_server, "_require_trust", _trust_at(1, []))
    _keystone_storage(monkeypatch)

    out = await mcp_server.caura_keystones_set(
        **_KEYSTONE,
        scope="agent",
        weight="low",
        agent_id="alice",
        caller_agent_id="alice",
    )

    assert _code(out) == "FORBIDDEN"


async def test_l111_the_standalone_operator_needs_no_agent_row(mcp_env, monkeypatch):
    monkeypatch.setattr(settings, "is_standalone", True)
    seen: list[str] = []
    trust = _trust_at(0, seen, not_found=True)
    monkeypatch.setattr(mcp_server, "_require_trust", trust)
    _keystone_storage(monkeypatch)

    out = await mcp_server.caura_keystones_set(
        **_KEYSTONE, scope="tenant", weight="low"
    )

    assert not is_error_envelope(out)


# --- L-11: a failed trust lookup in caura_stats is an envelope -------------------


async def test_l11_stats_trust_lookup_failure_is_an_envelope(mcp_env, monkeypatch):
    async def _down(*args, **kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr(mcp_server, "_require_trust", _down)

    out = await mcp_server.caura_stats()

    assert _code(out) == "INTERNAL_ERROR"


# --- L-09: a transition is audited as its caller, with the owner in detail -------


async def test_l09_transition_audits_the_caller_not_the_owner(mcp_env, monkeypatch):
    memory = {
        "id": str(uuid.uuid4()),
        "agent_id": "bob",
        "fleet_id": None,
        "visibility": "scope_team",
        "status": "active",
    }
    stub_storage_client(monkeypatch, get_memory=memory, update_memory_status=None)
    allow = AsyncMock(return_value=True)
    monkeypatch.setattr(mcp_server, "authorize_memory_access", allow)
    audit = AsyncMock()
    monkeypatch.setattr(mcp_server, "log_action", audit)

    await mcp_server.caura_manage(
        op="transition", memory_id=memory["id"], status="archived", agent_id="alice"
    )

    sent = audit.await_args.kwargs
    assert sent["agent_id"] == "alice"
    assert sent["detail"]["owner_agent_id"] == "bob"


# --- L-137: caura_manage does not call REST-served ops MCP-only ------------------


def test_l137_caura_manage_names_the_plugin_not_mcp_as_the_gap():
    from core_api.app import app

    # The schema, not ``app.routes``: included routers are opaque there
    # (tests/_route_table.py says why).
    assert "/api/v1/memories/bulk-delete" in app.openapi()["paths"]
    assert "MCP-only" not in REGISTRY["caura_manage"].description
