"""Request boundaries and isolated resource budgets."""

import asyncio
import signal
import socket
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from core_api.auth import AuthContext, get_auth_context
from core_api.routes import entities, memories
from core_api.routes.documents import DocWriteRequest
from core_api.routes.keystones import KeystoneSetRequest
from core_api.schemas import EntityUpsert, MemoryCreate, MemoryUpdate
from core_api.services import ingest_service, memory_field
from core_storage_api.routers.keystones import _validate_payload

pytestmark = pytest.mark.unit


def test_html_openers_finish_within_one_second():
    def expired(*_):
        raise TimeoutError("HTML stripping exceeded one second")

    old = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 1)
    started = time.monotonic()
    try:
        assert ingest_service.decode_text_body(b"<script>" * 100000, "text/html") == ""
        assert time.monotonic() - started < 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("value", ["<style>" * 100000, "<" * 100000])
def test_other_html_openers_are_bounded(value):
    def expired(*_):
        raise TimeoutError("HTML stripping exceeded one second")

    old = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 1)
    started = time.monotonic()
    try:
        ingest_service.decode_text_body(value.encode(), "text/html")
        assert time.monotonic() - started < 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("field,cap", [("title", 200), ("content", 2000)])
def test_keystone_rest_and_storage_cap_at_limit_plus_one(field, cap):
    body = {
        "tenant_id": "t",
        "doc_id": "rule",
        "title": "Title",
        "content": "Rule",
        "scope": "tenant",
        "weight": "high",
    }
    KeystoneSetRequest(**{**body, field: "x" * cap})
    _validate_payload({**body, field: "x" * cap})
    with pytest.raises(ValidationError):
        KeystoneSetRequest(**{**body, field: "x" * (cap + 1)})
    with pytest.raises(HTTPException) as exc:
        _validate_payload({**body, field: "x" * (cap + 1)})
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    "model,body,field",
    [
        (MemoryCreate, {"tenant_id": "t", "content": "text"}, "metadata"),
        (MemoryUpdate, {}, "metadata"),
        (
            EntityUpsert,
            {"tenant_id": "t", "entity_type": "person", "canonical_name": "Name"},
            "attributes",
        ),
        (
            DocWriteRequest,
            {"tenant_id": "t", "collection": "notes", "doc_id": "one"},
            "data",
        ),
    ],
)
def test_json_fields_cap_serialized_utf8_bytes(model, body, field):
    # Compact UTF-8 JSON for {"x":"..."} has eight bytes of overhead.
    model(**{**body, field: {"x": "x" * (65536 - 8)}})
    with pytest.raises(ValidationError):
        model(**{**body, field: {"x": "x" * (65536 - 7)}})
    with pytest.raises(ValidationError):
        model(**{**body, field: {"x": "é" * 32765}})


@pytest.mark.parametrize(
    "model,field,cap",
    [
        (MemoryCreate, "source_uri", 2048),
        (MemoryCreate, "run_id", 200),
        (MemoryCreate, "predicate", 200),
        (MemoryCreate, "object_value", 10000),
        (MemoryUpdate, "title", 500),
        (MemoryUpdate, "source_uri", 2048),
        (MemoryUpdate, "predicate", 200),
        (MemoryUpdate, "object_value", 10000),
    ],
)
def test_memory_text_fields_at_limit_plus_one(model, field, cap):
    body = {"tenant_id": "t", "content": "text"} if model is MemoryCreate else {}
    model(**{**body, field: "x" * cap})
    with pytest.raises(ValidationError):
        model(**{**body, field: "x" * (cap + 1)})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,path,param,cap",
    [
        (memories, "/memories", "offset", 10000),
        (entities, "/entities", "search", 200),
    ],
)
async def test_public_list_boundaries(monkeypatch, module, path, param, cap):
    sc = SimpleNamespace(
        list_memories_by_filters=AsyncMock(return_value=[]),
        list_entities=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(module, "get_storage_client", lambda: sc)
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        tenant_id="t", readable_tenant_ids=["t"]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        value = cap if param == "offset" else "x" * cap
        assert (
            await c.get(path, params={"tenant_id": "t", param: value})
        ).status_code == 200
        value = cap + 1 if param == "offset" else "x" * (cap + 1)
        assert (
            await c.get(path, params={"tenant_id": "t", param: value})
        ).status_code == 422


@pytest.mark.asyncio
async def test_field_signature_is_cached_for_sequential_requests(monkeypatch):
    sc = SimpleNamespace(memory_field_signature=AsyncMock(return_value={"count": 0}))
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: sc)
    memory_field._signatures.clear()
    try:
        await memory_field._signature({"tenant_id": "cache-test"})
        await memory_field._signature({"tenant_id": "cache-test"})
        assert sc.memory_field_signature.await_count == 1
    finally:
        memory_field._signatures.clear()


