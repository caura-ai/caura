#!/bin/sh
# With uv already installed, install the released CLI and join:
#   sh scripts/caura-join.sh --url https://caura.example
# Set CAURA_JOIN_REF explicitly for development from a reviewed source ref.
set -eu

main() {
  version="0.3.4" # Update only after the new version is published to PyPI.
  spec="caura-bus-cli==${version}"
  if [ -n "${CAURA_JOIN_REF:-}" ]; then
    spec="git+https://github.com/caura-ai/caura@${CAURA_JOIN_REF}#subdirectory=clients/collaboration/cli"
  fi

  if ! command -v uv >/dev/null 2>&1; then
    echo "caura-join: install uv first (https://docs.astral.sh/uv/getting-started/installation/)" >&2
    exit 1
  fi

  echo "caura-join: installing ${spec}" >&2
  uv tool install --quiet "$spec" >&2
  bin="$(uv tool dir --bin)"
  case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "caura-join: note: add $bin to your PATH to run caura-bus later (or run: uv tool update-shell)" >&2 ;;
  esac
  exec "$bin/caura-bus" join "$@" </dev/null
}

main "$@"
