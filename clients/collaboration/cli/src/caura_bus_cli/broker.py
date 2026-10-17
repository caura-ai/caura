"""Prefer the Caura broker (`caura agent connect`) when it is installed.

The broker connects an agent with a browser sign-in, keeps the agent key in
the OS keychain and owns the runtime wiring, peer instructions and wake-ups.
`caura-bus join` stays for hosts without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .join import JoinError, default_name, detect_runtime, sanitize_name

BROKER = "caura"
THREAD_PLACEHOLDER = "<CODEX_THREAD_ID>"
BROKER_RUNTIMES = {"claude": "claude-code", "codex": "codex"}


@dataclass
class Broker:
    path: str
    # Flags `caura agent connect` accepts, e.g. {"agent", "runtime", "description"}.
    flags: set[str] = field(default_factory=set)


def _connect_flags(usage: str) -> set[str]:
    flags = set()
    for line in usage.splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            flags.add(stripped.lstrip("-").split()[0])
    return flags


def find_broker(
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Broker | None:
    """The `caura` broker on PATH, if it is one that has `agent connect`."""
    path = which(BROKER)
    if not path:
        return None
    try:
        probe = run(
            [path, "agent", "connect", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    flags = _connect_flags((probe.stdout or "") + (probe.stderr or ""))
    if not {"agent", "runtime", "project", "url"} <= flags:
        return None
    return Broker(path=path, flags=flags)


@dataclass
class BrokerPlan:
    argv: list[str]
    notes: list[str]
    runnable: bool


def connect_plan(
    broker: Broker,
    *,
    url: str,
    runtime: str,
    directory: Path,
    name: str | None,
    description: str | None,
    tenant: str | None,
    hooks: bool,
    memory: bool = True,
    env: Mapping[str, str] | None = None,
    ancestor: Callable[[], str | None] | None = None,
) -> BrokerPlan:
    """The `caura agent connect` command equivalent to these join options."""
    env = os.environ if env is None else env
    resolved = detect_runtime(runtime, env, ancestor) if ancestor else detect_runtime(runtime, env)
    directory = directory.expanduser().resolve()
    agent = sanitize_name(name) if name else ""
    if name and not agent:
        raise JoinError(f"--name {name!r} has no usable characters (use a-z, 0-9 and -)")
    agent = agent or default_name(resolved, directory)
    argv = [
        broker.path,
        "agent",
        "connect",
        "--runtime",
        BROKER_RUNTIMES[resolved],
        "--agent",
        agent,
        "--project",
        str(directory),
        "--url",
        url,
    ]
    notes: list[str] = []
    runnable = True
    if resolved == "codex":
        thread = env.get("CODEX_THREAD_ID", "")
        if not thread:
            thread, runnable = THREAD_PLACEHOLDER, False
            notes.append(
                "Codex needs its thread: run this inside the Codex session or replace the placeholder."
            )
        argv += ["--thread", thread]
    if description is not None:
        if "description" in broker.flags:
            argv += ["--description", description]
        else:
            notes.append("This broker cannot register a description; set it later with peer op=describe.")
    if tenant:
        notes.append("--tenant is not passed: the broker uses the workspace you approve in the browser.")
    if not hooks:
        notes.append("--no-hooks is not passed: the broker always installs wake-ups.")
    # A broker that knows --no-memory also registers the server's memory MCP.
    if "no-memory" in broker.flags:
        if not memory:
            argv.append("--no-memory")
    elif memory:
        notes.append(
            "This broker does not register the Caura memory MCP; upgrade it, or use "
            "`caura-bus join --no-broker` to get the memory server wired too."
        )
    return BrokerPlan(argv=argv, notes=notes, runnable=runnable)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
