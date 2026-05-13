"""Plugin source, hash, and install script endpoints."""

import hashlib
import json
import shlex
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

router = APIRouter(tags=["System"])

# Resolve plugin/src relative to the repo root.
# In Docker: /app/plugin/src. Locally: ../../plugin/src from core-api/src/core_api/routes/
_plugin_dir = Path(__file__).resolve().parent.parent.parent.parent / "plugin" / "src"
if not _plugin_dir.is_dir():
    # Docker layout: /app/core-api/src/... → /app/plugin/src
    _plugin_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "plugin" / "src"
_plugin_src = _plugin_dir / "index.ts"
_plugin_files = [
    "index.ts",
    "prompt-section.ts",
    "tools.ts",
    "tool-specs.ts",
    "version.ts",
    "env.ts",
    "transport.ts",
    "validation.ts",
    "config.ts",
    "paths.ts",
    "logger.ts",
    "resolve-agent.ts",
    "tool-definitions.ts",
    "deploy.ts",
    "heartbeat.ts",
    "educate.ts",
    "context-engine.ts",
    "agent-auth.ts",
    "health.ts",
    "install-id.ts",
    "identity.ts",
    "reconcile-skills.ts",
]


# Plugin-root-relative files served alongside the src/*.ts files.
# - tools.json: tool SoT, loaded by tool-specs.ts.
# - skills/memclaw/SKILL.md: shared plugin skill, discovered by OpenClaw
#   via openclaw.plugin.json:skills (one copy per node).
# - openclaw.plugin.json: plugin manifest. Must be served (not baked into
#   the install script as a HEREDOC) because the manifest changes
#   periodically as OpenClaw evolves its plugin contract — e.g. the
#   ``contracts.tools`` field became strictly enforced upstream on
#   2026-05-01 (openclaw/openclaw@7641783d), and any user installing
#   from a stale baked HEREDOC silently lost their entire MemClaw tool
#   surface. Serving from disk keeps the manifest in lockstep with
#   ``plugin/openclaw.plugin.json`` so the installer never falls behind.
_plugin_root_files = {
    "tools.json",
    "skills/memclaw/SKILL.md",
    "openclaw.plugin.json",
}

# Direct-MCP skill adapter. Lives under the repo-root ``static/`` tree
# rather than ``plugin/`` — it is NOT an OpenClaw plugin artifact; it is
# served to Claude Code / Codex users who connect to MemClaw directly via
# MCP. Resolved from app.py's position: core-api/src/core_api/routes/ →
# five ``.parent``s up land on the repo root.
_skill_md_path = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "static" / "skills" / "memclaw" / "SKILL.md"
)


def _plugin_version() -> str:
    """Read the plugin's own version from plugin/package.json.

    Plugin is released on its own cadence (release-please ``plugin``
    package); its version is independent of the backend ``VERSION``.
    """
    return json.loads((_plugin_dir.parent / "package.json").read_text(encoding="utf-8"))["version"]


@router.get("/plugin-source", response_class=PlainTextResponse)
async def plugin_source(file: str = Query(default="index.ts")):
    """Serve plugin source files. Use ?file=prompt-section.ts for other files."""
    if file in _plugin_root_files:
        path = _plugin_dir.parent / file
    elif file in _plugin_files:
        path = _plugin_dir / file
    else:
        return PlainTextResponse("File not found", status_code=404)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return PlainTextResponse("Plugin source not found", status_code=404)


