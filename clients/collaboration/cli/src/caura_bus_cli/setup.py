"""`caura-bus setup`: connect a Claude Code or Codex runtime to Caura in one step.

Everything identity-related comes from the server: the key is verified first and
the effective agent and tenant are read back from it. Each step only rewrites
what setup owns (its agent config, the ``caura-bus`` MCP entry, and a marked
instruction block), so re-running updates in place.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from caura_bus_core import AgentConfig, Bus, PlatformError, load_config

SERVER_NAME = "caura-bus"
MARK_START = "<!-- caura-bus:peer-instructions:start"
MARK_END = "<!-- caura-bus:peer-instructions:end -->"
MARK_HEADER = MARK_START + " (managed by `caura-bus setup`; re-running setup replaces this block) -->"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RUNTIMES = {"claude": "claude", "claude-code": "claude", "codex": "codex"}
INSTRUCTION_FILES = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}
RUNTIME_NAMES = {"claude": "Claude Code", "codex": "Codex"}
CODEX_HEADER = re.compile(r'^\[\s*mcp_servers\s*\.\s*(?:"caura-bus"|caura-bus)\s*\]\s*(?:#.*)?$', re.M)
NEXT_HEADER = re.compile(r"^[ \t]*\[", re.M)
OWNED_KEYS = re.compile(r"^[ \t]*(?:command|args|env)[ \t]*=")


class SetupError(RuntimeError):
    """A step failed; the message is safe to print (keys are masked)."""


def mask(key: str) -> str:
    key = key.strip()
    return f"{key[:6]}…{key[-4:]}" if len(key) >= 16 else "****"


def scrub(text: str, key: str) -> str:
    return text.replace(key, mask(key)) if key else text


def show(path: Path | str) -> str:
    """Shorten a path under HOME to ~/... for display (never for commands with VAR=path)."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def shell_path(path: Path) -> str:
    """A shell word for ``path``, keeping a leading ~/ expandable."""
    shown = show(path)
    return "~/" + shlex.quote(shown[2:]) if shown.startswith("~/") else shlex.quote(shown)


def toml_str(value: str) -> str:
    # JSON string escapes (\" \\ \n \uXXXX ...) are valid TOML basic-string escapes.
    return json.dumps(value)


