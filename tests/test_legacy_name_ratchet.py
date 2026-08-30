"""The CI gate for hard rule 7 of the sunset plan: mint nothing new with the old brand.

The script lives at ``scripts/legacy_name_ratchet.py`` and compares the legacy-name
line count per file between the tree being built and a base tree, failing on any
file that went up.

These tests build a throwaway git repository per case and run the real script
against it. Mocking ``git grep`` would test the arithmetic and none of the part
that actually breaks — the output parsing, the tree-ish prefix, and the
case-insensitive match are all git's behaviour, not ours.
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = [pytest.mark.unit]

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "legacy_name_ratchet.py"

# Assembled rather than written out, so this file does not itself carry the
# literal the gate scans for and need exempting.
LEGACY = "mem" + "claw"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one committed file that already carries the legacy name.

    A pre-existing occurrence is the realistic baseline: the gate's whole job is
    to hold a large existing footprint flat, not to demand zero.
    """
    r = tmp_path / "scratch"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "existing.py").write_text(f'URL = "https://{LEGACY}.net"\n')
    (r / "clean.py").write_text("VALUE = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _run(
    repo: Path, *extra: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run the script; ``env`` entries are merged over the inherited environment.

    ``GITHUB_HEAD_REF`` is stripped from the inherited environment first. The
    suite itself runs under Actions on pull_request builds, where that variable
    names whatever branch the PR happens to be — including release-please's own,
    which is exactly when its CHANGELOG exemption tests would otherwise flip.
    Branch context is simulated explicitly via ``env``, never inherited.
    """
    merged = {k: v for k, v in os.environ.items() if k != "GITHUB_HEAD_REF"}
    merged.update(env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD", *extra],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,  # a non-zero exit is the thing under test
        env=merged,
    )


def _stage(repo: Path, name: str, body: str) -> None:
    """Write and stage — ``git grep`` on the working tree reads the index, so an
    untracked file is invisible to it and would make every test vacuously pass."""
    (repo / name).write_text(body)
    _git(repo, "add", name)


# ── the rule ─────────────────────────────────────────────────────────────────


def test_a_new_file_carrying_the_name_fails(repo: Path) -> None:
    _stage(repo, "new.py", f'KEY = "{LEGACY.upper()}_API_KEY"\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "new.py" in result.stdout
    assert "(0 -> 1)" in result.stdout


def test_an_existing_file_gaining_a_line_fails(repo: Path) -> None:
    _stage(
        repo, "existing.py", f'URL = "https://{LEGACY}.net"\nKEY = "{LEGACY}-api-key"\n'
    )

    result = _run(repo)

    assert result.returncode == 1
    assert "(1 -> 2)" in result.stdout


def test_the_name_is_matched_case_insensitively(repo: Path) -> None:
    """Every casing of the brand is the same rule — prose, code and env names."""
    _stage(repo, "new.py", f"# {LEGACY.capitalize()}Client is the old name\n")

    assert _run(repo).returncode == 1


# ── the escape hatch, which rule 3 requires ──────────────────────────────────


def test_a_marked_line_is_exempt(repo: Path) -> None:
    _stage(
        repo,
        "new.py",
        f'ALIAS = "{LEGACY}_write"  # legacy-name-ok: permanent tool shim\n',
    )

    result = _run(repo)

    assert result.returncode == 0, result.stdout
    assert "No new lines" in result.stdout


def test_the_marker_needs_no_reason_to_work(repo: Path) -> None:
    """A reason is asked for, not enforced. End-of-line is a boundary too, so a
    bare marker still exempts rather than failing on a technicality."""
    _stage(repo, "new.py", f'ALIAS = "{LEGACY}_write"  # legacy-name-ok\n')

    assert _run(repo).returncode == 0


def test_the_marker_must_be_a_whole_token(repo: Path) -> None:
    """A bare substring test is a hole, not a nuisance.

    Prose that merely contains the marker inside a longer word — "not
    legacy-name-okay to leave in" — would silence a real occurrence on that line,
    and the more the marker is written about, the likelier it gets typed.
    """
    _stage(repo, "new.py", f'KEY = "{LEGACY}"  # not legacy-name-okay to leave in\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "new.py" in result.stdout


def test_the_marker_must_not_be_glued_to_the_token_before_it(repo: Path) -> None:
    """The mirror of the whole-token rule, and a separate check.

    A right-hand boundary alone stops ``legacy-name-okay`` but not
    ``somelegacy-name-ok``, which nobody wrote as an annotation and which would
    exempt the line just the same.
    """
    _stage(repo, "new.py", f'KEY = "{LEGACY}"  # somelegacy-name-ok\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "new.py" in result.stdout


def test_the_marker_is_recognised_whatever_its_casing(repo: Path) -> None:
    """The name is matched case-insensitively; the marker matching it
    case-sensitively would fail a line somebody deliberately annotated."""
    _stage(
        repo, "new.py", f'ALIAS = "{LEGACY}_write"  # Legacy-Name-OK: permanent shim\n'
    )

    assert _run(repo).returncode == 0


def test_the_marker_exempts_only_its_own_line(repo: Path) -> None:
    """Otherwise one marker at the top of a file would silence the whole file."""
    _stage(
        repo,
        "new.py",
        f'ALIAS = "{LEGACY}_write"  # legacy-name-ok: shim\nOTHER = "{LEGACY}-service"\n',
    )

    result = _run(repo)

    assert result.returncode == 1
    # Scoped to the offenders section: the exemption report above it names the
    # marked line on purpose, so a bare "not in stdout" would be testing the
    # wrong thing now.
    offenders = result.stdout.split("adds the legacy name in", 1)[1]
    assert "OTHER" in offenders
    assert "ALIAS" not in offenders


# ── the floor marker: same exemption, a different claim ──────────────────────
#
# ``legacy-name-floor`` exempts a line exactly as ``legacy-name-ok`` does. The
# gate cannot tell them apart and no build changes colour because of which one is
# used; only the report reads the difference. It exists because the two
# populations grow from different work — an alias arrives when something is
# renamed, a floor mention whenever a document mentioning the product's own name
# is EDITED — so under one marker the mentions bury the aliases. Measured on
# caura-daemon#136: eleven exemptions in one PR, four of them aliases.


# One regex serves both markers, with the boundaries outside the alternation, so
# there is no second code path for a per-marker copy of these to cover — a
# loosened bound loosens both at once. These are parametrized rather than cloned
# so that is what gets asserted, and so a third marker is one list entry.
#
# The ``legacy-name-ok`` cases above are deliberately left standing and unedited.
# They pin that marker's own contract independently of this table, which is worth
# keeping on a gate where the whole promise is that nothing about the existing
# marker changed.
class _Marker(NamedTuple):
    """One marker and the two things a boundary test needs beside it."""

    marker: str
    near_miss: str
    label: str


_MARKERS = [
    _Marker("legacy-name-ok", "legacy-name-okay", "compat alias(es)"),
    _Marker("legacy-name-floor", "legacy-name-floored", "floor mention(s)"),
]
_EACH = pytest.mark.parametrize("m", _MARKERS, ids=lambda m: m.marker)

# Comfortably above the script's ``_EXEMPTION_GROUP_AT``, so a list of this many
# identical reasons is collapsed to a "Nx <reason>" header rather than printed
# line by line.
_GROUPED = 6


@_EACH
def test_every_marker_exempts_its_own_line(repo: Path, m: _Marker) -> None:
    """The whole premise: a marker exempts, or it is not a marker."""
    _stage(repo, "cli.md", f"    {LEGACY} setup --non-interactive  # {m.marker}: why\n")

    result = _run(repo)

    assert result.returncode == 0, result.stdout
    assert "No new lines" in result.stdout


@_EACH
def test_every_marker_needs_no_reason_to_work(repo: Path, m: _Marker) -> None:
    """Asked for, not enforced — and end-of-line is a boundary, so a bare marker
    still exempts rather than failing on a technicality."""
    _stage(repo, "cli.md", f"    {LEGACY} setup  # {m.marker}\n")

    assert _run(repo).returncode == 0


@_EACH
def test_every_marker_must_be_a_whole_token(repo: Path, m: _Marker) -> None:
    """The right-hand bound, asserted for every marker at once.

    One pattern matches them all, so a loosened lookahead silently loosens every
    marker — which is exactly the shape of hole a per-marker test cannot see,
    because it passes for the marker it names while the other is already open.
    """
    _stage(repo, "new.py", f'KEY = "{LEGACY}"  # not {m.near_miss} to leave in\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "new.py" in result.stdout


@_EACH
def test_every_marker_must_not_be_glued_to_the_token_before_it(
    repo: Path, m: _Marker
) -> None:
    """The mirror bound, and a separate check: a right-hand boundary alone stops
    ``legacy-name-okay`` but not ``somelegacy-name-ok``."""
    _stage(repo, "new.py", f'KEY = "{LEGACY}"  # some{m.marker}\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "new.py" in result.stdout


@_EACH
def test_every_marker_is_classified_whatever_its_casing(repo: Path, m: _Marker) -> None:
    """Matched case-insensitively, so a line somebody deliberately annotated is
    never failed on its capitalisation.

    The exit code is the weaker half and on its own is nearly worthless. A kind
    that does not normalise back to a marker literal still exempts the line — it
    is not ``None`` — so the gate stays green while the report, which looks the
    kind up to decide which list it belongs in, finds no match and drops the line
    silently. Exempt but invisible is the one outcome this report exists to
    prevent, so the classification is asserted too. Parametrizing extends that
    assertion to ``legacy-name-ok``, whose own casing test above checks only the
    exit code.
    """
    _stage(repo, "cli.md", f"    {LEGACY} setup  # {m.marker.upper()}: the command\n")

    result = _run(repo)

    assert result.returncode == 0, result.stdout
    assert f"1 {m.label} — " in result.stdout
    assert "cli.md:1" in result.stdout


@_EACH
def test_an_oddly_cased_marker_still_yields_its_reason(repo: Path, m: _Marker) -> None:
    """The reason is sliced off the winning marker's own match, so that match has
    to be found under any casing.

    Asserted through the GROUPED path on purpose. Below the grouping threshold
    the report prints each line verbatim, and the line contains its own reason
    text — so a reason that failed to parse is invisible, and an assertion there
    passes whether the slicing works or not. Only the grouped header prints the
    parsed reason on its own, which is where a silent degrade to
    "(no reason given)" can actually be seen.
    """
    _stage(
        repo,
        "many.py",
        "".join(
            f'A{i} = "{LEGACY}_x"  # {m.marker.upper()}: a permanent name\n'
            for i in range(_GROUPED)
        ),
    )

    out = _run(repo).stdout

    assert f"{_GROUPED}x  a permanent name" in out
    assert "(no reason given)" not in out


@pytest.mark.parametrize(
    "line_suffix",
    [
        "# legacy-name-ok: dual-read alias  legacy-name-floor: the command",
        "# legacy-name-floor: the command  legacy-name-ok: dual-read alias",
    ],
    ids=["alias-first", "floor-first"],
)
def test_a_doubly_marked_reason_stops_at_the_next_marker(
    repo: Path, line_suffix: str
) -> None:
    """A reason is bounded by the NEXT marker, not by end of line.

    Both orders, because the bug only appears in one of them: with the winning
    marker written first, slicing to end-of-line swallows the other marker's
    literal and its reason too. The grouped header is where that shows, so this
    writes enough identical lines to trip the grouping — and the reason is also
    the grouping KEY, so a polluted one stops identical claims collapsing
    together, which is what the grouping exists to do.
    """
    _stage(
        repo,
        "many.py",
        "".join(f'A{i} = "{LEGACY}_x"  {line_suffix}\n' for i in range(_GROUPED)),
    )

    out = _run(repo).stdout

    assert f"{_GROUPED}x  dual-read alias" in out
    # The other marker's literal must not have bled into this one's reason.
    assert "dual-read alias  legacy-name-floor" not in out
    assert "the command  legacy-name-ok" not in out


def test_a_doubly_marked_line_is_filed_as_an_alias_whatever_the_order(
    repo: Path,
) -> None:
    """Precedence is by table order, not by position on the line.

    28 lines across the fleet make both claims at once — an image tag whose
    repository name is frozen while its version is dual-read, say — so which
    marker an author reaches for is a real question rather than a degenerate
    one. Breaking the tie by whichever was typed first makes the report depend
    on line order; breaking it toward the alias means the expensive mistake
    (a rule 3 decision filed where nobody is looking for it) cannot happen.

    The floor marker is written FIRST here deliberately: with positional
    precedence this line files as a floor mention and the assertion below fails.
    """
    _stage(
        repo,
        "cli.md",
        f"    {LEGACY} setup  # legacy-name-floor: command  legacy-name-ok: alias\n",
    )

    result = _run(repo)

    assert result.returncode == 0, result.stdout
    assert "1 compat alias(es) — " in result.stdout
    assert "floor mention(s)" not in result.stdout
    # The reason quoted is the winning marker's own, not the other one's.
    assert "alias" in result.stdout


def test_a_floor_marker_buys_no_headroom_either(repo: Path) -> None:
    """Rule 7 stays armed, and the new marker is not a softer way past it.

    The exempt-and-add swap, run through the floor marker: mark one line, fill
    the freed slot with a new unmarked one. It fails for the same reason the
    older marker's version does — the text comparison charges non-exempt text the
    repo never had, and the marker's kind is not part of that comparison.
    """
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: the served mirror path\n'
        f'SNUCK = "{LEGACY}-new-service"\n',
    )

    result = _run(repo)

    assert result.returncode == 1
    offenders = result.stdout.split("adds the legacy name in", 1)[1]
    assert "SNUCK" in offenders


def test_the_report_counts_the_two_kinds_apart(repo: Path) -> None:
    """The change's whole purpose, in the shape caura-daemon#136 landed.

    Undifferentiated, eleven exemptions are a wall a reviewer scrolls past — the
    same failure the reason-grouping exists to prevent, one level up. The counts
    have to be separable at a glance or the split bought nothing.
    """
    _stage(
        repo,
        "env.md",
        f"Also read as `{LEGACY.upper()}_HOME`.<!-- legacy-name-ok: rule 3 dual-read alias -->\n"
        f"    {LEGACY} policy show  <!-- legacy-name-floor: the command name -->\n"
        f"    {LEGACY} uninstall  <!-- legacy-name-floor: the command name -->\n",
    )

    out = _run(repo).stdout

    # The total still counts every kind, so it can never disagree with the lists.
    assert "3 exempt line(s) written by this change in 1 file(s)" in out
    assert "1 compat alias(es), 2 floor mention(s)." in out
    assert "1 compat alias(es) — rule 3's escape hatch" in out
    assert "2 floor mention(s) — a permanent name in text" in out


def test_aliases_are_listed_before_floor_mentions(repo: Path) -> None:
    """Fixed order, not sorted by size.

    Mentions outnumber aliases in the ordinary case — that is the whole reason
    the split exists — so a count-ordered report would put the four lines rule 3
    wants eyes on underneath six that it does not. Position must not depend on a
    tally.
    """
    _stage(
        repo,
        "env.md",
        f"Also read as `{LEGACY.upper()}_HOME`.<!-- legacy-name-ok: the one alias -->\n"
        + "".join(
            f"    {LEGACY} verb{i}  <!-- legacy-name-floor: a command name -->\n"
            for i in range(3)
        ),
    )

    out = _run(repo).stdout

    assert out.index("compat alias(es) — ") < out.index("floor mention(s) — ")


def test_one_kind_alone_gets_no_redundant_split_line(repo: Path) -> None:
    """Most PRs write one exemption of one kind. Printing "1 compat alias(es)"
    twice running — once as the split, once as the section header — reads as a
    form rather than a finding, and the section header already says it."""
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-ok: gateway mirror\n',
    )

    out = _run(repo).stdout

    assert "1 exempt line(s) written by this change in 1 file(s)" in out
    assert "1 compat alias(es) — rule 3's escape hatch" in out
    # The comma-joined split line, which only earns its place with two kinds.
    # Matched as a WHOLE line: a substring test would separate it from the
    # section header by nothing but the trailing "." versus " —", and would
    # start misfiring the moment that punctuation is reworded.
    assert "1 compat alias(es)." not in out.splitlines()


def test_a_removed_floor_mention_is_still_reported(repo: Path) -> None:
    """Removal is reported for both kinds, under guidance true of both.

    A floor mention has no alias to survive, so the old wording — "confirm each
    alias still exists" — would be a false instruction on a real removal, which
    is precisely the corruption this marker split exists to stop.
    """
    line = f"    {LEGACY} uninstall  # legacy-name-floor: the command name\n"
    (repo / "cli.md").write_text(line)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add a floor mention")
    _stage(repo, "cli.md", "    caura uninstall\n")

    result = _run(repo)

    assert result.returncode == 0
    assert "exempt line(s) removed" in result.stdout
    assert "the command name" in result.stdout


# ── what must NOT fail ───────────────────────────────────────────────────────


def test_removing_a_line_passes_and_reports_progress(repo: Path) -> None:
    """Decreases are the point of the programme, so they are reported, not merely allowed."""
    _stage(repo, "existing.py", 'URL = "https://caura.ai"\n')

    result = _run(repo)

    assert result.returncode == 0
    assert (
        "No new lines. 0 annotated, 1 removed, 0 excused moves (-1 net)."
        in result.stdout
    )


def test_annotating_a_line_is_not_reported_as_removing_it(repo: Path) -> None:
    """A marker changes the ratchet count, but the underlying line remains."""
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )

    result = _run(repo)

    assert result.returncode == 0
    assert (
        "No new lines. 1 annotated, 0 removed, 0 excused moves (-1 net)."
        in result.stdout
    )


def test_an_unrelated_marked_line_does_not_disguise_a_removal(repo: Path) -> None:
    (repo / "existing.py").write_text(f"{LEGACY}\n")
    _git(repo, "add", "existing.py")
    _git(repo, "commit", "-qm", "use a short legacy line")
    _stage(
        repo,
        "existing.py",
        f'NEW = "{LEGACY}-cli"  # legacy-name-ok: permanent command alias\n',
    )

    result = _run(repo)

    assert result.returncode == 0
    assert (
        "No new lines. 0 annotated, 1 removed, 0 excused moves (-1 net)."
        in result.stdout
    )


def test_removing_an_old_exemption_does_not_hide_an_annotation(repo: Path) -> None:
    (repo / "existing.py").write_text(
        f'URL = "https://{LEGACY}.net"\n'
        f'ALIAS = "{LEGACY}-cli"  # legacy-name-ok: old alias\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add an exempt alias")
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )

    result = _run(repo)

    assert result.returncode == 0
    assert (
        "No new lines. 1 annotated, 0 removed, 0 excused moves (-1 net)."
        in result.stdout
    )


def test_an_addition_is_not_masked_by_a_deletion_elsewhere(repo: Path) -> None:
    """The reason the gate counts per file rather than per repo.

    One file loses an occurrence while another gains one, so a repo-wide total is
    unchanged and a total-based gate waves it through — during a programme whose
    whole shape is "totals go down", which is exactly when nobody would look.
    """
    _stage(repo, "existing.py", 'URL = "https://caura.ai"\n')
    _stage(repo, "new.py", f'KEY = "{LEGACY}"\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "new.py" in result.stdout


def test_a_move_within_one_file_is_net_zero(repo: Path) -> None:
    """Waves 3-5 move these lines constantly. A per-line diff would flag every
    move as an addition; the per-file count is what keeps the gate usable."""
    _stage(repo, "existing.py", f'# reordered\nURL = "https://{LEGACY}.net"\n')

    assert _run(repo).returncode == 0


def test_a_move_between_two_existing_files_is_net_zero(repo: Path) -> None:
    """#882, and the likelier shape of the two during a consolidation wave.

    The per-file count alone fails this: the destination goes ``0 -> 1`` while
    the source drops, so a change that minted nothing is reported as an
    addition. Rename detection would not have helped — both files already exist,
    so there is no rename for git to find.

    It mattered more than the spurious red because of the remedy the failure text
    offered. None of the three reasons rule 3 recognises describes a move, so the
    only way past the gate was to write an exemption reason that was not true —
    in the one annotation whose whole value is that its reasons are true.
    """
    _stage(repo, "existing.py", "")
    _stage(repo, "moved-here.py", f'URL = "https://{LEGACY}.net"\n')

    result = _run(repo)

    assert result.returncode == 0, result.stdout


def test_a_whole_file_rename_is_net_zero(repo: Path) -> None:
    """The other move shape, which rename detection would also have covered."""
    _git(repo, "rm", "-q", "existing.py")
    _stage(repo, "renamed.py", f'URL = "https://{LEGACY}.net"\n')

    assert _run(repo).returncode == 0


def test_a_renamed_file_whose_other_lines_changed_is_still_a_move(repo: Path) -> None:
    """The real case this was blocking, from caura-enterprise's 1.4 rename.

    An old-brand-named entrypoint script was renamed to its Caura name by
    ``git mv``, and the same commit renamed variables inside it. The branded lines
    themselves were untouched; everything around them moved.

    Rename detection would have had to survive a similarity threshold to see this,
    and a move-plus-edit is exactly where that threshold is in play. Comparing the
    text of the branded lines does not care: those lines are byte-identical, so
    they pair with their own deletions whatever happened to their neighbours.
    """
    _git(repo, "rm", "-q", "existing.py")
    _stage(
        repo,
        "renamed.py",
        f'RENAMED_NEIGHBOUR = 1\nURL = "https://{LEGACY}.net"\nANOTHER_EDIT = 2\n',
    )

    assert _run(repo).returncode == 0


def test_a_reindented_move_is_still_a_move(repo: Path) -> None:
    """A line moving into a class body or a deeper block is re-indented on the
    way. Comparing text with its leading whitespace would call that new."""
    _stage(repo, "existing.py", "")
    _stage(repo, "moved-here.py", f'class C:\n    URL = "https://{LEGACY}.net"\n')

    assert _run(repo).returncode == 0


def test_a_move_does_not_launder_an_addition_beside_it(repo: Path) -> None:
    """The obvious way to abuse move detection, and the reason it compares text
    rather than merely totals: the moved line pairs with its own deletion, the
    new one has nothing to pair with, so only the new one is named."""
    _stage(repo, "existing.py", "")
    _stage(
        repo,
        "moved-here.py",
        f'URL = "https://{LEGACY}.net"\nSNUCK = "{LEGACY}-new-service"\n',
    )

    result = _run(repo)

    assert result.returncode == 1
    # Scoped to the offenders section: the excused-moves report above it names
    # the moved line on purpose, so a bare "not in stdout" would be testing the
    # wrong thing.
    offenders = result.stdout.split("adds the legacy name in", 1)[1]
    assert "SNUCK" in offenders
    # The moved line is not why this failed, and pointing at it there sends the
    # reader to "fix" a line that was already in the tree.
    assert "URL" not in offenders


def test_identical_added_lines_are_all_named_with_the_split_stated(repo: Path) -> None:
    """A file can gain more copies of a text than were minted.

    Two identical lines move in and a third is added. All three are lines this
    change added, and they are byte-identical, so which one is the mint cannot be
    determined — naming one would be a guess dressed as a finding. All three are
    printed and the split is stated instead.

    An earlier version spent a text-keyed budget and named whichever occurrence
    came first in the file. That is worse than imprecise: where the file ALREADY
    held the text it names a line nobody touched, which the next test pins.
    """
    # Base holds two copies: the fixture's existing.py, plus a second file.
    _stage(repo, "second.py", f'URL = "https://{LEGACY}.net"\n')
    _git(repo, "commit", "-qm", "a second copy in the base")

    # Both are emptied and three copies appear in one new file: two moved, one new.
    _stage(repo, "existing.py", "")
    _stage(repo, "second.py", "")
    _stage(repo, "gathered.py", f'URL = "https://{LEGACY}.net"\n' * 3)

    result = _run(repo)
    offenders = result.stdout.split("adds the legacy name in", 1)[1]

    assert result.returncode == 1
    assert "(0 -> 3)" in offenders
    # All three are lines this change added, so all three are named...
    assert offenders.count('URL = "https://') == 3
    # ...and the report says how many of them are actually new.
    assert "3 added lines share this text; 1 new, 2 moved in" in offenders


def test_one_deletion_pays_for_only_one_of_two_destinations(repo: Path) -> None:
    """The repo-wide charge is spent once, not offered to every file that grew.

    One copy leaves ``existing.py`` and a copy appears in each of two new files.
    The repo gained one, so exactly one destination is a mint and the other is a
    move. Charging each file against a freshly recomputed repo-wide figure hands
    both the same budget and reports two additions for one — failing a legitimate
    multi-destination move, which is the shape a consolidation wave produces.
    """
    _stage(repo, "existing.py", "")
    _stage(repo, "one.py", f'URL = "https://{LEGACY}.net"\n')
    _stage(repo, "two.py", f'URL = "https://{LEGACY}.net"\n')

    result = _run(repo)
    offenders = result.stdout.split("adds the legacy name in", 1)[1]

    assert result.returncode == 1
    assert "in 1 file(s)" in result.stdout
    # Whichever file is charged is arbitrary; that exactly one is charged is not.
    assert offenders.count("(0 -> 1)") == 1
    # And the other is named as the move it is, not silently dropped.
    assert "treated as moved rather than added" in result.stdout


def test_the_line_named_is_one_the_change_added(repo: Path) -> None:
    """The report must never point at a line nobody touched.

    ``existing.py`` already carries the text on line 1 and keeps it. The change
    appends an identical line further down. Both hold the same text, so a
    text-keyed budget spent in file order names line 1 — a line that did not
    change — and never prints the one that did. For a gate whose entire output is
    "here is the line you added", sending the reader to an untouched line is
    worse than saying nothing: they go looking for a mistake that is not there.

    Which physical line the change added is the one question the text cannot
    answer and git's diff can, so the report asks git.
    """
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"\n'
        + "filler = 1\n" * 5
        + f'URL = "https://{LEGACY}.net"\n',
    )

    result = _run(repo)
    offenders = result.stdout.split("adds the legacy name in", 1)[1]

    assert result.returncode == 1
    assert "      7: " in offenders  # the appended line
    assert "      1: " not in offenders  # the untouched one


def test_an_excused_move_is_always_named(repo: Path) -> None:
    """Pairing by text cannot know the deletion it paired with is the same line.

    A change that adds a genuinely new occurrence while coincidentally deleting a
    byte-identical one elsewhere comes out flat and is excused. No count
    separates that from a real move, so — exactly as with the exempt-and-add swap
    — it is reported rather than adjudicated, on the passing path, with the file
    whose deletion paid for it. That attribution is the only thing that makes a
    laundered mint visible to a reviewer.
    """
    _stage(repo, "existing.py", "")
    _stage(repo, "somewhere-unrelated.py", f'URL = "https://{LEGACY}.net"\n')

    result = _run(repo)

    assert result.returncode == 0
    assert "treated as moved rather than added" in result.stdout
    assert "somewhere-unrelated.py" in result.stdout
    assert "was in: existing.py" in result.stdout
    assert (
        "No new lines. 0 annotated, 0 removed, 1 excused move (+0 net)."
        in result.stdout
    )


def test_a_line_moved_while_being_annotated_is_not_removed(repo: Path) -> None:
    _stage(repo, "existing.py", "")
    _stage(
        repo,
        "moved.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )

    result = _run(repo)

    assert result.returncode == 0
    assert (
        "No new lines. 1 annotated, 0 removed, 0 excused moves (-1 net)."
        in result.stdout
    )


def test_a_file_that_kept_its_copy_is_not_named_as_the_source(repo: Path) -> None:
    """The attribution has to name the deletion, not every file with the text.

    ``aaa-bystander.py`` holds an unchanged copy throughout — it contributed
    nothing. Listing sources by containment names it anyway, and because the list
    is truncated and sorted, a bystander that sorts earlier can push the real
    source out of view behind "(+N more)". The one line a reviewer is supposed to
    check then points at the wrong file, which is worse than printing nothing.
    """
    _stage(repo, "aaa-bystander.py", f'URL = "https://{LEGACY}.net"\n')
    _git(repo, "commit", "-qm", "a bystander holding the same text")

    _stage(repo, "existing.py", "")
    _stage(repo, "moved-here.py", f'URL = "https://{LEGACY}.net"\n')

    result = _run(repo)

    assert result.returncode == 0
    assert "was in: existing.py" in result.stdout
    assert "aaa-bystander.py" not in result.stdout


def test_new_text_still_fails_even_though_something_was_deleted(repo: Path) -> None:
    """Strictness is unchanged for text the repo did not have. A deletion
    elsewhere buys no credit unless the added text is the same text."""
    _stage(repo, "existing.py", "")
    _stage(repo, "new.py", f'KEY = "{LEGACY}-something-else"\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "new.py" in result.stdout


def test_a_second_copy_of_an_existing_line_is_an_addition(repo: Path) -> None:
    """A move deletes its source. Keeping the original and adding a copy is not a
    move, and the repo-wide count is what tells them apart."""
    _stage(repo, "copy.py", f'URL = "https://{LEGACY}.net"\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "copy.py" in result.stdout


def test_an_unrelated_change_passes(repo: Path) -> None:
    _stage(repo, "clean.py", "VALUE = 2\n")

    assert _run(repo).returncode == 0


# ── the one thing counting cannot decide ─────────────────────────────────────


def test_a_newly_exempted_line_is_always_named(repo: Path) -> None:
    """An exemption is the only move that buys a file headroom, so it is never
    allowed to be quiet — including on the passing path, which is the path the
    swap below takes."""
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-ok: gateway mirror\n',
    )

    result = _run(repo)

    assert result.returncode == 0
    assert "1 exempt line(s) written by this change" in result.stdout
    assert "existing.py:1" in result.stdout
    assert "gateway mirror" in result.stdout


def _with_two_aliases_already(repo: Path) -> str:
    """Commit a file that already carries two exemptions, and return its body.

    The realistic starting point for this report, and the one the grouping tests
    do not cover: they build files from nothing, where every line is new and no
    filter can be wrong.
    """
    body = (
        f'A = "{LEGACY}-one"  # legacy-name-ok: pre-existing alias one\n'
        f'B = "{LEGACY}-two"  # legacy-name-ok: pre-existing alias two\n'
    )
    (repo / "aliases.py").write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two aliases already here")
    return body


def test_only_the_exemptions_this_change_wrote_are_listed(repo: Path) -> None:
    """The file's exempt count selects the FILE; git's diff selects the LINES.

    Re-greping the file and printing everything it holds puts its whole
    accumulated pile under a header counting one — three lines under a "1", two
    of them not in the diff. Phase 5's dual-read wave is precisely what builds
    those piles, so this is the case that would have degraded fastest: every
    later PR touching such a file reprints the lot, and a reader who learns the
    list is mostly noise stops reading it.
    """
    body = _with_two_aliases_already(repo)
    _stage(
        repo,
        "aliases.py",
        body + f'C = "{LEGACY}-three"  # legacy-name-ok: brand new alias three\n',
    )

    out = _run(repo).stdout

    # The claim, asserted before the header wording so a later rephrasing of the
    # message cannot be what this test is seen to be about.
    assert "pre-existing alias one" not in out
    assert "pre-existing alias two" not in out
    assert "brand new alias three" in out
    assert "1 exempt line(s) written by this change in 1 file(s)" in out


def test_an_edited_exemption_is_listed_though_it_is_not_newly_exempt(
    repo: Path,
) -> None:
    """Why the header counts lines written rather than the rise in the tally.

    Rewriting an existing exemption's reason does not newly exempt anything, but
    it is exactly how a true reason becomes a false one — so the line has to be
    read. Git calls a rewritten line an addition, which is what puts it in scope,
    and the header counts what is printed so the two can never disagree.
    """
    body = _with_two_aliases_already(repo)
    _stage(
        repo,
        "aliases.py",
        body.replace("pre-existing alias two", "actually a pinned wire format")
        + f'C = "{LEGACY}-three"  # legacy-name-ok: brand new alias three\n',
    )

    out = _run(repo).stdout

    # Exempt tally rose by one; two lines were written, and both are shown.
    assert "2 exempt line(s) written by this change in 1 file(s)" in out
    assert "actually a pinned wire format" in out
    assert "brand new alias three" in out
    assert "pre-existing alias one" not in out


def test_a_net_zero_swap_of_exemptions_is_still_reported(repo: Path) -> None:
    """The hole a risen-tally selection leaves, and why git picks the files now.

    Drop one exemption and add another in the same file and the tally is flat, so
    a rise-based filter never examines the file and the new reason is never
    printed. The gate still catches the dangerous form — un-marking a line raises
    the non-exempt count and the text gets charged — but a brand-new reason
    nobody is pointed at is the exact thing this report exists to prevent, and in
    a file already full of exemptions the swap is unremarkable in a diff.
    """
    body = _with_two_aliases_already(repo)
    _stage(
        repo,
        "aliases.py",
        # "alias two" loses its marker and "alias three" arrives with one:
        # exempt 2 -> 2, flat.
        body.replace("  # legacy-name-ok: pre-existing alias two", "")
        + f'C = "{LEGACY}-three"  # legacy-name-ok: brand new alias three\n',
    )

    out = _run(repo).stdout

    assert "brand new alias three" in out
    assert "1 exempt line(s) written by this change in 1 file(s)" in out
    # Still only the written one — widening the file selection must not widen
    # the line list back out to the whole pile.
    assert "pre-existing alias one" not in out


def test_a_net_zero_swap_is_reported_under_a_non_ascii_filename(repo: Path) -> None:
    """Same swap, in a file git will not name plainly.

    ``git diff --name-only`` honours ``core.quotePath`` and returns a path
    holding any non-ASCII byte C-quoted — ``café.py`` comes back as
    ``"caf\\303\\251.py"``, quotes included — while every other path in the
    module is raw, because ``_grep`` reads them with ``-z``. A quoted path
    matches nothing in the exempt tally, so the file drops out of the diff-based
    selection and falls back to the risen tally alone, which is exactly the hole
    this selection exists to close. The ASCII case above passes either way, so
    this is the only test that pins the ``-z``.

    Not a hypothetical class of file: ``_git`` pins UTF-8 rather than trusting
    the locale precisely because the tree carries accented names in fixtures.
    """
    name = "café.py"
    body = (
        f'A = "{LEGACY}-one"  # legacy-name-ok: pre-existing alias one\n'
        f'B = "{LEGACY}-two"  # legacy-name-ok: pre-existing alias two\n'
    )
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two aliases already here, under an accented name")
    # "alias two" loses its marker and "alias three" arrives with one: flat.
    (repo / name).write_text(
        body.replace("  # legacy-name-ok: pre-existing alias two", "")
        + f'C = "{LEGACY}-three"  # legacy-name-ok: brand new alias three\n',
        encoding="utf-8",
    )
    _git(repo, "add", "-A")

    out = _run(repo).stdout

    assert "brand new alias three" in out
    assert "1 exempt line(s) written by this change in 1 file(s)" in out


def test_a_touched_file_whose_exemptions_are_all_old_says_nothing(repo: Path) -> None:
    """The cost of letting git choose the files, and the guard against paying it.

    Selecting on the diff means every touched file that happens to hold a marker
    becomes a candidate — including ones where the change went nowhere near it.
    If an empty per-line result fell back to "print them all", #894 would be back
    through the file-selection door, and worse: it would fire on files whose
    exempt count never moved at all.
    """
    body = _with_two_aliases_already(repo)
    _stage(repo, "aliases.py", body + "UNRELATED = 1\n")

    result = _run(repo)

    assert result.returncode == 0
    assert "exempt line(s) written by this change" not in result.stdout
    assert "pre-existing alias" not in result.stdout


def test_a_file_only_deleted_from_reports_no_exemptions(repo: Path) -> None:
    """A deletion adds no lines, so there is nothing for this report to name.

    Distinct from the case above because `_added_lines` returns an empty set here
    rather than a set that simply misses — which is why it has to be able to say
    "empty" separately from "I could not read the diff".
    """
    body = _with_two_aliases_already(repo)
    _stage(repo, "aliases.py", body.splitlines(keepends=True)[0])

    result = _run(repo)

    assert result.returncode == 0
    assert "exempt line(s) written by this change" not in result.stdout


def test_many_exemptions_sharing_a_reason_are_grouped(repo: Path) -> None:
    """The wall is the failure mode, and Phase 5 builds walls.

    A dual-read wave exempts dozens of lines carrying one identical reason. At
    that length the list stops being read, which costs the report the thing it is
    for: the swap it exposes shows up as the ONE reason that does not match its
    neighbours. Collapsing repeats is what keeps the odd one visible.
    """
    body = "".join(
        f'A{i} = "{LEGACY}_x"  # legacy-name-ok: rule 3 dual-read alias\n'
        for i in range(6)
    )
    _stage(repo, "many.py", body)

    out = _run(repo).stdout

    assert "6x  rule 3 dual-read alias" in out
    assert "in 1 file(s): many.py" in out
    # Collapsed, not listed line by line.
    assert out.count("A0 = ") == 0


def test_an_odd_reason_out_survives_the_grouping(repo: Path) -> None:
    """The whole point of grouping: the one that differs is still printed in full,
    where a reader scanning past forty identical lines would have missed it."""
    body = "".join(
        f'A{i} = "{LEGACY}_x"  # legacy-name-ok: rule 3 dual-read alias\n'
        for i in range(6)
    )
    body += f'SNUCK = "{LEGACY}-new"  # legacy-name-ok: headroom, honestly\n'
    _stage(repo, "many.py", body)

    out = _run(repo).stdout

    assert "6x  rule 3 dual-read alias" in out
    assert "headroom, honestly" in out
    assert "SNUCK" in out


def test_the_exempt_and_add_swap_is_caught(repo: Path) -> None:
    """The gap the fourth review round found, and counting really cannot close it.

    Marking an existing line exempt frees exactly one slot, which a new unmarked
    line in the same file then fills: non-exempt flat, so a count-based gate
    passes. No count distinguishes that from legitimately adding one marked alias
    — both are non-exempt flat, exempt +1, total +1 — because the difference is
    *which* line carries the marker, not how many do.

    Comparing the text does distinguish them, which is why this now fails rather
    than merely being reported. The legitimate case adds new EXEMPT text and no
    new non-exempt text, so there is nothing to charge. The swap adds non-exempt
    text the repo never had, and that is what gets charged. Both directions are
    pinned — see the two tests below.

    The exemption is still named on the way out: the report is what made this
    visible in the first place, and it stays useful when the swap is deliberate.
    """
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-ok: gateway mirror\n'
        f'SNUCK = "{LEGACY}-new-service"\n',
    )

    result = _run(repo)

    assert result.returncode == 1
    assert "exempt line(s) written by this change" in result.stdout
    assert "gateway mirror" in result.stdout
    offenders = result.stdout.split("adds the legacy name in", 1)[1]
    assert "SNUCK" in offenders
    assert "count unchanged" in offenders


def test_adding_one_marked_alias_still_passes(repo: Path) -> None:
    """The legitimate half of the swap, and the reason the two are hard to tell
    apart by counting: identical in every tally. The marked line's text is exempt,
    so it is never charged, and nothing else in the file changed."""
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"\n'
        f'ALIAS = "{LEGACY}_write"  # legacy-name-ok: permanent tool shim\n',
    )

    assert _run(repo).returncode == 0


def test_marking_an_existing_line_exempt_still_passes(repo: Path) -> None:
    """The other legitimate half: an exemption on its own buys headroom but does
    not spend it, and the gate must not punish taking the decision alone."""
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-ok: gateway mirror\n',
    )

    assert _run(repo).returncode == 0


def test_a_falling_count_is_not_described_as_unchanged(repo: Path) -> None:
    """Two branded lines go, one brand-new one arrives: the count FELL, 2 -> 1,
    and the change still minted a name. Reporting that as "count unchanged" is
    false on its face, and a diagnostic a reader can catch being wrong is one
    they stop trusting — which costs more than the line is worth."""
    _stage(repo, "existing.py", f'A = "{LEGACY}-one"\nB = "{LEGACY}-two"\n')
    _git(repo, "commit", "-qm", "two branded lines")

    _stage(repo, "existing.py", f'C = "{LEGACY}-brand-new"\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "(2 -> 1, count fell — the text is new)" in result.stdout


def test_a_same_file_swap_for_a_brand_new_name_is_caught(repo: Path) -> None:
    """A file can drop one branded line and add a different, brand-new one in the
    same edit. The total stays flat, so a gate that filters on the count before
    looking at the text skips the file entirely and reports "No new lines."

    This predates the move check — the original per-file comparison had the same
    hole — but the text machinery the move check needed is exactly what closes it,
    so it is closed here rather than left for the count to keep missing.
    """
    _stage(
        repo,
        "existing.py",
        f'RENAMED = "{LEGACY}-brand-new-service"\n',
    )

    result = _run(repo)

    assert result.returncode == 1
    assert "RENAMED" in result.stdout


# ── parsing and failure modes ────────────────────────────────────────────────


def test_a_matched_line_full_of_colons_does_not_corrupt_the_path(repo: Path) -> None:
    """``git grep`` emits ``[tree:]path:lineno:text`` and the text may hold colons.

    Splitting one field too far attributes the line to a path named after its own
    contents, so the file's real count never rises and the gate silently passes.
    """
    _stage(repo, "new.py", f'URL = "https://{LEGACY}.net:8080/a:b:c"\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "new.py  (0 -> 1)" in result.stdout


def test_a_path_containing_a_colon_is_attributed_correctly(repo: Path) -> None:
    """A colon is legal in a filename on Linux, which is what CI runs on.

    Colon-separated ``git grep`` output cannot tell that colon from a field
    separator, so the line is attributed to a truncated path, the real file's
    count never rises, and the gate passes for it — permanently and silently.
    ``-z`` is what removes the ambiguity.
    """
    _stage(repo, "we:ird.py", f'KEY = "{LEGACY}"\n')

    result = _run(repo)

    assert result.returncode == 1
    assert "we:ird.py  (0 -> 1)" in result.stdout


def test_a_glob_in_a_path_does_not_pull_in_another_files_lines(repo: Path) -> None:
    """The same hazard one level up, in the failure report.

    The report re-greps each offending path, and a bare path is read as a
    *pathspec*: ``a[b].py`` is a character class that also matches ``ab.py``. So
    the report blames one file for another's lines, and the reader goes looking
    for an occurrence that is not there. ``:(literal)`` turns the path back into
    a path.
    """
    # Already present in the base, so it is not itself an offender — it exists
    # only to be wrongly swept in by the glob.
    (repo / "ab.py").write_text(f'INNOCENT = "{LEGACY}-bystander"\n')
    _git(repo, "add", "ab.py")
    _git(repo, "commit", "-qm", "add ab.py")

    _stage(repo, "a[b].py", f'GUILTY = "{LEGACY}"\n')
    result = _run(repo)

    assert result.returncode == 1
    assert "a[b].py  (0 -> 1)" in result.stdout
    assert "GUILTY" in result.stdout
    assert "INNOCENT" not in result.stdout


def test_an_unresolvable_base_fails_loudly(repo: Path) -> None:
    """Fail closed: a mistyped base must not read as an empty baseline, which
    would make every existing file look newly added.

    Exit 2, not 1. A broken invocation and a rule violation want different
    reactions from whoever is reading CI, and a traceback reads as neither —
    it looks like the gate crashed rather than like the ref was wrong.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "no-such-ref"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "no-such-ref" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_failing_working_tree_scan_also_exits_two(tmp_path: Path) -> None:
    """The head scan reaches git the same way the base scan does, so it can fail
    the same way. Left unguarded it surfaced as a traceback and exit 1 — and in
    this script exit 1 means "ran, and found new names". A gate that could not run
    must never be mistaken for a gate that failed you.
    """
    not_a_repo = tmp_path / "bare"
    not_a_repo.mkdir()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD"],
        cwd=not_a_repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "not a git repository" in result.stderr
    assert "Traceback" not in result.stderr


def test_finding_nothing_at_all_fails_rather_than_passing(tmp_path: Path) -> None:
    """The silent-pass hole, and the reason it needs its own guard.

    A pathspec matching nothing exits 1 with an empty stderr — indistinguishable
    from a clean tree. Without this, breaking the pattern or the pathspec turns
    the gate into a no-op that reports success on every PR for the rest of the
    programme, which is the worst possible failure for a gate.
    """
    r = tmp_path / "empty"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "clean.py").write_text("VALUE = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")

    result = _run(r)

    assert result.returncode == 1
    assert "no-op" not in result.stdout  # it should explain, not merely fail
    assert "passing without checking" in result.stdout


