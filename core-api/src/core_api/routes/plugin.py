"""Plugin source, hash, and install script endpoints."""

import hashlib
import json
import logging
import shlex
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from core_api.version_compat import MIN_AUTO_DEPLOY_PLUGIN_VERSION

logger = logging.getLogger(__name__)

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
    "context-engine.internal.ts",
    "agent-auth.ts",
    "health.ts",
    "install-id.ts",
    "identity.ts",
    "reconcile-skills.ts",
    "keystones.ts",
    "openclaw-sdk-bridge.ts",
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


def _read_combined_plugin_source() -> tuple[str, list[str], list[str]]:
    """Read every file listed in `_plugin_files` + `_plugin_root_files`
    in deterministic order and concatenate. Missing files produce a
    `logger.warning(...)` (a missing entry means either the deployment
    dropped a file or the on-disk layout drifted from the allowlist —
    either is a real bug worth surfacing).

    Returns ``(combined, present_src, present_root)`` so callers can
    declare the canonical content alongside its exact constituent
    filenames — the manifest endpoint uses ``present_src`` /
    ``present_root`` for its ``src_files`` / ``root_files`` fields so
    a client never sees a file listed in the manifest whose bytes
    aren't in the hash (and vice versa).

    Shared by `plugin_manifest` and `plugin_source_hash` so the two
    endpoints always agree on content and missing-file behaviour.
    """
    combined = ""
    present_src: list[str] = []
    present_root: list[str] = []
    for fname in _plugin_files:
        path = _plugin_dir / fname
        if path.is_file():
            combined += path.read_text(encoding="utf-8")
            present_src.append(fname)
        else:
            logger.warning("expected plugin file missing on disk: %s", path)
    for fname in sorted(_plugin_root_files):
        path = _plugin_dir.parent / fname
        if path.is_file():
            combined += path.read_text(encoding="utf-8")
            present_root.append(fname)
        else:
            logger.warning("expected plugin root file missing on disk: %s", path)
    return combined, present_src, present_root


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


@router.get("/plugin-manifest")
async def plugin_manifest():
    """Single source of truth for what a plugin should fetch on update.

    Returns the canonical version string, the list of source files
    (``plugin/src/*.ts``) and root files (``tools.json``,
    ``skills/memclaw/SKILL.md``, ``openclaw.plugin.json``) the plugin
    must download to materialise a fresh install or upgrade, plus the
    combined content hash so callers can short-circuit when they're
    already current.

    Why a manifest instead of two separate hardcoded lists (Python here
    + bash in the install script + TypeScript in
    ``plugin/src/heartbeat.ts``): drift. A 2026-04-16 refactor added
    ``paths.ts`` and ``logger.ts`` to ``plugin/src`` and forgot the
    bash list, breaking every fresh install for three days. Today the
    plugin's deploy command (``heartbeat.ts:processCommand``) carries
    its OWN hardcoded array of 15 files — drifting from this module's
    22-entry ``_plugin_files`` — so a plugin upgrade silently leaves
    six files stale on disk. Centralising the answer here lets the
    plugin pull the live list and removes one drift class entirely.

    Response shape (stable contract; see CAURA-444):
        {
            "version":                         "<plugin version from plugin/package.json>",
            "src_files":                       ["index.ts", ...],
            "root_files":                      ["openclaw.plugin.json", ...],
            "content_hash":                    "<sha256 over all served files>",
            "min_auto_deploy_plugin_version":  "<server floor for plugin auto-deploy>"
        }

    ``version`` is the **plugin's** release version (read from
    ``plugin/package.json``), NOT the backend's ``VERSION`` — the two
    are decoupled (PR #131). The plugin's deploy handler uses this
    value to stamp ``version.ts`` and ``package.json`` post-build, so
    it must reflect what the plugin SHOULD be after a successful deploy.

    ``min_auto_deploy_plugin_version`` is the server-side floor below
    which plugins must NOT auto-upgrade (matches
    ``MIN_AUTO_DEPLOY_PLUGIN_VERSION`` enforced in /fleet/heartbeat).
    Surfaced here so the plugin can read the current floor without a
    second round trip.

    Auth: unauthenticated (mirrors ``/plugin-source`` and
    ``/plugin-source-hash``); the plugin uses this on the update path
    before its API key may have been issued for the new install.
    """
    combined, present_src, present_root = _read_combined_plugin_source()
    if not combined:
        # Consistent with /plugin-source-hash: no files served means
        # 404, not 200-with-empty-hash. A pre-fix 200 + empty hash was
        # indistinguishable from "current — nothing changed" and would
        # fool clients into thinking they're up to date when the server
        # is actually broken.
        return JSONResponse({"detail": "Plugin source not found"}, status_code=404)
    content_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    # Use ``present_*`` (not the raw allowlists) so the manifest never
    # advertises a file the hash doesn't cover. A missing file is
    # silently dropped from both — clients then either skip it (if
    # their local copy exists) or 404 it via ``/plugin-source`` and
    # take their own fallback path. Either way: never a state where
    # the listed files and the hash disagree.
    return {
        "version": _plugin_version(),
        "src_files": present_src,
        "root_files": present_root,
        "content_hash": content_hash,
        "min_auto_deploy_plugin_version": MIN_AUTO_DEPLOY_PLUGIN_VERSION,
    }


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
    combined, _present_src, _present_root = _read_combined_plugin_source()
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

