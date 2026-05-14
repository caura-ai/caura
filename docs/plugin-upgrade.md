# Upgrading the memclaw plugin

**Audience:** operators running OpenClaw with the memclaw plugin against a memclaw server (memclaw.dev, memclaw.net, on-prem). Two flows are documented: the **automatic** path (heartbeat-driven) and the **manual** path (`/api/v1/install-plugin`).

## When auto-upgrade runs

After the manifest-aware deploy work (CAURA-444 / #113), the server tracks per-node `plugin_version` from each heartbeat and queues a `deploy` command when it's below `MIN_RECOMMENDED_PLUGIN_VERSION`. Cycle:

1. Plugin sends a heartbeat (every 60 s by default).
2. Server compares the reported `plugin_version` against the floor.
3. If below the floor **and** the plugin version isn't in the no-go list (see below), server returns a `deploy` command in the heartbeat response.
4. Plugin fetches current source via `/api/v1/plugin-manifest` + `/api/v1/plugin-source?file=...`, writes new files, rebuilds `dist/`.
5. New code takes effect at the next OpenClaw restart.

`.env` is **preserved** across auto-upgrade — `deployPlugin` reads the existing values into a Map and merges any new keys the server pushed.

## When auto-upgrade does *not* run

The server refuses to queue a deploy in these cases. Each falls through to the **manual** path below.

| Skip reason | Log line | Operator action |
|---|---|---|
| Plugin version below `MIN_AUTO_DEPLOY_PLUGIN_VERSION` (pre-manifest-aware) | `skipping auto-upgrade for node=… on pre-manifest-aware version <v> (manual re-install required; floor=<F>)` | Run the manual re-install on each affected node |
| Plugin version in `KNOWN_BROKEN_DEPLOY_VERSIONS` (e.g. 2.3.0) | `skipping auto-upgrade for node=… on broken-deploy version <v> (manual re-install required)` | Run the manual re-install on each affected node |
| Tenant has `memclaw.auto_upgrade_enabled = false` | (silent skip; opt-out) | Either flip the setting or run the manual path |
| Node is in deploy cooldown | (silent skip; plugin reported `deploy_blocked_until`) | Wait, or run the manual path |
| Plugin version unparseable or absent | (silent skip; fail-closed) | Investigate; node may need a fresh install |

The **pre-manifest-aware floor** is the critical one for currently-deployed fleets. Plugins below the floor fetch source from their own hardcoded list set at release time. Any file the backend later adds (e.g. `keystones.ts`, statically imported by `context-engine.ts`) is never pulled, the post-deploy build completes but `dist/` imports a module that isn't on disk, and the plugin is **terminal** on the next OpenClaw restart. The gate prevents this; the manual path recovers from it.

## Manual re-install (one-liner per node)

The server's `/api/v1/install-plugin` endpoint returns a complete bash installer (idempotent, self-contained, ~350 lines). Run it on each affected node:

### Minimal — fresh install or operator-driven re-install with explicit creds

```bash
curl -ks -X POST "$MEMCLAW_API_URL/api/v1/install-plugin" \
  -H "Content-Type: application/json" \
  -d '{
    "api_url":   "https://your-memclaw-server",
    "api_key":   "mc_… or mca_…",
    "fleet_id":  "your-fleet",
    "tenant_id": "your-tenant",
    "node_name": "this-node"
  }' | bash
```

The installer:
- Downloads the current plugin source from `/api/v1/plugin-source?file=…`
- Writes `.env` with the values you passed
- Runs `npm install && npm run build` to compile `dist/`
- Re-runs are safe — same command works for first install and re-install

### Identity-preserving — re-install over an existing node

When upgrading an existing install, read its current `.env` first and pass the values back so per-node identity (`tenant_id`, `node_name`, `fleet_id`, `api_key`) is preserved verbatim:

```bash
ENV=$HOME/.openclaw/plugins/memclaw/.env
URL=$(grep    '^MEMCLAW_API_URL='    "$ENV" | cut -d= -f2-)
KEY=$(grep    '^MEMCLAW_API_KEY='    "$ENV" | cut -d= -f2-)
FLEET=$(grep  '^MEMCLAW_FLEET_ID='   "$ENV" | cut -d= -f2-)
TENANT=$(grep '^MEMCLAW_TENANT_ID='  "$ENV" | cut -d= -f2-)
NODE=$(grep   '^MEMCLAW_NODE_NAME='  "$ENV" | cut -d= -f2-)

curl -ks -X POST "$URL/api/v1/install-plugin" \
  -H "Content-Type: application/json" \
  -d "$(jq -nc \
        --arg u "$URL" \
        --arg k "$KEY" \
        --arg f "$FLEET" \
        --arg t "$TENANT" \
        --arg n "$NODE" \
        '{api_url:$u, api_key:$k, fleet_id:$f, tenant_id:$t, node_name:$n}')" | bash
```

Use this form when re-installing across an existing fleet — it preserves node identity end-to-end, so audit logs, fleet stats, and any per-node trust elevation stay intact.

### Verify

After the installer exits, confirm the new version is on disk:

```bash
grep PLUGIN_VERSION $HOME/.openclaw/plugins/memclaw/dist/version.js
# → export const PLUGIN_VERSION = "<new version>";
```

The plugin's in-memory code still reflects the *old* version until the next OpenClaw restart. Heartbeats fire from the new code only after restart.

## Fleet-scale re-install

For operators with many nodes (and SSH or OpenClaw-agent reach to all of them), wrap the identity-preserving form in a loop over an inventory file. The enterprise repo ships a reference script at `scripts/reinstall-fleet.sh` that does exactly this — see its `--help` for inventory format and dry-run options.

Identifying which nodes need re-install:

```bash
curl -s "https://your-memclaw-server/api/v1/fleet/stats?tenant_id=$TENANT_ID&fleet_id=$FLEET_ID" \
  -H "Authorization: Bearer $JWT" \
  | jq '.nodes[]
        | select((.plugin_version // "0") | split(".") | map(tonumber? // 0) | . < [2,6,0])
        | {node_name, plugin_version, last_heartbeat}'
```

That returns the stale-plugin set the auto-deploy gate is currently skipping. The dashboard's Fleet page surfaces the same count in the Claws header.

## What's preserved vs. clobbered

| Resource | Auto-upgrade (heartbeat) | Manual install (with creds) | Manual install (default creds) |
|---|---|---|---|
| `.env` (`MEMCLAW_API_KEY` etc.) | Preserved + merged | Preserved (you pass them back) | Overwritten with defaults |
| `dist/*.js`, `src/*.ts` | Replaced | Replaced | Replaced |
| `node_modules/` | Replaced | Replaced | Replaced |
| `.agent-keys.json`, `.educated`, `.allowlist-applied` | Preserved | Preserved | Preserved |
| Custom skills in `skills/` (operator-added) | Preserved | Preserved | Preserved |
| `install.json` (`install_id` for agent identity) | Preserved | Preserved | Preserved |

The "manual install (default creds)" column is what you get if you pipe `/api/v1/install-plugin` to bash *without* passing values in the POST body — the installer's defaults (empty key, hostname for node_name) write a new `.env`. Don't do this on a node with existing identity unless you mean to.

## Recovering from a partial-deploy state

A pre-manifest-aware plugin that's been auto-upgraded against a server with new must-fetch files (the failure mode the floor exists to prevent) will fail to load on next OpenClaw restart with `ERR_MODULE_NOT_FOUND`. To recover:

1. Confirm the plugin isn't loading: tail OpenClaw logs for `ERR_MODULE_NOT_FOUND` on a `dist/*.js` path.
2. Run the identity-preserving manual install above. The installer downloads the full current source set, writes them all, rebuilds — completely replacing the partially-deployed tree.
3. Restart OpenClaw. The plugin should load cleanly at the new version.

If `.env` has been corrupted, see step 1 of the minimal form and rebuild with fresh values.