@pytest.mark.asyncio
async def test_field_builds_have_independent_tenant_slots(monkeypatch):
    monkeypatch.setattr(
        memory_field, "_signature", AsyncMock(return_value={"count": 0})
    )
    monkeypatch.setattr(memory_field.settings, "memory_field_max_concurrent_builds", 1)
    entered = []
    release = asyncio.Event()

    async def build(scope, *_args, **_kwargs):
        entered.append(scope["tenant_id"])
        await release.wait()
        return {"points": [], "clusters": [], "entities": []}

    monkeypatch.setattr(memory_field, "_build", build)
    tasks = [
        asyncio.create_task(memory_field.get_memory_field({"tenant_id": t}, limit=1))
        for t in ("one", "two")
    ]
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert set(entered) == {"one", "two"}
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        memory_field._builds.clear()
        memory_field._building_tenants.clear()


def test_resolver_errors_do_not_disclose_addresses_or_dns_details(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_a: [(2, 1, 6, "", ("10.8.0.5", 0))]
    )
    with pytest.raises(HTTPException) as blocked:
        ingest_service._resolve_and_vet("https://internal.example/")
    assert blocked.value.detail == "URL not allowed"

    def failed(*_a):
        raise socket.gaierror("resolver-internal-detail")

    monkeypatch.setattr(socket, "getaddrinfo", failed)
    with pytest.raises(HTTPException) as missing:
        ingest_service._resolve_and_vet("https://missing.example/")
    assert missing.value.detail == "URL not allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,cap", [("title", 200), ("content", 2000)])
async def test_keystone_mcp_rejects_oversized_fields(mcp_env, monkeypatch, field, cap):
    from core_api import mcp_server
    from tests._mcp_test_helpers import parse_envelope
    from tests.test_mcp_keystones import _stub_storage_client

    sc = _stub_storage_client(
        monkeypatch,
        get_document=None,
        upsert_keystone={
            "id": "11111111-1111-4111-8111-111111111111",
            "doc_id": "rule",
        },
    )
    body = {
        "op": "set",
        "doc_id": "rule",
        "title": "Title",
        "content": "Rule",
        "scope": "tenant",
        "weight": "high",
    }
    result = parse_envelope(
        await mcp_server.caura_keystones_set(**{**body, field: "x" * cap})
    )
    assert result["ok"] is True
    sc.upsert_keystone.reset_mock()
    result = parse_envelope(
        await mcp_server.caura_keystones_set(**{**body, field: "x" * (cap + 1)})
    )
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    sc.upsert_keystone.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_dns_calls_keep_their_tenant_slots(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from core_api.tenant_context import set_current_tenant

    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=3)
    monkeypatch.setattr(ingest_service, "_DNS_EXECUTOR", pool)
    monkeypatch.setattr(ingest_service, "DNS_RESOLUTION_TIMEOUT", 0.02)
    monkeypatch.setattr(
        ingest_service,
        "_resolve_and_vet",
        lambda _url: (release.wait(2), ["8.8.8.8"])[1],
    )
    set_current_tenant("dns-tenant")
    try:
        for _ in range(2):
            with pytest.raises(HTTPException):
                await ingest_service._resolve_and_vet_async("https://example.com")
        with pytest.raises(HTTPException) as exc:
            await ingest_service._resolve_and_vet_async("https://example.com")
        assert exc.value.status_code == 429
        release.set()
        set_current_tenant("other-dns-tenant")
        assert await ingest_service._resolve_and_vet_async("https://example.com") == [
            "8.8.8.8"
        ]
    finally:
        release.set()
        set_current_tenant(None)
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_field_rate_limit_runs_before_signature(monkeypatch):
    signature = AsyncMock()
    monkeypatch.setattr(memory_field, "_signature", signature)
    monkeypatch.setattr(memory_field.limiter, "enabled", True)
    monkeypatch.setattr(memory_field.limiter.limiter, "hit", lambda *_a: False)
    with pytest.raises(HTTPException) as exc:
        await memory_field.get_memory_field({"tenant_id": "limited"}, limit=1)
    assert exc.value.status_code == 429
    signature.assert_not_awaited()