# Preflight: warn (don't fail) if the local OpenClaw runtime is older
# than the minimum the plugin's APIs require. Reasons we don't hard-fail:
# (1) operators sometimes run patched older builds; (2) OpenClaw versions
# below the minimum still load the plugin partially (legacy
# before_prompt_build path), so install proceeds and the operator decides.
# See plugin/openclaw.plugin.json + AGENT-INSTALL.md for the rationale.
MIN_OPENCLAW_VERSION="2026.3.22"
INSTALLED_OPENCLAW_VERSION="$(openclaw --version 2>/dev/null | awk '{{print $2}}' || true)"
# POSIX-safe version compare via awk: split each dotted string on '.',
# compare components numerically, zero-pad shorter side. Avoids `sort -V`
# which is GNU coreutils — older macOS BSD `sort` lacked it (and even
# where present, the binary is shadowed by Homebrew gnu-coreutils on
# some operator boxes), producing a false "version too old" warning.
# Prints "lt" if a < b, "eq" if equal, "gt" if a > b. Usage:
#   _version_compare 2026.3.22 2026.4.2 → "lt"
_version_compare() {{
  awk -v a="$1" -v b="$2" 'BEGIN {{
    na = split(a, av, ".")
    nb = split(b, bv, ".")
    n = (na > nb) ? na : nb
    for (i = 1; i <= n; i++) {{
      ai = (i <= na) ? av[i] + 0 : 0
      bi = (i <= nb) ? bv[i] + 0 : 0
      if (ai < bi) {{ print "lt"; exit }}
      if (ai > bi) {{ print "gt"; exit }}
    }}
    print "eq"
  }}'
}}
if [ -z "$INSTALLED_OPENCLAW_VERSION" ]; then
  echo "WARNING: openclaw CLI not found in PATH or returned no version."
  echo "         Plugin v$MEMCLAW_PLUGIN_VERSION targets OpenClaw >= $MIN_OPENCLAW_VERSION."
elif [ "$(_version_compare "$INSTALLED_OPENCLAW_VERSION" "$MIN_OPENCLAW_VERSION")" = "lt" ]; then
  echo "WARNING: OpenClaw $INSTALLED_OPENCLAW_VERSION is older than the recommended"
  echo "         minimum $MIN_OPENCLAW_VERSION for MemClaw plugin v$MEMCLAW_PLUGIN_VERSION."
  echo "         The recall-policy gate (assemble({{prompt}}) param) and"
  echo "         registerContextEngine slot landed in OpenClaw v$MIN_OPENCLAW_VERSION;"
  echo "         on older runtimes the plugin falls back to before_prompt_build but"
  echo "         loses per-turn message context. Continuing install — upgrade OpenClaw"
  echo "         when convenient."
else
  echo "OpenClaw $INSTALLED_OPENCLAW_VERSION (>= $MIN_OPENCLAW_VERSION) — OK."
