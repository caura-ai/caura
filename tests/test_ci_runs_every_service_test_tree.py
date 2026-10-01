"""Every service's own ``tests/`` tree is run AND linted by CI.

``core-api/tests/`` held the only regression cover for the tenantless
``enforce_tenant(None)`` write gate and for broker write attribution, and no
workflow ran or linted it: CI's pytest steps named ``tests/``,
``core-storage-api/tests/``, ``core-operations/tests/`` and
``core-worker/tests/`` explicitly, and an explicit path overrides the root
``testpaths``. A revert of either guard would have merged green.

This pins the shape so the next service tree cannot be missed the same way:
for every top-level ``core-*`` directory with a ``tests/`` holding test
modules, ``ci.yml`` must name that tree in a ``pytest`` step, a
``ruff check`` step and a ``ruff format --check`` step.

WHAT THIS DOES NOT DO: parse the workflow. It reads ``run:`` lines as text,
so a step that is present but skipped by an ``if:`` would still pass here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github/workflows/ci.yml"


def _service_test_trees() -> list[str]:
    return sorted(
        f"{d.name}/tests/"
        for d in REPO.glob("core-*")
        if (d / "tests").is_dir() and any((d / "tests").glob("test_*.py"))
    )


def _run_commands() -> list[str]:
    """Every command line in ci.yml's ``run:`` steps, one-line or block."""
    lines = CI.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_block = False
    block_indent = 0
    for line in lines:
        m = re.match(r"^(\s*)run:\s*(.*)$", line)
        if m:
            rest = m.group(2).strip()
            if rest in {"|", ">", "|-", ">-"}:
                in_block, block_indent = True, len(m.group(1))
            else:
                in_block = False
                out.append(rest)
            continue
        if in_block:
            if line.strip() and len(line) - len(line.lstrip()) <= block_indent:
                in_block = False
            else:
                out.append(line.strip())
    return out


def test_there_are_service_trees_to_check():
    assert "core-api/tests/" in _service_test_trees()


@pytest.mark.parametrize("tree", _service_test_trees())
def test_tree_is_run_by_pytest(tree: str):
    cmds = _run_commands()
    assert any(re.search(rf"\bpytest\s+{re.escape(tree)}", c) for c in cmds), (
        f"no CI pytest step runs {tree}"
    )


@pytest.mark.parametrize("tree", _service_test_trees())
def test_tree_is_linted(tree: str):
    cmds = _run_commands()
    checks = [c for c in cmds if "ruff check" in c]
    formats = [c for c in cmds if "ruff format --check" in c]
    assert any(tree in c.split() for c in checks), f"no ruff check covers {tree}"
    assert any(tree in c.split() for c in formats), (
        f"no ruff format check covers {tree}"
    )
