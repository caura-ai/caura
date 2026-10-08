"""Merge native runtime hooks without replacing unrelated project settings."""

import json
import shlex
import shutil
import sys
from pathlib import Path

# Background listener lifetime per arming. A Stop re-arms it after every turn.
DEFAULT_LISTEN_SECONDS = 43200


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
    settings = json.loads(path.read_text()) if path.exists() else {}
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
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            continue
        owned = False
        for group in entries:
            kept = [h for h in group.get("hooks", []) if not owned_command(h.get("command", ""))]
            owned = owned or len(kept) != len(group.get("hooks", []))
            group["hooks"] = kept
        if owned:
            entries[:] = [group for group in entries if group.get("hooks")]
            if not entries:
                del hooks[event]
    for event, hook in entries_by_event.items():
        entry = {"hooks": [hook]}
        hooks.setdefault(event, []).append(entry)
        written[event] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n")
    temporary.replace(path)
    return {"path": str(path), "hooks": written}


def sibling_executable():
    """caura-bus next to the running interpreter (a venv or pipx install not on PATH)."""
    candidate = Path(sys.executable).parent / "caura-bus"
    return str(candidate) if candidate.is_file() else None


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