@router.get("/plugin-source-hash", response_class=PlainTextResponse)
async def plugin_source_hash():
    """SHA-256 hash of all plugin source files (for update checks).

    Covers both ``_plugin_files`` (``plugin/src/*.ts``) and
    ``_plugin_root_files`` (``plugin/tools.json``, ``plugin/skills/...``)
    so content changes in plugin-root artifacts (e.g. the shared
    ``skills/memclaw/SKILL.md``) are reflected in the hash and picked up
    by clients polling for updates.

    Root files are iterated in sorted order to keep the hash stable
    across processes (``_plugin_root_files`` is a ``set``; Python's
    hash randomization would otherwise make iteration order
    non-deterministic).
    """
    combined = ""
    for fname in _plugin_files:
        path = _plugin_dir / fname
        if path.is_file():
            combined += path.read_text(encoding="utf-8")
    for fname in sorted(_plugin_root_files):
        path = _plugin_dir.parent / fname
        if path.is_file():
            combined += path.read_text(encoding="utf-8")
    if not combined:
        return PlainTextResponse("Plugin source not found", status_code=404)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _resolve_tenant_id() -> str:
    """Resolve tenant_id from standalone config."""
    from core_api.config import settings

    if settings.is_standalone:
        from core_api.standalone import get_standalone_tenant_id

        return get_standalone_tenant_id()
    return ""


