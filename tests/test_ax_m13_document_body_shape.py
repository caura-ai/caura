"""ax-0917-m-13 — a memories-shaped body is told the document shape.

Documents store their payload under ``data``; memories store it under
``content``. Nothing said so, so an agent that had already used
``POST /memories`` sent ``{"title": ..., "content": ...}`` to ``POST
/documents`` and got back the generic unknown-field 422 plus "Field required"
for three fields it had never heard of. The error named everything wrong with
the request and nothing about what a right one looks like.

What is pinned here:

1.  The naive body's 422 now teaches: it names ``collection`` / ``doc_id`` /
    ``data``, says the payload goes inside ``data``, and carries a body the
    caller can actually send.
2.  The fix is a MESSAGE, not a shape change. ``collection`` and ``doc_id``
    stay required (they are the upsert key — see the idempotency test at the
    bottom, which is what a server-minted ``doc_id`` would have broken), and
    the payload keys are NOT accepted at the top level.
3.  An ordinary typo keeps the ordinary unknown-field 422. The teaching list
    is named, not a category, so widening it into "anything unrecognised" —
    which would swallow the SAFE-01 behaviour — fails here.
"""

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(resp) -> dict:
    """The canonical error envelope, which is what a caller reads."""
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()
    err = body.get("error")
    assert err, f"no canonical error envelope in {body}"
    assert err["code"] == "INVALID_ARGUMENTS", err
    return err


# ---------------------------------------------------------------------------
# 1. The reported body
# ---------------------------------------------------------------------------


async def test_memories_shaped_body_is_told_the_document_shape(client):
    """The exact body from the audit row, and everything it needs to be told.

    One response has to carry the whole correction, because a caller that has
    to discover the shape one 422 at a time is the friction this row is about.
    """
    _tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/documents",
        json={
            "title": "Postgres tuning",
            "content": "Set shared_buffers to 25% of RAM.",
        },
        headers=headers,
    )
    message = _envelope(resp)["message"]

    # Names what was wrong: both borrowed keys, and where they go.
    assert "'title'" in message, message
    assert "'content'" in message, message
    assert "'data'" in message, message

    # Names the three fields a document actually has, and what the pair means —
    # a caller that is not told ``doc_id`` is the upsert key will invent a
    # fresh one per write and never update anything.
    assert "'collection'" in message, message
    assert "'doc_id'" in message, message
    assert "upsert key" in message, message

    # Says which fields this body is missing, rather than leaving the caller to
    # diff its payload against the prose.
    assert "collection, doc_id, data" in message, message

    # And carries a body that can be sent as-is.
    assert '"collection": "notes"' in message, message
    assert '"doc_id": "my-note"' in message, message
    assert '"data"' in message, message

    # ``content`` specifically means the caller may have wanted the other store.
    assert "POST /memories" in message, message


async def test_message_explains_the_summary_consequence(client):
    """The example shows a ``summary``, so it has to say why one is there.

    ``data["summary"]`` is the only embedded field. A caller that copies the
    example without it gets a document that stores and reads back fine and is
    permanently absent from ``POST /documents/search`` — the silent outcome
    ax-0917-h-08 already had to add an ``indexed`` flag for.
    """
    _tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/documents",
        json={"content": "Set shared_buffers to 25% of RAM."},
        headers=headers,
    )
    message = _envelope(resp)["message"]
    assert "summary" in message, message
    assert "/documents/search" in message, message


async def test_missing_list_names_only_what_is_actually_missing(client):
    """A caller that got two of the three fields right is not told to re-send them."""
    _tenant_id, headers = get_test_auth()
    tag = _uid()
    resp = await client.post(
        "/api/v1/documents",
        json={
            "collection": f"notes-{tag}",
            "doc_id": f"doc-{tag}",
            "content": "Set shared_buffers to 25% of RAM.",
        },
        headers=headers,
    )
    message = _envelope(resp)["message"]
    assert "also missing: data." in message, message
    assert "missing: collection" not in message, message


# ---------------------------------------------------------------------------
# 2. The fix is a message, not a shape change
# ---------------------------------------------------------------------------


async def test_payload_keys_are_not_accepted_at_the_top_level(client):
    """No alias: a top-level ``content`` is refused, not folded into ``data``.

    If this ever starts returning 2xx, the store has acquired a second spelling
    for its payload and a precedence rule for bodies that send both.
    """
    _tenant_id, headers = get_test_auth()
    tag = _uid()
    resp = await client.post(
        "/api/v1/documents",
        json={
            "collection": f"notes-{tag}",
            "doc_id": f"doc-{tag}",
            "data": {},
            "content": "Set shared_buffers to 25% of RAM.",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_ordinary_typo_keeps_the_unknown_field_422(client):
    """SAFE-01 is untouched: an unrecognised key that is not a borrowed payload
    name still gets the unknown-field envelope, ``details.unknown_fields``
    included. The teaching list is named, not a category."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": f"notes-{tag}",
            "doc_id": f"doc-{tag}",
            "data": {"title": "Hello"},
            "collectoin": "typo",
        },
        headers=headers,
    )
    err = _envelope(resp)
    assert "collectoin" in err["message"], err["message"]
    assert "collectoin" in err["details"]["unknown_fields"], err["details"]


async def test_payload_keys_inside_data_are_none_of_this_validator_s_business(client):
    """Only the TOP level is inspected. ``data`` is free-form JSONB and the
    keys inside it are the caller's own — a document whose data holds
    ``title`` / ``content`` / ``summary`` is the shape the error recommends,
    so it had better write."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    resp = await client.post(
        "/api/v1/documents",
        json={
            "collection": f"notes-{tag}",
            "doc_id": f"doc-{tag}",
            "data": {
                "title": "Postgres tuning",
                "content": "Set shared_buffers to 25% of RAM.",
                "summary": "How to size shared_buffers.",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["data"]["content"] == "Set shared_buffers to 25% of RAM."
    assert doc["tenant_id"] == tenant_id


async def test_upsert_is_still_idempotent_on_collection_and_doc_id(client):
    """The semantic a server-minted ``doc_id`` would have quietly traded away.

    Sending the same ``(collection, doc_id)`` twice must leave ONE row with the
    second payload — not two rows, which is what "we'll generate an id for you"
    turns every retry into.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    collection = f"notes-{tag}"
    doc_id = f"doc-{tag}"

    for body in ("first", "second"):
        resp = await client.post(
            "/api/v1/documents",
            json={
                "collection": collection,
                "doc_id": doc_id,
                "data": {"content": body, "summary": f"the {body} write"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

    listed = await client.get(
        f"/api/v1/documents?tenant_id={tenant_id}&collection={collection}",
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    docs = listed.json()
    assert len(docs) == 1, docs
    assert docs[0]["data"]["content"] == "second"


# ---------------------------------------------------------------------------
# 3. The published contract says it too
# ---------------------------------------------------------------------------


def test_openapi_schema_describes_where_the_payload_goes():
    """A caller reading the spec should never need the 422 at all.

    ``data`` was an undescribed ``object`` in the published schema, which is
    exactly as informative as the field name. The descriptions are part of the
    fix, not decoration.
    """
    from core_api.app import app

    props = app.openapi()["components"]["schemas"]["DocWriteRequest"]["properties"]

    data_desc = props["data"].get("description", "")
    assert "free-form JSON object" in data_desc, data_desc
    assert "summary" in data_desc, data_desc

    doc_id_desc = props["doc_id"].get("description", "")
    assert "upsert key" in doc_id_desc, doc_id_desc
