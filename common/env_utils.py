"""Tiny env-var parsing helpers shared across ``common/`` constants modules.

Lives at ``common/`` (not ``common/llm/`` or ``common/embedding/``) so it
has zero imports from peer ``common/`` subpackages — keeping the LLM
and embedding constants modules independent of each other while letting
them share the same defensive parsing without each module having to
maintain its own copy. Without this, both modules would need to repeat
the same validation logic and a bug fix in one would silently miss the
other.
"""

from __future__ import annotations

import os
import sys
from typing import overload


def read_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    """Read ``name`` from the environment, parse as int, fall back on bad input.

    Falls back to ``default`` and writes a warning to stderr in three
    cases:

    1. Env value is not a valid integer literal (``"200abc"``,
       ``"25s"``, etc.) — would raise ``ValueError`` from bare
       ``int(...)`` and crash module import.
    2. Env value is below ``minimum`` — ``int(...)`` accepts ``"0"`` and
       ``"-1"`` but most consumers here (``httpx.Limits``,
       semaphore-style caps) read 0 / negative as "block forever" or
       silently degrade, so the floor defaults to 1.
    3. ``TypeError`` from a non-string env value (defensive; shouldn't
       happen in practice but cheap to guard).

    *minimum* exists because that floor is not universal: a retry budget
    or a reserved-slot count has 0 as a legitimate, and sometimes
    intended, value. Without it, a knob whose default is 0 cannot be set
    to 0 — the value lands by falling back rather than by being accepted,
    which warns on a correct configuration AND, worse, would silently
    substitute a NEW default if one were ever chosen. Keyword-only, and
    defaulting to the historical 1, so every existing caller is unchanged.

    Module-level callers can't use a logger because structured logging
    isn't wired up yet at import time — stderr is the only universal
    channel and shows up in Cloud Logging's ``stderr`` stream.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(
            f"WARN: {name}={raw!r} is not a valid int; falling back to {default}",
            file=sys.stderr,
        )
        return default
    if value < minimum:
        print(
            f"WARN: {name}={raw!r} must be >= {minimum}; falling back to {default}",
            file=sys.stderr,
        )
        return default
    return value


@overload
def read_float_env(name: str, default: float) -> float: ...


@overload
def read_float_env(name: str, default: None) -> float | None: ...


def read_float_env(name: str, default: float | None) -> float | None:
    """Read ``name`` from the environment, parse as float, fall back on bad input.

    Same rationale as ``read_int_env``: a bare
    ``float(os.environ.get(...))`` raises ``ValueError`` at module import
    on a misconfigured value (e.g. ``"30s"`` instead of ``"30"``),
    crashing core-api / the worker before structured logging exists to
    report it.

    Unlike ``read_int_env`` this permits 0 and negatives — callers use
    them meaningfully (an explicit ``0.0`` timeout means "don't wait") —
    and ``None`` is a valid default meaning "unset, track another
    budget". The overloads keep ``float``-in / ``float``-out so callers
    with a concrete default don't inherit an ``Optional``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(
            f"WARN: {name}={raw!r} is not a valid float; falling back to {default}",
            file=sys.stderr,
        )
        return default


def clamp_keepalive(
    max_connections: int,
    max_keepalive: int,
    *,
    max_connections_var: str = "OPENAI_HTTPX_MAX_CONNECTIONS",
    max_keepalive_var: str = "OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS",
) -> int:
    """Clamp ``max_keepalive`` to ``max_connections``, warning if it had to.

    ``httpx.Limits`` silently clamps the keepalive value to
    ``max_connections`` when keepalive > max — making the env var
    misleading for an operator tuning under an incident. This helper
    surfaces the misconfig as a stderr warning at import time and
    applies the clamp explicitly.

    Each importing module produces its own warning when misconfigured
    (a process loading both ``common/llm/constants`` and
    ``common/embedding/constants`` will see the warning twice). The
    helper exists to keep the warning text + clamp logic identical
    across modules, not to deduplicate at runtime — the env vars
    themselves are imported once per module.

    ``max_connections_var`` / ``max_keepalive_var`` keyword-only args
    let a future provider with different env-var names (e.g. an
    Anthropic-only or vertex-only client pool) reuse the helper with
    accurate variable names in the warning text.
    """
    if max_keepalive > max_connections:
        print(
            f"WARN: {max_keepalive_var} ({max_keepalive}) > "
            f"{max_connections_var} ({max_connections}); "
            f"clamping keepalive to max_connections",
            file=sys.stderr,
        )
        return max_connections
    return max_keepalive
