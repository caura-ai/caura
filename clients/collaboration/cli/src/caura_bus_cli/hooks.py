"""Merge native runtime hooks without replacing unrelated project settings."""

import json
import shlex
import shutil
from pathlib import Path


def install_hooks(
    scope="project",
    project=None,
    config=None,
    state=None,
    listen_seconds=600,
    idle_listen_seconds=5,
    shared=False,
):
    root = Path(project or Path.cwd()) if scope == "project" else Path.home()
    if shared and scope != "project":
        raise ValueError("--shared applies only to --scope project")
    filename = "settings.local.json" if scope == "project" and not shared else "settings.json"
    path = root / ".claude" / filename
    settings = json.loads(path.read_text()) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    executable = shutil.which("caura-bus")
    if not executable:
        raise RuntimeError("caura-bus must be installed on PATH before installing hooks")
    base = [str(Path(executable).absolute()), "recv", "--brief"]
    if config:
        base.extend(["--config", str(Path(config).resolve())])
    if state:
        base.extend(["--state", str(Path(state).resolve())])
    written = {}
    for event in ("Stop", "UserPromptSubmit"):
        args = [*base, "--hook", event]
        if event == "Stop":
            args.extend(["--wait", str(listen_seconds)])
            args.extend(["--idle-listen-seconds", str(idle_listen_seconds)])
        command = shlex.join(args)
        entries = hooks.setdefault(event, [])
        timeout = listen_seconds + 10 if event == "Stop" else 10
        entry = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        # Preserve unrelated hooks, replacing only entries installed by us.
        for group in entries:
            group["hooks"] = [h for h in group.get("hooks", []) if not owned_command(h.get("command", ""))]
        entries[:] = [group for group in entries if group.get("hooks")]
        entries.append(entry)
        written[event] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n")
    temporary.replace(path)
    return {"path": str(path), "hooks": written}


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
