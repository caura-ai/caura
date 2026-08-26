"""STM is advertised over REST and reachable nowhere — CAP-01 / F6.

Three facts, each verified against a running stack before this was written:

* ``POST /stm/notes`` and ``POST /stm/bulletin`` do not exist. There is no REST
  write route for short-term memory at all, so a caller who follows the docs
  gets a bare 405.
* Every read, delete and promote is gated on ``USE_STM``, which is off in the
  hosted deployment and is a server setting rather than a per-tenant one — so
  a hosted customer cannot enable it at any price.
* The gate's message used to read "Set USE_STM=true to enable short-term
  memory": instructions the reader it reaches cannot follow.

STM stays dead by standing decision. These tests pin the LABELLING, and one of
them pins the other half — that nobody quietly adds the missing write routes
and turns a documentation fix into a feature. A25 is not reopened by any of
this.
"""

from __future__ import annotations

import pytest

from core_api.app import app

STM_PATHS = ("/api/v1/stm/notes", "/api/v1/stm/bulletin", "/api/v1/stm/promote")

# Phrases that make the constraint findable by someone reading the spec rather
# than the source. Matching on meaning-bearing substrings rather than the exact
# sentence, so wording can improve without breaking the test.
REQUIRED_SIGNALS = ("plugin-only", "hosted")


@pytest.mark.unit
def test_every_advertised_stm_operation_is_labelled() -> None:
    schema = app.openapi()
    operations = [
        (path, method, op)
        for path, item in schema["paths"].items()
        if path.startswith("/api/v1/stm/")
        for method, op in item.items()
    ]
    assert operations, "no STM operations in the schema — did the routes move?"
    for path, method, op in operations:
        description = (op.get("description") or "").lower()
        assert description, f"{method.upper()} {path} is advertised with no description"
        for signal in REQUIRED_SIGNALS:
            assert signal in description, (
                f"{method.upper()} {path} does not tell the reader it is {signal}; "
                "an operation nobody can call must say so where it is advertised"
            )


@pytest.mark.unit
def test_the_stm_tag_carries_the_same_warning() -> None:
    """The sidebar is what a reader sees before opening any single operation."""
    tags = {t["name"]: t.get("description", "") for t in app.openapi().get("tags", [])}
    assert "stm" in tags, "the stm tag has no metadata"
    for signal in REQUIRED_SIGNALS:
        assert signal in tags["stm"].lower()


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/api/v1/stm/notes", "/api/v1/stm/bulletin"])
def test_stm_has_no_rest_write_route(path: str) -> None:
    """The 'don't build it' half.

    CAP-01 is a labelling task precisely because the tempting fix for a 405 is
    to add the route. If someone does, this fails and the decision gets made
    deliberately instead of in a pull request about documentation.
    """
    item = app.openapi()["paths"].get(path, {})
    assert "post" not in item, (
        f"{path} grew a POST route. STM is plugin-only by standing decision — "
        "adding a REST write path is a product decision, not a docs fix."
    )


@pytest.mark.unit
async def test_the_disabled_gate_does_not_give_unfollowable_advice(client) -> None:
    """A hosted caller cannot set USE_STM, so it must not be the only remedy offered."""
    response = await client.get(
        "/api/v1/stm/notes", params={"tenant_id": "t-f6", "agent_id": "a-f6"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"].lower()
    assert "plugin-only" in detail
    # It must point somewhere the reader can actually go.
    assert "/memories" in detail or "/search" in detail
