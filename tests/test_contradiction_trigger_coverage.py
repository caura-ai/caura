"""Trigger-coverage guard for the contradiction engine seam (A55).

The #1 refactor risk when consolidating scattered triggers is that a site is
missed — or a NEW write path is added later that calls the legacy detector
directly and silently bypasses the engine (and, in Phase 2, the conflict-record
write). These static checks make that a test failure:

1. No production module calls ``detect_contradictions_async`` /
   ``detect_contradictions_by_entities_async`` directly — every trigger goes
   through ``run_contradiction_detection`` — except the detector itself and the
   engine/dispatch that legitimately delegate to it.
2. Every ``Trigger`` kind is actually wired at some call site (no dead trigger).
"""

from __future__ import annotations

import pathlib
import re

import pytest
from core_api.services.contradiction import Trigger

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).parents[1] / "core-api" / "src" / "core_api"

# Only these modules may reference the legacy detector entries by name: the
# detector defines them; the engine + dispatch delegate to them behind the flag.
_ALLOWED = {
    _SRC / "services" / "contradiction_detector.py",
    _SRC / "services" / "contradiction" / "engine.py",
    _SRC / "services" / "contradiction" / "dispatch.py",
}

_LEGACY_CALL = re.compile(
    r"\bdetect_contradictions(_by_entities)?_async\s*\(", re.MULTILINE
)


def _py_files() -> list[pathlib.Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_direct_legacy_detector_calls_outside_engine():
    """Every trigger routes through run_contradiction_detection."""
    offenders: list[str] = []
    for path in _py_files():
        if path in _ALLOWED:
            continue
        text = path.read_text()
        for m in _LEGACY_CALL.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(_SRC)}:{line}")
    assert not offenders, (
        "direct legacy contradiction-detector calls found (must route through "
        f"run_contradiction_detection): {offenders}"
    )


def test_every_trigger_kind_is_wired():
    """Each Trigger enum member is referenced at a production call site, so no
    trigger kind is silently dropped by the consolidation."""
    corpus = "\n".join(p.read_text() for p in _py_files() if p not in _ALLOWED)
    missing = [t.name for t in Trigger if f"Trigger.{t.name}" not in corpus]
    assert not missing, (
        f"Trigger kinds declared but never wired at a call site: {missing}"
    )
