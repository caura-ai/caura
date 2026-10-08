#!/usr/bin/env bash
# Provision only on the disposable Ubuntu Actions runner, before secrets arrive.
set -euo pipefail

# Each download is bounded, so a mirror that stops answering fails this script in
# minutes and says which download it was. Unbounded, on 2026-10-07 both the
# review and the CI job of caura#1992 sat in `apt-get update` while every
# azure.archive.ubuntu.com source answered "Ign:": the review until its job's
# 20-minute timeout cancelled it with no review and no error, and CI, whose job
# has no timeout, for as long as nobody cancelled it. Healthy, this takes ~35 s.
bounded() {
  local limit=$1 what=$2
  shift 2
  if ! timeout -k 15 "$limit" "$@"; then
    echo "::error::${what} failed or ran past ${limit}s; the runner's package mirror is probably unreachable. Re-run the job."
    return 1
  fi
}

# Per request: give up on a stalled connection after 30 s and try it again, rather
# than wait on apt's longer default.
APT=(sudo apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
bounded 180 "apt-get update" "${APT[@]}" update
bounded 180 "apt-get install" "${APT[@]}" install -y bubblewrap apparmor-utils

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

bounded 180 "npm install of the Claude CLI" npm install -g @anthropic-ai/claude-code@2.1.159
REVIEW_PROXY_TOKEN=sandbox-smoke-test REVIEW_PROXY_URL=http://127.0.0.1:1 \
  bash "$(dirname "$0")/claude_review_sandbox.sh" --version
