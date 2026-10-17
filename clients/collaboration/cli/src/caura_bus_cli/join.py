"""`caura-bus join`: sign in as the human, mint an agent key, and run setup in one step.

The human signs in the way the dashboard does (email and password, giving a
session cookie and a CSRF token). The session is used only for this command and
signed out at the end. Nothing from it is stored, and the password is never
written anywhere. The new agent key goes through the normal ``setup`` path,
which verifies it against the server before writing anything.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import socket
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .setup import RUNTIME_NAMES, Options, SetupError, check_url, run_setup, shell_path, show

LABEL_PREFIX = "caura-bus join"
NAME_MAX = 64
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"


class JoinError(RuntimeError):
    """A step failed; the message is safe to print (no password, no key)."""


# --- runtime detection ------------------------------------------------------


def _ancestor_runtime(pid: int | None = None, depth: int = 25) -> str | None:
    """The nearest ancestor process that is Claude Code or Codex, if any."""
    pid = pid or os.getppid()
    for _ in range(depth):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, check=False
            ).stdout.strip()
        except OSError:
            return None
        if not out:
            return None
        parent, _, command = out.partition(" ")
        name = Path(command.strip()).name.lower()
        if name.startswith("codex"):
            return "codex"
        if name.startswith("claude"):
            return "claude"
        try:
            pid = int(parent)
        except ValueError:
            return None
    return None


def detect_runtime(
    requested: str,
    env: Mapping[str, str] | None = None,
    ancestor: Callable[[], str | None] = _ancestor_runtime,
) -> str:
    """Resolve --runtime. ``auto`` reads the markers each runtime sets in its shell tool.

    Claude Code sets ``CLAUDECODE=1``. Codex sets ``CODEX_THREAD_ID``,
    ``CODEX_SESSION_ID`` and ``CODEX_SANDBOX`` in the commands it runs. Both
    are inherited by child processes, so when both are present (one runtime
    started from inside the other) the nearest ancestor process decides.
    """
    requested = (requested or "auto").strip().lower()
    if requested in {"claude", "claude-code"}:
        return "claude"
    if requested == "codex":
        return "codex"
    if requested != "auto":
        raise JoinError("--runtime must be auto, claude or codex")
    env = os.environ if env is None else env
    claude = env.get("CLAUDECODE") == "1"
    codex = any(env.get(name) for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX"))
    if claude and codex:
        nearest = ancestor()
        if nearest:
            return nearest
        raise JoinError(
            "both Claude Code and Codex markers are set in this environment; pass --runtime claude or codex"
        )
    if claude:
        return "claude"
    if codex:
        return "codex"
    raise JoinError(
        "could not detect the runtime: run this from inside a Claude Code or Codex session, "
        "or pass --runtime claude or --runtime codex"
    )


# --- naming -----------------------------------------------------------------


def sanitize_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower())
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:NAME_MAX].strip("-")


def default_name(runtime: str, directory: Path) -> str:
    stem = sanitize_name(directory.name)[:24].strip("-") or "agent"
    return sanitize_name(f"{runtime}-{stem}-{secrets.token_hex(2)}")


def dir_tag(directory: Path) -> str:
    """A stable, non-reversible tag for this directory on this machine."""
    return hashlib.sha256(f"{socket.gethostname()}:{directory}".encode()).hexdigest()[:12]


def make_label(runtime: str, directory: Path, tag: str) -> str:
    host = socket.gethostname().split(".")[0][:40] or "host"
    label = f"{LABEL_PREFIX}: {runtime} in {directory.name or '/'} on {host}"
    suffix = f" [dir {tag}]"
    return label[: 200 - len(suffix)] + suffix


# --- dashboard session ------------------------------------------------------


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list | dict):
        detail = str(detail)[:300]
    return f"{detail} (HTTP {response.status_code})" if detail else f"HTTP {response.status_code}"


class Session:
    """The human's dashboard session for the length of one command."""

    def __init__(self, url: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=url, timeout=30, follow_redirects=False, transport=transport)
        self.headers: dict[str, str] = {}
        self.org_id = ""
        self.user_id = ""
        self.tenant_id = ""

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise JoinError(f"cannot reach {self.http.base_url}: {type(exc).__name__}") from None

    def login(self, email: str, password: str, totp: Callable[[], str] | None = None) -> dict:
        payload: dict[str, str] = {"email": email, "password": password}
        response = self._call("POST", "/api/auth/user/login", json=payload)
        body = self._login_body(response)
        if body.get("totp_required"):
            if totp is None:
                raise JoinError(
                    "this account uses two-factor sign-in; set CAURA_TOTP_CODE or run in a terminal"
                )
            payload["totp_code"] = totp()
            body = self._login_body(self._call("POST", "/api/auth/user/login", json=payload))
        if not body.get("ok"):
            raise JoinError("sign-in did not complete")
        if body.get("must_change_password"):
            raise JoinError("this account must change its password first; sign in to the dashboard once")
        csrf = self.http.cookies.get(CSRF_COOKIE)
        if not csrf or not body.get("org_id") or not body.get("user_id"):
            raise JoinError("the server did not return a dashboard session; is --url a Caura gateway?")
        self.headers = {CSRF_HEADER: csrf}
        self.org_id, self.user_id = str(body["org_id"]), str(body["user_id"])
        self.tenant_id = str(body.get("tenant_id") or "")
        return body

    @staticmethod
    def _login_body(response: httpx.Response) -> dict:
        if response.status_code == 401:
            raise JoinError("sign-in failed: wrong email, password or code")
        if response.status_code == 429:
            raise JoinError("sign-in refused: the account is locked or rate limited; try again later")
        if response.status_code == 404:
            raise JoinError("no sign-in endpoint at this URL; is --url a Caura gateway?")
        if response.status_code != 200:
            raise JoinError(f"sign-in failed: {_detail(response)}")
        try:
            body = response.json()
        except ValueError:
            raise JoinError("sign-in returned a non-JSON response; is --url a Caura gateway?") from None
        return body if isinstance(body, dict) else {}

    @property
    def credentials_path(self) -> str:
        return f"/api/v1/admin/orgs/{self.org_id}/api-credentials"

    def tenant_options(self) -> list[str] | None:
        response = self._call("GET", f"{self.credentials_path}/tenant-options")
        if response.status_code != 200:
            return None
        return [str(item.get("tenant_id")) for item in response.json() if isinstance(item, dict)]

    def agent_keys(self, tenant_id: str) -> list[dict]:
        response = self._call(
            "GET",
            self.credentials_path,
            params={"kind": "agent_key", "home_tenant_id": tenant_id, "include_revoked": "true"},
        )
        if response.status_code == 403:
            raise JoinError(
                "your account cannot manage API credentials in this organization "
                "(needs the org.api_credentials.manage permission); ask an org admin"
            )
        if response.status_code != 200:
            raise JoinError(f"listing agent keys failed: {_detail(response)}")
        return [row for row in response.json().get("items", []) if row.get("home_tenant_id") == tenant_id]

    def mint(self, tenant_id: str, agent_id: str, label: str) -> dict:
        response = self._call(
            "POST",
            self.credentials_path,
            headers=self.headers,
            json={
                "kind": "agent_key",
                "home_tenant_id": tenant_id,
                "home_agent_id": agent_id,
                "capabilities": ["read", "write"],
                "label": label,
            },
        )
        if response.status_code != 201:
            raise JoinError(f"minting the agent key failed: {_detail(response)}")
        body = response.json()
        if not body.get("raw_key") or not body.get("id"):
            raise JoinError("the server did not return the new agent key")
        return body

    def revoke(self, credential_id: str) -> bool:
        response = self._call("DELETE", f"{self.credentials_path}/{credential_id}", headers=self.headers)
        return response.status_code in {200, 204}

    def close(self) -> None:
        try:
            if self.headers:
                self.http.post("/api/auth/user/logout", headers=self.headers)
        except httpx.HTTPError:
            pass
        finally:
            self.http.close()


