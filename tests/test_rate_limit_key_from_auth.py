"""Auth names the rate-limit bucket after what it verified.

L-69: the limiter bucketed by whatever ``X-API-Key`` or bearer value a request
carried, before knowing whether anything had checked it. On the standalone path
no key is checked, and on the header-trust path without a gateway secret the
identity headers are the caller's own, so a fresh made-up key per request was a
fresh budget per request.

Behind the gateway the key is not proof either. The gateway's auth subrequest
accepts a JWT bearer, then a session cookie, and only then ``X-API-Key``, and it
forwards every one of them unchanged; core-api cannot tell which it accepted. A
signed-in user could add a fresh ``X-API-Key`` per request. So behind the gateway
a credential names the bucket only when it is the request's sole one, and
otherwise the identity the gateway itself set does: tenant, user, agent, install.
"""

from types import SimpleNamespace

from core_api import auth as auth_mod
from core_api import standalone
from core_api.middleware.rate_limit import _key_func
from tests._legacy_contracts import LEGACY_API_KEY_FIELD


class _Req:
    def __init__(self, headers):
        self.headers = headers
        self.state = SimpleNamespace()
        self.client = SimpleNamespace(host="203.0.113.5")
        self.scope = {"client": ("203.0.113.5", 0)}


async def _bucket(
    monkeypatch,
    headers,
    *,
    admin_key=None,
    caura_key=None,
    secret=None,
    standalone_mode=False,
):
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", secret)
    monkeypatch.setattr(auth_mod.settings, "is_standalone", standalone_mode)
    monkeypatch.setattr(auth_mod.settings, LEGACY_API_KEY_FIELD, caura_key)
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: admin_key)
    monkeypatch.setattr(standalone, "get_standalone_tenant_id", lambda: "t")

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)
    request = _Req(headers)
    await auth_mod.get_auth_context(request, key=headers.get("x-api-key"))
    return _key_func(request)


def _gateway(headers):
    """A request as the gateway forwards it: its identity headers, its secret,
    and whatever credential headers the client sent."""
    return {"x-tenant-id": "t", "x-gateway-secret": "gw", **headers}


IP = "ip:203.0.113.5"


async def test_the_admin_key_names_the_bucket(monkeypatch):
    bucket = await _bucket(monkeypatch, {"x-api-key": "adm"}, admin_key="adm")
    assert bucket.startswith("key:")


async def test_the_caura_api_key_names_the_bucket(monkeypatch):
    headers = {"x-api-key": "ck", "x-tenant-id": "t"}
    assert (await _bucket(monkeypatch, headers, caura_key="ck")).startswith("key:")


async def test_behind_the_gateway_a_sole_key_names_the_bucket(monkeypatch):
    """With one credential and no cookie, that is what the gateway accepted, so
    each key keeps its own budget."""
    sole = [{"x-api-key": "k1"}, {"x-api-key": "k2"}, {"authorization": "Bearer k3"}]
    buckets = {await _bucket(monkeypatch, _gateway(h), secret="gw") for h in sole}
    assert len(buckets) == 3
    assert IP not in buckets


async def test_a_rotated_key_beside_a_bearer_does_not_escape_the_limit(monkeypatch):
    """The review's case: the gateway accepted the JWT and never read the key."""
    rotated = [
        {"authorization": "Bearer jwt", "x-api-key": f"junk-{i}", "x-user-id": "u1"}
        for i in range(3)
    ]
    buckets = {await _bucket(monkeypatch, _gateway(h), secret="gw") for h in rotated}
    assert len(buckets) == 1


async def test_a_rotated_credential_beside_a_cookie_does_not_escape_the_limit(
    monkeypatch,
):
    """The gateway accepted the session cookie, so neither header was checked."""
    signed_in = {"cookie": "session_token=s", "x-user-id": "u1"}
    rotated = [{**signed_in, "x-api-key": f"junk-{i}"} for i in range(2)]
    rotated += [{**signed_in, "authorization": f"Bearer junk-{i}"} for i in range(2)]
    buckets = {await _bucket(monkeypatch, _gateway(h), secret="gw") for h in rotated}
    assert len(buckets) == 1


async def test_the_identity_bucket_is_per_user_not_per_org(monkeypatch):
    users = [{"cookie": "session_token=s", "x-user-id": u} for u in ("u1", "u2")]
    buckets = {await _bucket(monkeypatch, _gateway(h), secret="gw") for h in users}
    assert len(buckets) == 2
    assert IP not in buckets


async def test_standalone_does_not_bucket_by_a_key_header(monkeypatch):
    headers = {"x-api-key": "made-up"}
    assert await _bucket(monkeypatch, headers, standalone_mode=True) == IP


async def test_header_trust_without_a_secret_does_not_bucket_by_a_key_header(
    monkeypatch,
):
    headers = {"x-tenant-id": "t", "x-api-key": "made-up"}
    assert await _bucket(monkeypatch, headers) == IP