@pytest.mark.asyncio
async def test_field_signature_refreshes_after_ttl(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(memory_field.time, "monotonic", lambda: now[0])
    memory_field._signature_cache.clear()
    sc = SimpleNamespace(memory_field_signature=AsyncMock(return_value={"count": 0}))
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: sc)
    scope = {"tenant_id": "ttl-test"}
    await memory_field._signature(scope)
    now[0] += 5.01
    await memory_field._signature(scope)
    assert sc.memory_field_signature.await_count == 2
    memory_field._signature_cache.clear()


@pytest.mark.asyncio
async def test_field_limit_variants_share_the_same_build(monkeypatch):
    memory_field.field_cache.clear()
    memory_field._build_started.clear()
    monkeypatch.setattr(
        memory_field, "_signature", AsyncMock(return_value={"count": 1})
    )
    released = asyncio.Event()
    started = asyncio.Event()

    async def build(*_a, **_kw):
        started.set()
        await released.wait()
        return {
            "points": [{"id": "one", "cluster": -1, "entity_ids": []}],
            "clusters": [],
            "entities": [],
        }

    built = AsyncMock(side_effect=build)
    monkeypatch.setattr(memory_field, "_build", built)
    scope = {"tenant_id": "limit-variants"}
    first = asyncio.create_task(memory_field.get_memory_field(scope, limit=1))
    await started.wait()
    second = asyncio.create_task(memory_field.get_memory_field(scope, limit=2))
    await asyncio.sleep(0)
    released.set()
    await asyncio.gather(first, second)
    assert built.await_count == 1
    assert built.await_args.kwargs["limit"] == memory_field.FIELD_MAX_LIMIT
    memory_field._build_started.clear()


@pytest.mark.asyncio
async def test_memory_metadata_merge_cannot_grow_past_the_stored_budget(monkeypatch):
    from uuid import uuid4

    from core_storage_api.services import postgres_service

    # Each incoming patch fits individually, but their stored union does not.
    row = SimpleNamespace(
        deleted_at=None, holds_content=True, metadata_={"x": "x" * 65528}
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: row))
    )

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(postgres_service, "get_session", get_session)
    svc = postgres_service.PostgresService()
    monkeypatch.setattr(svc, "_assert_pointers_in_tenant", AsyncMock())
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        await svc.memory_update(uuid4(), "t", {"metadata_patch": {"y": "z"}})
    assert session.execute.await_count == 1  # lock/read only; no UPDATE issued


@pytest.mark.asyncio
async def test_field_timeout_keeps_slot_until_layout_worker_finishes(monkeypatch):
    memory_field._build_started.clear()
    memory_field._signature_cache.clear()
    scope = {"tenant_id": "slow-layout"}
    release = asyncio.Event()
    started = asyncio.Event()
    sc = SimpleNamespace(memory_field_page=AsyncMock(return_value={"rows": []}))
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: sc)
    monkeypatch.setattr(
        memory_field, "_signature", AsyncMock(return_value={"count": 0})
    )
    monkeypatch.setattr(memory_field, "FIELD_BUILD_TIMEOUT_SECONDS", 0.02)

    async def slow_layout(*_a, **_kw):
        started.set()
        await release.wait()
        return {"points": [], "clusters": [], "entities": []}

    monkeypatch.setattr(memory_field.asyncio, "to_thread", slow_layout)
    try:
        with pytest.raises((TimeoutError, memory_field.FieldBusyError)):
            await memory_field.get_memory_field(scope, limit=1)
        assert started.is_set()
        assert "slow-layout" in memory_field._building_tenants
    finally:
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
    assert "slow-layout" not in memory_field._building_tenants


