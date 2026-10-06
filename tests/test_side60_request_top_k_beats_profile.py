"""A caller-named ``top_k`` beats an agent-profile / tenant-default ``top_k``.

SIDE-60 (tracker row lme-0929-m-07). ``resolve_search_params`` resolved
``top_k`` as ``profile.get("top_k", request_top_k)``, so any tenant
``search.default_profile.top_k`` (or agent search profile ``top_k``) silently
replaced the value the caller sent. Wet-tested before the fix: with
``default_profile.top_k=10``, a request ``top_k=12`` returned 10 rows and a
request ``top_k=3`` also returned 10 rows.

The rule now, on both search paths and on both ``/search`` and ``/recall``:

    request top_k (named in the body) > agent profile > tenant default > constant

A profile ``top_k`` is still the default for a caller that does not send one.
The explicit signal is the SIDE-57 ``top_k_explicit`` flag the routes already
derive from ``"top_k" in body.model_fields_set``.
"""

from __future__ import annotations

import uuid

import pytest

from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.constants import SEARCH_OVERFETCH_FACTOR
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.resolve_search_profile import ResolveSearchProfile
from core_api.services import memory_service
from core_api.services.memory_service import resolve_search_params
from core_api.services.organization_settings import ResolvedConfig
from core_api.tenant_context import set_current_tenant
from tests.conftest import get_test_auth

_PROFILE_TOP_K = 10


def _tenant_config(top_k: int = _PROFILE_TOP_K) -> ResolvedConfig:
    return ResolvedConfig({"search": {"default_profile": {"top_k": top_k}}})


# ── resolver: the precedence ladder itself ──


@pytest.mark.unit
@pytest.mark.parametrize("request_top_k", [3, 12], ids=["smaller", "larger"])
@pytest.mark.parametrize("source", ["tenant_default", "agent_profile"])
def test_explicit_request_top_k_beats_profile(request_top_k, source):
    profile = {"top_k": _PROFILE_TOP_K} if source == "agent_profile" else None
    tenant_config = _tenant_config() if source == "tenant_default" else None

    sp = resolve_search_params(
        profile,
        query="anything",
        top_k=request_top_k,
        tenant_config=tenant_config,
        top_k_explicit=True,
    )
    assert sp["top_k"] == request_top_k


@pytest.mark.unit
@pytest.mark.parametrize("source", ["tenant_default", "agent_profile"])
def test_profile_top_k_is_the_default_when_request_does_not_name_one(source):
    profile = {"top_k": _PROFILE_TOP_K} if source == "agent_profile" else None
    tenant_config = _tenant_config() if source == "tenant_default" else None

    sp = resolve_search_params(
        profile,
        query="anything",
        top_k=5,  # the schema default, not named by the caller
        tenant_config=tenant_config,
    )
    assert sp["top_k"] == _PROFILE_TOP_K


@pytest.mark.unit
def test_agent_profile_still_beats_tenant_default_when_not_explicit():
    sp = resolve_search_params(
        {"top_k": 7},
        query="anything",
        top_k=5,
        tenant_config=_tenant_config(),
    )
    assert sp["top_k"] == 7


@pytest.mark.unit
@pytest.mark.parametrize("explicit", [True, False])
async def test_pipeline_step_reads_the_explicit_flag(explicit):
    ctx = PipelineContext(
        data={
            "query": "anything",
            "top_k": 12,
            "top_k_explicit": explicit,
            "search_profile": None,
        },
        tenant_config=_tenant_config(),
    )
    await ResolveSearchProfile().execute(ctx)
    assert ctx.data["search_params"]["top_k"] == (12 if explicit else _PROFILE_TOP_K)


# ── both search paths: what storage is actually asked for ──


def _spy_on_scored_search(monkeypatch) -> list[dict]:
    from core_storage_api.services import postgres_service as pg

    real = pg.PostgresService.memory_scored_search
    calls: list[dict] = []

    async def _spy(self, *a, **kw):
        calls.append(dict(kw))
        return await real(self, *a, **kw)

    monkeypatch.setattr(pg.PostgresService, "memory_scored_search", _spy)
    return calls


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
@pytest.mark.parametrize(
    ("request_top_k", "explicit", "expected"),
    [(3, True, 3), (12, True, 12), (5, False, _PROFILE_TOP_K)],
    ids=["explicit-smaller", "explicit-larger", "not-named"],
)
async def test_storage_window_follows_the_precedence_on_both_paths(
    tenant_id, monkeypatch, use_pipeline, request_top_k, explicit, expected
):
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    calls = _spy_on_scored_search(monkeypatch)

    await memory_service.search_memories(
        tenant_id=tenant_id,
        query="which region hosts the billing database",
        top_k=request_top_k,
        tenant_config=_tenant_config(),
        top_k_explicit=explicit,
    )

    path = "pipeline" if use_pipeline else "legacy"
    assert calls, f"the {path} path never reached memory_scored_search"
    assert calls[0].get("top_k") == expected * SEARCH_OVERFETCH_FACTOR, (
        f"the {path} path asked storage for top_k={calls[0].get('top_k')!r}; "
        f"expected {expected} x overfetch {SEARCH_OVERFETCH_FACTOR}"
    )


