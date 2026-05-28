#!/usr/bin/env bash
#
# Regenerate plugin/src/version.ts from plugin/package.json's "version" field.
#
# This script is the SINGLE source of writes to plugin/src/version.ts.
# Do not hand-edit plugin/src/version.ts — bump the version in
# plugin/package.json + plugin/openclaw.plugin.json instead, then run any
# of:
#
#   npm run build               # via "prebuild" hook
#   npm test                    # via "pretest" hook
#   npm run dev                 # in the watch chain
#   bash scripts/gen-version.sh # direct invocation
#
# CI also runs `npm run check:version` (defined in plugin/package.json)
# to fail fast if version.ts ever drifts from package.json without going
# through this script — guarding against future release-management or
# manual-edit drift.
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(python3 -c "import json; print(json.load(open('$ROOT/plugin/package.json'))['version'])")
cat > "$ROOT/plugin/src/version.ts" <<EOF
// Auto-generated from plugin/package.json by scripts/gen-version.sh — do not edit
export const PLUGIN_VERSION = "$VERSION";
EOF
