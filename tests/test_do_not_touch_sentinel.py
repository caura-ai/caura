"""The other half of hard rule 4: strings that must survive a sweep.

``scripts/legacy_name_ratchet.py`` fails a file whose old-brand count goes up.
``scripts/do_not_touch_sentinel.py`` fails a change that removes a string
something outside this repo depends on — the direction the ratchet reads as
progress.

The tests below come in two halves. The first builds synthetic files and checks
the mechanism. The second is pointed at the real list and asserts that every
entry in it actually bites: an entry whose string cannot be removed is a line of
list that looks like protection and is not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "do_not_touch_sentinel.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import do_not_touch_sentinel as sentinel_module
from do_not_touch_sentinel import (
    LITERAL,
    LOG_MESSAGE,
    SENTINELS,
    Sentinel,
    _check,
)


def _root(tmp_path: Path, path: str, body: str) -> Path:
    """A throwaway tree holding one file at ``path``."""
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return tmp_path


# ── the mechanism ────────────────────────────────────────────────────────────


def test_a_surviving_literal_passes(tmp_path: Path) -> None:
    root = _root(tmp_path, "a.py", 'KEY = "keep-me"\n')

    assert _check(Sentinel("a.py", "keep-me", LITERAL, "x"), root) is None


def test_a_removed_literal_fails(tmp_path: Path) -> None:
    root = _root(tmp_path, "a.py", 'KEY = "renamed"\n')

    assert (
        _check(Sentinel("a.py", "keep-me", LITERAL, "x"), root) == "the string is gone"
    )


def test_a_missing_file_fails_rather_than_skipping(tmp_path: Path) -> None:
    """A gate that skips what it cannot find passes every PR once the file moves,
    and moving the file is one of the ways the string goes away."""
    result = _check(Sentinel("gone.py", "keep-me", LITERAL, "x"), tmp_path)

    assert result == "the file no longer exists"


# ── the log-message kind, and why it is not a substring check ────────────────


def test_a_log_message_still_emitted_passes(tmp_path: Path) -> None:
    root = _root(tmp_path, "a.py", 'logger.error("Widget degraded: %d", n)\n')

    assert _check(Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"), root) is None


def test_a_phrase_surviving_only_in_prose_fails(tmp_path: Path) -> None:
    """The case that makes this kind worth having, and it is not hypothetical.

    ``common/embedding/_service.py`` discusses all three of its alert phrases in
    its own comments as well as emitting them, so a plain text search passes on a
    tree where the ``logger.error`` call is gone and only the commentary about it
    survives. That tree is exactly the one that kills the monitor: a log filter
    matches emitted messages, and a comment emits nothing.
    """
    root = _root(
        tmp_path,
        "a.py",
        '# the streak drives "Widget degraded", which alerting matches on\n'
        'logger.error("Widget unhealthy: %d", n)\n',
    )

    result = _check(Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"), root)

    assert result == "it survives only in prose — no logging call emits it any more"


def test_a_message_split_across_source_lines_still_matches(tmp_path: Path) -> None:
    """All three real phrases are written as adjacent literals wrapped for line
    length. The parser joins them before we see them; a line-based reader would
    not, and would fail every one of them on a tree where nothing is wrong."""
    root = _root(
        tmp_path,
        "a.py",
        'logger.error(\n    "Widget degraded [%s]: %d consecutive "\n    "failures",\n    a,\n    n,\n)\n',
    )

    assert _check(Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"), root) is None


def test_the_level_first_logging_form_is_read_too(tmp_path: Path) -> None:
    """``logger.log(level, msg)`` puts the message second, so keying on argument
    position would miss it silently. The phrase still counts as emitted."""
    root = _root(tmp_path, "a.py", 'logger.log(logging.ERROR, "Widget degraded")\n')

    assert _check(Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"), root) is None


def test_the_level_first_form_cannot_satisfy_a_level_floor(tmp_path: Path) -> None:
    """Its level is an argument, and the one real call of this shape in the repo
    picks it with a conditional. There is no static answer, so the gate says it
    cannot check rather than assuming either way — and asks for the form it can.
    """
    root = _root(tmp_path, "a.py", 'logger.log(logging.ERROR, "Widget degraded")\n')

    result = _check(
        Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x", min_level="error"), root
    )

    assert result is not None
    assert "logger.log" in result


def test_an_fstring_message_still_matches(tmp_path: Path) -> None:
    """An f-string's literal segments are still emitted verbatim.

    A false failure is the safe direction, but one that reports "the phrase is
    gone" about a phrase that is right there would send the reader hunting for a
    deletion that never happened, mid-sweep, when they are least able to spare it.
    """
    root = _root(tmp_path, "a.py", 'logger.error(f"Widget degraded: {reason}")\n')

    assert _check(Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"), root) is None


def test_a_phrase_broken_up_by_an_interpolation_does_not_match(tmp_path: Path) -> None:
    """The limit of the above, and the correct one: a substring filter would not
    find that message either, so the monitor is broken and the gate should say so."""
    root = _root(tmp_path, "a.py", 'logger.error(f"Widget {kind} degraded")\n')

    assert (
        _check(Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"), root) is not None
    )


# ── severity, which the phrase alone does not pin ────────────────────────────


def test_a_downgraded_level_fails_even_though_the_phrase_survives(
    tmp_path: Path,
) -> None:
    """The gap a phrase-only check leaves wide open.

    The Datadog filter does not select on severity, which is exactly why the
    level has to be pinned here rather than left to the monitor: what the level
    decides is whether the line is emitted at all. Downgrade the call to
    ``debug`` and the phrase is intact, the filter would match it, and it never
    reaches Cloud Logging to be matched.
    """
    root = _root(tmp_path, "a.py", 'logger.debug("Widget degraded: %d", n)\n')

    result = _check(
        Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x", min_level="error"), root
    )

    assert result is not None
    assert "below error" in result


def test_raising_the_level_is_not_a_regression(tmp_path: Path) -> None:
    """A minimum, not a set. More severe still reaches the sink."""
    root = _root(tmp_path, "a.py", 'logger.critical("Widget degraded")\n')

    sentinel = Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x", min_level="error")

    assert _check(sentinel, root) is None


def test_exception_counts_as_error(tmp_path: Path) -> None:
    """``logger.exception`` is ``error`` with a traceback, and logs at ERROR."""
    root = _root(tmp_path, "a.py", 'logger.exception("Widget degraded")\n')

    sentinel = Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x", min_level="error")

    assert _check(sentinel, root) is None


def test_a_phrase_in_a_non_logging_call_does_not_count(tmp_path: Path) -> None:
    """Otherwise any string anywhere satisfies the strictest kind in the list."""
    root = _root(tmp_path, "a.py", 'print("Widget degraded")\n')

    result = _check(Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"), root)

    assert result == "it survives only in prose — no logging call emits it any more"


def test_a_file_that_does_not_parse_fails_loudly(tmp_path: Path) -> None:
    """Fail closed. An unparseable file yields no messages, which would otherwise
    read as "the phrase is gone" — right answer, wrong reason, and it would send
    the reader looking for a deleted string that is still there."""
    root = _root(tmp_path, "a.py", "def broken(:\n")

    with pytest.raises(RuntimeError, match="does not parse"):
        _check(Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"), root)


# ── the real list ────────────────────────────────────────────────────────────


def test_the_real_list_passes_against_this_tree() -> None:
    """If this fails, the change under review removed something load-bearing."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("sentinel", SENTINELS, ids=lambda s: f"{s.path}:{s.text}")
