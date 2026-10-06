"""Nearest registered routes for a path that matched none (ax-0917-m-13).

An agent that does not know this API's shape guesses one, and REST guesses are
predictable: ``/documents/{collection}/{doc_id}`` for a store whose write is
``POST /documents``, ``/memories/search`` for a search that lives at
``/search``. The guess returns 404 — correctly — and the 404 says
``{"detail": "Not Found"}``, which confirms the path is wrong and says nothing
about what is right. The agent's next move is another guess.

The server knows every route it serves, so it can answer the question the
caller was actually asking. This picks the registered paths closest to the one
attempted and hands them back in the error envelope.

The route table comes from ``app.openapi()``, NOT from walking ``app.routes``.
That is not a style preference: since FastAPI 0.137, ``include_router(prefix=…)``
mounts an opaque ``_IncludedRouter`` whose children are reachable only through a
private attribute, so ``app.routes`` no longer carries the prefixed paths at all
— it yields the wrappers. Walking it returns nothing useful, silently, which is
the failure mode that passes review and fails in production (and did: it passed
against a stale local fastapi 0.136 and produced zero suggestions in CI).
``tests/_route_table.py`` documents the same obstacle for the same reason.
The schema is public API, carries the full prefixed path and its methods, and
omits ``include_in_schema=False`` routes — which is right here, since a hidden
legacy alias is not something to point a confused caller at.

Structural first, not fuzzy: routes are ranked by how many leading path
segments they share with the attempt, so ``/api/v1/documents/skills/foo``
surfaces the ``/api/v1/documents`` family and nothing from ``/api/v1/agents``.
Whole-path string similarity would rank ``/api/v1/documents`` against
``/api/v1/comments`` well — one character apart — while a caller who typed the
former meant nothing like the latter.

The narrow exception is a resource named *almost* right, which is the most
common guess of all: ``/keystone`` for ``/keystones``. Nothing shares a prefix
with it past the version, so the structural pass returns nothing precisely
where the caller is closest to being right. A second pass therefore compares
the one differing segment — only that segment, never the whole path, which is
what keeps documents/comments apart.
"""

from __future__ import annotations

# Enough to name a family ("/api/v1/documents"), few enough that the suggestion
# stays a pointer rather than a route dump the caller has to search.
MAX_SUGGESTIONS = 3

# How many leading segments must match before a route is a plausible target.
# Two clears the version prefix (``api``, ``v1``) and requires agreement on the
# resource itself, which is what makes a suggestion worth reading.
MIN_SHARED_SEGMENTS = 3


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _shared_prefix_len(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        # A path parameter matches whatever sat in that position: the caller
        # supplying a real id should still match ``/memories/{memory_id}``.
        if x == y or (y.startswith("{") and y.endswith("}")):
            n += 1
            continue
        break
    return n


def route_table(app) -> dict[str, str]:
    """``{path: "GET,POST"}`` for every documented route on ``app``.

    Built from the OpenAPI schema (see module docstring for why, not
    ``app.routes``). ``app.openapi()`` caches into ``app.openapi_schema`` after
    the first call, so the cost lands once — and on a 404, which is not a hot
    path. Any failure yields an empty table: a broken schema must degrade the
    suggestion, never turn a 404 into a 500.
    """
    try:
        schema = app.openapi()
    except Exception:  # a suggestion is never worth turning a 404 into a 500
        return {}

    table: dict[str, str] = {}
    for route_path, operations in (schema.get("paths") or {}).items():
        verbs = ",".join(
            sorted(
                verb.upper() for verb in operations if verb.upper() not in {"HEAD", "OPTIONS", "PARAMETERS"}
            )
        )
        if verbs:
            table[route_path] = verbs
    return table


def suggest_routes(path: str, routes: dict[str, str]) -> list[str]:
    """Return up to ``MAX_SUGGESTIONS`` registered routes nearest to ``path``.

    ``routes`` is a ``{path: "GET,POST"}`` mapping from :func:`route_table`.
    Each suggestion is rendered as ``"GET,POST /api/v1/documents"`` so the
    caller learns the verb as well as the path — a guess is as often the wrong
    method as the wrong path.
    """
    want = _segments(path)
    if not want:
        return []

    scored: list[tuple[int, int, str, str]] = []
    for route_path, verbs in routes.items():
        have = _segments(route_path)
        shared = _shared_prefix_len(want, have)
        if shared < MIN_SHARED_SEGMENTS:
            continue
        # Prefer more shared segments, then the route closest in length to the
        # attempt — a caller who supplied two extra segments is likelier to
        # want the deeper route than the bare collection, and vice versa.
        scored.append((-shared, abs(len(have) - len(want)), route_path, verbs))

    scored.sort()
    if scored:
        return [f"{verbs} {route_path}" for _, _, route_path, verbs in scored[:MAX_SUGGESTIONS]]
    return _near_miss_on_the_resource(want, routes)


def _singular(word: str) -> str:
    """Crude English singular, enough for resource names.

    Deliberately not a library: the input is a path segment from a closed set
    of names this server chose, so the three rules that cover them all are
    worth more than a dependency that also handles "geese".
    """
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("ses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _same_resource(a: str, b: str) -> bool:
    """True when two segments name the same resource up to plurality."""
    return a == b or _singular(a) == _singular(b)


def _near_miss_on_the_resource(want: list[str], routes: dict[str, str]) -> list[str]:
    """Routes whose resource segment is a near-miss of the one attempted.

    Runs only when the structural pass found nothing, so it can never dilute a
    confident answer — it fills the case where the caller is one plural away
    and the prefix match therefore fails completely.

    Scoped to the segment in the position that failed, compared against real
    route segments in that same position. ``difflib`` on the whole path is what
    confuses unrelated resources; on a single segment, against a closed set of
    names the server actually serves, it answers the question it is asked.
    """
    import difflib

    if len(want) < MIN_SHARED_SEGMENTS:
        return []
    prefix, resource = want[: MIN_SHARED_SEGMENTS - 1], want[MIN_SHARED_SEGMENTS - 1]

    by_segment: dict[str, list[tuple[str, str]]] = {}
    for route_path, verbs in routes.items():
        have = _segments(route_path)
        if len(have) < MIN_SHARED_SEGMENTS or have[: MIN_SHARED_SEGMENTS - 1] != prefix:
            continue
        seg = have[MIN_SHARED_SEGMENTS - 1]
        if seg.startswith("{"):
            continue
        by_segment.setdefault(seg, []).append((route_path, verbs))

    # Plural agreement first, because that IS the common guess and it is exact:
    # ``keystone`` for ``keystones``, ``memory`` for ``memories``. No
    # similarity threshold can cover the second of those without also merging
    # ``documents`` with ``comments`` — those score 0.71 and 0.71, so any cutoff
    # that admits one admits the other.
    close = [seg for seg in by_segment if _same_resource(seg, resource)]
    if not close:
        # Backstop for the rest: a typo, not a plural. 0.8 is deliberately
        # tight — documents/comments sits at 0.71 and must never be suggested
        # for the other.
        close = difflib.get_close_matches(resource, list(by_segment), n=2, cutoff=0.8)

    out: list[str] = []
    for seg in close:
        for route_path, verbs in sorted(by_segment[seg], key=lambda r: len(_segments(r[0]))):
            out.append(f"{verbs} {route_path}")
            if len(out) >= MAX_SUGGESTIONS:
                return out
    return out
