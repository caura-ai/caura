"""structlog configuration that emits GCP-compatible log entries.

Cloud Logging promotes the top-level `severity` field to the log entry's
severity level and the top-level `message` field to its summary line. Without
this, log lines written through `structlog` end up as severity `DEFAULT`
(unlabeled) in Cloud Logging, which breaks severity filtering and distorts
severity-based histograms.

This module wires those two fields in via processors, and is safe to call
from multiple service entry points because it's idempotent. Callers pass
their own `environment` and `log_level` so this module stays agnostic of
per-service settings schemas.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

import structlog

# structlog's log-level method names → GCP Cloud Logging's severity enum.
# Severities beyond ERROR (CRITICAL/ALERT/EMERGENCY) aren't emitted by the
# standard log-level methods; use `logger.critical(...)` for CRITICAL, or
# attach `severity=` explicitly when needed.
_LEVEL_TO_SEVERITY = {
    "critical": "CRITICAL",
    "error": "ERROR",
    "warning": "WARNING",
    "warn": "WARNING",  # stdlib alias — defensive; unused under structlog proper
    "info": "INFO",
    "debug": "DEBUG",
}

# Allowlist for log_level validation. `isinstance(min_level, int)` would
# admit `logging.NOTSET` (== 0), which silently disables both the level
# filter and the dep-silencing block — a footgun we'd rather reject.
_VALID_LEVELS = frozenset(
    {logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL}
)

_configured = False
_configured_environment: str | None = None
_configured_log_level: str | None = None
_configured_json_logs: bool | None = None
_configured_log_file: str | None = None
# Track the root handlers this module installs so reset + reconfigure
# removes only OUR handlers, not ones installed by logging.basicConfig()
# or a library that ran before us.
_installed_handler: logging.Handler | None = None
_installed_file_handler: logging.Handler | None = None
# RLock so a custom `warnings.showwarning` hook that re-enters
# `configure_logging` on the same thread won't deadlock. The mismatch
# warning below is the only user-visible call site that could plausibly
# trigger re-entry.
_configure_lock = threading.RLock()

# Sentinel so `_rename_event_to_message` can tell an absent `event` key apart
# from one that's explicitly set to `None` — `pop(..., None)` can't.
_MISSING: Any = object()


class LoggingConfigurationWarning(UserWarning):
    """Emitted when `configure_logging` is re-called with different arguments.

    Distinct from the bare `UserWarning` so tests (or operators) can filter
    this specific condition without affecting unrelated user warnings.
    """


# Canonical attrs on a vanilla `logging.LogRecord`. Mirrors
# `structlog.stdlib._LOG_RECORD_KEYS` (the set that ExtraAdder excludes)
# — by constructing a dummy record we stay aligned with whatever the
# stdlib reports for the current Python version, rather than hard-coding
# the names (which change between releases — e.g. ``taskName`` was added
# in 3.12). Frozen at import time so a downstream library's runtime
# monkey-patching of the LogRecord class can't widen the deny set under
# our feet.
_LOG_RECORD_STANDARD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("name", 0, "pathname", 0, "msg", (), None).__dict__.keys()
)

# Keys the structlog/GCP pipeline reserves for itself. A user-supplied
# ``extra={...}`` must not be allowed to populate any of these — each
# would open a silent-corruption channel into a different GCP field:
#
#   * ``event`` — would overwrite ProcessorFormatter's
#     ``event_dict["event"]`` (the real LogRecord message); then
#     ``_rename_event_to_message`` would rename the extra's value as
#     the GCP ``message`` field, silently losing the original text.
#     (Stdlib ``logger.*()`` accepts ``extra={"event": ...}`` because
#     ``event`` isn't a standard LogRecord attribute — structlog's
#     convention only.)
#
#   * ``message`` — stdlib's own LogRecord-attr-collision guard
#     ``raise KeyError("Attempt to overwrite 'message'")`` already
#     blocks the ``extra={"message": ...}`` route. The deny entry
#     here is defense-in-depth for the ``logging.Filter`` path: a
#     filter that stamps ``record.message = "x"`` directly bypasses
#     stdlib's ``extra=`` validation.
#
#   * ``severity`` — ``_add_logrecord_extras`` runs BEFORE
#     ``_map_to_gcp_severity``, whose guard only fills the field when
#     it is ``None`` or ``""``. A stdlib ``extra={"severity": "P1"}``
#     (an application-domain label) would therefore propagate
#     untouched into the GCP severity field, overriding the correct
#     log-level-derived value, producing an invalid GCP severity, and
#     potentially misrouting log-based alerts.
#
#   * ``stack`` — set by ``StackInfoRenderer`` ONLY when the caller
#     passes ``stack_info=True``. Without that flag, the renderer is
#     a no-op and an ``extra={"stack": "noise"}`` would propagate
#     untouched into the JSON payload alongside any future
#     stack-trace-based tooling.
#
#   * ``exception`` — set by ``format_exc_info`` ONLY when ``exc_info``
#     is present on the call. Without exception context an
#     ``extra={"exception": "fake"}`` persists in the JSON payload,
#     fabricating exception data on a non-exception log line.
_RESERVED_OUTPUT_KEYS: frozenset[str] = frozenset(
    {"event", "message", "severity", "stack", "exception"}
)


def _add_logrecord_extras(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """structlog processor: copy a ``LogRecord``'s non-standard attrs
    into the event dict, but block reserved-output-key collisions.

    Equivalent to ``structlog.stdlib.ExtraAdder()`` for typical extras
    (``path``, ``tenant_id``, ``total_ms``, etc.) but additionally
    refuses to propagate ``event`` / ``message`` — the two keys that
    silently corrupt the GCP ``message`` field if a caller passes them
    via ``extra={...}``. ``ExtraAdder`` exposes only ``allow`` (since
    structlog 21.5), not ``deny``, so we can't use it directly without
    resorting to a brittle static allowlist of every key the codebase
    might ever log.

    Also covers attributes stamped onto records by a ``logging.Filter``
    — no such filter exists in-tree today; flagged so a future addition
    is a known channel, not a surprise.
    """
    record = event_dict.get("_record")
    if record is None:
        return event_dict
    for key, value in record.__dict__.items():
        if key in _LOG_RECORD_STANDARD_ATTRS:
            continue
        if key.startswith("_"):
            continue
        if key in _RESERVED_OUTPUT_KEYS:
            continue
        # ``setdefault`` so a key a prior processor already set wins
        # over a user extra of the same name — defensive, though in
        # practice no prior processor in our chain sets keys an
        # ``extra={}`` would collide with.
        event_dict.setdefault(key, value)
    return event_dict


def _map_to_gcp_severity(
    _logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    # Respect an explicit `severity` override if the caller set one. Treat
    # `severity=None` and `severity=""` as absent: GCP maps both to DEFAULT,
    # which would silently mask the method's level. Use an explicit check
    # rather than `not ...` so callers binding a falsy-but-intentional
    # value (0, False, []) don't see it silently rewritten.
    severity = event_dict.get("severity")
    if severity is None or severity == "":
        event_dict["severity"] = _LEVEL_TO_SEVERITY.get(method_name, "DEFAULT")
    return event_dict


def _rename_event_to_message(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    # Pop `event` unconditionally: GCP only reads `message`, and leaving a
    # redundant `event` on every entry adds noise. A caller-supplied
    # `message=` still wins; we just discard the structlog-convention `event`.
    # Distinguish absent (leave event_dict alone) from explicit `None`
    # (emit empty string so the entry still has a summary line in GCP).
    event = event_dict.pop("event", _MISSING)
    if event is _MISSING:
        return event_dict
    if "message" not in event_dict:
        event_dict["message"] = event if event is not None else ""
    return event_dict


def _drop_level_field(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    # Cloud Logging reads severity from the top-level `severity` field; a
    # lowercase `level` sibling (from add_log_level) is pure noise in the
    # JSON payload. Dev's ConsoleRenderer still uses `level` for coloured
    # output, so this processor only runs in the JSON branches.
    event_dict.pop("level", None)
    return event_dict


# Processors shared by both the structlog-native chain and the stdlib bridge —
# extracted so adding a new cross-cutting processor (e.g. request-id injection)
# only needs a single edit.
def _base_processors() -> list[Any]:
    return [
        # merge_contextvars first so a context-bound `level` can't clobber
        # the one add_log_level derives from the real call site.
        structlog.contextvars.merge_contextvars,
        # add_log_level populates `level`, needed by stdlib records (which
        # don't have `method_name`) and kept on the native side for shape
        # consistency across the two pipelines.
        structlog.stdlib.add_log_level,
        # ``_add_logrecord_extras`` (defined above) is the deny-list
        # equivalent of ``structlog.stdlib.ExtraAdder()``. It propagates
        # ``extra={...}`` keys from stdlib ``logger.*()`` calls into the
        # JSON payload but refuses to forward ``event`` / ``message``,
        # which would otherwise silently corrupt the GCP ``message``
        # field. Without it, every existing ``extra``-based call site
        # (memory-search / memory-get summary logs, the per-tenant
        # concurrency saturation log, CAURA-682's memory_write_latency
        # phase timings) emits to GCP with only ``message`` and
        # ``timestamp`` populated — the structured fields are silently
        # dropped. This is the stdlib-bridge counterpart to passing
        # kwargs directly on the structlog-native side.
        _add_logrecord_extras,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # StackInfoRenderer is a no-op unless a caller passes stack_info=True;
        # when they do, it puts the formatted stack under `stack` so both
        # JSONRenderer and ConsoleRenderer render it properly. `format_exc_info`
        # is intentionally NOT here: ConsoleRenderer expects raw `exc_info`
        # and pretty-prints it, while `format_exc_info` pops the key and
        # stringifies it under `exception`, producing an ugly inline field
        # in dev. It's appended per-renderer below, JSON-only.
        structlog.processors.StackInfoRenderer(),
    ]


def _json_processors() -> list[Any]:
    # Production-only chain: runs after _base_processors() in both the native
    # structlog pipeline and the stdlib-bridge foreign_pre_chain. Centralised
    # so adding a new prod processor (e.g. trace-ID injection) is one edit.
    return [
        structlog.processors.format_exc_info,
        _drop_level_field,
        _map_to_gcp_severity,
        _rename_event_to_message,
    ]


def configure_logging(
    environment: str,
    log_level: str = "INFO",
    *,
    json_logs: bool = True,
    log_file: str | None = None,
) -> None:
    """Configure structlog to emit GCP-compatible entries. Idempotent.

    `environment` is retained for future env-specific hooks but the
    renderer is chosen by `json_logs` so operators can force JSON in dev
    (or ConsoleRenderer in prod) without changing environment. Callers
    pass their own settings so this helper stays agnostic of per-service
    settings schemas.

    `log_file` (optional) adds a second sink: a TimedRotatingFileHandler
    writing daily-rotated JSON logs to disk, retained for 5 days. Used
    by on-prem deployments so operators have greppable log files on the
    host without `docker logs` contortions and the support-bundle
    tooling has a stable place to read from. When empty, only stdout is
    used (SaaS behaviour, unchanged).
    """
    global _configured, _configured_environment, _configured_log_level
    global _configured_json_logs, _configured_log_file
    # Single lock acquisition — `configure_logging` runs at most a few
    # times per process (once per service entry point), so skipping the
    # fast-path-outside-lock keeps the mismatch comparison race-free
    # without measurable cost.
    with _configure_lock:
        if _configured:
            # Silent no-op for the normal case. Warn when a second caller
            # passes different settings so the mismatch surfaces instead
            # of producing wrong output.
            normalized_level = log_level.upper()
            if (
                environment != _configured_environment
                or normalized_level != _configured_log_level
                or json_logs != _configured_json_logs
                or log_file != _configured_log_file
            ):
                import warnings

                warnings.warn(
                    f"configure_logging re-called with different arguments: "
                    f"({environment!r}, {log_level!r}, json_logs={json_logs!r}, "
                    f"log_file={log_file!r}) differs from original "
                    f"({_configured_environment!r}, {_configured_log_level!r}, "
                    f"json_logs={_configured_json_logs!r}, "
                    f"log_file={_configured_log_file!r}); original retained.",
                    LoggingConfigurationWarning,
                    stacklevel=2,
                )
            return
        _configure_logging_impl(environment, log_level, json_logs, log_file)
        _configured_environment = environment
        _configured_log_level = log_level.upper()
        _configured_json_logs = json_logs
        _configured_log_file = log_file
        _configured = True


# Names of third-party loggers that install their own (typically stderr,
# plain-text) handlers OR set ``propagate=False``, bypassing the root
# ``ProcessorFormatter`` without explicit re-routing. CAURA-588.
#
# Ordering caveat: ``_route_third_party_to_root`` only re-routes loggers
# whose libraries have already initialised them at call time. uvicorn's
# ``Config.configure_logging()`` runs at server start; if our
# ``configure_logging()`` is called at module import (the typical app.py
# path), the uvicorn loggers may not yet have their handlers when we
# iterate. The function logs a WARNING in that case so the caller can
# move the call into an ASGI lifespan startup handler if production logs
# show un-rerouted output.
_THIRD_PARTY_LOGGERS_TO_REROUTE: tuple[str, ...] = (
    # uvicorn ships three loggers with propagate=False + own StreamHandlers
    # via uvicorn.config.LOGGING_CONFIG.
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    # FastMCP / underlying mcp library register their own stream handler.
    "fastmcp",
    "mcp",
    # slowapi (rate-limit middleware) — included defensively. Some versions
    # install their own handler when rate-limit decisions are configured to
    # log; on versions that don't, the rerouting is a harmless no-op (no
    # snapshotted handlers, level untouched).
    "slowapi",
)

# Pre-config state for each rerouted logger (handlers, propagate, level).
# Captured by ``_route_third_party_to_root`` on first invocation and
# replayed by ``_reset_for_testing`` so test fixtures can return to a
# state structurally identical to import-time. Handlers are *not* closed
# in the route-to-root path so the snapshot retains usable handler objects
# for restore — Python's GC handles FD release when the logger no longer
# references them after the next reset.
_third_party_logger_original_state: dict[
    str, tuple[list[logging.Handler], bool, int]
] = {}


def _route_third_party_to_root() -> None:
    """Strip third-party loggers' own handlers and force propagation to root.

    Without this, uvicorn access/error and FastMCP server lines bypass the
    root ``ProcessorFormatter`` and emit as unstructured plain text at
    DEFAULT severity in Cloud Logging — breaking severity filtering and
    audit log searches. After this runs, those lines flow through the
    same JSON formatter as application logs.

    Only resets the level when the library actually had its own handlers
    — otherwise the level was an operator-/library-configured filter
    relative to root (e.g. ``uvicorn --log-level warning``) and we mustn't
    silently widen it.
    """
    for name in _THIRD_PARTY_LOGGERS_TO_REROUTE:
        lg = logging.getLogger(name)
        # Skip iff the snapshot has this logger: the snapshot is stored
        # only when rerouting actually ran (the uninitialised early-exit
        # below continues before it), so presence in the dict is the
        # complete "work was done" signal. Keying on captured handlers
        # instead would mis-handle a logger rerouted from
        # ``propagate=False, no-handlers`` (snapshot ``([], False, lvl)``)
        # — a re-call (e.g. from an ASGI lifespan startup after uvicorn
        # initialised) would fall through to the early-exit branch and
        # emit a spurious WARNING about rerouting being a no-op.
        if name in _third_party_logger_original_state:
            continue
        # Library hasn't installed its own handlers and still propagates — there
        # is nothing to reroute, and crucially nothing is LOST: a handler-less,
        # propagate=True logger already flows to the root handler (verified in
        # prod — ``mcp.server.*`` / ``uvicorn.error`` records reach GCP as
        # structured JSON this way). So this is the benign steady state, not a
        # routing failure. Log at DEBUG, not WARNING — the old WARNING was a
        # misleading false alarm (~per instance start) implying records wouldn't
        # reach GCP. Don't snapshot the empty state (would freeze us into "no
        # work to do" forever) so a later reroute can still act if the library
        # ever installs handlers / sets propagate=False.
        if not lg.handlers and lg.propagate:
            logging.getLogger(__name__).debug(
                "logger %r has no own handlers at rerouting time; nothing to "
                "reroute — it already propagates to the root handler",
                name,
            )
            continue
        # First effective call. Snapshot the pre-reroute state so
        # ``_reset_for_testing`` can restore. Handlers retained for the
        # process lifetime — bounded for the listed libraries (uvicorn /
        # fastmcp / mcp / slowapi only install StreamHandlers pointing at
        # stderr, ~kilobytes total) so no FD-leak risk.
        if name not in _third_party_logger_original_state:
            _third_party_logger_original_state[name] = (
                list(lg.handlers),
                lg.propagate,
                lg.level,
            )
        had_own_handlers = bool(_third_party_logger_original_state[name][0])
        # Remove the library's own handlers without closing them. Closing
        # would invalidate the snapshot's handler objects — ``_reset_for_testing``
        # restores from that snapshot, and a closed FileHandler/SocketHandler
        # silently emits nothing.
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True
        # Reset level only when the library installed its own handlers — in
        # that case the level was the library's filter on its own log path
        # (which we just removed) and resetting to NOTSET delegates filtering
        # to root. If the library *didn't* install handlers but we got here
        # via ``propagate=False`` (intentional library suppression that we're
        # flipping), leave the level alone so the operator's filter intent is
        # preserved.
        if had_own_handlers:
            lg.setLevel(logging.NOTSET)


def reroute_third_party_loggers() -> None:
    """Public entry point to (re-)route third-party loggers to the root handler.

    ``configure_logging`` already calls this routing at import time, but on a
    uvicorn-fronted service the third-party libraries (uvicorn / fastmcp / mcp
    / slowapi) usually aren't imported yet at that point, so that pass no-ops
    for them. Call this from the ASGI lifespan startup — after those libraries
    are imported — to route them. Idempotent (guarded by the per-logger
    snapshot), so the re-call is safe.
    """
    _route_third_party_to_root()


def _reset_for_testing() -> None:
    """Clear the idempotency guard so tests can reconfigure with new settings.

    Production code must NOT call this — service entry points configure
    logging once at module import and the guard exists to keep later
    imports from racing or overriding. This function is not safe to call
    concurrently with active log calls; use it only between test fixtures
    when no logging is in flight. Tests that exercise both environments
    (e.g. dev ConsoleRenderer vs prod JSONRenderer) can call this between
    fixtures to get a clean slate without reaching into private module state.

    Note: callers must also recreate any `structlog.get_logger()` instances
    they hold. We set `cache_logger_on_first_use=True`, so BoundLoggers that
    already bound their processor chain can't be un-cached;
    `structlog.reset_defaults()` only prevents *new* instances from
    inheriting the old config.
    """
    global _configured, _configured_environment, _configured_log_level
    global _configured_json_logs, _configured_log_file
    global _installed_handler, _installed_file_handler
    with _configure_lock:
        structlog.reset_defaults()
        # Drop any bound contextvars so test fixtures don't leak request-scoped
        # state (tenant_id, trace_id, …) into the next test's log output.
        structlog.contextvars.clear_contextvars()
        # Tear down only the handlers configure_logging installed so stdlib
        # loggers stop emitting through the old processor chain, without
        # touching handlers owned by pytest caplog or other test infra.
        root = logging.getLogger()
        for h in (_installed_handler, _installed_file_handler):
            if h is not None:
                root.removeHandler(h)
                # Close file handles so the rotating file descriptor doesn't
                # leak between test runs (Windows would fail on tmp cleanup).
                try:
                    h.close()
                except Exception:
                    pass
        _installed_handler = None
        _installed_file_handler = None
        root.setLevel(logging.WARNING)
        # Clear the per-logger WARNING overrides that _configure_logging_impl
        # installs at INFO level. Without this, a test that reconfigures at
        # DEBUG would see root at DEBUG but these four stuck at WARNING —
        # silent divergence from production behavior.
        for dep in ("httpx", "httpcore", "google.auth", "google.auth.transport"):
            logging.getLogger(dep).setLevel(logging.NOTSET)
        # Clear the ddtrace-writer CRITICAL floor installed by
        # _configure_logging_impl so a test reconfiguring at DEBUG doesn't see
        # these stuck at CRITICAL — same rationale as the overrides above.
        for dep in ("ddtrace.internal.writer.writer", "ddtrace.llmobs._writer"):
            logging.getLogger(dep).setLevel(logging.NOTSET)
        # Restore third-party loggers to pre-config state from the snapshot
        # captured by ``_route_third_party_to_root``. Best-effort: any
        # FileHandler / SocketHandler that was closed during stripping won't
        # actually write again, but the logger's ``handlers``, ``propagate``,
        # and ``level`` attributes are restored to their pre-config shape so
        # tests observing logger structure see what they would have seen
        # before ``configure_logging`` ran.
        for name, (
            handlers,
            propagate,
            level,
        ) in _third_party_logger_original_state.items():
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
            for h in handlers:
                lg.addHandler(h)
            lg.propagate = propagate
            lg.setLevel(level)
        _third_party_logger_original_state.clear()
        _configured = False
        _configured_environment = None
        _configured_log_level = None
        _configured_json_logs = None
        _configured_log_file = None


def _configure_logging_impl(
    environment: str,
    log_level: str,
    json_logs: bool,
    log_file: str | None = None,
) -> None:
    # Validate up-front so a typo fails fast — before we build the processor
    # chain or touch the root logger. Upstream callers often validate via
    # pydantic Literal, but this helper is shared so we also enforce at the
    # boundary. Silent fallback to INFO would quietly re-enable INFO traffic
    # when prod is meant to run at WARNING.
    min_level = getattr(logging, log_level.upper(), None)
    if min_level not in _VALID_LEVELS:
        raise ValueError(
            f"Invalid log_level {log_level!r}; "
            "expected one of DEBUG/INFO/WARNING/ERROR/CRITICAL"
        )

    processors: list[Any] = _base_processors()

    if not json_logs:
        # ConsoleRenderer pops `event` itself, so we don't rename the key
        # here — doing so would leave the renderer with no message text.
        # Also skip `_map_to_gcp_severity`: it would duplicate
        # `add_log_level`'s output as a redundant `severity=INFO` field.
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()))
    else:
        # JSON for Cloud Run. The JSON chain (`format_exc_info` → drop
        # `level` → GCP severity → `event` → `message`) is centralised in
        # `_json_processors()` because the stdlib bridge below needs the
        # same sequence. ConsoleRenderer handles `exc_info` directly, so
        # `format_exc_info` MUST NOT run in the dev branch.
        processors.extend(_json_processors())
        processors.append(structlog.processors.JSONRenderer(default=str))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (SQLAlchemy, httpx, …) through the same processor
    # chain so their output also carries GCP severity labels instead of
    # landing as unstructured DEFAULT-severity text. Uvicorn and FastMCP
    # install their own handlers with ``propagate=False`` and bypass the
    # root handler — ``_route_third_party_to_root`` (called below) reverses
    # that so their access/error lines also flow through the formatter.
    foreign_pre_chain: list[Any] = _base_processors()
    # add_logger_name surfaces the originating stdlib logger (`sqlalchemy.engine`,
    # `httpx`, …) as the `logger` field — useful in both dev and prod. Keep
    # it foreign-only: on the native structlog chain it reads `logger.name`
    # from PrintLogger which doesn't have that attribute, so sharing it via
    # _base_processors() would crash every native log call.
    foreign_pre_chain.append(structlog.stdlib.add_logger_name)
    if not json_logs:
        foreign_renderer: Any = structlog.dev.ConsoleRenderer(
            colors=sys.stdout.isatty(),
        )
    else:
        # Same JSON chain as the native path — kept in sync via
        # `_json_processors()`. ConsoleRenderer pops `event` itself and
        # handles `exc_info` directly, so dev doesn't run these.
        foreign_pre_chain.extend(_json_processors())
        foreign_renderer = structlog.processors.JSONRenderer(default=str)
    # Modern ProcessorFormatter API: a single `processors=` list runs in
    # order. `remove_processors_meta` sits immediately before the renderer
    # per structlog's documented canonical pattern — functionally
    # equivalent to running it first today (exc_info is copied to the
    # event_dict root, not nested under _record), but placing it last
    # sweeps any future meta keys structlog might add without a code edit.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            *foreign_pre_chain,
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            foreign_renderer,
        ],
    )
    global _installed_handler, _installed_file_handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # Replace only the handler this module installed — leaves any
    # StreamHandler put there by logging.basicConfig() or another library
    # untouched. The idempotency guard means this normally runs once; on
    # reset + reconfigure it swaps our handler cleanly.
    if _installed_handler is not None and _installed_handler in root.handlers:
        root.removeHandler(_installed_handler)
    root.addHandler(handler)
    _installed_handler = handler

    # Optional file sink — on-prem deployments want greppable JSON logs on
    # disk without `docker logs` contortions. Uses the exact same
    # ProcessorFormatter as the stdout handler so file contents match
    # `docker compose logs` byte-for-byte. Rotation: daily at UTC midnight,
    # 5 historical files retained (today + 5 → "last 5 days of context"
    # per the product spec). Size-based rotation would also work but daily
    # lines up neatly with the support-bundle `--since 5d` default.
    if _installed_file_handler is not None and _installed_file_handler in root.handlers:
        root.removeHandler(_installed_file_handler)
        try:
            _installed_file_handler.close()
        except Exception:
            pass
    _installed_file_handler = None
    if log_file:
        import os
        from logging.handlers import TimedRotatingFileHandler

        log_dir = os.path.dirname(log_file)
        if log_dir:
            # Best-effort: compose bind-mounts the parent dir, install.sh
            # creates it with the right ownership. If it's missing we log
            # a warning via the already-installed stdout handler and skip
            # the file sink rather than crashing service startup — stdout
            # is still working.
            try:
                os.makedirs(log_dir, exist_ok=True)
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "log_file parent dir not writable",
                    extra={"path": log_file, "error": str(exc)},
                )
                log_file = None
        if log_file:
            try:
                fh = TimedRotatingFileHandler(
                    filename=log_file,
                    when="midnight",
                    interval=1,
                    backupCount=5,
                    utc=True,
                    encoding="utf-8",
                    delay=True,  # don't open the file until the first write
                )
                # Suffix format so rotated files sort lexicographically and
                # humans recognise the date at a glance:
                #   platform-auth-api.log           (today)
                #   platform-auth-api.log.2026-04-17
                fh.suffix = "%Y-%m-%d"
                fh.setFormatter(formatter)
                root.addHandler(fh)
                _installed_file_handler = fh
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "failed to open log_file; continuing with stdout only",
                    extra={"path": log_file, "error": str(exc)},
                )

    root.setLevel(min_level)

    # Silence noisy dependency loggers so INFO traffic from httpx's
    # per-request lines and google-auth token refreshes doesn't drown the
    # app's own entries. Target `google.auth`/`google.auth.transport`
    # specifically rather than the `google.*` root so operationally useful
    # INFO from google.cloud.storage / google.cloud.bigquery / etc. still
    # comes through. Skip at DEBUG (explicit opt-in to verbose output) and
    # at WARNING+ (root already filters there, so the setLevel calls are
    # redundant). Only INFO actually needs the floor.
    if logging.DEBUG < min_level < logging.WARNING:
        for dep in ("httpx", "httpcore", "google.auth", "google.auth.transport"):
            logging.getLogger(dep).setLevel(logging.WARNING)

    # ddtrace's trace + LLMObs writers emit a benign ERROR every time a flush to
    # the local agent (localhost:8126) times out or the agent resets the
    # connection ("failed to send, dropping N traces …" / "failed to send N
    # LLMObs span events …"). These are telemetry-plumbing failures — dropped
    # spans, send_to_telemetry:false — not app errors, but at ERROR they surface
    # as errors in every sink (Datadog Logs, GCP, the caura-ops error alerter)
    # and drive alert flapping. Floor them at CRITICAL so the ERROR/WARNING noise
    # is dropped while a genuine writer CRITICAL still comes through. Unlike the
    # httpx floor above (INFO-only), this must also apply at WARNING — the ERROR
    # clears root's WARNING filter otherwise. Skip only at DEBUG (opt-in verbose).
    if min_level > logging.DEBUG:
        for dep in ("ddtrace.internal.writer.writer", "ddtrace.llmobs._writer"):
            logging.getLogger(dep).setLevel(logging.CRITICAL)

    _route_third_party_to_root()