def test_report_mode_never_fails(repo: Path) -> None:
    _stage(repo, "new.py", f'KEY = "{LEGACY}"\n')

    result = _run(repo, "--report")

    assert result.returncode == 0
    assert "2 lines across 2 files" in result.stdout
    assert (
        "Change from HEAD: 1 added, 0 annotated, 0 removed, "
        "0 excused moves (+1 net)." in result.stdout
    )


def test_report_mode_keeps_counts_when_the_base_is_unresolvable(repo: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "no-such-ref", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "1 lines across 1 files" in result.stdout
    assert "Change from no-such-ref unavailable" in result.stderr


def test_report_mode_separates_annotation_from_removal(repo: Path) -> None:
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )

    result = _run(repo, "--report")

    assert result.returncode == 0
    assert "0 lines across 0 files" in result.stdout
    assert (
        "Change from HEAD: 0 added, 1 annotated, 0 removed, "
        "0 excused moves (-1 net)." in result.stdout
    )


def test_summary_replays_annotation_before_later_exempt_churn(repo: Path) -> None:
    """A later deletion cannot rewrite an earlier annotation as a removal."""
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )
    _git(repo, "commit", "-qm", "annotate the URL")
    _stage(repo, "existing.py", 'URL = "https://caura.ai"\n')
    _stage(
        repo,
        "new.py",
        f'ALIAS = "{LEGACY}-cli"  # legacy-name-ok: permanent command alias\n',
    )
    _git(repo, "commit", "-qm", "replace the URL and add an alias")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~2", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "Change from HEAD~2: 0 added, 1 annotated, 0 removed, "
        "0 excused moves (-1 net)." in result.stdout
    )


