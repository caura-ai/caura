"""Operator CLI using the same Caura credential boundary as every other client."""

import asyncio
import json
import os
from pathlib import Path
from typing import Literal

import httpx
import typer
from caura_bus_core import Bus, PlatformError, SendMessage, load_config
from caura_bus_core.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH

from .hooks import install_hooks
from .runtime import CodexQueue, WakeState, receive, state_path, supervise

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Caura agent messaging.")
agents_app = typer.Typer(no_args_is_help=True)
threads_app = typer.Typer(no_args_is_help=True)
app.add_typer(agents_app, name="agents")
app.add_typer(threads_app, name="threads")
hooks_app = typer.Typer(no_args_is_help=True)
app.add_typer(hooks_app, name="hooks")


@app.command()
def wake(
    runtime: str = typer.Option(...),
    thread: str | None = None,
    config: Path | None = typer.Option(None),
    state: Path | None = typer.Option(None),
):
    """Wake a Codex session through its native queue. Claude Code uses native hooks."""
    try:
        if runtime not in {"codex", "claude-code"}:
            raise ValueError("Cursor automatic wake is unsupported in this release")
        if runtime == "codex" and not thread:
            raise ValueError("--thread is required for Codex native queue delivery")
        if runtime == "claude-code":
            raise ValueError(
                "Claude Code has no session queue API; use hooks install "
                "--runtime claude-code --scope project for its bounded Stop listener"
            )
        cfg = load_config(config)
        asyncio.run(
            supervise(
                cfg,
                runtime,
                WakeState(state or state_path(cfg)),
                CodexQueue(thread) if runtime == "codex" else None,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        typer.echo("Caura waker stopped.", err=True)
    except PlatformError as exc:
        if exc.status in {401, 403}:
            typer.echo("Caura credential revoked or unauthorized; waker stopped.", err=True)
            return
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def recv(
    brief: bool = typer.Option(False),
    hook: str | None = None,
    config: Path | None = typer.Option(None),
    state: Path | None = typer.Option(None),
    wait: float = typer.Option(0, min=0, max=3600),
    idle_listen_seconds: float = typer.Option(5, min=0, max=3600),
):
    """Emit a fixed wake hint without claiming, acknowledging, or reading message bodies."""
    config = config or Path(os.environ.get(CONFIG_ENV_VAR, str(DEFAULT_CONFIG_PATH)))
    if not os.environ.get("CAURA_API_KEY", "").strip() or not config.is_file():
        # Project hooks also run in ordinary contributors' sessions. Missing
        # identity is opt-out, and must not create a hook error or touch state.
        return
    try:
        if not brief or hook not in {None, "Stop", "UserPromptSubmit"}:
            raise ValueError("use --brief, optionally with --hook Stop or UserPromptSubmit")
        cfg = load_config(config)
        output = asyncio.run(
            receive(cfg, WakeState(state or state_path(cfg)), hook, wait, idle_listen_seconds)
        )
        if output:
            typer.echo(output)
    except (PlatformError, httpx.TransportError, ValueError, RuntimeError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@hooks_app.command("install")
def hooks_install(
    runtime: str = typer.Option(...),
    scope: Literal["project", "user"] = "project",
    config: Path | None = typer.Option(None),
    state: Path | None = typer.Option(None),
    listen_seconds: int = typer.Option(600, min=0, max=3600),
    idle_listen_seconds: int = typer.Option(5, min=0, max=3600),
    shared: bool = typer.Option(False),
):
    """Merge Claude hooks into project settings; user settings require --scope user."""
    try:
        if runtime != "claude-code":
            raise ValueError("hooks support claude-code; use wake for Codex; Cursor unsupported")
        typer.echo(
            json.dumps(
                install_hooks(
                    scope,
                    config=config,
                    state=state,
                    listen_seconds=listen_seconds,
                    idle_listen_seconds=idle_listen_seconds,
                    shared=shared,
                ),
                indent=2,
            )
        )
    except (ValueError, RuntimeError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def execute(config, operation):
    async def run():
        async with Bus(load_config(config)) as bus:
            result = await operation(bus)
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))

    try:
        asyncio.run(run())
    except (PlatformError, httpx.TransportError, ValueError, RuntimeError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def doctor(config: Path | None = typer.Option(None)):
    """Verify gateway access and the credential's effective identity."""
    execute(config, lambda bus: bus.connect())


@app.command()
def send(
    body: str,
    to: list[str] = typer.Option(..., "--to"),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
    kind: str = "info",
    thread: str | None = None,
    reply_to: str | None = None,
    config: Path | None = typer.Option(None),
):
    """Send a message; keep the idempotency key to safely retry an uncertain send."""

    async def run(bus):
        receipt = await bus.send(
            SendMessage(to=to, body=body, kind=kind, thread_id=thread, reply_to=reply_to),
            idempotency_key=idempotency_key,
        )
        return receipt.model_dump()

    execute(config, run)


@app.command()
def replay(
    limit: int = 20,
    thread: str | None = None,
    peer: str | None = None,
    before: str | None = None,
    config: Path | None = typer.Option(None),
):
    """Read your sent and received messages without consuming them."""
    execute(
        config,
        lambda bus: bus.recent(limit=limit, thread_id=thread, peer_agent_id=peer, before=before),
    )


@app.command()
def status(message_id: str, config: Path | None = typer.Option(None)):
    """Inspect a message and its delivery states."""
    execute(config, lambda bus: bus.status(message_id))


@app.command()
def discover(
    capability: str | None = None,
    include_offline: bool = False,
    config: Path | None = typer.Option(None),
):
    """Find connected peers by capability and availability."""
    execute(config, lambda bus: bus.discover(capability=capability, available_only=not include_offline))


@app.command()
def watch(config: Path | None = typer.Option(None), after: int = 0):
    """Follow live Caura events; reconnects resume from the durable event cursor."""

    async def run():
        async with Bus(load_config(config)) as bus:
            async for event in bus.events(after=after):
                typer.echo(json.dumps(event, ensure_ascii=False))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        typer.echo("Caura event watch stopped.", err=True)


@agents_app.command("list")
def agents_list(fleet: str | None = None, config: Path | None = typer.Option(None)):
    execute(config, lambda bus: bus.agents(fleet))


@threads_app.command("list")
def threads_list(config: Path | None = typer.Option(None)):
    execute(config, lambda bus: bus.threads())


if __name__ == "__main__":
    app()
