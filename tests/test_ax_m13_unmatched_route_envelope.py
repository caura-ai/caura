"""ax-0917-m-13 — a guessed URL got `{"detail":"Not Found"}` and nothing else.

Every 4xx and 5xx on this surface returns `{"error": {"code", "message",
"details"}}` — except the one a caller is most likely to meet while finding its
way around. A path that matched no route at all is a 404 raised by Starlette's
router, not by any handler of ours, so it skipped the envelope entirely: no
code, and a shape nothing else uses.

The audit hit it guessing `/documents/{collection}/{doc_id}` for a store whose
write is `POST /documents`. The guess was reasonable and the answer was "no",
with no indication of what "yes" would look like — so the next move is another
guess.

Two things change. The envelope arrives, with a code that says *route*, not
*row*. And since the server knows every route it serves, the response names the
nearest ones.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def _get(client, path, method="GET"):
    """``client`` is the shared conftest fixture — the real app, with
    standalone mode initialised."""
    return await client.request(method, path)


# ── the envelope ─────────────────────────────────────────────────────────


async def test_an_unmatched_path_returns_the_canonical_envelope(client):
    resp = await _get(client, "/api/v1/documents/skills/my-doc")

    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "NO_SUCH_ROUTE"
    assert err["details"]["path"] == "/api/v1/documents/skills/my-doc"
    assert err["details"]["method"] == "GET"


async def test_the_code_distinguishes_a_missing_route_from_a_missing_row(client):
    """`NOT_FOUND` on this surface means the row you asked for is not there.
    A caller that cannot tell that from "this URL has never existed" either
    retries a path that will never work, or concludes its data is gone when
    only its URL was wrong."""
    from core_api.errors import code_for_status

    resp = await _get(client, "/api/v1/documents/skills/my-doc")

    assert resp.json()["error"]["code"] != code_for_status(404)


async def test_the_old_bare_shape_is_preserved(client):
    """`{"detail": "Not Found"}` is what every existing client parses. The
    envelope is added alongside it, exactly as the HTTPException handler has
    done since the envelope was introduced — not swapped for it."""
    resp = await _get(client, "/api/v1/nope")

    assert resp.json()["detail"] == "Not Found"


# ── the suggestions ──────────────────────────────────────────────────────


async def test_a_guessed_document_path_names_the_real_one(client):
    """The exact guess from the audit."""
    resp = await _get(client, "/api/v1/documents/skills/my-doc")

    suggestions = resp.json()["error"]["details"]["did_you_mean"]
    assert any("/api/v1/documents/{doc_id}" in s for s in suggestions)


async def test_suggestions_carry_the_method(client):
    """A guess is as often the wrong verb as the wrong path, and a path alone
    would send a caller to retry the same method against it."""
    resp = await _get(client, "/api/v1/keystone", method="POST")

    suggestions = resp.json()["error"]["details"]["did_you_mean"]
    assert any(s.startswith(("GET", "POST", "DELETE")) for s in suggestions)


@pytest.mark.parametrize(
    "guess,expected",
    [
        ("/api/v1/keystone", "/api/v1/keystones"),
        ("/api/v1/memory", "/api/v1/memories"),
    ],
)
async def test_a_singular_resource_finds_its_plural(client, guess, expected):
    """The most common guess of all, and the one the structural pass is worst
    at: nothing shares a prefix past the version, so prefix matching returns
    nothing exactly where the caller is closest to being right."""
    resp = await _get(client, guess)

    suggestions = resp.json()["error"]["details"]["did_you_mean"]
    assert any(s.endswith(expected) for s in suggestions)


async def test_an_unrelated_path_gets_no_suggestions(client):
    """Silence beats a wrong pointer. A caller told "did you mean /memories?"
    after asking for something unrelated will try it."""
    resp = await _get(client, "/totally/elsewhere")

    assert "did_you_mean" not in resp.json()["error"]["details"]


def test_a_similar_looking_but_unrelated_resource_is_not_suggested():
    """`documents` and `comments` are one plausible typo apart by string
    similarity (0.71) and name nothing alike. This is the pair any loosened
    threshold merges first, so it is pinned directly."""
    from core_api.route_suggestions import suggest_routes

    routes = {"/api/v1/documents": "GET", "/api/v1/documents/{doc_id}": "GET"}

    assert suggest_routes("/api/v1/comments", routes) == []


def test_the_route_table_comes_from_the_schema_not_app_routes():
    """Load-bearing, and it fails silently the wrong way round.

    Since FastAPI 0.137, ``include_router(prefix=…)`` mounts an opaque
    ``_IncludedRouter`` whose children are reachable only through a private
    attribute — so ``app.routes`` yields the wrappers, not the prefixed paths.
    Walking it returns an empty suggestion list rather than an error, which is
    exactly what happened: green against a stale local fastapi 0.136, zero
    suggestions in CI on the version ``pyproject`` actually requires.

    Asserting the table is non-empty and carries a real prefixed path is what
    catches that, since the reader of a `did_you_mean` that is merely absent
    cannot tell "nothing was close" from "we looked in the wrong place".
    """
    from core_api.app import app
    from core_api.route_suggestions import route_table

    table = route_table(app)

    assert table, "empty route table — the suggestion source is not working"
    assert "/api/v1/documents/{doc_id}" in table
    assert "GET" in table["/api/v1/documents/{doc_id}"]


def test_a_broken_schema_does_not_turn_the_404_into_a_500():
    """The suggestion is a courtesy. If building the schema raises, the caller
    must still get their 404."""
    from core_api.route_suggestions import route_table

    class _Exploding:
        def openapi(self):
            raise RuntimeError("schema build failed")

    assert route_table(_Exploding()) == {}


# ── the routes that DO exist are untouched ───────────────────────────────


async def test_a_real_route_still_answers_normally(client):
    """The handler must only fire for a path that matched nothing. A route
    that matched and then raised its own 404 — or any other status — keeps the
    behaviour it had."""
    resp = await _get(client, "/api/v1/health")

    assert resp.status_code == 200


async def test_a_matched_route_raising_its_own_404_keeps_NOT_FOUND(client):
    """The distinction the new code exists to make, exercised from the other
    side. `/memories/{memory_id}` matches, and asking for an id that is not
    there is a missing ROW — so it must keep `NOT_FOUND` and gain no route
    suggestion. Only a path that matched nothing becomes `NO_SUCH_ROUTE`.

    `request.scope["route"]` is what separates them: Starlette sets it once a
    route matches, and the handler defers to the normal mapping whenever it is
    present.
    """
    resp = await _get(client, "/api/v1/memories/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "NOT_FOUND"
    assert "did_you_mean" not in (err.get("details") or {})