def test_summary_replays_transient_addition_and_removal(repo: Path) -> None:
    """Endpoint equality must not erase gross churn inside the range."""
    _stage(repo, "new.py", f'KEY = "{LEGACY}"\n')
    _git(repo, "commit", "-qm", "add a transient legacy line")
    _stage(repo, "new.py", "")
    _git(repo, "commit", "-qm", "remove the transient legacy line")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~2", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "Change from HEAD~2: 1 added, 0 annotated, 1 removed, "
        "0 excused moves (+0 net)." in result.stdout
    )


def test_summary_does_not_call_a_removed_then_readded_line_annotated(
    repo: Path,
) -> None:
    _stage(repo, "existing.py", 'URL = "https://caura.ai"\n')
    _git(repo, "commit", "-qm", "remove the legacy URL")
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )
    _git(repo, "commit", "-qm", "restore the URL as a floor mention")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~2", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "Change from HEAD~2: 0 added, 0 annotated, 1 removed, "
        "0 excused moves (-1 net)." in result.stdout
    )


def test_summary_replays_a_pure_rename_before_removal(repo: Path) -> None:
    _git(repo, "mv", "existing.py", "moved.py")
    _git(repo, "commit", "-qm", "move the legacy URL")
    _stage(repo, "moved.py", 'URL = "https://caura.ai"\n')
    _git(repo, "commit", "-qm", "remove the legacy URL")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~2", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "Change from HEAD~2: 0 added, 0 annotated, 1 removed, "
        "1 excused move (-1 net)." in result.stdout
    )