def _generate_install_script(
    *, api_url: str, api_key: str, fleet_id: str, tenant_id: str, node_name: str
) -> str:
    """Generate a bash install script with shell-safe variable assignments."""
    # Shell-quote all user inputs and assign to bash variables at the top
    safe_api_url = shlex.quote(api_url)
    safe_api_key = shlex.quote(api_key)
    safe_fleet_id = shlex.quote(fleet_id)
    safe_tenant_id = shlex.quote(tenant_id)
    safe_node_name = shlex.quote(node_name) if node_name else ""
    safe_version = shlex.quote(_plugin_version())
    api_key_preview = api_key[:6] + "..." if len(api_key) > 6 else "(not set)"

    return f"""#!/usr/bin/env bash
set -euo pipefail

# ── Shell-safe variable assignments ──
MEMCLAW_API_URL={safe_api_url}
MEMCLAW_API_KEY={safe_api_key}
MEMCLAW_FLEET_ID={safe_fleet_id}
MEMCLAW_TENANT_ID={safe_tenant_id}
MEMCLAW_NODE_NAME={safe_node_name or '"$(hostname -s)"'}
MEMCLAW_PLUGIN_VERSION={safe_version}

echo "=== MemClaw Plugin Installer ==="
echo ""

PLUGIN_DIR="$HOME/.openclaw/plugins/memclaw"
CONFIG_PATH="$HOME/.openclaw/openclaw.json"

# When the on-prem uses self-signed TLS, the bootstrap fetches below
# (plugin-source, tools.json, SKILL.md) hit certificate verification
# errors before step 8 has a chance to install the trust anchor.
# Switch to TOFU mode for the bootstrap curls when MEMCLAW_API_URL is
# HTTPS — same reasoning as `docker login` against a self-signed
# registry. After install, NODE_EXTRA_CA_CERTS handles long-term
# trust at runtime, so no -k anywhere outside this script.
case "$MEMCLAW_API_URL" in
  https://*) CURL_INSECURE="-k" ;;
  *)         CURL_INSECURE="" ;;
esac

# 1. Create directory structure
echo "[1/7] Creating plugin directory..."
mkdir -p "$PLUGIN_DIR/src"

# 2. Write package.json
echo "[2/7] Writing package.json..."
cat > "$PLUGIN_DIR/package.json" << PACKAGE_EOF
{{
  "name": "@caura/memclaw",
  "version": "$MEMCLAW_PLUGIN_VERSION",
  "description": "OpenClaw plugin for MemClaw central memory",
  "private": true,
  "type": "module",
  "main": "dist/index.js",
  "openclaw": {{
    "extensions": ["./dist/index.js"]
  }},
  "scripts": {{
    "build": "tsc",
    "dev": "tsc --watch"
  }},
  "devDependencies": {{
    "@types/node": "^25.4.0",
    "typescript": "^5.4"
  }}
}}
PACKAGE_EOF

# 3. Write tsconfig.json
echo "[3/7] Writing tsconfig.json..."
cat > "$PLUGIN_DIR/tsconfig.json" << 'TSCONFIG_EOF'
{{
  "compilerOptions": {{
    "target": "ES2022",
    "module": "Node16",
    "moduleResolution": "Node16",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "declaration": true
  }},
  "include": ["src"]
}}
TSCONFIG_EOF

# 4. Fetch plugin manifest from the server (single source of truth).
# Previously this was a baked HEREDOC, which caused silent drift
# whenever the canonical ``plugin/openclaw.plugin.json`` gained new
# fields (notably ``contracts.tools``, required by OpenClaw upstream
# from 2026-05-01) — the on-disk file in the repo had the field, but
# the HEREDOC the installer wrote to fresh installs did not, so every
# fresh install silently lost the tool surface. Fetching from
# ``/plugin-source`` keeps the canonical file the only source.
echo "[4/7] Fetching plugin manifest from $MEMCLAW_API_URL..."
curl $CURL_INSECURE -sf "$MEMCLAW_API_URL/api/plugin-source?file=openclaw.plugin.json" > "$PLUGIN_DIR/openclaw.plugin.json" || {{
  echo "ERROR: Could not fetch openclaw.plugin.json from $MEMCLAW_API_URL/api/plugin-source?file=openclaw.plugin.json"
  exit 1
}}

# 5. Fetch latest plugin source from MemClaw server
echo "[5/7] Fetching latest plugin source from $MEMCLAW_API_URL..."
for srcfile in index.ts prompt-section.ts tools.ts tool-specs.ts version.ts env.ts transport.ts validation.ts config.ts paths.ts logger.ts resolve-agent.ts tool-definitions.ts deploy.ts heartbeat.ts educate.ts context-engine.ts agent-auth.ts health.ts install-id.ts identity.ts reconcile-skills.ts; do
  curl $CURL_INSECURE -sf "$MEMCLAW_API_URL/api/plugin-source?file=$srcfile" > "$PLUGIN_DIR/src/$srcfile" || {{
    echo "ERROR: Could not fetch $srcfile from $MEMCLAW_API_URL/api/plugin-source?file=$srcfile"
    exit 1
  }}
done
# tools.json sits at plugin root (loaded at runtime by tool-specs.ts)
curl $CURL_INSECURE -sf "$MEMCLAW_API_URL/api/plugin-source?file=tools.json" > "$PLUGIN_DIR/tools.json" || {{
  echo "ERROR: Could not fetch tools.json"
  exit 1
}}
# Shared plugin skill file — OpenClaw discovers this via openclaw.plugin.json:skills
mkdir -p "$PLUGIN_DIR/skills/memclaw" || {{
  echo "ERROR: Could not create skills directory"
  exit 1
}}
curl $CURL_INSECURE -sf "$MEMCLAW_API_URL/api/plugin-source?file=skills/memclaw/SKILL.md" > "$PLUGIN_DIR/skills/memclaw/SKILL.md" || {{
  echo "ERROR: Could not fetch skills/memclaw/SKILL.md"
  exit 1
}}
echo "    Downloaded all plugin source files"

# Generate version.ts (imported by index.ts)
cat > "$PLUGIN_DIR/src/version.ts" << VERSION_EOF
// Auto-generated by install script
export const PLUGIN_VERSION = "$MEMCLAW_PLUGIN_VERSION";
VERSION_EOF

# Write .env file (includes heartbeat config)
cat > "$PLUGIN_DIR/.env" << ENV_EOF
MEMCLAW_API_URL=$MEMCLAW_API_URL
MEMCLAW_API_KEY=$MEMCLAW_API_KEY
MEMCLAW_FLEET_ID=$MEMCLAW_FLEET_ID
MEMCLAW_TENANT_ID=$MEMCLAW_TENANT_ID
MEMCLAW_NODE_NAME=$MEMCLAW_NODE_NAME
ENV_EOF
chmod 600 "$PLUGIN_DIR/.env"

# 6. Install dependencies and build
echo "[6/7] Installing dependencies and building..."
cd "$PLUGIN_DIR"
npm install --silent --no-optional --no-fund --no-audit 2>&1 || true
npm run build 2>&1
echo "    Build successful"

# 7. Configure OpenClaw
echo "[7/7] Configuring OpenClaw..."
if [ -f "$CONFIG_PATH" ]; then
  # Write a temp script to safely modify JSON (avoids inline node -e quoting issues)
  _SETUP_JS=$(mktemp /tmp/memclaw-setup-XXXXXX.mjs)
  cat > "$_SETUP_JS" << 'SETUP_EOF'
import fs from 'fs';
const configPath = process.argv[2];
const pluginDir = process.argv[3];
const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));

if (!config.plugins) config.plugins = {{}};
if (!Array.isArray(config.plugins.allow)) config.plugins.allow = [];
if (!config.plugins.allow.includes('memclaw')) config.plugins.allow.push('memclaw');

if (!config.plugins.entries) config.plugins.entries = {{}};
config.plugins.entries.memclaw = {{ enabled: true, config: {{}} }};

// Disable memory-core — OpenClaw only loads one kind:"memory" plugin at a time.
// Without this, the memory slot stays with memory-core and register() is never called.
if (config.plugins.entries['memory-core']) {{
  config.plugins.entries['memory-core'].enabled = false;
}}

// Claim the exclusive memory slot
if (!config.plugins.slots) config.plugins.slots = {{}};
config.plugins.slots.memory = 'memclaw';

if (!config.plugins.load) config.plugins.load = {{}};
if (!Array.isArray(config.plugins.load.paths)) config.plugins.load.paths = [];
if (!config.plugins.load.paths.includes(pluginDir)) config.plugins.load.paths.push(pluginDir);

if (!config.tools) config.tools = {{}};
if (!Array.isArray(config.tools.alsoAllow)) config.tools.alsoAllow = [];
const tools = ['memclaw_recall','memclaw_write','memclaw_manage','memclaw_doc','memclaw_list','memclaw_entity_get','memclaw_tune','memclaw_insights','memclaw_evolve','memclaw_stats'];
for (const t of tools) {{
  if (!config.tools.alsoAllow.includes(t)) config.tools.alsoAllow.push(t);
}}

fs.writeFileSync(configPath, JSON.stringify(config, null, 2) + '\\n');
console.log('    Updated openclaw.json');
SETUP_EOF
  node "$_SETUP_JS" "$CONFIG_PATH" "$PLUGIN_DIR" 2>&1
  rm -f "$_SETUP_JS"
else
  echo "    WARNING: $CONFIG_PATH not found — you will need to configure allowlist manually"
fi

# 8. TLS trust bootstrap — only when MEMCLAW_API_URL is HTTPS.
# OSS / dev installs (http://localhost:8000) skip this entirely.
# For an enterprise on-prem with a self-signed cert, the gateway exposes
# the cert at /onprem-ca.pem; we curl it once with -k (TOFU — same trust
# pattern as `docker login` to a self-signed registry), save it next to
# the plugin, and write a systemd drop-in that exports
# NODE_EXTRA_CA_CERTS so Node trusts it across openclaw-gateway restarts.
case "$MEMCLAW_API_URL" in
  https://*)
    echo "[8/8] Bootstrapping TLS trust for $MEMCLAW_API_URL"
    if curl -ksSL "$MEMCLAW_API_URL/onprem-ca.pem" -o "$PLUGIN_DIR/onprem-ca.pem" \
        && [ -s "$PLUGIN_DIR/onprem-ca.pem" ] \
        && head -1 "$PLUGIN_DIR/onprem-ca.pem" | grep -q '^-----BEGIN CERTIFICATE-----$'; then
      chmod 0644 "$PLUGIN_DIR/onprem-ca.pem"
      _SD_DIR="$HOME/.config/systemd/user/openclaw-gateway.service.d"
      mkdir -p "$_SD_DIR"
      cat > "$_SD_DIR/memclaw-tls.conf" << SDEOF
[Service]
Environment="NODE_EXTRA_CA_CERTS=$PLUGIN_DIR/onprem-ca.pem"
SDEOF
      # daemon-reload so the next openclaw-gateway start picks up the env.
      # Skip the failure path quietly — some hosts run openclaw differently
      # (Docker-in-Docker, custom init), in which case the customer just
      # needs NODE_EXTRA_CA_CERTS set however their wrapper accepts env.
      systemctl --user daemon-reload 2>/dev/null || true
      # Also export NODE_EXTRA_CA_CERTS for the user's interactive shell so
      # `openclaw agent` invoked from the CLI (which spawns its own embedded
      # plugin process when it can't reach the gateway) trusts the same cert.
      # The systemd drop-in only covers the openclaw-gateway *service*, not
      # the customer's terminal sessions. Idempotent — guarded by a marker
      # comment so re-running install-plugin doesn't append duplicates.
      _RC_MARKER="# memclaw-onprem CA - managed by install-plugin"
      _RC_LINE='[ -r "'"$PLUGIN_DIR"'/onprem-ca.pem" ] && export NODE_EXTRA_CA_CERTS="'"$PLUGIN_DIR"'/onprem-ca.pem"'
      for _RC in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -f "$_RC" ] || continue
        if ! grep -qF "$_RC_MARKER" "$_RC"; then
          printf '\n%s\n%s\n' "$_RC_MARKER" "$_RC_LINE" >> "$_RC"
        fi
      done
      echo "    Saved CA → $PLUGIN_DIR/onprem-ca.pem"
      echo "    Drop-in   → $_SD_DIR/memclaw-tls.conf"
      echo "    Shell env → ~/.bashrc + ~/.zshrc (NODE_EXTRA_CA_CERTS for new shells)"
    else
      rm -f "$PLUGIN_DIR/onprem-ca.pem"
      echo "    WARNING: could not fetch $MEMCLAW_API_URL/onprem-ca.pem."
      echo "    If your on-prem uses a publicly-trusted cert (Let's Encrypt or"
      echo "    a corporate CA already in the system trust store), this is fine."
      echo "    Otherwise plugin requests will fail TLS verification — either"
      echo "    trust the cert manually or set NODE_TLS_REJECT_UNAUTHORIZED=0"
      echo "    in the openclaw-gateway environment (insecure)."
    fi
    ;;
esac

echo ""
echo "=== Installation complete ==="
echo ""
echo "Plugin directory: $PLUGIN_DIR"
echo "API URL:          $MEMCLAW_API_URL"
echo "Fleet ID:         $MEMCLAW_FLEET_ID"
echo "API Key:          {api_key_preview}"
echo ""
echo "Next: restart your OpenClaw gateway to activate the plugin."
echo "  Linux:  systemctl --user restart openclaw-gateway"
echo '  macOS:  launchctl kickstart -k "gui/$(id -u)/ai.openclaw.gateway"'
echo ""
echo "After restart, MemClaw will:"
echo "  1. Claim the memory slot (replacing memory-core)"
echo "  2. Load the plugin and register 12 tools"
echo "  3. Auto-educate your agents (SKILL.md, TOOLS.md, AGENTS.md, HEARTBEAT.md)"
echo "  4. Start heartbeating to the MemClaw API"
echo ""
echo "Your node will appear in Fleet Management within 60 seconds."
echo ""
"""


