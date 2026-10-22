"""A person refused a write for their role is told so, not about a key.

Prism App Review v2 N024. The enterprise gateway now answers a person's write
in a tenant where their role is read-only (a Reader) with ``X-Capabilities:
read``, the header an API key's capabilities travel in, so
``enforce_read_only`` refuses it. Its refusal spoke only to keys: "This API key
is read-only", and mint a key with the ``write`` capability under Connect >
Keys. A person signed in to the dashboard holds no key, so it named the wrong
cause and a way out they can't take.

Pins:
- a person with read-only capabilities gets ``READ_ONLY_ROLE`` and a message
  about their role that sends them to an admin, not to Connect > Keys;
- a read-only key keeps ``READ_ONLY_CREDENTIAL`` and its own message;
- end to end on Path 4, with the headers the gateway sends for a Reader.
"""

import pytest
from fastapi import HTTPException

from core_api import auth as auth_mod
from core_api import errors
from core_api.auth import AuthContext
from tests._legacy_contracts import LEGACY_API_KEY_FIELD

pytestmark = pytest.mark.unit


class _Req:
    def __init__(self, headers):
        self.headers = headers


def _refusal(ctx: AuthContext) -> dict:
    with pytest.raises(HTTPException) as caught:
        ctx.enforce_read_only()
    assert caught.value.status_code == 403
    return caught.value.detail


def _assert_about_the_role(detail: dict) -> None:
    assert detail["code"] == "READ_ONLY_ROLE"
    message = detail["message"].lower()
    assert "role" in message
    assert "api key" not in message
    remediation = detail["details"]["remediation"].lower()
    assert "admin" in remediation
    assert "connect" not in remediation
    assert "key" not in remediation


def test_a_person_with_a_read_only_role_is_told_about_their_role():
    _assert_about_the_role(
        _refusal(AuthContext(tenant_id="t", capabilities={"read"}, is_person=True))
    )


def test_a_read_only_key_keeps_its_own_refusal():
    detail = _refusal(AuthContext(tenant_id="t", capabilities={"read"}))
    assert detail["code"] == errors.AUTH_READ_ONLY_KEY
    assert (
        detail["message"]
        == "This API key is read-only and cannot perform write operations."
    )


async def test_a_readers_write_through_the_gateway_is_refused_for_the_role(monkeypatch):
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", None)
    monkeypatch.setattr(auth_mod.settings, "is_standalone", False)
    monkeypatch.setattr(auth_mod.settings, LEGACY_API_KEY_FIELD, None)
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)
    ctx = await auth_mod.get_auth_context(
        _Req({"x-tenant-id": "t", "x-org-role": "member", "x-capabilities": "read"}),
        key=None,
    )
    assert ctx.is_person
    _assert_about_the_role(_refusal(ctx))
