"""ax-0917-m-12 — `tenant_id` was required even though the credential has one.

Every request body demanded `tenant_id`, so the first call an agent could make
was never the one it came for: `GET /whoami`, read the tenant back, echo it into
the body. Both independent probes of this API (Hermes and Codex, 2026-09-17)
paid that round-trip before they could write or recall anything, and an agent
that skips it gets a 422 naming a field whose value it was never given — a dead
end unless it already knows the fix.

The credential is the authority on which tenant a caller belongs to, so it is
also the default. An explicit value still wins and is gated exactly as before.
"""

import contextvars
import inspect

import pytest
from pydantic import ValidationError

from core_api import schemas
from core_api.tenant_context import _current_tenant_id, set_current_tenant

pytestmark = pytest.mark.unit


# Every request body that carries a tenant. Parametrised rather than spot-checked
# so a NEW body added with a bare `tenant_id: str` is caught here rather than
# reintroducing the round-trip on one endpoint.
BODIES = [
    "MemoryCreate",
    "BulkMemoryCreate",
    "ConflictResolveRequest",
    "SearchRequest",
    "EntityUpsert",
    "RelationUpsert",
    "IngestRequest",
    "IngestCommitRequest",
]

# Bodies declared next to their route rather than in ``schemas`` — same defect,
# same fix, and the keystone one is its own audit row (ax-0917-m-17: keystones
# are documented as the mandatory FIRST call, which made requiring a value only
# /whoami could supply a contradiction in the docs).
ROUTE_BODIES = [
    ("core_api.routes.documents", "DocWriteRequest"),
    ("core_api.routes.documents", "DocQueryRequest"),
    ("core_api.routes.documents", "DocSearchRequest"),
    ("core_api.routes.documents", "InstallableSkillsRequest"),
    ("core_api.routes.keystones", "KeystoneSetRequest"),
    ("core_api.routes.evolve", "EvolveRequest"),
    ("core_api.routes.insights", "InsightsRequest"),
    ("core_api.routes.fleet", "FleetCreateIn"),
    ("core_api.routes.fleet", "HeartbeatIn"),
]


@pytest.fixture
def credential_tenant():
    """Set the tenant the way `get_auth_context` does, in an isolated context."""

    def _set(tenant_id):
        set_current_tenant(tenant_id)

    token = _current_tenant_id.set(None)
    try:
        yield _set
    finally:
        _current_tenant_id.reset(token)


def _minimal(name: str) -> dict:
    """Smallest valid payload per body, minus tenant_id."""
    return {
        "MemoryCreate": {"content": "x"},
        "BulkMemoryCreate": {"memories": [{"content": "x"}]},
        "ConflictResolveRequest": {
            "conflict_id": "c1",
            "resolution": "keep_new",
        },
        "SearchRequest": {"query": "x"},
        "EntityUpsert": {"name": "x", "entity_type": "person"},
        "RelationUpsert": {
            "from_entity_id": "e1",
            "to_entity_id": "e2",
            "relation_type": "knows",
        },
        "IngestRequest": {"text": "x"},
        "IngestCommitRequest": {"facts": []},
    }[name]


# ── the fix ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", BODIES)
def test_the_body_may_omit_tenant_id(name, credential_tenant):
    """The whole point: no /whoami first."""
    credential_tenant("tenant-from-key")
    model = getattr(schemas, name)

    try:
        body = model(**_minimal(name))
    except ValidationError as exc:
        # A body may legitimately require other fields this test doesn't know
        # about; only a complaint about tenant_id is a failure of this change.
        assert "tenant_id" not in str(exc), f"{name} still demands tenant_id"
        return

    assert body.tenant_id == "tenant-from-key"


@pytest.mark.parametrize("name", BODIES)
def test_an_explicit_tenant_id_still_wins(name, credential_tenant):
    """Cross-tenant reads name their source tenant. Defaulting must not
    quietly rewrite a value the caller supplied."""
    credential_tenant("home-tenant")
    model = getattr(schemas, name)

    try:
        body = model(**_minimal(name), tenant_id="other-tenant")
    except ValidationError as exc:
        assert "tenant_id" not in str(exc)
        return

    assert body.tenant_id == "other-tenant"


@pytest.mark.parametrize("name", BODIES)
def test_every_tenant_body_shares_the_defaulting(name):
    """Inheritance, not eight copies — so the next body added gets it too."""
    model = getattr(schemas, name)
    assert issubclass(model, schemas.TenantScopedBody)