def check_url(url: str) -> tuple[str, bool]:
    """Return the normalised origin and whether plain HTTP is allowed (localhost only)."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise SetupError(f"--url must be an http(s) origin such as https://caura.example, got {url!r}")
    if parts.path not in {"", "/"} or parts.query or parts.fragment or parts.username or parts.password:
        raise SetupError(
            "--url must be the gateway origin only (no path such as /api/v1, query or credentials)"
        )
    insecure = parts.scheme == "http"
    if insecure and parts.hostname not in LOCAL_HOSTS:
        raise SetupError("plain http is allowed only for localhost; use https://")
    return f"{parts.scheme}://{parts.netloc}", insecure


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value).strip(".") or "_"


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(base) if base else Path.home() / ".config"


def default_config_path(url: str, tenant_id: str, agent_id: str) -> Path:
    netloc = urlsplit(url).netloc
    return config_home() / "caura-bus" / "agents" / slug(netloc) / slug(tenant_id) / f"{slug(agent_id)}.toml"


def codex_config_path() -> Path:
    base = os.environ.get("CODEX_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".codex") / "config.toml"


def find_mcp_binary() -> Path | None:
    """Prefer the caura-bus-mcp installed next to this CLI (same version), then PATH."""
    sibling = Path(sys.executable).parent / "caura-bus-mcp"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return sibling.absolute()
    found = shutil.which("caura-bus-mcp")
    return Path(found).absolute() if found else None


def peer_template() -> str:
    return files("caura_bus_cli").joinpath("peer_instructions.md").read_text()


# --- 1. verify the key ----------------------------------------------------


async def verify(url: str, insecure: bool, key: str, transport: httpx.AsyncBaseTransport | None = None):
    """Read the effective identity, then re-check it the way `doctor` does."""
    probe = AgentConfig(
        api_url=url,
        allow_insecure_http=insecure,
        agent={"agent_id": "unverified", "tenant_id": "unverified"},
    )
    bus = Bus(probe, api_key=key, transport=transport)
    try:
        identity = await bus.request("GET", "identity")
    finally:
        await bus.close()
    if not isinstance(identity, dict) or not identity.get("agent_id") or not identity.get("tenant_id"):
        raise SetupError("the server did not return an agent identity; is --url a Caura gateway?")
    return identity


async def describe(config: AgentConfig, key: str, text: str, transport=None) -> dict:
    async with Bus(config, api_key=key, transport=transport) as bus:
        return await bus.describe(text)


# --- 2. agent config ------------------------------------------------------


def render_config(url: str, insecure: bool, identity: dict, existing: Path) -> str:
    peers: list[str] = ["*"]
    consultation: dict[str, Any] = {}
    if existing.is_file():
        try:
            old = tomllib.loads(existing.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            old = {}
        if isinstance(old.get("peers"), list):
            peers = [str(p) for p in old["peers"]]
        if isinstance(old.get("consultation"), dict):
            consultation = old["consultation"]
    lines = [
        "# Managed by `caura-bus setup`; re-run it to update. The API key is not stored here.",
        f"api_url = {toml_str(url)}",
    ]
    if insecure:
        lines.append("allow_insecure_http = true")
    lines.append("peers = [" + ", ".join(toml_str(p) for p in peers) + "]")
    lines += [
        "",
        "[agent]",
        f"agent_id = {toml_str(identity['agent_id'])}",
        f"tenant_id = {toml_str(identity['tenant_id'])}",
    ]
    if identity.get("fleet_id"):
        lines.append(f"fleet_id = {toml_str(str(identity['fleet_id']))}")
    if consultation:
        lines += ["", "[consultation]"]
        for name in ("max_requests", "deadline_seconds"):
            if isinstance(consultation.get(name), int | float) and not isinstance(
                consultation.get(name), bool
            ):
                lines.append(f"{name} = {consultation[name]}")
    return "\n".join(lines) + "\n"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


# --- 3. runtime wiring ----------------------------------------------------


def claude_add_argv(claude: str, key: str, config: Path, mcp: Path) -> list[str]:
    return [
        claude,
        "mcp",
        "add",
        "--scope",
        "local",
        SERVER_NAME,
        "-e",
        f"CAURA_API_KEY={key}",
        "-e",
        f"CAURA_BUS_AGENT_CONFIG={config}",
        "--",
        str(mcp),
    ]


def claude_manual_command(directory: Path, config: Path, mcp: Path) -> str:
    """The exact command to run by hand; the key comes from the user's environment."""
    argv = claude_add_argv("claude", "KEYPLACEHOLDER", config, mcp)
    command = shlex.join(argv).replace(
        "CAURA_API_KEY=KEYPLACEHOLDER", '"CAURA_API_KEY=${CAURA_API_KEY:?export your agent key first}"'
    )
    return f"cd {shlex.quote(str(directory))} && {command}"