class InstallPluginRequest(BaseModel):
    fleet_id: str = ""
    api_url: str = "http://localhost:8000"
    api_key: str = ""
    node_name: str = ""


@router.get("/install-plugin", response_class=PlainTextResponse)
async def install_plugin_script(
    request: Request,
    fleet_id: str = Query(default=""),
    api_url: str = Query(default="http://localhost:8000"),
    node_name: str = Query(default=""),
):
    """Generate a bash install script for first-time plugin setup on an OpenClaw gateway."""
    api_key = request.headers.get("X-API-Key", "")
    tenant_id = _resolve_tenant_id()

    script = _generate_install_script(
        api_url=api_url,
        api_key=api_key,
        fleet_id=fleet_id,
        tenant_id=tenant_id,
        node_name=node_name,
    )
    return PlainTextResponse(script, media_type="text/plain")


@router.post("/install-plugin", response_class=PlainTextResponse)
async def install_plugin_script_post(
    request: Request,
    body: InstallPluginRequest,
):
    """Generate a bash install script via POST (preferred — no secrets in URL)."""
    api_key = body.api_key or request.headers.get("X-API-Key", "")
    tenant_id = _resolve_tenant_id()

    script = _generate_install_script(
        api_url=body.api_url,
        api_key=api_key,
        fleet_id=body.fleet_id,
        tenant_id=tenant_id,
        node_name=body.node_name,
    )
    return PlainTextResponse(script, media_type="text/plain")


