"""The CAURA-132 diagnostics must not reach a default-level deployment.

These seven sites fire once per (memory x candidate) pair rather than once
per memory, so at INFO they dominated the module's output — 3,767 of 4,947 prod
lines in 6h on 2026-08-29. Demoting them to DEBUG only helps if a default
deployment actually filters DEBUG, so assert the tier at the call sites AND
assert the filtering end to end.

The lesson this encodes is a real one from the same week: the first
UVICORN_ACCESS_LOG change asserted that a flag was passed rather than that
logging stopped, shipped green, and changed nothing in production for a day.
A verbosity test that only reads the source has the same defect, so the
end-to-end case below drives a real logger through a real configuration.
"""

from __future__ import annotations

import ast
import io
import logging
from pathlib import Path

import pytest

from common.structlog_config import _reset_for_testing, configure_logging

MODULE = (
    Path(__file__).resolve().parents[1]
    / "core-api/src/core_api/services/contradiction_detector.py"
)

# Substrings identifying the CAURA-132 diagnostic messages. Matched against
# the log format string, which is the first argument at each call site.
DIAG_MESSAGES = (
    "PATH_A_SEMANTIC entry",
    "PATH_A_SEMANTIC verdict",
    "PATH_C_DETECTION after_a1_17",
    "PATH_C_DETECTION context_fetched",
    "PATH_C_DETECTION entry",
    "PATH_C_DETECTION judge_selection",
    "PATH_C_DETECTION verdict",
)

# Outcome lines that must STAY at INFO: they are what a prod investigation
# reads. Losing them costs real forensics, unlike per-candidate trace lines.
OUTCOME_MESSAGES = (
    "path_a_completed",
    "path_c_completed",
    "Semantic contradiction:",
    "Entity-based contradiction:",
)


def _log_calls() -> list[tuple[str, str]]:
    """Return (level, format_string) for every logger.<level>(...) call."""
    tree = ast.parse(MODULE.read_text())
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "logger"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.append((fn.attr, first.value))
    return out


@pytest.mark.parametrize("needle", DIAG_MESSAGES)
def test_caura_132_diagnostics_log_at_debug(needle: str) -> None:
    """Each diagnostic is emitted through logger.debug, never logger.info."""
    matches = [(lvl, msg) for lvl, msg in _log_calls() if needle in msg]
    assert matches, f"no log call found for {needle!r} — did the message change?"
    for level, msg in matches:
        assert level == "debug", (
            f"{needle!r} logs at {level!r}; the CAURA-132 diagnostics fire "
            f"per-candidate and must stay at DEBUG. Full message: {msg!r}"
        )


@pytest.mark.parametrize("needle", OUTCOME_MESSAGES)
def test_outcome_lines_stay_at_info(needle: str) -> None:
    """Guard the other direction: a later cleanup must not sweep these down.

    These record what actually happened — conflict counts, timings, the
    contradictions found. They are the reason the module is greppable in an
    incident, and they cost a fraction of the per-candidate lines.
    """
    matches = [(lvl, msg) for lvl, msg in _log_calls() if needle in msg]
    assert matches, f"no log call found for {needle!r} — did the message change?"
    for level, msg in matches:
        assert level == "info", (
            f"{needle!r} logs at {level!r}; outcome lines must stay at INFO. "
            f"Full message: {msg!r}"
        )


def test_debug_is_filtered_at_the_deployed_log_level() -> None:
    """End to end: DEBUG must not reach output under the deployed config.

    Asserting the call-site tier is necessary but not sufficient — it proves
    the source says ``debug``, not that a deployment drops it. core-api's
    settings default log_level to INFO and prod sets no override, so drive a
    real logger through configure_logging() and check what comes out.
    """
    _reset_for_testing()
    configure_logging("production", "INFO", json_logs=True, log_file=None)
    root = logging.getLogger()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    if root.handlers:
        handler.setFormatter(root.handlers[0].formatter)
    root.addHandler(handler)
    try:
        lg = logging.getLogger("core_api.services.contradiction_detector")
        lg.debug("PATH_A_SEMANTIC verdict memory=%s candidate=%s", "m1", "c1")
        lg.info("path_a_completed for memory %s n_conflicts=%d", "m1", 0)
        out = buf.getvalue()
        assert "PATH_A_SEMANTIC" not in out, (
            "a DEBUG record reached output at log_level=INFO — demoting the "
            "diagnostics would not reduce anything in production"
        )
        assert "path_a_completed" in out, (
            "the INFO outcome line did not reach output; the test harness is "
            "wrong, so the DEBUG assertion above proves nothing"
        )
    finally:
        root.removeHandler(handler)
        _reset_for_testing()
