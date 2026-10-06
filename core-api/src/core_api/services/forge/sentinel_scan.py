"""Sentinel scanner for Skill Factory skill docs (plan §9).

Phase 2 ships the 8 real checks. The Phase 0 stub returned
``state='clean'`` for every input; the call sites
(``routes/documents.py`` pre-write hook + Phase 3 pre-apply hook in
``services/skill_lifecycle.py``) are unchanged — Phase 2 is a body-only
swap.

The 8 checks (one ``ScanFinding`` per hit; severity drives caller
behavior — see :class:`ScanFinding`):

  1. **prompt-injection** markers in name / content / description /
     summary / goal / tags[] / evidence (``critical``) — quarantine.
  2. **shell-injection** patterns in the skill text (``name``,
     ``content``, ``summary``, ``description``) and inside
     ``support_files`` entry bodies under EVERY ``role`` (``critical``) —
     quarantine. In skill text, routine admin lines (``rm -rf`` of a path
     other than the root, the home directory or a top-level system
     directory, ``dd`` from ``/dev/zero``, ``mkfs``, ``chmod 777`` on a
     directory other than those) are a ``DESTRUCTIVE_COMMAND`` warning
     instead; a script keeps them critical. ``dd`` or ``mkfs`` onto a disk
     warns whatever the disk: the text cannot show whether ``/dev/sdb`` is a
     data volume or the boot disk. A warning does not quarantine, so
     under ``auto_promote_clean`` such a skill auto-activates: the owner's
     decision of 2026-10-05 (M-120), stated with that flag in
     ``skill_promoter.promote_pending_candidates``.
  3. **URL exfiltration** patterns in the same four doc fields and in
     script-roled ``support_files`` bodies (``warn``) — surfaces on the
     inbox card; doc may still proceed.
  4. **path violations** on ``support_files`` (absolute, traversal,
     hidden, bare-dot, executable, non-ASCII) — ``fatal=True``;
     refuse the write.
  5. **PII** (SSN / credit card / phone / email) in the same fields as
     check #1 (``warn``; redact-on-display flag set by the inbox
     renderer).
  6. **memory-id stuffing** — more than 20 unique cited memory ids in
     ``data.cites`` (the field Forge writes) or ``data.evidence.memory_ids``
     (the dict shape an external writer may use); ``warn``, capped at 20 on
     render.
  7. **body size** — UTF-8 byte length of ``data.content`` exceeds
     ``body_max_bytes`` — ``fatal=True``.
  8. **description size** — UTF-8 byte length of ``data.description``
     exceeds ``description_max_bytes`` — ``fatal=True``.

``support_files`` (checks #2, #3, #4) has no production WRITER yet
─────────────────────────────────────────────────────────────────
(Checks #2 and #3 ALSO run over ``content`` / ``summary`` /
``description`` — the text that does reach disk as ``<slug>/SKILL.md``
and that the harness loads. The paragraphs below are about the
``support_files`` half only.)

09/02 L-01. Nothing in the shipped product populates the key.
``forge_service._distill_cluster`` — the only production writer of a
skill doc — builds ``data`` without it, and the only harness-install
path that exists (the plugin's skill reconciler) materialises
``<slug>/SKILL.md`` from ``data.content`` alone and never asks for
side-car files. The identically-named ``support_files`` documented in
``routes/documents.py`` belongs to the ``skills_rollback`` collection,
carries a different shape
(``{path, existed, previous_content_hash, previous_content}``), and is
never handed to this scanner.

So these three checks are FORWARD-LOOKING, not dead, and the
distinction is the whole point. Contrast check #6 (09/02 L-35), which
was repointed because a real, populated field — ``data.cites`` — was
going unscanned while the check watched a shape no writer produced.
There is no such alternate field here: no side-car content reaches
disk by any route, so nothing is slipping past an unfired check.

They are also reachable TODAY. ``POST /documents`` with
``collection='skills'`` type-checks a fixed set of keys and passes
every other key in ``data`` through untouched, so an external writer
using the shape above is scanned exactly as written — see
``tests/test_l01_sentinel_support_files_forward_looking.py``, which
pins that. What is absent is a consumer, not a caller. Deleting the
checks would drop BOTH of this module's ``fatal=True`` content guards
while the write surface still accepts the field, and they would have
to be written again for the Phase-3 install path.

Performance budget (plan §9): p95 < 500ms on a 40KB body — regex +
path checks + classifiers, **NO LLM, NO network**. Cacheable by
``content_hash``.

The scanner is deterministic + side-effect-free: callers cache results
by ``content_hash``, so re-scanning an unchanged doc is free.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

logger = logging.getLogger(__name__)


ScanState = Literal["pending", "clean", "failed", "quarantined"]
ScanMode = Literal["pre-write", "pre-apply"]


# ── Defaults (mirror ``org_settings.skills_factory.*``) ───────────────
#
# Sentinel re-checks the size caps even though
# ``skill_lifecycle.validate_and_normalize_skill_write`` already
# enforces them at write time — belt-and-suspenders, since the
# pre-apply hook (Phase 3) runs on already-persisted docs whose caller
# may have skipped the validator (e.g. legacy imports).
DEFAULT_BODY_MAX_BYTES: int = 40_000
DEFAULT_DESCRIPTION_MAX_BYTES: int = 160
MAX_MEMORY_IDS_BEFORE_WARN: int = 20


@dataclass(frozen=True)
class ScanFinding:
    """A single Sentinel finding.

    Severity drives caller behavior:

      - ``critical`` → caller should set ``status='quarantined'``
        on the doc (or refuse to write at all for hard-reject
        findings like size / path violations — see :attr:`fatal`).
      - ``warn``     → finding surfaces on the inbox card; doc may
        still proceed to ``staged``.
      - ``info``     → audit/debug only; no UX surface.
    """

    code: str
    severity: Literal["critical", "warn", "info"]
    message: str
    # ``fatal=True`` means the caller MUST refuse the operation
    # (e.g. ``HTTPException(422)``) rather than persisting + tagging
    # quarantine. Reserved for path violations and hard size caps —
    # things that should never be stored at all.
    fatal: bool = False
    # Optional pointer at the offending span; e.g.
    # ``"data.support_files[2].path"`` or ``"data.content[14012:14050]"``.
    locator: str | None = None


@dataclass(frozen=True)
class ScanResult:
    """Output of a single scan. Shape mirrors plan §3
    ``data.scan`` block, ready to merge straight into the doc.
    """

    state: ScanState
    scanned_at: str
    critical: int
    warn: int
    info: int
    findings: tuple[ScanFinding, ...] = field(default_factory=tuple)

    def as_doc_field(self) -> dict:
        """Render to the jsonb shape the doc carries on disk."""
        return {
            "state": self.state,
            "scanned_at": self.scanned_at,
            "critical": self.critical,
            "warn": self.warn,
            "info": self.info,
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity,
                    "message": f.message,
                    # Always emit ``fatal`` so Phase 2 consumers can
                    # index ``finding["fatal"]`` directly — uniform schema.
                    "fatal": f.fatal,
                    **({"locator": f.locator} if f.locator else {}),
                }
                for f in self.findings
            ],
        }

    @property
    def any_fatal(self) -> bool:
        return any(f.fatal for f in self.findings)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# How much of a match a finding's message quotes. A fetch-to-run match can span
# the whole text, and the message is stored with the scan and shown on the
# inbox card (third review of caura PR #1864); the locator keeps the full span.
_QUOTE_MAX: int = 200


def _quoted(match: re.Match[str]) -> str:
    """The matched text for a finding message: its ``repr``, cut with an ellipsis."""
    text = match.group(0)
    return repr(text if len(text) <= _QUOTE_MAX else text[:_QUOTE_MAX] + "…")


# Max depth the evidence walker recurses. Skill-doc evidence is
# author-controlled; an adversarial writer could nest dicts arbitrarily
# to slow the scan, so we bound recursion at a depth that comfortably
# covers any realistic structured evidence shape.
_EVIDENCE_RECURSE_MAX_DEPTH: int = 4


def _iter_evidence_strings(obj, prefix: str, depth: int = 0) -> Iterable[tuple[str, str]]:
    """Yield ``(locator_path, text)`` for every string-valued leaf in a
    dict/list/str evidence shape. Used by ``scan_skill_doc`` so the
    prompt-injection + PII regexes see deeply-nested quoted text — a
    flat ``evidence.items()`` walk would skip
    ``evidence.context.user_message`` silently and let an adversary
    smuggle markers past the scan.
    """
    if depth > _EVIDENCE_RECURSE_MAX_DEPTH:
        return
    if isinstance(obj, str):
        yield prefix, obj
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_evidence_strings(v, f"{prefix}.{k}", depth + 1)
        return
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_evidence_strings(v, f"{prefix}[{i}]", depth + 1)


# ── Check #1 — prompt-injection markers ────────────────────────────
#
# Keyword/regex set tuned for high precision on known marker phrases.
# A Phase-2+ upgrade can swap in a classifier; the call site doesn't
# change. Multi-line + case-insensitive at the regex level.
_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bignore\s+(?:all\s+)?(?:the\s+)?(?:previous|above|prior)\s+(?:instructions?|prompts?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdisregard\s+(?:the\s+)?(?:above|previous|prior)\s+(?:instructions?|prompts?|context)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bforget\s+(?:everything|all)\s+(?:above|prior|previously)\b", re.IGNORECASE),
    # Pseudo-role injection. Narrowed: the bare ``system:`` prefix
    # appears in legitimate log output ("system: starting service") and
    # in code comments — we only fire when it's followed by a verb-y
    # command pattern that indicates an injection attempt.
    re.compile(
        r"^\s*system\s*:\s*(?:you\s+(?:are|must|will)|ignore|act\s+as|disregard|forget|override)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"\{\{\s*system\s*\}\}", re.IGNORECASE),
    re.compile(r"<\|im_start\|>\s*system", re.IGNORECASE),
    # Jailbreak signals
    re.compile(r"\b(?:jailbreak|DAN\s+mode|developer\s+mode\s+enabled)\b", re.IGNORECASE),
    re.compile(r"\boverride\s+(?:the\s+)?(?:safety|guardrails?|filters?)\b", re.IGNORECASE),
    # New-rules / role-takeover
    re.compile(r"\byou\s+are\s+now\s+a?\s*(?:new|different)\s+(?:assistant|ai|model)\b", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:an?\s+)?unrestricted\b", re.IGNORECASE),
)


def _scan_prompt_injection(text: object, field_name: str) -> Iterable[ScanFinding]:
    # Non-``str`` in, nothing out. The guard used to be a bare ``if not
    # text``, which a non-empty non-string (a ``list``, say) passes —
    # sending it straight into ``re.search``, which raises ``TypeError``
    # and takes down a scanner this module promises will never be the
    # thing that fails a write. Reachable for every field: Forge and the
    # pre-apply rescan both hand Sentinel data that never went through
    # the SF-002 validator.
    if not isinstance(text, str) or not text:
        return
    for pat in _PROMPT_INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            yield ScanFinding(
                code="PROMPT_INJECTION",
                severity="critical",
                message=f"prompt-injection marker detected in data.{field_name}: {_quoted(m)}",
                locator=f"data.{field_name}[{m.start()}:{m.end()}]",
            )
            # One finding per field is sufficient — the inbox card
            # surfaces the first hit; users review the raw text anyway.
            return


# ── Check #2 — shell-injection in skill text + support_file bodies ─
#
# Runs on EVERY support_file body regardless of ``role`` — see the
# orchestrator, which deliberately dropped the role gate because a
# fork-bomb shipped under role='templates' would otherwise have gone
# unscanned. This comment used to claim the opposite ("only fires on
# support_files whose role looks script-y"); ``_SCRIPT_ROLES`` is what
# check #3 (URL exfiltration) and check #4 (executable-extension
# placement) gate on, and it does not narrow this check.
_SCRIPT_ROLES: frozenset[str] = frozenset({"scripts", "script", "exec", "command"})

# Any ``rm -rf`` that starts a command. Named because only a script gets it
# (``_SHELL_INJECTION_PATTERNS``): in skill text, ``rm -rf node_modules`` and
# ``rm -rf ./build`` are ordinary steps.
_GENERIC_RM_PATTERN: re.Pattern[str] = re.compile(
    r"(?:^|[;&|`])\s*rm\s+-(?:rf|fr)\s", re.IGNORECASE | re.MULTILINE
)

_SHELL = r"(?:ba|z|da|k)?sh"
# An interpreter runs a download only when it reads its program from stdin:
# ``| python3`` or ``| python3 -``, not ``| python3 -m json.tool``.
_STDIN_INTERPRETER = r"(?:python[0-9.]*|perl|ruby|node)(?:\s+-)?(?=\s*(?:$|[;&|)`'\"]))"
# What can stand before the shell's name and still run it: ``sudo`` with its
# options (``-u root`` takes a value), ``env`` with its options and ``VAR=value``
# assignments, and a directory (``/bin/bash``, ``/usr/bin/env bash``). Third
# review of caura PR #1864.
_SUDO = r"(?:sudo(?:\s+-\S+(?:\s+[^-\s]\S*)?)*\s+)?"
_ENV = r"(?:(?:[\w./-]*/)?env(?:\s+(?:-\S+|\w+=\S*))*\s+)?"
_RUNNER = rf"{_SUDO}{_ENV}(?:[\w./-]*/)?"
# A pipe, not either half of ``||``: ``curl … || bash restart.sh`` runs a
# fallback, not the download (seventh review of caura PR #1864).
_PIPE = r"(?<!\|)\|(?!\|)"
_SPAN = re.compile(r"[\s\S]*")


class _Searcher(Protocol):
    """What :func:`_scan_shell_injection` asks of a pattern: a compiled regex,
    or a check one regex cannot express safely."""

    def search(self, string: str, /) -> re.Match[str] | None: ...


def _logical_lines(text: str) -> Iterator[tuple[int, int]]:
    """``(start, end)`` of each command line. A newline after a trailing ``\\``
    or ``|`` continues the command, as the shell reads it. A line that starts
    with ``|`` is a markdown table row, never a command, so its trailing ``|``
    does not: joined, the rows ``| curl | … |`` and ``| bash | … |`` read as a
    fetch piped to bash (sixth review of caura PR #1864)."""
    start = pos = 0
    while (nl := text.find("\n", pos)) != -1:
        j = nl - 1
        while j >= start and text[j] in " \t\r":
            j -= 1
        if j >= start and (text[j] == "\\" or (text[j] == "|" and not text[pos:nl].lstrip().startswith("|"))):
            pos = nl + 1
            continue
        yield start, nl
        start = pos = nl + 1
    yield start, len(text)


class _FetchThenTail:
    """A fetch and, later on the same command line, what hands it to a shell.

    Each command line is read whole: the first fetch on it, then the tail
    searched from there to the line's end. That is linear, and no amount of
    padding between the two hides the tail, as a bounded gap did (second
    review of caura PR #1864).
    """

    def __init__(self, fetch: str, tail: str) -> None:
        self._fetch = re.compile(fetch, re.IGNORECASE)
        self._tail = re.compile(tail, re.IGNORECASE)

    def search(self, string: str, /) -> re.Match[str] | None:
        for start, end in _logical_lines(string):
            fetch = self._fetch.search(string, start, end)
            if fetch and (tail := self._tail.search(string, fetch.end(), end)):
                # One match over fetch and tail, for the finding's text and locator.
                return _SPAN.match(string, fetch.start(), tail.end())
        return None


# Code fetched from the network and run as it lands. M-91: the first form
# missed ``| sudo bash`` and ``| python3 -``, and the others scanned clean:
# ``bash -c "$(curl …)"``, ``bash <(curl …)``, ``eval "$(curl …)"`` and
# PowerShell's ``iwr … | iex``.
_PIPE_TO_SHELL_PATTERNS: tuple[_Searcher, ...] = (
    _FetchThenTail(r"\b(?:curl|wget)\b", rf"{_PIPE}\s*{_RUNNER}(?:{_SHELL}\b|{_STDIN_INTERPRETER})"),
    re.compile(rf"\b{_SHELL}\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b", re.IGNORECASE),
    re.compile(r"\beval\s+[\"']?\$\(\s*(?:curl|wget)\b", re.IGNORECASE),
    re.compile(
        rf"(?:\b(?:{_SHELL}|source)|(?:^|[\s;&|])\.)\s+<\(\s*(?:curl|wget)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
    _FetchThenTail(
        r"\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b", rf"{_PIPE}\s*(?:iex|Invoke-Expression)\b"
    ),
)

_FETCH_COMMAND = re.compile(r"\b(curl|wget)\b", re.IGNORECASE)
# One statement of a command line. A quoted string is one unit, so a ``;`` or
# ``|`` in a header does not end the fetch before its ``-o`` (third review of
# caura PR #1864). An unclosed quote runs to the line's end, as the shell
# reads it, so each character is read once.
_STATEMENT = re.compile(r"""(?:'[^']*'?|"(?:[^"\\]|\\[\s\S])*"?|[^'"&|;])*""")
_TOKEN = re.compile(r"\S+")
# ``| tee FILE``: the next stage of the pipe saves what the fetch wrote to
# stdout (fourth review of caura PR #1864).
_TEE = re.compile(rf"\|[ \t]*{_RUNNER}tee(?:[ \t]+-\S+)*[ \t]+[\"']?([^\s;&|<>\"']+)", re.IGNORECASE)
# A command that runs a file: a shell or python on it, after its options and a
# ``--``, ``source`` or ``.``, or ``./file``. ``chmod +x`` alone is not a run:
# making a downloaded binary executable and installing it is a routine step
# (fourth review of caura PR #1864), and ``./file`` still catches running it.
_RUN_OF_FILE = re.compile(
    rf"(?:^|&&|\|\||;)[ \t]*{_RUNNER}"
    rf"(?:(?:{_SHELL}|python[0-9.]*|source|\.)(?:[ \t]+(?:-\w+|--))*[ \t]+|(?=\./))"
    r"[\"']?([\w./-]+)",
    re.IGNORECASE | re.MULTILINE,
)


def _base_name(path: str) -> str:
    path = path.strip("\"'").split("?", 1)[0].split("#", 1)[0]
    return path.rsplit("/", 1)[-1]


def _saved_name(tool: str, tokens: list[str]) -> str | None:
    """The base name of the file a ``curl`` or ``wget`` command saves, or None.
    curl saves only with ``-o FILE`` or ``-O`` (the URL's name); wget saves under
    the URL's name unless ``-O`` names another. What either writes to stdout
    (curl by default, ``-o -``, ``-O -``) is saved by a ``> FILE`` or ``>> FILE``
    redirect (fourth review of caura PR #1864)."""
    out: str | None = None
    redirect: str | None = None
    remote_name = False
    url_name: str | None = None
    out_flag = "o" if tool == "curl" else "O"
    i = 0
    while i < len(tokens):
        tok = tokens[i].strip("\"'")
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok.startswith(">"):
            target = tok.lstrip(">")
            redirect = target or nxt
            i += 0 if target else 1
        elif tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if name == ("--output" if tool == "curl" else "--output-document"):
                out = value if eq else nxt
                i += 0 if eq else 1
            elif tool == "curl" and name in ("--remote-name", "--remote-name-all"):
                remote_name = True
        elif tok.startswith("-") and len(tok) > 1:
            flags = tok[1:]
            if out_flag in flags:
                rest = flags[flags.index(out_flag) + 1 :]
                out = rest or nxt
                i += 0 if rest else 1
            elif tool == "curl" and "O" in flags:
                remote_name = True
        elif url_name is None and "://" in tok:
            url_name = _base_name(tok)
        i += 1
    if out is not None and _base_name(out) not in ("", "-"):
        return _base_name(out)
    if out is not None or (tool == "curl" and not remote_name):
        name = _base_name(redirect) if redirect else ""
        return None if name in ("", "-") else name
    return url_name or None


class _DownloadThenRun:
    """A download saved to a file, and a later command that runs that file
    (M-91): ``curl -o x.sh URL && sh x.sh``, ``wget URL/x.sh`` then ``bash x.sh``
    on a later line, ``curl -O URL/x.sh; ./x.sh``.

    Two linear passes rather than one regex with a backreference. The first
    collects every run of a file by its base name; the second reads each fetch
    command, works out the name it saves under, and looks that name up. The
    regex this replaced backtracked super-linearly on a long line of fetches,
    and its bounded successor missed a run padded past its window (both
    reviews of caura PR #1864).
    """

    def search(self, string: str, /) -> re.Match[str] | None:
        runs = {_base_name(run.group(1)): run for run in _RUN_OF_FILE.finditer(string)}
        if not runs:
            return None
        for start, end in _logical_lines(string):
            pos = start
            while fetch := _FETCH_COMMAND.search(string, pos, end):
                statement = _STATEMENT.match(string, fetch.end(), end)
                statement_end = statement.end() if statement else end
                tokens = _TOKEN.findall(string, fetch.end(), statement_end)
                name = _saved_name(fetch.group(1).lower(), tokens)
                if name is None and (tee := _TEE.match(string, statement_end, end)):
                    name, statement_end = _base_name(tee.group(1)), tee.end()
                # The last run of that name, if it comes after this fetch.
                run = runs.get(name) if name else None
                if run is not None and run.start() >= statement_end:
                    return _SPAN.match(string, fetch.start(), run.end())
                pos = statement_end
        return None


# What a wipe that leaves the host unusable names: the filesystem root, a
# top-level system directory or the home directory, bare or followed by
# ``/``, ``/*``, ``/.`` or ``/.*``. A path under one of them
# (``/var/lib/apt/lists/*``) is a routine admin line (``_ADMIN_PATTERNS``).
# Repeated slashes and ``.`` or ``..`` segments that land back on one of them
# name it too: ``//``, ``/./``, ``/etc/..``, ``/usr/../etc``, ``~/..`` (fifth
# review of caura PR #1864).
_SLASHES = r"/+"
_TOP_DIR = r"(?:bin|boot|dev|etc|home|lib(?:32|64|x32)?|media|mnt|opt|proc|root|run|sbin|srv|sys|usr|var)"
# A segment that leaves the path at the root: ``.``, ``..``, or a top-level
# directory and straight back out of it.
_STAY_AT_ROOT = rf"(?:\.\.?|{_TOP_DIR}{_SLASHES}\.\.)"
# After a target: ``.`` and ``..`` segments, then ``/``, ``/*`` or ``/.*``.
_TARGET_TAIL = rf"(?:{_SLASHES}\.\.?)*(?:{_SLASHES}(?:\*|\.\*)?)?"
# Possessive (``*+``), so a long chain that never resolves is read once.
_ROOT_PATH = rf"{_SLASHES}(?:{_STAY_AT_ROOT}{_SLASHES})*+"
_WIPE_TARGET = (
    rf"(?:{_ROOT_PATH}(?:{_STAY_AT_ROOT}|{_TOP_DIR}{_TARGET_TAIL}|\*|\.\*)?"
    rf"|(?:~|\"?\$(?:HOME|\{{HOME\}})\"?){_TARGET_TAIL})"
)
# One operand that is a wipe target, whole. A path may be quoted (``"/etc"``,
# ``'/'``); a quoted ``~`` or ``'$HOME'`` names a literal directory, not home
# (seventh review of caura PR #1864).
_WIPE_OPERAND = re.compile(rf"(?:[\"'](?=/))?{_WIPE_TARGET}(?=$|[\s;&|`'\")])", re.IGNORECASE)
# Where a command ends: a separator, a comment, or a newline the shell does not
# continue.
_STATEMENT_END = re.compile(r"[;&|`)]|(?<=\s)#|(?<!\\)(?<!\\\r)\n")
_WORD = re.compile(r"\S+")


class _OnWipeTarget:
    """A command with what a root wipe names among its operands.

    Every operand up to the end of the command counts, not just the first, and
    options may stand among them: ``rm -rf /var/cache/apt /etc``,
    ``rm -rf -v /`` (sixth review of caura PR #1864). Each command is read
    once, so the check is linear however many operands it has.
    """

    def __init__(self, head: str, options: Callable[[list[str]], bool]) -> None:
        self._head = re.compile(head, re.IGNORECASE)
        self._options = options

    def search(self, string: str, /) -> re.Match[str] | None:
        pos = 0
        while head := self._head.search(string, pos):
            stop = _STATEMENT_END.search(string, head.end())
            end = stop.start() if stop else len(string)
            options: list[str] = []
            target: re.Match[str] | None = None
            for word in _WORD.finditer(string, head.end(), end):
                if word.group().startswith("-"):
                    options.append(word.group())
                elif target is None:
                    target = _WIPE_OPERAND.match(string, word.start(), end)
            if target and self._options(options):
                return _SPAN.match(string, head.start(), target.end())
            pos = end
        return None


def _recursive_and_forced(options: list[str]) -> bool:
    """``rm``'s options ask for ``-r`` and ``-f``, together or apart."""
    short = "".join(o[1:].lower() for o in options if not o.startswith("--"))
    return ("r" in short or "--recursive" in options) and ("f" in short or "--force" in options)


_ROOT_WIPE_PATTERN = _OnWipeTarget(r"(?<![\w-])rm\b", _recursive_and_forced)

# ``chmod 777`` on what a root wipe names. sudo and sshd refuse to work once
# the root or a system directory is world-writable, so it breaks the host as the
# wipe does; on any other directory it is a routine admin line (fourth review of
# caura PR #1864).
_OPEN_ROOT_PATTERN = _OnWipeTarget(r"\bchmod\s+(?:-R\s+)?[0-7]*7{2,3}\b", lambda _: True)

# Critical wherever they appear: there is no routine reason to ship them.
_ATTACK_PATTERNS: tuple[_Searcher, ...] = (
    _ROOT_WIPE_PATTERN,
    _OPEN_ROOT_PATTERN,
    re.compile(r"\$\(\s*rm\s", re.IGNORECASE),
    re.compile(r":\(\s*\)\s*\{\s*:\|:&\s*\}\s*;\s*:", re.MULTILINE),  # fork bomb
    re.compile(r"\b(?:cat|less|more|head|tail)\s+/etc/(?:passwd|shadow|sudoers)\b", re.IGNORECASE),
    *_PIPE_TO_SHELL_PATTERNS,
    _DownloadThenRun(),
    re.compile(r"\beval\s*\(\s*(?:base64_decode|atob|fromCharCode)", re.IGNORECASE),
    re.compile(r"\bexec\s*\(\s*['\"]?(?:cmd|powershell|sh|bash)\b", re.IGNORECASE),
)

# Destructive, but routine in a runbook for a host the operator owns: emptying
# a directory, a swapfile, formatting a data volume, opening a shared
# directory. Critical in a script, which runs as written; a warning in skill
# text, where quarantining them buried every real hit (M-120, owner decision
# 2026-10-05). ``dd`` and ``mkfs`` warn whatever the device: a data volume is a
# whole disk too (``mkfs.ext4 /dev/xvdf``), and the text cannot show which disk
# boots the host (sixth review of caura PR #1864).
_ADMIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Match both ``-rf`` and ``-fr`` — functionally identical, equally
    # common in the wild. A single-flag regex (``-rf`` only) lets the
    # rarer-but-still-trivial ``-fr`` slip through.
    re.compile(r"\brm\s+-(?:rf|fr)\s+[\"']?/(?!tmp\b)", re.IGNORECASE),  # rm -rf [quoted] /<path>, not /tmp
    re.compile(r"\bdd\s+if=/dev/(?:zero|random|urandom)\b", re.IGNORECASE),
    re.compile(r"\bmkfs\.[a-z0-9]+\s", re.IGNORECASE),
    re.compile(r"\bchmod\s+(?:-R\s+)?[0-7]*7{2,3}\b"),  # chmod 777 / 0777
)

# A ``support_files`` body is a script that runs as written: every pattern,
# critical, plus the bare "``rm -rf`` at the start of a command".
_SHELL_INJECTION_PATTERNS: tuple[_Searcher, ...] = (
    *_ATTACK_PATTERNS,
    *_ADMIN_PATTERNS,
    _GENERIC_RM_PATTERN,
)


def _scan_shell_injection(
    body: str,
    locator_prefix: str,
    patterns: tuple[_Searcher, ...] = _SHELL_INJECTION_PATTERNS,
) -> Iterable[ScanFinding]:
    if not body:
        return
    for pat in patterns:
        m = pat.search(body)
        if m:
            yield ScanFinding(
                code="SHELL_INJECTION",
                severity="critical",
                message=f"shell-injection pattern detected: {_quoted(m)}",
                locator=f"{locator_prefix}[{m.start()}:{m.end()}]",
            )
            return


def _scan_destructive_commands(body: str, locator_prefix: str) -> Iterable[ScanFinding]:
    """Warn on a routine but destructive admin line in skill text (M-120)."""
    for pat in _ADMIN_PATTERNS:
        m = pat.search(body)
        if m:
            yield ScanFinding(
                code="DESTRUCTIVE_COMMAND",
                severity="warn",
                message=f"destructive command: {_quoted(m)}",
                locator=f"{locator_prefix}[{m.start()}:{m.end()}]",
            )
            return


# ── Check #3 — URL exfiltration in skill text + script bodies ──────
#
# Looser net than shell-injection — we flag suspicious outbound POST
# patterns + obviously fishy hosts, but DON'T fail the doc (warn only).
# False positives are likely on legitimate ops scripts that POST to
# internal observability endpoints; surface the finding on the inbox
# card so a human can confirm.
_URL_EXFIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # POST to webhook-style URLs
    re.compile(
        r"(?:curl|wget|http[sx]?\.post|fetch)\s*[^,\n]*POST[^,\n]*(?:webhook|hooks?\.|paste\.|requestbin|ngrok|pipedream)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:curl|wget)\s+(?:-X\s+POST\s+)?[^|>\n]*https?://(?:[^/\s]+\.)?(?:requestbin|webhook\.site|hookbin|interactsh|burpcollaborator)",
        re.IGNORECASE,
    ),
    # Data exfil to known throwaway domains
    re.compile(
        r"https?://(?:[a-z0-9-]+\.)?(?:pastebin\.com|paste\.ee|hastebin\.com|0x0\.st|transfer\.sh)/",
        re.IGNORECASE,
    ),
    # Inline base64-decoded URL fetches — common obfuscation
    re.compile(r"base64\s*-d\s*\|\s*(?:curl|wget|sh|bash)", re.IGNORECASE),
)


