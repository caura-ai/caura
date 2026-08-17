"""Permanent import alias: ``memclaw_client`` → ``caura_client``.

MemClaw was renamed Caura in 2026-08. This package keeps every historical
import working forever — ``from memclaw_client import MemClaw`` behaves
exactly as it did in 0.4.x, and the objects are the *same* objects
``caura_client`` exports (aliases, not copies, so isinstance/except clauses
agree across both spellings). New code should import ``caura_client``.

This module ships inside the ``caura-client`` distribution. The old
``memclaw-client`` distribution (0.5.0+) is an empty shell that depends on
``caura-client``, so both ``pip install`` spellings converge here.
"""

from __future__ import annotations

import sys as _sys
import warnings as _warnings

from caura_client import *  # noqa: F401,F403
from caura_client import DEFAULT_BASE_URL, __all__, __version__  # noqa: F401

_warnings.warn(
    "memclaw_client is the legacy name of caura_client and remains supported "
    "forever; new code should `import caura_client`.",
    DeprecationWarning,
    stacklevel=2,
)

# Mirror the real submodules so `import memclaw_client.client` (and friends)
# resolve to the same module objects rather than parallel copies. Deeper
# paths (e.g. memclaw_client.interviewer.cli) resolve through the aliased
# parent's __path__, which is the real caura_client/interviewer directory —
# `python -m memclaw_client.interviewer.cli` keeps working for cron entries
# written by pre-rename installers.
from caura_client import client, exceptions, interviewer, models  # noqa: E402,F401

for _name, _module in (
    ("client", client),
    ("exceptions", exceptions),
    ("interviewer", interviewer),
    ("models", models),
):
    _sys.modules[f"{__name__}.{_name}"] = _module
