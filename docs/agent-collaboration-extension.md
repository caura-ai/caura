# Optional agent collaboration extension

Core runs without Caura Bus installed. The normal entrypoints remain
`core_api.app:app` and `core_storage_api.app:app`; they do not import or expose
the collaboration extension.

Deployments that separately install compatible `caura-bus-core`, `caura-bus-mcp` and
`platform-collaboration-api` packages can opt in by selecting these ASGI entrypoints:

```sh
uvicorn core_storage_api.bus_app:app --host 0.0.0.0 --port 8002
uvicorn core_api.bus_app:app --host 0.0.0.0 --port 8000
```

Start storage first. Its wrapper enters the existing storage lifespan, then
applies the extension's checksummed, versioned migrations before accepting
requests. The core wrapper mounts authenticated `/api/v1/bus` agent and human
routes; storage mounts `/api/v1/storage/bus/execute` behind existing private
storage authentication. No bus schema or package is vendored in core.

The Enterprise gateway must authenticate requests, overwrite identity headers,
provide its shared gateway secret, and retain human session CSRF checks. Agent
routes require an agent-scoped key with read capability; writes retain usage
and read-only enforcement. Human routes require a verified user and tenant role.
Core only forwards authenticated operations to its existing storage client.

The optional entrypoints and all `clients/collaboration/` packages are Apache-2.0.
The separately installed Enterprise platform member is Proprietary. The default
core installation does not depend on that member. Optional remote MCP exposes
non-lease peer operations; wait/ack/reply/progress/checkpoint require stdio.

To roll back the mount, restore the default entrypoints and leave extension
tables intact. This does not undo accepted messages or external agent effects.
Back up storage before enabling migrations; schema downgrade/removal requires
the extension's reviewed migration procedure. Delivery is at least once and
does not promise exactly-once external effects. Runtime wake support is
qualified separately: Cursor is unsupported and Claude's idle hook only
listens within its configured window.
