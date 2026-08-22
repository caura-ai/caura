"""Guards on the frozen broker contract baseline (``openapi.broker.json``).

CI already checks that the baseline is fresh (``gen_broker_openapi.py --check``)
and that it introduces no breaking change vs main (``oasdiff breaking``). What
neither catches is a broker operation entering the contract with an **untyped**
response body: oasdiff can only flag a breaking response change against a
declared shape, so ``"schema": {}`` means the operation is listed in the gate
while its response is not actually pinned by it.

That is the failure mode this whole baseline exists to prevent — a gate that
reads as protection and isn't — so the untyped set is an explicit allowlist
rather than a footnote.
"""

from __future__ import annotations

import json
import pathlib

import pytest

_BASELINE = (
    pathlib.Path(__file__).resolve().parent.parent / "core-api" / "openapi.broker.json"
)
_METHODS = ("get", "post", "patch", "delete", "put")

# Operations whose 200 body is deliberately NOT pinned by the contract, each
# with the reason. Adding an entry is a conscious decision to leave a response
# shape unguarded — that is the point of listing them here.
#
#   GET /health, GET /version   — liveness/handshake payloads with no
#       response_model on the route; trivial, stable shapes.
#
# ``GET /memories/{memory_id}`` was listed here until its handler stopped returning
# a ``JSONResponse`` (FastAPI skips response validation for a Response object, so a
# ``response_model`` alongside one documents a guarantee the code does not make).
# It now returns a dict against ``MemoryDetailResponse`` and is genuinely typed —
# and the assertion below FAILS on a stale entry, which is what removed it.
_UNTYPED_200 = {
    ("get", "/api/v1/health"),
    ("get", "/api/v1/version"),
}


def _baseline() -> dict:
    return json.loads(_BASELINE.read_text())


def _operations():
    for path, path_item in sorted(_baseline()["paths"].items()):
        for method, operation in sorted(path_item.items()):
            if method in _METHODS:
                yield method, path, operation


@pytest.mark.unit
def test_every_broker_response_is_typed_or_explicitly_allowlisted() -> None:
    """A contract entry with an untyped 200 body is not actually pinned by it."""
    untyped = set()
    for method, path, operation in _operations():
        body = (operation.get("responses", {}).get("200", {}).get("content") or {}).get(
            "application/json"
        )
        # No 200 content at all (e.g. DELETE returns 204) is not an untyped body.
        if body is not None and not body.get("schema"):
            untyped.add((method, path))

    unexpected = untyped - _UNTYPED_200
    assert not unexpected, (
        "broker operation(s) added to the frozen contract with an UNTYPED 200 body: "
        + ", ".join(f"{m.upper()} {p}" for m, p in sorted(unexpected))
        + ". oasdiff cannot detect a breaking response change without a declared "
        "shape, so listing these in the contract does not protect their responses. "
        "Give the route a response_model it genuinely honours, or add it to "
        "_UNTYPED_200 with the reason."
    )

    # The allowlist must not outlive the problem: an entry that is now typed
    # should be removed, or it quietly permits a future regression.
    stale = _UNTYPED_200 - untyped
    assert not stale, (
        "allowlisted operation(s) now have a typed 200 body — remove them from "
        "_UNTYPED_200 so it keeps meaning something: "
        + ", ".join(f"{m.upper()} {p}" for m, p in sorted(stale))
    )


@pytest.mark.unit
def test_baseline_operations_match_the_generator_table() -> None:
    """The committed baseline must contain exactly ``BROKER_OPERATIONS``.

    ``--check`` in CI regenerates and compares, which covers this too; asserting
    it here means a hand-edited baseline fails in the local test run rather than
    only on the runner.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_gen_broker_openapi",
        _BASELINE.parent / "scripts" / "gen_broker_openapi.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    declared = {
        (method, path)
        for path, methods in module.BROKER_OPERATIONS.items()
        for method in methods
    }
    present = {(method, path) for method, path, _ in _operations()}
    assert present == declared, (
        f"baseline drifted from BROKER_OPERATIONS.\n"
        f"  in baseline only: {sorted(present - declared)}\n"
        f"  in table only:    {sorted(declared - present)}\n"
        f"Regenerate with core-api/scripts/gen_broker_openapi.py."
    )
