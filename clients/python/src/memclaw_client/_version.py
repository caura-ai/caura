"""Single source of truth for the client version.

The version is read from the installed distribution metadata so it can never
drift from ``pyproject.toml``. When the package is imported from source without
being installed (running against ``src/`` directly rather than via
``pip install -e .``), the metadata lookup fails and we fall back to a local
sentinel.

This lives in its own module (rather than in ``__init__``) so that both
``__init__`` and ``client`` can import the value without a circular import.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

#: Used when distribution metadata is unavailable (uninstalled source checkout).
FALLBACK_VERSION = "0.0.0+dev"

#: The distribution name as declared in ``pyproject.toml``.
_DISTRIBUTION_NAME = "memclaw-client"


def _detect_version() -> str:
    try:
        return _pkg_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION


__version__ = _detect_version()