_VALID_SKILL_AGENTS = {"claude-code", "codex", "both"}


def _derive_api_url_from_request(request: Request) -> str:
    """Pick the URL the caller used to reach this endpoint.

    When the endpoint sits behind a proxy (nginx, Cloud Run, etc.) the
    reverse proxy forwards the original scheme and host via
    ``X-Forwarded-Proto`` and ``X-Forwarded-Host``. We prefer those — that
    way a ``curl https://memclaw.dev/api/v1/install-skill | bash`` yields a
    script that keeps fetching from ``https://memclaw.dev``, not from the
    internal ``http://127.0.0.1:8000`` the upstream service sees.
    """
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def _generate_skill_install_script(*, api_url: str, agent: str, api_key: str = "") -> str:
    """Bash installer for the direct-MCP memclaw skill (Claude Code / Codex).

    Fetches ``static/skills/memclaw/SKILL.md`` (served by the
    ``/skill/memclaw`` endpoint) and writes it to the user-scope skills
    dir(s) for the selected agent runtime(s).

    ``api_key`` — when non-empty, embedded into the script and forwarded
    as ``-H "X-API-Key: ..."`` on the internal curl calls. Required for
    edge-auth-gated deploys (memclaw.dev's nginx rejects unauthenticated
    requests on every path). Empty when the deploy is standalone / lets
    the skill file be fetched without a key.
    """
    safe_api_url = shlex.quote(api_url)
    safe_api_key = shlex.quote(api_key)
    install_claude = agent in {"claude-code", "both"}
    install_codex = agent in {"codex", "both"}

    # Only emit the ``-H X-API-Key: …`` flag when a key was supplied.
    # Otherwise the header becomes an empty string and curl rejects.
    key_header = ' -H "X-API-Key: $MEMCLAW_API_KEY"' if api_key else ""
    skill_url = '"$MEMCLAW_API_URL/api/v1/skill/memclaw"'
    blocks = []
    if install_claude:
        blocks.append(
            'CLAUDE_DIR="$HOME/.claude/skills/memclaw"\n'
            'mkdir -p "$CLAUDE_DIR" || { echo "ERROR: mkdir $CLAUDE_DIR"; exit 1; }\n'
            f'curl -sf{key_header} {skill_url} > "$CLAUDE_DIR/SKILL.md" || '
            '{ echo "ERROR: fetch failed"; exit 1; }\n'
            'echo "  → Claude Code: $CLAUDE_DIR/SKILL.md"'
        )
    if install_codex:
        blocks.append(
            'CODEX_DIR="$HOME/.agents/skills/memclaw"\n'
            'mkdir -p "$CODEX_DIR" || { echo "ERROR: mkdir $CODEX_DIR"; exit 1; }\n'
            f'curl -sf{key_header} {skill_url} > "$CODEX_DIR/SKILL.md" || '
            '{ echo "ERROR: fetch failed"; exit 1; }\n'
            'echo "  → Codex: $CODEX_DIR/SKILL.md"'
        )
    install_blocks = "\n\n".join(blocks)

    # Note on security: the caller already sent us this key to fetch the
    # script; embedding it back is not a net new leak on the wire. Users
    # should treat the script as sensitive (do not paste into Slack, etc.)
    # — the header below flags that in a terse comment, but only emitted
    # when a key is actually present (unauthenticated fetches don't need
    # the warning and should contain no "API-Key" text at all).
    if api_key:
        header_line = (
            "# Installer for the MemClaw direct-MCP skill. Contains a "
            "tenant-scoped credential —\n"
            "# treat as sensitive; do not paste the rendered script into "
            "shared channels.\n"
        )
        key_assign = f"MEMCLAW_API_KEY={safe_api_key}"
    else:
        header_line = ""
        key_assign = ""

    return f"""#!/usr/bin/env bash
{header_line}set -euo pipefail

MEMCLAW_API_URL={safe_api_url}
{key_assign}

echo "=== MemClaw Skill Installer (direct-MCP) ==="
echo ""
echo "Fetching SKILL.md from $MEMCLAW_API_URL and installing to:"

{install_blocks}

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Restart your agent (Claude Code / Codex) — skills are loaded at startup."
echo "  2. Your agent now has MemClaw usage guidance available on-demand."
echo "  3. To update the skill, re-run this installer."
"""