def wire_claude(directory: Path, key: str, config: Path, mcp: Path) -> str:
    claude = shutil.which("claude")
    if not claude:
        raise SetupError(
            "`claude` is not on PATH. Run this yourself (with CAURA_API_KEY exported):\n  "
            + claude_manual_command(directory, config, mcp)
        )
    # Replace a previous entry so re-running updates the key and paths in place.
    subprocess.run(
        [claude, "mcp", "remove", "--scope", "local", SERVER_NAME],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        claude_add_argv(claude, key, config, mcp),
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = scrub((result.stderr or result.stdout).strip(), key)
        raise SetupError(f"`claude mcp add` failed ({result.returncode}): {detail}")
    return f"claude mcp add --scope local {SERVER_NAME} (project {show(directory)})"


def codex_block(key: str, config: Path, mcp: Path) -> list[str]:
    return [
        f"command = {toml_str(str(mcp))}",
        "env = { CAURA_API_KEY = "
        + toml_str(key)
        + ", CAURA_BUS_AGENT_CONFIG = "
        + toml_str(str(config))
        + " }",
    ]


def without_ours(data: dict) -> dict:
    data = json.loads(json.dumps(data, default=str))
    entry = data.get("mcp_servers", {}).get(SERVER_NAME)
    if isinstance(entry, dict):
        for name in ("command", "args", "env"):
            entry.pop(name, None)
        if not entry:
            data["mcp_servers"].pop(SERVER_NAME)
        if not data["mcp_servers"]:
            data.pop("mcp_servers")
    return data


def update_codex_text(text: str, key: str, config: Path, mcp: Path) -> str:
    """Add or update only [mcp_servers.caura-bus]; refuse anything that would change more."""
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"existing Codex config is not valid TOML ({exc}); not modified") from None
    ours = codex_block(key, config, mcp)
    match = CODEX_HEADER.search(text)
    if match is None:
        if SERVER_NAME in before.get("mcp_servers", {}):
            raise SetupError(
                f"Codex config defines mcp_servers.{SERVER_NAME} without a [mcp_servers.{SERVER_NAME}] "
                "table header; edit it by hand or move it into that table, then re-run setup"
            )
        updated = (text.rstrip("\n") + "\n\n" if text.strip() else "") + "\n".join(
            [f"[mcp_servers.{SERVER_NAME}]", *ours]
        )
        updated += "\n"
    else:
        start = match.end() + 1 if match.end() < len(text) else match.end()
        following = NEXT_HEADER.search(text, start)
        end = following.start() if following else len(text)
        kept = [line for line in text[start:end].splitlines() if not OWNED_KEYS.match(line)]
        body = "\n".join([*ours, *kept]).rstrip("\n") + "\n"
        if following:
            body += "\n"
        prefix = text[:start] if text[:start].endswith("\n") else text[:start] + "\n"
        updated = prefix + body + text[end:].lstrip("\n")
    try:
        after = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(
            f"updating [mcp_servers.{SERVER_NAME}] would produce invalid TOML ({exc}); "
            "the table probably uses multi-line command/args/env values. Not modified."
        ) from None
    entry = after.get("mcp_servers", {}).get(SERVER_NAME, {})
    expected_env = {"CAURA_API_KEY": key, "CAURA_BUS_AGENT_CONFIG": str(config)}
    if entry.get("command") != str(mcp) or entry.get("env") != expected_env or "args" in entry:
        raise SetupError(f"could not verify the updated [mcp_servers.{SERVER_NAME}] table; not modified")
    if without_ours(before) != without_ours(after):
        raise SetupError("updating the Codex config would change other settings; not modified")
    return updated


def wire_codex(key: str, config: Path, mcp: Path, path: Path | None = None) -> str:
    path = path or codex_config_path()
    text = path.read_text() if path.exists() else ""
    updated = update_codex_text(text, key, config, mcp)
    if updated == text:
        return f"[mcp_servers.{SERVER_NAME}] already current in {show(path)}"
    if path.exists():
        backup = path.with_name(path.name + ".caura-bus-setup.bak")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    write_private(path, updated)  # it now holds an agent key
    return f"[mcp_servers.{SERVER_NAME}] written to {show(path)}" + (
        f" (backup {path.name}.caura-bus-setup.bak)" if text else ""
    )


# --- 4. peer instructions -------------------------------------------------


def instruction_block() -> str:
    return f"{MARK_HEADER}\n{peer_template().rstrip()}\n{MARK_END}\n"


def merge_instructions(existing: str | None) -> tuple[str, str]:
    block = instruction_block()
    if existing is None:
        return block, "created"
    start = existing.find(MARK_START)
    if start != -1:
        end = existing.find(MARK_END, start)
        if end == -1:
            raise SetupError(f"found the {MARK_START} marker without its end marker; fix the file by hand")
        end += len(MARK_END)
        if existing[end : end + 1] == "\n":
            end += 1
        updated = existing[:start] + block + existing[end:]
        return updated, "unchanged" if updated == existing else "updated"
    if not existing or existing.endswith("\n\n"):
        separator = ""
    else:
        separator = "\n" if existing.endswith("\n") else "\n\n"
    return existing + separator + block, "appended"


