"""The bundled scripts address routes this API actually serves.

Three of them did not, and none could have been noticed by running the suite:
they are operator tools, not tests. ``latency_test`` and ``hyperagent_test``
both built their base URL as ``{url}/api`` while every router is mounted under
``/api/v1``, so each 404'd on its first call — against its own documented
default target. ``hyperagent_test`` then posted to ``/admin/keys``, a route
that has never existed anywhere, and ``latency_test`` finished by deleting
``/admin/tenants/{tenant}``, which does not exist either and whose 404 went
unread because the response was discarded.

The check is against the OpenAPI spec rather than a list kept here, so it
tracks the app instead of needing to be told about it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from core_api.app import app

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"

# Scripts that build a versioned base URL and hang paths off it.
_API_SCRIPTS = ("latency_test.py", "hyperagent_test.py", "gateway_integration_test.py")

# Paths a script calls deliberately that THIS app does not serve. Each entry is
# a statement about the deployment the script targets, not an exemption:
# provisioning a tenant-scoped key is an admin-API surface, and OSS core-api
# has no equivalent — which is why the script now says so when it 404s.
_NOT_SERVED_BY_OSS = {
    "/admin/agent-keys": "tenant key provisioning lives in the enterprise admin API",
}

# ``f"{api}/some/path..."`` — capture the literal segment before any {param}.
_CALL = re.compile(r'f"\{(?:api|api_url|self\.api)\}(/[a-zA-Z0-9_\-/]*)')


def _served_prefixes() -> set[str]:
    """Every documented path with its ``/api/v1`` prefix stripped."""
    return {
        p[len("/api/v1") :] for p in app.openapi()["paths"] if p.startswith("/api/v1")
    }


def _called_paths(script: pathlib.Path) -> set[str]:
    return {m.rstrip("/") or "/" for m in _CALL.findall(script.read_text())}


def test_the_spec_and_the_scan_both_see_something() -> None:
    """Vacuity: an empty spec or an empty scan would pass every assertion below."""
    served = _served_prefixes()
    assert len(served) > 50, f"only {len(served)} versioned paths in the spec"
    called = {p: _called_paths(_SCRIPTS / p) for p in _API_SCRIPTS}
    for name, paths in called.items():
        assert paths, (
            f"{name}: the scan found no API calls — the regex no longer matches"
        )


@pytest.mark.parametrize("script", _API_SCRIPTS)
def test_no_script_builds_an_unversioned_api_base(script: str) -> None:
    text = (_SCRIPTS / script).read_text()
    assert not re.search(r"""rstrip\(['"]/['"]\)\}/api["']""", text), (
        f"{script} builds an unversioned /api base; every router is mounted under /api/v1"
    )


@pytest.mark.parametrize("script", _API_SCRIPTS)
def test_every_path_a_script_calls_is_one_this_app_serves(script: str) -> None:
    served = _served_prefixes()
    unknown = []
    for path in sorted(_called_paths(_SCRIPTS / script)):
        if any(s == path or s.startswith(path + "/") for s in served):
            continue
        if any(path == k or path.startswith(k + "/") for k in _NOT_SERVED_BY_OSS):
            continue
        unknown.append(path)
    assert not unknown, (
        f"{script} calls paths this app does not serve: {unknown}. "
        "Either the path is wrong, or it belongs to another deployment — in which "
        "case add it to _NOT_SERVED_BY_OSS with the reason."
    )