def _scan_url_exfil(body: str, locator_prefix: str) -> Iterable[ScanFinding]:
    if not body:
        return
    for pat in _URL_EXFIL_PATTERNS:
        m = pat.search(body)
        if m:
            yield ScanFinding(
                code="URL_EXFILTRATION",
                severity="warn",
                message=f"suspicious outbound network pattern: {_quoted(m)}",
                locator=f"{locator_prefix}[{m.start()}:{m.end()}]",
            )
            return


# ── Check #4 — path violations on support_files ────────────────────
#
# A support_file is a side-car artefact (assets, scripts, templates,
# references) that ships next to a SKILL.md on harness install. We
# only allow paths that:
#   - are non-empty
#   - decode as UTF-8 (when the doc carries the literal bytes)
#   - are relative (no leading "/"), with no ".." segments
#   - are not hidden (no segment starts with ".")
#   - do not target executable system paths
#
# Hits return ``fatal=True`` — the doc is never persisted.
_PATH_TRAVERSAL_RE = re.compile(r"(?:^|[\\/])\.{2}(?:[\\/]|$)")
_HIDDEN_SEGMENT_RE = re.compile(r"(?:^|[\\/])\.[^\\/]")
# Split executable extensions by *auditability*:
#  * Scripts (text) — readable, may live under role='scripts' and pass
#    through the shell-injection + URL-exfil scans.
#  * Binaries — opaque blobs. Sentinel cannot inspect them, so they are
#    NEVER allowed regardless of role. A skill that needs a compiled
#    helper must be Phase-3+ work with a separate trust path.
_SCRIPT_EXT_RE = re.compile(r"\.(?:sh|bash|zsh|ps1|bat|cmd)\b", re.IGNORECASE)
_BINARY_EXT_RE = re.compile(r"\.(?:exe|dll|so|dylib)\b", re.IGNORECASE)
# Matches any Windows drive-letter absolute path — ``C:\``, ``D:\``,
# ``z:\``, etc. The literal-prefix list below only covered C/D, which
# left A/B (floppy), E-Z (mounted external drives), and the
# attacker-favourite ``\\?\C:\`` UNC pass-through silently allowed.
_WINDOWS_ABS_RE = re.compile(r"^[a-zA-Z]:\\", re.IGNORECASE)
# ``/`` alone covers ``/etc``, ``/var``, ``/root``, and every other
# Unix absolute path — they were dead entries. ``~`` catches home-
# expansion patterns; ``\\\\`` catches Windows UNC (``\\server\share``).
_FORBIDDEN_ABS_PREFIXES: tuple[str, ...] = ("/", "~", "\\\\")


