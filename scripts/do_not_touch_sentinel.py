#!/usr/bin/env python3
"""Fail a PR that removes a string something outside this repo still depends on.

Hard rule 4 of the sunset plan: the do-not-touch list becomes CI. Half of it
shipped as ``scripts/legacy_name_ratchet.py``, which is *directional* — it fails a
file whose old-brand count goes **up**. A sweep that **deletes** a load-bearing
string makes that count go **down**, so the ratchet reads it as progress and
passes it. This is the other half: the same list, asserted in the other
direction.

The two gates are not redundant and neither subsumes the other. The ratchet
answers "did this change mint a new old-brand name?"; this one answers "did this
change remove a name something already depends on?" A rename wave trips the
first, a prose sweep trips the second, and Phase 7 is a prose sweep.

**The strings here are not all old-brand strings, and that is the point.** The
three embedding phrases carry no brand at all — they are ordinary English that a
production Datadog monitor matches on as a regex substring
(``terraform/datadog-gcp/gcp_alerts.tf:40`` in caura-enterprise, a CRITICAL
policy that has run since 2026-07-27). Reword one in a tidy-up — or merely
downgrade the level it is logged at — and the alert stops firing with nothing to
fail: no test breaks, no count moves, and the only symptom is an alert that never
arrives. No brand-based gate can ever see that, which is why the list is a list
rather than a pattern.

Usage::

    scripts/do_not_touch_sentinel.py            # fail on any missing string
    scripts/do_not_touch_sentinel.py --list     # print the list and exit 0
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# A plain substring of the file's text. Right for a name that is a contract:
# whoever depends on it depends on the characters, not on where they sit.
LITERAL = "literal"

# A substring of a string literal passed to a logging call, checked through the
# AST rather than the file text. The distinction matters here and nowhere else:
# ``_service.py`` discusses these phrases in its own comments as well as emitting
# them, so a text search passes on a tree where the ``logger.error`` call is gone
# and only the prose about it survives — which is precisely the tree that kills
# the alert. Only an emitted message can match a log filter.
LOG_MESSAGE = "log_message"

# Severity order, because a LOG_MESSAGE entry pins a floor as well as a phrase.
# The Datadog filter does not select on severity, which is exactly why the level
# has to be pinned here: what the level decides is whether the line is emitted at
# all. Downgrade a ``logger.error`` to ``logger.debug`` and the phrase is intact,
# the filter would still match it, and it never reaches Cloud Logging to be
# matched. Raising a level is harmless, so this is a minimum rather than a set.
_LEVELS = ("debug", "info", "warning", "error", "critical")
_LEVEL_RANK = {name: i for i, name in enumerate(_LEVELS)}
# ``warn`` is the deprecated alias for ``warning``; ``exception`` is ``error``
# with a traceback attached, and logs at ERROR.
_LEVEL_RANK["warn"] = _LEVEL_RANK["warning"]
_LEVEL_RANK["exception"] = _LEVEL_RANK["error"]

# ``logger.log(level, msg)`` names its level in an argument rather than in the
# method, and the one call in this repo picks it with a conditional. There is no
# static answer for that shape, so it counts as an emitter of the phrase but can
# never satisfy a level floor: unverifiable is reported, not assumed. A message a
# monitor depends on should be emitted through an explicit level method anyway,
# and this is what says so.
_LOG_METHODS = frozenset(_LEVEL_RANK) | {"log"}


@dataclass(frozen=True)
class Sentinel:
    path: str
    text: str
    kind: str
    breaks: str
    # LOG_MESSAGE only: the least severe level that still reaches the monitor.
    min_level: str | None = None


# ── the list ─────────────────────────────────────────────────────────────────
#
# Every entry carries ``legacy-name-ok`` because this file is itself scanned by
# the ratchet, and the reason is the same one every time: the line exists to pin
# a string rule 3 keeps readable forever. Excluding the file from the ratchet
# instead would leave a hole in that scan, which is the trade its author already
# refused once for the same reason.

SENTINELS: tuple[Sentinel, ...] = (
    # -- Matched by a live production Datadog monitor. Carries no brand. -------
    Sentinel(
        path="common/embedding/_service.py",
        text="Embedding service degraded",
        kind=LOG_MESSAGE,
        min_level="error",
        breaks="gcp_alerts.tf:40 stops detecting provider failure (>=3 consecutive)",
    ),
    Sentinel(
        path="common/embedding/_service.py",
        text="Embedding concurrency gate timeout",
        kind=LOG_MESSAGE,
        min_level="warning",
        breaks="gcp_alerts.tf:40 stops detecting our own concurrency cap filling",
    ),
    Sentinel(
        path="common/embedding/_service.py",
        text="Embedding backend refused at capacity",
        kind=LOG_MESSAGE,
        min_level="warning",
        breaks="gcp_alerts.tf:40 stops detecting TEI 429 — the only aggregate-demand signal",
    ),
    # -- The smoke probe: an emitter and a matcher that must move together. ----
    Sentinel(
        path="plugin/src/context-engine.ts",
        text="memclaw-smoke-",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the health-check probe stops writing the title report_corpus.py filters on",
    ),
    Sentinel(
        path="core-api/src/core_api/services/report_corpus.py",
        text="cache refresh|memclaw-smoke)",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="~200 probe facts/day stop being filtered and flood every per-agent report",
    ),
    # -- Wire contracts. A partial rename here does not degrade, it bricks. ----
    Sentinel(
        path="core-api/src/core_api/mcp_server.py",
        text='name.startswith("memclaw_")',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="every legacy-named tool call 404s — the alias shim is the whole promise",
    ),
    Sentinel(
        path="core-api/src/core_api/app.py",
        text='prefix="/api/v1/memclaw"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the keystones route mount moves and every deployed plugin loses keystones",
    ),
    Sentinel(
        path="core-api/src/core_api/constants.py",
        text='API_KEY_PREFIX = "mc_"',
        kind=LITERAL,
        breaks="every issued API key stops validating; the prefix is in customers' configs",
    ),
    Sentinel(
        path="core-api/src/core_api/agent_ids.py",
        text="memclaw-insighter",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the insighter's existing rows orphan — migration 030 seeded this id",
    ),
    Sentinel(
        path="core-api/src/core_api/agent_ids.py",
        text="memclaw-doc-indexer",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the doc indexer's existing rows orphan; nothing else references the literal",
    ),
    Sentinel(
        path="plugin/openclaw.plugin.json",
        text='"id": "memclaw"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="every installed plugin is orphaned — the id keys the on-disk install",
    ),
    Sentinel(
        path="core-api/src/core_api/routes/plugin.py",
        text=".openclaw/plugins/memclaw",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the generated installer writes to a path no installed plugin reads",
    ),
    Sentinel(
        path="core-storage-api/src/core_storage_api/observability.py",
        text="memclaw.observability",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the logger name changes and every log-based filter keyed to it goes quiet",
    ),
    Sentinel(
        path="core-api/src/core_api/services/organization_settings.py",
        text="memclaw.auto_upgrade_enabled",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="stored tenant settings key off this string; a rename reverts them to default",
    ),
    # -- Published channel names. Renaming these strands installed users. ------
    Sentinel(
        path="clients/typescript/package.json",
        text="@caura/memclaw-client",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the published npm package name changes under everyone who installed it",
    ),
    Sentinel(
        path="clients/python/pyproject.toml",
        text="memclaw-interviewer",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the installed console script disappears from every existing crontab",
    ),
    Sentinel(
        path="clients/python/src/caura_client/interviewer/installer.py",
        text='".config" / "memclaw-interviewer"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the interviewer stops finding config customers already have on disk",
    ),
    Sentinel(
        path=".github/workflows/publish-python-client.yml",
        text="memclaw-client-v*",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the release tag pattern stops triggering a publish",
    ),
    Sentinel(
        path=".github/workflows/publish-npm-client.yml",
        text="memclaw-client-ts-v*",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the release tag pattern stops triggering a publish",
    ),
    # -- Immutable migration history. Rule 2: point at it, never rewrite it. ---
    Sentinel(
        path=(
            "core-storage-api/src/core_storage_api/database/migrations/versions/"
            "030_register_memclaw_insighter.py"  # legacy-name-ok: pinned floor string
        ),
        text="WHERE m.agent_id = 'memclaw-insighter'",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="a replayed migration seeds a different agent id than the one in live rows",
    ),
    Sentinel(
        path=(
            "core-storage-api/src/core_storage_api/database/migrations/versions/"
            "012_vector_dim_1024.py"
        ),
        text='os.environ.get("MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the destructive-migration opt-out stops being readable by the env that sets it",
    ),
    Sentinel(
        path=(
            "core-storage-api/src/core_storage_api/database/migrations/versions/"
            "019_tenant_suppression.py"
        ),
        text="memclaw.org.suppression-changed",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the migration stops naming the topic it documents, before Phase 2 renames it",
    ),
)


def _static_text(node: ast.expr) -> str | None:
    """The compile-time-known text of a string argument, if it has any.

    Adjacent literals are concatenated by the parser before we see them, so a
    message split across source lines — which all three of ours are — arrives as
    one string and matches as one string.

    An f-string arrives as a ``JoinedStr`` instead, and its literal segments are
    still emitted verbatim. Joining them keeps a future f-string conversion from
    reading as "the phrase is gone" — a false failure is the safe direction, but
    one that names the wrong cause costs somebody an afternoon mid-sweep.
    Interpolations are dropped, so a phrase broken up by one does not match,
    which is right: it is no longer emitted whole and would no longer be found
    by a substring filter either.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _log_calls(source: str, path: str) -> list[tuple[str | None, str]]:
    """``(level, message)`` for every logging call — level ``None`` if unknowable.

    Positional args only, and every one of them: ``logger.log`` takes the level
    first, so keying on argument position would miss the message while keying on
    "any string argument" costs nothing.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # a file this gate cannot read is a failed gate
        raise RuntimeError(f"{path} does not parse: {exc}") from exc

    out: list[tuple[str | None, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            not isinstance(node.func, ast.Attribute)
            or node.func.attr not in _LOG_METHODS
        ):
            continue
        level = node.func.attr if node.func.attr in _LEVEL_RANK else None
        for arg in node.args:
            text = _static_text(arg)
            if text:
                out.append((level, text))
    return out


def _check(sentinel: Sentinel, root: Path) -> str | None:
    """``None`` if the string survives, else why it did not."""
    target = root / sentinel.path
    if not target.is_file():
        # Not "skip": a path that stopped existing is the loudest possible
        # version of the thing this gate exists to catch, and a gate that skips
        # what it cannot find passes every PR once the file moves.
        return "the file no longer exists"

    source = target.read_text(encoding="utf-8", errors="replace")

    if sentinel.kind == LITERAL:
        return None if sentinel.text in source else "the string is gone"

    emitting = [
        (level, text)
        for level, text in _log_calls(source, sentinel.path)
        if sentinel.text in text
    ]
    if not emitting:
        if sentinel.text in source:
            return "it survives only in prose — no logging call emits it any more"
        return "the string is gone"

    if not sentinel.min_level:
        return None

    required = _LEVEL_RANK[sentinel.min_level]
    if any(level and _LEVEL_RANK[level] >= required for level, _ in emitting):
        return None

    known = sorted({level for level, _ in emitting if level})
    if not known:
        return (
            "it is emitted through logger.log, whose level is an argument — use an "
            f"explicit .{sentinel.min_level}() so the level can be checked"
        )
    return (
        f"it is emitted at {', '.join(known)}, below {sentinel.min_level} — the phrase "
        "is intact but the line no longer reaches the sink the monitor reads"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assert that load-bearing strings survive this change."
    )
    parser.add_argument("--list", action="store_true", help="print the list and exit 0")
    parser.add_argument(
        "--root", default=str(REPO_ROOT), help="repository root to check"
    )
    args = parser.parse_args()

    if args.list:
        print(f"{len(SENTINELS)} protected strings:")
        for s in SENTINELS:
            print(f"  {s.path}\n      {s.text!r} ({s.kind}) — {s.breaks}")
        return 0

    if not SENTINELS:
        # An empty list is a gate that passes everything while looking green.
        print("The sentinel list is empty, so this gate is checking nothing.")
        return 1

    root = Path(args.root).resolve()
    failures = [(s, why) for s in SENTINELS if (why := _check(s, root)) is not None]

    if not failures:
        print(f"All {len(SENTINELS)} protected strings survive.")
        return 0

    print(f"This change removes {len(failures)} string(s) that something depends on.\n")
    for s, why in failures:
        print(f"  {s.path}")
        print(f"      {s.text!r} — {why}")
        print(f"      breaks: {s.breaks}\n")
    print(
        "Rule 4 of the sunset plan: the do-not-touch list is CI. These strings are on\n"
        "the floor by design — a contract, an immutable migration, or prose a production\n"
        "monitor matches on — so nothing in the tree fails when they go.\n\n"
        "Restore the string. If it genuinely must change, the dependant has to move\n"
        "first and in its own repo, and this list has to change in the same PR as the\n"
        "code — never after it, and never instead of it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
