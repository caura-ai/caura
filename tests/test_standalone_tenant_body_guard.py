"""#1028 — the standalone body guard must pick the route the router will pick.

``StandaloneTenantMiddleware`` injects ``tenant_id`` into a JSON body only when
the route the request hits declares it. It used to decide that by matching the
request path against the OpenAPI path templates, one segment per placeholder.
A template has lost its converters — ``{slug:path}`` renders as ``{slug}`` — so
a skills-inbox action on a slug carrying a slash (``forge/<name>``,
``agent/<name>``: every candidate the factory produces) matched nothing, the
guard failed open, and ``reject`` / ``quarantine`` / ``edit`` answered 422 for
the ``tenant_id`` their strict bodies forbid.

Reading every placeholder across slashes instead is not a fix: an ordinary
route registered earlier can serve the same path, and the guard would then
withhold ``tenant_id`` from a body that requires it. The overlapping-route
cases below pin that, alongside the converter case itself.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict

from core_api.middleware import standalone_tenant
from core_api.middleware.standalone_tenant import (
    StandaloneTenantMiddleware,
    _body_model_accepts_tenant_id,
)

pytestmark = pytest.mark.unit

TENANT = "standalone-tenant"


class _TakesTenant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    reason: str


class _NoTenant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


def _hermetic_app() -> FastAPI:
    """Routes under a prefixed router, the way core-api mounts its own."""
    router = APIRouter()

    # Registered BEFORE the converter route it overlaps: for a two-segment
    # path the router picks this one, and its body requires ``tenant_id``.
    @router.post("/pairs/{group}/{name}/reject")
    async def reject_pair(group: str, name: str, body: _TakesTenant) -> dict:
        return {"route": "pair", "tenant_id": body.tenant_id}

    @router.post("/pairs/{slug:path}/reject")
    async def reject_pair_slug(slug: str, body: _NoTenant) -> dict:
        return {"route": "pair-slug", "slug": slug}

    @router.post("/items/{slug:path}/reject")
    async def reject_item(slug: str, body: _NoTenant) -> dict:
        return {"route": "item", "slug": slug}

    @router.post("/items/{item_id}/note")
    async def note_item(item_id: str, body: _TakesTenant) -> dict:
        return {"route": "note", "tenant_id": body.tenant_id}

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_middleware(StandaloneTenantMiddleware)
    return app


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(standalone_tenant, "get_standalone_tenant_id", lambda: TENANT)
    transport = httpx.ASGITransport(app=_hermetic_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_a_path_converter_slug_with_a_slash_is_not_injected(client) -> None:
    # The #1028 shape: 422 ``extra_forbidden`` on body.tenant_id before the fix.
    resp = await client.post(
        "/api/v1/items/agent/probe-skill-1/reject", json={"reason": "probe"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"route": "item", "slug": "agent/probe-skill-1"}


async def test_a_path_converter_slug_without_a_slash_is_not_injected(client) -> None:
    resp = await client.post(
        "/api/v1/items/probe-skill-2/reject", json={"reason": "probe"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"route": "item", "slug": "probe-skill-2"}


async def test_an_ordinary_route_registered_first_still_gets_the_tenant(client) -> None:
    # The path also matches ``/pairs/{slug:path}/reject``, which takes no
    # tenant_id. The router serves it from the route registered first, whose
    # body requires one, so the guard must inject: a missing ``tenant_id``
    # 422s here if the guard reads the converter route instead.
    resp = await client.post("/api/v1/pairs/a/b/reject", json={"reason": "probe"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"route": "pair", "tenant_id": TENANT}


async def test_the_converter_route_behind_it_serves_what_the_ordinary_one_cannot(
    client,
) -> None:
    resp = await client.post("/api/v1/pairs/a/b/c/reject", json={"reason": "probe"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"route": "pair-slug", "slug": "a/b/c"}


async def test_a_body_that_declares_tenant_id_still_receives_it(client) -> None:
    resp = await client.post("/api/v1/items/probe/note", json={"reason": "probe"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"route": "note", "tenant_id": TENANT}


def _accepts(method: str, path: str) -> bool:
    from core_api.app import app

    return _body_model_accepts_tenant_id({"app": app, "method": method, "path": path})


@pytest.mark.parametrize("action", ["reject", "quarantine", "edit"])
@pytest.mark.parametrize(
    "slug", ["agent/probe-skill-1", "forge/probe-skill-1", "probe-skill-1"]
)
def test_the_skills_inbox_actions_take_no_tenant_id_on_the_real_app(
    action: str, slug: str
) -> None:
    # An empty or broken route walk fails open, which would make this True.
    assert _accepts("POST", f"/api/v1/skills-inbox/{slug}/{action}") is False


def test_a_real_route_whose_body_declares_tenant_id_is_still_injected() -> None:
    assert _accepts("POST", "/api/v1/memories") is True