def _scan_path_violations(support_files: list, locator_prefix: str) -> Iterable[ScanFinding]:
    if not isinstance(support_files, list):
        return
    for i, sf in enumerate(support_files):
        if not isinstance(sf, dict):
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=f"support_files[{i}] is not a dict",
                fatal=True,
                locator=f"{locator_prefix}[{i}]",
            )
            continue
        path = sf.get("path")
        if not isinstance(path, str) or not path:
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=f"support_files[{i}].path missing or not a string",
                fatal=True,
                locator=f"{locator_prefix}[{i}].path",
            )
            continue
        # ASCII-only check. Support-file paths become directory names
        # on the harness install (Claude Code / OpenClaw) -- Cyrillic-
        # vs-Latin homoglyph attacks on slugs are an established
        # supply-chain vector. The prior check
        # ``path.encode("utf-8").decode("utf-8")`` was dead code
        # (Python 3 ``str`` is already Unicode; the round-trip never
        # raises).
        try:
            path.encode("ascii")
        except UnicodeEncodeError:
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=f"support_files[{i}].path={path!r} contains non-ASCII characters",
                fatal=True,
                locator=f"{locator_prefix}[{i}].path",
            )
            continue
        # Absolute / drive-letter paths
        if any(path.startswith(p) for p in _FORBIDDEN_ABS_PREFIXES) or _WINDOWS_ABS_RE.match(path):
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=f"support_files[{i}].path={path!r} is absolute or targets a system path",
                fatal=True,
                locator=f"{locator_prefix}[{i}].path",
            )
            continue
        # Traversal
        if _PATH_TRAVERSAL_RE.search(path):
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=f"support_files[{i}].path={path!r} contains '..' traversal",
                fatal=True,
                locator=f"{locator_prefix}[{i}].path",
            )
            continue
        # Hidden segments
        if _HIDDEN_SEGMENT_RE.search(path):
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=f"support_files[{i}].path={path!r} contains a hidden segment",
                fatal=True,
                locator=f"{locator_prefix}[{i}].path",
            )
            continue
        # Bare ``.`` components — ``./scripts/x`` or ``scripts/./x``.
        # The traversal regex catches ``..`` but a single-dot segment
        # slips through; it normalizes-away on the harness side but is
        # a strong smell that the writer is trying to obscure the path.
        parts = path.replace("\\", "/").split("/")
        if any(p == "." for p in parts):
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=f"support_files[{i}].path={path!r} contains a bare '.' component",
                fatal=True,
                locator=f"{locator_prefix}[{i}].path",
            )
            continue
        # Executable extensions. Binaries (.exe / .dll / .so / .dylib)
        # are NEVER allowed — Sentinel cannot audit them. Scripts
        # (.sh / .bash / .zsh / .ps1 / .bat / .cmd) are allowed under
        # role='scripts' (where the shell-injection + URL-exfil scans
        # apply), but rejected under any other role to prevent
        # scripts being smuggled in as "templates" or "references".
        role = (sf.get("role") or "").lower()
        if _BINARY_EXT_RE.search(path):
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=(
                    f"support_files[{i}].path={path!r} has a binary executable extension "
                    f"and cannot be safety-audited; only text scripts are permitted"
                ),
                fatal=True,
                locator=f"{locator_prefix}[{i}].path",
            )
            continue
        if _SCRIPT_EXT_RE.search(path) and role not in _SCRIPT_ROLES:
            yield ScanFinding(
                code="PATH_VIOLATION",
                severity="critical",
                message=(
                    f"support_files[{i}].path={path!r} has an executable extension "
                    f"but role={role!r} (not in {sorted(_SCRIPT_ROLES)}). "
                    f"Move to role='scripts' or rename."
                ),
                fatal=True,
                locator=f"{locator_prefix}[{i}].path",
            )


