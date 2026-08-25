"""C30 — envelope convergence per the ratified wire contract (D1).

`items` is the canonical list key everywhere. /documents/search dual-emits
`results` + `items` (same list, two keys — `results` stays until a separate
announced deprecation wave). GET /keystones keeps the bare array as its
default shape and gains an `envelope=1` opt-in returning {count, items}.

Source-level guards (the route handlers need auth/storage to run end-to-end;
the shapes themselves are wet-tested against the live stack).
"""

import inspect
import pathlib

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[1] / "core-api/src/core_api/routes"


def test_documents_search_dual_emits_items():
    src = (ROOT / "documents.py").read_text()
    # Anchor on the response construction (unique), not the collection key
    # (which also appears in write payloads earlier in the file).
    idx = src.index('"count": len(items)')
    window = src[idx - 200 : idx + 200]
    assert '"results": items' in window
    assert '"items": items' in window


def test_keystones_envelope_param_exists_and_defaults_off():
    from core_api.routes import keystones

    sig = inspect.signature(keystones.list_keystones)
    assert "envelope" in sig.parameters
    # FastAPI Query default: the bare array MUST stay the default shape.
    default = sig.parameters["envelope"].default
    assert getattr(default, "default", default) is False


def test_keystones_envelope_shape():
    src = (ROOT / "keystones.py").read_text()
    assert '{"count": len(rows), "items": rows}' in src
    # bare-array default preserved
    assert "return rows" in src
