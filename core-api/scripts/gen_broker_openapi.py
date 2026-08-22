#!/usr/bin/env python3
"""Generate / verify the frozen v1 broker OpenAPI contract for core-api.

The baseline (``core-api/openapi.broker.json``) is the FROZEN v1 contract for
the broker-facing gateway operations that memclawd (the on-prem broker) calls
against Caura cloud. It is a *subset* of core-api's full ~91-path OpenAPI
surface: only the broker operations plus the schema / security components
those operations reach (transitively).

Broker operations (gateway paths), all of them called by ``internal/cloud``
in caura-ai/memclawd:

    POST   /api/v1/memories/bulk
    POST   /api/v1/search
    GET    /api/v1/health
    GET    /api/v1/version
    GET    /api/v1/memories                  (memory_list)
    GET    /api/v1/memories/{memory_id}      (memory_recall)
    PATCH  /api/v1/memories/{memory_id}      (memory_update)
    DELETE /api/v1/memories/{memory_id}      (memory_delete)

The last four were added once the broker's MCP dispatcher was wired to serve
``memory_list`` / ``memory_recall`` / ``memory_update`` / ``memory_delete``.
The tools shipped; the contract baseline was not widened with them, so a
breaking change to those operations passed this gate. That is the same
undeclared-contract failure mode as the #723-#736 series, one layer out.

``info`` is normalized to a fixed contract identity (``CONTRACT_VERSION``)
rather than core-api's rolling package version, so the baseline only changes
when the broker API *shape* changes -- not on every release. Bump
``CONTRACT_VERSION`` only when intentionally shipping a new broker contract
major per the broker<->cloud API-versioning RFC.

Breaking changes to this contract are gated in CI (see .github/workflows/ci.yml):
    1. ``--check`` fails if the committed baseline is stale vs the current code.
    2. ``oasdiff breaking`` fails the PR if this branch's baseline introduces a
       BREAKING change vs main's baseline.

Usage:
    python scripts/gen_broker_openapi.py            # (re)write the baseline
    python scripts/gen_broker_openapi.py --check     # verify; exit 1 if stale
    python scripts/gen_broker_openapi.py --out PATH  # write elsewhere (e.g. tmp)

Import note: ``core_api.app`` needs the repo root on PYTHONPATH for the
``common`` package (e.g. ``PYTHONPATH=<repo-root>`` when run from core-api/).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The broker-facing gateway OPERATIONS that make up the frozen v1 contract,
# as ``path -> methods``. Removing an entry, or renaming a path/method the
# broker calls, is itself a breaking contract change -- keep in sync with the
# broker<->cloud API-versioning RFC. ADDING an entry is additive: it only
# widens what the oasdiff gate protects, so it does not move CONTRACT_VERSION.
#
# METHOD-level, not path-level. ``/api/v1/memories`` also serves POST and
# DELETE, which the broker never calls; gating the whole path item would fail
# this PR's own gate on unrelated changes to those operations and train people
# to ignore it.
#
# Each entry must correspond to something memclawd actually calls -- see the
# ``internal/cloud`` client in caura-ai/memclawd:
#
#   /memories/bulk        POST    cloud.Client.SaveMemory  (memory_save, mirror)
#   /search               POST    cloud.Client.Search      (memory_search)
#   /health               GET     health probe
#   /version              GET     version handshake
#   /memories             GET     cloud.Client.ListMemories   (memory_list)
#   /memories/{memory_id} GET     cloud.Client.GetMemory      (memory_recall)
#                         PATCH   cloud.Client.UpdateMemory   (memory_update)
#                         DELETE  cloud.Client.DeleteMemory   (memory_delete)
BROKER_OPERATIONS: dict[str, tuple[str, ...]] = {
    "/api/v1/memories/bulk": ("post",),
    "/api/v1/search": ("post",),
    "/api/v1/health": ("get",),
    "/api/v1/version": ("get",),
    "/api/v1/memories": ("get",),
    "/api/v1/memories/{memory_id}": ("get", "patch", "delete"),
}

# Path-item keys that are not operations and must survive filtering.
_NON_OPERATION_KEYS = frozenset(
    {"summary", "description", "servers", "parameters", "$ref"}
)

# Frozen broker-contract version, deliberately decoupled from core-api's
# package version. Bump ONLY for an intentional contract major per the RFC.
CONTRACT_VERSION = "v1"
CONTRACT_TITLE = "MemClaw core-api broker contract"

BASELINE_PATH = Path(__file__).resolve().parent.parent / "openapi.broker.json"


def _operation_count() -> int:
    """Total operations in the contract, for the CLI summary line."""
    return sum(len(methods) for methods in BROKER_OPERATIONS.values())


def _collect_refs(node: object) -> set[str]:
    """Recursively collect every local ``$ref`` target string in ``node``."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value)
            else:
                found |= _collect_refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_refs(item)
    return found


