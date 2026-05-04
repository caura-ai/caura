"""Guards the two hardcoded plugin-file lists in core_api.routes.plugin.

A 2026-04-16 refactor added plugin/src/paths.ts + logger.ts but forgot to
register them in either the Python allow-list (`_plugin_files`) or the
bash `for srcfile in …` loop inside the install-script template. Every
fresh `curl … | bash` install broke with `TS2307: Cannot find module
'./paths.js'` until both lists were fixed on 2026-04-19.

This test keeps them in lockstep with `plugin/src/*.ts`.
"""

from __future__ import annotations

import re
from pathlib import Path


from core_api.routes import plugin as plugin_mod


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SRC = REPO_ROOT / "plugin" / "src"


def _expected_source_files() -> set[str]:
    """Every .ts file the install script needs to list.

    Excludes only test files. `version.ts` is present on disk, served by
    /api/plugin-source, AND listed in the bash loop (the install script
    then overwrites it inline from the request's ``version`` parameter,
    but the fetch still happens for parity with the manifest).
    """
    return {p.name for p in PLUGIN_SRC.glob("*.ts") if not p.name.endswith(".test.ts")}


def test_python_allow_list_matches_plugin_src():
    """`_plugin_files` (serves `/api/plugin-source?file=…`) must cover plugin/src."""
    actual = set(plugin_mod._plugin_files)
    expected = _expected_source_files()
    missing = expected - actual
    extra = actual - expected
    assert not missing and not extra, (
        f"_plugin_files drift — missing={sorted(missing)}, extra={sorted(extra)}. "
        "Add the new file to core_api/routes/plugin.py _plugin_files (and to "
        "the bash srcfile loop) so fresh plugin installs can download it."
    )


def test_install_script_srcfile_loop_matches_plugin_src():
    """The bash `for srcfile in …` loop in the install script template must match."""
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    match = re.search(r"for srcfile in\s+([^;]+);", src)
    assert match, (
        "Could not find the `for srcfile in …` loop in plugin.py — if you "
        "renamed the install-script template, update this test's regex too."
    )
    loop_files = set(match.group(1).split())
    expected = _expected_source_files()
    missing = expected - loop_files
    extra = loop_files - expected
    assert not missing and not extra, (
        f"install-script srcfile loop drift — missing={sorted(missing)}, "
        f"extra={sorted(extra)}. Any .ts file in plugin/src/ must appear here "
        "or `npm run build` on the target VM will fail with TS2307."
    )


def test_python_and_bash_lists_agree():
    """Keep the two hardcoded lists in lockstep with each other."""
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    match = re.search(r"for srcfile in\s+([^;]+);", src)
    assert match
    loop_files = set(match.group(1).split())
    python_files = set(plugin_mod._plugin_files)
    assert loop_files == python_files, (
        f"_plugin_files and install-script loop disagree — "
        f"only-in-python={sorted(python_files - loop_files)}, "
        f"only-in-bash={sorted(loop_files - python_files)}."
    )


def test_install_script_does_not_bake_a_manifest_heredoc():
    """The install script must NOT inline a manifest via HEREDOC.

    Origin of this guard: in 2026-05 OpenClaw upstream made
    ``contracts.tools`` strictly enforced in plugin manifests
    (openclaw/openclaw@7641783d). caura-memclaw's
    ``plugin/openclaw.plugin.json`` was updated to declare it, but the
    install script's baked HEREDOC was left behind — so every fresh
    ``curl /api/v1/install-plugin | bash`` produced a manifest without
    ``contracts.tools``, which OpenClaw silently rejected, dropping the
    entire MemClaw tool surface from the agent.

    The structural fix was to serve the manifest via ``/plugin-source``
    (single source of truth) and have the installer fetch it. This test
    locks in that fix: any future regression that re-introduces an
    inline HEREDOC for the manifest will fail here.
    """
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    assert "MANIFEST_EOF" not in src, (
        "Install script appears to bake openclaw.plugin.json via a HEREDOC "
        "again. Don't — fetch from /plugin-source instead. See "
        "_plugin_root_files in this module and the [4/7] step of the "
        "install script template."
    )


def test_install_script_fetches_manifest_from_plugin_source():
    """Step [4/7] must curl the manifest from /plugin-source."""
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    assert "/api/plugin-source?file=openclaw.plugin.json" in src, (
        "Install script must fetch openclaw.plugin.json from "
        "/api/plugin-source so the manifest stays in lockstep with the "
        "canonical plugin/openclaw.plugin.json. See "
        "test_install_script_does_not_bake_a_manifest_heredoc for context."
    )


def test_plugin_root_files_includes_manifest():
    """``_plugin_root_files`` must include ``openclaw.plugin.json``."""
    assert "openclaw.plugin.json" in plugin_mod._plugin_root_files, (
        "openclaw.plugin.json must be in _plugin_root_files so the "
        "/plugin-source endpoint serves it. The install script depends on "
        "this — without it, fresh installs would fall back to a 404."
    )


def test_served_manifest_declares_contracts_tools():
    """The on-disk manifest must declare ``contracts.tools``.

    OpenClaw upstream rejects every ``api.registerTool`` call when this
    field is missing (since 2026-05-01). The plugin TS suite has its
    own drift test (tool-definitions.test.ts) that asserts the list
    matches MEMCLAW_TOOLS exactly; this Python test only guards the
    file-level invariant so a server-side test failure surfaces too
    if someone deletes the field from the manifest file.
    """
    import json

    manifest_path = REPO_ROOT / "plugin" / "openclaw.plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    contracts = manifest.get("contracts", {})
    tools = contracts.get("tools", [])
    assert isinstance(tools, list) and len(tools) > 0, (
        f"plugin/openclaw.plugin.json must declare contracts.tools — got "
        f"{contracts!r}. OpenClaw rejects all api.registerTool calls "
        "without it."
    )