# --- orchestration ----------------------------------------------------------


@dataclass
class JoinOptions:
    url: str
    email: str
    password: str = field(repr=False)
    directory: Path
    runtime: str = "auto"
    name: str | None = None
    description: str | None = None
    tenant: str | None = None
    hooks: bool = True
    memory: bool = True
    totp: Callable[[], str] | None = field(default=None, repr=False)
    env: Mapping[str, str] | None = field(default=None, repr=False)
    ancestor: Callable[[], str | None] = field(default=_ancestor_runtime, repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    setup_transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    codex_config: Path | None = None


def _active(row: dict) -> bool:
    return not row.get("revoked_at")


def choose_name(
    options: JoinOptions, runtime: str, directory: Path, rows: list[dict], user_id: str, tag: str
):
    """Return (agent name, how it was chosen). Refuse a name another person's keys own."""
    mine_here = [
        row
        for row in rows
        if _active(row)
        and str(row.get("created_by")) == user_id
        and str(row.get("label", "")).startswith(f"{LABEL_PREFIX}: {runtime} ")
        and str(row.get("label", "")).endswith(f"[dir {tag}]")
    ]
    if options.name:
        name = sanitize_name(options.name)
        if not name:
            raise JoinError(f"--name {options.name!r} has no usable characters (use a-z, 0-9 and -)")
        owners = {str(row.get("created_by")) for row in rows if row.get("home_agent_id") == name}
        if owners and user_id not in owners:
            raise JoinError(
                f"agent {name!r} already exists in this workspace and was created by someone else; "
                "pick another --name"
            )
        return name, "rejoin" if owners else "new"
    if mine_here:
        latest = max(mine_here, key=lambda row: str(row.get("created_at", "")))
        return str(latest["home_agent_id"]), "rejoin"
    taken = {row.get("home_agent_id") for row in rows}
    for _ in range(8):
        name = default_name(runtime, directory)
        if name not in taken:
            return name, "new"
    raise JoinError("could not pick a free agent name; pass --name")


def run_join(options: JoinOptions, echo: Callable[[str], None], run_async) -> int:
    url, _ = check_url(options.url)
    runtime = detect_runtime(options.runtime, options.env, options.ancestor)
    directory = options.directory.expanduser().resolve()
    if not directory.is_dir():
        raise JoinError(f"--dir {directory} is not a directory")
    if not options.email.strip():
        raise JoinError("an email is required (--email or CAURA_EMAIL)")
    if not options.password:
        raise JoinError("a password is required (CAURA_PASSWORD or the prompt)")

    echo(f"Joining {RUNTIME_NAMES[runtime]} in {show(directory)} to Caura at {url}")
    session = Session(url, options.transport)
    try:
        session.login(options.email.strip(), options.password, options.totp)
        echo(f"  [ok] signed in as {options.email.strip()}")
        tenant = (options.tenant or session.tenant_id).strip()
        if not tenant:
            raise JoinError("the sign-in did not name a workspace; pass --tenant")
        if options.tenant:
            choices = session.tenant_options()
            if choices is not None and tenant not in choices:
                raise JoinError(
                    f"workspace {tenant!r} is not in your organization; choices: {', '.join(choices)}"
                )
        echo(f"  [ok] workspace {tenant}" + ("" if options.tenant else " (your landing workspace)"))

        tag = dir_tag(directory)
        rows = session.agent_keys(tenant)
        name, how = choose_name(options, runtime, directory, rows, session.user_id, tag)
        if options.name and name != options.name:
            echo(f"  [..] agent name {options.name!r} sanitized to {name!r}")
        replace = [
            row
            for row in rows
            if _active(row)
            and row.get("home_agent_id") == name
            and str(row.get("created_by")) == session.user_id
            and str(row.get("label", "")).startswith(LABEL_PREFIX)
            and str(row.get("label", "")).endswith(f"[dir {tag}]")
        ]
        others = [
            row for row in rows if _active(row) and row.get("home_agent_id") == name and row not in replace
        ]

        credential = session.mint(tenant, name, make_label(runtime, directory, tag))
        key = credential["raw_key"]
        verb = "re-joined (new key)" if how == "rejoin" else "created"
        echo(f"  [ok] agent {name} {verb}: key {credential.get('key_prefix', 'mc_')}… minted")

        setup_options = Options(
            runtime=runtime,
            url=url,
            key=key,
            directory=directory,
            agent_id=name,
            description=options.description,
            hooks=options.hooks,
            memory=options.memory,
            codex_config=options.codex_config,
            transport=options.setup_transport,
            next_steps=False,
        )
        try:
            code = run_setup(setup_options, echo, run_async)
        except (SetupError, ValueError, OSError) as exc:
            code, failure = 1, str(exc).replace(key, "****")
        else:
            failure = "" if code == 0 else "setup did not finish"
        if code != 0:
            revoked = session.revoke(str(credential["id"]))
            raise JoinError(
                f"{failure}; the new key was {'revoked' if revoked else 'NOT revoked (revoke it in API Credentials)'}"
                ". Fix the problem above and run join again."
            )

        for row in replace:
            done = session.revoke(str(row["id"]))
            echo(
                f"  [{'ok' if done else '!!'}] previous join key {row.get('key_prefix', '')}… for this directory "
                + ("revoked" if done else "could NOT be revoked; revoke it in API Credentials")
            )
        if others:
            echo(
                f"  [..] left {len(others)} other active key(s) for {name} untouched "
                "(not minted by join for this directory)"
            )
    finally:
        session.close()

    echo("")
    echo(f"Agent {name} is registered in workspace {tenant}.")
    echo("Next steps:")
    if runtime == "claude":
        loaded = "caura-bus and the caura memory server" if options.memory else "caura-bus"
        echo(f"  1. Restart this Claude Code session (or run /mcp) to load {loaded}.")
        echo("     An MCP server added while a session is running is not loaded until the session restarts.")
        if options.hooks:
            echo("     Wake-up hooks are read at startup too, so a restart is best.")
    else:
        echo("  1. Restart Codex in this directory: MCP servers load only when Codex starts.")
        echo(f"       cd {shell_path(directory)} && codex")
        if setup_options.waker:
            echo("     Send it one message, then keep this running in another terminal for wake-ups:")
            echo(f"       {setup_options.waker}")
    echo('  2. Try: "Use the caura-bus peer tool: discover available peers and tell me who they are."')
    if options.memory:
        listing = "claude mcp list" if runtime == "claude" else "codex mcp list"
        echo(f"     `{listing}` should show this server's memory MCP at {url}/mcp/.")
    if setup_options.config_path:
        config_path = setup_options.config_path
        key_hint = (
            f"--key-file {shell_path(config_path.with_suffix('.key'))}"
            if options.hooks
            else "(export CAURA_API_KEY first)"
        )
        echo(f"  3. Check the connection: caura-bus doctor --config {shell_path(config_path)} {key_hint}")
    return 0
