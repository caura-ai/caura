"""Bounded receipt matching uses protected original provenance on reads.

The enterprise transport and identity provider are synthetic. Route cases use
the real REST/storage paths; inputs and stored rows must not be rewritten by
validation, and an unavailable lookup must not turn into a positive claim.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from core_api.services.memory_service import _memory_to_out
from core_api.services.system_metadata import sanitize_caller_metadata
from tests.test_rules_receipt import as_auth as as_auth
from tests.test_rules_receipt_origin import (
    AGENT,
    INSTALL,
    OTHER_AGENT,
    OTHER_INSTALL,
    RECEIPT,
    SESSION,
    _broker,
    _created,
    _item,
    _metadata,
)

TENANT = "receipt-reader-tenant"
BASE_URL = "https://receipt-storage.invalid"
DELIVERED = "2026-11-11T10:00:00Z"
STATE = "rules_receipt_validation"


def _module(name):
    assert importlib.util.find_spec(name) is not None, f"{name} is not implemented"
    return importlib.import_module(name)


def _client_module():
    return _module("core_api.clients.session_receipts")


def _adapter_module():
    return _module("core_api.services.receipt_validation")


def _check(**changes):
    return {
        "event_id": RECEIPT["event_id"],
        "install_id": INSTALL,
        "session_id": SESSION,
        "agent_id": AGENT,
        "rule_set_hash": RECEIPT["rule_set_hash"],
        **changes,
    }


def _row(tenant=TENANT, **changes):
    return {
        "id": str(uuid4()),
        "tenant_id": tenant,
        "agent_id": OTHER_AGENT,
        "memory_type": "fact",
        "content": "The maintenance window is Thursday.",
        "weight": 1.0,
        "created_at": datetime(2026, 11, 11, tzinfo=UTC),
        "expires_at": None,
        "verification_status": "human_verified",
        "status": "active",
        "visibility": "scope_team",
        "recall_count": 0,
        "metadata": {
            "session_id": "editable-later-session",
            "agent_id": OTHER_AGENT,
            "_system": {
                "rules_receipt": dict(RECEIPT),
                "rules_receipt_origin": {
                    "version": 1,
                    "tenant_id": tenant,
                    "install_id": INSTALL,
                    "session_id": SESSION,
                    "agent_id": AGENT,
                },
            },
        },
        **changes,
    }


def _state(row):
    system = (
        row.get("system_metadata") if isinstance(row, dict) else row.system_metadata
    )
    return (system or {}).get(STATE)


def _matching_client():
    client = AsyncMock()

    async def match(tenant_id, checks):
        return [
            {
                "event_id": check["event_id"],
                "status": "matched",
                "delivered_at": DELIVERED,
            }
            for check in checks
        ]

    client.match.side_effect = match
    return client


def _install_lookup(monkeypatch):
    client = _matching_client()
    # Missing support must not skip exercising the existing read boundary.
    if importlib.util.find_spec("core_api.services.receipt_validation") is not None:
        module = _adapter_module()
        monkeypatch.setattr(module, "get_session_receipt_client", lambda: client)
    return client


@pytest.mark.unit
def test_caller_cannot_supply_a_validation_result():
    assert sanitize_caller_metadata(
        {STATE: {"status": "matched"}, "note": "caller"}
    ) == {"note": "caller"}


@pytest.mark.unit
@pytest.mark.parametrize("status", ["matched", "not_found", "mismatch", "unverifiable"])
async def test_lookup_uses_service_identity_and_the_exact_bounded_contract(
    monkeypatch, status
):
    module = _client_module()
    token = AsyncMock(return_value={"Authorization": "Bearer synthetic-service-token"})
    monkeypatch.setattr(module, "fetch_auth_header", token)
    requests = []

    def handler(request):
        requests.append(request)
        result = {"event_id": RECEIPT["event_id"], "status": status}
        if status == "matched":
            result["delivered_at"] = DELIVERED
        if status == "unverifiable":
            result["reason"] = "missing_binding"
        return httpx.Response(200, json={"results": [result]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http
        )
        results = await client.match(TENANT, [_check()])
    [request] = requests
    assert request.method == "POST"
    assert (
        request.url
        == f"{BASE_URL}/api/v1/storage/tenants/{TENANT}/session-receipts/match"
    )
    assert json.loads(request.content) == {"checks": [_check()]}
    assert request.headers["Authorization"] == "Bearer synthetic-service-token"
    assert (
        "x-api-key" not in request.headers and "x-storage-secret" not in request.headers
    )
    token.assert_awaited_once_with(BASE_URL)
    assert results[0]["status"] == status


@pytest.mark.unit
async def test_lookup_accepts_100_checks_with_order_preserved(monkeypatch):
    module = _client_module()
    monkeypatch.setattr(
        module,
        "fetch_auth_header",
        AsyncMock(return_value={"Authorization": "Bearer synthetic"}),
    )
    checks = [_check(event_id=str(uuid4())) for _ in range(100)]

    def handler(request):
        body = json.loads(request.content)
        assert body["checks"] == checks
        return httpx.Response(
            200,
            json={
                "results": [
                    {"event_id": c["event_id"], "status": "not_found"} for c in checks
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http
        )
        results = await client.match(TENANT, checks)
    assert [r["event_id"] for r in results] == [c["event_id"] for c in checks]


@pytest.mark.unit
@pytest.mark.parametrize("count", [0, 101])
async def test_lookup_refuses_invalid_batch_sizes_before_auth_or_network(
    monkeypatch, count
):
    module = _client_module()
    token = AsyncMock()
    monkeypatch.setattr(module, "fetch_auth_header", token)
    network = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: network.append(request))
    ) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http
        )
        with pytest.raises(ValueError):
            await client.match(TENANT, [_check()] * count)
    token.assert_not_awaited()
    assert network == []


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["url", "audience", "token"])
async def test_lookup_never_falls_back_to_anonymous_auth(monkeypatch, missing):
    module = _client_module()
    monkeypatch.setattr(
        module,
        "fetch_auth_header",
        AsyncMock(
            return_value={}
            if missing == "token"
            else {"Authorization": "Bearer synthetic"}
        ),
    )
    network = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: network.append(request))
    ) as http:
        client = module.SessionReceiptMatchClient(
            base_url="" if missing == "url" else BASE_URL,
            audience="" if missing == "audience" else BASE_URL,
            http_client=http,
        )
        with pytest.raises(module.ReceiptLookupUnavailable):
            await client.match(TENANT, [_check()])
    assert network == []


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403, 503])
async def test_lookup_dependency_errors_are_unavailable_without_logging_details(
    monkeypatch, caplog, status
):
    module = _client_module()
    monkeypatch.setattr(
        module,
        "fetch_auth_header",
        AsyncMock(return_value={"Authorization": "Bearer synthetic-private-token"}),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="synthetic-private-response")
        )
    ) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http
        )
        with pytest.raises(module.ReceiptLookupUnavailable):
            await client.match(TENANT, [_check()])
    assert "synthetic-private-token" not in caplog.text
    assert "synthetic-private-response" not in caplog.text


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"results": []},
        {"results": [{"event_id": str(uuid4()), "status": "not_found"}]},
        {"results": [{"event_id": RECEIPT["event_id"], "status": "unknown"}]},
        {"results": [{"event_id": RECEIPT["event_id"], "status": "matched"}]},
        {
            "results": [
                {
                    "event_id": RECEIPT["event_id"],
                    "status": "matched",
                    "delivered_at": "2026-11-11T10:00:00",
                }
            ]
        },
        {
            "results": [
                {
                    "event_id": RECEIPT["event_id"],
                    "status": "not_found",
                    "install_id": OTHER_INSTALL,
                }
            ]
        },
    ],
    ids=[
        "missing-results",
        "wrong-count",
        "wrong-event",
        "unknown-state",
        "missing-time",
        "naive-time",
        "foreign-fields",
    ],
)
async def test_lookup_refuses_malformed_or_misbound_results(monkeypatch, response):
    module = _client_module()
    monkeypatch.setattr(
        module,
        "fetch_auth_header",
        AsyncMock(return_value={"Authorization": "Bearer synthetic"}),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http
        )
        with pytest.raises(module.ReceiptLookupUnavailable):
            await client.match(TENANT, [_check()])


@pytest.mark.unit
@pytest.mark.parametrize("where", ["identity", "http"])
async def test_lookup_deadline_covers_identity_and_http(monkeypatch, where):
    module = _client_module()
    blocked = asyncio.Event()

    async def identity(_):
        if where == "identity":
            await blocked.wait()
        return {"Authorization": "Bearer synthetic"}

    async def handler(_):
        await blocked.wait()
        return httpx.Response(200)

    monkeypatch.setattr(module, "fetch_auth_header", identity)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http, timeout_seconds=0.02
        )
        with pytest.raises(module.ReceiptLookupUnavailable):
            await asyncio.wait_for(client.match(TENANT, [_check()]), timeout=0.5)


@pytest.mark.unit
@pytest.mark.parametrize("shape", ["public-dict", "storage-dict", "model"])
async def test_validation_uses_protected_origin_and_does_not_mutate_memory(shape):
    module = _adapter_module()
    row = _row()
    if shape == "storage-dict":
        row["metadata_"] = row.pop("metadata")
    elif shape == "model":
        row = _memory_to_out(row)
    before = deepcopy(row)
    client = _matching_client()
    [result] = await module.annotate_memory_receipts(
        [row], authorized_tenant_ids={TENANT}, client=client
    )
    client.match.assert_awaited_once_with(TENANT, [_check()])
    assert _state(result)["status"] == "matched"
    assert row == before
    assert result is not row
    assert (
        result["verification_status"]
        if isinstance(result, dict)
        else result.verification_status
    ) == "human_verified"


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "legacy",
        "top-level-origin",
        "derived-origin",
        "wrong-tenant",
        "agent-label",
        "boolean-version",
        "null-hash",
    ],
)
async def test_untrusted_or_incomplete_binding_never_becomes_a_match(bad):
    module = _adapter_module()
    row = _row()
    system = row["metadata"]["_system"]
    if bad in {"legacy", "top-level-origin", "derived-origin"}:
        origin = system.pop("rules_receipt_origin")
        if bad == "top-level-origin":
            row["metadata"]["rules_receipt_origin"] = origin
        if bad == "derived-origin":
            row["system_metadata"] = {"rules_receipt_origin": origin}
    elif bad == "wrong-tenant":
        system["rules_receipt_origin"]["tenant_id"] = "foreign-tenant"
    elif bad == "agent-label":
        system["rules_receipt_origin"]["agent_id"] = "claude-code"
    elif bad == "boolean-version":
        system["rules_receipt_origin"]["version"] = True
    else:
        system["rules_receipt"]["rule_set_hash"] = None
    row.setdefault("system_metadata", {})[STATE] = {"status": "matched"}
    client = _matching_client()
    [result] = await module.annotate_memory_receipts(
        [row], authorized_tenant_ids={TENANT}, client=client
    )
    assert _state(result)["status"] == "unverifiable"
    client.match.assert_not_awaited()


@pytest.mark.unit
async def test_absent_receipt_cannot_keep_a_forged_positive_state():
    module = _adapter_module()
    row = _row(
        metadata={"note": "caller"}, system_metadata={STATE: {"status": "matched"}}
    )
    client = _matching_client()
    [result] = await module.annotate_memory_receipts(
        [row], authorized_tenant_ids={TENANT}, client=client
    )
    assert (_state(result) or {}).get("status") != "matched"
    client.match.assert_not_awaited()


@pytest.mark.unit
async def test_authorization_precedes_any_receipt_lookup():
    module = _adapter_module()
    client = _matching_client()
    with pytest.raises(HTTPException) as exc:
        await module.annotate_memory_receipts(
            [_row("foreign")], authorized_tenant_ids={TENANT}, client=client
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "TENANT_MISMATCH"
    client.match.assert_not_awaited()


@pytest.mark.unit
async def test_cross_tenant_batches_use_stored_tenants_and_preserve_order():
    module = _adapter_module()
    rows = [_row(), _row("sibling-tenant"), _row()]
    client = _matching_client()
    results = await module.annotate_memory_receipts(
        rows, authorized_tenant_ids={TENANT, "sibling-tenant"}, client=client
    )
    assert [r["id"] for r in results] == [r["id"] for r in rows]
    assert all(_state(r)["status"] == "matched" for r in results)
    assert {call.args[0] for call in client.match.await_args_list} == {
        TENANT,
        "sibling-tenant",
    }
    assert all(len(call.args[1]) <= 100 for call in client.match.await_args_list)


@pytest.mark.unit
async def test_one_event_with_two_expected_sessions_is_not_collapsed():
    module = _adapter_module()
    rows = [_row(), _row()]
    rows[1]["metadata"]["_system"]["rules_receipt_origin"]["session_id"] = (
        "another-original-session"
    )
    client = _matching_client()

    async def match(_, checks):
        return [
            {"event_id": c["event_id"], "status": "matched", "delivered_at": DELIVERED}
            if c["session_id"] == SESSION
            else {"event_id": c["event_id"], "status": "mismatch"}
            for c in checks
        ]

    client.match.side_effect = match
    results = await module.annotate_memory_receipts(
        rows, authorized_tenant_ids={TENANT}, client=client
    )
    assert [_state(r)["status"] for r in results] == ["matched", "mismatch"]


@pytest.mark.unit
async def test_large_page_is_split_into_at_most_100_checks_per_request():
    module = _adapter_module()
    rows = [_row() for _ in range(250)]
    for row in rows:
        row["metadata"]["_system"]["rules_receipt"]["event_id"] = str(uuid4())
    client = _matching_client()
    results = await module.annotate_memory_receipts(
        rows, authorized_tenant_ids={TENANT}, client=client
    )
    assert len(results) == 250 and all(
        _state(r)["status"] == "matched" for r in results
    )
    assert all(1 <= len(call.args[1]) <= 100 for call in client.match.await_args_list)
    assert sum(len(call.args[1]) for call in client.match.await_args_list) == 250


@pytest.mark.unit
async def test_total_lookup_volume_is_bounded_and_unattempted_rows_are_unavailable():
    module = _adapter_module()
    rows = [_row(f"tenant-{i}") for i in range(12)]
    client = _matching_client()
    results = await module.annotate_memory_receipts(
        rows, authorized_tenant_ids={r["tenant_id"] for r in rows}, client=client
    )
    assert len(results) == 12
    assert client.match.await_count <= 10
    assert any(_state(r)["status"] == "evidence_unavailable" for r in results)


@pytest.mark.unit
async def test_lookup_outage_preserves_reads_and_never_claims_a_match():
    module = _adapter_module()
    client = _matching_client()
    client.match.side_effect = RuntimeError("synthetic lookup outage")
    row = _row()
    [result] = await module.annotate_memory_receipts(
        [row], authorized_tenant_ids={TENANT}, client=client
    )
    assert result["content"] == row["content"]
    assert _state(result)["status"] == "evidence_unavailable"


@pytest.mark.unit
async def test_overall_deadline_bounds_a_hung_dependency():
    module = _adapter_module()
    client = _matching_client()

    async def blocked(*_):
        await asyncio.Event().wait()

    client.match.side_effect = blocked
    [result] = await asyncio.wait_for(
        module.annotate_memory_receipts(
            [_row()],
            authorized_tenant_ids={TENANT},
            client=client,
            timeout_seconds=0.02,
        ),
        timeout=0.5,
    )
    assert _state(result)["status"] == "evidence_unavailable"


@pytest.mark.parametrize("surface", ["detail", "list", "held", "admin"])
async def test_rest_reads_match_the_original_writer_not_the_current_reader(
    client, as_auth, sc, tenant_id, monkeypatch, surface
):
    lookup = _install_lookup(monkeypatch)
    _broker(as_auth, tenant_id)
    item = _item(_metadata())
    if surface == "held":
        item["status"] = "quarantined"
    memory_id = await _created(client, tenant_id, item)
    stored = await sc.get_memory(memory_id, tenant_id, include_held=True, read=False)
    before = deepcopy(stored["metadata_"])
    if surface == "admin":
        as_auth(tenant_id, is_admin=True)
        url = f"/api/v1/admin/memories?tenant_id={tenant_id}"
    elif surface == "held":
        as_auth(tenant_id, is_person=True, user_id="reviewer", org_role="admin")
        url = f"/api/v1/memories/held?tenant_id={tenant_id}"
    else:
        _broker(as_auth, tenant_id, install=OTHER_INSTALL)
        url = (
            f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}"
            if surface == "detail"
            else f"/api/v1/memories?tenant_id={tenant_id}"
        )
    response = await client.get(url)
    assert response.status_code == 200, response.text
    payload = response.json()
    memory = (
        payload
        if surface == "detail"
        else next(row for row in payload["items"] if row["id"] == memory_id)
    )
    assert (_state(memory) or {}).get("status") == "matched"
    assert lookup.match.await_count >= 1
    assert all(
        call.args[0] == tenant_id
        and all(
            check["install_id"] == INSTALL
            and check["session_id"] == SESSION
            and check["agent_id"] == AGENT
            for check in call.args[1]
        )
        for call in lookup.match.await_args_list
    )
    assert (await sc.get_memory(memory_id, tenant_id, include_held=True, read=False))[
        "metadata_"
    ] == before


@pytest.mark.unit
@pytest.mark.parametrize("pipeline", [False, True], ids=["legacy", "pipeline"])
async def test_both_search_paths_validate_before_recall_serialization(
    monkeypatch, pipeline
):
    from core_api.pipeline.steps.search import record_recall_value
    from core_api.services import memory_exposure, memory_service, recall_service

    lookup = _install_lookup(monkeypatch)
    original = _memory_to_out(_row())
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", pipeline)
    monkeypatch.setattr(
        memory_service, "_search_memories_pipeline", AsyncMock(return_value=[original])
    )
    monkeypatch.setattr(
        memory_service, "_search_memories_legacy", AsyncMock(return_value=[original])
    )
    monkeypatch.setattr(
        record_recall_value, "schedule_recall_value", lambda *a, **k: None
    )
    monkeypatch.setattr(memory_exposure, "schedule_exposure", lambda *a, **k: None)
    [result] = await memory_service.search_memories(
        TENANT, "maintenance", caller_tenant_id=TENANT, readable_tenant_ids=[TENANT]
    )
    assert (_state(result) or {}).get("status") == "matched"
    [dumped] = recall_service._dump_rows([result])
    assert _state(dumped)["status"] == "matched"
    assert "_system" not in (dumped.get("metadata") or {})
    lookup.match.assert_awaited_once_with(TENANT, [_check()])


@pytest.mark.unit
async def test_mcp_list_validates_its_served_memories(monkeypatch):
    from core_api import mcp_server
    from tests._mcp_test_helpers import parse_envelope

    lookup = _install_lookup(monkeypatch)
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: TENANT)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: OTHER_AGENT)
    monkeypatch.setattr(mcp_server, "_get_readable_tenants", lambda: [TENANT])
    monkeypatch.setattr(mcp_server, "_refuse_default_agent_on_gateway", lambda _: None)
    monkeypatch.setattr(
        mcp_server, "_refuse_unowned_agent", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        mcp_server, "_resolve_read_fleet_gate", AsyncMock(return_value=(1, None))
    )
    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None))
    )
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock(return_value=None))
    sc = SimpleNamespace(list_memories_by_filters=AsyncMock(return_value=[_row()]))
    monkeypatch.setattr(mcp_server, "get_storage_client", lambda: sc)
    payload = parse_envelope(await mcp_server.caura_list(agent_id=OTHER_AGENT))
    [memory] = payload["items"]
    assert (_state(memory) or {}).get("status") == "matched"
    lookup.match.assert_awaited_once_with(TENANT, [_check()])


@pytest.mark.unit
async def test_entity_linked_memory_read_has_receipt_state(monkeypatch):
    from core_api.services import entity_service

    lookup = _install_lookup(monkeypatch)
    entity_id = uuid4()
    sc = SimpleNamespace(
        get_entity_with_linked_memories=AsyncMock(
            return_value={
                "entity": {
                    "id": str(entity_id),
                    "tenant_id": TENANT,
                    "entity_type": "person",
                    "canonical_name": "Reader",
                    "attributes": {},
                },
                "linked_memories": [{"memory": _row()}],
            }
        ),
        get_outgoing_relations=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(entity_service, "get_storage_client", lambda: sc)
    result = await entity_service.get_entity(
        entity_id, TENANT, caller_agent_id=None, caller_tenant_id=TENANT
    )
    assert result is not None
    [memory] = result.linked_memories
    assert (_state(memory) or {}).get("status") == "matched"
    lookup.match.assert_awaited_once_with(TENANT, [_check()])


async def test_denied_tenant_read_does_not_call_receipt_lookup(
    client, as_auth, monkeypatch, tenant_id
):
    lookup = _install_lookup(monkeypatch)
    as_auth(tenant_id, is_install_credential=True, install_uuid=OTHER_INSTALL)
    response = await client.get(f"/api/v1/memories/{uuid4()}?tenant_id=foreign-tenant")
    assert response.status_code == 403, response.text
    lookup.match.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("status", ["matched", "not_found", "mismatch", "unverifiable"])
async def test_read_state_preserves_the_dependency_verdict_without_upgrading_it(status):
    module = _adapter_module()
    result = {"event_id": RECEIPT["event_id"], "status": status}
    if status == "matched":
        result["delivered_at"] = DELIVERED
    elif status == "unverifiable":
        result["reason"] = "missing_binding"
    client = _matching_client()
    client.match.side_effect = None
    client.match.return_value = [result]
    row = _row()
    [annotated] = await module.annotate_memory_receipts(
        [row], authorized_tenant_ids={TENANT}, client=client
    )
    assert _state(annotated) == {k: v for k, v in result.items() if k != "event_id"}
    assert annotated["verification_status"] == row["verification_status"]


@pytest.mark.unit
@pytest.mark.parametrize("response", ["oversized", "redirect"])
async def test_lookup_bounds_response_size_and_never_forwards_identity_on_redirect(
    monkeypatch, response
):
    module = _client_module()
    monkeypatch.setattr(
        module,
        "fetch_auth_header",
        AsyncMock(return_value={"Authorization": "Bearer synthetic"}),
    )
    requests = []

    def handler(request):
        requests.append(request)
        if response == "redirect":
            return httpx.Response(307, headers={"Location": "https://foreign.invalid/"})
        return httpx.Response(200, content=b" " * 65537)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http
        )
        with pytest.raises(module.ReceiptLookupUnavailable):
            await client.match(TENANT, [_check()])
    assert len(requests) == 1 and requests[0].url.host == "receipt-storage.invalid"


@pytest.mark.unit
async def test_timed_out_reads_share_one_pending_identity_refresh(monkeypatch):
    module = _client_module()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls = []

    async def identity(_):
        calls.append(True)
        try:
            await release.wait()
            return {"Authorization": "Bearer synthetic"}
        finally:
            finished.set()

    monkeypatch.setattr(module, "fetch_auth_header", identity)
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http, timeout_seconds=0.01
        )
        try:
            for _ in range(3):
                with pytest.raises(module.ReceiptLookupUnavailable):
                    await client.match(TENANT, [_check()])
            assert calls == [True] and not finished.is_set()
            assert requests == []
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=0.5)


@pytest.mark.unit
async def test_busy_client_refuses_extra_work_without_starting_more_identity_tasks(
    monkeypatch,
):
    module = _client_module()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def identity(_):
        return {"Authorization": "Bearer synthetic"}

    async def handler(_):
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "results": [{"event_id": RECEIPT["event_id"], "status": "not_found"}]
            },
        )

    token = AsyncMock(side_effect=identity)
    monkeypatch.setattr(module, "fetch_auth_header", token)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = module.SessionReceiptMatchClient(
            base_url=BASE_URL, audience=BASE_URL, http_client=http, timeout_seconds=1
        )
        pending = [
            asyncio.create_task(client.match(TENANT, [_check()])) for _ in range(8)
        ]
        try:
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            with pytest.raises(module.ReceiptLookupUnavailable):
                await client.match(TENANT, [_check()])
            token.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(*pending)