@pytest.mark.parametrize(
    "delete_first", [True, False], ids=["delete-add", "add-delete"]
)
def test_summary_pairs_a_move_across_separate_commits(
    repo: Path, delete_first: bool
) -> None:
    if delete_first:
        _git(repo, "rm", "existing.py")
        _git(repo, "commit", "-qm", "remove the legacy URL")
    else:
        _stage(repo, "moved.py", f'URL = "https://{LEGACY}.net"\n')
        _git(repo, "commit", "-qm", "copy the URL elsewhere")
    _stage(repo, "clean.py", "VALUE = 2\n")
    _git(repo, "commit", "-qm", "unrelated edit")
    if delete_first:
        _stage(repo, "moved.py", f'URL = "https://{LEGACY}.net"\n')
        _git(repo, "commit", "-qm", "restore the URL elsewhere")
    else:
        _git(repo, "rm", "existing.py")
        _git(repo, "commit", "-qm", "remove the original URL")

    gate = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~3"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    report = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~3", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert gate.returncode == report.returncode == 0
    assert "1 line(s) treated as moved rather than added" in gate.stdout
    assert (
        "No new lines. 0 annotated, 0 removed, 1 excused move (+0 net)." in gate.stdout
    )
    assert (
        "Change from HEAD~3: 0 added, 0 annotated, 0 removed, "
        "1 excused move (+0 net)." in report.stdout
    )