# ── end-to-end: /search and /recall row counts ──


_QUERY = "which region hosts the billing database cluster"
_ROWS = 14


async def _seed(client, headers, tenant_id):
    # The query text itself, so every row is an exact lexical and embedding
    # match and survives the similarity floor under the fake embedder.
    for i in range(_ROWS):
        resp = await client.post(
            "/api/v1/memories",
            headers=headers,
            json={
                "tenant_id": tenant_id,
                "agent_id": "side60-bot",
                "content": f"{_QUERY} — note {i} {uuid.uuid4().hex}",
            },
        )
        assert resp.status_code == 201, resp.text


@pytest.fixture
def tenant_default_top_k(monkeypatch):
    """Every tenant resolves ``search.default_profile.top_k = 10``."""
    monkeypatch.setattr(
        ResolvedConfig,
        "default_search_profile",
        property(lambda self: {"top_k": _PROFILE_TOP_K}),
    )


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
@pytest.mark.parametrize("route", ["/api/v1/search", "/api/v1/recall"])
async def test_route_returns_request_top_k_over_tenant_default(
    client, monkeypatch, tenant_default_top_k, use_pipeline, route
):
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    tenant_id = f"test-tenant-side60-{uuid.uuid4().hex[:8]}"
    headers = get_test_auth(tenant_id)[1]
    await _seed(client, headers, tenant_id)

    def _count(body: dict) -> int:
        rows = body["items"] if route.endswith("/search") else body["memories"]
        return len(rows)

    for sent, expected in ((12, 12), (3, 3), (None, _PROFILE_TOP_K)):
        payload = {"tenant_id": tenant_id, "query": _QUERY}
        if sent is not None:
            payload["top_k"] = sent
        resp = await client.post(route, headers=headers, json=payload)
        assert resp.status_code == 200, resp.text
        assert _count(resp.json()) == expected, (
            f"{route} ({'pipeline' if use_pipeline else 'legacy'}) with top_k={sent} "
            f"and tenant default_profile.top_k={_PROFILE_TOP_K} returned "
            f"{_count(resp.json())} rows, expected {expected}"
        )


_AGENT_TOP_K = 7


@pytest.fixture
def as_tenant():
    """Authenticate with a tenant credential, as an SDK caller does.

    ``get_test_auth()`` returns the ADMIN key, which resolves to
    ``tenant_id=None``, and both routes look the agent up only behind
    ``if auth.tenant_id:``, so under it no agent profile is ever read.
    """

    def _install(tenant_id: str):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(tenant_id=tenant_id, readable_tenant_ids=[tenant_id])

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
@pytest.mark.parametrize("route", ["/api/v1/search", "/api/v1/recall"])
async def test_route_applies_the_agents_tuned_top_k(
    client, monkeypatch, as_tenant, use_pipeline, route
):
    """M-32: /recall never passed the agent's search profile to the search, so a
    knob tuned with caura_tune or PATCH /agents/{id}/tune applied on /search and
    MCP recall and not here. ``top_k`` stands in for every knob: the same profile
    carries them all. /search is the control.
    """
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    tenant_id = f"test-tenant-m32-{uuid.uuid4().hex[:8]}"
    headers = get_test_auth(tenant_id)[1]
    await _seed(client, headers, tenant_id)
    tuned = await client.patch(
        f"/api/v1/agents/side60-bot/tune?tenant_id={tenant_id}",
        headers=headers,
        json={"top_k": _AGENT_TOP_K},
    )
    assert tuned.status_code == 200, tuned.text

    as_tenant(tenant_id)
    resp = await client.post(
        route,
        headers=headers,
        json={"tenant_id": tenant_id, "query": _QUERY, "caller_agent_id": "side60-bot"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = body["items"] if route.endswith("/search") else body["memories"]
    assert len(rows) == _AGENT_TOP_K, (
        f"{route} returned {len(rows)} rows for an agent tuned to {_AGENT_TOP_K}"
    )
