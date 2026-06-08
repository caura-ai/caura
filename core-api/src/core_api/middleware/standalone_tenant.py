"""ASGI middleware that injects tenant_id into requests when running in standalone mode."""

import json
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