def write_instructions(path: Path) -> str:
    existing = path.read_text() if path.exists() else None
    updated, action = merge_instructions(existing)
    if action != "unchanged":
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(updated)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        temporary.replace(path)
    return action


# --- 5. wake-ups (opt-in) -----------------------------------------------


def key_file_path(config: Path) -> Path:
    return config.with_suffix(".key")


def codex_waker_command(directory: Path, config: Path, key_file: Path) -> str:
    return shlex.join(
        [
            "caura-bus",
            "wake",
            "--runtime",
            "codex",
            "--thread",
            "latest",
            "--dir",
            str(directory),
            "--config",
            str(config),
            "--key-file",
            str(key_file),
        ]
    )


def install_wake(options, runtime, directory, config_path, key, echo) -> str | None:
    """Store the key for hooks/wakers (0600) and install Claude hooks or print the Codex waker.

    Hooks and wakers are started by the runtime, not by the shell that holds
    CAURA_API_KEY, so they read the key from a private file next to the config.
    """
    from .hooks import install_hooks

    key_file = key_file_path(config_path)
    prefix = "[dry-run] would " if options.dry_run else ""
    if options.dry_run:
        echo(f"  {prefix}write the agent key to {show(key_file)} (mode 600) for wake-ups")
    else:
        write_private(key_file, key + "\n")
        echo(f"  [ok] agent key for wake-ups {show(key_file)} (mode 600)")
    if runtime == "codex":
        command = codex_waker_command(directory, config_path, key_file)
        echo(f"  {prefix if options.dry_run else '[ok] '}Codex wake-ups: run `{command}` while Codex is open")
        return command
    if options.dry_run:
        echo(f"  {prefix}install Claude Code wake hooks in {show(directory / '.claude/settings.local.json')}")
        return None
    try:
        result = install_hooks(project=directory, config=config_path, key_file=key_file)
    except (RuntimeError, ValueError, OSError) as exc:
        echo(f"  [!!] wake hooks not installed: {exc}")
        return None
    echo(f"  [ok] wake hooks {show(result['path'])} (SessionStart/Stop listener, UserPromptSubmit check)")
    return None


# --- orchestration --------------------------------------------------------


@dataclass
class Options:
    runtime: str
    url: str
    key: str
    directory: Path
    agent_id: str | None = None
    description: str | None = None
    config: Path | None = None
    dry_run: bool = False
    hooks: bool = False
    codex_config: Path | None = None
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)


