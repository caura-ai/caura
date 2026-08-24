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
# Entries whose pinned text contains the old brand carry ``legacy-name-ok``,
# because this file is itself scanned by the ratchet, and the reason is the same
# every time: the line exists to pin a string rule 3 keeps readable forever.
# Entries pinning brand-free text need no marker and correctly have none — the
# ratchet never sees them. Excluding this file from the ratchet instead would
# leave a hole in that scan, which is the trade its author already refused once.

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
    # The whole assignment, not the marker value alone — and this one is a
    # judgement call the comment-only guard below CANNOT make for us.
    #
    # That guard asks whether the pinned text appears on a line that starts with
    # a comment character. This value *is* comment-shaped (it starts with ``#``),
    # but the line defining it starts with ``CRON_MARKER``, so the guard sees
    # nothing and every candidate form passes it. Pinning the bare value would
    # therefore be accepted while being satisfiable by anyone who later writes a
    # single line of commentary quoting the marker — and commentary quoting it is
    # likelier here than usual, precisely because it reads as a comment already.
    #
    # Anchoring to the assignment also fails if the value survives only in prose
    # after the constant is deleted. It costs a false red on a rename that keeps
    # the value, which is behaviour-preserving; that trade is deliberate, because
    # a false red is loud and a gate that quietly stopped protecting is not.
    Sentinel(
        path="clients/python/src/caura_client/interviewer/installer.py",
        text='CRON_MARKER = "# memclaw-interviewer (managed)"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "uninstall stops finding crontab entries customers already have, "
            "and re-install duplicates them instead of replacing"
        ),
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
    # -- Plugin self-migration anchors. -----------------------------------------
    #
    # A category the entries above do not cover. Everything else here is pinned
    # for a consumer in another repo or another company's config — Datadog, npm,
    # a crontab, a customer's .env. These are pinned because the consumer is the
    # plugin's OWN PREVIOUS OUTPUT, already sitting in end users' TOOLS.md,
    # AGENTS.md and .env files, written by versions 0.98.5 and 1.x.
    #
    # That makes them invisible twice over. The ratchet is directional, so
    # renaming one lowers the file's count and reads as progress. And a reviewer
    # seeing a legacy heading prefix in a diff sees brand prose, because it IS
    # brand prose — it just happens to be brand prose that a regex on a user's
    # disk has to keep matching.
    #
    # Every text below is anchored to its code form rather than to the bare
    # string, for the reason the CRON_MARKER entry gives above: LITERAL is a
    # substring test over the whole file, and educate.ts discusses each of these
    # in its own comments as well as using it. Pinning the bare phrase would be
    # satisfied by the commentary alone, on exactly the tree where the code that
    # matters is gone. Verified: each text below appears only on code lines.
    Sentinel(
        path="plugin/src/educate.ts",
        text="const tag = `memclaw:${marker}`;",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "the fence tag written into every install's TOOLS.md/AGENTS.md as an "
            "HTML comment also builds the regex that finds that block again — "
            "rename it and the next run appends a second block instead of "
            "updating the first, in every existing install at once"
        ),
    ),
    Sentinel(
        path="plugin/src/educate.ts",
        text='"## MemClaw —",',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "the legacyHeadingPrefix that finds pre-fence sections this plugin "
            "itself emitted (1.x and 0.98.5) — rename it and those sections are "
            "never replaced, so users keep a stale duplicate forever"
        ),
    ),
    Sentinel(
        path="plugin/src/educate.ts",
        text='.memclaw-bak"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "the one-shot backup is written only when the path does not already "
            "exist — rename it and the guard stops seeing the backup already on "
            "disk, overwriting the hand-edits it was created to preserve"
        ),
    ),
    Sentinel(
        path="plugin/src/educate.ts",
        # Regex syntax, so this form cannot occur in prose about the phrase.
        text="You have been connected to MemClaw[^\\n]*?always include your agent_id",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "the pre-C1 blurb stops being stripped and accumulates alongside "
            "the current one on every subsequent run"
        ),
    ),
    Sentinel(
        path="plugin/src/educate.ts",
        text='includes("MemClaw — Tools Available")',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="phantom plugin-written files under workspaces/ stop being collected",
    ),
    # Diagnostic rather than a data contract, and pinned anyway: breaking these
    # does not corrupt anything, it makes every existing install report itself as
    # not-installed, which is the shape of bug that gets chased for a week.
    Sentinel(
        path="plugin/src/heartbeat.ts",
        text='"HEARTBEAT.md"), "utf-8").includes("memclaw")',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="heartbeat reports heartbeat_md=false for every install written before the rename",
    ),
    Sentinel(
        path="plugin/src/heartbeat.ts",
        text='"TOOLS.md"), "utf-8").toLowerCase().includes("memclaw")',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="heartbeat reports tools_md=false for every install written before the rename",
    ),
    # Same category as the six above, found one PR later while rebranding the
    # prose in this very file — which is exactly why it is being pinned here:
    # the surrounding comments now say Caura, so the next reader has one more
    # reason to think the marker should follow them. It must not.
    Sentinel(
        path="plugin/src/reconcile-skills.ts",
        text='OWNED_MARKER = ".memclaw-owned"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "the per-skill ownership marker already written into every managed "
            "skill dir on disk — the ownership check gates all pruning on it, so "
            "a rename makes every existing dir read as foreign and orphans stop "
            "being collected, permanently and silently"
        ),
    ),
    # -- The plugin's own .env gate and install root. ----------------------------
    #
    # These three outrank everything above them in blast radius, and they were
    # the last to be pinned, for a reason worth writing down: all three sit on
    # lines that already carried an exemption marker.
    #
    # The marker and this list do different jobs. The marker tells the ratchet
    # "this old-brand text is deliberate, stop failing the build". It says
    # nothing about DELETING the line, and a sweep that deletes it lowers the
    # file's count, which the ratchet reads as progress. So the three lines with
    # the largest consequence in the whole plugin were annotated in a way that
    # looks like protection and is not. An exemption is a statement of intent;
    # only an entry here is a guard.
    #
    # If a future dual-read alias is added anywhere, it wants both.
    Sentinel(
        path="plugin/src/env.ts",
        text="/^(?:CAURA|MEMCLAW)_[A-Z_]+$/",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "the gate deciding which .env keys may reach process.env — drop the "
            "legacy alternative and every existing install silently stops loading "
            "its api key, tenant, agent and fleet id, then starts up behaving "
            "like a fresh install with nothing configured"
        ),
    ),
    # Anchored through ``.test(key)`` deliberately. The bare prefix pattern is a
    # SUBSTRING of the stricter regex two functions above, so pinning it alone
    # would still pass on a tree where this function had been deleted and only
    # the stricter one survived — a pin satisfied by the wrong line.
    Sentinel(
        path="plugin/src/env.ts",
        text="/^(?:CAURA|MEMCLAW)_/.test(key)",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "deploy.ts filters the operator's existing .env through this and then "
            "writes the survivors back as the WHOLE file — drop the legacy "
            "alternative and the next redeploy ERASES every legacy-prefixed key "
            "from a file the plugin explicitly does not own the contents of"
        ),
    ),
    Sentinel(
        path="plugin/src/paths.ts",
        text='join(getOpenClawBaseDir(), "plugins", "memclaw")',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks=(
            "the install root on every user's disk — the .env, install.json, "
            "dist/ and shared-skill paths all derive from it. The server's copy "
            "of this same path is pinned separately, so renaming one side alone "
            "leaves a server writing where the plugin no longer reads"
        ),
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

    One argument per call, by position: index 0, or index 1 for ``logger.log``
    where the level takes the first slot. Reading every positional argument
    instead — which this did until a %-substitution value was found able to
    satisfy a check for a message that had been renamed — is the wider net that
    catches the wrong fish.
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
        # The message is the first positional argument — except for
        # ``logger.log(level, msg)``, where the level takes that slot. Reading
        # every argument instead would let a %-substitution VALUE satisfy the
        # check for a call whose message text had changed, which is the exact
        # false pass this kind exists to prevent.
        index = 1 if node.func.attr == "log" else 0
        if len(node.args) > index:
            text = _static_text(node.args[index])
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

    if sentinel.kind != LOG_MESSAGE:
        # Not defensive padding: a mistyped kind would otherwise fall through to
        # the log-message path and quietly check the wrong thing, on an entry
        # whose author believed they had written a literal one. A gate running
        # the wrong check is worse than one that refuses to run.
        raise RuntimeError(f"unknown kind {sentinel.kind!r} on {sentinel.path}")

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
    try:
        failures = [(s, why) for s in SENTINELS if (why := _check(s, root)) is not None]
    except RuntimeError as exc:
        # A file this gate cannot read is a gate that did not run, which is not
        # the same as a gate that found something — and a traceback reads as
        # neither. Exit 2 says it could not run; exit 1 always means it ran.
        print(exc, file=sys.stderr)
        return 2

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