def test_no_request_body_redeclares_tenant_id():
    """A subclass re-declaring `tenant_id: str` would shadow the inherited
    default and silently restore the round-trip on that one endpoint, while
    every test above still passed via the other seven."""
    import ast

    tree = ast.parse(inspect.getsource(schemas))
    offenders = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(b, ast.Name) and b.id == "TenantScopedBody" for b in node.bases
        )
        and any(
            isinstance(st, ast.AnnAssign)
            and getattr(st.target, "id", "") == "tenant_id"
            for st in node.body
        )
    ]
    assert offenders == []


# ── the admin case ───────────────────────────────────────────────────────


def test_an_admin_key_must_still_name_the_tenant(credential_tenant):
    """`AuthContext.tenant_id` is None for an admin key by design (admin
    bypasses RLS), so there is genuinely nothing to default to. Guessing one
    would be the dangerous failure; refusing is right."""
    credential_tenant(None)

    with pytest.raises(ValidationError) as exc:
        schemas.SearchRequest(query="x")

    assert "admin key" in str(exc.value)


def test_the_refusal_says_what_to_do():
    """The old 422 said `field required` about a value the caller was never
    given, which is what sent both probes to /whoami. The message has to name
    the way out."""
    set_current_tenant(None)
    with pytest.raises(ValidationError) as exc:
        schemas.SearchRequest(query="x")

    msg = str(exc.value)
    assert "tenant_id" in msg
    assert "supplies it automatically" in msg


def test_the_empty_default_is_never_observable(credential_tenant):
    """`tenant_id: str = ""` keeps the field typed `str` for every consumer
    downstream — the reason this is a validator and not `str | None`. The
    empty value must not survive validation, or that convenience becomes an
    empty tenant reaching storage."""
    credential_tenant(None)

    with pytest.raises(ValidationError):
        schemas.SearchRequest(query="x")

    with pytest.raises(ValidationError):
        schemas.SearchRequest(query="x", tenant_id="")


# ── the framework behaviour this leans on ────────────────────────────────


@pytest.mark.asyncio
async def test_fastapi_solves_dependencies_before_validating_the_body():
    """Load-bearing and not ours to control.

    The defaulting reads a contextvar that `get_auth_context` sets. That only
    works because FastAPI solves dependencies BEFORE it validates the request
    body. If a future release reorders those, the contextvar would be unset at
    validation time and every defaulted request would 422 — for an admin-key
    reason that has nothing to do with the caller.
    """
    from fastapi import Depends, FastAPI
    from httpx import ASGITransport, AsyncClient

    order: list[str] = []
    probe: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "probe", default=None
    )

    async def fake_auth():
        order.append("dependency")
        probe.set("tenant-from-dependency")

    class Body(schemas.BaseModel):
        value: str = ""

        @schemas.model_validator(mode="after")
        def _observe(self):
            order.append("body-validation")
            object.__setattr__(self, "value", probe.get() or "UNSET")
            return self

    app = FastAPI()

    @app.post("/probe")
    async def _route(body: Body, _=Depends(fake_auth)):
        return {"value": body.value}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/probe", json={})

    assert order == ["dependency", "body-validation"]
    assert resp.json()["value"] == "tenant-from-dependency"


# ── the bodies that live next to their routes ────────────────────────────


@pytest.mark.parametrize("module,name", ROUTE_BODIES)
def test_route_local_bodies_default_the_tenant_too(module, name, credential_tenant):
    """A body declared in a route file is no less a REST body. Missing these
    would leave the round-trip in place on exactly the endpoints an agent
    reaches for first — documents and keystones."""
    import importlib

    credential_tenant("tenant-from-key")
    model = getattr(importlib.import_module(module), name)
    assert issubclass(model, schemas.TenantScopedBody)


def test_no_route_body_still_requires_a_tenant_id():
    """Sweeps every route module rather than trusting the list above, so a body
    added later with a bare ``tenant_id: str`` is caught here.

    Detects request bodies by USE — a class annotated as a ``body:`` parameter
    on some route — rather than by name. A name-based rule (skip ``*Out`` /
    ``*Response``) let ``AuditEntry`` through, which is a response model whose
    ``tenant_id`` the server reports and no caller ever supplies; converting it
    would have been meaningless at best.
    """
    import ast
    import pathlib

    routes = pathlib.Path(schemas.__file__).parent / "routes"
    trees = {p: ast.parse(p.read_text()) for p in sorted(routes.glob("*.py"))}

    request_bodies: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.arg == "body" and arg.annotation is not None:
                    request_bodies.add(ast.unparse(arg.annotation))

    offenders = [
        f"{path.name}:{node.name}"
        for path, tree in trees.items()
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in request_bodies
        for st in node.body
        if isinstance(st, ast.AnnAssign)
        and getattr(st.target, "id", "") == "tenant_id"
        and st.value is None
    ]

    assert offenders == []
