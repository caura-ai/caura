"""Operator CLI using the same Caura credential boundary as every other client."""

import asyncio
import json
import os
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Literal

import httpx
import typer
from caura_bus_core import RESYNC_EVENT, Bus, PlatformError, ResponseCollector, SendMessage, load_config
from caura_bus_core.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH

from .hooks import DEFAULT_LISTEN_SECONDS, install_hooks
from .runtime import CodexQueue, WakeState, listen, load_key, receive, state_path, supervise
from .setup import Options, SetupError, run_setup

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Caura agent messaging.")
agents_app = typer.Typer(no_args_is_help=True)
threads_app = typer.Typer(no_args_is_help=True)
app.add_typer(agents_app, name="agents")
app.add_typer(threads_app, name="threads")
hooks_app = typer.Typer(no_args_is_help=True)
app.add_typer(hooks_app, name="hooks")


def show_version(value: bool):
    if value:
        typer.echo("caura-bus " + package_version("caura-bus-cli"))
        raise typer.Exit()


@app.callback()
def options(version: bool = typer.Option(False, "--version", callback=show_version, is_eager=True)):
    """Caura agent messaging."""


@app.command()
def wake(
    runtime: str = typer.Option(...),
    thread: str | None = typer.Option(
        None, help="Codex session id or exact name, or 'latest' for the newest session in --dir"
    ),
    directory: Path = typer.Option(Path("."), "--dir", help="Project directory for --thread latest"),
    config: Path | None = typer.Option(None),
    state: Path | None = typer.Option(None),
    key_file: Path | None = typer.Option(None, help="0600 file holding the agent key"),
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
                "--runtime claude-code --dir PROJECT for its background wake listener"
            )
        if not load_key(key_file):
            raise RuntimeError("no agent key: export CAURA_API_KEY or pass --key-file")
        cfg = load_config(config)
        asyncio.run(
            supervise(
                cfg,
                runtime,
                WakeState(state or state_path(cfg)),
                CodexQueue(thread, directory=directory.expanduser().resolve())
                if runtime == "codex"
                else None,
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
    wait: float = typer.Option(0, min=0, max=86400),
    idle_listen_seconds: float = typer.Option(5, min=0, max=3600),
    key_file: Path | None = typer.Option(None, help="0600 file holding the agent key"),
):
    """Emit a fixed wake hint without claiming, acknowledging, or reading message bodies.

    ``--hook Rewake`` is the background Claude Code listener: it waits up to
    ``--wait`` seconds and exits 2 with the wake text on stderr when new work
    arrives, which an ``asyncRewake`` hook turns into a new turn.
    """
    config = config or Path(os.environ.get(CONFIG_ENV_VAR, str(DEFAULT_CONFIG_PATH)))
    try:
        has_key = load_key(key_file)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if not has_key or not config.is_file():
        # Project hooks also run in ordinary contributors' sessions. Missing
        # identity is opt-out, and must not create a hook error or touch state.
        return
    try:
        if not brief or hook not in {None, "Stop", "UserPromptSubmit", "Rewake"}:
            raise ValueError("use --brief, optionally with --hook Rewake, Stop or UserPromptSubmit")
        cfg = load_config(config)
        if hook == "Rewake":
            pid = os.environ.get("CLAUDE_PID", "")
            message = asyncio.run(
                listen(
                    cfg,
                    WakeState(state or state_path(cfg)),
                    wait or DEFAULT_LISTEN_SECONDS,
                    int(pid) if pid.isdigit() else None,
                )
            )
            output = ""
        else:
            message = None
            output = asyncio.run(
                receive(cfg, WakeState(state or state_path(cfg)), hook, wait, idle_listen_seconds)
            )
        if output:
            typer.echo(output)
    except (PlatformError, httpx.TransportError, ValueError, RuntimeError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if message:
        # asyncRewake: exit code 2 starts a Claude Code turn with this text.
        typer.echo(message, err=True)
        raise typer.Exit(2)


@hooks_app.command("install")
def hooks_install(
    runtime: str = typer.Option(...),
    scope: Literal["project", "user"] = "project",
    directory: Path | None = typer.Option(None, "--dir", help="Project directory (default: current)"),
    config: Path | None = typer.Option(None),
    state: Path | None = typer.Option(None),
    key_file: Path | None = typer.Option(None, help="0600 file holding the agent key"),
    listen_seconds: int = typer.Option(DEFAULT_LISTEN_SECONDS, min=60, max=86400),
    idle_listen_seconds: int = typer.Option(5, min=0, max=3600, hidden=True),
    shared: bool = typer.Option(False),
):
    """Install Claude Code wake hooks (background listener) into project local settings."""
    try:
        if runtime != "claude-code":
            raise ValueError("hooks support claude-code; use wake for Codex; Cursor unsupported")
        typer.echo(
            json.dumps(
                install_hooks(
                    scope,
                    project=directory.expanduser().resolve() if directory else None,
                    config=config,
                    key_file=key_file,
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


@app.command()
def setup(
    runtime: str = typer.Option(..., help="claude or codex"),
    url: str = typer.Option(..., help="Caura gateway origin, e.g. https://caura.example"),
    key: str | None = typer.Option(
        None, help="Agent key (or set CAURA_API_KEY; prompted when absent)", envvar="CAURA_API_KEY"
    ),
    directory: Path = typer.Option(Path("."), "--dir", help="Project directory the runtime starts in"),
    agent_id: str | None = typer.Option(None, help="Expected agent id; setup fails if the key differs"),
    description: str | None = typer.Option(None, help="Register this agent's expertise for discovery"),
    config: Path | None = typer.Option(None, help="Agent config path (default: per-user config dir)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Verify the key and print the plan only"),
    hooks: bool = typer.Option(
        False,
        "--hooks",
        help="Also enable wake-ups: Claude Code hooks, or the Codex waker command (stores the key 0600)",
    ),
):
    """Connect Claude Code or Codex to Caura: verify the key, write config, wire MCP and instructions."""
    if not key:
        key = typer.prompt("Caura agent key", hide_input=True)
    options = Options(
        runtime=runtime,
        url=url,
        key=key or "",
        directory=directory,
        agent_id=agent_id,
        description=description,
        config=config,
        dry_run=dry_run,
        hooks=hooks,
    )
    try:
        code = run_setup(options, typer.echo, asyncio.run)
    except (SetupError, ValueError, OSError) as exc:
        typer.echo(f"caura-bus setup: {exc}", err=True)
        raise typer.Exit(1) from None
    raise typer.Exit(code)


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
    expect_reply_within_seconds: int | None = typer.Option(None, min=60, max=604800),
    capability: str | None = None,
    config: Path | None = typer.Option(None),
):
    """Send a message; keep the idempotency key to safely retry an uncertain send."""

    async def run(bus):
        receipt = await bus.send(
            SendMessage(
                to=to,
                body=body,
                kind=kind,
                thread_id=thread,
                reply_to=reply_to,
                expect_reply_within_seconds=expect_reply_within_seconds,
                capability=capability,
            ),
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
    reply_to: str | None = typer.Option(None, "--reply-to"),
    config: Path | None = typer.Option(None),
):
    """Read your sent and received messages without consuming them."""
    execute(
        config,
        lambda bus: bus.recent(
            limit=limit, thread_id=thread, peer_agent_id=peer, before=before, reply_to=reply_to
        ),
    )


@app.command()
def collect(
    message_id: str,
    timeout: float = typer.Option(30, min=0, max=45),
    expected: list[str] | None = typer.Option(None, "--expected"),
    config: Path | None = typer.Option(None),
):
    """Wait a bounded time for correlated replies to a sent request; reads only."""

    async def run(bus):
        result = await ResponseCollector(bus, message_id, expected or None, timeout=timeout).collect()
        return {
            "request_id": result.request_id,
            "outcome": result.outcome,
            "expected": result.expected,
            "answers": [vars(a) for _, a in sorted(result.answers.items())],
            "pending": result.pending,
            "closed": result.closed,
            "summary": result.summary(),
        }

    execute(config, run)


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
    execute(
        config,
        lambda bus: bus.discover_all(capability=capability, available_only=not include_offline),
    )


@app.command()
def watch(config: Path | None = typer.Option(None), after: int = 0):
    """Follow live Caura events; reconnects resume from the durable event cursor."""

    async def run():
        async with Bus(load_config(config)) as bus:
            async for event in bus.events(after=after):
                if event["event_type"] == RESYNC_EVENT:
                    typer.echo(
                        f"Caura: events before #{event['seq']} were removed by retention; "
                        "current inbox state was reloaded.",
                        err=True,
                    )
                typer.echo(json.dumps(event, ensure_ascii=False))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        typer.echo("Caura event watch stopped.", err=True)


@agents_app.command("list")
def agents_list(fleet: str | None = None, config: Path | None = typer.Option(None)):
    execute(config, lambda bus: bus.agents_all(fleet))


@threads_app.command("list")
def threads_list(config: Path | None = typer.Option(None)):
    execute(config, lambda bus: bus.threads())


@app.command()
def requests(
    state: str | None = None, limit: int = typer.Option(20, min=1, max=100), config: Path | None = None
):
    """List sent requests and retire their active notices."""
    if state not in {None, "awaiting", "overdue", "unanswered"}:
        raise typer.BadParameter("state must be awaiting, overdue or unanswered")
    execute(config, lambda bus: bus.requests(state=state, limit=limit))


if __name__ == "__main__":
    app()
