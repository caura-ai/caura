"""Unit tests for common.structlog_config — GCP-compatible structlog processors."""

from __future__ import annotations

import json
import logging

import pytest

from common.structlog_config import (
    _THIRD_PARTY_LOGGERS_TO_REROUTE,
    _map_to_gcp_severity,
    _rename_event_to_message,
    _reset_for_testing,
    _route_third_party_to_root,
    _third_party_logger_original_state,
    configure_logging,
)


def test_map_to_gcp_severity_adds_matching_label() -> None:
    for method, expected in [
        ("info", "INFO"),
        ("warning", "WARNING"),
        ("warn", "WARNING"),
        ("error", "ERROR"),
        ("critical", "CRITICAL"),
        ("debug", "DEBUG"),
    ]:
        event_dict = {"event": "hello"}
        result = _map_to_gcp_severity(None, method, event_dict)
        assert result["severity"] == expected


def test_map_to_gcp_severity_unknown_method_falls_back_to_default() -> None:
    result = _map_to_gcp_severity(None, "notice", {"event": "hi"})
    assert result["severity"] == "DEFAULT"


def test_map_to_gcp_severity_preserves_explicit_override() -> None:
    # Callers can pass severity= directly to log a higher level than the
    # method name implies (e.g. logger.info("x", severity="NOTICE")).
    result = _map_to_gcp_severity(None, "info", {"event": "x", "severity": "NOTICE"})
    assert result["severity"] == "NOTICE"


def test_map_to_gcp_severity_treats_none_override_as_absent() -> None:
    # `logger.info("x", severity=None)` must not leak "severity": null to
    # Cloud Logging (which treats it as DEFAULT).
    result = _map_to_gcp_severity(None, "info", {"event": "x", "severity": None})
    assert result["severity"] == "INFO"


def test_map_to_gcp_severity_treats_empty_string_as_absent() -> None:
    # GCP maps severity="" to DEFAULT too — replace with the method-derived
    # label the same way we do for None.
    result = _map_to_gcp_severity(None, "info", {"event": "x", "severity": ""})
    assert result["severity"] == "INFO"


def test_map_to_gcp_severity_preserves_falsy_non_none_non_empty() -> None:
    # Don't silently rewrite 0/False/[] — those are caller-bound values, not
    # an "absent" signal. Contract is: None and "" are absent; anything else
    # is intentional.
    values: list[object] = [0, False, []]
    for value in values:
        result = _map_to_gcp_severity(None, "info", {"event": "x", "severity": value})
        assert result["severity"] == value


def test_rename_event_to_message_moves_field() -> None:
    result = _rename_event_to_message(
        None, "info", {"event": "hello world", "extra": 1}
    )
    assert result == {"message": "hello world", "extra": 1}


def test_rename_event_to_message_preserves_existing_message() -> None:
    # If someone explicitly set `message`, don't overwrite it with `event`.
    # But still remove `event` so Cloud Logging JSON doesn't carry both.
    result = _rename_event_to_message(
        None, "info", {"event": "e", "message": "explicit"}
    )
    assert result == {"message": "explicit"}


def test_rename_event_to_message_noop_without_event() -> None:
    result = _rename_event_to_message(None, "info", {"other": "x"})
    assert result == {"other": "x"}


def test_rename_event_to_message_event_none_produces_empty_message() -> None:
    # logger.info(None) reaches here with `event` explicitly set to None.
    # Emit an empty string so the GCP entry still has a `message` summary.
    result = _rename_event_to_message(None, "info", {"event": None})
    assert result == {"message": ""}


# ─── _route_third_party_to_root ─────────────────────────────────────────


def test_route_third_party_to_root_clears_handlers_and_enables_propagation() -> None:
    """Each rerouted logger ends with handlers=[] and propagate=True so its
    lines flow through the root ProcessorFormatter."""
    # Pre-populate one of the listed loggers with a fake handler + propagate=False
    # to simulate uvicorn / fastmcp's shipped state.
    target = logging.getLogger(_THIRD_PARTY_LOGGERS_TO_REROUTE[0])
    fake_handler = logging.NullHandler()
    target.addHandler(fake_handler)
    target.propagate = False
    try:
        _route_third_party_to_root()
        assert fake_handler not in target.handlers
        assert target.propagate is True
    finally:
        # Restore for any subsequent test that touches this logger.
        target.propagate = True


def test_route_third_party_to_root_is_idempotent() -> None:
    """Calling twice doesn't change state on the second call (already-rerouted
    loggers stay handler-less, propagate stays True)."""
    _route_third_party_to_root()
    snapshot = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
        )
        for name in _THIRD_PARTY_LOGGERS_TO_REROUTE
    }
    _route_third_party_to_root()
    for name in _THIRD_PARTY_LOGGERS_TO_REROUTE:
        lg = logging.getLogger(name)
        assert list(lg.handlers) == snapshot[name][0]
        assert lg.propagate == snapshot[name][1]


