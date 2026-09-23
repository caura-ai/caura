"""Adapter lifecycle with Caura delivery leases and explicit acknowledgements."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import uuid
from collections.abc import Awaitable, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx
from caura_bus_core import (
    CONFIG_ENV_VAR,
    AgentConfig,
    Bus,
    Envelope,
)
from caura_bus_core.bus import HumanRequired, PlatformError
from caura_bus_core.collaboration import Checkpoint, Presence
from caura_bus_core.retry import Backoff, transient_status

log = logging.getLogger("caura-bus-adapter")


@runtime_checkable
class Adapter(Protocol):
    """Structural interface every adapter implements."""

    async def wait_until_idle(self) -> None: ...

    async def consume(self, env: Envelope) -> None: ...


class NoIdleGate:
    """Mixin/base for adapters that have nothing to gate on.

    Override ``consume`` only; ``wait_until_idle`` is a no-op so the
    adapter consumes envelopes as fast as the bus delivers them.
    """

    async def wait_until_idle(self) -> None:
        return None

    async def consume(self, env: Envelope) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


_active_delivery = ContextVar("caura_active_delivery", default=None)


async def checkpoint(
    *,
    checkpoint_key,
    summary,
    proposed_action,
    action_type="read",
    confidence=1.0,
    missing_information=None,
    conflicting_results=False,
    request_human=False,
):
    """Call before a consequential action; Caura may pause execution for a human."""
    active = _active_delivery.get()
    if active is None:
        raise RuntimeError("checkpoint must run inside adapter.consume")
    bus, claim = active
    return await bus.checkpoint(
        Checkpoint(
            delivery_id=claim.delivery_id,
            lease_token=claim.lease_token,
            checkpoint_key=checkpoint_key,
            summary=summary,
            proposed_action=proposed_action,
            action_type=action_type,
            confidence=confidence,
            missing_information=missing_information or [],
            conflicting_results=conflicting_results,
            request_human=request_human,
        )
    )


async def process_delivery(bus, adapter, claim, *, lease_seconds=30):
    """Renew and observe control events while the cooperative runtime executes."""

    async def renew():
        while True:
            await asyncio.sleep(min(lease_seconds / 3, 2.0))
            await bus.settle(claim, "renew")

    async def control():
        async for event in bus.events(after=claim.event_cursor):
            if (
                event["event_type"] == "delivery.interrupt"
                and event["payload"].get("delivery_id") == claim.delivery_id
            ):
                raise HumanRequired(event["payload"])

    context_token = _active_delivery.set((bus, claim))
    failed = False
    try:
        try:
            async with asyncio.TaskGroup() as group:
                renewal = group.create_task(renew())
                controls = group.create_task(control())
                env = claim.envelope
                if claim.resume_context:
                    env = env.model_copy(
                        update={
                            "parts": [
                                *env.parts,
                                {"type": "caura_human_decision", **claim.resume_context},
                            ]
                        }
                    )
                await adapter.consume(env)
                renewal.cancel()
                controls.cancel()
        except* HumanRequired:
            failed = True
            log.info("Caura paused delivery %s for human input", claim.delivery_id)
        except* PlatformError as errors:
            if errors.subgroup(
                lambda exc: (
                    isinstance(exc, PlatformError) and exc.status != 409 and not transient_status(exc.status)
                )
            ):
                raise
            failed = True
            log.warning("Delivery interrupted by an unavailable API or lost lease")
        except* Exception:
            failed = True
            log.exception("delivery failed id=%s attempt=%s", claim.delivery_id, claim.attempt)
        if failed:
            if getattr(adapter, "supports_interrupt", False):
                try:
                    await bus.settle(claim, "paused")
                    return
                except (PlatformError, httpx.TransportError):
                    log.warning("Pause confirmation failed; attempting safe lease settlement")
            try:
                await bus.settle(claim, "nack")
            except (PlatformError, httpx.TransportError):
                log.warning(
                    "delivery remains paused or will recover after lease expiry: %s",
                    claim.delivery_id,
                )
            return
        try:
            await bus.settle(claim, "ack")
        except PlatformError as exc:
            # A human can pause between consume returning and the ACK. The
            # runtime is now stopped, so confirm instead of stranding the case.
            if exc.status == 409 and getattr(adapter, "supports_interrupt", False):
                try:
                    await bus.settle(claim, "paused")
                    return
                except PlatformError:
                    log.warning("Pause confirmation rejected after ACK conflict")
            raise
        log.info("consumed msg_id=%s thread=%s", claim.envelope.id, claim.envelope.thread_id)
    finally:
        _active_delivery.reset(context_token)


@asynccontextmanager
async def connected_bus(config):
    """An upstream restart during startup must not strand the adapter either."""
    bus = Bus(config)
    backoff = Backoff()
    try:
        while True:
            try:
                await bus.connect()
                break
            except (httpx.TransportError, PlatformError) as exc:
                if isinstance(exc, PlatformError) and not transient_status(exc.status):
                    raise
                log.warning("Caura connection unavailable; retrying")
                await backoff.sleep()
        yield bus
    finally:
        await bus.close()


async def run_adapter(config: AgentConfig, adapter: Adapter) -> None:
    """Advertise presence, wake on live events, and execute one leased item at a time."""
    profile = Presence(
        session_id=str(uuid.uuid4()),
        display_name=config.agent.agent_id,
        description=config.agent.description,
        capabilities=getattr(adapter, "capabilities", []),
        supports_interrupt=getattr(adapter, "supports_interrupt", False),
    )
    async with connected_bus(config) as bus:
        wake = asyncio.Event()
        wake.set()

        async def presence():
            backoff = Backoff()
            while True:
                try:
                    await bus.advertise(profile)
                except (httpx.TransportError, PlatformError) as exc:
                    if isinstance(exc, PlatformError) and not transient_status(exc.status):
                        raise
                    log.warning("Presence update unavailable; retrying without bypassing Caura")
                    await backoff.sleep()
                    continue
                backoff.reset()
                await asyncio.sleep(15)

        async def notifications():
            async for event in bus.events():
                if event["event_type"] in {"message.available", "human.decided"}:
                    wake.set()

        async def consume():
            backoff = Backoff()
            while True:
                try:
                    await adapter.wait_until_idle()
                    claim = await bus.claim()
                    if claim:
                        profile.status = "busy"
                        try:
                            await bus.advertise(profile)
                            await process_delivery(bus, adapter, claim)
                        finally:
                            profile.status = "ready"
                            await bus.advertise(profile)
                    else:
                        # Reconciliation also finds expired leases if an event was lost.
                        try:
                            await asyncio.wait_for(wake.wait(), timeout=2)
                        except TimeoutError:
                            log.debug("Inbox event wait timed out; polling durable state")
                        wake.clear()
                    backoff.reset()
                except (httpx.TransportError, PlatformError) as exc:
                    # A lost delivery lease is reconciled by the next claim;
                    # authentication and other client errors remain fatal.
                    if (
                        isinstance(exc, PlatformError)
                        and exc.status != 409
                        and not transient_status(exc.status)
                    ):
                        raise
                    log.warning("Caura unavailable or lease lost; retrying")
                    await backoff.sleep()

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(presence())
                tasks.create_task(notifications())
                tasks.create_task(consume())
        finally:
            profile.status = "offline"
            try:
                await bus.advertise(profile)
            except (httpx.TransportError, PlatformError):
                log.warning("Could not announce shutdown; presence will expire automatically")


def make_arg_parser(prog: str, description: str) -> argparse.ArgumentParser:
    """argparse with the flags every adapter shares."""
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(f"path to caura-bus.toml (default $ {CONFIG_ENV_VAR} or ./caura-bus.toml)"),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("CAURA_BUS_LOG_LEVEL", "INFO"),
        help="logging level (default INFO; or $CAURA_BUS_LOG_LEVEL)",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run_with_signals(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    loop = asyncio.get_running_loop()

    def _cancel(*_: object) -> None:
        if not task.done():
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _cancel)
        except NotImplementedError:
            # Non-POSIX: SIGINT propagates via KeyboardInterrupt anyway.
            log.debug("Native signal handler unavailable; retaining KeyboardInterrupt fallback")

    try:
        await task
    except asyncio.CancelledError:
        log.debug("Adapter stopped after cancellation")


def adapter_main(coro: Awaitable[None]) -> None:
    """Run an adapter coroutine with SIGINT/SIGTERM-driven shutdown."""
    if not isinstance(coro, Coroutine):
        raise TypeError("adapter_main expects a coroutine, got " + type(coro).__name__)
    asyncio.run(_run_with_signals(coro))
