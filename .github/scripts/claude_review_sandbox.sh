#!/usr/bin/env bash
# Linux-only reviewer boundary. Never fall back to running on the host.
# stdin and argv are review data; real credentials never cross from the job env.
set -euo pipefail
ulimit -c 0

command -v bwrap >/dev/null || { echo 'Reviewer sandbox unavailable' >&2; exit 1; }
NODE=$(readlink -f "$(command -v node)")
CLAUDE=$(readlink -f "$(command -v claude)")
NODE_ROOT=$(dirname "$(dirname "$NODE")")
case "$CLAUDE" in
  /usr/*|"$NODE_ROOT"/*) ;;
  *) echo 'Claude must be installed in the Node runtime prefix' >&2; exit 1 ;;
esac

umask 077
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT
mkdir "$WORK/repo"
# Only committed source: no .git credentials, untracked .env, runner command files,
# or host home. Archive extraction does not execute repository hooks or scripts.
git archive HEAD | tar -x -C "$WORK/repo"

MOUNTS=(--ro-bind /usr /usr)
for path in /bin /lib /lib64; do
  if [ -L "$path" ]; then
    MOUNTS+=(--symlink "$(readlink "$path")" "$path")
  elif [ -d "$path" ]; then
    MOUNTS+=(--ro-bind "$path" "$path")
  fi
done
case "$NODE_ROOT" in
  /usr|/usr/*) ;;
  *) MOUNTS+=(--ro-bind "$NODE_ROOT" "$NODE_ROOT") ;;
esac

# The native CLI needs procfs. A new PID namespace exposes ONLY sandbox
# processes, all of which receive a short-lived loopback proxy token instead of
# the provider key. The proxy (and its real key) stay outside this namespace.
# No sysfs, host /tmp, /run or home mounts. Empty HOME prevents loading host
# credentials/settings. No GH_TOKEN, CAURA_AGENTS_KEY, NODE_OPTIONS, etc.
env -i PATH=/usr/bin:/bin aa-exec -p caura-review-bwrap -- bwrap \
  --unshare-all --share-net --die-with-parent --new-session --cap-drop ALL \
  --dev /dev --tmpfs /tmp --dir /home/reviewer --proc /proc \
  "${MOUNTS[@]}" \
  --ro-bind /etc/ssl/certs /etc/ssl/certs \
  --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --ro-bind /etc/hosts /etc/hosts \
  --ro-bind "$WORK/repo" /workspace --chdir /workspace \
  --setenv PATH "$(dirname "$NODE"):/usr/bin:/bin" \
  --setenv HOME /home/reviewer --setenv TMPDIR /tmp --setenv LANG C.UTF-8 \
  --setenv ANTHROPIC_API_KEY "${REVIEW_PROXY_TOKEN:?Proxy token required}" \
  --setenv ANTHROPIC_BASE_URL "${REVIEW_PROXY_URL:?Proxy URL required}" \
  -- "$CLAUDE" "$@"
