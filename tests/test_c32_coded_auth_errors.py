"""Refusals must say WHICH refusal they are — C32 / API-05.

``code_for_status`` maps every 403 to ``FORBIDDEN`` and every 401 to
``UNAUTHORIZED``. The auth boundary has eight distinct reasons to refuse a
request, and all of them arrived at the caller as one of those two words. An
agent that gets FORBIDDEN on a write cannot tell "this credential is read-only"
from "your org is over its plan limit" from "this needs an admin", so the only
move left is to guess.

One did. An agent was refused a write with a tenant key and concluded that
tenant keys cannot write — false, and it stopped trying. That is the failure mode these
tests exist for: not a missing error, a *misleading* one. A wrong general rule
learned from a specific refusal is worse than no answer.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core_api import errors
from core_api.auth import AuthContext

pytestmark = pytest.mark.unit


def _refusal(fn) -> tuple[int, dict]:
    with pytest.raises(HTTPException) as caught:
        fn()
    return caught.value.status_code, caught.value.detail


def test_two_different_403s_are_distinguishable() -> None:
    """The whole point. Same status, same shape, different machine-readable code."""
    _, read_only = _refusal(AuthContext(tenant_id="t", capabilities=set()).enforce_read_only)
    _, over_limit = _refusal(
        AuthContext(tenant_id="t", is_read_only=True).enforce_usage_limits
    )
    _, needs_admin = _refusal(AuthContext(tenant_id="t", is_admin=False).enforce_admin)

    codes = {read_only["code"], over_limit["code"], needs_admin["code"]}
    assert codes == {
        errors.AUTH_READ_ONLY_KEY,
        errors.AUTH_PLAN_LIMIT,
        errors.AUTH_ADMIN_REQUIRED,
    }
    assert len(codes) == 3, "three reasons collapsed into fewer codes"


def test_the_read_only_refusal_cannot_teach_the_false_general_rule() -> None:
    """The exact reported failure: refused a write, concluded tenant keys cannot write.

    The remediation has to say the limit is a property of the CREDENTIAL, or the
    reader is entitled to the wrong inference.
    """
    _, detail = _refusal(AuthContext(tenant_id="t", capabilities=set()).enforce_read_only)
    assert detail["code"] == errors.AUTH_READ_ONLY_KEY
    remediation = detail["details"]["remediation"].lower()
    assert "credential" in remediation
    assert "retry" in remediation


def test_the_human_message_is_unchanged() -> None:
    """Back-compat: ``detail`` stays the string clients already read.

    ``app.http_exception_handler`` flattens the structured detail back to this
    message at the top level, so nothing that logged or displayed it moves.
    """
    _, detail = _refusal(AuthContext(tenant_id="t", is_admin=False).enforce_admin)
    assert detail["message"] == "Admin access required"


def test_tenant_mismatch_reports_which_tenant_was_refused() -> None:
    ctx = AuthContext(tenant_id="mine", is_admin=False)
    _, detail = _refusal(lambda: ctx.enforce_tenant("theirs"))
    assert detail["code"] == errors.AUTH_TENANT_MISMATCH
    assert detail["details"]["requested_tenant"] == "theirs"
    # The credential is valid — saying so prevents "my key is broken".
    assert "different tenant" in detail["details"]["remediation"]


async def test_the_suppression_refusal_stays_deliberately_vague(monkeypatch) -> None:
    """Not every message should improve.

    The org-suspended 403 is generic on purpose: naming the lifecycle state
    leaks it to a partner whose key was provisioned under that org. So the CODE
    carries the machine signal while the MESSAGE stays exactly as opaque as it
    was — a case where adding remediation would be the bug.
    """
    from core_api import auth

    monkeypatch.setattr(auth, "is_tenant_suppressed", lambda _tenant: _true())

    with pytest.raises(HTTPException) as caught:
        await auth._block_if_suppressed("t-suppressed")

    detail = caught.value.detail
    assert caught.value.status_code == 403
    assert detail["code"] == errors.AUTH_ORG_SUSPENDED
    assert detail["message"] == "Organization is suspended; access denied."
    # No lifecycle vocabulary, and nothing added under details either.
    leaked = ("soft-delete", "deleted", "lifecycle", "suspended org")
    assert not any(word in str(detail["details"]).lower() for word in leaked)


async def _true() -> bool:
    return True


def test_every_auth_code_is_unique() -> None:
    """Two reasons sharing a code is the bug this whole item is about,
    reintroduced one level down."""
    codes = [v for k, v in vars(errors).items() if k.startswith("AUTH_") and isinstance(v, str)]
    assert len(codes) == len(set(codes)), sorted(codes)
    assert all(c.isupper() for c in codes), codes


def test_the_spec_documents_the_errors_every_route_can_return() -> None:
    """422 used to be the only documented error response on the entire surface,
    so a generated client had no type for a body it will certainly receive."""
    from core_api.app import app

    schema = app.openapi()
    assert "CauraError" in schema["components"]["schemas"]

    operations = [
        (path, method, op)
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert operations
    missing = [
        f"{m.upper()} {p}"
        for p, m, op in operations
        if not {"401", "403"} <= set(op.get("responses", {}))
    ]
    assert not missing, missing[:5]

    ref = {"$ref": "#/components/schemas/CauraError"}
    _, _, sample = operations[0]
    assert sample["responses"]["403"]["content"]["application/json"]["schema"] == ref
