"""Fail fast if a "remove unused deps" cleanup breaks a real import.

The 2026-04-16 dep cleanup deleted `cachetools` while `tenant_settings`
still imported `TTLCache` from it — the core-api wouldn't even boot.
The regression was only caught during E2E. These tests exercise the
modules with load-time side-effects so missing transitive deps fail a
~1-second unit test instead of a container boot.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_path",
    [
        # Home of the cachetools.TTLCache import that got broken.
        "core_api.services.organization_settings",
        # App bootstrap — imports most routers transitively.
        "core_api.app",
        # MCP server — registers every tool spec at import time, any
        # dep gap in the tools package surfaces here.
        "core_api.mcp_server",
    ],
)
def test_module_imports_cleanly(module_path: str):
    importlib.import_module(module_path)
