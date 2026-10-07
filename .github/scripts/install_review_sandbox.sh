#!/usr/bin/env bash
# Provision only on the disposable Ubuntu Actions runner, before secrets arrive.
set -euo pipefail
sudo apt-get update
sudo apt-get install -y bubblewrap apparmor-utils

# Ubuntu restricts unprivileged user namespaces. Grant them to this named
# launcher profile, rather than disabling that restriction system-wide or
# running the reviewer as root. The filesystem policy is enforced by bwrap.
# No executable attachment: only our explicit aa-exec invocation selects it.
PROFILE=$(mktemp)
trap 'rm -f -- "$PROFILE"' EXIT
cat > "$PROFILE" <<'APPARMOR'
abi <abi/4.0>,
profile caura-review-bwrap flags=(unconfined) {
  userns,
}
APPARMOR
sudo apparmor_parser -r "$PROFILE"

npm install -g @anthropic-ai/claude-code@2.1.159
REVIEW_PROXY_TOKEN=sandbox-smoke-test REVIEW_PROXY_URL=http://127.0.0.1:1 \
  bash "$(dirname "$0")/claude_review_sandbox.sh" --version