@pytest.mark.asyncio
async def test_field_new_scope_observes_tenant_build_cooldown(monkeypatch):
    memory_field._build_started.clear()
    scope = {"tenant_id": "cooldown-test"}
    monkeypatch.setattr(
        memory_field, "_signature", AsyncMock(return_value={"count": 0})
    )
    build = AsyncMock(return_value={"points": [], "clusters": [], "entities": []})
    monkeypatch.setattr(memory_field, "_build", build)
    try:
        await memory_field.get_memory_field(scope, limit=1)
        with pytest.raises(HTTPException) as exc:
            await memory_field.get_memory_field(
                {**scope, "caller_agent_id": "another-scope"}, limit=1
            )
        assert exc.value.status_code == 429
        assert "tenant" in exc.value.detail.lower()
        assert 1 <= int(exc.value.headers["Retry-After"]) <= 5
        assert build.await_count == 1
    finally:
        memory_field._build_started.clear()


@pytest.mark.parametrize("text", ["a < b and more text", "before <unfinished"])
def test_html_preserves_unmatched_angle_bracket_tail(text):
    assert ingest_service.decode_text_body(text.encode(), "text/html") == text


def test_legacy_metadata_accepts_maintenance_and_shrinking_but_not_growth():
    from core_storage_api.request_bounds import bound_metadata_merge

    stored = {"legacy": "x" * 70000}
    bound_metadata_merge(stored, {"_system": {"enriched": True}})
    bound_metadata_merge(stored, {"legacy": "x" * 66000})
    bound_metadata_merge(stored, {"legacy": "small"})
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        bound_metadata_merge(stored, {"legacy": "x" * 70001})
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        bound_metadata_merge(stored, {"extra": "growth"})


def test_platform_metadata_does_not_consume_caller_budget():
    from core_storage_api.request_bounds import bound_metadata_merge

    bound_metadata_merge({"x": "x" * 65528}, {"_system": {"enriched": True}})
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        bound_metadata_merge({"_system": {"legacy": "x" * 70000}}, {"x": "x" * 65529})


@pytest.mark.asyncio
async def test_signature_service_unavailable_becomes_field_backpressure(monkeypatch):
    memory_field._signature_cache.clear()
    response = httpx.Response(
        503, request=httpx.Request("POST", "http://storage/field-signature")
    )
    sc = SimpleNamespace(
        memory_field_signature=AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "busy", request=response.request, response=response
            )
        )
    )
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: sc)
    with pytest.raises(memory_field.FieldBusyError):
        await memory_field.get_memory_field({"tenant_id": "signature-timeout"}, limit=1)