def test_summary_does_not_reuse_a_loss_after_same_path_readdition(
    repo: Path,
) -> None:
    _git(repo, "rm", "existing.py")
    _git(repo, "commit", "-qm", "remove the legacy URL")
    _stage(repo, "existing.py", f'URL = "https://{LEGACY}.net"\n')
    _git(repo, "commit", "-qm", "restore the URL")
    _stage(repo, "copied.py", f'URL = "https://{LEGACY}.net"\n')
    _git(repo, "commit", "-qm", "copy the URL elsewhere")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~3", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "Change from HEAD~3: 2 added, 0 annotated, 1 removed, "
        "0 excused moves (+1 net)." in result.stdout
    )


def test_change_summary_falls_back_when_replay_cannot_spawn_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPT))
    change_summary = namespace["_change_summary"]
    scan_type = namespace["Scan"]
    summary_type = namespace["_ChangeSummary"]
    base = scan_type(
        {"existing.py": Counter({"legacy text": 1})},
        Counter({"legacy text": 1}),
        {},
    )
    head = scan_type({}, Counter(), {})

    def cannot_spawn(_: list[str]) -> str:
        raise OSError("argument list too long")

    monkeypatch.setitem(change_summary.__globals__, "_git", cannot_spawn)

    assert change_summary("HEAD", base, head) == summary_type(0, 0, 1, 0, -1)