# ── Check #5 — PII detection (regex set, OSS-safe) ─────────────────
#
# Enterprise can substitute the back-v2 PII detector (94.1% accuracy)
# by injecting a callable; this regex set covers the high-frequency
# US patterns + obviously-shaped emails/phones. ``warn`` only — PII
# does not block writes; the inbox renderer redacts on display.
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # SSN — XXX-XX-XXXX, with strict boundaries.
    ("SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b")),
    # Credit card — 13-19 digits with optional dashes/spaces, Visa/MC/Amex/Discover prefixes.
    ("CC", re.compile(r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6011)[- ]?\d{4}[- ]?\d{4}[- ]?\d{1,4}\b")),
    # US phone — (NNN) NNN-NNNN or NNN-NNN-NNNN.
    ("PHONE", re.compile(r"(?:^|[^\d])(?:\(\d{3}\)\s?|\d{3}[- .])\d{3}[- .]\d{4}\b")),
    # Email (warn level — common in evidence quotes, but still flagged).
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)


def _scan_pii(text: object, field_name: str) -> Iterable[ScanFinding]:
    # Same non-``str`` guard as ``_scan_prompt_injection`` — see there.
    if not isinstance(text, str) or not text:
        return
    for kind, pat in _PII_PATTERNS:
        m = pat.search(text)
        if m:
            yield ScanFinding(
                code=f"PII_{kind}",
                severity="warn",
                message=f"possible PII ({kind}) in data.{field_name}",
                locator=f"data.{field_name}[{m.start()}:{m.end()}]",
            )


