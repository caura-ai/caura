"""Sentinel scanner for Skill Factory skill docs (plan §9).

Phase 2 ships the 8 real checks. The Phase 0 stub returned
``state='clean'`` for every input; the call sites
(``routes/documents.py`` pre-write hook + Phase 3 pre-apply hook in
``services/skill_lifecycle.py``) are unchanged — Phase 2 is a body-only
swap.

The 8 checks (one ``ScanFinding`` per hit; severity drives caller
behavior — see :class:`ScanFinding`):

  1. **prompt-injection** markers in content / description / summary /
     evidence (``critical``) — quarantine.
  2. **shell-injection** patterns inside ``support_files`` entries
     whose ``role`` looks like a script (``critical``) — quarantine.
  3. **URL exfiltration** patterns in the same script bodies
     (``warn``) — surfaces on the inbox card; doc may still proceed.
  4. **path violations** on ``support_files`` (absolute, traversal,
     hidden, executable, non-UTF8) — ``fatal=True``; refuse the write.
  5. **PII** (SSN / credit card / phone / email) in content / evidence
     (``warn``; redact-on-display flag set by the inbox renderer).
  6. **memory-id stuffing** — more than 20 unique cited memory ids in
     ``data.evidence.memory_ids`` (``warn``; capped at 20 on render).
  7. **body size** — UTF-8 byte length of ``data.content`` exceeds
     ``body_max_bytes`` — ``fatal=True``.
  8. **description size** — UTF-8 byte length of ``data.description``
     exceeds ``description_max_bytes`` — ``fatal=True``.

Performance budget (plan §9): p95 < 500ms on a 40KB body — regex +
path checks + classifiers, **NO LLM, NO network**. Cacheable by
``content_hash``.

The scanner is deterministic + side-effect-free: callers cache results
by ``content_hash``, so re-scanning an unchanged doc is free.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

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


def _scan_prompt_injection(text: str | None, field_name: str) -> Iterable[ScanFinding]:
    if not text:
        return
    for pat in _PROMPT_INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            yield ScanFinding(
                code="PROMPT_INJECTION",
                severity="critical",
                message=f"prompt-injection marker detected in data.{field_name}: {m.group(0)!r}",
                locator=f"data.{field_name}[{m.start()}:{m.end()}]",
            )
            # One finding per field is sufficient — the inbox card
            # surfaces the first hit; users review the raw text anyway.
            return


# ── Check #2 — shell-injection in script bodies ────────────────────
#
# Only fires on support_files whose ``role`` looks script-y (the
# harness install ships scripts/* under that path). Skips
# non-executable artefacts like README/templates/references.
_SCRIPT_ROLES: frozenset[str] = frozenset({"scripts", "script", "exec", "command"})

_SHELL_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Match both ``-rf`` and ``-fr`` — functionally identical, equally
    # common in the wild. A single-flag regex (``-rf`` only) lets the
    # rarer-but-still-trivial ``-fr`` slip through.
    re.compile(r"\brm\s+-(?:rf|fr)\s+/(?!tmp\b)", re.IGNORECASE),  # rm -rf / (but not /tmp)
    re.compile(r"\$\(\s*rm\s", re.IGNORECASE),
    re.compile(r"(?:^|[;&|`])\s*rm\s+-(?:rf|fr)\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r":\(\s*\)\s*\{\s*:\|:&\s*\}\s*;\s*:", re.MULTILINE),  # fork bomb
    re.compile(r"\bdd\s+if=/dev/(?:zero|random|urandom)\b", re.IGNORECASE),
    re.compile(r"\bmkfs\.[a-z0-9]+\s", re.IGNORECASE),
    re.compile(r"\bchmod\s+(?:-R\s+)?[0-7]*7{2,3}\b"),  # chmod 777 / 0777
    re.compile(r"\b(?:cat|less|more|head|tail)\s+/etc/(?:passwd|shadow|sudoers)\b", re.IGNORECASE),
    re.compile(r"(?:curl|wget)\s[^|]*\|\s*(?:sh|bash|zsh|sudo\s+sh)\b", re.IGNORECASE),  # pipe-to-shell
    re.compile(r"\beval\s*\(\s*(?:base64_decode|atob|fromCharCode)", re.IGNORECASE),
    re.compile(r"\bexec\s*\(\s*['\"]?(?:cmd|powershell|sh|bash)\b", re.IGNORECASE),
)


def _scan_shell_injection(body: str, locator_prefix: str) -> Iterable[ScanFinding]:
    if not body:
        return
    for pat in _SHELL_INJECTION_PATTERNS:
        m = pat.search(body)
        if m:
            yield ScanFinding(
                code="SHELL_INJECTION",
                severity="critical",
                message=f"shell-injection pattern detected: {m.group(0)!r}",
                locator=f"{locator_prefix}[{m.start()}:{m.end()}]",
            )
            return


# ── Check #3 — URL exfiltration in script bodies ───────────────────
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
                message=f"suspicious outbound network pattern: {m.group(0)[:120]!r}",
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


def _scan_pii(text: str | None, field_name: str) -> Iterable[ScanFinding]:
    if not text:
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
def _scan_memory_id_stuffing(evidence: dict | None) -> Iterable[ScanFinding]:
    if not isinstance(evidence, dict):
        return
    mids = evidence.get("memory_ids")
    if not isinstance(mids, list):
        return
    n_unique = len({m for m in mids if isinstance(m, str)})
    if n_unique > MAX_MEMORY_IDS_BEFORE_WARN:
        yield ScanFinding(
            code="MEMORY_ID_STUFFING",
            severity="warn",
            message=(
                f"evidence.memory_ids has {n_unique} unique cites "
                f"(> {MAX_MEMORY_IDS_BEFORE_WARN} cap); inbox renderer will truncate"
            ),
            locator="data.evidence.memory_ids",
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
    for field_name in ("content", "description", "summary", "goal"):
        findings.extend(_scan_prompt_injection(data.get(field_name), field_name))
        findings.extend(_scan_pii(data.get(field_name), field_name))

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
    findings.extend(_scan_memory_id_stuffing(evidence))

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