def test_summary_replays_a_file_becoming_binary_then_text(repo: Path) -> None:
    original = f'URL = "https://{LEGACY}.net"\n'.encode()
    (repo / "existing.py").write_bytes(original + b"\0binary payload\n")
    _git(repo, "add", "existing.py")
    _git(repo, "commit", "-qm", "make the legacy file binary")
    (repo / "existing.py").write_bytes(original)
    _git(repo, "add", "existing.py")
    _git(repo, "commit", "-qm", "make the legacy file text again")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~2", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "Change from HEAD~2: 1 added, 0 annotated, 1 removed, "
        "0 excused moves (+0 net)." in result.stdout
    )


@pytest.mark.parametrize(
    ("empty_commits", "expected_greps"),
    [(False, 6), (True, 2)],
)
def test_multi_commit_summary_avoids_full_tree_replay(
    repo: Path, empty_commits: bool, expected_greps: int
) -> None:
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )
    _git(repo, "commit", "-qm", "annotate the URL")
    for i in range(4):
        if empty_commits:
            _git(repo, "commit", "--allow-empty", "-qm", f"empty {i}")
        else:
            _stage(repo, "clean.py", f"VALUE = {i}\n")
            _git(repo, "commit", "-qm", f"unrelated edit {i}")
    trace = repo / "git-trace.log"
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_HEAD_REF"}
    env["GIT_TRACE"] = str(trace)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~5", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    grep_calls = [
        line for line in trace.read_text().splitlines() if " git grep " in line
    ]
    assert result.returncode == 0
    assert (
        "Change from HEAD~5: 0 added, 1 annotated, 0 removed, "
        "0 excused moves (-1 net)." in result.stdout
    )
    assert len(grep_calls) == expected_greps
    assert sum(line.endswith(" -- :/") for line in grep_calls) == 2


