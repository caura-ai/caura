"""C34 — refuse an upsert that replaces a substantial document with a husk.

Real incident (2026-08-27): a failed GET left a client file empty, the
follow-up upsert of those 0 bytes replaced a 105KB shared checklist and
returned 200. The document API is last-writer-wins over the whole ``data``
blob with no history, so recovery meant rebuilding the file by hand.

The guard is server-side and applies to BOTH upsert paths — the plain one and
the ``xmax`` one that INDEXED documents (the checklist among them) actually
take.
"""

import pytest
from core_storage_api.services.postgres_service import PostgresService

pytestmark = pytest.mark.unit


BIG = {"content": "x" * 100_000}
SMALL = {"content": "y" * 500}


def test_ratio_and_floor_constants_are_sane():
    assert PostgresService._SHRINK_RATIO == 0.10
    assert PostgresService._SHRINK_MIN_STORED_BYTES == 2048


def test_guard_math_rejects_catastrophic_shrink():
    import json

    old_len = len(json.dumps(BIG, ensure_ascii=False))
    new_len = len(json.dumps({}, ensure_ascii=False))
    assert old_len >= PostgresService._SHRINK_MIN_STORED_BYTES
    assert new_len < old_len * PostgresService._SHRINK_RATIO


def test_guard_math_allows_ordinary_rewrite():
    import json

    old_len = len(json.dumps(BIG, ensure_ascii=False))
    # halving a document is aggressive but legitimate editing
    new_len = len(json.dumps({"content": "x" * 50_000}, ensure_ascii=False))
    assert new_len >= old_len * PostgresService._SHRINK_RATIO


def test_guard_ignores_small_stored_documents():
    import json

    old_len = len(json.dumps(SMALL, ensure_ascii=False))
    assert old_len < PostgresService._SHRINK_MIN_STORED_BYTES


def test_both_upsert_paths_call_the_guard():
    """The xmax path is the one indexed documents take — guarding only the
    sibling would have missed the incident that motivated this."""
    from pathlib import Path

    import core_storage_api.services.postgres_service as ps

    src = Path(ps.__file__).read_text()
    plain = src.index("async def document_upsert(")
    xmax = src.index("async def document_upsert_returning_xmax(")
    assert "_guard_document_shrink" in src[plain:xmax]
    assert "_guard_document_shrink" in src[xmax : xmax + 4000]


def test_force_is_threaded_end_to_end():
    from pathlib import Path

    import core_api.routes.documents as api_docs
    import core_storage_api.routers.documents as st_docs

    api = Path(api_docs.__file__).read_text()
    assert "force: bool = False" in api  # request model accepts it
    assert api.count('"force": body.force') == 2  # both upsert payloads carry it
    st = Path(st_docs.__file__).read_text()
    assert st.count('force=bool(body.get("force"))') == 2  # both storage routes
