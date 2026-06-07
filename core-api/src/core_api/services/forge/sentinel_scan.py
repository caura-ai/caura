"""Sentinel scanner for Skill Factory skill docs (plan §9).

Phase 0 SHIPS A STUB. The signature, return type, and call sites are
real so SF-002 (``memclaw_doc`` skills-write adjustments) can invoke
it end-to-end and the lifecycle plumbing is exercised in Phase 0
tests. Phase 2 (HITL Inbox + Sentinel) fills in the 8 real checks.

The stub:

  - returns ``state="clean"`` with zero findings for any input
  - obeys size-related ``HTTPException``-style guards by returning a
    structured ScanFinding the caller can promote to a 422 — the
    caller decides whether a given finding is fatal (path / size
    violations) or quarantine-worthy (prompt-injection / shell-inject)
  - is deterministic + fast (no LLM, no network) by design

Both call sites are introduced in Phase 0:

  1. ``routes/documents.py`` — pre-write hook on every
     ``collection='skills'`` upsert (SF-002 adjustment #7).
  2. (Phase 2) ``services/skill_lifecycle.py`` — pre-apply hook on
     ``staged → active`` transitions, so a doc that became unsafe
     between propose and apply is caught before mutation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)


ScanState = Literal["pending", "clean", "failed", "quarantined"]
ScanMode = Literal["pre-write", "pre-apply"]


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


async def scan_skill_doc(
    data: dict,
    *,
    mode: ScanMode = "pre-write",
) -> ScanResult:
    """Phase 0 STUB. Returns ``state='clean'`` with zero findings.

    Phase 2 replaces the body with the 8 real checks (plan §9):

      1. prompt-injection markers in content/description/summary/evidence
      2. shell-injection in support_files/scripts/*
      3. URL exfiltration patterns in scripts
      4. path violations on support_files (hard-reject; ``fatal=True``)
      5. PII in content/evidence
      6. memory-id stuffing (> 20 cites)
      7. body size > body_max_bytes (hard-reject; ``fatal=True``)
      8. description size > description_max_bytes (hard-reject; ``fatal=True``)

    Until then, every input scans clean. The call site is real so
    that Phase 2 is a body-only swap.

    Performance budget (Phase 2): p95 < 500ms on a 40KB body, regex +
    classifiers + path checks, NO LLM, NO network. Cacheable by
    ``content_hash``.
    """
    logger.debug(
        "sentinel_scan stub invoked (Phase 0; always clean)",
        extra={
            "mode": mode,
            "data_keys": sorted(data) if isinstance(data, dict) else None,
            "stub": True,
            "phase": "0",
        },
    )
    return ScanResult(
        state="clean",
        scanned_at=_now_iso(),
        critical=0,
        warn=0,
        info=0,
        findings=(),
    )
