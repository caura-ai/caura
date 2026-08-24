"""Env lookup shared by the standalone repro/wet-test harnesses.

Every name these scripts read was renamed by the Caura rebrand, and rule 3
makes the pre-rename spelling permanent — so each read has to try both. That
rule lives here once rather than in each script, which is the difference
between closing the legacy window later as one edit and remembering six.

Private, underscore-prefixed module, imported by sibling scripts through the
``sys.path`` prologue they already use for ``_locomo_bench`` and
``_f3_wet_test_observer``: ``scripts/`` deliberately has no ``__init__.py``,
because these are harnesses you run by path, not an importable package.

The three entry points differ only in what they do when nothing is set:

    env_any       -> ``None``            (the value is optional)
    env_required  -> exits 2 with a message
    env_default   -> the supplied default
"""

from __future__ import annotations

import os
import sys


def env_any(name: str) -> str | None:
    """First non-empty of ``name`` and its pre-rename spelling.

    ``or`` rather than a defined-check on purpose: blank counts as unset, so an
    exported-but-empty ``CAURA_*`` cannot shadow a working legacy value. That is
    the same first-non-empty rule the plugin's ``readEnv`` and core-api's
    settings use, and it is the reason neither uses ``AliasChoices``.
    """
    legacy = name.replace("CAURA_", "MEMCLAW_", 1)  # legacy-name-ok: rule 3 dual-read alias
    return os.environ.get(name) or os.environ.get(legacy)


def env_required(name: str) -> str:
    """``env_any``, exiting 2 when neither spelling carries a value."""
    val = env_any(name)
    if not val:
        print(f"ERROR: ${name} must be set", file=sys.stderr)
        sys.exit(2)
    return val


def env_default(name: str, default: str = "") -> str:
    """``env_any`` falling back to ``default``."""
    return env_any(name) or default