# ── Check #6 — memory-id stuffing ──────────────────────────────────
def _scan_memory_id_stuffing(data: dict) -> Iterable[ScanFinding]:
    """Warn when a doc cites more memory ids than the inbox will render.

    09/02 L-35: this used to take ``evidence`` and read
    ``evidence["memory_ids"]``, guarded by ``isinstance(evidence, dict)``. The
    only production writer is Forge, and its distill schema declares
    ``evidence`` as a STRING — "a 2-3 sentence human-readable rationale"
    (``distill_prompt``). So the isinstance guard returned on every real doc
    and the check never fired once.

    Meanwhile the ids it was meant to bound live at ``data["cites"]``
    (``all_memory_ids`` in ``forge_service``), top-level and unguarded — so a
    runaway or adversarial distillation could stuff hundreds there with no
    warning, which is precisely what this check exists to surface.

    Both locations are read now. ``cites`` is the real one; the
    ``evidence.memory_ids`` path stays because the documents API accepts
    arbitrary ``data``, so an external writer may legitimately use the dict
    shape this check was originally written against. Ids are unioned rather
    than counted per-field: the cap describes what the renderer will show for
    the doc, not per-location quotas.
    """
    seen: set[str] = set()

    cites = data.get("cites")
    if isinstance(cites, list):
        seen.update(m for m in cites if isinstance(m, str))

    evidence = data.get("evidence")
    if isinstance(evidence, dict):
        mids = evidence.get("memory_ids")
        if isinstance(mids, list):
            seen.update(m for m in mids if isinstance(m, str))

    n_unique = len(seen)
    if n_unique > MAX_MEMORY_IDS_BEFORE_WARN:
        yield ScanFinding(
            code="MEMORY_ID_STUFFING",
            severity="warn",
            message=(
                f"{n_unique} unique cited memory ids "
                f"(> {MAX_MEMORY_IDS_BEFORE_WARN} cap); inbox renderer will truncate"
            ),
            locator="data.cites",
        )