@pytest.mark.asyncio
async def test_waiting_map_requests_leave_search_slots_available(monkeypatch):
    from core_api.middleware.per_tenant_concurrency import per_tenant_slot

    memory_field._build_started.clear()
    monkeypatch.setattr(memory_field.settings, "per_tenant_search_concurrency", 1)
    monkeypatch.setattr(
        memory_field, "_signature", AsyncMock(return_value={"count": 0})
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def build(*_a, **_kw):
        started.set()
        await release.wait()
        return {"points": [], "clusters": [], "entities": []}

    monkeypatch.setattr(memory_field, "_build", build)
    scope = {"tenant_id": "map-slot-test"}
    first = asyncio.create_task(memory_field.get_memory_field(scope, limit=1))
    try:
        await asyncio.wait_for(started.wait(), 1)
        async with per_tenant_slot("search", scope["tenant_id"]):
            pass
        second = asyncio.create_task(memory_field.get_memory_field(scope, limit=2))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
    finally:
        release.set()
        await first
        memory_field._build_started.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("metadata", {"x": "x" * 65529}),
        ("source_uri", "x" * 2049),
        ("run_id", "x" * 201),
        ("predicate", "x" * 201),
        ("object_value", "x" * 10001),
    ],
)
async def test_bulk_item_size_error_precedes_dedup(monkeypatch, field, value):
    from uuid import uuid4

    from core_api.schemas import BulkMemoryCreate
    from core_api.services import memory_service, organization_settings

    async def existing(_tenant, hashes, **_kw):
        return {h: {"id": str(uuid4()), "client_request_id": None} for h in hashes}

    sc = SimpleNamespace(bulk_find_by_content_hashes=existing)
    monkeypatch.setattr(memory_service, "get_storage_client", lambda: sc)
    monkeypatch.setattr(
        organization_settings,
        "resolve_config",
        AsyncMock(
            return_value=SimpleNamespace(
                governance_pii=SimpleNamespace(enabled=False), enrichment_enabled=False
            )
        ),
    )
    monkeypatch.setattr(memory_service, "hold_for", AsyncMock(return_value=None))
    result = await memory_service.create_memories_bulk(
        BulkMemoryCreate(
            tenant_id="bulk-size-test",
            agent_id="test-agent",
            items=[
                {
                    "content": "An oversized item with enough content to pass the length gate",
                    field: value,
                },
                {
                    "content": "A valid sibling with enough content to pass the length gate"
                },
            ],
        ),
        bulk_attempt_id="size-check",
    )
    assert result.results[0].status == "error"
    assert "must be <=" in result.results[0].error
    assert result.results[1].status == "duplicate_content"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch", [{"_system": {"enriched": True}}, {"legacy": "small"}]
)
async def test_legacy_metadata_maintenance_reaches_update_under_lock(
    monkeypatch, patch
):
    from uuid import uuid4

    from core_storage_api.services import postgres_service

    row = SimpleNamespace(
        deleted_at=None, holds_content=True, metadata_={"legacy": "x" * 70000}
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: row))
    )

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(postgres_service, "get_session", get_session)
    svc = postgres_service.PostgresService()
    monkeypatch.setattr(svc, "_assert_pointers_in_tenant", AsyncMock())
    assert await svc.memory_update(uuid4(), "t", {"metadata_patch": patch})
    assert session.execute.await_count == 2  # lock/read, then metadata UPDATE


def test_html_preserves_text_after_unclosed_final_tag():
    assert (
        ingest_service.decode_text_body(b"<p>a < b and more text</p", "text/html")
        == "a < b and more text</p"
    )


@pytest.mark.asyncio
async def test_layout_finishing_after_deadline_is_cached_for_following_request(
    monkeypatch,
):
    memory_field.field_cache.clear()
    memory_field._build_started.clear()
    scope = {"tenant_id": "late-layout-cache"}
    release = asyncio.Event()
    started = asyncio.Event()
    sc = SimpleNamespace(memory_field_page=AsyncMock(return_value={"rows": []}))
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: sc)
    monkeypatch.setattr(
        memory_field, "_signature", AsyncMock(return_value={"count": 0})
    )
    monkeypatch.setattr(memory_field, "FIELD_BUILD_TIMEOUT_SECONDS", 0.02)
    payload = {"points": [], "clusters": [], "entities": []}

    async def slow_layout(*_a, **_kw):
        started.set()
        await release.wait()
        return payload

    layout_worker = AsyncMock(side_effect=slow_layout)
    monkeypatch.setattr(memory_field.asyncio, "to_thread", layout_worker)
    try:
        with pytest.raises(memory_field.FieldBusyError):
            await memory_field.get_memory_field(scope, limit=1)
        assert started.is_set()
        assert scope["tenant_id"] in memory_field._building_tenants
        layout = memory_field._layouts[scope["tenant_id"]]
        release.set()
        await layout
        await asyncio.sleep(0)  # run completion callbacks before retrying
        assert scope["tenant_id"] not in memory_field._building_tenants
        assert await memory_field.get_memory_field(scope, limit=1) is payload
        assert layout_worker.await_count == 1
        assert sc.memory_field_page.await_count == 1
    finally:
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        memory_field._build_started.clear()
        memory_field.field_cache.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_ms", [2000, 7500])
async def test_field_signature_uses_configured_transaction_local_timeout(
    monkeypatch, timeout_ms
):
    from core_storage_api.services import postgres_service

    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[None, SimpleNamespace(one=lambda: (0, 0, None, None))]
        )
    )

    @asynccontextmanager
    async def get_read_session():
        yield session

    monkeypatch.setattr(postgres_service, "get_read_session", get_read_session)
    from core_storage_api import config

    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(memory_field_signature_timeout_ms=timeout_ms),
    )
    await postgres_service.PostgresService().memory_field_signature(
        tenant_id="signature-budget"
    )
    statement = session.execute.await_args_list[0].args[0]
    assert list(statement.compile().params.values()) == [
        "statement_timeout",
        f"{timeout_ms}ms",
        True,
    ]


