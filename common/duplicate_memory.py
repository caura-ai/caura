"""Wire shape for the "this content already exists" 409, shared by both services.

Why this module exists. The winning row's id used to travel as English. Storage
built the sentence ``Duplicate memory exists: <uuid>``, core-api lifted it out
of storage's JSON verbatim, raised it as its own 409 detail, and the MCP server
regex-parsed the uuid back out at the far end (``_DUPLICATE_DETAIL_RE``). A
uuid was serialised into prose, shipped across two service boundaries, and
recovered with a regular expression — by us, against our own API, which is the
clearest possible evidence that the contract was wrong. Anything a client
needed that the sentence did not happen to mention was simply unavailable, and
one thing it never mentioned was whether the row it names is still active.

The messages here are byte-identical to the ones they replace, deliberately.
They are what a human reads, what four test modules assert on, and what any
customer who parsed them already depends on; the structured fields are added
ALONGSIDE rather than instead. That also lets the two services deploy in either
order — an old core-api reading a new storage response still finds ``detail``
where it expects it, and a new core-api reading an old storage response finds
no structured fields and falls back to the prose.

Pure data: no framework imports, so both services and their tests can use it.
"""

from __future__ import annotations

DUPLICATE_MEMORY_CODE = "DUPLICATE_MEMORY"

# Why the write was refused. Distinguishing these is most of the point: an
# exact-hash duplicate means "use this row instead", a semantic one means "a
# judge decided these say the same thing", and the third means "the row that
# beat you has since been deleted, so try again" — three different next moves
# that the single word "duplicate" collapsed into one.
REASON_EXACT = "exact_content_hash"
REASON_SEMANTIC = "semantic_similarity"
REASON_RACE_NOT_LIVE = "winner_no_longer_live"

# The prose forms. Kept as functions rather than format strings at each call
# site so a future wording change cannot drift between the producers, and so
# the regex fallback in mcp_server has exactly one thing to stay in step with.
NOT_LIVE_MESSAGE = "Duplicate memory exists but is no longer live; retry the write"


def exact_message(existing_id: object) -> str:
    return f"Duplicate memory exists: {existing_id}"


def near_message(existing_id: object) -> str:
    return f"Near-duplicate memory exists: {existing_id}"


def duplicate_fields(
    *,
    reason: str,
    existing_id: object = None,
    existing_status: str | None = None,
    similarity: float | None = None,
) -> dict:
    """The structured half: everything the sentence could not carry.

    ``existing_status`` is the field the prose never had. A duplicate of an
    archived or conflicted row is not the same situation as a duplicate of an
    active one, and a caller told only "duplicate" cannot tell which it got.
    Omitted keys are left out entirely rather than sent as null, so a consumer
    can distinguish "not applicable" from "unknown".
    """
    fields: dict = {"reason": reason}
    if existing_id is not None:
        fields["existing_id"] = str(existing_id)
    if existing_status is not None:
        fields["existing_status"] = existing_status
    if similarity is not None:
        fields["similarity"] = round(float(similarity), 4)
    return fields


def core_api_detail(message: str, **fields: object) -> dict:
    """The ``{code, message, details}`` detail core-api's handler expands.

    ``app.http_exception_handler`` recognises this shape and emits the
    canonical envelope while keeping top-level ``detail`` as the plain message
    string — so this adds a field for new clients without moving one for old
    ones.
    """
    return {
        "code": DUPLICATE_MEMORY_CODE,
        "message": message,
        "details": dict(fields),
    }
