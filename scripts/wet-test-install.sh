#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# wet-test-install.sh — Install MemClaw plugin from a Git branch
# directly into a remote OpenClaw fleet for testing.
#
# Usage (on the remote machine):
#   curl -sL <raw-url>/scripts/wet-test-install.sh | bash -s -- \
#     --api-url http://localhost:8000 \
#     --api-key mc_key_xxx \
#     --fleet-id my-fleet \
#     --node-name dev-vm-1 \
#     --branch CAURA-888-new-plugin
#
# Or clone first, then:
#   bash scripts/wet-test-install.sh \
#     --api-url http://localhost:8000 \
#     --api-key mc_key_xxx \
#     --fleet-id my-fleet \
#     --node-name dev-vm-1
# ──────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults ──
BRANCH="main"
REPO="git@github.com:caura-ai/caura-memclaw.git"
PLUGIN_DIR="$HOME/.openclaw/plugins/memclaw"
MEMCLAW_API_URL=""
MEMCLAW_API_KEY=""
MEMCLAW_FLEET_ID=""
MEMCLAW_NODE_NAME=""
MEMCLAW_TENANT_ID=""
SKIP_RESTART=""

# ── Parse arguments ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url)      MEMCLAW_API_URL="$2"; shift 2 ;;
    --api-key)      MEMCLAW_API_KEY="$2"; shift 2 ;;
    --fleet-id)     MEMCLAW_FLEET_ID="$2"; shift 2 ;;
    --node-name)    MEMCLAW_NODE_NAME="$2"; shift 2 ;;
    --tenant-id)    MEMCLAW_TENANT_ID="$2"; shift 2 ;;
    --branch)       BRANCH="$2"; shift 2 ;;
    --repo)         REPO="$2"; shift 2 ;;
    --plugin-dir)   PLUGIN_DIR="$2"; shift 2 ;;
    --skip-restart) SKIP_RESTART="1"; shift ;;
    -h|--help)
      echo "Usage: $0 --api-url URL --api-key KEY --fleet-id ID --node-name NAME [--branch BRANCH]"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Validate required args ──
if [[ -z "$MEMCLAW_API_URL" || -z "$MEMCLAW_API_KEY" ]]; then
  echo "ERROR: --api-url and --api-key are required"
  exit 1
fi
if [[ -z "$MEMCLAW_NODE_NAME" ]]; then
  MEMCLAW_NODE_NAME="$(hostname)"
  echo "INFO: --node-name not set, using hostname: $MEMCLAW_NODE_NAME"
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  MemClaw Wet-Test Installer                     ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  Branch:    $BRANCH"
echo "  API URL:   $MEMCLAW_API_URL"
echo "  Fleet ID:  ${MEMCLAW_FLEET_ID:-"(not set)"}"
echo "  Node:      $MEMCLAW_NODE_NAME"
echo "  Plugin:    $PLUGIN_DIR"
echo ""

# ── Step 1: Clone or update ──
CLONE_DIR="/tmp/memclaw-wet-test"
if [[ -d "$CLONE_DIR/.git" ]]; then
  echo "[1/6] Updating existing clone..."
  cd "$CLONE_DIR"
  git fetch origin "$BRANCH" 2>/dev/null
  git checkout "$BRANCH" 2>/dev/null
  git reset --hard "origin/$BRANCH" 2>/dev/null
else
  echo "[1/6] Cloning branch $BRANCH..."
  rm -rf "$CLONE_DIR"
  git clone -b "$BRANCH" --single-branch --depth 1 "$REPO" "$CLONE_DIR"
  cd "$CLONE_DIR"
fi

# ── Step 2: Install dependencies ──
echo "[2/6] Installing dependencies..."
cd plugin
npm install --production=false 2>&1 | tail -1

# ── Step 3: Build ──
echo "[3/6] Building plugin..."
npm run build 2>&1 | tail -2
echo "    Build OK"

# ── Step 4: Install to plugin directory ──
echo "[4/6] Installing to $PLUGIN_DIR..."
mkdir -p "$PLUGIN_DIR/src"
mkdir -p "$PLUGIN_DIR/dist"

