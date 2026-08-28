"""The standalone middleware's body-injection guard and path converters.

``StandaloneTenantMiddleware`` injects ``tenant_id`` into a JSON body only
when the route's declared body schema takes the field (SAFE-01). The guard
matches the request path against the OpenAPI path templates, and OpenAPI
renders ``{slug:path}`` as ``{slug}``: a ``:path`` parameter carries slashes
the template cannot show. The skills-inbox actions are such routes, and a
Forge candidate's slug is ``forge/<name>``, so the guard must match a
placeholder across slashes or the body gets the field its model forbids.
"""

import pytest
from core_api.middleware import standalone_tenant as st
from core_api.middleware.standalone_tenant import StandaloneTenantMiddleware
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

pytestmark = pytest.mark.asyncio

TENANT = "standalone-tenant"


class StrictReason(BaseModel):
    """The shape of the inbox action bodies: one field, extras forbidden."""

    model_config = ConfigDict(extra="forbid")
    reason: str


class TakesTenant(BaseModel):
    tenant_id: str | None = None
    note: str


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/things/{slug:path}/act")
    async def act(slug: str, body: StrictReason):
        return {"slug": slug, "reason": body.reason}

    @app.post("/api/v1/plain/{name}/act")
    async def plain(name: str, body: StrictReason):
        return {"name": name, "reason": body.reason}

    @app.post("/api/v1/notes")
    async def notes(body: TakesTenant):
        return {"tenant_id": body.tenant_id, "note": body.note}

    app.add_middleware(StandaloneTenantMiddleware)
    return app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(st, "get_standalone_tenant_id", lambda: TENANT)
    app = _app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_path_converter_param_with_a_slash_gets_no_injection(client):
    """``{slug:path}`` matched across slashes: the forbid-extra body passes."""
    resp = await client.post("/api/v1/things/forge/handoff/act", json={"reason": "r"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"slug": "forge/handoff", "reason": "r"}


async def test_single_segment_param_gets_no_injection(client):
    """The control: a one-segment parameter already worked."""
    resp = await client.post("/api/v1/plain/handoff/act", json={"reason": "r"})
    assert resp.status_code == 200, resp.text


async def test_body_that_takes_tenant_id_is_still_injected(client):
    """The other half of the guard: a body that declares the field gets it."""
    resp = await client.post("/api/v1/notes", json={"note": "n"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == TENANT


def test_real_inbox_action_path_is_skipped():
    """On the real app a Forge slug reaches the inbox actions untouched."""
    from core_api.app import app

    scope = {
        "app": app,
        "method": "POST",
        "path": "/api/v1/skills-inbox/forge/summarize-oncall-handoff/reject",
    }
    assert st._body_model_accepts_tenant_id(scope) is False
    scope["path"] = "/api/v1/skills-inbox/summarize-oncall-handoff/edit"
    assert st._body_model_accepts_tenant_id(scope) is False