@router.get("/install-skill", response_class=PlainTextResponse)
async def install_skill_script(
    request: Request,
    agent: str = Query(default="both", description="claude-code | codex | both"),
    api_url: str | None = Query(
        default=None,
        description=(
            "Override the server URL the script will install from. Auto-derived "
            "from the request Host (and X-Forwarded-Proto) when omitted — so "
            "``curl https://memclaw.dev/api/v1/install-skill | bash`` just works."
        ),
    ),
):
    """Bash installer for the direct-MCP memclaw skill.

    Serves a shell script that fetches the SKILL.md adapter and writes it to
    ``~/.claude/skills/memclaw/`` (Claude Code) and/or ``~/.agents/skills/memclaw/``
    (Codex). Designed for ``curl -s ... | bash`` use by teammates who have
    already connected via ``claude mcp add`` or the equivalent Codex MCP
    registration.

    - Forwards the caller's ``X-API-Key`` into the generated script so the
      script's internal curl calls pass auth (required on edge-gated deploys).
    - Auto-derives the install URL from the request host when ``api_url`` is
      not passed, so the one-liner works against any deployed host with no
      manual parameter tuning.
    """
    if agent not in _VALID_SKILL_AGENTS:
        return PlainTextResponse(
            f"Invalid 'agent' parameter. Expected one of: {sorted(_VALID_SKILL_AGENTS)}. Got: {agent!r}",
            status_code=400,
        )
    resolved_api_url = api_url if api_url else _derive_api_url_from_request(request)
    api_key = request.headers.get("x-api-key", "")
    script = _generate_skill_install_script(api_url=resolved_api_url, agent=agent, api_key=api_key)
    return PlainTextResponse(script, media_type="text/plain")


