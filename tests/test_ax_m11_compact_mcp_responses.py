"""ax-0917-m-11 — MCP responses were pretty-printed for an LLM consumer.

Every MCP response is read by a model, not by a person, so `json.dumps`'s two
defaults are both pure cost here.

`indent=2` spends tokens on whitespace that carries no meaning to the reader.

`ensure_ascii=True` is the larger one, and it only bites non-English tenants: it
renders every non-ASCII character as `\\uXXXX` — six ASCII characters where one
character was meant — and those escapes tokenise badly.

Measured in cl100k tokens on realistic payloads:

    10-item English recall payload   572 -> 462   (19% fewer)
    5-item Hebrew payload          1,207 -> 422   (65% fewer)

`doc_indexing._render_value` already reached the same conclusion for text fed to
an embedder and an LLM; this applies it to the transport.
"""

import ast
import inspect
import json

import pytest

pytestmark = pytest.mark.unit


def _module_code(mod) -> str:
    """Module source with comments and docstrings stripped.

    The comments explaining this fix quote `indent=2` and `ensure_ascii`
    repeatedly, so a raw-source search would find the prose.
    """
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    return ast.unparse(tree)


# ── the serializer ───────────────────────────────────────────────────────


def test_mcp_responses_are_not_pretty_printed():
    from core_api import mcp_server

    assert "indent=2" not in _module_code(mcp_server)


def test_the_recall_block_is_not_pretty_printed():
    from core_api.services import recall_service

    assert "indent=2" not in _module_code(recall_service)


def test_non_ascii_is_not_escaped():
    """The bigger saving, and the one that only affects non-English tenants."""
    from core_api.mcp_server import _dumps

    out = _dumps({"content": "פגישת צוות"})
    assert "\\u" not in out
    assert "פגישת צוות" in out


def test_the_output_is_compact():
    from core_api.mcp_server import _dumps

    out = _dumps({"a": 1, "b": [2, 3]})
    assert out == '{"a":1,"b":[2,3]}'


def test_it_still_produces_valid_json():
    """Compaction must not cost correctness — this is a wire format."""
    from core_api.mcp_server import _dumps

    payload = {"items": [{"t": "fact", "c": "x"}], "n": 1, "ok": True, "none": None}
    assert json.loads(_dumps(payload)) == payload


def test_non_serialisable_values_still_degrade_rather_than_raise():
    """``default=str`` was carried over deliberately: an MCP response must not
    fail to serialise because a datetime or UUID reached it."""
    from datetime import UTC, datetime

    from core_api.mcp_server import _dumps

    out = _dumps({"when": datetime(2026, 9, 20, tzinfo=UTC)})
    assert "2026-09-20" in out


# ── one helper, so the call sites cannot drift ───────────────────────────


def test_every_mcp_call_site_goes_through_the_helper():
    """The reason the compaction was inconsistent before is that each site
    made the choice itself. A site calling ``json.dumps`` directly would
    silently keep the old defaults."""
    from core_api import mcp_server

    code = _module_code(mcp_server)
    # The helper itself is the one legitimate ``json.dumps`` in the module.
    assert code.count("json.dumps(") == 1


def test_the_helper_keeps_all_three_settings_together():
    from core_api.mcp_server import _JSON_COMPACT

    assert _JSON_COMPACT["separators"] == (",", ":")
    assert _JSON_COMPACT["ensure_ascii"] is False
    assert _JSON_COMPACT["default"] is str


# ── the saving has to survive the transport ──────────────────────────────


def test_the_jsonrpc_envelope_does_not_re_escape_the_body():
    """Load-bearing assumption behind ``ensure_ascii=False``.

    Our string is carried as `TextContent.text` inside a JSON-RPC envelope that
    the MCP SDK serialises itself. If that outer pass escaped non-ASCII, every
    character we stopped escaping would simply be escaped one layer up and the
    saving would be zero — invisibly, because our own output would still look
    right.

    It doesn't: pydantic's `model_dump_json` emits UTF-8. This test fails if a
    future SDK release changes that, which is the only way we would find out.
    """
    from mcp.types import CallToolResult, TextContent

    from core_api.mcp_server import _dumps

    wire = CallToolResult(
        content=[TextContent(type="text", text=_dumps({"c": "פגישת צוות"}))]
    ).model_dump_json()

    assert "\\u05" not in wire
    assert "פגישת צוות" in wire
