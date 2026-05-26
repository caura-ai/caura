"""Plugin↔backend version compatibility.

Plugin is released on its own cadence (see release-please ``plugin``
package), so backend cannot assume ``plugin_version == backend VERSION``.
We log a warning when a heartbeat reports a plugin older than the minimum
recommended version; no hard rejection — operators decide when to upgrade.
"""

MIN_RECOMMENDED_PLUGIN_VERSION = "2.6.3"

# Server-side floor below which plugins must NOT auto-upgrade — the
# heartbeat path in ``routes/fleet.py`` enforces this hard, and
# ``routes/plugin.py`` surfaces it via ``/plugin-manifest`` so the
# plugin client can read the floor in a single round trip.
#
# Why this floor exists: pre-CAURA-444 plugin releases (0.98.x /
# 2.0-2.5) shipped without manifest-aware deploy, so a one-shot
# auto-deploy from those versions could leave the install in a
# partial state (some files updated, some stale, no rollback).
# Manifest-aware deploy is on main as of CAURA-444 and was first cut
# into a tagged plugin release at 2.6.0 — that's the first version
# the server trusts to be safe-to-auto-deploy. Older plugins need a
# one-time manual re-install before auto-deploy can take over.
MIN_AUTO_DEPLOY_PLUGIN_VERSION: str = "2.6.0"


def _parse(v: str) -> tuple[int, ...]:
    """Parse a dotted version into an int tuple. Pre-release/build suffixes are dropped."""
    core = v.split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for seg in core.split("."):
        if not seg.isdigit():
            break
        parts.append(int(seg))
    return tuple(parts)


def is_plugin_outdated(reported: str | None) -> bool:
    """Return True iff ``reported`` parses successfully and is strictly below the recommended minimum."""
    if not reported:
        return False
    r = _parse(reported)
    m = _parse(MIN_RECOMMENDED_PLUGIN_VERSION)
    if not r or not m:
        return False
    return r < m
