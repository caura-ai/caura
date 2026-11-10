"""Rule reads produce bounded, install-bound evidence for internal matching.

These are behavior tests for the read handlers, the bounded recorder and the
actual HTTP application. Storage is mocked here; the storage suite exercises
the migrated table, conflict handling, retention and match SQL separately.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import Response
from httpx import ASGITransport, AsyncClient

from common.governance.ruleset_hash import rule_set_hash, rules_from_keystone_rows
from core_api.auth import AuthContext, get_auth_context
from core_api.routes import keystones

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "test-tenant-keystone-fetch"
INSTALL = "11111111-1111-4111-8111-111111111111"
CREDENTIAL = "22222222-2222-4222-8222-222222222222"
PRINCIPAL = "recipient-agent"
FLEET = "recipient-fleet"
NOW = datetime(2026, 11, 10, 12, 0, 5, tzinfo=UTC)
ROWS = [
    {
        "doc_id": "rule-a",
        "updated_at": "2026-11-10T10:00:00Z",
        "data": {
            "scope": "tenant",
            "title": "Rule A",
            "content": "Keep evidence.",
            "weight": 50,
        },
    }
]
HASH = rule_set_hash(rules_from_keystone_rows(ROWS))


@pytest.fixture(autouse=True)
def isolated_capture_binding(monkeypatch):
    # A process-wide coalescer intentionally survives requests. Each test
    # starts a different binding without reaching into that cache's internals.
    monkeypatch.setattr(
        sys.modules[__name__], "TENANT", f"test-tenant-fetch-{uuid4().hex}"
    )


def _auth(**changes) -> AuthContext:
    return AuthContext(
        **{
            "tenant_id": TENANT,
            "is_install_credential": True,
            "credential_kind": "install_credential",
            "install_uuid": INSTALL,
            "credential_id": CREDENTIAL,
            **changes,
        }
    )


def _storage() -> MagicMock:
    sc = MagicMock(name="storage")
    sc.list_keystones = AsyncMock(return_value=(ROWS, False))
    sc.record_keystone_fetch = AsyncMock(return_value={"created": True})
    sc.match_keystone_fetches = AsyncMock(
        return_value={"results": [{"status": "no_matching_fetch"}]}
    )
    return sc


async def _rest(monkeypatch, sc, *, auth=None, envelope=False):
    monkeypatch.setattr(keystones, "get_storage_client", lambda: sc)
    # The route's existing principal resolver is tested elsewhere. Capture
    # must use its resolved answer, rather than the raw query aliases.
    monkeypatch.setattr(
        keystones, "_principal", AsyncMock(return_value=(FLEET, PRINCIPAL))
    )
    response = Response()
    body = await keystones.list_keystones(
        response,
        tenant_id=TENANT,
        fleet_id="query-fleet-alias",
        agent_id="query-agent-alias",
        envelope=envelope,
        auth=auth if auth is not None else _auth(),
    )
    return body, response


@pytest.mark.parametrize("envelope", [False, True], ids=["bare", "envelope"])
async def test_rest_read_records_the_actual_returned_set(monkeypatch, envelope):
    sc = _storage()
    body, _ = await _rest(monkeypatch, sc, envelope=envelope)
    assert body["items"] == ROWS if envelope else body == ROWS
    sc.record_keystone_fetch.assert_awaited_once()
    record = sc.record_keystone_fetch.await_args.args[0]
    assert {k: record[k] for k in ("tenant_id", "install_uuid", "credential_id")} == {
        "tenant_id": TENANT,
        "install_uuid": INSTALL,
        "credential_id": CREDENTIAL,
    }
    # Principal means the resolved recipient of the rule set; authenticated
    # credential/install attribution is recorded separately above.
    assert (record["principal"], record["fleet_id"], record["rule_set_hash"]) == (
        PRINCIPAL,
        FLEET,
        HASH,
    )
    assert datetime.fromisoformat(
        str(record["served_at"]).replace("Z", "+00:00")
    ).tzinfo
    assert "content" not in record and "rules" not in record


async def test_empty_and_truncated_sets_record_the_hash_the_reader_received(
    monkeypatch,
):
    sc = _storage()
    sc.list_keystones.return_value = ([], True)
    body, response = await _rest(monkeypatch, sc, envelope=True)
    assert body["items"] == [] and response.headers["X-Truncated"] == "true"
    sc.record_keystone_fetch.assert_awaited_once()
    record = sc.record_keystone_fetch.await_args.args[0]
    assert record["rule_set_hash"] == body["rule_set_hash"] == rule_set_hash([])
    assert record["truncated"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"is_install_credential": False, "credential_kind": "user_api_key"},
        {"install_uuid": None},
        {"install_uuid": "not-a-uuid"},
        {"credential_id": None},
    ],
    ids=["ordinary-key", "missing-install", "malformed-install", "missing-credential"],
)
async def test_incomplete_or_non_install_attribution_never_creates_evidence(
    monkeypatch, changes
):
    sc = _storage()
    body, _ = await _rest(monkeypatch, sc, auth=_auth(**changes))
    assert body == ROWS
    sc.record_keystone_fetch.assert_not_awaited()


async def test_unhashable_rules_are_still_served_without_positive_evidence(monkeypatch):
    sc = _storage()
    rows = [{"doc_id": "invalid-rule", "data": {"scope": "tenant"}}]
    sc.list_keystones.return_value = (rows, False)
    body, _ = await _rest(monkeypatch, sc, envelope=True)
    assert body["items"] == rows and body["rule_set_hash"] is None
    sc.record_keystone_fetch.assert_not_awaited()


async def test_recording_failure_logs_loss_and_preserves_the_rules(monkeypatch, caplog):
    sc = _storage()
    sc.record_keystone_fetch.side_effect = RuntimeError("synthetic recording outage")
    with caplog.at_level(logging.WARNING):
        body, _ = await _rest(monkeypatch, sc)
    assert body == ROWS
    sc.record_keystone_fetch.assert_awaited_once()
    assert any("fetch" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.parametrize(
    "recording_fails", [False, True], ids=["recorded", "recording-outage"]
)
@pytest.mark.parametrize("resolved_fleet", [FLEET, None])
async def test_mcp_read_records_install_and_resolved_recipient(
    monkeypatch, recording_fails, resolved_fleet
):
    from core_api import mcp_server
    from core_api.audit_actor import audit_principal
    from tests._mcp_test_helpers import parse_envelope

    sc = _storage()
    if recording_fails:
        sc.record_keystone_fetch.side_effect = RuntimeError(
            "synthetic recording outage"
        )
    monkeypatch.setattr(mcp_server, "get_storage_client", lambda: sc)
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: TENANT)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: None)
    monkeypatch.setattr(
        mcp_server, "lookup_agent", AsyncMock(return_value={"fleet_id": resolved_fleet})
    )
    monkeypatch.setattr(mcp_server, "_refuse_default_agent_on_gateway", lambda _: None)
    monkeypatch.setattr(
        mcp_server, "_refuse_unowned_agent", AsyncMock(return_value=None)
    )
    variables = [
        (mcp_server._credential_kind_var, "install_credential"),
        (mcp_server._install_uuid_var, INSTALL),
        (
            audit_principal,
            {"credential_id": CREDENTIAL, "credential_kind": "install_credential"},
        ),
    ]
    tokens = [(var, var.set(value)) for var, value in variables]
    try:
        body = parse_envelope(
            await mcp_server.caura_keystones(
                agent_id=PRINCIPAL, fleet_id=resolved_fleet
            )
        )
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
    assert body["rules"] == ROWS
    sc.record_keystone_fetch.assert_awaited_once()
    record = sc.record_keystone_fetch.await_args.args[0]
    assert (record["install_uuid"], record["credential_id"]) == (INSTALL, CREDENTIAL)
    assert (record["principal"], record["fleet_id"], record["rule_set_hash"]) == (
        PRINCIPAL if resolved_fleet else None,
        resolved_fleet,
        HASH,
    )
    sc.list_keystones.assert_awaited_once_with(
        tenant_id=TENANT,
        fleet_id=resolved_fleet,
        agent_id=PRINCIPAL if resolved_fleet else None,
    )


def _recorder(sc, *, capacity=4, timeout=0.02):
    # Import at execution time: the red-first failure names the missing
    # bounded recorder instead of preventing collection of every route test.
    name = "core_api.services.keystone_fetches"
    assert importlib.util.find_spec(name) is not None, (
        "bounded fetch recorder is not implemented"
    )
    module = importlib.import_module(name)
    clock = [NOW]
    recorder = module.KeystoneFetchRecorder(
        clock=lambda: clock[0],
        max_entries=capacity,
        max_inflight=1,
        timeout_seconds=timeout,
    )

    async def record(**changes):
        await recorder.record(
            **{
                "storage": sc,
                "auth": _auth(),
                "tenant_id": TENANT,
                "principal": PRINCIPAL,
                "fleet_id": FLEET,
                "rows": ROWS,
                "truncated": False,
                **changes,
            }
        )

    return recorder, clock, record


async def test_same_minute_coalesces_and_next_minute_records_a_new_first_read():
    sc = _storage()
    _, clock, record = _recorder(sc)
    await record()
    clock[0] = NOW.replace(second=59)
    await record()
    assert sc.record_keystone_fetch.await_count == 1
    first = sc.record_keystone_fetch.await_args.args[0]
    assert datetime.fromisoformat(str(first["served_at"]).replace("Z", "+00:00")) == NOW
    clock[0] = NOW.replace(second=0) + timedelta(minutes=1)
    await record()
    assert sc.record_keystone_fetch.await_count == 2


async def test_concurrent_repeats_share_one_bounded_direct_write():
    sc = _storage()
    _, _, record = _recorder(sc)
    await asyncio.gather(*(record() for _ in range(20)))
    sc.record_keystone_fetch.assert_awaited_once()


@pytest.mark.parametrize(
    "dimension", ["install", "principal", "fleet", "hash", "tenant"]
)
async def test_coalescing_does_not_hide_a_different_binding(dimension):
    sc = _storage()
    _, _, record = _recorder(sc)
    await record()
    changes = {
        "install": {"auth": _auth(install_uuid="33333333-3333-4333-8333-333333333333")},
        "principal": {"principal": "another-recipient"},
        "fleet": {"fleet_id": "another-fleet"},
        "hash": {"rows": []},
        "tenant": {
            "tenant_id": "another-tenant",
            "auth": _auth(tenant_id="another-tenant"),
        },
    }
    await record(**changes[dimension])
    assert sc.record_keystone_fetch.await_count == 2


async def test_failed_bucket_is_counted_once_without_retrying_every_read():
    sc = _storage()
    sc.record_keystone_fetch.side_effect = RuntimeError("synthetic recording outage")
    recorder, clock, record = _recorder(sc)
    await record()
    await record()
    sc.record_keystone_fetch.assert_awaited_once()
    assert recorder.failed_writes == 1
    clock[0] += timedelta(minutes=1)
    await record()
    assert sc.record_keystone_fetch.await_count == recorder.failed_writes == 2


async def test_hung_recording_has_a_deadline_and_does_not_cancel_the_read():
    sc = _storage()

    async def hang(_):
        await asyncio.Event().wait()

    sc.record_keystone_fetch.side_effect = hang
    recorder, _, record = _recorder(sc)
    await asyncio.wait_for(record(), timeout=1)
    assert recorder.failed_writes == 1


async def test_concurrent_distinct_bindings_cannot_exceed_the_inflight_limit():
    sc = _storage()
    active = 0
    peak = 0

    async def hang(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    sc.record_keystone_fetch.side_effect = hang
    _, _, record = _recorder(sc, capacity=20)
    await asyncio.wait_for(
        asyncio.gather(*(record(principal=f"recipient-{i}") for i in range(10))),
        timeout=2,
    )
    assert peak == 1
    assert active == 0


async def test_key_saturation_bounds_attempts_without_evicting_a_live_bucket():
    sc = _storage()
    _, clock, record = _recorder(sc, capacity=2)
    await record(principal="one")
    await record(principal="two")
    for i in range(20):
        await record(principal=f"overflow-{i}")
    await record(principal="one")
    assert sc.record_keystone_fetch.await_count == 2
    clock[0] += timedelta(minutes=1)
    await record(principal="overflow-0")
    assert sc.record_keystone_fetch.await_count == 3


def _check(**changes):
    return {
        "tenant_id": TENANT,
        "install_uuid": INSTALL,
        "principal": PRINCIPAL,
        "fleet_id": FLEET,
        "rule_set_hash": HASH,
        "delivered_at": NOW.isoformat(),
        **changes,
    }


@pytest.fixture
async def match_http(monkeypatch):
    from core_api.app import app
    from core_api.clients import storage_client
    from core_api.config import settings
    from core_api.middleware import standalone_tenant

    # Exercise the service deployment shape without starting the standalone
    # tenant initializer or a database; route authorization still runs.
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(standalone_tenant, "get_standalone_tenant_id", lambda: None)
    sc = _storage()
    monkeypatch.setattr(storage_client, "get_storage_client", lambda: sc)
    name = "core_api.routes.keystone_fetches"
    if importlib.util.find_spec(name) is not None:
        monkeypatch.setattr(
            importlib.import_module(name), "get_storage_client", lambda: sc
        )
    auth = [AuthContext(tenant_id=None, is_admin=True)]

    async def resolve_auth():
        return auth[0]

    previous = app.dependency_overrides.get(get_auth_context)
    app.dependency_overrides[get_auth_context] = resolve_auth
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http, sc, auth
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_auth_context, None)
        else:
            app.dependency_overrides[get_auth_context] = previous


async def test_internal_match_is_registered_and_returns_explicit_missing_evidence(
    match_http,
):
    http, sc, _ = match_http
    response = await http.post(
        "/api/v1/internal/keystone-fetches/match", json={"checks": [_check()]}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"results": [{"status": "no_matching_fetch"}]}
    sc.match_keystone_fetches.assert_awaited_once()


@pytest.mark.parametrize("kind", ["install", "agent", "org-admin", "tenant-key"])
async def test_match_is_not_an_install_agent_or_org_admin_surface(match_http, kind):
    http, sc, auth = match_http
    auth[0] = {
        "install": _auth(),
        "agent": _auth(
            is_install_credential=False, agent_id=PRINCIPAL, agent_id_verified=True
        ),
        "org-admin": _auth(
            is_install_credential=False, org_role="admin", is_person=True
        ),
        "tenant-key": _auth(
            is_install_credential=False, credential_kind="user_api_key"
        ),
    }[kind]
    response = await http.post(
        "/api/v1/internal/keystone-fetches/match", json={"checks": [_check()]}
    )
    assert response.status_code == 403, response.text
    sc.match_keystone_fetches.assert_not_awaited()


@pytest.mark.parametrize("count", [0, 101])
async def test_internal_match_rejects_unbounded_or_empty_batches(match_http, count):
    http, sc, _ = match_http
    response = await http.post(
        "/api/v1/internal/keystone-fetches/match", json={"checks": [_check()] * count}
    )
    assert response.status_code == 422, response.text
    sc.match_keystone_fetches.assert_not_awaited()


async def test_internal_match_accepts_100_checks(match_http):
    http, sc, _ = match_http
    sc.match_keystone_fetches.return_value = {
        "results": [{"status": "no_matching_fetch"}] * 100
    }
    response = await http.post(
        "/api/v1/internal/keystone-fetches/match", json={"checks": [_check()] * 100}
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 100


async def test_match_cannot_choose_a_wider_window(match_http):
    http, sc, _ = match_http
    response = await http.post(
        "/api/v1/internal/keystone-fetches/match",
        json={"checks": [_check(window_seconds=86400)]},
    )
    assert response.status_code == 422, response.text
    sc.match_keystone_fetches.assert_not_awaited()


async def test_match_outage_is_unavailable_and_never_a_verified_result(match_http):
    http, sc, _ = match_http
    sc.match_keystone_fetches.side_effect = RuntimeError("synthetic matching outage")
    response = await http.post(
        "/api/v1/internal/keystone-fetches/match", json={"checks": [_check()]}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"results": [{"status": "evidence_unavailable"}]}


async def test_storage_client_groups_by_tenant_and_preserves_check_order():
    from core_api.clients.storage_client import CoreStorageClient

    client = CoreStorageClient.__new__(CoreStorageClient)

    async def post(path, body, **kwargs):
        assert path == "/keystone-fetches/match"
        assert kwargs["idempotent"] is True
        assert all(check["tenant_id"] == body["tenant_id"] for check in body["checks"])
        return {
            "results": [{"principal": check["principal"]} for check in body["checks"]]
        }

    client._post = AsyncMock(side_effect=post)
    checks = [
        _check(principal="one"),
        _check(tenant_id="another-tenant", principal="two"),
        _check(principal="three"),
    ]
    result = await client.match_keystone_fetches({"checks": checks})
    assert client._post.await_count == 2
    assert [r["principal"] for r in result["results"]] == ["one", "two", "three"]


@pytest.mark.parametrize("results", [[], [None]])
async def test_storage_client_refuses_incomplete_or_invalid_results(results):
    from core_api.clients.storage_client import CoreStorageClient

    client = CoreStorageClient.__new__(CoreStorageClient)
    client._post = AsyncMock(return_value={"results": results})
    with pytest.raises(ValueError):
        await client.match_keystone_fetches({"checks": [_check()]})
