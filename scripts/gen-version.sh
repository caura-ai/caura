#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(python3 -c "import json; print(json.load(open('$ROOT/plugin/package.json'))['version'])")
cat > "$ROOT/plugin/src/version.ts" <<EOF
// Auto-generated from plugin/package.json — do not edit
export const PLUGIN_VERSION = "$VERSION";
EOF
