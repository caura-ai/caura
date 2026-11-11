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
    echo "::warning::${what} failed or ran past ${limit}s."
    return 1
  fi
}

# The runner prefers Azure even when its archive downloads stall. Keep the
# official Ubuntu fallbacks, and change only this disposable runner's mirror list.
# Do not remove anything unless the official archive entry is already present.
if [[ -f /etc/apt/apt-mirrors.txt ]] && grep -Eq '^https?://archive[.]ubuntu[.]com/ubuntu/' /etc/apt/apt-mirrors.txt; then
  sudo sed -i '/azure[.]archive[.]ubuntu[.]com/d' /etc/apt/apt-mirrors.txt
fi

# Match the runner image's short acquire bounds so retries cannot consume the
# whole outer download budget before apt can try another mirror.
APT=(sudo apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)

# On 2026-11-11 the runners stopped resolving azure.archive.ubuntu.com and then
# archive.ubuntu.com as well (actions/runner-images#15297), so pruning to the
# official mirror no longer helped. If the runner's mirrors fail, install from a
# mirror outside ubuntu.com instead. It only carries the files: apt still checks
# every index against Ubuntu's archive key. The list replaces the runner's
# sources for these calls only, and List-Cleanup=0 keeps the runner's indexes.
FALLBACK_MIRROR=https://mirrors.edge.kernel.org/ubuntu
FALLBACK_LIST=$(mktemp)
PROFILE=$(mktemp)
trap 'rm -f -- "$FALLBACK_LIST" "$PROFILE"' EXIT
for suite in noble noble-updates noble-security; do
  echo "deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] $FALLBACK_MIRROR $suite main"
done > "$FALLBACK_LIST"
FALLBACK=(-o "Dir::Etc::SourceList=$FALLBACK_LIST" -o Dir::Etc::SourceParts=- -o APT::Get::List-Cleanup=0)

# 90 s per download: healthy, each takes seconds, and a hung mirror plus the
# fallback and the CLI install below then fit in the callers' 10-minute step.
install_packages() {
  bounded 90 "apt-get update" "${APT[@]}" "$@" update &&
    bounded 90 "apt-get install" "${APT[@]}" "$@" install -y bubblewrap apparmor-utils
}
if ! install_packages; then
  echo "::warning::The runner's Ubuntu mirrors failed. Installing bubblewrap and apparmor-utils from the fallback mirror, $FALLBACK_MIRROR."
  if ! install_packages "${FALLBACK[@]}"; then
    echo "::error::Could not install bubblewrap and apparmor-utils from the runner's mirrors or from $FALLBACK_MIRROR. Re-run the job."
    exit 1
  fi
fi

# Ubuntu restricts unprivileged user namespaces. Grant them to this named
# launcher profile, rather than disabling that restriction system-wide or
# running the reviewer as root. The filesystem policy is enforced by bwrap.
# No executable attachment: only our explicit aa-exec invocation selects it.
cat > "$PROFILE" <<'APPARMOR'
abi <abi/4.0>,
profile caura-review-bwrap flags=(unconfined) {
  userns,
}
APPARMOR
sudo apparmor_parser -r "$PROFILE"

if ! bounded 180 "npm install of the Claude CLI" npm install -g @anthropic-ai/claude-code@2.1.159; then
  echo "::error::Could not install the Claude CLI. Re-run the job."
  exit 1
fi
REVIEW_PROXY_TOKEN=sandbox-smoke-test REVIEW_PROXY_URL=http://127.0.0.1:1 \
  bash "$(dirname "$0")/claude_review_sandbox.sh" --version