def test_every_listed_string_is_actually_load_bearing(
    sentinel: Sentinel, tmp_path: Path
) -> None:
    """Every entry must bite when its string goes — no vacuous rows.

    A row naming a string the file does not contain, or one that survives its own
    removal, is worse than no row: the list is what the sweep is checked against,
    so a dead entry reads as coverage while protecting nothing. Removing the
    string is the only way to tell the difference, so the test removes it.
    """
    body = (REPO_ROOT / sentinel.path).read_text(encoding="utf-8", errors="replace")
    root = _root(tmp_path, sentinel.path, body)

    assert _check(sentinel, root) is None, "the string is not in the file it names"

    scrubbed = _root(
        tmp_path / "scrubbed", sentinel.path, body.replace(sentinel.text, "X")
    )

    assert _check(sentinel, scrubbed) is not None


_COMMENT_STARTS = ("#", "//", "*")
# SQL's marker needs its trailing space to tell ``-- a note`` from a command-line
# flag. Without it a pinned ``--update-labels=…`` reads as a comment and the
# guard fails the very entry it exists to protect. No OSS entry starts with a
# dash today; the enterprise copy has several, and found this.
_SQL_COMMENT = "-- "


def _is_comment_only(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(_COMMENT_STARTS) or stripped.startswith(_SQL_COMMENT)


@pytest.mark.parametrize(
    "sentinel", [s for s in SENTINELS if s.kind == LITERAL], ids=lambda s: s.path
)
def test_no_literal_can_be_satisfied_by_a_comment_alone(sentinel: Sentinel) -> None:
    """A LITERAL entry must pin functional syntax, not a name prose also mentions.

    This is the LOG_MESSAGE prose problem in the other kind, and it is not
    hypothetical — it is why several of these entries pin an expression rather
    than a bare name. ``mcp_server.py`` names the tool-alias prefix in three
    comments besides the two lines that implement the shim, and ``app.py``
    describes the keystones route in the comment directly above the mount.
    Pinning the bare name in either would have passed a tree where the shim and
    the mount were both deleted and only the commentary was left.

    Pinning the surrounding expression instead — the ``startswith`` test, the
    ``prefix=`` keyword — makes the entry unsatisfiable by prose, so no separate
    mechanism has to enforce that at runtime. This test is what keeps it true as
    entries are added.
    """
    lines = (REPO_ROOT / sentinel.path).read_text(encoding="utf-8").splitlines()
    in_comment = [
        line.strip()
        for line in lines
        if sentinel.text in line and _is_comment_only(line)
    ]

    assert not in_comment, (
        f"{sentinel.path} mentions {sentinel.text!r} in a comment, so deleting the "
        f"code that uses it would still pass: {in_comment}"
    )


def test_main_turns_an_unreadable_file_into_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file the gate cannot read means the gate did not run — which is not the
    same as the gate finding something, and a traceback reads as neither. Exit 2
    says it could not run; exit 1 always means it ran and something is missing.

    Driven through a substituted list rather than the real one, so the case is
    identical in every copy of this file: only a LOG_MESSAGE entry reaches the
    parser, and not every repo's list has one.
    """
    root = _root(tmp_path, "a.py", "def broken(:\n")
    monkeypatch.setattr(
        sentinel_module,
        "SENTINELS",
        (Sentinel("a.py", "Widget degraded", LOG_MESSAGE, "x"),),
    )
    monkeypatch.setattr(sys, "argv", ["do_not_touch_sentinel.py", "--root", str(root)])

    assert sentinel_module.main() == 2
    assert "does not parse" in capsys.readouterr().err


def test_the_list_is_not_empty() -> None:
    """An empty list is a gate that passes everything while reporting green."""
    assert SENTINELS


def test_listing_the_strings_never_fails() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--list"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert f"{len(SENTINELS)} protected strings" in result.stdout


def test_a_removal_names_the_file_and_what_breaks(tmp_path: Path) -> None:
    """The report has to be actionable from CI output alone: which string, and
    what stops working. The reader is mid-sweep and has no other context."""
    sentinel = SENTINELS[0]
    body = (REPO_ROOT / sentinel.path).read_text(encoding="utf-8", errors="replace")
    root = _root(tmp_path, sentinel.path, body.replace(sentinel.text, "X"))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert sentinel.path in result.stdout
    # repr, because that is how the report prints it — a pinned string containing
    # a backslash (a terraform filter, say) never appears verbatim in the output.
    assert repr(sentinel.text) in result.stdout
    assert sentinel.breaks in result.stdout
