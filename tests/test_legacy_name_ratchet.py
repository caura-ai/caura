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

import subprocess
import sys
from pathlib import Path

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


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD", *extra],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,  # a non-zero exit is the thing under test
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


# ── what must NOT fail ───────────────────────────────────────────────────────


def test_removing_a_line_passes_and_reports_progress(repo: Path) -> None:
    """Decreases are the point of the programme, so they are reported, not merely allowed."""
    _stage(repo, "existing.py", 'URL = "https://caura.ai"\n')

    result = _run(repo)

    assert result.returncode == 0
    assert "1 removed" in result.stdout
    assert "-1 net" in result.stdout


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


def test_only_the_minted_copies_of_a_repeated_line_are_named(repo: Path) -> None:
    """A file can hold more copies of a text than were actually minted.

    Two identical lines move in and a third is added. All three read the same, so
    a membership test on the minted set prints all three and sends the reader to
    "fix" two lines that were already in the tree. Only the count separates them,
    so the report spends a budget rather than testing membership.
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
    assert offenders.count("URL = ") == 1


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
    assert "1 line(s) newly exempted" in result.stdout
    assert "existing.py:1" in result.stdout
    assert "gateway mirror" in result.stdout


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
    assert "newly exempted" in result.stdout
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
