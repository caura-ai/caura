"""Uvicorn launcher that configures structlog logging in the *parent* process.

With ``uvicorn --workers N`` the multiprocess supervisor (the parent process)
never imports the ASGI app module, so the import-time ``configure_logging()``
call in ``<service>/app.py`` runs only inside the workers. The parent's own
lifecycle lines — ``Started parent process [N]``, ``Waiting for child process
[N]``, ``Received SIGTERM, exiting.``, ``Uvicorn running on ...`` — are emitted
via the ``uvicorn.error`` logger with uvicorn's *default* configuration, which
writes plain ``INFO:`` text to stdout. Those lines bypass the structlog JSON
pipeline and reach Datadog without a ``status`` field, where they are
mis-classified as ``status:error`` (the dominant false-error source across the
Cloud Run services in the 2026-07 error sweeps).

This launcher fixes that by calling ``configure_logging()`` — from the *same*
settings object the app uses — before starting the server, then running uvicorn
with ``log_config=None``. With ``log_config=None`` uvicorn does not install its
own stream handlers, so the ``uvicorn.*`` loggers keep ``propagate=True`` and
their records reach the structlog root handler in both the parent and the
workers. The supervisor lines then emit as structured JSON with ``status:info``.

Usage (see each service's Dockerfile ``CMD``)::

    python -m common.serve <app_import> --settings <settings_import> \
        --port <port> [--host H] [--workers N] [--timeout-keep-alive S] \
        [--timeout-worker-healthcheck S]

``<app_import>`` is an ASGI import string (``core_api.app:app``); it is passed
to uvicorn as a string so the workers can re-import it. ``<settings_import>`` is
``module:attr`` for the service's settings singleton, imported here only to read
``environment`` / ``log_level`` / ``log_format_json`` / ``log_file`` — matching
the app's own ``configure_logging()`` call exactly.

Vendoring constraint: this file is vendored byte-for-byte into
caura-enterprise as an ``identical``-policy ``common/`` file, whose CI
runs ``ruff format --check`` with no resolved config (ruff's 88-col default).
Keep every line <= 88 cols so the vendored copy stays format-stable there — a
wider line passes OSS CI (which does not format-check ``common/``) but wedges
the enterprise re-vendor (the 2026-06 structlog_config drift incident).
"""

from __future__ import annotations

import argparse
import importlib
from typing import Any

import uvicorn

from common.structlog_config import configure_logging


def _load(path: str) -> Any:
    """Return the attribute named by a ``module:attr`` import path."""
    module_name, sep, attr = path.partition(":")
    if not sep or not attr:
        raise ValueError(f"expected a 'module:attr' import path, got {path!r}")
    return getattr(importlib.import_module(module_name), attr)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="common.serve")
    p.add_argument("app", help="ASGI app import string, e.g. core_api.app:app")
    p.add_argument(
        "--settings",
        required=True,
        help="Import path of the settings singleton, e.g. core_api.config:settings",
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument(
        "--timeout-keep-alive", type=int, default=65, dest="timeout_keep_alive"
    )
    # uvicorn's multiprocess supervisor pings each worker every 0.5s and
    # SIGKILLs one that has not answered within this many seconds, then
    # respawns it. A SIGKILLed worker never runs lifespan shutdown, so
    # whatever its shutdown hooks release stays leaked. Exposed here because
    # the value was previously unreachable: this parser rejects unknown
    # flags, so putting --timeout-worker-healthcheck in a service CMD made
    # argparse exit 2 and the container never started.
    #
    # Defaulted to uvicorn's own 5 rather than an opinion of ours: this file
    # is vendored byte-for-byte into the enterprise repo, so the per-service
    # value belongs in that service's CMD, next to its --workers.
    p.add_argument(
        "--timeout-worker-healthcheck",
        type=int,
        default=5,
        dest="timeout_worker_healthcheck",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    settings = _load(args.settings)

    # Parent-process logging: the workers re-run this via the app import, but
    # the supervisor process must configure it here or its lifecycle lines
    # bypass structlog. Mirrors the app's own configure_logging() arguments.
    configure_logging(
        settings.environment,
        settings.log_level,
        json_logs=settings.log_format_json,
        log_file=settings.log_file or None,
    )

    # log_config=None: do NOT let uvicorn install its own stream handlers.
    # That keeps uvicorn.{error,access} propagating to the structlog root
    # handler configured above (in the parent AND the workers), so every
    # uvicorn line — including the parent supervisor's — emits as JSON with a
    # Datadog status. Installing uvicorn's config would re-plain-text them.
    uvicorn.run(
        args.app,
        host=args.host,
        port=args.port,
        workers=args.workers,
        timeout_keep_alive=args.timeout_keep_alive,
        timeout_worker_healthcheck=args.timeout_worker_healthcheck,
        log_config=None,
    )


if __name__ == "__main__":
    main()
