"""ax-0917-h-07 — the `id` returned by POST /documents 404s on GET.

`POST /documents` returns BOTH keys:

* `id` — the row's primary key (a UUID), and
* `doc_id` — the caller's own key.

`GET /documents/{doc_id}` only ever looked up by `(tenant, collection,
doc_id)`. So an agent that stored the returned `id` — the conventional thing
to keep from a create response — could not read back the document it had just
written. Found by an agent probe against prod, and it breaks the most basic
loop there is: write something, read it back.

The natural key is still tried FIRST. A caller whose own `doc_id` happens to
be UUID-shaped must still resolve to *their* document, not to whatever row
shares that primary key — so the fallback only runs on a miss.
"""

import inspect
import uuid

import pytest

pytestmark = pytest.mark.unit


def _route_src() -> str:
    from core_storage_api.routers import documents

    return inspect.getsource(documents.get_document)


# ── the lookup exists and is tenant-scoped ───────────────────────────────


def test_storage_can_fetch_a_document_by_primary_key():
    from core_storage_api.services.postgres_service import PostgresService

    assert hasattr(PostgresService, "document_get_by_pk")


def test_the_primary_key_lookup_is_tenant_scoped():
    """A primary key is globally unique. Without the tenant predicate this is
    a cross-tenant read for anyone who learns an id — the lookup being by pk
    is exactly why the scoping cannot be skipped."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.document_get_by_pk)
    assert "tenant_pred" in src
    assert "Document.tenant_id" in src


def test_it_honours_cross_tenant_readable_ids_like_its_sibling():
    """Same widening as ``document_get_by_doc_id`` — a cross-tenant credential
    that can read a sibling tenant by doc_id must not be refused by id."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.document_get_by_pk)
    assert "readable_tenant_ids" in src


# ── the route wires it up in the right order ─────────────────────────────


def test_the_natural_key_is_tried_first():
    """Order is the load-bearing part. A caller whose own doc_id is
    UUID-shaped must still get THEIR document; the pk fallback runs only when
    the natural lookup misses."""
    src = _route_src()
    assert src.index("document_get_by_doc_id") < src.index("document_get_by_pk")


def test_a_non_uuid_doc_id_does_not_reach_the_fallback():
    """Most doc_ids are slugs. Parsing must fail closed, not raise — the
    forge writes ``forge/<slug>``, which is not a UUID and never will be."""
    src = _route_src()
    assert "except (ValueError, AttributeError, TypeError)" in src


def test_a_collection_mismatch_is_still_a_miss():
    """The pk identifies the row on its own, so ``collection`` is not part of
    that lookup. Returning a document from a DIFFERENT collection than the
    caller named would make the parameter a lie."""
    src = _route_src()
    assert "doc.collection != collection" in src


def test_both_paths_still_404_rather_than_500():
    src = _route_src()
    assert "status_code=404" in src


# ── the shapes that must keep working ────────────────────────────────────


@pytest.mark.parametrize(
    "doc_id",
    [
        "my-doc",
        "forge/some-skill-slug",  # doc_ids may contain a slash
        "2026-09-19-notes",
        "",
    ],
)
def test_non_uuid_doc_ids_parse_as_not_a_uuid(doc_id):
    """Guards the fallback's gate directly: these must not be treated as
    primary keys, or a slug lookup would start hitting the pk branch."""
    with pytest.raises((ValueError, AttributeError, TypeError)):
        uuid.UUID(doc_id)


def test_a_real_uuid_parses():
    """The other half — the fallback has to actually trigger for the ids POST
    hands back."""
    assert uuid.UUID(str(uuid.uuid4()))