def test_route_third_party_to_root_recall_after_handlerless_reroute_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A logger rerouted from ``propagate=False, no-handlers`` (snapshot
    ``([], False, lvl)``) must be treated as already-done on a re-call.
    Pre-fix, the idempotency guard keyed on captured handlers being
    non-empty, so the re-call fell through to the uninitialised early-exit
    branch and emitted a spurious 'rerouting was a no-op' WARNING."""
    target = logging.getLogger(_THIRD_PARTY_LOGGERS_TO_REROUTE[0])
    _third_party_logger_original_state.clear()
    for h in list(target.handlers):
        target.removeHandler(h)
    target.propagate = False
    target.setLevel(logging.WARNING)
    try:
        _route_third_party_to_root()  # reroutes: propagate False→True
        assert target.propagate is True
        with caplog.at_level(logging.WARNING, logger="common.structlog_config"):
            _route_third_party_to_root()  # re-call must be a silent no-op
        # Other listed loggers may legitimately warn (uninitialised in the
        # test env) — only the already-rerouted target must stay silent.
        assert not [
            r
            for r in caplog.records
            if "rerouting was a no-op" in r.getMessage()
            and repr(target.name) in r.getMessage()
        ], "re-call after a handler-less reroute must not warn for that logger"
    finally:
        target.setLevel(logging.NOTSET)
        target.propagate = True
        _third_party_logger_original_state.clear()


def test_route_third_party_to_root_pristine_logger_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pristine listed logger (no own handlers, propagate=True) already
    propagates to the root handler, so the 'nothing to reroute' note is DEBUG,
    not a misleading WARNING. The old WARNING falsely implied records wouldn't
    reach GCP, when they do via propagation (prod: ``mcp.server.*`` lands as
    structured JSON)."""
    target = logging.getLogger(_THIRD_PARTY_LOGGERS_TO_REROUTE[0])
    _third_party_logger_original_state.clear()
    for h in list(target.handlers):
        target.removeHandler(h)
    target.propagate = True  # pristine: handler-less + propagating
    try:
        with caplog.at_level(logging.WARNING, logger="common.structlog_config"):
            _route_third_party_to_root()
        assert not [
            r
            for r in caplog.records
            if "no-op" in r.getMessage() or "nothing to reroute" in r.getMessage()
        ], "a pristine handler-less logger must not WARN — it already reaches root"
    finally:
        target.propagate = True
        _third_party_logger_original_state.clear()


def test_route_third_party_to_root_preserves_operator_set_level_when_no_handlers() -> (
    None
):
    """If a logger has no own handlers, its level was set by the operator
    relative to root (e.g. ``uvicorn --log-level warning``). Don't silently
    reset to NOTSET — that would flood Cloud Logging with previously
    suppressed access logs."""
    target = logging.getLogger(_THIRD_PARTY_LOGGERS_TO_REROUTE[0])
    # Clear leaked snapshot state from earlier tests so this test sees a
    # genuinely-empty handler list at snapshot time. Without this, a prior
    # test's snapshot of ``[NullHandler]`` would make this run go through
    # the rerouting path that resets the level — masking the regression
    # this test is supposed to catch.
    _third_party_logger_original_state.clear()
    for h in list(target.handlers):
        target.removeHandler(h)
    target.propagate = True
    target.setLevel(logging.WARNING)
    try:
        _route_third_party_to_root()
        assert target.level == logging.WARNING, (
            "operator-set level should not be silently reset to NOTSET when "
            "the library hadn't installed its own handlers"
        )
    finally:
        target.setLevel(logging.NOTSET)
        _third_party_logger_original_state.clear()


# ─── stdlib ``extra={...}`` propagation (ExtraAdder bridge) ─────────────


@pytest.fixture
def _json_log_buffer():
    """Reset structlog config, re-configure with JSON output, then swap
    every root StreamHandler's stream to an in-memory buffer so the
    test can inspect emitted JSON. ``capsys``/``capfd`` don't see the
    handler's output because pytest's stdout swap happens before this
    file's conftest configure_logging runs, leaving the handler bound
    to the original FD; the cleanest fix is to capture at the
    handler-stream layer, not the FD layer."""
    import io

    _reset_for_testing()
    configure_logging(environment="test", log_level="DEBUG", json_logs=True)
    buf = io.StringIO()
    swapped: list[tuple[logging.Handler, object]] = []
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler):
            swapped.append((h, h.stream))
            h.stream = buf
    try:
        yield buf
    finally:
        for h, original in swapped:
            h.stream = original  # type: ignore[assignment]
        _reset_for_testing()


