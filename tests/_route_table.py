"""Walking the real app's route table, for tests that need every declaration.

WHY THIS IS NOT ``app.routes``. FastAPI 0.137 mounts
``include_router(prefix=...)`` as an opaque ``_IncludedRouter`` whose routes are
reachable only through the private ``original_router``. ``core_api/app.py``
documents the same obstacle at its ``_TIMEOUT_OPT_OUT_PATHS`` guard, and
declines to depend on it at import time for the same reason a dependency bump
should not become a production boot failure.

Because the attribute is private, a version bump can make this return NOTHING,
and returning less is the failure mode that passes. Every caller must therefore
assert something an empty walk breaks. Both of today's do, by construction
rather than by promise: ``test_capability_usage`` requires every
``_REST_CAPABILITY`` key to match a route, so an empty walk makes all of them
unmatched, and ``test_request_observation`` requires each probe path to appear
exactly once, which an empty walk makes zero.

SCOPE, because two other walks in this repo look like this one and must NOT be
folded into it. They answer the opposite question about prefixes:

    this helper                     UNPREFIXED  ``/memories``, ``/health``
    ``test_authz_gate_inventory``   PREFIXED    ``/api/v1/memories``
    ``scripts/tenant_scope_gate``   PREFIXED, and in another tree

The difference is load-bearing, not an accident of who wrote which. The two
callers here match against values that are unprefixed on purpose:
``_REST_CAPABILITY`` is keyed ``("GET", "/memories")`` and ``PROBE_ROUTES``
holds ``/health`` rather than ``/api/v1/health``, both because the middleware
reads the route template before the prefix is applied. Descending via
``original_router`` alone yields exactly that. The inventory additionally reads
``include_context`` to rebuild the prefix, because its allowlist keys are the
paths a client calls. Unifying the two would break one side or the other.
"""

from __future__ import annotations

from collections.abc import Iterator


def iter_route_declarations(routes) -> Iterator[tuple[str, frozenset[str]]]:
    """``(unprefixed path, methods)`` for every route declaration under ``routes``.

    One tuple per DECLARATION rather than per path: a path declared twice yields
    twice, which is what lets a caller assert that it is declared exactly once.

    A route with no ``methods`` attribute — a ``Mount``, a WebSocket — is
    skipped. One with an empty ``methods`` is yielded with an empty set rather
    than dropped, so a caller that distinguishes "declared with no verbs" from
    "not declared" still can.
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from iter_route_declarations(inner.routes)
            continue
        path = getattr(route, "path", None)
        if path is None or not hasattr(route, "methods"):
            continue
        yield path, frozenset(route.methods or ())
