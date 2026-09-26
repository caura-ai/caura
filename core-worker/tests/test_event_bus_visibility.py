"""OSS audit 09/02 L-54 — the worker must say which event bus it resolved,
and must not report success into one nothing is listening to.

Both halves of one failure: ``common.events.factory.get_event_bus`` reads the
PROCESS ENVIRONMENT, while ``SettingsConfigDict(env_file=".env")`` loads a .env
value onto the settings OBJECT without exporting it. A developer who set
``EVENT_BUS_BACKEND=pubsub`` in .env got a worker that looked configured and
silently came up on the in-process bus — subscribed to a bus no other process
publishes to, consuming nothing, with every probe green.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from common.events.inprocess import InProcessEventBus
from core_worker import cli

# No ``pytestmark``: core-worker/pytest.ini sets ``asyncio_mode = auto``, and a
# blanket asyncio mark warns on every sync test in this file.


# ---------------------------------------------------------------------------
# The three dead settings fields.
# ---------------------------------------------------------------------------


def test_settings_declares_no_event_bus_fields():
    """A declared field nothing reads is worse than no field: it is what made
    the .env value look effective. The names live in the environment, and
    neither core-api nor core-storage-api declares them either."""
    from core_worker.config import Settings

    for field in ("event_bus_backend", "gcp_project_id", "event_bus_subscription_prefix"):
        assert field not in Settings.model_fields, (
            f"{field} is read from os.environ by common.events.factory, not from "
            "Settings — declaring it here makes a .env value look effective"
        )


# ---------------------------------------------------------------------------
# The compensating control: name the bus that was actually resolved.
# ---------------------------------------------------------------------------


def test_startup_names_the_bus_the_factory_resolved(caplog):
    """Asserts on the EMITTED RECORD, not on the source text.

    It has to name the resolved CLASS. Logging a configured value would restate
    the operator's intent, which is exactly the thing that is already wrong in
    the case this exists to catch — so the test pins the class name reaching
    the log, which only a real resolution can produce.
    """
    bus = InProcessEventBus()

    with (
        patch("core_worker.app.init_platform_providers"),
        patch("core_worker.app.configure_consumer"),
        patch("core_worker.app.register_consumers"),
        patch("core_worker.app.register_lifecycle_consumers"),
        patch("core_worker.app.get_event_bus", return_value=bus),
        patch("core_worker.app.close_storage_client", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="core_worker.app"),
    ):
        from core_worker.app import create_app

        with TestClient(create_app()):
            pass

    named = [r for r in caplog.records if getattr(r, "event_bus", None) == "InProcessEventBus"]
    assert named, (
        "startup must log the resolved bus class; got "
        f"{[getattr(r, 'event_bus', None) for r in caplog.records]}"
    )


def test_the_bus_is_named_before_it_is_started():
    """``start()`` is where a misconfigured Pub/Sub backend hangs or throws, so
    a line emitted after it would be missing in precisely the incident that
    needs it."""
    bus = MagicMock()
    order: list[str] = []
    bus.start = AsyncMock(side_effect=lambda: order.append("start"))
    bus.stop = AsyncMock(return_value=None)
    bus.is_healthy = True

    def _record(msg, *a, **kw):
        if "event bus" in str(msg):
            order.append("log")

    with (
        patch("core_worker.app.init_platform_providers"),
        patch("core_worker.app.configure_consumer"),
        patch("core_worker.app.register_consumers"),
        patch("core_worker.app.register_lifecycle_consumers"),
        patch("core_worker.app.get_event_bus", return_value=bus),
        patch("core_worker.app.close_storage_client", new=AsyncMock()),
        patch("core_worker.app.logger.info", side_effect=_record),
    ):
        from core_worker.app import create_app

        with TestClient(create_app()):
            pass

    assert order[: order.index("start") + 1].count("log") == 1, (
        f"the bus must be named before start(); order was {order}"
    )


# ---------------------------------------------------------------------------
# The backfill CLI refuses rather than reporting success into a void.
# ---------------------------------------------------------------------------


@pytest.fixture
def run_cli(monkeypatch):
    """Drive ``_amain`` with the bus and the backfill both under control.

    ``calls`` staying empty is the assertion that matters on the refusal path:
    exit code 2 alone would also be satisfied by an argument-parsing failure.
    """
    calls: list[dict] = []

    async def _fake_backfill(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(scanned=3, published=3, elapsed_s=0.1)

    async def _noop():
        return None

    monkeypatch.setattr(cli, "run_embedding_backfill", _fake_backfill)
    monkeypatch.setattr(cli, "close_storage_client", _noop)

    async def _run(bus_factory, argv):
        monkeypatch.setattr(cli, "get_event_bus", bus_factory)
        return await cli._amain(argv)

    return SimpleNamespace(run=_run, calls=calls)


async def test_the_backfill_refuses_on_the_in_process_bus(run_cli, capsys):
    """The backfill's entire output is published ``EMBED_REQUESTED`` events and
    this process registers no handlers, so on the in-process bus every one is
    delivered to zero subscribers — while the run still prints ``published=N``,
    which reads as success."""
    bus = InProcessEventBus()
    rc = await run_cli.run(lambda: bus, ["backfill-embeddings", "--tenant-id", "t1"])

    assert rc == 2
    assert run_cli.calls == [], "the backfill ran despite the refusal"
    assert "EVENT_BUS_BACKEND" in capsys.readouterr().err, (
        "the refusal must name the variable an operator has to set"
    )


async def test_dry_run_never_resolves_the_bus(run_cli):
    """The exemption has to hold on a BROKEN bus, which is the whole point of
    it: ``get_event_bus()`` raises ``RuntimeError`` when ``EVENT_BUS_BACKEND=pubsub``
    is set without ``GCP_PROJECT_ID`` / ``EVENT_BUS_SUBSCRIPTION_PREFIX``.

    Resolving it eagerly — above the ``not args.dry_run`` term rather than
    inside the short-circuit — turns a dry run that used to exit 0 into an
    uncaught traceback. The only other call site is the ``finally``, which
    wraps it deliberately for this reason.
    """

    def _explode():
        raise RuntimeError("EVENT_BUS_BACKEND=pubsub requires GCP_PROJECT_ID")

    rc = await run_cli.run(_explode, ["backfill-embeddings", "--tenant-id", "t1", "--dry-run"])

    assert rc == 0
    assert len(run_cli.calls) == 1
    assert run_cli.calls[0]["dry_run"] is True


async def test_a_configured_bus_runs_normally(run_cli):
    """The refusal keys off the resolved bus type, not off the env var being
    unset, so any real backend proceeds."""

    async def _stop():
        return None

    rc = await run_cli.run(lambda: SimpleNamespace(stop=_stop), ["backfill-embeddings", "--tenant-id", "t1"])

    assert rc == 0
    assert len(run_cli.calls) == 1
    assert run_cli.calls[0]["dry_run"] is False
