"""Skill lifecycle service (plan §5).

Phase 0 ships only the pre-write validator (SF-002) used by
``routes/documents.py`` to enforce the 7 adjustments listed in plan
§4 on every ``collection='skills'`` write:

  1. schema validator (required fields present + typed)
  2. ``description`` cap (default 160 bytes, configurable)
  3. ``content`` body cap (default 40 000 bytes, configurable)
  4. ``source`` defaulting + RBAC (regular caller → ``agent``;
     ``manual`` admin-only; ``forge`` reserved for internal callers)
  5. ``status`` defaulting + RBAC (regular caller → ``staged``;
     ``active`` admin-only; ``candidate`` reserved for Forge)
  6. auto-fill server-controlled fields (``content_hash``,
     ``origin.agent_id``, ``created_at``, ``updated_at``)
  7. Sentinel pre-scan + ``kind='update'`` hash-binding

Phase 2 will extend this module with the staged → active /
quarantined / rejected / stale transition logic. Phase 4 adds the
v2 update-proposal hash-bind check.

Everything in this module is GATED by
``org_settings.skills_factory.enabled`` (default ``False``).
The route checks the flag and only calls
:func:`validate_and_normalize_skill_write` when it is on. OSS
tenants that have never opted in see ZERO behavior change.

The validator returns the *normalized* doc body (server-controlled
fields auto-filled, hash computed, scan result attached). The caller
swaps it into ``body.data`` before the existing embedding +
storage-api round-trip.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

from fastapi import HTTPException

from core_api.services.forge.sentinel_scan import ScanResult, scan_skill_doc

logger = logging.getLogger(__name__)


# Allowed enum values. Mirrors plan §3 + §5. Kept here, not in the
# schema validator, so a typo on the route side fails fast at the
# enum check rather than as a generic 'invalid value'.
ALLOWED_SOURCES: frozenset[str] = frozenset({"forge", "agent", "manual", "imported"})
ALLOWED_KINDS: frozenset[str] = frozenset({"create", "update"})
ALLOWED_STATUSES: frozenset[str] = frozenset(
    {"candidate", "staged", "active", "rejected", "quarantined", "stale", "deprecated"}
)

# Sources only an admin call (or, eventually, the internal Forge
# lifecycle worker) may write directly. Regular agent calls cannot
# self-assign these.
ADMIN_ONLY_SOURCES: frozenset[str] = frozenset({"manual"})
INTERNAL_ONLY_SOURCES: frozenset[str] = frozenset({"forge"})

# Statuses an admin write may land in directly. Everything else
# defaults to ``staged`` or is reserved for the lifecycle machinery.
ADMIN_ONLY_STATUSES: frozenset[str] = frozenset({"active"})
INTERNAL_ONLY_STATUSES: frozenset[str] = frozenset({"candidate"})

# System-driven terminal / hold states. No HTTP caller — not Forge,
# not admin, not a regular agent — may set these directly via
# ``memclaw_doc``. The legitimate transitions to these states are:
#   - ``quarantined`` ← Sentinel scanner inside this validator
#   - ``rejected``    ← HITL Inbox Reject action (Phase 2 dashboard)
#   - ``stale``       ← hash-binding stale detection (Phase 4)
#   - ``deprecated``  ← skill lifecycle deprecation flow (Phase 4)
# Letting an arbitrary write set them would let a misbehaving agent
# silently retire an active skill or hide a candidate from review.
SYSTEM_ONLY_STATUSES: frozenset[str] = frozenset({"quarantined", "rejected", "stale", "deprecated"})

REQUIRED_TOP_LEVEL_KEYS: tuple[str, ...] = ("name", "slug", "description", "domain", "kind", "source")

# ``slug`` regex mirrors the existing ``_SKILL_SLUG_RE`` in
# ``routes/documents.py``. Duplicated here intentionally — the route
# uses the regex on ``doc_id``; this module uses it on
# ``data["slug"]``, and the two can diverge in the future
# (e.g. doc_id namespacing prefix). Keep them in lockstep until that
# divergence is needed.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


# Default caps (mirrored in org_settings.DEFAULT_SETTINGS.skills_factory.*).
# Listed here as fallback only — the route resolves the actual values
# from the tenant settings and passes them in. These defaults are used
# by direct unit tests against this module that don't go through the
# settings layer.
_DEFAULT_DESCRIPTION_MAX_BYTES = 160
_DEFAULT_BODY_MAX_BYTES = 40_000


@dataclass(frozen=True)
class SkillWriteContext:
    """Caller context the validator needs. Constructed by the route
    from the AuthContext + org settings; bundled here so the
    validator stays trivially unit-testable without FastAPI deps."""

    caller_agent_id: str | None
    is_admin: bool
    is_internal_forge: bool  # True only when the internal lifecycle worker is the caller
    description_max_bytes: int = _DEFAULT_DESCRIPTION_MAX_BYTES
    body_max_bytes: int = _DEFAULT_BODY_MAX_BYTES
    # True when the caller is the Inbox ``edit`` endpoint, which
    # PRESERVES the existing doc's ``source`` rather than minting a
    # new one. The validator's INTERNAL_ONLY_SOURCES /
    # ADMIN_ONLY_SOURCES RBAC checks are intended to gate the
    # MINTING surface; an inbox edit of a Forge-minted candidate
    # would otherwise 403 because ``source='forge'`` survives the
    # snapshot+restore round-trip. The inbox endpoint already
    # enforces admin via ``_require_inbox_admin``, so relaxing both
    # source checks under this flag is safe.
    is_inbox_edit: bool = False


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bytes_len(value: str) -> int:
    """UTF-8 byte length (NOT character count) — what HTTP caps care about."""
    return len(value.encode("utf-8"))


def _raise(status_code: int, detail: str) -> NoReturn:
    raise HTTPException(status_code=status_code, detail=detail)


async def validate_and_normalize_skill_write(
    data: dict,
    *,
    ctx: SkillWriteContext,
    live_skill_doc: dict | None = None,
) -> tuple[dict, ScanResult]:
    """Run the 7 SF-002 adjustments on a skills-collection write.

    Returns ``(normalized_data, scan_result)``. The route swaps
    ``body.data = normalized_data`` and proceeds to embedding +
    upsert. The scan result is already merged into
    ``normalized_data['scan']`` — the separate return value lets
    the caller decide what to do about a quarantined result (e.g.
    log it specially, surface a different audit code).

    Raises ``HTTPException`` for:

      - 422 missing/malformed required fields, over-cap sizes, slug
        regex mismatch, invalid enum values, scan ``fatal`` findings
      - 403 RBAC violations on ``source`` / ``status``
      - 409 ``kind='update'`` hash mismatch vs live target
      - 404 ``kind='update'`` targeting a non-existent live skill
    """

    if not isinstance(data, dict):
        _raise(422, "skills write requires a JSON object 'data' body.")

    # Work on a shallow copy so the caller's body isn't mutated
    # under their feet. Nested mutations (origin, scan) are explicit
    # below.
    out: dict[str, Any] = dict(data)

    # ── Adjustment 1: schema validator hook ──────────────────────
    missing = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in out]
    if missing:
        _raise(
            422,
            f"skills write missing required key(s): {sorted(missing)}. "
            f"All of {sorted(REQUIRED_TOP_LEVEL_KEYS)} must be present.",
        )

    # Typed top-level fields. ``content`` is required for non-imported
    # docs but stays optional for the source=imported backwards-compat
    # path (eToro's pointer-only docs lack it by design — they get
    # source=imported via the SF-001 migration). We defer the content
    # presence check until after source resolution.
    for k in ("name", "slug", "description", "domain"):
        if not isinstance(out[k], str):
            _raise(422, f"skills write: data['{k}'] must be a string.")
    if not _SLUG_RE.fullmatch(out["slug"]):
        _raise(
            422,
            f"skills write: data['slug'] must match {_SLUG_RE.pattern} — got {out['slug']!r}.",
        )

    if out["kind"] not in ALLOWED_KINDS:
        _raise(
            422,
            f"skills write: data['kind'] must be one of {sorted(ALLOWED_KINDS)}, got {out['kind']!r}.",
        )
    if out["source"] not in ALLOWED_SOURCES:
        _raise(
            422,
            f"skills write: data['source'] must be one of {sorted(ALLOWED_SOURCES)}, got {out['source']!r}.",
        )

    # Optional top-level types (only enforced when present)
    if "tags" in out and not (isinstance(out["tags"], list) and all(isinstance(t, str) for t in out["tags"])):
        _raise(422, "skills write: data['tags'] must be a list of strings if present.")

    # ── Adjustment 4: source defaulting + RBAC ───────────────────
    src = out["source"]
    # The INTERNAL_ONLY / ADMIN_ONLY source RBAC gates the MINTING
    # surface (a regular caller cannot create a new skill claiming
    # ``source='forge'``). Inbox edits PRESERVE the existing source
    # rather than minting a new one, so these checks would block
    # legitimate operator edits of Forge candidates. The inbox edit
    # endpoint enforces admin via ``_require_inbox_admin`` already,
    # so bypassing both source-RBAC arms under this flag is safe.
    if not ctx.is_inbox_edit:
        if src in INTERNAL_ONLY_SOURCES and not ctx.is_internal_forge:
            _raise(
                403,
                f"skills write: source={src!r} is reserved for the internal Forge worker. "
                "Regular agent and admin callers cannot mint source=forge directly.",
            )
        if src in ADMIN_ONLY_SOURCES and not ctx.is_admin:
            _raise(
                403,
                f"skills write: source={src!r} requires admin role. "
                "Use source='agent' for a regular agent-direct write.",
            )

    # ── Adjustment 5: status defaulting + RBAC ───────────────────
    if "status" not in out:
        # Forge candidates flow through the lifecycle worker (which sets
        # candidate explicitly); a non-Forge caller defaults to staged
        # so the doc lands in the inbox HITL gate.
        out["status"] = "candidate" if ctx.is_internal_forge else "staged"
    status = out["status"]
    if status not in ALLOWED_STATUSES:
        _raise(
            422,
            f"skills write: data['status'] must be one of {sorted(ALLOWED_STATUSES)}, got {status!r}.",
        )
    if status in INTERNAL_ONLY_STATUSES and not ctx.is_internal_forge:
        _raise(
            403,
            f"skills write: status={status!r} is reserved for the internal Forge worker.",
        )
    if status in ADMIN_ONLY_STATUSES and not ctx.is_admin:
        _raise(
            403,
            f"skills write: status={status!r} requires admin role. "
            "Use HITL Inbox approval to transition staged → active.",
        )
    if status in SYSTEM_ONLY_STATUSES:
        # System-managed terminal / hold states cannot be set by ANY
        # HTTP caller (Forge, admin, or otherwise). The legitimate
        # writers are: Sentinel (quarantined, set inside this same
        # validator after the scan), HITL Inbox Reject action
        # (rejected), hash-binding stale detection (stale), and the
        # deprecation flow (deprecated).
        _raise(
            403,
            f"skills write: status={status!r} is system-managed and cannot be "
            "written directly. These transitions happen via Sentinel "
            "scan / Inbox Reject / hash-binding stale detection / "
            "deprecation flow — not via memclaw_doc.",
        )

    # ── Adjustment 2: description cap ────────────────────────────
    desc_bytes = _bytes_len(out["description"])
    if desc_bytes > ctx.description_max_bytes:
        _raise(
            422,
            f"skills write: data['description'] is {desc_bytes} bytes; "
            f"cap is {ctx.description_max_bytes} (configurable via "
            "org_settings.skills_factory.description_max_bytes).",
        )

    # ── Adjustment 3: body cap ───────────────────────────────────
    content = out.get("content")
    if content is not None:
        if not isinstance(content, str):
            _raise(422, "skills write: data['content'] must be a string if present.")
        body_bytes = _bytes_len(content)
        if body_bytes > ctx.body_max_bytes:
            _raise(
                422,
                f"skills write: data['content'] is {body_bytes} bytes; "
                f"cap is {ctx.body_max_bytes} (configurable via "
                "org_settings.skills_factory.body_max_bytes).",
            )
    elif src not in {"imported"}:
        # Non-imported docs must carry a body. Imported docs (the
        # eToro pointer-only shape) are allowed to skip it.
        _raise(
            422,
            f"skills write: data['content'] is required when source={src!r}. "
            "Only source='imported' may omit the content body.",
        )

    # ── Adjustment 6: auto-fill server-controlled fields ─────────
    now = _now_iso()
    if "created_at" not in out:
        out["created_at"] = now
    out["updated_at"] = now  # always bumped by server

    # content_hash is derived from content (when present). For
    # imported pointer-only docs (no content), leave it absent so
    # downstream code can branch on `'content_hash' not in data`.
    if content is not None:
        out["content_hash"] = _sha256(content)

    # origin.agent_id is server-stamped from the auth context, never
    # trusted from the body. Other origin fields (session_key, run_id,
    # message_id) ride through unchanged — the client may legitimately
    # set them.
    origin = dict(out.get("origin") or {})
    origin["agent_id"] = ctx.caller_agent_id or "unknown"
    out["origin"] = origin

    # ── Adjustment 7a: kind='update' hash-binding ────────────────
    if out["kind"] == "update":
        target = out.get("target")
        if not isinstance(target, dict) or "target_content_hash" not in target:
            _raise(
                422,
                "skills write: kind='update' requires data['target']['target_content_hash'].",
            )
        if live_skill_doc is None:
            _raise(
                404,
                f"skills write: kind='update' targets slug={out['slug']!r} but no live skill exists.",
            )
        live_data = live_skill_doc.get("data") if isinstance(live_skill_doc, dict) else None
        live_hash = (live_data or {}).get("content_hash")
        if live_hash is None:
            # The live doc predates SF-002 (e.g. an imported pointer-
            # only skill with no content). Reject the update — we have
            # nothing to bind to, and silently letting it through would
            # let the update clobber an imported doc.
            _raise(
                409,
                f"skills write: kind='update' on slug={out['slug']!r} cannot bind — "
                "the live skill has no content_hash (likely an imported pointer-only doc). "
                "Re-author it as a fresh kind='create' if appropriate.",
            )
        if target["target_content_hash"] != live_hash:
            _raise(
                409,
                f"skills write: target_content_hash mismatch for slug={out['slug']!r}. "
                f"Caller saw {target['target_content_hash']!r}; live is {live_hash!r}. "
                "Live skill changed between propose and write — re-revise the proposal.",
            )

    # ── Adjustment 7b: Sentinel pre-scan ─────────────────────────
    # Forward the tenant-resolved caps so per-tenant overrides via
    # ``org_settings.skills_factory.{body,description}_max_bytes`` are
    # respected. Without these kwargs, Sentinel falls back to its
    # module defaults (40 000 / 160) and over-rejects (or under-rejects)
    # for tenants that have customized the cap.
    scan_result = await scan_skill_doc(
        out,
        mode="pre-write",
        body_max_bytes=ctx.body_max_bytes,
        description_max_bytes=ctx.description_max_bytes,
    )
    if scan_result.any_fatal:
        # Hard-reject findings (size / path violations) come back as
        # fatal. Surface the first one verbatim — easier debugging
        # than a generic 422.
        first_fatal = next(f for f in scan_result.findings if f.fatal)
        _raise(422, f"skills write: {first_fatal.code} — {first_fatal.message}")
    if scan_result.state == "quarantined":
        # Non-fatal critical findings (prompt-injection / shell-inject)
        # let the doc persist but in quarantine. Plan §4 adjustment #7.
        out["status"] = "quarantined"
        out["quarantined_at"] = now
    out["scan"] = scan_result.as_doc_field()

    return out, scan_result


# ── SF-204 — Auto-gate evaluator (candidate → staged) ─────────────


@dataclass(frozen=True)
class GateOutcome:
    """One gate's verdict. ``passed=False`` carries a human-readable
    ``reason`` so the audit log can pin which gate blocked promotion.
    """

    name: str
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class AutoGateResult:
    """Aggregate of the 6 gates. ``promote=True`` means *all* gates
    passed and the lifecycle worker may transition
    ``candidate → staged``. The per-gate breakdown is preserved so the
    Inbox UI can surface ``"blocked by: freshness"`` etc.
    """

    promote: bool
    gates: tuple[GateOutcome, ...]

    def fail_reasons(self) -> list[str]:
        return [f"{g.name}: {g.reason}" for g in self.gates if not g.passed]


# Default knobs — mirrored from ``org_settings.skills_factory.forge.*``.
# These are the FALLBACK constants for direct unit tests. The lifecycle
# worker resolves per-tenant overrides and passes them in.
_DEFAULT_MIN_CLUSTER_SIZE = 3
_DEFAULT_MIN_DISTINCT_AGENTS = 3
_DEFAULT_FRESHNESS_WINDOW_DAYS = 14


# Signature: (tenant_id, fleet_id, fingerprint) → bool (True = poisoned).
# The lifecycle worker injects a real implementation that hits the
# ``forge_rejected_fingerprints`` table; unit tests inject a fake.
PoisonChecker = Any  # Callable[[str, str | None, str], Awaitable[bool]]
# Signature: (tenant_id, collection, doc_id) → live_data dict | None.
# Used to resolve the live ``content_hash`` for hash-binding gate (G6).
LiveDataFetcher = Any  # Callable[[str, str, str], Awaitable[dict | None]]


async def evaluate_auto_gates(
    doc: dict,
    *,
    tenant_id: str,
    fleet_id: str | None,
    now: datetime,
    poison_checker: PoisonChecker | None = None,
    live_data_fetcher: LiveDataFetcher | None = None,
    min_cluster_size: int = _DEFAULT_MIN_CLUSTER_SIZE,
    min_distinct_agents: int = _DEFAULT_MIN_DISTINCT_AGENTS,
    freshness_window_days: int = _DEFAULT_FRESHNESS_WINDOW_DAYS,
) -> AutoGateResult:
    """Evaluate the 6 auto-promotion gates against a candidate doc.

    The candidate must have been produced by Forge (so it already
    carries ``origin``, ``fingerprint``, ``evidence``, ``scan``). We
    RE-CHECK the volume + diversity + poison gates because between the
    Forge write and the lifecycle worker run:
      - new rejects may have landed in the poison table,
      - the cluster window may have aged out (freshness),
      - the doc may have been edited via Inbox (G5 re-fires off
        ``data.scan.state``).

    Gates (plan §15 Phase 2 acceptance):

      G1 ``volume``         — ``origin.cluster_size >= min_cluster_size``
      G2 ``diversity``      — ``origin.distinct_agents >= min_distinct_agents``
      G3 ``freshness``      — cluster window end is within
                              ``freshness_window_days`` of ``now``
      G4 ``poison``         — ``poison_checker(fingerprint)`` returns False
      G5 ``scan``           — ``data.scan.state == 'clean'``  (no quarantine)
      G6 ``hash_binding``   — for ``kind='update'`` docs, the
                              ``target.target_content_hash`` still
                              matches the live skill's ``content_hash``

    Inputs that the gate is uncomfortable evaluating (missing
    ``origin`` block, missing ``fingerprint``, …) cause that gate to
    FAIL CLOSED — we'd rather hold a candidate in the inbox than
    auto-promote one we can't verify.
    """
    gates: list[GateOutcome] = []
    origin = doc.get("origin") or {}

    # G1 — volume
    cluster_size = origin.get("cluster_size")
    if not isinstance(cluster_size, int):
        gates.append(GateOutcome("volume", False, "missing origin.cluster_size"))
    elif cluster_size < min_cluster_size:
        gates.append(
            GateOutcome(
                "volume",
                False,
                f"cluster_size={cluster_size} < min_cluster_size={min_cluster_size}",
            )
        )
    else:
        gates.append(GateOutcome("volume", True))

    # G2 — diversity
    distinct_agents = origin.get("distinct_agents")
    if not isinstance(distinct_agents, int):
        gates.append(GateOutcome("diversity", False, "missing origin.distinct_agents"))
    elif distinct_agents < min_distinct_agents:
        gates.append(
            GateOutcome(
                "diversity",
                False,
                f"distinct_agents={distinct_agents} < min_distinct_agents={min_distinct_agents}",
            )
        )
    else:
        gates.append(GateOutcome("diversity", True))

    # G3 — freshness (cluster window end within N days of now)
    window_end_raw = origin.get("window_end")
    window_end_dt: datetime | None = None
    if isinstance(window_end_raw, str):
        try:
            window_end_dt = datetime.fromisoformat(window_end_raw.replace("Z", "+00:00"))
        except ValueError:
            window_end_dt = None
    elif isinstance(window_end_raw, datetime):
        window_end_dt = window_end_raw
    if window_end_dt is None:
        gates.append(GateOutcome("freshness", False, "missing/unparseable origin.window_end"))
    else:
        # Normalize to aware UTC so the subtraction is safe.
        if window_end_dt.tzinfo is None:
            window_end_dt = window_end_dt.replace(tzinfo=UTC)
        # Same naive-datetime guard on ``now`` — callers occasionally
        # pass ``datetime.utcnow()`` (returns naive), which would
        # TypeError on the subtraction below since ``window_end_dt``
        # is now always aware.
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        age_days = (now - window_end_dt).total_seconds() / 86400.0
        if age_days > freshness_window_days:
            gates.append(
                GateOutcome(
                    "freshness",
                    False,
                    f"window_end is {age_days:.1f}d old (> {freshness_window_days}d)",
                )
            )
        else:
            gates.append(GateOutcome("freshness", True))

    # G4 — poison check
    fingerprint = doc.get("cluster_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        gates.append(GateOutcome("poison", False, "missing fingerprint"))
    elif poison_checker is None:
        # No checker injected — fail closed. The worker is expected to
        # always inject one; direct unit tests pass an explicit fake.
        gates.append(GateOutcome("poison", False, "no poison_checker available"))
    else:
        try:
            poisoned = await poison_checker(tenant_id, fleet_id, fingerprint)
        except Exception as e:
            gates.append(GateOutcome("poison", False, f"poison_checker raised: {type(e).__name__}"))
        else:
            if poisoned:
                gates.append(GateOutcome("poison", False, f"fingerprint {fingerprint} is poisoned"))
            else:
                gates.append(GateOutcome("poison", True))

    # G5 — scan
    scan = doc.get("scan") or {}
    scan_state = scan.get("state")
    if scan_state == "clean":
        gates.append(GateOutcome("scan", True))
    else:
        gates.append(GateOutcome("scan", False, f"scan.state={scan_state!r} (expected 'clean')"))

    # G6 — hash binding (only relevant for kind='update' docs)
    kind = doc.get("kind", "create")
    if kind != "update":
        gates.append(GateOutcome("hash_binding", True, "n/a (kind=create)"))
    else:
        target = doc.get("target") or {}
        target_hash = target.get("target_content_hash") if isinstance(target, dict) else None
        slug = doc.get("slug")
        if not isinstance(target_hash, str) or not isinstance(slug, str):
            gates.append(
                GateOutcome("hash_binding", False, "kind=update missing target.target_content_hash or slug")
            )
        elif live_data_fetcher is None:
            gates.append(GateOutcome("hash_binding", False, "no live_data_fetcher available"))
        else:
            try:
                live = await live_data_fetcher(tenant_id, "skills", slug)
            except Exception as e:
                gates.append(
                    GateOutcome("hash_binding", False, f"live_data_fetcher raised: {type(e).__name__}")
                )
            else:
                live_hash = (live or {}).get("content_hash") if isinstance(live, dict) else None
                if live_hash == target_hash:
                    gates.append(GateOutcome("hash_binding", True))
                else:
                    gates.append(
                        GateOutcome(
                            "hash_binding",
                            False,
                            f"target_hash={target_hash!r} != live_hash={live_hash!r}",
                        )
                    )

    promote = all(g.passed for g in gates)
    return AutoGateResult(promote=promote, gates=tuple(gates))
