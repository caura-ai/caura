"""Plugin↔backend version compatibility.

Plugin is released on its own cadence (see release-please ``plugin``
package), so backend cannot assume ``plugin_version == backend VERSION``.
We log a warning when a heartbeat reports a plugin older than the minimum
recommended version; no hard rejection — operators decide when to upgrade.
"""

MIN_RECOMMENDED_PLUGIN_VERSION = "2.6.2"


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
