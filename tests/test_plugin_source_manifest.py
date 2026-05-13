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


def _bash_fallback_src_files(src: str) -> set[str]:
    """Extract the hardcoded ``SRC_FILES="..."`` fallback list from the install script."""
    match = re.search(r'SRC_FILES="([^"]+)"', src)
    assert match, (
        "Could not find the hardcoded ``SRC_FILES=\"…\"`` fallback in plugin.py. "
        "The install script must keep a fallback list so installs still succeed "
        "when /api/plugin-manifest is unreachable or python3 is missing."
    )
    return set(match.group(1).split())


def test_install_script_srcfile_fallback_matches_plugin_src():
    """The bash ``SRC_FILES=`` fallback in the install script template must match plugin/src."""
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    fallback = _bash_fallback_src_files(src)
    expected = _expected_source_files()
    missing = expected - fallback
    extra = fallback - expected
    assert not missing and not extra, (
        f"install-script SRC_FILES fallback drift — missing={sorted(missing)}, "
        f"extra={sorted(extra)}. Any .ts file in plugin/src/ must appear in the "
        "fallback list too, or installs that can't reach /api/plugin-manifest "
        "(e.g. minimal containers without python3) will fail with TS2307."
    )


def test_python_and_bash_lists_agree():
    """Keep the Python allow-list and the bash fallback in lockstep with each other."""
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    fallback = _bash_fallback_src_files(src)
    python_files = set(plugin_mod._plugin_files)
    assert fallback == python_files, (
        f"_plugin_files and install-script SRC_FILES fallback disagree — "
        f"only-in-python={sorted(python_files - fallback)}, "
        f"only-in-bash={sorted(fallback - python_files)}."
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
    """Step [4/7] must curl ``/api/plugin-manifest`` for the file list.

    Drives both ``SRC_FILES`` and ``ROOT_FILES`` from the server response so
    fresh installs don't lag the canonical allow-list. The hardcoded fallback
    is exercised only when ``python3`` is missing or the endpoint is down.
    """
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    # Accept either the versioned (``/api/v1/plugin-manifest``, used with
    # X-API-Key — the production path through the enterprise nginx
    # gateway) or the unversioned bootstrap alias (``/api/plugin-manifest``,
    # used if/when nginx allowlists it the way it does ``/plugin-source``).
    # The contract being locked here is "the script DOES fetch the
    # manifest at step [4/7]", not which exact URL it uses.
    assert "/plugin-manifest" in src, (
        "Install script must fetch /plugin-manifest at step [4/7] to drive "
        "SRC_FILES/ROOT_FILES so file lists stay in lockstep with the "
        "server's _plugin_files/_plugin_root_files."
    )


def test_plugin_root_files_includes_manifest():
    """``_plugin_root_files`` must include ``openclaw.plugin.json``."""
    assert "openclaw.plugin.json" in plugin_mod._plugin_root_files, (
        "openclaw.plugin.json must be in _plugin_root_files so the "
        "/plugin-source endpoint serves it. The install script depends on "
        "this — without it, fresh installs would fall back to a 404."
    )


async def test_plugin_manifest_endpoint_shape_and_contents(client):
    """``/api/v1/plugin-manifest`` is the single source of truth for upgrades.

    The plugin's deploy command (``heartbeat.ts:processCommand``) used to
    carry its own hardcoded srcFiles array (CAURA-444 drift 1). Centralising
    the answer here means the plugin queries one endpoint and trusts it.
    Anyone changing the response shape will silently break every fleet
    that has already upgraded — so this test pins the contract.
    """
    resp = await client.get("/api/v1/plugin-manifest")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Locked contract — adding fields is OK, removing or renaming is not.
    assert "version" in data and isinstance(data["version"], str) and data["version"]
    assert "src_files" in data and isinstance(data["src_files"], list)
    assert "root_files" in data and isinstance(data["root_files"], list)
    assert (
        "content_hash" in data
        and isinstance(data["content_hash"], str)
        and len(data["content_hash"]) == 64  # sha256 hex
    )

    # The src_files list MUST equal the in-process list — that's the
    # whole point of having a manifest endpoint. If they drift we
    # introduce a NEW class of drift bugs, defeating the purpose.
    assert data["src_files"] == list(plugin_mod._plugin_files)
    assert set(data["root_files"]) == plugin_mod._plugin_root_files

    # Sanity: hash should match the existing /plugin-source-hash output
    # so old clients (still using -hash) and new ones (using manifest)
    # see the same fingerprint.
    hash_resp = await client.get("/api/v1/plugin-source-hash")
    assert hash_resp.status_code == 200
    assert data["content_hash"] == hash_resp.text.strip()


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
