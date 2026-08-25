"""C33 — OpenAPI completeness guarantees.

Three invariants:

1. Every endpoint annotated via ``openapi_responses`` actually surfaces a
   non-empty success schema in the generated spec (the ``responses=``
   attachment is easy to typo into a no-op).
2. The number of success responses WITHOUT a documented schema only goes
   down — a ratchet, so new routes can't ship blank and regressions on
   annotated routes are caught. Lower the ceiling when you document more.
3. The ``servers`` block is driven by ``public_api_url``: absent when the
   setting is empty (OSS default — spec byte-identical to pre-C33), present
   with the trailing-slash-normalized URL when set.
"""

import pytest

pytestmark = pytest.mark.unit

# Success responses (200/201 on get/put/post/patch/delete ops) still lacking
# a real schema. 66 before C33. Deliberately undocumented for now: STM (dead
# feature), plugin/skill delivery (script/text payloads), and admin
# internals. Lower this as any of them get documented.
EMPTY_SUCCESS_CEILING = 23

ANNOTATED = [
    ("get", "/api/v1/memories/stats"),
    ("get", "/api/v1/memories/count"),
    ("post", "/api/v1/memories/bulk-delete"),
    ("get", "/api/v1/memories/{memory_id}/contradictions"),
    ("patch", "/api/v1/memories/{memory_id}/status"),
    ("post", "/api/v1/recall"),
    ("get", "/api/v1/version"),
    ("get", "/api/v1/health"),
    ("post", "/api/v1/documents/search"),
    ("get", "/api/v1/documents"),
    ("get", "/api/v1/documents/{doc_id}"),
    ("get", "/api/v1/documents/collections"),
    ("post", "/api/v1/documents/query"),
    ("post", "/api/v1/skills/installable"),
    ("get", "/api/v1/keystones"),
    ("post", "/api/v1/keystones"),
    ("delete", "/api/v1/keystones/{doc_id}"),
    ("post", "/api/v1/evolve/report"),
    ("get", "/api/v1/entities"),
    ("get", "/api/v1/graph"),
    ("get", "/api/v1/settings"),
    ("put", "/api/v1/settings"),
    ("get", "/api/v1/settings/providers"),
    ("get", "/api/v1/tenants"),
    ("get", "/api/v1/fleets"),
    ("get", "/api/v1/tool-descriptions"),
]


def _fresh_spec():
    from core_api.app import app

    app.openapi_schema = None  # bust the cache; other tests may have filled it
    try:
        return app.openapi()
    finally:
        app.openapi_schema = None


def _success_schema(spec, method, path):
    op = spec["paths"][path][method]
    for code in ("200", "201"):
        if code in op["responses"]:
            content = op["responses"][code].get("content", {})
            return content.get("application/json", {}).get("schema")
    return None


def _is_empty(schema) -> bool:
    if not schema:
        return True
    keys = set(schema.keys())
    return keys <= {"title", "type"} and schema.get("type") in (None, "object")


def test_annotated_endpoints_have_real_schemas():
    spec = _fresh_spec()
    blank = [
        f"{m.upper()} {p}"
        for m, p in ANNOTATED
        if _is_empty(_success_schema(spec, m, p))
    ]
    assert not blank, f"annotated but schema still empty: {blank}"


def test_empty_success_schema_ratchet():
    spec = _fresh_spec()
    empty = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method not in ("get", "put", "post", "delete", "patch"):
                continue
            found = False
            for code in ("200", "201"):
                if code in op.get("responses", {}):
                    found = True
                    if _is_empty(
                        op["responses"][code]
                        .get("content", {})
                        .get("application/json", {})
                        .get("schema")
                    ):
                        empty.append(f"{method.upper()} {path}")
                    break
            del found
    assert len(empty) <= EMPTY_SUCCESS_CEILING, (
        f"{len(empty)} success responses lack a schema "
        f"(ceiling {EMPTY_SUCCESS_CEILING}). New/regressed: document them in "
        f"core_api/openapi_responses.py or raise the ceiling with justification. "
        f"Full list: {empty}"
    )


def test_servers_block_absent_by_default(monkeypatch):
    from core_api.config import settings

    monkeypatch.setattr(settings, "public_api_url", "")
    spec = _fresh_spec()
    assert "servers" not in spec or not spec.get("servers")


def test_servers_block_present_when_configured(monkeypatch):
    from core_api.config import settings

    monkeypatch.setattr(settings, "public_api_url", "https://api.caura.ai/")
    spec = _fresh_spec()
    assert spec.get("servers") == [{"url": "https://api.caura.ai"}]