fi
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
# The manifest endpoint advertises which ``src_files`` and ``root_files``
# the plugin must download. Driving the loops from the manifest removes
# a drift class that previously bit us: a hardcoded shell list here would
# silently lag the backend's ``_plugin_files`` allowlist whenever a new
# ``plugin/src/*.ts`` was added (e.g. ``paths.ts``/``logger.ts`` 2026-04-16,
# ``keystones.ts`` 2026-05). Fall back to the hardcoded list with a warning
# if ``python3`` isn't available (older minimal containers) or the
# manifest endpoint is unreachable.
echo "[4/7] Fetching plugin manifest from $MEMCLAW_API_URL..."
# Use ``/api/v1/plugin-manifest`` with X-API-Key rather than the
# unversioned bootstrap alias: the enterprise nginx gateway only
# allowlists a small set of unauthenticated bootstrap paths
# (``/plugin-source``, ``/plugin-source-hash``, ``/install-plugin``), and
# adding a new path there is an enterprise-repo change. The install
# script always has ``MEMCLAW_API_KEY`` (required arg), so sending it
# satisfies the gateway's auth subrequest. OSS standalone (no gateway)
# ignores the header and serves the same route — works in both.
MANIFEST_JSON=$(curl $CURL_INSECURE -sf -H "X-API-Key: $MEMCLAW_API_KEY" "$MEMCLAW_API_URL/api/v1/plugin-manifest" || true)

SRC_FILES=""
ROOT_FILES=""
if [ -n "$MANIFEST_JSON" ] && command -v python3 >/dev/null 2>&1; then
  SRC_FILES=$(printf '%s' "$MANIFEST_JSON" | python3 -c 'import json,sys; m=json.load(sys.stdin); print(" ".join(m.get("src_files") or []))' 2>/dev/null || true)
  ROOT_FILES=$(printf '%s' "$MANIFEST_JSON" | python3 -c 'import json,sys; m=json.load(sys.stdin); print(" ".join(m.get("root_files") or []))' 2>/dev/null || true)
fi

if [ -z "$SRC_FILES" ] || [ -z "$ROOT_FILES" ]; then
  echo "WARNING: Could not fetch/parse /api/v1/plugin-manifest (python3 missing or endpoint unreachable). Falling back to hardcoded file list."
  SRC_FILES="index.ts prompt-section.ts tools.ts tool-specs.ts version.ts env.ts transport.ts validation.ts config.ts paths.ts logger.ts resolve-agent.ts tool-definitions.ts deploy.ts heartbeat.ts educate.ts context-engine.ts context-engine.internal.ts agent-auth.ts health.ts install-id.ts identity.ts reconcile-skills.ts keystones.ts openclaw-sdk-bridge.ts"
  ROOT_FILES="openclaw.plugin.json tools.json skills/memclaw/SKILL.md"
fi

