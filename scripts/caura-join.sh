#!/bin/sh
# Connect this Claude Code or Codex session to Caura in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/caura-ai/caura/release/agent-collaboration-rc/scripts/caura-join.sh \
#     | sh -s -- --url https://caura.example
#
# Installs uv if missing, installs (or updates) the caura-bus CLI, then runs
# `caura-bus join` with the arguments given. The password comes from
# CAURA_PASSWORD or a prompt on the terminal; it is never passed as an argument.
# Set CAURA_JOIN_REF to install the client from another branch or tag.
set -eu

main() {
  ref="${CAURA_JOIN_REF:-release/agent-collaboration-rc}"
  spec="git+https://github.com/caura-ai/caura@${ref}#subdirectory=clients/collaboration/cli"

  if ! command -v uv >/dev/null 2>&1; then
    echo "caura-join: installing uv (https://docs.astral.sh/uv/)" >&2
    command -v curl >/dev/null 2>&1 || { echo "caura-join: curl is required to install uv" >&2; exit 1; }
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >&2
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || { echo "caura-join: uv was installed but is not on PATH" >&2; exit 1; }
  fi

  echo "caura-join: installing the caura-bus client from ${ref}" >&2
  uv tool install --quiet --force --reinstall "$spec" >&2

  bin="$(uv tool dir --bin)"
  case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "caura-join: note: add $bin to your PATH to run caura-bus later (or run: uv tool update-shell)" >&2 ;;
  esac
  # stdin may be this script (curl | sh); prompts read the terminal directly.
  exec "$bin/caura-bus" join "$@" </dev/null
}

main "$@"
