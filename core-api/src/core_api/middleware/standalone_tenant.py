"""ASGI middleware that injects tenant_id into requests when running in standalone mode."""

import json
import re
from urllib.parse import parse_qs, urlencode

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

        # Don't inject for superadmin sessions (they may intentionally omit tenant_id)
        headers_list = scope.get("headers", [])
        for name, value in headers_list:
            if name.lower() == b"authorization":
                auth_val = value.decode("latin-1", errors="ignore")
                if auth_val.startswith("Bearer "):
                    try:
                        from jose import jwt as jose_jwt

                        from core_api.config import settings as _settings

                        token = auth_val[7:]  # strip "Bearer "
                        payload = jose_jwt.decode(
                            token,
                            _settings.jwt_secret,
                            algorithms=["HS256"],
                            options={"verify_exp": False},
                        )
                        if payload.get("super_admin"):
                            await self.app(scope, receive, send)
                            return
                    except Exception:
                        pass
                break

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

    Reads ``app.openapi()`` rather than walking ``app.router.routes``, for the
    reason spelled out in ``app.py``'s timeout-opt-out guard: FastAPI 0.137
    mounts prefixed routers as an opaque ``_IncludedRouter`` with no public
    ``.routes``, so a walk sees 30 top-level entries instead of the ~91 real
    paths and would silently conclude "no match → inject" for every router-
    mounted endpoint. The schema is the stable public surface, it is already
    built and cached at import time by that same guard, and this result is
    computed once per app and memoised.
    """
    app = scope.get("app")
    if app is None:
        return True
    skip = _no_tenant_id_body_routes(app)
    if skip is None:
        return True
    method = scope.get("method", "").lower()
    path = scope.get("path", "")
    return not any(methods and method in methods and pattern.match(path) for pattern, methods in skip)


# ``id(app)`` -> tuple of (compiled path regex, frozenset of lowercase methods)
# for the operations whose JSON body schema has no ``tenant_id`` property.
# Memoised because it is derived from a schema that cannot change after
# startup; keyed by app identity so a test that builds a second app is correct
# rather than merely fast.
_SKIP_CACHE: dict[int, tuple | None] = {}


def _no_tenant_id_body_routes(app) -> tuple | None:
    cached = _SKIP_CACHE.get(id(app))
    if cached is not None or id(app) in _SKIP_CACHE:
        return cached
    try:
        schema = app.openapi()
        components = schema.get("components", {}).get("schemas", {})
        entries = []
        for raw_path, path_item in schema.get("paths", {}).items():
            methods = set()
            for method, operation in path_item.items():
                if method.lower() not in ("post", "put", "patch"):
                    continue
                body_schema = (
                    operation.get("requestBody", {})
                    .get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if not isinstance(body_schema, dict):
                    continue  # no JSON body declared → nothing to contradict
                resolved = _resolve_schema(body_schema, components)
                if resolved is None or "properties" not in resolved:
                    continue  # free-form (``body: dict``) → historical behaviour
                if "tenant_id" not in resolved["properties"]:
                    methods.add(method.lower())
            if methods:
                entries.append((_path_template_to_regex(raw_path), frozenset(methods)))
        result: tuple | None = tuple(entries)
    except Exception:  # pragma: no cover — never break requests over this
        result = None
    _SKIP_CACHE[id(app)] = result
    return result


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


def _path_template_to_regex(template: str):
    """``/api/v1/memories/{memory_id}`` -> a pattern matching one concrete path."""
    parts = re.split(r"(\{[^/}]*\})", template)
    pattern = "".join("[^/]+" if p.startswith("{") and p.endswith("}") else re.escape(p) for p in parts)
    return re.compile(f"^{pattern}$")


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
