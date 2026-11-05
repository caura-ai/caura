"""``CAURA_ASSET_ROOT`` relocates the plugin and skill trees.

A build that installs core-api as a regular package runs
``core_api/routes/plugin.py`` from ``site-packages``. The default walk up to
the repo root then lands inside the virtualenv, so every plugin and skill
file reads as missing: ``/plugin-manifest`` and ``/plugin-source`` answer
404, ``/install-plugin`` 500s reading ``plugin/package.json``, and the
direct-MCP skill fetch 404s. The hosted image is built that way, and the
override is how it points these routes at the trees it copied.

The module resolves its paths at import time, so the override case imports
it in a fresh interpreter rather than reloading it under the other tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core_api.routes import plugin as plugin_mod

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent

_PROBE = """
import asyncio, json
from core_api.routes import plugin as p

def body(r):
    return r if isinstance(r, (str, dict)) else {"status": r.status_code}

print(json.dumps({
    "manifest": body(asyncio.run(p.plugin_manifest())),
    "index_ts": body(asyncio.run(p.plugin_source(file="index.ts"))),
    "default_skill_path": str(p._skill_md_path),
    "named_skill": body(asyncio.run(p.skill_by_name("caura"))),
}))
"""


@pytest.mark.parametrize("value", [None, "", "   "])
def test_default_root_is_the_repo_root(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CAURA_ASSET_ROOT", raising=False)
    else:
        monkeypatch.setenv("CAURA_ASSET_ROOT", value)
    assert plugin_mod._resolve_asset_root() == REPO_ROOT
    assert (REPO_ROOT / "plugin" / "package.json").is_file()


def test_override_serves_the_relocated_trees(tmp_path):
    # Skill paths relative to the root, taken from the module so this test
    # follows the allowlist instead of restating it.
    default_skill = plugin_mod._skill_md_path.relative_to(REPO_ROOT)
    named_skill = plugin_mod._SKILL_FILES["caura"].relative_to(REPO_ROOT)

    (tmp_path / "plugin" / "src").mkdir(parents=True)
    (tmp_path / "plugin" / "package.json").write_text(
        json.dumps({"version": "0.0.0-relocated"})
    )
    (tmp_path / "plugin" / "src" / "index.ts").write_text("// relocated index\n")
    for rel, text in (
        (default_skill, "relocated default skill\n"),
        (named_skill, "relocated caura skill\n"),
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)

    env = {
        **os.environ,
        "CAURA_ASSET_ROOT": str(tmp_path),
        # pytest's ``pythonpath`` setting edits sys.path only; hand the child
        # the same import path so it imports this checkout's core_api.
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["manifest"]["version"] == "0.0.0-relocated"
    assert out["manifest"]["src_files"] == ["index.ts"]
    assert out["index_ts"] == "// relocated index\n"
    assert out["default_skill_path"] == str(tmp_path / default_skill)
    assert out["named_skill"] == "relocated caura skill\n"
