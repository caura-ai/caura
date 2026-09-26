"""C33 — OpenAPI completeness guarantees.

Four invariants:

1. Every endpoint annotated via ``openapi_responses`` actually surfaces a
   non-empty success schema in the generated spec (the ``responses=``
   attachment is easy to typo into a no-op).
2. The number of success responses WITHOUT a documented schema only goes
   down — a ratchet, so new routes can't ship blank and regressions on
   annotated routes are caught. Lower the ceiling when you document more.
3. The ``servers`` block is driven by ``public_api_url``: absent when the
   setting is empty (OSS default — spec byte-identical to pre-C33), present
   with the trailing-slash-normalized URL when set.
4. Spec-only models cover what the handler actually serializes, where the
   drift already happened once: every key ``/recall`` puts in ``diagnostic``
   must be a declared ``RecallDiagnostic`` field (oss-0902-l-16 — WT-1 added
   ``recall_raw`` to the handler and the model was never widened, so the
   spec and generated clients understated the response).
5. No routed handler declares a parameter it never reads. A declared-and-
   ignored parameter is worse than an absent one: it is documented, it is
   accepted, and it silently does nothing. Three shipped that way at once
   (oss-0814-m-11 ``since``, oss-0902-m-26 ``node_id``, oss-0902-m-27
   ``visibility``), each an unapplied FILTER — so the caller got MORE rows
   than they asked for and no indication the narrowing had been dropped.
"""

import ast
import pathlib

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


# ---------------------------------------------------------------------------
# Invariant 4 — RecallDiagnostic covers the wire (oss-0902-l-16).
#
# ``openapi_responses`` models are spec-only (never ``response_model=``), so
# nothing at runtime drops or flags an undeclared key; its module docstring
# makes handler↔model sync a same-PR rule instead. These tests are that rule's
# teeth for the one payload that already drifted: ``summarize_memories``
# builds ``diagnostic`` at three sites (no-memories, recall-disabled, LLM
# path) which are kept in lockstep by hand — every key each of them emits
# must be a declared ``RecallDiagnostic`` field.
# ---------------------------------------------------------------------------


def test_recall_diagnostic_documents_recall_raw():
    import typing

    from core_api.openapi_responses import RecallDiagnostic

    field = RecallDiagnostic.model_fields.get("recall_raw")
    assert field is not None, (
        "RecallDiagnostic must declare recall_raw — /recall has returned it in "
        "diagnostic since WT-1 (recall_service.summarize_memories)"
    )
    # Nullable: the no-memories and recall-disabled branches emit null.
    assert type(None) in typing.get_args(field.annotation), (
        f"recall_raw must be nullable (str | None), got {field.annotation!r}"
    )


async def test_recall_diagnostic_covers_every_wire_key(monkeypatch):
    """Keys serialized into ``diagnostic`` ⊆ declared ``RecallDiagnostic`` fields,
    on all three builder branches."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import core_api.services.recall_service as rs_mod
    from core_api.openapi_responses import RecallDiagnostic

    def _mem():
        return SimpleNamespace(
            content="The platform database is PostgreSQL 16.",
            memory_type="fact",
            title=None,
            status="active",
            ts_valid_start=None,
            model_dump=lambda mode=None: {"content": "c", "memory_type": "fact"},
        )

    # No-memories branch (no LLM, no config reads).
    empty = await rs_mod.summarize_memories([], "q", SimpleNamespace(), diagnostic=True)
    # Recall-disabled branch.
    disabled = await rs_mod.summarize_memories(
        [_mem()],
        "q",
        SimpleNamespace(recall_enabled=False, recall_provider="p"),
        diagnostic=True,
    )
    # LLM branch — the one that populates recall_raw with the completion.
    monkeypatch.setattr(
        rs_mod, "call_with_fallback", AsyncMock(return_value="**Answer:** 16")
    )
    llm = await rs_mod.summarize_memories(
        [_mem()],
        "q",
        SimpleNamespace(recall_enabled=True, recall_provider="p", recall_model="m"),
        diagnostic=True,
    )

    model_keys = set(RecallDiagnostic.model_fields)
    for branch, resp in {
        "no_memories": empty,
        "recall_disabled": disabled,
        "llm": llm,
    }.items():
        undocumented = set(resp["diagnostic"]) - model_keys
        assert not undocumented, (
            f"/recall ({branch} branch) serializes diagnostic keys missing from "
            f"RecallDiagnostic: {sorted(undocumented)} — declare them in "
            f"core_api/openapi_responses.py (spec-only; see its module docstring)"
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


# --- 5: declared parameters must actually be read -----------------------------

_ROUTES_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "core-api/src/core_api/routes"
)
_ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete"}
# Framework-injected parameters a handler may legitimately declare without
# naming again: FastAPI populates them for their side effects (DI, auth
# enforcement via a dependency, request/response objects reached implicitly).
_INJECTED = {"request", "response", "background_tasks", "auth", "self"}


def _is_route(fn: ast.AST) -> bool:
    for deco in fn.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr in _ROUTE_DECORATORS:
            return True
    return False


def _unused_params(fn) -> list[str]:
    body = ast.Module(body=fn.body, type_ignores=[])
    # Attribute names count as uses: a parameter reached only as ``body.field``
    # still appears as a Name, but this also tolerates the reverse spelling.
    used = {n.id for n in ast.walk(body) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)}
    declared = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    return [
        name
        for name in declared
        if name not in used and name not in _INJECTED and not name.startswith("_")
    ]


def test_no_route_declares_a_parameter_it_never_reads():
    """A declared parameter that is never read is a promise the route breaks.

    Deliberately has no ceiling constant, unlike the ratchet above: this one is
    at zero and every entry is a live bug, so there is nothing to grandfather.
    If a route genuinely needs an unread parameter, name it with a leading
    underscore and the check will skip it.
    """
    offenders = []
    for path in sorted(_ROUTES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
                and _is_route(node)
                and (unused := _unused_params(node))
            ):
                offenders.append(
                    f"{path.name}:{node.lineno} {node.name}() ignores {unused}"
                )

    assert not offenders, (
        "routed handlers declare parameters they never read — each is accepted, "
        "documented, and silently ignored:\n  " + "\n  ".join(offenders)
    )
