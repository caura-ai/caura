"""The published schema says which way ``superseded_by`` points (M-101).

``GET /memories/{id}/contradictions`` fills ``superseded_by`` from this memory's
``supersedes_id``: the OLDER row this memory replaced. The route's own comment
says so, and MCP ``caura_manage op=lineage`` returns the same key the same way.
The OpenAPI description said the opposite ("the newer memory that superseded
this one"), so a client built from the published schema walked the chain
backwards: it read the live winner as stale and the retired row as its
correction. ``tests/test_api_contradictions.py`` pins the behaviour; this pins
the contract text to it.
"""

from core_api.app import app


def _properties() -> dict:
    schemas = app.openapi()["components"]["schemas"]
    return schemas["MemoryContradictionsResponse"]["properties"]


def test_superseded_by_is_documented_as_the_older_memory():
    description = _properties()["superseded_by"].get("description", "")
    assert description.startswith("The older memory this one superseded"), description


def test_superseded_memories_is_documented_as_the_newer_ones():
    description = _properties()["superseded_memories"].get("description", "")
    assert description.startswith("Newer memories that superseded this"), description
