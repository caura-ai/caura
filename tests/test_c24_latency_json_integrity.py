"""C24 — ``_with_latency`` must never corrupt a JSON payload.

The old fall-through appended a plain-text ``_latency_ms`` line after ANY
non-dict result. For ``_serialize``'s list branch that produced
``[...]\\n\\n_latency_ms: N`` — a JSON document with trailing junk that
strict parsers reject. Dict payloads carry the stamp inside the object;
valid-but-non-dict JSON now returns unchanged; only non-JSON prose keeps
the trailing text line (it was never parseable as JSON to begin with).
"""

import json
import time

import pytest

from core_api.mcp_server import _with_latency

pytestmark = pytest.mark.unit


def _t0():
    return time.perf_counter()


def test_dict_payload_gets_inline_stamp_and_stays_valid_json():
    out = _with_latency(json.dumps({"items": [1, 2]}), _t0())
    data = json.loads(out)  # strict parse must succeed
    assert data["items"] == [1, 2]
    assert isinstance(data["_latency_ms"], int)


def test_array_payload_returns_unchanged_valid_json():
    payload = json.dumps([{"id": "a"}, {"id": "b"}], indent=2)
    out = _with_latency(payload, _t0())
    assert out == payload  # byte-identical: no stamp, no trailing junk
    assert json.loads(out) == [{"id": "a"}, {"id": "b"}]


def test_array_payload_has_no_trailing_latency_line():
    out = _with_latency(json.dumps([1, 2, 3]), _t0())
    assert "_latency_ms" not in out


def test_scalar_json_payload_returns_unchanged():
    out = _with_latency("42", _t0())
    assert out == "42"


def test_prose_payload_keeps_latency_suffix():
    out = _with_latency("plain text summary", _t0())
    assert out.startswith("plain text summary")
    assert "_latency_ms:" in out


def test_error_envelope_still_promoted_to_iserror():
    from mcp.types import CallToolResult

    payload = json.dumps({"error": {"code": "FORBIDDEN", "message": "no"}})
    out = _with_latency(payload, _t0())
    assert isinstance(out, CallToolResult)
    assert out.isError is True
    body = json.loads(out.content[0].text)
    assert body["error"]["code"] == "FORBIDDEN"
    assert "_latency_ms" in body