def test_field_signature_timeout_has_bounded_default_and_accepts_tuning(monkeypatch):
    from core_storage_api.config import Settings

    assert Settings.model_fields["memory_field_signature_timeout_ms"].default == 30000
    monkeypatch.setenv("MEMORY_FIELD_SIGNATURE_TIMEOUT_MS", "7500")
    assert Settings(_env_file=None).memory_field_signature_timeout_ms == 7500
    monkeypatch.setenv("MEMORY_FIELD_SIGNATURE_TIMEOUT_MS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.asyncio
async def test_map_rate_backend_outage_fails_open_and_warns(monkeypatch, caplog):
    from unittest.mock import Mock

    monkeypatch.setattr(memory_field.limiter, "enabled", True)
    monkeypatch.setattr(
        memory_field.limiter.limiter,
        "hit",
        Mock(side_effect=RuntimeError("backend down")),
    )
    result = {"points": [], "clusters": [], "entities": []}
    get_field = AsyncMock(return_value=result)
    monkeypatch.setattr(memory_field, "_get_memory_field", get_field)
    assert (
        await memory_field.get_memory_field({"tenant_id": "limiter-down"}, limit=1)
        is result
    )
    get_field.assert_awaited_once()
    assert any(
        r.levelname == "WARNING" and "rate" in r.message.lower() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_map_enforces_every_compound_rate_limit(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(
        memory_field.settings, "rate_limit_search", "30/second;100/minute"
    )
    monkeypatch.setattr(memory_field.limiter, "enabled", True)
    hit = Mock(side_effect=[True, False])
    monkeypatch.setattr(memory_field.limiter.limiter, "hit", hit)
    signature = AsyncMock(
        side_effect=AssertionError("map admitted before all limits checked")
    )
    monkeypatch.setattr(memory_field, "_signature", signature)
    with pytest.raises(HTTPException) as exc:
        await memory_field.get_memory_field({"tenant_id": "compound-rate"}, limit=1)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "1"
    assert hit.call_count == 2
    signature.assert_not_awaited()


@pytest.mark.asyncio
async def test_tenant_build_cooldown_reports_remaining_time_and_then_allows_other_scope(
    monkeypatch,
):
    memory_field._build_started.clear()
    clock = [100.0]
    monkeypatch.setattr(memory_field.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        memory_field, "_signature", AsyncMock(return_value={"count": 0})
    )
    build = AsyncMock(return_value={"points": [], "clusters": [], "entities": []})
    monkeypatch.setattr(memory_field, "_build", build)
    scope = {"tenant_id": "tenant-cooldown", "caller_agent_id": "first"}
    try:
        await memory_field.get_memory_field(scope, limit=1)
        clock[0] += 1.2
        with pytest.raises(HTTPException) as exc:
            await memory_field.get_memory_field(
                {**scope, "caller_agent_id": "second"}, limit=1
            )
        assert exc.value.status_code == 429
        assert exc.value.headers["Retry-After"] == "4"
        clock[0] += 4
        await memory_field.get_memory_field(
            {**scope, "caller_agent_id": "second"}, limit=1
        )
        assert build.await_count == 2
    finally:
        memory_field._build_started.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status,message",
    [
        (RuntimeError("internal fetch detail"), 502, "Could not fetch URL"),
        (httpx.ReadTimeout("internal timeout detail"), 504, "URL fetch timed out"),
    ],
)
async def test_url_fetch_failure_messages_preserve_policy_distinction(
    monkeypatch, error, status, message
):
    monkeypatch.setattr(
        ingest_service, "resolve_config", AsyncMock(return_value=SimpleNamespace())
    )
    monkeypatch.setattr(ingest_service, "_fetch_url_text", AsyncMock(side_effect=error))
    with pytest.raises(HTTPException) as exc:
        await ingest_service.ingest_preview(
            ingest_service.IngestRequest(
                tenant_id="fetch-errors", agent_id="agent", url="https://example.com"
            )
        )
    assert exc.value.status_code == status
    assert exc.value.detail == message