def run_setup(options: Options, echo: Callable[[str], None], run_async) -> int:
    runtime = RUNTIMES.get(options.runtime)
    if runtime is None:
        raise SetupError("--runtime must be claude or codex")
    key = options.key.strip()
    if not key:
        raise SetupError("an agent key is required (--key or CAURA_API_KEY)")
    url, insecure = check_url(options.url)
    directory = options.directory.expanduser().resolve()
    if not directory.is_dir():
        raise SetupError(f"--dir {directory} is not a directory")
    prefix = "[dry-run] would " if options.dry_run else ""
    echo(f"Connecting {RUNTIME_NAMES[runtime]} in {show(directory)} to Caura at {url}")

    try:
        identity = run_async(verify(url, insecure, key, options.transport))
    except PlatformError as exc:
        if exc.status in {401, 403}:
            raise SetupError(
                f"the server rejected key {mask(key)} ({exc.status}); check it and --url"
            ) from None
        raise SetupError(scrub(str(exc), key)) from None
    except httpx.HTTPError as exc:
        raise SetupError(f"cannot reach {url}: {scrub(str(exc), key) or type(exc).__name__}") from None
    agent_id, tenant_id = identity["agent_id"], identity["tenant_id"]
    if options.agent_id and options.agent_id != agent_id:
        raise SetupError(
            f"key {mask(key)} belongs to agent {agent_id!r}, not {options.agent_id!r}; nothing was changed"
        )
    echo(f"  [ok] key {mask(key)} verified: agent {agent_id}, tenant {tenant_id}")

    config_path = (options.config or default_config_path(url, tenant_id, agent_id)).expanduser().absolute()
    text = render_config(url, insecure, identity, config_path)
    AgentConfig.model_validate(tomllib.loads(text))
    unchanged = config_path.is_file() and config_path.read_text() == text
    if options.dry_run:
        echo(f"  {prefix}write agent config {show(config_path)} (mode 600)")
    else:
        if not unchanged:
            write_private(config_path, text)
        os.chmod(config_path, 0o600)
        load_config(config_path)
        echo(f"  [ok] agent config {show(config_path)} (mode 600{', unchanged' if unchanged else ''})")

    mcp = find_mcp_binary()
    if mcp is None:
        raise SetupError(
            "caura-bus-mcp is not installed next to caura-bus or on PATH; install it "
            "(it ships with caura-bus-cli) and re-run setup"
        )
    if runtime == "claude":
        if options.dry_run:
            echo(f"  {prefix}run in {directory}:")
            echo("      " + shlex.join(claude_add_argv("claude", mask(key), config_path, mcp)))
        else:
            try:
                echo("  [ok] MCP server " + wire_claude(directory, key, config_path, mcp))
            except SetupError as exc:
                echo(f"  [!!] {exc}")
                return finish(options, runtime, directory, config_path, echo, run_async, key, failed=True)
    else:
        codex_path = (options.codex_config or codex_config_path()).expanduser()
        if options.dry_run:
            update_codex_text(codex_path.read_text() if codex_path.exists() else "", key, config_path, mcp)
            echo(
                f"  {prefix}add/update [mcp_servers.{SERVER_NAME}] in {show(codex_path)} (backup kept, mode 600)"
            )
            echo(
                f"      command = {mcp}; env CAURA_API_KEY={mask(key)}, CAURA_BUS_AGENT_CONFIG={config_path}"
            )
        else:
            echo("  [ok] MCP server " + wire_codex(key, config_path, mcp, codex_path))
    return finish(options, runtime, directory, config_path, echo, run_async, key, failed=False)


def finish(options, runtime, directory, config_path, echo, run_async, key, *, failed) -> int:
    prefix = "[dry-run] would " if options.dry_run else ""
    instructions = directory / INSTRUCTION_FILES[runtime]
    if options.dry_run:
        _, action = merge_instructions(instructions.read_text() if instructions.exists() else None)
        verb = {"created": "create", "appended": "append to", "updated": "update", "unchanged": "keep"}[
            action
        ]
        echo(f"  {prefix}{verb} peer instructions in {show(instructions)}")
    else:
        echo(f"  [ok] peer instructions {show(instructions)} ({write_instructions(instructions)})")

    if options.description is not None:
        if options.dry_run:
            echo(f"  {prefix}register description ({len(options.description)} chars)")
        else:
            try:
                run_async(describe(load_config(config_path), key, options.description, options.transport))
                echo("  [ok] description registered")
            except PlatformError as exc:
                note = "this server does not support descriptions yet" if exc.status == 404 else str(exc)
                echo(f"  [!!] description not registered: {scrub(note, key)}")
            except ValueError as exc:
                echo(f"  [!!] description not registered: {exc}")

    waker = None
    if options.hooks:
        waker = install_wake(options, runtime, directory, config_path, key, echo)

    if options.dry_run:
        echo("Dry run: nothing was written.")
        return 0
    start = f"cd {shell_path(directory)} && {'claude' if runtime == 'claude' else 'codex'}"
    echo("")
    echo("Next steps:" if not failed else "Finish the MCP step above, then:")
    if runtime == "claude":
        echo(f"  1. Start Claude Code in the project:  {start}")
    else:
        echo(f"  1. Start (or restart) Codex in the project:  {start}")
    if waker:
        echo(f"     In another terminal, keep the wake listener running:  {waker}")
    echo('  2. Try: "Use the caura-bus peer tool: discover available peers and tell me who they are."')
    echo(
        "  3. Check the connection any time:  CAURA_API_KEY=<your key> caura-bus doctor "
        f"--config {shell_path(config_path)}"
    )
    return 1 if failed else 0
