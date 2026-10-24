"""ASGI middleware that injects tenant_id into requests when running in standalone mode."""

import json
from urllib.parse import parse_qs, urlencode

from fastapi.routing import iter_route_contexts
from starlette.routing import WebSocketRoute

from core_api.standalone import get_standalone_tenant_id


class StandaloneTenantMiddleware:
    """When IS_STANDALONE=true and tenant_id is missing, inject it into query string and JSON body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        tenant_id = get_standalone_tenant_id()
        if not tenant_id:
            await self.app(scope, receive, send)
            return

        # Don't inject into paths where tenant_id has a different semantic
        path = scope.get("path", "")
        if path.startswith(
            (
                "/api/auth/",
                "/api/orgs/",
                "/api/admin/",
                "/api/register",
                "/api/superadmin/",
                "/api/billing/webhook",
            )
        ):
            await self.app(scope, receive, send)
            return

        # Inject tenant_id into query string if missing
        qs = scope.get("query_string", b"")
        if b"tenant_id=" not in qs:
            params = parse_qs(qs.decode(), keep_blank_values=True)
            params["tenant_id"] = [tenant_id]
            scope = dict(scope, query_string=urlencode(params, doseq=True).encode())

        # Inject tenant_id into JSON body if missing (POST/PUT/PATCH with JSON content only)
        method = scope.get("method", "")
        if method in ("POST", "PUT", "PATCH"):
            headers = scope.get("headers", [])
            content_type = ""
            content_length = None
            for name, value in headers:
                lower = name.lower()
                if lower == b"content-type":
                    content_type = value.decode("latin-1").split(";")[0].strip()
                elif lower == b"content-length":
                    try:
                        content_length = int(value)
                    except (ValueError, TypeError):
                        pass
            can_inject = (
                content_type == "application/json"
                and content_length is not None
                and content_length <= _MAX_BODY_INJECT
                and _body_model_accepts_tenant_id(scope)
            )
            if can_inject:
                body = await _read_body(receive)
                if len(body) <= _MAX_BODY_INJECT:
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict) and "tenant_id" not in data:
                            data["tenant_id"] = tenant_id
                            body = json.dumps(data).encode()
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                new_headers = [
                    (n, str(len(body)).encode() if n.lower() == b"content-length" else v) for n, v in headers
                ]
                scope = dict(scope, headers=new_headers)
                receive = _make_receive(body)

        await self.app(scope, receive, send)


_MAX_BODY_INJECT = 1_048_576  # 1 MiB — skip injection for larger payloads


def _body_model_accepts_tenant_id(scope) -> bool:
    """Does the route this request will hit actually take ``tenant_id`` in its body?

    SAFE-01. Until write bodies became ``extra="forbid"``, this middleware could
    inject ``tenant_id`` into every JSON body on the way past and be sure it
    would land harmlessly: a route whose model didn't declare the field simply
    dropped it. That silent drop is the bug the strictness fix removes — so the
    injection has to become deliberate about where it applies, or standalone
    mode 422s itself on every endpoint that takes ``tenant_id`` as a QUERY
    param instead (``PATCH /memories/{id}``, ``PATCH /agents/{id}/trust``,
    ``/agents/{id}/tune``, ``/stm/promote``, ``/memories/redistribute``,
    ``/install-plugin``, the skills-inbox actions …). The query-string half of
    this middleware already serves those routes; the body half never should
    have.

    Note this was never *only* a latent problem: a body arriving at the app
    with a field its own schema doesn't define is exactly the invisible payload
    mutation SAFE-01 is about, one layer earlier than the client.

    Fails OPEN — an unmatched path, a route with no declared JSON body schema
    (``body: dict``), or a scope without the app all return True, preserving
    the historical behaviour. The only case that changes is the one we can
    prove is wrong: a resolved body schema with no ``tenant_id`` property.

    Which route the request will hit is decided the way the router decides it:
    the first route, in registration order, whose own compiled pattern matches
    the path and whose methods include the request's (#1028). The OpenAPI path
    templates cannot answer that, because a template has lost its converters —
    ``{slug:path}`` renders as ``{slug}``. Read one segment per placeholder, a
    template misses the skills-inbox actions whenever the slug carries a slash,
    which every Forge and agent candidate's does; read across slashes, it claims
    paths that an ordinary route registered earlier actually serves, and would
    withhold ``tenant_id`` from a body that requires it.

    The routes come from ``fastapi.routing.iter_route_contexts``, the public
    walk ``app.openapi()`` itself builds the schema from, so the table sees
    every route the router tries, prefixed and in its order, without touching
    the ``_IncludedRouter`` internals that ``app.py``'s timeout-opt-out guard
    declines to depend on. The body schemas still come from ``app.openapi()``,
    keyed by each route's ``path_format``; a route the schema hides has none to
    read, so it keeps the historical injection. Both are fixed after startup,
    so the table is computed once per app and memoised.
    """
    app = scope.get("app")
    if app is None:
        return True
    routes = _route_table(app)
    if routes is None:
        return True
    method = scope.get("method", "").upper()
    path = scope.get("path", "")
    for pattern, methods, no_tenant_id_methods in routes:
        if (methods is None or method in methods) and pattern.match(path):
            return method not in no_tenant_id_methods
    return True


# ``id(app)`` -> tuple, in routing order, of (compiled path regex, frozenset of
# uppercase methods or None for "any", frozenset of the methods whose JSON body
# schema has no ``tenant_id`` property). Memoised because it is derived from
# routes and a schema that cannot change after startup; keyed by app identity
# so a test that builds a second app is correct rather than merely fast.
_ROUTE_CACHE: dict[int, tuple | None] = {}


def _route_table(app) -> tuple | None:
    cached = _ROUTE_CACHE.get(id(app))
    if cached is not None or id(app) in _ROUTE_CACHE:
        return cached
    try:
        no_tenant_id = _no_tenant_id_body_operations(app)
        entries = []
        for route in iter_route_contexts(app.routes):
            if isinstance(route.original_route, WebSocketRoute):
                continue  # never matches an HTTP request, so never shadows one
            pattern = getattr(route, "path_regex", None)
            if pattern is None:
                continue
            # A ``Mount`` declares no methods; the router hands it every one.
            methods = frozenset(m.upper() for m in route.methods) if route.methods else None
            skip = frozenset(m for m in methods or () if (route.path_format, m.lower()) in no_tenant_id)
            entries.append((pattern, methods, skip))
        result: tuple | None = tuple(entries)
    except Exception:  # pragma: no cover — never break requests over this
        result = None
    _ROUTE_CACHE[id(app)] = result
    return result


def _no_tenant_id_body_operations(app) -> set[tuple[str, str]]:
    """``(OpenAPI path, lowercase method)`` for each body schema with no ``tenant_id``."""
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    operations = set()
    for raw_path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in ("post", "put", "patch"):
                continue
            body_schema = (
                operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            )
            if not isinstance(body_schema, dict):
                continue  # no JSON body declared → nothing to contradict
            resolved = _resolve_schema(body_schema, components)
            if resolved is None or "properties" not in resolved:
                continue  # free-form (``body: dict``) → historical behaviour
            if "tenant_id" not in resolved["properties"]:
                operations.add((raw_path, method.lower()))
    return operations


def _resolve_schema(node: dict, components: dict) -> dict | None:
    """Follow one ``$ref``, and look through the ``anyOf`` an optional body emits."""
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        target = components.get(name)
        return target if isinstance(target, dict) else None
    for key in ("anyOf", "allOf", "oneOf"):
        for member in node.get(key, []):
            if isinstance(member, dict) and member.get("type") != "null":
                resolved = _resolve_schema(member, components)
                if resolved is not None:
                    return resolved
        # An ``anyOf`` we couldn't resolve is not evidence of anything.
        if key in node:
            return None
    return node


async def _read_body(receive) -> bytes:
    """Read the full request body from the ASGI receive callable."""
    chunks = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _make_receive(body: bytes):
    """Create a receive callable that returns the given body."""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive
