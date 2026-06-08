#!/bin/bash

# PreToolUse hook — blocks `git commit` unless ruff + mypy pass.
# Runs on Bash tool calls that contain "git commit".
# Adapted for multi-service monorepo: checks each service with its own pyproject.toml.

COMMAND=$(jq -r '.tool_input.command // empty')

# Only intercept git commit commands (including amend)
[[ "$COMMAND" =~ \bgit[[:space:]]+commit\b ]] || exit 0

SERVICES=("core-api" "core-storage-api")

for svc in "${SERVICES[@]}"; do
  [[ -f "$svc/pyproject.toml" ]] || continue

  echo "Pre-commit: ruff check ($svc)..."
  uv run --project "$svc" ruff check "$svc/" || { echo "BLOCKED: ruff check failed in $svc"; exit 2; }

  echo "Pre-commit: ruff format --check ($svc)..."
  uv run --project "$svc" ruff format --check "$svc/" || { echo "BLOCKED: ruff format failed in $svc"; exit 2; }

  echo "Pre-commit: mypy ($svc)..."
  uv run --project "$svc" mypy "$svc/src/" || { echo "BLOCKED: mypy failed in $svc"; exit 2; }
done

echo "Pre-commit: all checks passed."
