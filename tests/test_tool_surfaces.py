"""MCP tool surfaces and tool annotations.

The server serves one of two tool sets per connection (``core_api.tools.Surface``):

- ``default`` — every API-key agent, the OpenClaw plugin, any unmarked connection.
- ``directory`` — a Claude connector-directory connection, marked by the gateway
  with ``X-Caura-Tool-Surface: directory``. The directory requires that no tool
  mixes reads and writes, so this set replaces ``caura_manage`` and ``caura_doc``
  with six narrowings, each covering one effect.

What is pinned here:

1. Every listed tool, on both surfaces, carries a title and the read-only /
   destructive hints, and the hints match a classification table kept in this
   file, so a reclassification is a reviewed diff rather than a side effect.
2. The default surface lists exactly the twelve tools it always has; the
   directory surface lists the same minus the two mixed tools plus the six.
3. The six cover every op of the two mixed tools exactly once, and none mixes
   reads and writes.
4. Each narrowing is a pure forwarder: it checks ``op`` against its subset and
   hands every argument, by name, to the original handler. That shape is what
   lets ``test_mcp_authz_gate_inventory`` judge it by the original's gates.
5. The header is honored only on the gateway-verified path.
6. A tool is callable exactly where it is listed; off its surface it answers
   ``TOOL_NOT_ON_SURFACE`` naming the replacement.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
import uuid
from pathlib import Path

import pytest

from core_api import mcp_server
from core_api.config import settings
from core_api.tools import (
    ALL_SURFACES,
    DEFAULT_SURFACE,
    DIRECTORY_SURFACE,
    REGISTRY,
    specs_for_surface,
)
from tests.test_mcp_authz_gate_inventory import MUTATING_OPS, READING_OPS

pytestmark = pytest.mark.unit

MIXED = {"caura_manage", "caura_doc"}

# The settings attribute the MCP shared-key gate reads (the resolved dual-read
# field), named once as ``test_mcp_gateway_secret`` does.
_SHARED_KEY_FIELD = "memclaw_api_key"  # legacy-name-ok: rule 3 dual-read field

NARROWINGS = {
    "caura_memory_read",
    "caura_memory_update",
    "caura_memory_delete",
    "caura_doc_read",
    "caura_doc_write",
    "caura_doc_delete",
}

# caura_tune reads when called with no fields; the directory surface serves
# that form as its own read-only tool and caura_tune there only writes.
READ_FORMS = {"caura_tune_get"}

DEFAULT_TOOLS = {
    "caura_doc",
    "caura_entity_get",
    "caura_evolve",
    "caura_insights",
    "caura_keystones",
    "caura_keystones_set",
    "caura_list",
    "caura_manage",
    "caura_recall",
    "caura_stats",
    "caura_tune",
    "caura_write",
}

# (readOnlyHint, destructiveHint). The reasoning for each row is in the PR that
# introduced it and in ``ToolSpec.read_only``: bookkeeping a read leaves behind
# (usage meters, recall counters, audit and read-receipt rows) is not a write;
# anything that can change a row the caller can see is.
CLASSIFICATION = {
    "caura_recall": (True, False),
    "caura_list": (True, False),
    "caura_stats": (True, False),
    "caura_entity_get": (True, False),
    "caura_keystones": (True, False),
    "caura_memory_read": (True, False),
    "caura_doc_read": (True, False),
    "caura_tune_get": (True, False),
    # A write can mark older memories outdated or conflicted (contradiction
    # detection), so it is not only additive.
    "caura_write": (False, True),
    # Overwrites the agent's stored profile; reset clears it.
    "caura_tune": (False, True),
    # Persists findings and supersedes the prior run's insight memories.
    "caura_insights": (False, True),
    # Adjusts the weights of existing memories and may write rules.
    "caura_evolve": (False, True),
    "caura_keystones_set": (False, True),
    "caura_manage": (False, True),
    "caura_doc": (False, True),
    "caura_memory_update": (False, True),
    "caura_memory_delete": (False, True),
    "caura_doc_write": (False, True),
    "caura_doc_delete": (False, True),
}


@pytest.fixture
def surface():
    """Set the connection's tool surface for the test body, then put back the
    default. Set rather than reset: an async test body runs in a different
    context from a sync fixture, so a token from one cannot reset the other
    (the same reason ``test_mcp_gateway_secret`` resets its vars by value)."""

    def _set(value: str) -> None:
        mcp_server._tool_surface_var.set(value)

    yield _set
    mcp_server._tool_surface_var.set(DEFAULT_SURFACE)


async def _listed(surface_value: str) -> list:
    token = mcp_server._tool_surface_var.set(surface_value)
    try:
        return await mcp_server.mcp.list_tools()
    finally:
        mcp_server._tool_surface_var.reset(token)


# ---------------------------------------------------------------------------
# 1. Annotations
# ---------------------------------------------------------------------------


def test_the_server_module_spells_the_surfaces_as_the_registry_does() -> None:
    assert mcp_server._DEFAULT_SURFACE == DEFAULT_SURFACE
    assert mcp_server._DIRECTORY_SURFACE == DIRECTORY_SURFACE
    assert set(ALL_SURFACES) == {DEFAULT_SURFACE, DIRECTORY_SURFACE}


def test_every_registered_tool_is_classified() -> None:
    assert set(CLASSIFICATION) == set(REGISTRY)


@pytest.mark.parametrize("surface_value", ALL_SURFACES)
async def test_every_listed_tool_carries_a_title_and_its_hints(
    surface_value: str,
) -> None:
    tools = await _listed(surface_value)
    assert tools
    for tool in tools:
        wire = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        spec = REGISTRY[tool.name]
        assert wire.get("title") == spec.title and spec.title.strip(), wire
        annotations = wire.get("annotations") or {}
        assert annotations.get("title") == spec.title, wire
        read_only, destructive = CLASSIFICATION[tool.name]
        assert annotations.get("readOnlyHint") is read_only, (tool.name, annotations)
        assert annotations.get("destructiveHint") is destructive, (
            tool.name,
            annotations,
        )
        # Every tool stays inside the caller's tenant (OpenAI's review asks
        # for all three hints; MCP's unstated default is true).
        assert annotations.get("openWorldHint") is False, (tool.name, annotations)


def test_a_read_only_tool_has_only_reading_ops() -> None:
    """``readOnlyHint`` is a promise clients act on without confirmation."""
    for spec in REGISTRY.values():
        if spec.read_only:
            mutating = {op.name for op in spec.ops} & MUTATING_OPS
            assert not mutating, (
                f"{spec.name} is read-only but takes {sorted(mutating)}"
            )


def test_a_tool_with_a_mutating_op_is_not_read_only() -> None:
    for spec in REGISTRY.values():
        if {op.name for op in spec.ops} & MUTATING_OPS:
            assert not spec.read_only, spec.name


# ---------------------------------------------------------------------------
# 2. What each surface lists
# ---------------------------------------------------------------------------


async def test_the_default_surface_is_unchanged() -> None:
    assert {t.name for t in await _listed(DEFAULT_SURFACE)} == DEFAULT_TOOLS


async def test_an_unknown_surface_value_is_never_stored() -> None:
    """The middleware only ever stores the two known values; were anything else
    to reach the var, it would match no spec and list nothing, not everything."""
    assert {t.name for t in await _listed("bogus")} == set()


async def test_the_directory_surface_swaps_the_mixed_tools_for_the_narrowings() -> None:
    listed = {t.name for t in await _listed(DIRECTORY_SURFACE)}
    assert listed == (DEFAULT_TOOLS - MIXED) | NARROWINGS | READ_FORMS


def test_specs_for_surface_agrees_with_tools_list() -> None:
    assert {s.name for s in specs_for_surface(DEFAULT_SURFACE)} == DEFAULT_TOOLS
    assert {s.name for s in specs_for_surface(DIRECTORY_SURFACE)} == (
        DEFAULT_TOOLS - MIXED
    ) | NARROWINGS | READ_FORMS


def test_the_narrowings_stay_out_of_the_plugin_manifest_and_tool_descriptions() -> None:
    """The plugin is a default-surface client: it must not start exposing them."""
    manifest = json.loads(
        (Path(__file__).parent.parent / "plugin" / "tools.json").read_text()
    )
    names = {entry["name"] for entry in manifest}
    assert names == DEFAULT_TOOLS
    assert not any(
        entry["plugin_exposed"]
        for entry in manifest
        if entry["name"] in NARROWINGS | READ_FORMS
    )
    for name in NARROWINGS | READ_FORMS:
        assert REGISTRY[name].plugin_exposed is False


async def test_tool_descriptions_describe_the_default_surface() -> None:
    from core_api.routes.health import tool_descriptions

    assert set(await tool_descriptions(enriched=False)) == DEFAULT_TOOLS
    assert set(await tool_descriptions(enriched=True)) == DEFAULT_TOOLS


# ---------------------------------------------------------------------------
# 3. Coverage of the split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mixed", sorted(MIXED))
def test_the_narrowings_cover_every_op_of_a_mixed_tool_exactly_once(mixed: str) -> None:
    covering = [s for s in REGISTRY.values() if s.delegate == mixed]
    assert covering, mixed
    seen: list[str] = [op.name for s in covering for op in s.ops]
    assert sorted(seen) == sorted({op.name for op in REGISTRY[mixed].ops}), (
        mixed,
        seen,
    )


@pytest.mark.parametrize("name", sorted(NARROWINGS))
def test_no_narrowing_mixes_reads_and_writes(name: str) -> None:
    ops = {op.name for op in REGISTRY[name].ops}
    assert ops, name
    assert ops <= READING_OPS or ops <= MUTATING_OPS, (name, ops)
    assert REGISTRY[name].read_only is (ops <= READING_OPS), name


@pytest.mark.parametrize("name", sorted(NARROWINGS))
def test_a_narrowing_publishes_its_delegates_op_specs(name: str) -> None:
    spec = REGISTRY[name]
    delegate_ops = {op.name: op for op in REGISTRY[spec.delegate].ops}
    for op in spec.ops:
        assert op == delegate_ops[op.name], (name, op.name)


@pytest.mark.parametrize("name", sorted(NARROWINGS))
def test_narrowings_are_directory_only_and_delegate_to_a_mixed_tool(name: str) -> None:
    spec = REGISTRY[name]
    assert spec.surfaces == (DIRECTORY_SURFACE,)
    assert spec.delegate in MIXED
    assert len(name) <= 64


# ---------------------------------------------------------------------------
# 4. Each narrowing is a pure forwarder
# ---------------------------------------------------------------------------


def _body(fn) -> list[ast.stmt]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    func = tree.body[0]
    assert isinstance(func, ast.AsyncFunctionDef)
    body = list(func.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body = body[1:]  # docstring
    return body


@pytest.mark.parametrize("name", sorted(NARROWINGS | READ_FORMS))
def test_a_narrowing_only_checks_op_and_forwards(name: str) -> None:
    spec = REGISTRY[name]
    delegate = REGISTRY[spec.delegate]
    params = list(inspect.signature(spec.handler).parameters)
    delegate_params = set(inspect.signature(delegate.handler).parameters)
    assert set(params) <= delegate_params, (name, set(params) - delegate_params)

    body = _body(spec.handler)
    *guard, last = body
    # The final statement: ``return await <delegate>(...)``.
    assert isinstance(last, ast.Return) and isinstance(last.value, ast.Await), name
    call = last.value.value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name), name
    assert call.func.id == delegate.handler.__name__, name
    assert not call.args, f"{name} must forward by keyword"
    forwarded = {kw.arg: kw.value for kw in call.keywords}

    # Every own parameter goes through unchanged, under its own name.
    for param in params:
        value = forwarded.get(param)
        assert isinstance(value, ast.Name) and value.id == param, (name, param)

    ops = {op.name for op in spec.ops}
    if "op" in params:
        # ``_valid_ops = {...}`` then ``if op not in _valid_ops: return ...``.
        assert len(guard) == 2, name
        assign, check = guard
        assert isinstance(assign, ast.Assign) and isinstance(assign.value, ast.Set), (
            name
        )
        literal = {e.value for e in assign.value.elts if isinstance(e, ast.Constant)}
        assert literal == ops, (name, literal, ops)
        assert isinstance(check, ast.If) and isinstance(check.test, ast.Compare), name
        assert isinstance(check.test.ops[0], ast.NotIn), name
        assert set(forwarded) == set(params), name
    elif not ops:
        # A read form (caura_tune_get) forwards its own parameters and nothing
        # else: no fields is the delegate's read.
        assert guard == [], name
        assert set(forwarded) == set(params), name
    else:
        # A single-op narrowing passes that op as a constant and does nothing else.
        assert guard == [], name
        (only,) = ops
        op_value = forwarded.get("op")
        assert isinstance(op_value, ast.Constant) and op_value.value == only, name
        assert set(forwarded) == set(params) | {"op"}, name


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(NARROWINGS))
async def test_a_narrowing_reaches_its_delegate_with_the_callers_arguments(
    name: str, monkeypatch
) -> None:
    spec = REGISTRY[name]
    seen: list[dict] = []

    async def _recorder(**kwargs):
        seen.append(kwargs)
        return "ok"

    monkeypatch.setattr(mcp_server, spec.delegate, _recorder)
    params = inspect.signature(spec.handler).parameters
    for op in spec.ops:
        args = {
            p: f"v-{p}"
            for p in params
            if p not in ("op",) and params[p].default is inspect.Parameter.empty
        }
        if "op" in params:
            args["op"] = op.name
        args["agent_id"] = "agent-a"
        assert await spec.handler(**args) == "ok"
        assert seen[-1]["op"] == op.name
        assert seen[-1]["agent_id"] == "agent-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    sorted(
        n
        for n in NARROWINGS
        if "op" in inspect.signature(REGISTRY[n].handler).parameters
    ),
)
async def test_a_narrowing_refuses_an_op_outside_its_subset(
    name: str, monkeypatch
) -> None:
    spec = REGISTRY[name]

    async def _must_not_run(**_kwargs):
        raise AssertionError("the delegate ran for an op outside the narrowing")

    monkeypatch.setattr(mcp_server, spec.delegate, _must_not_run)
    outside = next(
        op.name
        for op in REGISTRY[spec.delegate].ops
        if op.name not in {o.name for o in spec.ops}
    )
    result = await spec.handler(op=outside)
    assert result.is_error
    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert sorted(error["details"]["expected_ops"]) == sorted(o.name for o in spec.ops)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args"),
    [
        (
            "caura_memory_update",
            {"op": "update", "memory_id": str(uuid.uuid4()), "content": "x"},
        ),
        ("caura_memory_delete", {"op": "delete", "memory_id": str(uuid.uuid4())}),
        ("caura_doc_write", {"collection": "c", "doc_id": "d", "data": {"a": 1}}),
        ("caura_doc_delete", {"collection": "c", "doc_id": "d"}),
    ],
)
async def test_a_write_narrowing_hits_the_delegates_write_scope_gate(
    name: str, args: dict
) -> None:
    """End to end through the real delegate: a read-only credential is refused
    by the same gate, with the same envelope, as on the mixed tool."""
    tenant = mcp_server._tenant_id_var.set("tenant-surface-test")
    scopes = mcp_server._scopes_var.set({"read"})
    try:
        result = await REGISTRY[name].handler(**args)
    finally:
        mcp_server._scopes_var.reset(scopes)
        mcp_server._tenant_id_var.reset(tenant)
    assert result is mcp_server._READ_ONLY_ERROR


# ---------------------------------------------------------------------------
# 5. The header is a gateway attribute
# ---------------------------------------------------------------------------


async def _surface_after(headers: list[tuple[bytes, bytes]]) -> tuple[bool, str | None]:
    seen: dict = {}

    async def _app(scope, receive, send):
        seen["surface"] = mcp_server._get_tool_surface()

    async def _recv():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def _send(message):
        sent.append(message)

    await mcp_server.MCPAuthMiddleware(_app)(
        {"type": "http", "headers": headers}, _recv, _send
    )
    return "surface" in seen, seen.get("surface")


@pytest.fixture
def _clean_identity_vars():
    yield
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)
    mcp_server._agent_id_var.set(None)
    mcp_server._readable_tenant_ids_var.set(None)
    mcp_server._scopes_var.set(None)
    mcp_server._via_gateway_var.set(False)
    mcp_server._tool_surface_var.set(DEFAULT_SURFACE)


_DIRECTORY_HEADER = (b"x-caura-tool-surface", b"directory")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
async def test_the_gateway_path_honors_the_directory_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    ran, value = await _surface_after(
        [(b"x-tenant-id", b"t1"), (b"x-gateway-secret", b"s3cret"), _DIRECTORY_HEADER]
    )
    assert ran and value == DIRECTORY_SURFACE


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
@pytest.mark.parametrize("value", [b"", b"default", b"Directory-ish", b"all"])
async def test_the_gateway_path_falls_back_to_default(
    monkeypatch, value: bytes
) -> None:
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    headers = [(b"x-tenant-id", b"t1"), (b"x-gateway-secret", b"s3cret")]
    if value:
        headers.append((b"x-caura-tool-surface", value))
    ran, seen = await _surface_after(headers)
    assert ran and seen == DEFAULT_SURFACE


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
async def test_a_shared_key_caller_cannot_pick_the_directory_surface(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "shared-key")
    # Non-standalone, so the shared-key path resolves the caller's
    # X-Tenant-ID instead of requiring init_standalone().
    monkeypatch.setattr(settings, "is_standalone", False)
    ran, value = await _surface_after(
        [(b"x-api-key", b"shared-key"), (b"x-tenant-id", b"t1"), _DIRECTORY_HEADER]
    )
    assert ran and value == DEFAULT_SURFACE


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
async def test_an_admin_key_caller_cannot_pick_the_directory_surface(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    monkeypatch.setattr(mcp_server, "get_admin_key", lambda: "admin-key")
    ran, value = await _surface_after(
        [(b"x-api-key", b"admin-key"), (b"x-tenant-id", b"t1"), _DIRECTORY_HEADER]
    )
    assert ran and value == DEFAULT_SURFACE


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
async def test_a_directory_request_does_not_bleed_into_the_next(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    await _surface_after(
        [(b"x-tenant-id", b"t1"), (b"x-gateway-secret", b"s3cret"), _DIRECTORY_HEADER]
    )
    assert mcp_server._get_tool_surface() == DIRECTORY_SURFACE
    ran, value = await _surface_after(
        [(b"x-tenant-id", b"t1"), (b"x-gateway-secret", b"s3cret")]
    )
    assert ran and value == DEFAULT_SURFACE


# ---------------------------------------------------------------------------
# 6. Callable exactly where listed
# ---------------------------------------------------------------------------


def _error(result) -> dict:
    assert result.is_error, result
    return json.loads(result.content[0].text)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", sorted(MIXED))
async def test_a_mixed_tool_is_refused_on_the_directory_surface(
    surface, mixed: str
) -> None:
    surface(DIRECTORY_SURFACE)
    error = _error(await mcp_server.mcp.call_tool(mixed, {"op": "read"}))
    assert error["code"] == "TOOL_NOT_ON_SURFACE"
    for narrowing in (s.name for s in REGISTRY.values() if s.delegate == mixed):
        assert narrowing in error["message"], error["message"]
    assert error["details"] == {"tool": mixed, "surface": DIRECTORY_SURFACE}


@pytest.mark.asyncio
async def test_a_legacy_alias_of_a_mixed_tool_is_refused_too(surface) -> None:
    surface(DIRECTORY_SURFACE)
    legacy = "memclaw_manage"  # legacy-name-ok: supported dispatch alias
    error = _error(await mcp_server.mcp.call_tool(legacy, {"op": "read"}))
    assert error["code"] == "TOOL_NOT_ON_SURFACE"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(NARROWINGS | READ_FORMS))
async def test_a_narrowing_is_refused_on_the_default_surface(
    surface, name: str
) -> None:
    surface(DEFAULT_SURFACE)
    error = _error(await mcp_server.mcp.call_tool(name, {}))
    assert error["code"] == "TOOL_NOT_ON_SURFACE"
    assert REGISTRY[name].delegate in error["message"]


@pytest.mark.asyncio
async def test_a_narrowing_dispatches_on_the_directory_surface(surface) -> None:
    """Through the SDK's dispatch, not just the handler: an op outside the
    subset reaches the narrowing's own refusal, so the call was not refused
    for its surface."""
    surface(DIRECTORY_SURFACE)
    error = _error(
        await mcp_server.mcp.call_tool("caura_memory_read", {"op": "delete"})
    )
    assert error["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_a_tool_on_both_surfaces_is_not_refused(surface) -> None:
    for value in ALL_SURFACES:
        surface(value)
        assert mcp_server._refuse_off_surface("caura_recall") is None


# ---------------------------------------------------------------------------
# 7. Over HTTP, through the real transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
async def test_tools_list_over_http_follows_the_gateway_header(monkeypatch) -> None:
    """The surface is set in the middleware and read in ``list_tools``, which
    the SDK runs inside its own transport. This proves the value crosses that
    boundary, for both header states, on one app."""
    from httpx import ASGITransport, AsyncClient

    from tests.test_mcp_dual_stack import LEGACY_VERSION, _build_isolated_app, _payload

    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    base = {
        "X-Tenant-ID": "tenant-surface-http",
        "X-Gateway-Secret": "s3cret",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": LEGACY_VERSION,
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    app, restore = _build_isolated_app()
    try:
        async with (
            mcp_server.mcp.session_manager.run(),
            AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            plain = await client.post("/", json=body, headers=base)
            directory = await client.post(
                "/", json=body, headers={**base, "X-Caura-Tool-Surface": "directory"}
            )
    finally:
        restore()

    assert plain.status_code == 200, plain.text
    assert directory.status_code == 200, directory.text
    plain_tools = {t["name"] for t in _payload(plain)["result"]["tools"]}
    directory_tools = _payload(directory)["result"]["tools"]
    assert plain_tools == DEFAULT_TOOLS
    assert {t["name"] for t in directory_tools} == (
        DEFAULT_TOOLS - MIXED
    ) | NARROWINGS | READ_FORMS
    for tool in directory_tools:
        assert tool["title"] and tool["annotations"]["title"] == tool["title"], tool
        assert (
            "readOnlyHint" in tool["annotations"]
            and "destructiveHint" in tool["annotations"]
        )


# ---------------------------------------------------------------------------
# 8. Directory-surface wording: tool descriptions and server instructions
# ---------------------------------------------------------------------------
#
# The Claude connector directory refuses tool or server text that tells the
# model to set aside the user's instructions, to call a tool the user did not
# ask for, or to fetch behavioural instructions from elsewhere. The default
# surface keeps its text unchanged; the directory surface gets variants
# (``ToolSpec.directory_description``, ``mcp_server._DIRECTORY_INSTRUCTIONS``).

# Case-insensitive, in tool descriptions and the server instructions.
_DIRECTORY_FORBIDDEN_PROSE = (
    "override",
    "obey",
    "mandatory",
    "install-skill",
    "install the",
    "at session start",
    "once per session",
    # Describing what a tool answers helps a client's tool search find it;
    # telling the model when it must call it is the line the directory draws.
    "always call",
    "always use",
    "before answering",
    "before every",
)
# Case-insensitive, anywhere in the served tool list (descriptions AND input
# schemas). Narrower than the prose list on purpose: ``caura_recall``'s
# ``min_similarity`` parameter "overrides the tuned profile for this call",
# which is a statement about the argument, not about the user.
_DIRECTORY_FORBIDDEN_ANYWHERE = (
    "must obey",
    "obey",
    "install-skill",
    "override conflicting",
    "overrides conflicting",
)


async def _initialize_and_discover(headers: dict) -> tuple[dict, dict]:
    from httpx import ASGITransport, AsyncClient

    from tests.test_mcp_dual_stack import (
        _MODERN_META,
        LEGACY_VERSION,
        MODERN_VERSION,
        _build_isolated_app,
        _payload,
    )

    base = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **headers,
    }
    app, restore = _build_isolated_app()
    try:
        async with (
            mcp_server.mcp.session_manager.run(),
            AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            init = await client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LEGACY_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "surface-test", "version": "0"},
                    },
                },
                headers=base,
            )
            discover = await client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "server/discover",
                    "params": {"_meta": _MODERN_META},
                },
                headers={
                    **base,
                    "MCP-Protocol-Version": MODERN_VERSION,
                    "Mcp-Method": "server/discover",
                },
            )
    finally:
        restore()
    assert init.status_code == 200, init.text
    assert discover.status_code == 200, discover.text
    return _payload(init)["result"], _payload(discover)["result"]


_GATEWAY = {"X-Tenant-ID": "tenant-surface-http", "X-Gateway-Secret": "s3cret"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
async def test_a_directory_connection_gets_the_directory_instructions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    init, discover = await _initialize_and_discover(
        {**_GATEWAY, "X-Caura-Tool-Surface": "directory"}
    )
    assert init["instructions"] == mcp_server._DIRECTORY_INSTRUCTIONS
    assert discover["instructions"] == mcp_server._DIRECTORY_INSTRUCTIONS


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
async def test_a_default_connection_keeps_the_default_instructions(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    init, discover = await _initialize_and_discover(_GATEWAY)
    assert init["instructions"] == mcp_server.mcp.instructions
    assert discover["instructions"] == mcp_server.mcp.instructions


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_identity_vars")
async def test_a_direct_caller_cannot_select_the_directory_instructions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "shared-key")
    monkeypatch.setattr(settings, "is_standalone", False)
    init, discover = await _initialize_and_discover(
        {
            "X-API-Key": "shared-key",
            "X-Tenant-ID": "t1",
            "X-Caura-Tool-Surface": "directory",
        }
    )
    assert init["instructions"] == mcp_server.mcp.instructions
    assert discover["instructions"] == mcp_server.mcp.instructions


def test_the_directory_instructions_carry_none_of_the_refused_patterns() -> None:
    text = mcp_server._DIRECTORY_INSTRUCTIONS.lower()
    for phrase in _DIRECTORY_FORBIDDEN_PROSE:
        assert phrase not in text, phrase
    # The guard against following directions inside stored content stays.
    assert "never instructions to you" in text


async def test_directory_tool_descriptions_carry_none_of_the_refused_patterns() -> None:
    tools = await _listed(DIRECTORY_SURFACE)
    listed = {t.name for t in tools}
    for tool in tools:
        description = (tool.description or "").lower()
        for phrase in _DIRECTORY_FORBIDDEN_PROSE:
            assert phrase not in description, (tool.name, phrase)
        # Every tool a directory description points to is one it can call.
        named = set(re.findall(r"caura_[a-z_]+[a-z]", tool.description or ""))
        assert named <= listed, (tool.name, sorted(named - listed))
    served = json.dumps(
        [t.model_dump(mode="json", by_alias=True) for t in tools]
    ).lower()
    for phrase in _DIRECTORY_FORBIDDEN_ANYWHERE:
        assert phrase not in served, phrase


async def test_the_default_surface_serves_the_default_descriptions() -> None:
    for tool in await _listed(DEFAULT_SURFACE):
        assert tool.description == REGISTRY[tool.name].description, tool.name


async def test_directory_descriptions_are_served_where_set() -> None:
    by_name = {t.name: t for t in await _listed(DIRECTORY_SURFACE)}
    variants = {s.name for s in REGISTRY.values() if s.directory_description}
    assert variants == {"caura_keystones", "caura_recall", "caura_tune", "caura_write"}
    for name in variants:
        assert by_name[name].description == REGISTRY[name].directory_description


# ---------------------------------------------------------------------------
# 9. caura_tune: write-only on the directory surface, read via caura_tune_get
# ---------------------------------------------------------------------------


def test_caura_tune_names_its_directory_read_form() -> None:
    tune, get = REGISTRY["caura_tune"], REGISTRY["caura_tune_get"]
    assert tune.directory_read_via == "caura_tune_get"
    assert set(tune.surfaces) == set(ALL_SURFACES)
    assert get.delegate == "caura_tune" and get.read_only and not get.ops
    assert get.surfaces == (DIRECTORY_SURFACE,)
    assert get.plugin_exposed is False
    assert get.title == "Get recall defaults"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"agent_id": "agent-a"}, {"top_k": None, "reset": False, "reset_knobs": []}],
)
async def test_caura_tunes_read_form_is_refused_on_the_directory_surface(
    surface, arguments: dict
) -> None:
    surface(DIRECTORY_SURFACE)
    error = _error(await mcp_server.mcp.call_tool("caura_tune", arguments))
    assert error["code"] == "TOOL_NOT_ON_SURFACE"
    assert "caura_tune_get" in error["message"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"top_k": 5},
        # 0 is a value the handler validates, not an absent field.
        {"top_k": 0},
        {"fts_weight": 0.0},
        {"reset": True},
        {"reset_knobs": ["top_k"]},
    ],
)
def test_caura_tunes_write_form_passes_the_directory_surface(
    surface, arguments: dict
) -> None:
    surface(DIRECTORY_SURFACE)
    assert mcp_server._refuse_off_surface("caura_tune", arguments) is None


@pytest.mark.parametrize("arguments", [{}, {"agent_id": "agent-a"}, {"top_k": 5}])
def test_caura_tune_is_unchanged_on_the_default_surface(
    surface, arguments: dict
) -> None:
    surface(DEFAULT_SURFACE)
    assert mcp_server._refuse_off_surface("caura_tune", arguments) is None


def test_every_tune_field_but_agent_id_counts_as_a_write() -> None:
    """The read-form test reads the arguments the call names; it holds while
    every ``caura_tune`` parameter other than ``agent_id`` is a change."""
    params = set(inspect.signature(mcp_server.caura_tune).parameters)
    assert params >= mcp_server._READ_FORM_IGNORED_ARGS
    for param in params - mcp_server._READ_FORM_IGNORED_ARGS:
        assert not mcp_server._is_read_form({param: 1}), param


@pytest.mark.asyncio
async def test_caura_tune_get_forwards_a_no_field_call(monkeypatch) -> None:
    seen: list[dict] = []

    async def _recorder(**kwargs):
        seen.append(kwargs)
        return "ok"

    monkeypatch.setattr(mcp_server, "caura_tune", _recorder)
    assert await mcp_server.caura_tune_get(agent_id="agent-a") == "ok"
    assert seen == [{"agent_id": "agent-a"}]


@pytest.mark.asyncio
async def test_caura_tune_get_dispatches_on_the_directory_surface(surface) -> None:
    """Reaches caura_tune's handler (whose auth check answers an
    unauthenticated caller), so the surface did not refuse it."""
    surface(DIRECTORY_SURFACE)
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)
    error = _error(await mcp_server.mcp.call_tool("caura_tune_get", {}))
    assert error["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 10. Tools registered outside the registry
# ---------------------------------------------------------------------------


@pytest.fixture
def addon_tool():
    """A tool added to the server outside ``core_api.tools.REGISTRY``, the way
    ``core_api.bus_mcp.register_peer`` adds ``peer``: no spec, so no title,
    hints or read/write split."""
    name = "addon_probe"

    async def addon_probe() -> dict:
        return {"ok": True}

    mcp_server.mcp.add_tool(addon_probe, name=name)
    yield name
    mcp_server.mcp.remove_tool(name)


@pytest.mark.asyncio
async def test_an_unclassified_tool_is_not_listed_on_the_directory_surface(
    addon_tool,
) -> None:
    assert addon_tool in {t.name for t in await _listed(DEFAULT_SURFACE)}
    assert addon_tool not in {t.name for t in await _listed(DIRECTORY_SURFACE)}


@pytest.mark.asyncio
async def test_an_unclassified_tool_is_refused_on_the_directory_surface(
    surface, addon_tool
) -> None:
    surface(DIRECTORY_SURFACE)
    error = _error(await mcp_server.mcp.call_tool(addon_tool, {}))
    assert error["code"] == "TOOL_NOT_ON_SURFACE"
    assert error["details"] == {"tool": addon_tool, "surface": DIRECTORY_SURFACE}


@pytest.mark.asyncio
async def test_an_unclassified_tool_still_runs_on_the_default_surface(
    surface, addon_tool
) -> None:
    surface(DEFAULT_SURFACE)
    assert mcp_server._refuse_off_surface(addon_tool) is None


# ---------------------------------------------------------------------------
# 11. Found by the questions people ask; one copy of each row
# ---------------------------------------------------------------------------


def test_the_directory_keystones_description_only_describes() -> None:
    """The directory's review criteria: describe what the tool does, don't tell
    the model how to behave. How to treat keystones lives in the directory
    server instructions, which say to follow them and to surface a conflict."""
    text = REGISTRY["caura_keystones"].directory_description.lower()
    for phrase in ("follow them", "tell the user", "instead of silently"):
        assert phrase not in text, phrase
    assert "standing rules" in text
    instructions = mcp_server._DIRECTORY_INSTRUCTIONS.lower()
    assert (
        "follow them" in instructions
        and "tell the user about the conflict" in instructions
    )


def test_recall_and_write_describe_what_they_hold_in_peoples_words() -> None:
    """Claude clients with many connectors find tools by searching names and
    descriptions, so the directory wording names the memory and the questions
    it answers rather than the retrieval method alone."""
    recall = REGISTRY["caura_recall"].directory_description.lower()
    for phrase in ("shared memory", "decisions", "what did we decide"):
        assert phrase in recall, phrase
    write = REGISTRY["caura_write"].directory_description.lower()
    for phrase in ("shared memory", "decisions", "remember that"):
        assert phrase in write, phrase
    # The default wording is unchanged for every other client.
    assert REGISTRY["caura_write"].directory_description.endswith(
        REGISTRY["caura_write"].description
    )


@pytest.mark.parametrize(
    ("value", "keys"),
    [(DEFAULT_SURFACE, {"results", "items"}), (DIRECTORY_SURFACE, {"items"})],
)
def test_list_keys_by_surface(surface, value, keys) -> None:
    """``items`` is canonical everywhere; the ``results`` alias, the same list
    a second time, stays only where existing parsers read it."""
    surface(value)
    rows = [{"id": "m1"}]
    envelope = mcp_server._list_keys(rows)
    assert set(envelope) == keys
    assert envelope["items"] is rows


def test_every_list_envelope_goes_through_list_keys() -> None:
    """No envelope spells the alias by hand, so no tool can drift back to
    sending the directory surface every row twice."""
    source = inspect.getsource(mcp_server)
    body = source.split("def _list_keys", 1)[1].split("\ndef ", 1)[1]
    assert '"results":' not in body


# ---------------------------------------------------------------------------
# 12. Directory responses carry no internal fields
# ---------------------------------------------------------------------------


_ROW = {
    "id": "m1",
    "content": "we chose Postgres",
    "agent_id": "claude-dana",
    "agent_trust_level": 1,
    "trace_id": "mcp-abc",
    "tool_call_id": "t1",
    "score_parts": {"vec_sim": 0.7},
    "system_metadata": {"llm_ms": 900},
    "metadata": {
        "_system": {"llm_ms": 900},
        "llm_ms": 900,
        "write_latency_ms": 12,
        "summary": "Chose Postgres",
        "embedding_pending": False,
        "project": "atlas",
    },
    "evidence": {"trace": "mcp-abc", "why": "vector match"},
}


@pytest.mark.parametrize("value", [DEFAULT_SURFACE, DIRECTORY_SURFACE])
def test_internal_fields_reach_only_default_connections(surface, value) -> None:
    surface(value)
    sent = json.loads(mcp_server._dumps({"items": [_ROW], "count": 1}))
    row = sent["items"][0]
    if value == DEFAULT_SURFACE:
        assert row == json.loads(json.dumps(_ROW))
        return
    for gone in ("trace_id", "tool_call_id", "score_parts", "system_metadata"):
        assert gone not in row, gone
    assert row["metadata"] == {
        "summary": "Chose Postgres",
        "embedding_pending": False,
        "project": "atlas",
    }
    assert row["evidence"] == {"why": "vector match"}
    assert row["content"] == "we chose Postgres" and row["agent_trust_level"] == 1