def test_single_commit_summary_replays_a_dirty_worktree(repo: Path) -> None:
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )
    _git(repo, "commit", "-qm", "annotate the URL")
    _stage(repo, "existing.py", 'URL = "https://caura.ai"\n')

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~1", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "Change from HEAD~1: 0 added, 1 annotated, 0 removed, "
        "0 excused moves (-1 net)." in result.stdout
    )


def test_gate_caps_replay_but_report_remains_exact(repo: Path) -> None:
    _stage(
        repo,
        "existing.py",
        f'URL = "https://{LEGACY}.net"  # legacy-name-floor: published URL\n',
    )
    _git(repo, "commit", "-qm", "annotate the URL")
    _stage(repo, "existing.py", 'URL = "https://caura.ai"\n')
    _git(repo, "commit", "-qm", "remove the annotated URL")
    for i in range(62):
        _git(repo, "commit", "--allow-empty", "-qm", f"empty {i}")

    at_limit = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~64"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    _git(repo, "commit", "--allow-empty", "-qm", "empty 62")
    gate = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~65"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    report = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~65", "--report"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert at_limit.returncode == 0
    assert (
        "No new lines. 1 annotated, 0 removed, 0 excused moves (-1 net)."
        in at_limit.stdout
    )
    assert gate.returncode == 0
    assert "No new lines fail the gate. Change split omitted" in gate.stdout
    assert "range exceeds the 64-commit gate replay limit" in gate.stdout
    assert " net)." not in gate.stdout
    assert report.returncode == 0
    assert (
        "Change from HEAD~65: 0 added, 1 annotated, 0 removed, "
        "0 excused moves (-1 net)." in report.stdout
    )


def test_a_marker_swap_stays_visible_as_exemption_churn(repo: Path) -> None:
    """A kind change is neither annotation nor removal from the floor tally."""
    old = f'ALIAS = "{LEGACY}"  # legacy-name-ok: permanent alias\n'
    (repo / "alias.py").write_text(old)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add marked alias")
    _stage(
        repo,
        "alias.py",
        f'ALIAS = "{LEGACY}"  # legacy-name-floor: published name\n',
    )

    result = _run(repo)

    assert result.returncode == 0
    assert "1 exempt line(s) written by this change" in result.stdout
    assert "1 exempt line(s) removed by this change" in result.stdout
    assert result.stdout.rstrip().endswith("No new lines.")
    assert " annotated," not in result.stdout

    report = _run(repo, "--report")
    assert (
        "Change from HEAD: 0 added, 0 annotated, 0 removed, "
        "0 excused moves (+0 net)." in report.stdout
    )


# ── removed exemptions: reported, never fatal ────────────────────────────────
#
# ``legacy-name-ok`` exempts a line from this gate. It has never protected the
# line from being DELETED — and deleting one lowers the file's non-exempt count,
# which this gate reads as progress. A sweep that removed a dual-read alias
# deciding which ``.env`` keys survive a redeploy passed both required gates
# green. These pin the report that makes that visible.


def test_removing_an_exempt_line_is_reported(repo: Path) -> None:
    """The gap that let a data-loss change through green.

    Deleting a marked alias is indistinguishable from progress by count alone,
    so the only defence is telling the reader it happened.
    """
    alias = f'LEGACY = "{LEGACY}"  # legacy-name-ok: dual-read alias\n'
    (repo / "aliases.py").write_text(alias)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add alias")
    _stage(repo, "aliases.py", "LEGACY = None\n")

    result = _run(repo)

    assert "exempt line(s) removed" in result.stdout
    assert "dual-read alias" in result.stdout


