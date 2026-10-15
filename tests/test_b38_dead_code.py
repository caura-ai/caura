"""B38: code that shipped and nothing reached, and the claims made about it.

Each finding below was code with no production caller, or a comment that told a
reviewer something ran when it did not. The fix for most is deletion, so most
tests here assert absence: that the name is gone, so nothing can call it again
by accident or read the claim that it works. Eldad's decisions (2026-10-12):
L-200 delete the claim rather than build the escalation, L-203 stop queueing
rather than add a review route, L-207 add an admin verify route, L-220 delete
the scripts.

L-213 is not here: #1864 (M-91) gave ``rescan_before_apply`` a production
caller after the audit, so the "tested helper is the dead one" defect is gone;
this batch only corrects the docstring that named the wrong caller.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

import common.governance
import common.llm
from common.governance import pii_patterns
from common.llm import _credentials
from core_api import constants, tenant_context, tools
from core_api.app import http_exception_handler
from core_api.auth import AuthContext, get_auth_context
from core_api.clients import storage_client
from core_api.clients.storage_client import CoreStorageClient
from core_api.pipeline.steps.write import check_semantic_duplicate
from core_api.providers import _credentials as provider_credentials
from core_api.routes import audit
from core_api.services import agent_service, contradiction_detector, recall_service
from core_api.tools import _registry
from core_storage_api.app import create_app as create_storage_app
from core_storage_api.services.postgres_service import (
    PostgresService,
    _verify_audit_chain_rows,
)
from tests._route_table import iter_route_declarations
from tests.test_a1_identifier_prefilter import _build_ctx

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _storage_routes() -> set[tuple[str, str]]:
    """``(method, path)`` for every storage route, paths without the
    ``/api/v1/storage`` prefix. ``app.routes`` alone sees none of them: an
    included router is opaque in FastAPI 0.137 (see ``tests/_route_table``)."""
    return {
        (method, path)
        for path, methods in iter_route_declarations(create_storage_app().routes)
        for method in methods
    }


# -- L-200: nothing escalates by PII category ---------------------------------


def test_l200_no_category_set_claims_fail_closed_behaviour():
    assert not hasattr(common.governance, "HIGH_RISK_CATEGORIES")
    assert not hasattr(pii_patterns, "HIGH_RISK_CATEGORIES")
    assert "drive fail-closed behavior" not in (common.governance.__doc__ or "")


# -- L-201: has_credentials had no production caller ---------------------------


@pytest.mark.parametrize(
    "module",
    [common.llm, _credentials, provider_credentials],
    ids=lambda module: module.__name__,
)
def test_l201_has_credentials_is_gone(module):
    assert not hasattr(module, "has_credentials")


def test_l201_the_test_only_alias_is_gone():
    assert not hasattr(contradiction_detector, "_has_api_key")


# -- L-202: two authorization helpers only tests called ------------------------


def test_l202_the_uncalled_gates_are_gone():
    """The live gates are ``enforce_read_only`` and ``authorize_memory_access``."""
    assert not hasattr(AuthContext, "enforce_write_scope")
    assert not hasattr(agent_service, "enforce_memory_read")
    assert hasattr(AuthContext, "enforce_read_only")
    assert hasattr(agent_service, "authorize_memory_access")


# -- L-203: the dedup review queue was written and never read ------------------


async def test_l203_a_semantic_duplicate_queues_nothing(monkeypatch):
    """An auto-reject wrote a review row holding both memories' content, for a
    queue no route, tool or UI could list or decide."""
    storage = MagicMock()
    storage.enqueue_dedup_review = AsyncMock()
    monkeypatch.setattr(storage_client, "_client", storage)

    async def near_identical(*_args, **_kwargs):
        return {"id": "m-1", "similarity": 0.999, "content": "the same fact"}

    monkeypatch.setattr(
        check_semantic_duplicate, "_find_semantic_duplicate", near_identical
    )
    step = check_semantic_duplicate.CheckSemanticDuplicate()
    with pytest.raises(HTTPException) as refused:
        await step.execute(_build_ctx(content="the same fact"))

    assert refused.value.status_code == 409
    assert storage.mock_calls == []


def test_l203_the_unreachable_half_is_gone():
    for method in ("enqueue_dedup_review", "list_dedup_reviews", "decide_dedup_review"):
        assert not hasattr(CoreStorageClient, method), method
    for method in ("dedup_review_enqueue", "dedup_review_list", "dedup_review_decide"):
        assert not hasattr(PostgresService, method), method
    routes = _storage_routes()
    assert ("POST", "/entities/links/bulk") in routes, "the walk saw no routes"
    assert not [path for _, path in routes if "dedup-reviews" in path]


# -- L-204, L-205, L-206, L-216: storage chains with no production caller ------

_REMOVED_ROUTES = {
    ("GET", "/memories/recent"),
    ("POST", "/entities/links"),
    ("GET", "/agents/{agent_id}/search-profile"),
    ("GET", "/fleet/nodes/{node_name}"),
    ("DELETE", "/fleet/nodes/{node_name}"),
}
# The siblings production does call, which must survive. They are also what
# makes an empty walk of the route table fail rather than pass.
_KEPT_ROUTES = {
    ("POST", "/entities/links/bulk"),
    ("PATCH", "/agents/{agent_id}/search-profile"),
    ("POST", "/agents/{agent_id}/search-profile/reset"),
    ("GET", "/fleet/nodes"),
}


def test_the_uncalled_storage_routes_are_gone():
    routes = _storage_routes()
    assert routes >= _KEPT_ROUTES
    assert not _REMOVED_ROUTES & routes


@pytest.mark.parametrize(
    "method",
    [
        "get_recent_memories",
        "create_entity_link",
        "get_search_profile",
        "get_node",
        "delete_node",
    ],
)
def test_the_uncalled_client_methods_are_gone(method):
    assert not hasattr(CoreStorageClient, method)


@pytest.mark.parametrize(
    "method",
    [
        "memory_list_recent",
        "entity_add_entity_link",
        "fleet_delete_node",
        "fleet_delete_commands_for_nodes",
        "memory_count_missing_embeddings",  # L-217
    ],
)
def test_the_uncalled_service_methods_are_gone(method):
    assert not hasattr(PostgresService, method)


# -- L-207: the audit hash chain can be verified -------------------------------

# Verdicts from storage's own verifier rather than hand-written dicts, so a field
# the response model needs and storage does not send fails here, and so does a
# break shape ``AuditChainBreak`` cannot hold. An empty chain is intact.
_INTACT = _verify_audit_chain_rows("t1", [], None, 100_000)
_BREAKS = {
    # The head is past the last row read: rows were deleted off the tail.
    "tail-cut": _verify_audit_chain_rows(
        "t1", [], SimpleNamespace(last_seq=5, last_hash=b"h"), 100_000
    ),
    # A window resumed after a row that is gone.
    "missing-predecessor": _verify_audit_chain_rows(
        "t1", [], None, 100_000, start_seq=4, seed_prev=None
    ),
}


def _audit_client(auth: AuthContext, verdict: dict, monkeypatch) -> AsyncClient:
    storage = MagicMock()
    storage.verify_audit_chain = AsyncMock(return_value=verdict)
    monkeypatch.setattr(audit, "get_storage_client", lambda: storage)
    app = FastAPI()
    app.include_router(audit.router, prefix="/api/v1")
    app.add_exception_handler(HTTPException, http_exception_handler)

    async def _auth():
        return auth

    app.dependency_overrides[get_auth_context] = _auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_l207_a_tenant_can_verify_its_audit_chain(monkeypatch):
    """The chain was maintained on every write and nothing could check it."""
    auth = AuthContext(tenant_id="t1")
    async with _audit_client(auth, _INTACT, monkeypatch) as client:
        r = await client.get("/api/v1/audit-log/verify", params={"tenant_id": "t1"})
    assert r.status_code == 200, r.text
    assert r.json() == _INTACT


async def test_l207_a_truncated_window_says_where_the_next_starts(monkeypatch):
    window = {
        **_INTACT,
        "verified_count": 3,
        "head_seq": 3,
        "truncated": True,
        "next_seq": 4,
    }
    auth = AuthContext(tenant_id="t1")
    async with _audit_client(auth, window, monkeypatch) as client:
        r = await client.get(
            "/api/v1/audit-log/verify",
            params={"tenant_id": "t1", "limit": 3, "start_seq": 1},
        )
    assert r.status_code == 200, r.text
    assert (r.json()["truncated"], r.json()["next_seq"]) == (True, 4)


@pytest.mark.parametrize("verdict", list(_BREAKS.values()), ids=list(_BREAKS))
async def test_l207_a_break_comes_back_as_storage_reports_it(verdict, monkeypatch):
    auth = AuthContext(tenant_id="t1")
    async with _audit_client(auth, verdict, monkeypatch) as client:
        r = await client.get("/api/v1/audit-log/verify", params={"tenant_id": "t1"})
    assert r.status_code == 200, r.text
    assert r.json() == verdict


@pytest.mark.parametrize(
    "auth",
    [
        AuthContext(tenant_id="t1", agent_id="agent-7"),
        AuthContext(tenant_id="t1", org_role="member"),
        AuthContext(tenant_id="t2"),
    ],
    ids=["agent-credential", "org-member", "other-tenant"],
)
async def test_l207_verification_is_an_admin_plane_read(auth, monkeypatch):
    async with _audit_client(auth, _INTACT, monkeypatch) as client:
        r = await client.get("/api/v1/audit-log/verify", params={"tenant_id": "t1"})
    assert r.status_code == 403, r.text


# -- L-208: core-api constants nothing read ------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "ENTITY_BOOST_FACTOR",
        "MEMORY_RECALL_SUMMARY_NUM_SENTENCES",
        "TIER_LIMITS",
        "TRUST_LEVELS",
        "API_KEY_HASH_DISPLAY_LENGTH",
        "HEARTBEAT_INTERVAL_SECONDS",
        "BULK_EMBEDDING_CONCURRENCY",
        "LIFECYCLE_INTERVAL_HOURS",
        "LIFECYCLE_BATCH_SIZE",
        "EMBEDDING_REEMBED_BATCH_SIZE",
        "MEMORY_TYPES_PATTERN",
    ],
)
def test_l208_the_unread_constant_is_gone(name):
    assert not hasattr(constants, name)


# -- L-209: provider shims only tests imported ---------------------------------


@pytest.mark.parametrize("shim", ["fake", "gemini", "openai", "vertex"])
def test_l209_the_back_compat_shim_is_gone(shim):
    assert importlib.util.find_spec(f"core_api.providers.{shim}_provider") is None
    assert importlib.util.find_spec(f"common.llm.providers.{shim}") is not None


# -- L-212, L-214, L-215: a wrapper, a context var and a lookup nothing called -


def test_l212_the_uncalled_recall_wrapper_is_gone():
    assert not hasattr(recall_service, "recall")
    assert hasattr(recall_service, "summarize_memories")


def test_l214_the_unread_readable_tenants_var_is_gone():
    """The readable set travels on ``AuthContext.readable_tenant_ids``."""
    assert not hasattr(tenant_context, "get_readable_tenants")
    assert not hasattr(tenant_context, "set_readable_tenants")


def test_l215_trust_required_has_no_uncalled_lookup():
    assert not hasattr(tools, "required_trust")
    assert not hasattr(_registry, "required_trust")


# -- L-218, L-219: plugin dead data and the claim about UUID checks ------------


def _plugin(name: str) -> str:
    return (ROOT / "plugin" / "src" / name).read_text()


def test_l218_no_fallback_table_keyed_by_pre_v1_tool_names():
    env = _plugin("env.ts")
    for name in ("remember", "recall", "forget", "search", "ingest"):
        assert f'    {name}: "' not in env, name


def test_l219_the_plugin_claims_no_uuid_validation():
    assert "isValidUUID" not in _plugin("validation.ts")
    assert "UUID/safe-ID validation" not in _plugin("tool-definitions.ts")
    assert "UUID, HTTPS" not in _plugin("index.ts")


# -- L-220: scripts nothing referenced -----------------------------------------

_DEAD_SCRIPTS = (
    "capture_pg_locks_during_storm",
    "entity_retrieval_probe",
    "smoke_test_entity_matching",
    "wet_test_triple_emission",
    "benchmark_nano_vs_flash",
    "benchmark_openai_models",
    "repro_contradictions_collision",
    "repro_contradictions_diagnostic",
    "repro_contradictions_proper_noun",
    "repro_contradictions_with_entity_links",
    "wet_test_direction_invariance",
)


def test_l220_the_unreferenced_scripts_and_their_exemptions_are_gone():
    lint = (ROOT / "scripts" / "ruff.toml").read_text()
    types = (ROOT / "scripts" / "mypy.ini").read_text()
    for name in _DEAD_SCRIPTS:
        assert not (ROOT / "scripts" / f"{name}.py").exists(), name
        assert f'"{name}.py"' not in lint, name
        assert f"scripts.{name}]" not in types, name