_DDTRACE_WRITER_LOGGERS = ("ddtrace.internal.writer.writer", "ddtrace.llmobs._writer")


def test_ddtrace_writer_loggers_floored_at_critical_when_not_debug() -> None:
    """The ddtrace trace + LLMObs writer loggers emit benign flush-failure
    ERRORs ("failed to send, dropping N traces …"). configure_logging must
    floor them at CRITICAL at both INFO and WARNING so that ERROR noise is
    dropped from every sink (Datadog / GCP / the caura-ops error alerter) —
    the ERROR clears root's WARNING filter otherwise and drives alert flap."""
    for level in ("INFO", "WARNING"):
        _reset_for_testing()
        try:
            configure_logging(environment="test", log_level=level, json_logs=True)
            for name in _DDTRACE_WRITER_LOGGERS:
                assert logging.getLogger(name).level == logging.CRITICAL, (
                    f"{name} must be floored at CRITICAL when configured at {level}"
                )
        finally:
            _reset_for_testing()


def test_ddtrace_writer_loggers_not_floored_at_debug() -> None:
    """At DEBUG (explicit opt-in to verbose output) the writer loggers must NOT
    be floored, so ddtrace's own diagnostics stay visible."""
    _reset_for_testing()
    try:
        configure_logging(environment="test", log_level="DEBUG", json_logs=True)
        for name in _DDTRACE_WRITER_LOGGERS:
            assert logging.getLogger(name).level != logging.CRITICAL, (
                f"{name} must not be floored at CRITICAL when configured at DEBUG"
            )
    finally:
        _reset_for_testing()


def test_reset_clears_ddtrace_writer_floor() -> None:
    """_reset_for_testing must clear the CRITICAL floor back to NOTSET so a
    later DEBUG reconfigure isn't left with these stuck at CRITICAL."""
    _reset_for_testing()
    configure_logging(environment="test", log_level="INFO", json_logs=True)
    _reset_for_testing()
    for name in _DDTRACE_WRITER_LOGGERS:
        assert logging.getLogger(name).level == logging.NOTSET, (
            f"{name} must be reset to NOTSET after _reset_for_testing"
        )


def test_stdlib_logger_extras_reach_json_payload(_json_log_buffer) -> None:
    """``logger.info(msg, extra={...})`` from stdlib must surface its
    ``extra`` keys as top-level JSON fields, not be silently dropped.

    Pre-fix, ``_base_processors`` lacked ``structlog.stdlib.ExtraAdder()``,
    so the JSON payload contained only ``message`` and ``timestamp`` —
    every existing ``extra``-based call site (memory-search, memory-get,
    per-tenant concurrency saturation, CAURA-682's memory_write_latency)
    silently lost its structured data at the GCP boundary. This test
    pins the contract."""
    logger = logging.getLogger("test_stdlib_extras_propagation")
    logger.info(
        "request done",
        extra={
            "path": "test-path",
            "tenant_id": "test-tenant",
            "duration_ms": 42,
            "ok": True,
        },
    )
    lines = _json_log_buffer.getvalue().strip().splitlines()
    records = [json.loads(line) for line in lines if '"request done"' in line]
    assert len(records) == 1, f"expected one matching record, got: {lines}"
    record = records[0]
    assert record["message"] == "request done"
    assert record["path"] == "test-path"
    assert record["tenant_id"] == "test-tenant"
    assert record["duration_ms"] == 42
    assert record["ok"] is True


def test_stdlib_logger_without_extras_still_emits(_json_log_buffer) -> None:
    """No ``extra`` → no ExtraAdder fields → just the standard payload
    (message + timestamp + severity). Regression guard for the
    no-extras path."""
    logger = logging.getLogger("test_stdlib_no_extras")
    logger.info("plain message")
    lines = _json_log_buffer.getvalue().strip().splitlines()
    records = [json.loads(line) for line in lines if '"plain message"' in line]
    assert len(records) == 1
    record = records[0]
    assert record["message"] == "plain message"
    assert record["severity"] == "INFO"


def test_extra_event_key_does_not_replace_message(_json_log_buffer) -> None:
    """``ExtraAdder(deny=["event", ...])`` must block silent message
    corruption when a caller passes ``extra={"event": "x"}``.

    Without the deny entry: ExtraAdder overwrites ``event_dict["event"]``
    (which ProcessorFormatter set to the real log message), then
    ``_rename_event_to_message`` renames the extra's value as the GCP
    ``message`` field — losing the original text silently."""
    logger = logging.getLogger("test_extra_event_collision")
    logger.info("real message", extra={"event": "extra-value", "ok": True})
    lines = _json_log_buffer.getvalue().strip().splitlines()
    records = [json.loads(line) for line in lines if '"real message"' in line]
    assert len(records) == 1, f"expected one matching record, got: {lines}"
    record = records[0]
    # Real message wins; ``event`` extra is dropped by the deny list.
    assert record["message"] == "real message"
    assert record.get("event") is None
    # Other extras still propagate normally.
    assert record["ok"] is True


