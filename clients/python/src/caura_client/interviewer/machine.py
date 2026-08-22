"""Stable short machine identity for per-file node ids.

The node id must survive reboots and CLI reinstalls but distinguish two
machines that sync the same transcript file (each then owns an independent
watermark stream — clean, never corrupting).
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys


def _raw_machine_id() -> str:
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    parts = line.split('"')
                    # >= 4 means at least two quote PAIRS, so parts[-2] is a
                    # real quoted value — with >= 2 a single stray quote
                    # would return the raw line as the "UUID".
                    if len(parts) >= 4:
                        return parts[-2]
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        with open("/etc/machine-id", encoding="utf-8") as f:
            mid = f.read().strip()
            # Reject degenerate ids (e.g. the all-zeros /etc/machine-id
            # many container images ship): two such containers would
            # collide on the same watermark doc and corrupt each other's
            # cursors. Fall through to the host-based fallback instead.
            if mid and len(set(mid)) > 1:
                return mid
    except OSError:
        pass
    # Last resort: stable enough per user+host.
    return f"{socket.gethostname()}:{os.path.expanduser('~')}"


_MACHINE_ID_SHORT: str | None = None


def machine_id_short() -> str:
    """First 12 hex chars of sha1 over the platform machine identity.

    Cached per process: the identity is immutable for the process
    lifetime and the ioreg subprocess / file read shouldn't repeat.
    """
    global _MACHINE_ID_SHORT
    if _MACHINE_ID_SHORT is None:
        _MACHINE_ID_SHORT = hashlib.sha1(
            _raw_machine_id().encode(), usedforsecurity=False
        ).hexdigest()[:12]
    return _MACHINE_ID_SHORT