# ── Checks #7 + #8 — size caps ─────────────────────────────────────
def _utf8_len(text: object) -> int:
    if not isinstance(text, str):
        return 0
    return len(text.encode("utf-8"))


def _scan_sizes(data: dict, *, body_max_bytes: int, description_max_bytes: int) -> Iterable[ScanFinding]:
    body_size = _utf8_len(data.get("content"))
    if body_size > body_max_bytes:
        yield ScanFinding(
            code="BODY_TOO_LARGE",
            severity="critical",
            message=(f"data.content size {body_size} bytes exceeds cap {body_max_bytes}; refuse the write"),
            fatal=True,
            locator="data.content",
        )
    desc_size = _utf8_len(data.get("description"))
    if desc_size > description_max_bytes:
        yield ScanFinding(
            code="DESCRIPTION_TOO_LARGE",
            severity="critical",
            message=(
                f"data.description size {desc_size} bytes exceeds cap "
                f"{description_max_bytes}; refuse the write"
            ),
            fatal=True,
            locator="data.description",
        )


# ── Orchestrator ───────────────────────────────────────────────────
async def scan_skill_doc(
    data: dict,
    *,
    mode: ScanMode = "pre-write",
    body_max_bytes: int = DEFAULT_BODY_MAX_BYTES,
    description_max_bytes: int = DEFAULT_DESCRIPTION_MAX_BYTES,
) -> ScanResult:
    """Run the 8 Sentinel checks against ``data`` and return a result.

    The function is deterministic and side-effect-free. Callers cache
    the result by ``content_hash``; re-running on an unchanged body
    yields the same findings.

    Size caps default to the values mirrored from
    ``org_settings.skills_factory.{body_max_bytes,description_max_bytes}``;
    callers that already have a resolved per-tenant settings dict
    should pass the resolved values explicitly so multi-tenant
    deployments respect per-org overrides.

    The ``mode`` parameter is informational — ``pre-write`` and
    ``pre-apply`` run the same checks. It surfaces in audit logs to
    distinguish the two call sites.
    """
    if not isinstance(data, dict):
        # Defensive: callers should never reach here with non-dict data
        # (validate_and_normalize_skill_write already 422s on this),
        # but Sentinel must not raise — the call site uses ``any_fatal``
        # to decide reject vs quarantine.
        return ScanResult(
            state="failed",
            scanned_at=_now_iso(),
            critical=1,
            warn=0,
            info=0,
            findings=(
                ScanFinding(
                    code="MALFORMED_INPUT",
                    severity="critical",
                    message="scan_skill_doc received a non-dict input",
                    fatal=True,
                ),
            ),
        )

    findings: list[ScanFinding] = []

    # Checks #1 + #5 over the natural-language fields.
    #
    # 09/02 L-02: ``name`` was missing from this tuple. It is not a
    # cosmetic omission — the plugin's skill reconciler synthesises the
    # YAML frontmatter of ``<slug>/SKILL.md`` from ``data.name`` and
    # ``data.description`` whenever the body has none of its own, so an
    # injection marker in a skill's display NAME is written to the file
    # the agent harness loads, having passed a scan that looked at its
    # neighbour ``description`` and not at it. Forge takes ``name``
    # straight from the LLM distill response with an ``isinstance(str)``
    # check and nothing else.
    for field_name in ("name", "content", "description", "summary", "goal"):
        findings.extend(_scan_prompt_injection(data.get(field_name), field_name))
        findings.extend(_scan_pii(data.get(field_name), field_name))

    # ``tags`` is the other field the L-02 row named, and it cannot just
    # join the tuple above: it is a ``list[str]``, not a string, so it
    # needs per-element scanning to get a usable locator (a bare
    # ``data.tags`` would send an operator hunting through the list).
    # The non-``str`` guard now lives in the two scanners — a list
    # reaching ``re.search`` raised ``TypeError`` — but elements are
    # still filtered here so a mixed list scans the strings in it
    # instead of being skipped wholesale.
    tags = data.get("tags")
    if isinstance(tags, list):
        for i, tag in enumerate(tags):
            if not isinstance(tag, str):
                continue
            findings.extend(_scan_prompt_injection(tag, f"tags[{i}]"))
            findings.extend(_scan_pii(tag, f"tags[{i}]"))

    evidence = data.get("evidence")
    if isinstance(evidence, str):
        # Some writers pass evidence as a bare string (the legacy SF-002
        # convention before the dict-form was introduced). Scan it the
        # same way as the dict's quoted-text subfields.
        findings.extend(_scan_prompt_injection(evidence, "evidence"))
        findings.extend(_scan_pii(evidence, "evidence"))
    elif isinstance(evidence, dict):
        # Evidence often contains quoted user/agent text; PII + injection
        # markers travel through unredacted, so we scan those too.
        # The walker recurses into nested dicts + lists (depth-bounded
        # in ``_EVIDENCE_RECURSE_MAX_DEPTH``) so a writer can't smuggle
        # injection markers past the scan by burying them in
        # ``evidence.context.user_message`` or similar. ``memory_ids``
        # is a non-string leaf and is handled by the dedicated check #6.
        for locator, text_val in _iter_evidence_strings(evidence, "evidence"):
            findings.extend(_scan_prompt_injection(text_val, locator.removeprefix("evidence.")))
            findings.extend(_scan_pii(text_val, locator.removeprefix("evidence.")))

    # Checks #2 + #3 over the doc text that ships. ``content`` is the
    # SKILL.md body the plugin's reconciler writes to disk verbatim and
    # the harness loads as instructions; ``description`` lands in the
    # synthesised frontmatter beside it, and ``summary`` is what the
    # inbox card shows a reviewer. These checks used to run over
    # ``support_files`` alone — a key no production writer populates —
    # so a body whose steps said ``curl … | bash`` scanned clean and,
    # under ``auto_promote_clean``, went straight to ``active``. ``name``
    # lands in that frontmatter too (M-91). The attack patterns are critical
    # (quarantine → human review); routine admin lines are a warning here
    # (M-120, see ``_ADMIN_PATTERNS``); URL patterns stay warn-only, as for
    # script bodies.
    for field_name in ("name", "content", "summary", "description"):
        doc_text = data.get(field_name)
        if not isinstance(doc_text, str):
            continue
        locator = f"data.{field_name}"
        findings.extend(_scan_shell_injection(doc_text, locator, _ATTACK_PATTERNS))
        findings.extend(_scan_destructive_commands(doc_text, locator))
        findings.extend(_scan_url_exfil(doc_text, locator))

    # Checks #2 + #3 — shell-injection runs on EVERY support_file body
    # regardless of role: a malicious writer could ship a fork-bomb
    # under role='templates' / 'assets' / 'references' to dodge the
    # role gate, and the path-violation check only catches executable
    # *extensions*, not content. URL-exfil stays role-gated (warn-only,
    # false-positive sensitive on legit ops scripts).
    support_files = data.get("support_files")
    if isinstance(support_files, list):
        for i, sf in enumerate(support_files):
            if not isinstance(sf, dict):
                continue
            role = (sf.get("role") or "").lower()
            body = sf.get("content") or sf.get("body") or ""
            if not isinstance(body, str):
                continue
            findings.extend(_scan_shell_injection(body, f"data.support_files[{i}].content"))
            if role in _SCRIPT_ROLES:
                findings.extend(_scan_url_exfil(body, f"data.support_files[{i}].content"))

    # Check #4 — path violations (fatal). Runs over all support_files
    # regardless of role; even non-script artefacts must live under
    # a safe relative path.
    if support_files is not None:
        findings.extend(_scan_path_violations(support_files, "data.support_files"))

    # Check #6 — memory-id stuffing.
    findings.extend(_scan_memory_id_stuffing(data))

    # Checks #7 + #8 — size caps.
    findings.extend(
        _scan_sizes(
            data,
            body_max_bytes=body_max_bytes,
            description_max_bytes=description_max_bytes,
        )
    )

    critical = sum(1 for f in findings if f.severity == "critical")
    warn = sum(1 for f in findings if f.severity == "warn")
    info = sum(1 for f in findings if f.severity == "info")

    # State picks the worst outcome:
    #   fatal       → quarantined (or, equivalently, refused by caller)
    #   critical>0  → quarantined
    #   warn or 0   → clean
    state: ScanState
    if any(f.fatal for f in findings) or critical > 0:
        state = "quarantined"
    else:
        state = "clean"

    logger.debug(
        "sentinel_scan: state=%s critical=%d warn=%d info=%d mode=%s",
        state,
        critical,
        warn,
        info,
        mode,
    )

    return ScanResult(
        state=state,
        scanned_at=_now_iso(),
        critical=critical,
        warn=warn,
        info=info,
        findings=tuple(findings),
    )