# SECURITY: validate every manifest-supplied filename BEFORE any disk
# writes start. Mirrors the equivalent up-front guard in
# plugin/src/heartbeat.ts so the install script's bootstrap path closes
# the same path-traversal gap as the in-process deploy handler.
#
# Threat: a MITM (relevant when ``CURL_INSECURE=-k`` is set against a
# self-signed cert) or a compromised manifest server could otherwise
# direct the per-file curls into ``$HOME/.ssh/authorized_keys`` or any
# path outside ``$PLUGIN_DIR``. Fail fast — exit before the first curl
# so we never leave a half-written plugin tree behind.
#
# The single combined loop (rather than per-fetch ``case`` checks
# inside each loop) is deliberate: if a bad rootfile slips through and
# we've already written srcfiles, the operator has a corrupted install
# and no clean state to roll back to. Validating both lists in one pass
# is the canonical "validate at the boundary" shape.
for _f in $SRC_FILES $ROOT_FILES; do
  case "$_f" in
    /*|*/../*|*/..|../*|..|''|*//*)
      echo "ERROR: Manifest contained unsafe filename: $_f — aborting install."
      exit 1
      ;;
  esac
done

# 5. Fetch latest plugin source from MemClaw server
echo "[5/7] Fetching latest plugin source from $MEMCLAW_API_URL..."
for srcfile in $SRC_FILES; do
  curl $CURL_INSECURE -sf "$MEMCLAW_API_URL/api/plugin-source?file=$srcfile" > "$PLUGIN_DIR/src/$srcfile" || {{
    echo "ERROR: Could not fetch $srcfile from $MEMCLAW_API_URL/api/plugin-source?file=$srcfile"
    exit 1
  }}
done
# Root files (``openclaw.plugin.json``, ``tools.json``, nested skill paths
# like ``skills/memclaw/SKILL.md``) — mkdir -p their parent so nested
# paths from the manifest don't trip over a missing directory.
for rootfile in $ROOT_FILES; do
  _parent_dir=$(dirname "$PLUGIN_DIR/$rootfile")
  mkdir -p "$_parent_dir" || {{
    echo "ERROR: Could not create directory $_parent_dir"
    exit 1
  }}
  curl $CURL_INSECURE -sf "$MEMCLAW_API_URL/api/plugin-source?file=$rootfile" > "$PLUGIN_DIR/$rootfile" || {{
    echo "ERROR: Could not fetch $rootfile from $MEMCLAW_API_URL/api/plugin-source?file=$rootfile"
    exit 1
  }}
done
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

// Claim the exclusive memory slot (controls registerMemoryPromptSection +
// registerMemoryRuntime delivery) AND the contextEngine slot (controls
// ContextEngine.assemble() — the path that injects the <keystone_rules>
// block into the system prompt on every turn). Without contextEngine set
// to 'memclaw', OpenClaw falls back to the default "legacy" engine and
// our assemble() never runs, so keystones never appear in the prompt
// even though the tool surface is registered. Confirmed against
// OpenClaw 2026.5.4 dist/registry-DFFgCbcm.js:241 resolveContextEngine.
if (!config.plugins.slots) config.plugins.slots = {{}};
config.plugins.slots.memory = 'memclaw';
config.plugins.slots.contextEngine = 'memclaw';

if (!config.plugins.load) config.plugins.load = {{}};
if (!Array.isArray(config.plugins.load.paths)) config.plugins.load.paths = [];
if (!config.plugins.load.paths.includes(pluginDir)) config.plugins.load.paths.push(pluginDir);

if (!config.tools) config.tools = {{}};
if (!Array.isArray(config.tools.alsoAllow)) config.tools.alsoAllow = [];
const tools = ['memclaw_recall','memclaw_write','memclaw_manage','memclaw_doc','memclaw_list','memclaw_entity_get','memclaw_tune','memclaw_insights','memclaw_evolve','memclaw_stats','memclaw_keystones'];
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

# Skills installable via ``/install-skill?skill=…`` and served at
# ``/skill/{skill}``. Strictly allowlisted — the value is interpolated into a
# filesystem path and the generated script, so an arbitrary value must never
# reach either. ``memclaw`` is the default (the operational manual); the
# opt-in ``company-brain`` posture skill layers on top of it.
_SKILL_LABELS = {"memclaw": "MemClaw", "company-brain": "Company Brain"}
_VALID_SKILLS = frozenset(_SKILL_LABELS)
_static_skills_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "static" / "skills"
# Precomputed name → SKILL.md path map. Keys are the constant allowlist, so
# the request's ``skill`` value is only ever a dict KEY (membership / lookup),
# never a path segment — the served path is always one of these fixed
# constants. That keeps user input out of the path expression entirely.
_SKILL_FILES = {name: _static_skills_dir / name / "SKILL.md" for name in _SKILL_LABELS}


def _derive_api_url_from_request(request: Request) -> str:
    """Pick the URL the caller used to reach this endpoint.

    When the endpoint sits behind a proxy (nginx, Cloud Run, etc.) the
    reverse proxy forwards the original scheme and host via
    ``X-Forwarded-Proto`` and ``X-Forwarded-Host``. We prefer those — that
    way a ``curl https://memclaw.net/api/v1/install-skill | bash`` yields a
    script that keeps fetching from ``https://memclaw.net``, not from the
    internal ``http://127.0.0.1:8000`` the upstream service sees.
    """
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def _generate_skill_install_script(
    *, api_url: str, agent: str, api_key: str = "", skill: str = "memclaw"
) -> str:
    """Bash installer for a direct-MCP skill (Claude Code / Codex).

    Fetches ``static/skills/<skill>/SKILL.md`` (served by the
    ``/skill/<skill>`` endpoint) and writes it to the user-scope skills
    dir(s) for the selected agent runtime(s). ``skill`` is one of
    {@link _VALID_SKILLS} (validated by the caller); ``memclaw`` is the
    default and renders the original installer byte-for-byte.

    ``api_key`` — when non-empty, embedded into the script and forwarded
    as ``-H "X-API-Key: ..."`` on the internal curl calls. Required for
    edge-auth-gated deploys (memclaw.net's nginx rejects unauthenticated
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
    # ``skill`` is allowlisted by the caller, so interpolating it into the
    # path and URL is safe. For skill="memclaw" every line below is identical
    # to the original installer.
    label = _SKILL_LABELS[skill]
    skill_url = f'"$MEMCLAW_API_URL/api/v1/skill/{skill}"'
    blocks = []
    if install_claude:
        blocks.append(
            f'CLAUDE_DIR="$HOME/.claude/skills/{skill}"\n'
            'mkdir -p "$CLAUDE_DIR" || { echo "ERROR: mkdir $CLAUDE_DIR"; exit 1; }\n'
            f'curl -sf{key_header} {skill_url} > "$CLAUDE_DIR/SKILL.md" || '
            '{ echo "ERROR: fetch failed"; exit 1; }\n'
            'echo "  → Claude Code: $CLAUDE_DIR/SKILL.md"'
        )
    if install_codex:
        blocks.append(
            f'CODEX_DIR="$HOME/.agents/skills/{skill}"\n'
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
            f"# Installer for the {label} direct-MCP skill. Contains a "
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

echo "=== {label} Skill Installer (direct-MCP) ==="
echo ""
echo "Fetching SKILL.md from $MEMCLAW_API_URL and installing to:"

{install_blocks}

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Restart your agent (Claude Code / Codex) — skills are loaded at startup."
echo "  2. Your agent now has {label} usage guidance available on-demand."
echo "  3. To update the skill, re-run this installer."
"""


@router.get("/install-skill", response_class=PlainTextResponse)
async def install_skill_script(
    request: Request,
    agent: str = Query(default="both", description="claude-code | codex | both"),
    skill: str = Query(
        default="memclaw",
        description="Which skill to install: memclaw (default) | company-brain",
    ),
    api_url: str | None = Query(
        default=None,
        description=(
            "Override the server URL the script will install from. Auto-derived "
            "from the request Host (and X-Forwarded-Proto) when omitted — so "
            "``curl https://memclaw.net/api/v1/install-skill | bash`` just works."
        ),
    ),
):
    """Bash installer for the direct-MCP memclaw skill.

    Serves a shell script that fetches the SKILL.md adapter for the requested
    skill (default: memclaw) and writes it to the user-scope skills directory
    for the selected agent runtime(s). Designed for ``curl -s ... | bash`` use
    by teammates who have already connected via ``claude mcp add`` or the
    equivalent Codex MCP registration.

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
    if skill not in _VALID_SKILLS:
        return PlainTextResponse(
            f"Invalid 'skill' parameter. Expected one of: {sorted(_VALID_SKILLS)}. Got: {skill!r}",
            status_code=400,
        )
    resolved_api_url = api_url if api_url else _derive_api_url_from_request(request)
    api_key = request.headers.get("x-api-key", "")
    script = _generate_skill_install_script(
        api_url=resolved_api_url, agent=agent, api_key=api_key, skill=skill
    )
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


@router.get("/skill/{skill}", response_class=PlainTextResponse)
async def skill_by_name(skill: str):
    """Serve a direct-MCP SKILL.md adapter by name (allowlisted).

    Pairs with ``/install-skill?skill=…``. Public, auth-free — generic usage
    guidance with no tenant data. ``memclaw`` is also served by the explicit
    ``/skill/memclaw`` route above (registered first, so it wins for that
    name and keeps the default path unchanged); this handles the rest of the
    allowlist (e.g. ``company-brain``).

    ``skill`` is used only as a key into the precomputed ``_SKILL_FILES`` map,
    so the served path is always a fixed constant — the request value never
    becomes a path segment (no path-injection surface).
    """
    path = _SKILL_FILES.get(skill)
    if path is None or not path.is_file():
        return PlainTextResponse("skill not found", status_code=404)
    return path.read_text(encoding="utf-8")


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
# ``/plugin-manifest`` is also bootstrap-time (the install script's step
# [4/7] curls it to drive the SRC_FILES / ROOT_FILES download loops).
# Without this alias, the unversioned path 404s and the script silently
# falls through to its hardcoded fallback — defeating the manifest's
# whole purpose of removing the hardcoded-list drift class.
plugin_bootstrap_router.add_api_route("/plugin-manifest", plugin_manifest, methods=["GET"])
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