@router.get("/skill/memclaw", response_class=PlainTextResponse)
async def skill_memclaw():
    """Serve the direct-MCP SKILL.md adapter.

    Public, auth-free — it's generic usage guidance for Claude Code / Codex
    users with no tenant data in it. Content lives at
    ``static/skills/memclaw/SKILL.md`` (not under ``plugin/`` — it's not an
    OpenClaw artifact). The OpenClaw plugin's own shared skill lives at
    ``plugin/skills/memclaw/SKILL.md`` and is served via ``/plugin-source``.
    """
    if not _skill_md_path.is_file():
        return PlainTextResponse("skill not found", status_code=404)
    return _skill_md_path.read_text(encoding="utf-8")


# Non-versioned aliases for the install bootstrap. The generated install
# script (see ``_generate_install_script``) fetches plugin sources via
# ``$MEMCLAW_API_URL/api/plugin-source`` (no ``v1`` segment) so the same
# script runs unchanged against both OSS and the enterprise gateway —
# enterprise nginx whitelists ``/api/install-plugin`` and
# ``/api/plugin-source`` as unauthenticated bootstrap paths and rewrites
# them to the v1 prefix upstream. In OSS standalone there is no nginx,
# so without these aliases the script's curls 404 at step 5/7.
# Mounted at ``prefix="/api"`` in app.py — the v1 router still serves
# ``/api/v1/...`` for everything else.
plugin_bootstrap_router = APIRouter(tags=["System"])
plugin_bootstrap_router.add_api_route(
    "/plugin-source", plugin_source, methods=["GET"], response_class=PlainTextResponse
)
plugin_bootstrap_router.add_api_route(
    "/install-plugin",
    install_plugin_script,
    methods=["GET"],
    response_class=PlainTextResponse,
)
plugin_bootstrap_router.add_api_route(
    "/install-plugin",
    install_plugin_script_post,
    methods=["POST"],
    response_class=PlainTextResponse,
)
