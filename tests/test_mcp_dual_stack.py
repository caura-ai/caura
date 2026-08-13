"""Both protocol eras served from one MCP app.

The SDK routes by era on the ``MCP-Protocol-Version`` header
(``streamable_http_manager``): a value in the handshake set — or no header at
all, which is what an old client's first ``initialize`` looks like — reaches the
legacy transport, and anything else reaches the modern single-exchange one. So
``mcp`` 2.0 keeps serving ``2025-11-25`` clients while ``2026-07-28`` clients get
the enveloped surface, with no flag to set.

That is the SDK's behaviour. What this file pins is that it survives *our*
wiring — ``MCPAuthMiddleware`` and our 12 statically-registered tools — because
the migration was held back on the belief that adopting v2 drops the handshake
and breaks every pre-2026 client. It does not, and this is the assertion that
keeps it that way: without it, a later change that pins a single era would break
old clients silently, since every other test here speaks the modern dialect.

``memclaw_doc(op="list_collections")`` is the probe tool: read-only, no
arguments beyond ``op``, so it exercises real dispatch without writing.

Two constraints from ``test_mcp_server_discover.py`` apply here too, and both
were re-learned the hard way while writing this file.

``StreamableHTTPSessionManager.run()`` may be called once per instance, and
``core_api.mcp_server`` builds one app at import — so this file cannot simply
enter that app's lifespan, or whichever of the two test files ran second would
die on the second ``run()``. It builds its own app instead:
``streamable_http_app()`` mints a fresh session manager per call. That call also
*reassigns* the manager on the wrapped lowlevel server, so the original is
snapshotted and restored — without that, this file leaves an already-run manager
behind and the discover test fails depending on collection order.

The lifespan is entered in the test body rather than a fixture: a fixture
yielding across the anyio task group exits the cancel scope from a different task
than entered it.

The app under test is the same ``mcp`` object with the same tools behind the same
``MCPAuthMiddleware``; only the transport instance differs. The ``/mcp``
no-trailing-slash shim lives in ``app.py`` and is covered by ``test_api_mcp.py``
/ ``test_middleware.py``.
"""

import json

from core_api.mcp_server import MCPAuthMiddleware, mcp
from httpx import ASGITransport, AsyncClient
from mcp.server.transport_security import TransportSecuritySettings

from tests.conftest import get_test_auth

LEGACY_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"
_NS = "io.modelcontextprotocol"

_MODERN_META = {
    f"{_NS}/protocolVersion": MODERN_VERSION,
    f"{_NS}/clientCapabilities": {},
    f"{_NS}/clientInfo": {"name": "memclaw-tests", "version": "0"},
}


def _build_isolated_app():
    """A second MCP app over the same server, mirroring production's construction.

    Returns ``(asgi_app, restore)``. ``restore`` puts the module's original
    session manager back — see the module docstring.
    """
    # The manager lives on the wrapped lowlevel server; ``mcp.session_manager``
    # is a read-only property over it, so the restore has to go through here.
    lowlevel = mcp._lowlevel_server
    original_manager = lowlevel._session_manager
    app = MCPAuthMiddleware(
        mcp.streamable_http_app(
            streamable_http_path="/",
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
    )

    def restore() -> None:
        lowlevel._session_manager = original_manager

    return app, restore


def _headers(extra: dict | None = None) -> dict:
    _, auth = get_test_auth()
    return {
        **auth,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **(extra or {}),
    }


def _payload(resp) -> dict:
    """Unwrap a JSON-RPC payload from either a JSON body or an SSE stream."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                obj = json.loads(line[len("data:") :].strip())
                if "result" in obj or "error" in obj:
                    return obj
        raise AssertionError(f"no JSON-RPC payload in SSE stream: {resp.text!r}")
    return resp.json()


async def test_serves_both_protocol_eras(_setup_app_db):
    """A 2025-11-25 client and a 2026-07-28 client both work against one app."""
    app, restore = _build_isolated_app()
    try:
        async with (
            mcp.session_manager.run(),
            AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as mcp_client,
        ):
            # ── Legacy era ──
            # No MCP-Protocol-Version header: the spec omits it on the opening request,
            # and that absence is also what selects the legacy transport.
            init = await mcp_client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LEGACY_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "legacy-client", "version": "0"},
                    },
                },
                headers=_headers(),
            )
            session_id = init.headers.get("mcp-session-id")
            legacy_headers = _headers(
                {"MCP-Protocol-Version": LEGACY_VERSION}
                | ({"Mcp-Session-Id": session_id} if session_id else {})
            )
            legacy_list = await mcp_client.post(
                "/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers=legacy_headers,
            )
            legacy_call = await mcp_client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "memclaw_doc",
                        "arguments": {"op": "list_collections"},
                    },
                },
                headers=legacy_headers,
            )

            # ── Modern era ──
            # 2026-07-28 validates routing headers against the body and answers -32020
            # on a mismatch, so Mcp-Name is required alongside Mcp-Method and must equal
            # the tool being called.
            modern_call = await mcp_client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "memclaw_doc",
                        "arguments": {"op": "list_collections"},
                        "_meta": _MODERN_META,
                    },
                },
                headers=_headers(
                    {
                        "MCP-Protocol-Version": MODERN_VERSION,
                        "Mcp-Method": "tools/call",
                        "Mcp-Name": "memclaw_doc",
                    }
                ),
            )

            # Negative control. ``initialize`` does not exist in 2026-07-28, so declaring
            # that version must refuse it. Without this the test could pass while both
            # eras silently collapsed onto one transport — the legacy assertions would
            # then prove nothing about era routing, only that *some* path answered.
            cross_era_init = await mcp_client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LEGACY_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "confused-client", "version": "0"},
                    },
                },
                headers=_headers(
                    {"MCP-Protocol-Version": MODERN_VERSION, "Mcp-Method": "initialize"}
                ),
            )

    finally:
        restore()

    # ── Legacy assertions ──
    assert init.status_code == 200, init.text
    init_result = _payload(init)["result"]
    # Echoed back, NOT upgraded to the latest: an old client must keep speaking
    # the version it asked for.
    assert init_result["protocolVersion"] == LEGACY_VERSION, init_result
    assert init_result["serverInfo"]["name"].startswith("MemClaw"), init_result

    assert legacy_list.status_code == 200, legacy_list.text
    legacy_tools = {t["name"] for t in _payload(legacy_list)["result"]["tools"]}
    # The full catalogue, not a degraded subset.
    assert "memclaw_doc" in legacy_tools, legacy_tools
    assert len(legacy_tools) == 12, sorted(legacy_tools)

    assert legacy_call.status_code == 200, legacy_call.text
    legacy_body = _payload(legacy_call)
    assert "error" not in legacy_body, legacy_body
    assert legacy_body["result"]["content"], legacy_body

    # ── Modern assertions ──
    assert modern_call.status_code == 200, modern_call.text
    modern_body = _payload(modern_call)
    assert "error" not in modern_body, modern_body
    assert modern_body["result"]["content"], modern_body

    # Asserted as "not served" rather than an exact code: the point is that the
    # eras are distinct surfaces, not which layer refuses the cross-era call.
    refused = cross_era_init.status_code >= 400 or "error" in _payload(cross_era_init)
    assert refused, (
        "initialize was served on the 2026-07-28 dialect — the era split is gone, "
        "so the legacy assertions prove nothing: "
        f"{cross_era_init.status_code} {cross_era_init.text[:300]}"
    )