# Copy source, dist, package files
cp -r src/* "$PLUGIN_DIR/src/"
cp -r dist/* "$PLUGIN_DIR/dist/"
cp package.json "$PLUGIN_DIR/"
cp tsconfig.json "$PLUGIN_DIR/" 2>/dev/null || true
cp openclaw.plugin.json "$PLUGIN_DIR/"

# Copy node_modules if not already present
if [[ ! -d "$PLUGIN_DIR/node_modules" ]]; then
  cp -r node_modules "$PLUGIN_DIR/" 2>/dev/null || true
fi

echo "    Installed $(find "$PLUGIN_DIR/dist" -name '*.js' | wc -l | tr -d ' ') compiled files"

# ── Step 5: Write .env ──
echo "[5/6] Writing .env..."
ENV_FILE="$PLUGIN_DIR/.env"

# Preserve existing values if .env exists
if [[ -f "$ENV_FILE" ]]; then
  echo "    Existing .env found — backing up to .env.bak"
  cp "$ENV_FILE" "$ENV_FILE.bak"
fi

cat > "$ENV_FILE" << ENVEOF
# MemClaw wet-test config — generated $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Branch: $BRANCH
MEMCLAW_API_URL=$MEMCLAW_API_URL
MEMCLAW_API_KEY=$MEMCLAW_API_KEY
MEMCLAW_FLEET_ID=$MEMCLAW_FLEET_ID
MEMCLAW_NODE_NAME=$MEMCLAW_NODE_NAME
MEMCLAW_TENANT_ID=$MEMCLAW_TENANT_ID
MEMCLAW_AUTO_WRITE_TURNS=true
MEMCLAW_AUTO_FIX_CONFIG=true
ENVEOF

echo "    .env written"

# ── Step 6: Update openclaw.json ──
echo "[6/6] Updating openclaw.json..."
OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"
if [[ -f "$OPENCLAW_CONFIG" ]]; then
  # Use node to safely update JSON
  node -e "
    const fs = require('fs');
    const config = JSON.parse(fs.readFileSync('$OPENCLAW_CONFIG', 'utf-8'));

    // Ensure plugins section
    if (!config.plugins) config.plugins = {};
    if (!Array.isArray(config.plugins.allow)) config.plugins.allow = [];
    if (!config.plugins.allow.includes('memclaw')) config.plugins.allow.push('memclaw');

    if (!config.plugins.entries) config.plugins.entries = {};
    config.plugins.entries.memclaw = { enabled: true };

    if (!config.plugins.load) config.plugins.load = {};
    if (!Array.isArray(config.plugins.load.paths)) config.plugins.load.paths = [];
    const pluginDir = '$PLUGIN_DIR';
    if (!config.plugins.load.paths.includes(pluginDir)) config.plugins.load.paths.push(pluginDir);

    // Ensure tools are allowed
    if (!config.tools) config.tools = {};
    if (!Array.isArray(config.tools.alsoAllow)) config.tools.alsoAllow = [];
    const tools = [
      'memclaw_recall','memclaw_write','memclaw_manage','memclaw_doc',
      'memclaw_list','memclaw_entity_get','memclaw_tune','memclaw_insights',
      'memclaw_evolve','memclaw_stats','memclaw_share_skill','memclaw_unshare_skill'
    ];
    for (const t of tools) {
      if (!config.tools.alsoAllow.includes(t)) config.tools.alsoAllow.push(t);
    }

    fs.writeFileSync('$OPENCLAW_CONFIG', JSON.stringify(config, null, 2) + '\n');
    console.log('    openclaw.json updated');
  " 2>&1
else
  echo "    WARNING: $OPENCLAW_CONFIG not found — you'll need to configure manually"
fi

# ── Restart gateway ──
echo ""
if [[ -n "$SKIP_RESTART" ]]; then
  echo "Skipping restart (--skip-restart flag set)"
else
  echo "Restarting OpenClaw gateway..."
  if systemctl --user restart openclaw-gateway 2>/dev/null; then
    echo "    Gateway restarted"
  elif command -v openclaw >/dev/null 2>&1; then
    echo "    systemctl not available — restart manually: openclaw restart"
  else
    echo "    Could not restart automatically — restart OpenClaw manually"
  fi
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Wet-test install complete!                     ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║  Plugin:  $PLUGIN_DIR"
echo "║  Branch:  $BRANCH"
echo "║  Source:  $CLONE_DIR/plugin"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "To verify:"
echo "  openclaw gateway status"
echo "  # or check logs for: [memclaw] Smoke test passed"
echo ""
echo "To uninstall:"
echo "  rm -rf $PLUGIN_DIR $CLONE_DIR"
echo "  # Then remove 'memclaw' entries from $OPENCLAW_CONFIG"
echo ""
