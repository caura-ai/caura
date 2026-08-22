"""Permanent-alias contract for the 2026-08 client rename.

Mirrors the MCP tool rename guarantee: the canonical spelling is Caura /
caura_client, and every pre-rename spelling keeps working forever. These
tests are the tripwire — if a refactor breaks any of them, published 0.4.x
examples and installed cron entries break with it.
"""

from __future__ import annotations

import warnings

import caura_client


def test_legacy_import_package_is_the_same_objects():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import memclaw_client

    assert memclaw_client.MemClaw is caura_client.Caura
    assert memclaw_client.MemClawError is caura_client.CauraError
    assert memclaw_client.MemClawAPIError is caura_client.CauraAPIError
    assert memclaw_client.DEFAULT_BASE_URL == caura_client.DEFAULT_BASE_URL


def test_legacy_import_warns_deprecation():
    import importlib
    import sys

    for mod in [m for m in sys.modules if m.startswith("memclaw_client")]:
        del sys.modules[mod]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("memclaw_client")
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_class_alias_is_identity_not_subclass():
    assert caura_client.MemClaw is caura_client.Caura


def test_legacy_exception_catches_new_raises():
    err = caura_client.CauraAPIError(500, "boom")
    try:
        raise err
    except caura_client.MemClawAPIError as caught:
        assert caught.status_code == 500


def test_legacy_submodules_are_mirrored():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import memclaw_client.client as legacy_client

    import caura_client.client as canonical_client

    assert legacy_client is canonical_client