def test_extra_message_key_is_rejected_by_stdlib_then_blocked_in_depth(
    _json_log_buffer,
) -> None:
    """``logger.info(msg, extra={"message": "x"})`` is rejected by
    Python's stdlib ``logging`` itself with
    ``KeyError("Attempt to overwrite 'message' in LogRecord")`` — so the
    ``extra={"message": ...}`` path *can't* reach our processor chain.
    Documented here so a future maintainer doesn't assume the deny
    entry is unneeded.

    The deny entry is still load-bearing as defense-in-depth for the
    ``logging.Filter`` path — a filter that stamps ``record.message =
    "x"`` directly bypasses stdlib's ``extra=`` validation. The second
    half of this test exercises that path explicitly so the deny entry
    has a regression guard."""
    logger = logging.getLogger("test_extra_message_stdlib_guard")
    with pytest.raises(KeyError, match="Attempt to overwrite 'message'"):
        logger.info("real message", extra={"message": "extra-value"})

    # Defense-in-depth path: a logging.Filter stamps `record.message`
    # directly, bypassing stdlib's extra= guard. The deny entry on
    # `message` in `_add_logrecord_extras` must still prevent the GCP
    # `message` field from being corrupted by the filter's stamp.
    class _StampMessageFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            # Stamp directly on the record — stdlib doesn't validate
            # this path the way it validates ``extra={}``.
            record.message = "filter-stamped-value"  # type: ignore[attr-defined]
            return True

    filter_logger = logging.getLogger("test_extra_message_filter_path")
    filter_obj = _StampMessageFilter()
    filter_logger.addFilter(filter_obj)
    try:
        filter_logger.info("real-filter-message", extra={"ok": True})
    finally:
        filter_logger.removeFilter(filter_obj)

    lines = _json_log_buffer.getvalue().strip().splitlines()
    records = [json.loads(line) for line in lines if '"real-filter-message"' in line]
    assert len(records) == 1, f"expected one matching record, got: {lines}"
    record = records[0]
    # Real message wins; the filter-stamped `message` is dropped by the
    # deny entry in `_add_logrecord_extras`.
    assert record["message"] == "real-filter-message"
    # Other extras still propagate normally.
    assert record["ok"] is True


@pytest.mark.parametrize(
    "extra_key,extra_value,expected_value",
    [
        # ``severity`` — would override _map_to_gcp_severity's
        # log-level-derived value because the guard only fills on
        # None/""; a non-empty extra value would survive and produce
        # an invalid GCP severity or misroute alerts.
        ("severity", "P1", "INFO"),
        # ``stack`` — StackInfoRenderer only sets this when
        # stack_info=True; an extra would propagate untouched.
        ("stack", "fake-traceback", None),
        # ``exception`` — format_exc_info only sets this when
        # exc_info is present; an extra would persist on a log line
        # with no real exception.
        ("exception", "fabricated-exception", None),
    ],
)
def test_extra_pipeline_reserved_keys_are_dropped(
    _json_log_buffer, extra_key: str, extra_value: str, expected_value
) -> None:
    """Reserved-output-key extras (``severity``, ``stack``, ``exception``)
    must be dropped by ``_add_logrecord_extras`` so a caller can't
    silently inject pipeline-managed fields via ``extra={...}``. Each
    one corresponds to a real corruption vector documented on
    ``_RESERVED_OUTPUT_KEYS``."""
    logger = logging.getLogger(f"test_reserved_{extra_key}")
    logger.info("real-message", extra={extra_key: extra_value, "ok": True})
    lines = _json_log_buffer.getvalue().strip().splitlines()
    records = [json.loads(line) for line in lines if '"real-message"' in line]
    assert len(records) == 1, f"expected one matching record, got: {lines}"
    record = records[0]
    # The reserved key is either absent or holds its pipeline-derived
    # value, NOT the user-supplied extra.
    if expected_value is None:
        assert extra_key not in record, (
            f"{extra_key} should be dropped, but record has "
            f"{extra_key}={record.get(extra_key)!r}"
        )
    else:
        assert record[extra_key] == expected_value, (
            f"{extra_key} should equal pipeline value {expected_value!r}, "
            f"but record has {extra_key}={record.get(extra_key)!r}"
        )
    # Other extras still propagate normally — the deny list doesn't
    # break the happy path.
    assert record["ok"] is True
    assert record["message"] == "real-message"
