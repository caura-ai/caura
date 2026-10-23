"""Merge native runtime hooks without replacing unrelated project settings."""

import json
import shlex
import shutil
import sys
from pathlib import Path

# Background listener lifetime per arming. A Stop re-arms it after every turn,
# but an idle session has no turns: a short window left an open session deaf
# after one quiet night. The listener already exits with its Claude Code
# process, so the window only bounds a session left idle for a week.
DEFAULT_LISTEN_SECONDS = 7 * 24 * 3600
MAX_LISTEN_SECONDS = DEFAULT_LISTEN_SECONDS


def install_hooks(
    scope="project",
    project=None,
    config=None,
    state=None,
    listen_seconds=DEFAULT_LISTEN_SECONDS,
    idle_listen_seconds=None,
    shared=False,
    key_file=None,
):
    """Install the Claude Code wake hooks.

    SessionStart and Stop start a background ``asyncRewake`` listener: Claude
    Code does not wait for it, and when it exits 2 Claude starts a new turn
    with the wake text, also when the session is idle. UserPromptSubmit adds a
    quick check to every human turn. ``idle_listen_seconds`` is accepted for
    compatibility with older callers and ignored.
    """
    del idle_listen_seconds
    root = Path(project or Path.cwd()) if scope == "project" else Path.home()
    if shared and scope != "project":
        raise ValueError("--shared applies only to --scope project")
    filename = "settings.local.json" if scope == "project" and not shared else "settings.json"
    path = root / ".claude" / filename
    settings = load_settings(path)
    hooks = settings.setdefault("hooks", {})
    executable = shutil.which("caura-bus") or sibling_executable()
    if not executable:
        raise RuntimeError("caura-bus must be installed on PATH before installing hooks")
    base = [str(Path(executable).absolute()), "recv", "--brief"]
    if config:
        base.extend(["--config", str(Path(config).resolve())])
    if state:
        base.extend(["--state", str(Path(state).resolve())])
    if key_file:
        base.extend(["--key-file", str(Path(key_file).resolve())])
    listener = [*base, "--hook", "Rewake", "--wait", str(listen_seconds)]
    entries_by_event = {
        "SessionStart": {
            "type": "command",
            "command": shlex.join(listener),
            "asyncRewake": True,
            "timeout": listen_seconds + 60,
        },
        "Stop": {
            "type": "command",
            "command": shlex.join(listener),
            "asyncRewake": True,
            "timeout": listen_seconds + 60,
        },
        "UserPromptSubmit": {
            "type": "command",
            "command": shlex.join([*base, "--hook", "UserPromptSubmit"]),
            "timeout": 10,
        },
    }
    written = {}
    # Drop our entries everywhere first, so a reinstall also removes hooks an
    # older version installed under events it no longer uses.
    # Anything that is not shaped like a hook group belongs to the user or
    # another tool: leave it in place and do not look inside.
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            continue
        owned = False
        for group in entries:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            kept = [h for h in group["hooks"] if not owned_hook(h)]
            owned = owned or len(kept) != len(group["hooks"])
            group["hooks"] = kept
        if owned:
            entries[:] = [
                g
                for g in entries
                if not isinstance(g, dict) or not isinstance(g.get("hooks"), list) or g["hooks"]
            ]
            if not entries:
                del hooks[event]
    for event, hook in entries_by_event.items():
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f'"hooks.{event}" in {path} must be a JSON array; not modified')
        entry = {"hooks": [hook]}
        entries.append(entry)
        written[event] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return {"path": str(path), "hooks": written}


def load_settings(path):
    """Read a Claude Code settings file, or refuse one the merge could not keep intact.

    The file is the user's own configuration and may hold settings that are
    not ours, so a shape ``install_hooks`` cannot merge into is reported with
    its path and left exactly as it was, never rewritten. An empty file is the
    same as a missing one.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); not modified") from None
    if not isinstance(settings, dict):
        raise ValueError(f"{path} must hold a JSON object; not modified")
    if "hooks" in settings and not isinstance(settings["hooks"], dict):
        raise ValueError(f'"hooks" in {path} must be a JSON object; not modified')
    return settings


def sibling_executable():
    """caura-bus next to the running interpreter (a venv or pipx install not on PATH)."""
    candidate = Path(sys.executable).parent / "caura-bus"
    return str(candidate) if candidate.is_file() else None


def owned_hook(hook):
    """A hook entry this installer wrote: a command hook whose argv is ours."""
    return isinstance(hook, dict) and isinstance(hook.get("command"), str) and owned_command(hook["command"])


def owned_command(command):
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return (
        len(parts) >= 3
        and Path(parts[0]).name == "caura-bus"
        and parts[1:3] == ["recv", "--brief"]
        and "--hook" in parts
    )
