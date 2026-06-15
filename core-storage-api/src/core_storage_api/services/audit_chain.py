"""Tamper-evident audit hash-chain primitives (eToro governance).

Pure, side-effect-free helpers shared by the chained-insert path
(:meth:`PostgresService.audit_add_batch_chained`) and the verifier
(:meth:`PostgresService.audit_verify_chain`). Defining canonicalization +
hashing in ONE place guarantees writer and verifier agree byte-for-byte —
any drift between the two would make every chain look tampered.

The chain is per-tenant: each event carries a monotonic ``seq`` (from 1),
links to the prior event via ``prev_hash``, and commits
``event_hash = SHA256(canonical_event || prev_hash)``. A tenant's first
event (seq=1) chains onto :data:`GENESIS_PREV_HASH`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

# 32 zero bytes — the prev_hash a tenant's first (seq=1) event chains onto.
# A fixed, well-known sentinel so the verifier recomputes genesis exactly as
# the writer produced it, with no special-casing.
GENESIS_PREV_HASH: bytes = b"\x00" * 32


def canonical_created_at(created_at: datetime) -> str:
    """Render ``created_at`` deterministically (UTC, microseconds, trailing Z).

    The hash binds the timestamp, so writer and verifier MUST format it
    identically. Normalize to UTC at a fixed microsecond precision — never
    hash a client-supplied or locale-dependent rendering.
    """
    return created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _normalise_numbers(obj: object) -> object:
    """Canonicalize numbers so the hash survives a JSONB round-trip.

    A ``detail`` dict is hashed at write time from Python values, then
    re-hashed at verify time from the value read back out of the JSONB column.
    On some PostgreSQL versions an integral float (``1.0``) is rendered as an
    int (``1``) through that round-trip, which changes ``json.dumps`` output
    (``"1.0"`` vs ``"1"``) and would trip a false ``event_hash_mismatch`` on an
    otherwise-intact row. Collapsing integral floats to ints on BOTH sides
    (writer + verifier both call :func:`canonical_event`) makes them agree
    regardless of the driver/PG numeric representation. (pg16 — the CI/staging
    image — actually preserves ``1.0``; this is defense for other PG versions
    and any future float-bearing audit detail.)
    """
    if isinstance(obj, bool):  # bool is an int subclass — leave it alone
        return obj
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if isinstance(obj, dict):
        return {k: _normalise_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalise_numbers(v) for v in obj]
    return obj


def _canonical_resource_id(resource_id: str | UUID | None) -> str | None:
    """Canonical string form of ``resource_id`` for hashing.

    ``resource_id`` is persisted in a UUID column, so the verifier reads it
    back as a ``uuid.UUID`` whose ``str()`` is lowercase-hyphenated — while a
    batch event can supply it as a raw JSON string (possibly uppercase /
    non-canonical). Normalizing through ``UUID`` here, inside
    :func:`canonical_event`, makes writer and verifier hash the identical form
    without either call site having to remember to pre-normalize — the same
    guarantee :func:`_normalise_numbers` gives ``detail``. A non-UUID string is
    returned unchanged (it would fail the UUID-column insert anyway, but
    hash/verify stay consistent); falsy values collapse to ``None``.
    """
    if not resource_id:
        return None
    if isinstance(resource_id, str):
        try:
            return str(UUID(resource_id))
        except ValueError:
            return resource_id
    return str(resource_id)


def canonical_event(
    *,
    tenant_id: str,
    seq: int,
    agent_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None,
    detail: dict | None,
    created_at_iso: str,
) -> bytes:
    """Deterministic byte serialization of the hashed event fields.

    Excludes the random ``id`` (adds nothing to integrity, unknown until
    insert) and the chain columns themselves. ``sort_keys`` + compact
    separators make key order and whitespace irrelevant, so an auditor can
    reproduce this from the persisted row alone. ``detail`` numbers are
    normalised (see :func:`_normalise_numbers`) and ``resource_id`` is
    canonicalized (see :func:`_canonical_resource_id`) so the hash is stable
    across the JSONB round-trip and the UUID-column read-back.
    """
    obj = {
        "tenant_id": tenant_id,
        "seq": seq,
        "agent_id": agent_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": _canonical_resource_id(resource_id),
        "detail": _normalise_numbers(detail),
        "created_at": created_at_iso,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_event_hash(canonical: bytes, prev_hash: bytes) -> bytes:
    """``SHA256(canonical_event || prev_hash)`` → 32 raw bytes.

    Named ``compute_event_hash`` (not ``event_hash``) so call sites don't
    shadow the ``AuditLog.event_hash`` column attribute.
    """
    return hashlib.sha256(canonical + prev_hash).digest()


# ── PII-safe detail guard (defense in depth) ─────────────────────────
#
# The audit log is long-lived, broadly readable and replicated; storing a
# raw secret in ``detail`` would defeat the very masking it audits. Emit
# sites build PII-free details (category + span offsets + content hash only),
# but we re-check storage-side BEFORE hashing so the chain can never attest a
# raw value.
#
# This is a deliberately CONSERVATIVE (precision-over-recall) backstop: a hit
# rolls back the whole batch, which would leave a gap in the tamper-evident
# chain, so a false positive is worse than a miss here — the authoritative
# detector is the deterministic ``common.governance`` library on the write
# path, not this guard. We therefore match only an UNAMBIGUOUSLY card-shaped
# value (four groups of four digits, separated) rather than any 13-19 digit
# run, which would also flag benign 16-digit order IDs / transaction refs /
# phone numbers that legitimately appear in audit detail strings.
_CARD_SHAPE = re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b")
_SSN_SHAPE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Our own audit details legitimately carry hash tokens (e.g. content_sha256,
# "hmac-sha256:<hex>"). Skip those leaves so a hash that happens to hold a long
# digit run can't false-positive the card check.
_HASH_TOKEN = re.compile(r"\A(?:[a-z0-9]+-[a-z0-9]+:)?[0-9a-fA-F]{32,}\Z")


class PIIInAuditError(ValueError):
    """Raised when an audit ``detail`` appears to embed a raw secret."""


def assert_pii_safe(detail: dict | None) -> None:
    """Reject an audit ``detail`` whose string leaves look like raw PII.

    Backstop only — the contract is that emit sites never put raw values in
    ``detail`` (they emit category + spans + hashes). Raising here fails the
    audit write loudly rather than silently chaining a secret into the
    tamper-evident log.
    """
    if not detail:
        return
    for value in _iter_str_leaves(detail):
        if _HASH_TOKEN.match(value):
            continue
        if _CARD_SHAPE.search(value) or _SSN_SHAPE.search(value):
            raise PIIInAuditError(
                "audit detail appears to contain raw PII; emit category/spans/hashes instead"
            )


def _iter_str_leaves(obj: object) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_str_leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_str_leaves(v)
