"""``server/discover`` over HTTP, through MCPAuthMiddleware.

Protocol revision 2026-07-28 makes ``server/discover`` a MUST: it replaces the
``initialize`` handshake as how a client learns our protocol versions,
capabilities and identity. The interesting part is not that the SDK implements
it — it does — but that it survives our own mount: ``MCPAuthMiddleware``
wrapping the Starlette app at ``/mcp``, plus the request metadata the revision
made mandatory.

Three constraints shape this file, all learned the hard way.

``stateless_http=True`` does NOT make the mount self-contained. The Streamable
HTTP manager still needs its task group started, so a request fails with "Task
group is not initialized" unless the MCP lifespan is running, and the ``client``
fixture builds an ASGITransport without triggering the app lifespan.
Statelessness removed the protocol *session*, not the manager's lifecycle.

``StreamableHTTPSessionManager.run()`` may be called only once per instance, and
``mcp`` is a module singleton — so the lifespan can be entered exactly once per
process. Hence one test rather than several: splitting them makes every test
after the first fail. Entering it inside the test body (not a fixture) also
matters, because a fixture yielding across the anyio task group exits the cancel
scope from a different task than entered it.

The ``_meta`` envelope is load-bearing on every request now that capabilities
moved out of the handshake — omitting ``clientCapabilities`` is a -32602, which
is asserted below rather than assumed.
"""

import json

from core_api.mcp_server import mcp_lifespan

from tests.conftest import get_test_auth

PROTOCOL_VERSION = "2026-07-28"
_NS = "io.modelcontextprotocol"

_FULL_META = {
    f"{_NS}/protocolVersion": PROTOCOL_VERSION,
    f"{_NS}/clientCapabilities": {},
    f"{_NS}/clientInfo": {"name": "memclaw-tests", "version": "0"},
}


def _body(meta: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {"_meta": _FULL_META if meta is None else meta},
    }


def _headers(extra: dict | None = None, drop: str | None = None) -> dict:
    _, auth = get_test_auth()
    h = {
        **auth,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": "server/discover",
    }
    h.update(extra or {})
    if drop:
        h.pop(drop, None)
    return h


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


async def test_server_discover_over_http(client):
    """server/discover works through our mount, and rejects malformed requests.

    One test by necessity — see the module docstring on the once-per-process
    session manager.
    """
    async with mcp_lifespan():
        ok = await client.post("/mcp/", json=_body(), headers=_headers())

        # Unauthenticated discover. NOTE: reachable — MCPAuthMiddleware does not
        # reject keyless requests. Verified pre-existing rather than introduced
        # here: on mcp 1.x `tools/list` without a key also returns 200 with the
        # full catalogue. If keyless metadata is ever closed deliberately, this
        # is the assertion to update.
        unauth = await client.post(
            "/mcp/", json=_body(), headers=_headers(drop="X-API-Key")
        )
        # The _meta envelope is required, not defaulted: capabilities moved out
        # of the handshake, so dropping clientCapabilities must fail loudly.
        thin_meta = await client.post(
            "/mcp/",
            json=_body(meta={f"{_NS}/protocolVersion": PROTOCOL_VERSION}),
            headers=_headers(),
        )
        # Header validation runs on our mount, not only in SDK unit tests:
        # Mcp-Method is required and must agree with the body, and an
        # unsupported version must be refused.
        no_method = await client.post(
            "/mcp/", json=_body(), headers=_headers(drop="Mcp-Method")
        )
        mismatch = await client.post(
            "/mcp/", json=_body(), headers=_headers({"Mcp-Method": "tools/list"})
        )
        old_version = await client.post(
            "/mcp/",
            json=_body(),
            headers=_headers({"MCP-Protocol-Version": "2025-11-25"}),
        )

    assert ok.status_code == 200, ok.text
    payload = _payload(ok)
    assert "error" not in payload, payload
    result = payload["result"]

    assert result["supportedVersions"] == [PROTOCOL_VERSION]
    assert result["resultType"] == "complete"
    # Server identity lives in _meta, not a top-level serverInfo field.
    server_info = result["_meta"][f"{_NS}/serverInfo"]
    assert server_info["name"].startswith("MemClaw")
    assert server_info["version"], "serverInfo.version must not be empty"

    # DiscoverResult is a CacheableResult, so our cache_hints reach the wire.
    assert result["ttlMs"] == 300_000
    assert result["cacheScope"] == "private"

    # Tools and NOTHING else. MCPServer wires prompt/resource handlers
    # unconditionally and get_capabilities derives advertisement from handler
    # presence, so out of the box discover claimed prompts and resources
    # (including subscribe) that we do not serve. mcp_server drops those
    # handlers; this is the assertion that catches it silently regressing —
    # the removal warns rather than asserts at import so an SDK rename cannot
    # take the service down, which makes this test the actual guard.
    assert set(result["capabilities"]) == {"tools"}, result["capabilities"]

    # Reachable without a key — see the note above. Deliberately asserts only
    # that discover behaves the same either way, NOT what keyless callers may do
    # generally: that depends on deployment mode (OSS standalone default-tenant
    # vs. key-gated vs. enterprise header-trust) and belongs in an auth test.
    assert unauth.status_code == 200, unauth.text

    assert thin_meta.status_code == 400, thin_meta.text
    thin_error = _payload(thin_meta)["error"]
    assert thin_error["code"] == -32602, thin_error
    assert "clientCapabilities" in thin_error["message"]

    # Refusal is asserted as "no successful result", not as a 4xx. The three
    # cases genuinely differ: header problems are HTTP 400s, whereas declaring
    # MCP-Protocol-Version 2025-11-25 gets an HTTP 200 carrying JSON-RPC -32601
    # ("Method not found: server/discover") — the SDK routes by dialect, and
    # discover does not exist in that revision. Pinning exact codes here would
    # bind the test to SDK-internal error mapping; what matters is that none of
    # the three is served.
    for label, resp in (
        ("missing Mcp-Method", no_method),
        ("header/body mismatch", mismatch),
        ("unsupported version", old_version),
    ):
        refused = resp.status_code >= 400 or "error" in _payload(resp)
        assert refused, f"{label} was served: {resp.status_code} {resp.text[:300]}"