def _security_scheme_names(paths: dict, default_security: object) -> set[str]:
    """Names of the security schemes the broker OPERATIONS actually require.

    An operation's effective security is its own ``security`` if declared, else
    the spec-level default. Collecting the EFFECTIVE requirement per operation —
    rather than unconditionally unioning the global block — keeps schemes that
    no broker endpoint uses out of the contract, so the baseline stays accurate
    and future oasdiff runs don't flag spurious breaking changes if the full
    spec's global security later changes.
    """

    def scheme_names(security: object) -> set[str]:
        out: set[str] = set()
        for requirement in security or []:  # type: ignore[union-attr]
            if isinstance(requirement, dict):
                out |= set(requirement.keys())
        return out

    names: set[str] = set()
    for path_item in paths.values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            # Operation-level ``security`` OVERRIDES the global default (an
            # explicit ``[]`` means "no auth"); when absent the default applies.
            names |= scheme_names(operation.get("security", default_security))
    return names


def build_broker_spec() -> dict:
    """Build the filtered, deterministic broker-contract OpenAPI document."""
    # Imported lazily so ``--help`` stays cheap and import errors surface here.
    from core_api.app import app

    full = app.openapi()
    all_paths = full.get("paths", {})

    # Verify at METHOD granularity: a path that survives while the operation
    # the broker calls is renamed or dropped is exactly as breaking as losing
    # the path, and a path-only check would sail past it.
    missing = [
        f"{method.upper()} {path}"
        for path, methods in BROKER_OPERATIONS.items()
        for method in methods
        if method not in all_paths.get(path, {})
    ]
    if missing:
        raise SystemExit(
            "gen_broker_openapi: broker operation(s) missing from core-api OpenAPI: "
            + ", ".join(sorted(missing))
            + "\nA broker endpoint was renamed or removed -- that is itself a "
            "breaking contract change. Update BROKER_OPERATIONS and the RFC."
        )

    paths = {
        path: {
            key: value
            for key, value in all_paths[path].items()
            if key in methods or key in _NON_OPERATION_KEYS
        }
        for path, methods in BROKER_OPERATIONS.items()
    }

    # Transitive closure of components (any type) reachable from the broker
    # paths via ``$ref``, so every reference in the baseline resolves.
    all_components = full.get("components", {})
    reachable: dict[str, set[str]] = {}
    frontier = _collect_refs(paths)
    visited: set[str] = set()
    while frontier:
        ref = frontier.pop()
        if ref in visited:
            continue
        visited.add(ref)
        parts = ref.lstrip("#/").split("/")  # ["components", <type>, <name>]
        if len(parts) != 3 or parts[0] != "components":
            continue
        ctype, name = parts[1], parts[2]
        node = all_components.get(ctype, {}).get(name)
        if node is None:
            raise SystemExit(f"gen_broker_openapi: dangling $ref in spec: {ref}")
        reachable.setdefault(ctype, set()).add(name)
        frontier |= _collect_refs(node)

    # Security schemes are referenced by name via ``security``, not ``$ref``.
    scheme_names = _security_scheme_names(paths, full.get("security"))
    declared_schemes = all_components.get("securitySchemes", {})
    used_schemes = {n for n in scheme_names if n in declared_schemes}
    if used_schemes:
        reachable.setdefault("securitySchemes", set()).update(used_schemes)

    components = {
        ctype: {name: all_components[ctype][name] for name in sorted(names)}
        for ctype, names in reachable.items()
    }

    # Explicit ALLOWLIST of top-level keys (not a denylist): only the keys the
    # broker contract deliberately carries. A denylist would let any future
    # top-level key app.openapi() gains (``servers``, ``tags``, ``externalDocs``,
    # …) leak silently into the frozen baseline, causing spurious oasdiff noise
    # or stale-baseline CI failures with no broker-endpoint change. A new key
    # must be added here consciously. ``info`` is normalized to the frozen
    # contract identity.
    spec = {"openapi": full["openapi"]}
    spec["info"] = {"title": CONTRACT_TITLE, "version": CONTRACT_VERSION}
    spec["paths"] = paths
    if components:
        spec["components"] = components
    return spec


def _serialize(spec: dict) -> str:
    """Deterministic JSON: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify the frozen v1 broker OpenAPI baseline.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed baseline matches current code; exit 1 if stale.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the spec to this path instead of the committed baseline.",
    )
    args = parser.parse_args(argv)

    if args.check and args.out:
        print("--out is ignored with --check; pass one or the other", file=sys.stderr)
        return 1

    rendered = _serialize(build_broker_spec())

    if args.check:
        if not BASELINE_PATH.exists():
            print(
                f"broker OpenAPI baseline missing: {BASELINE_PATH}\n"
                "Run: python core-api/scripts/gen_broker_openapi.py",
                file=sys.stderr,
            )
            return 1
        if BASELINE_PATH.read_text(encoding="utf-8") != rendered:
            print(
                "broker OpenAPI baseline stale -- run "
                "gen_broker_openapi.py and commit core-api/openapi.broker.json",
                file=sys.stderr,
            )
            return 1
        print(f"broker OpenAPI baseline up to date ({_operation_count()} operations).")
        return 0

    out = args.out or BASELINE_PATH
    out.write_text(rendered, encoding="utf-8")
    print(f"wrote {out} ({_operation_count()} broker operations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