def test_the_removal_report_names_the_file_the_line_left(repo: Path) -> None:
    """The report has to be actionable, not just alarming.

    Its own instruction is "confirm each alias still exists in some form", and
    the text alone does not say where to look — short markers plausibly live in
    more than one file. Naming the base-tree file is what turns the report into
    something a reviewer can check without grepping the base tree themselves.
    """
    alias = f'LEGACY = "{LEGACY}"  # legacy-name-ok: dual-read alias\n'
    (repo / "compat" / "shims").mkdir(parents=True)
    (repo / "compat" / "shims" / "aliases.py").write_text(alias)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add alias")
    _stage(repo, "compat/shims/aliases.py", "LEGACY = None\n")

    result = _run(repo)

    assert "was in: compat/shims/aliases.py" in result.stdout


def test_the_removal_report_names_only_files_that_lost_the_line(repo: Path) -> None:
    """Sources, not bystanders — the same rule the moved-line report follows.

    Identical markers are common, so a file can hold the text without having
    lost anything. Listing on containment would name it anyway, and because the
    list truncates, an alphabetically earlier bystander can push the file that
    actually lost the line out of view — turning the one line a reviewer is
    meant to check into noise.
    """
    alias = f'LEGACY = "{LEGACY}"  # legacy-name-ok: dual-read alias\n'
    (repo / "aardvark.py").write_text(alias)
    (repo / "zebra.py").write_text(alias)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add aliases")
    _stage(repo, "zebra.py", "LEGACY = None\n")

    result = _run(repo)

    assert "was in: zebra.py" in result.stdout
    assert "aardvark.py" not in result.stdout


def test_a_gain_elsewhere_does_not_cancel_a_loss(repo: Path) -> None:
    """The blind spot a repo-wide net diff has, and this report exists to close.

    ``before - after`` over the whole repo cancels a deletion against any
    byte-identical exempt line added anywhere else in the same change. The
    motivating incident was a sweep — precisely the shape that deletes an alias
    in one file while writing something identical in another — so the netting
    version would have reported the incident it was built for as one line, not
    three, and named the file that lost three as though it had lost one.
    """
    alias = f'LEGACY = "{LEGACY}"  # legacy-name-ok: dual-read alias\n'
    (repo / "old.py").write_text(alias * 3)
    (repo / "new.py").write_text("PLACEHOLDER = None\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add aliases")
    _stage(repo, "old.py", "LEGACY = None\n")
    _stage(repo, "new.py", alias * 2)

    result = _run(repo)

    # Three left old.py. Netting against the two gained in new.py would say one.
    assert "(x3)" in result.stdout
    assert "3 exempt line(s) removed" in result.stdout
    assert "was in: old.py" in result.stdout


def test_a_moved_exempt_line_names_where_it_went(repo: Path) -> None:
    """Counting gross surfaces moves; the report has to explain them, not hide them.

    A line that left one file and appeared in another is usually benign, and is
    exactly the "reworded or consolidated" case the reader is asked to confirm.
    Suppressing it would restore the blind spot, so it is reported — with the
    destination named, so the benign reading needs no grep.
    """
    alias = f'LEGACY = "{LEGACY}"  # legacy-name-ok: dual-read alias\n'
    (repo / "old.py").write_text(alias)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add alias")
    _stage(repo, "old.py", "LEGACY = None\n")
    _stage(repo, "moved.py", alias)

    result = _run(repo)

    assert "was in: old.py" in result.stdout
    assert "identical text added in: moved.py" in result.stdout
    assert "likely a move" in result.stdout


def test_removing_an_exempt_line_does_not_fail_the_gate(repo: Path) -> None:
    """Reports rather than fails, and the reason is measured, not stylistic.

    Across the last 400 commits of the real repo, seven removed a marked line
    and none was a genuine removal of protection — every one reworded or
    consolidated markers while keeping the alias. Failing here would have been
    seven false positives and no true ones, and false positives on a gate teach
    people to stop reading it.
    """
    alias = f'LEGACY = "{LEGACY}"  # legacy-name-ok: dual-read alias\n'
    (repo / "aliases.py").write_text(alias)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add alias")
    _stage(repo, "aliases.py", "LEGACY = None\n")

    assert _run(repo).returncode == 0


def test_consolidating_exemptions_still_reports_the_removals(repo: Path) -> None:
    """The commonest real shape: several markers collapse into one.

    Net protection is unchanged, so this must not fail — but the removals are
    still named, because "did the alias survive the rewording" is exactly the
    question a human has to answer and a tool cannot.
    """
    (repo / "aliases.py").write_text(
        f'A = "{LEGACY}"  # legacy-name-ok: alias one\n'
        f'B = "{LEGACY}"  # legacy-name-ok: alias two\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add aliases")
    _stage(
        repo, "aliases.py", f'BOTH = "{LEGACY}"  # legacy-name-ok: one rule for both\n'
    )

    result = _run(repo)

    assert result.returncode == 0
    assert "alias one" in result.stdout
    assert "alias two" in result.stdout


def test_an_untouched_exemption_is_not_reported(repo: Path) -> None:
    """No report when nothing was removed — a section that prints on every run
    is one people stop reading, which is the failure this exists to prevent."""
    alias = f'LEGACY = "{LEGACY}"  # legacy-name-ok: dual-read alias\n'
    (repo / "aliases.py").write_text(alias)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add alias")
    _stage(repo, "clean.py", "VALUE = 2\n")

    result = _run(repo)

    assert "exempt line(s) removed" not in result.stdout


# ── release-please's own branches: generated CHANGELOGs are exempt ───────────
#
# release-please regenerates per-package CHANGELOGs by quoting merged PR titles
# verbatim, so a title that legitimately carried the old brand (history — rule 2
# says never edit it) resurfaces as a line the tally cannot tell from fresh
# minting. The exemption is gated on GITHUB_HEAD_REF naming the bot's own
# branch, and scoped to CHANGELOG files — nothing else on that branch, and no
# CHANGELOG anywhere else, is excused.

_RELEASE_ENV = {"GITHUB_HEAD_REF": "release-please--branches--main"}


def test_a_changelog_passes_on_a_release_please_branch(repo: Path) -> None:
    _stage(repo, "CHANGELOG.md", f"* fix: retire the {LEGACY} gateway (#123)\n")

    result = _run(repo, env=_RELEASE_ENV)

    assert result.returncode == 0, result.stdout
    assert "1 generated CHANGELOG file(s) exempt on this" in result.stdout
    assert "CHANGELOG.md" in result.stdout


def test_a_release_changelog_addition_is_not_hidden_from_the_net(repo: Path) -> None:
    _stage(repo, "existing.py", 'URL = "https://caura.ai"\n')
    _stage(repo, "CHANGELOG.md", f"* fix: retire the {LEGACY} gateway (#123)\n")

    result = _run(repo, env=_RELEASE_ENV)

    assert result.returncode == 0
    assert (
        "No new lines fail the gate. 1 added, 0 annotated, 1 removed, "
        "0 excused moves (+0 net)." in result.stdout
    )


def test_the_same_changelog_fails_off_the_bot_branch(repo: Path) -> None:
    """The exemption is the bot's, not the file's: a human minting the name in a
    CHANGELOG on an ordinary branch — or locally, where GITHUB_HEAD_REF is
    absent — still answers to the gate."""
    _stage(repo, "CHANGELOG.md", f"* fix: retire the {LEGACY} gateway (#123)\n")

    result = _run(repo)

    assert result.returncode == 1
    assert "CHANGELOG.md" in result.stdout


def test_the_exemption_is_path_scoped_to_changelogs(repo: Path) -> None:
    """The bot's branch buys no headroom outside the files the bot generates: a
    non-CHANGELOG mint on that branch fails exactly as it would anywhere."""
    _stage(repo, "CHANGELOG.md", f"* fix: retire the {LEGACY} gateway (#123)\n")
    _stage(repo, "new.py", f'KEY = "{LEGACY}-new-service"\n')

    result = _run(repo, env=_RELEASE_ENV)

    assert result.returncode == 1
    offenders = result.stdout.split("adds the legacy name in", 1)[1]
    assert "new.py" in offenders
    assert "CHANGELOG.md" not in offenders
