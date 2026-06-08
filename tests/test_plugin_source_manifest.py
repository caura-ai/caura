"""Guards the two hardcoded plugin-file lists in core_api.routes.plugin.

A 2026-04-16 refactor added plugin/src/paths.ts + logger.ts but forgot to
register them in either the Python allow-list (`_plugin_files`) or the
bash `for srcfile in …` loop inside the install-script template. Every
fresh `curl … | bash` install broke with `TS2307: Cannot find module
'./paths.js'` until both lists were fixed on 2026-04-19.

This test keeps them in lockstep with `plugin/src/*.ts`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


from core_api.routes import fleet as fleet_mod
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
    # ``min_auto_deploy_plugin_version`` lets the plugin client know the
    # server-side floor for auto-upgrade decisions without a second
    # round trip. Must mirror ``fleet.MIN_AUTO_DEPLOY_PLUGIN_VERSION``
    # exactly — drift would defeat the whole point of surfacing it here.
    assert "min_auto_deploy_plugin_version" in data
    assert data["min_auto_deploy_plugin_version"] == fleet_mod.MIN_AUTO_DEPLOY_PLUGIN_VERSION
    assert isinstance(data["min_auto_deploy_plugin_version"], str)

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


def test_install_script_alsoAllow_lockstep():
    """The install script's hardcoded ``alsoAllow`` array MUST match
    ``contracts.tools`` in ``plugin/openclaw.plugin.json`` (and, transitively,
    ``MEMCLAW_TOOLS`` in ``plugin/src/tools.ts`` — the TS-side boot-time
    drift check enforces that direction; this Python test pins the
    cross-language symmetry).

    Drift this guards: pre-fix the install script's ``alsoAllow`` was
    written by hand at ``plugin.py:506`` and shipped 10 entries — missing
    ``memclaw_keystones``. Every fresh ``curl /api/install-plugin | bash``
    install wrote that 10-tool list into ``~/.openclaw/openclaw.json``, and
    OpenClaw silently refused every ``memclaw_keystones`` invocation
    because it wasn't in the allowed set. The plugin's boot log emitted a
    warning, but operators don't read gateway.log, so the keystone tool
    was effectively disabled on every fresh-installed node.

    ``memclaw_keystones_set`` is intentionally NOT in the install-script's
    ``alsoAllow`` because ``tools.json`` marks it ``plugin_exposed: false``
    — it is the admin authoring path, served only via MCP (memclaw_server)
    and never exposed to OpenClaw-side agents. The two sides of this test
    deliberately use the SAME canonical source (``contracts.tools``) so a
    future plugin_exposed flip is picked up automatically.
    """
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    m = re.search(r"const tools = \[(.*?)\];", src, re.DOTALL)
    assert m, (
        "Could not find ``const tools = [...]`` in the install-script template. "
        "If you renamed the variable, update this test's regex."
    )
    install_tools = [s.strip().strip("'\"") for s in m.group(1).split(",")]
    install_tools = [t for t in install_tools if t]  # drop trailing-comma empties

    manifest_path = REPO_ROOT / "plugin" / "openclaw.plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    contracts_tools = manifest["contracts"]["tools"]

    assert install_tools == contracts_tools, (
        f"install-script alsoAllow drift — \n"
        f"  install-script: {install_tools}\n"
        f"  contracts.tools: {contracts_tools}\n"
        f"missing-from-install={sorted(set(contracts_tools) - set(install_tools))}, "
        f"extra-in-install={sorted(set(install_tools) - set(contracts_tools))}. "
        "Both lists must hold every ``plugin_exposed: true`` tool from "
        "``plugin/tools.json`` in the same order."
    )


def test_plugin_manifest_version_matches_package_json():
    """``openclaw.plugin.json:version`` must match ``plugin/package.json:version``.

    The two are read by different downstream surfaces:
      - ``package.json:version`` is what ``_plugin_version()`` (in
        ``core_api/routes/plugin.py``) returns; the heartbeat auto-upgrade
        trigger and the install script's stamped ``MEMCLAW_PLUGIN_VERSION``
        derive from this side.
      - ``openclaw.plugin.json:version`` is what OpenClaw reads when
        loading the plugin and what any plugin-info UI renders to operators.

    Drift this guards: ``openclaw.plugin.json`` was at "2.5.0" while
    ``package.json`` was at "2.6.0" on main 2026-05-20. A node fresh-installed
    via the install script ended up with two different version labels in
    its own directory, depending on which file the inspecting tool happened
    to read.
    """


    pkg = json.loads((REPO_ROOT / "plugin" / "package.json").read_text(encoding="utf-8"))
    mfst = json.loads((REPO_ROOT / "plugin" / "openclaw.plugin.json").read_text(encoding="utf-8"))
    assert pkg["version"] == mfst["version"], (
        f"version drift — package.json={pkg['version']!r}, "
        f"openclaw.plugin.json={mfst['version']!r}. Bump both in lockstep."
    )


def test_install_script_claims_both_slots():
    """The install script's inline setup.js MUST set both ``slots.memory``
    AND ``slots.contextEngine`` to ``"memclaw"``.

    Pre-fix the script only set ``slots.memory``. OpenClaw resolves the
    active ContextEngine from ``plugins.slots.contextEngine`` (see
    OpenClaw 2026.5.4 ``dist/registry-DFFgCbcm.js:241 resolveContextEngine``);
    when that slot is unset, OpenClaw falls back to the default "legacy"
    engine and our plugin's ``assemble()`` is never called.

    Symptom: a customer running plugin v2.6.0 reported via WhatsApp that
    the agent could fetch keystones via the ``memclaw_keystones`` tool
    but the ``<keystone_rules>`` block never appeared in the system
    prompt. Tool surface works (slot-independent); dynamic injection
    silently disabled. This test pins both slot lines in the install
    script so we can't regress to the half-claimed state.
    """
    src = Path(plugin_mod.__file__).read_text(encoding="utf-8")
    assert "config.plugins.slots.memory = 'memclaw'" in src, (
        "Install script must claim plugins.slots.memory for memclaw."
    )
    assert "config.plugins.slots.contextEngine = 'memclaw'" in src, (
        "Install script must ALSO claim plugins.slots.contextEngine for memclaw — "
        "without it, OpenClaw falls back to the legacy context engine and our "
        "assemble() never runs, silently disabling keystone injection."
    )

