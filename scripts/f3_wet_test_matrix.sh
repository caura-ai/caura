#!/usr/bin/env bash
# F3 Phase 0 — wet-test the two canonical cells of the
# (embed_on_hot_path × enrich_on_hot_path) matrix end-to-end against
# the local stack, capturing observable state as a golden record.
#
# Phase 0 deliverable: this run on `main` produces baseline JSON
# under `.f3-phase0-baseline/` that Phase 3 must reproduce after the
# deployment_mode swap.
#
# Usage:
#   scripts/f3_wet_test_matrix.sh                 # full matrix
#   scripts/f3_wet_test_matrix.sh inline          # just the (T,T) cell
#   scripts/f3_wet_test_matrix.sh deferred        # just the (F,F) cell
#
# Pre-reqs:
#   - Enterprise stack up via memclaw-local-dev skill
#   - Real OPENAI_API_KEY in caura-memclaw/.env so embed + enrich actually fire
#   - User authenticated (/tmp/local-dev.token, /tmp/local-dev.cookies)
#
# Env-flag override strategy: the script writes a temporary
# `.env.f3-override` file alongside the user's .env, ordered AFTER it
# on the --env-file chain so the override's values win. The file is
# removed on exit; the user's .env is never touched.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENT_DIR="${REPO_ROOT}/../caura-memclaw-enterprise"
ENV_FILE="${REPO_ROOT}/.env"
COMPOSE_OVERRIDE="${ENT_DIR}/docker-compose.override.yml"
OUTPUT_DIR="${REPO_ROOT}/.f3-phase0-baseline"
OBSERVER="${REPO_ROOT}/scripts/_f3_wet_test_observer.py"

mkdir -p "${OUTPUT_DIR}"
trap 'rm -f "${COMPOSE_OVERRIDE}"' EXIT

restart_with_flags() {
  # F3 Phase 3 reset: legacy ``EMBED_ON_HOT_PATH`` / ``ENRICH_ON_HOT_PATH``
  # env vars are no longer read by Settings (Pydantic ``extra: ignore``
  # silently drops them). ``DEPLOYMENT_MODE`` is the only per-deploy
  # control. The two positional args still map to the canonical cells
  # (true/true → inline, false/false → deferred) so the runner's
  # ``inline`` / ``deferred`` cell names keep their meaning.
  local embed="$1"
  local enrich="$2"
  local mode
  if [ "$embed" = "true" ] && [ "$enrich" = "true" ]; then
    mode="inline"
  elif [ "$embed" = "false" ] && [ "$enrich" = "false" ]; then
    mode="deferred"
  else
    # Asymmetric configurations are not expressible post-F3. Force the
    # conservative default and let the wet test surface it.
    mode="deferred"
  fi
  cat > "${COMPOSE_OVERRIDE}" <<EOF
services:
  core-api:
    environment:
      DEPLOYMENT_MODE: "${mode}"
EOF
  cd "${ENT_DIR}"
  TESTING=1 docker compose \
    --env-file "${ENV_FILE}" \
    up -d --no-deps --force-recreate core-api \
    > /dev/null 2>&1
  cd - > /dev/null

  # Wait for the stack to respond at all. /api/auth/me through the
  # gateway proves nginx is routing; we'll probe a /memories endpoint
  # right after to be sure core-api itself is serving.
  local attempts=0
  while ! curl -fsS -o /dev/null -m 2 http://localhost/api/auth/me \
        -b /tmp/local-dev.cookies 2>/dev/null; do
    sleep 1
    attempts=$((attempts+1))
    if [ "$attempts" -gt 45 ]; then
      echo "auth service did not respond within 45s after env flip"
      return 1
    fi
  done
  # Confirm core-api itself is serving (not just the auth service).
  attempts=0
  while [ "$(curl -s -o /dev/null -w '%{http_code}' \
             -H "Authorization: Bearer $(cat /tmp/local-dev.token 2>/dev/null || echo x)" \
             http://localhost/api/v1/memories?limit=1)" = "502" ]; do
    sleep 1
    attempts=$((attempts+1))
    if [ "$attempts" -gt 45 ]; then
      echo "core-api still 502 after 45s"
      return 1
    fi
  done

  local got
  got=$(docker exec caura-memclaw-enterprise-core-api-1 \
        sh -c 'echo "EMBED=${EMBED_ON_HOT_PATH:-unset} ENRICH=${ENRICH_ON_HOT_PATH:-unset}"')
  printf '  in-container: %s\n' "${got}"
}

# Refresh auth (bootstrap is idempotent; cookie/token may have expired).
EMAIL="dev@example.com" "${HOME}/.claude/skills/memclaw-local-dev/bootstrap.sh" \
  > /dev/null 2>&1 || true

run_cell() {
  # Run BOTH write_mode=strong and write_mode=fast under the same
  # env-flag pair. The strong runs prove the CAURA-229 contract is
  # preserved (strong always inline regardless of flags); the fast
  # runs are where the flags actually steer observable behavior.
  local cell="$1" embed="$2" enrich="$3"
  printf '\n=== restart core-api with EMBED=%s ENRICH=%s ===\n' "${embed}" "${enrich}"
  restart_with_flags "${embed}" "${enrich}"
  python3 "${OBSERVER}" "${cell}" "${OUTPUT_DIR}" strong
  python3 "${OBSERVER}" "${cell}" "${OUTPUT_DIR}" fast
}

case "${1:-all}" in
  inline)   run_cell inline   true  true  ;;
  deferred) run_cell deferred false false ;;
  all)
    run_cell inline   true  true
    run_cell deferred false false
    ;;
  *)
    echo "usage: $0 [inline|deferred|all]"
    exit 2
    ;;
esac

printf '\nbaseline captured under %s\n' "${OUTPUT_DIR}"
ls -la "${OUTPUT_DIR}"
