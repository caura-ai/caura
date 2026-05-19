"""F3 Phase 1 — ``deployment_mode`` setting with legacy-flag derivation.

Phase 1 adds the new ``deployment_mode: Literal["inline","deferred"]``
setting alongside the existing ``embed_on_hot_path`` and
``enrich_on_hot_path`` flags. No call site moves yet — every reader
of the legacy flags stays as-is. The new setting + two helpers are
the source of truth Phases 2+3 migrate onto.

Derivation rules (when ``DEPLOYMENT_MODE`` is unset)
────────────────────────────────────────────────────
- ``(embed=True,  enrich=True)``   → ``"inline"``   (OSS local default)
- ``(embed=False, enrich=False)``  → ``"deferred"`` (SaaS prod default)
- ``(embed=False, enrich=True)``   → log a WARNING + choose ``"deferred"``
  (conservative — never silently expose un-enriched rows to OSS users)
- ``(embed=True,  enrich=False)``  → log a WARNING + choose ``"deferred"``

When ``DEPLOYMENT_MODE`` IS set explicitly (``"inline"`` | ``"deferred"``):
- the explicit value wins; legacy flags are ignored for derivation
  purposes. The legacy flags themselves stay readable until Phase 3.

Helpers
───────
- ``settings.inline_embedding``  → True iff effective mode is ``"inline"``
- ``settings.inline_enrichment`` → True iff effective mode is ``"inline"``

Tests pinned BEFORE the implementation. They FAIL against current
main (no ``deployment_mode`` field, no helpers). Implementation makes
them pass without breaking any of the ~28 existing flag-touching
tests.
"""

from __future__ import annotations

import logging

import pytest


def _make_settings(**overrides):
    """Construct a fresh Settings() with the given fields overridden.

    Pydantic-Settings reads env at construction. To avoid pollution
    from the surrounding process env, we pass the overrides explicitly
    and assert behavior on the resulting instance.
    """
    from core_api.config import Settings

    return Settings(**overrides)


# ---------------------------------------------------------------------------
# deployment_mode field exists and accepts the two literal values
# ---------------------------------------------------------------------------


def test_deployment_mode_field_exists_with_default_none() -> None:
    """The setting must exist on Settings. Default is ``None`` so the
    derivation validator runs and pulls the value from legacy flags."""
    s = _make_settings(embed_on_hot_path=True, enrich_on_hot_path=True)
    assert hasattr(s, "deployment_mode")


def test_deployment_mode_accepts_inline() -> None:
    s = _make_settings(deployment_mode="inline")
    assert s.deployment_mode == "inline"


def test_deployment_mode_accepts_deferred() -> None:
    s = _make_settings(deployment_mode="deferred")
    assert s.deployment_mode == "deferred"


def test_deployment_mode_rejects_garbage() -> None:
    """Pydantic must reject unknown literals at construction so a typo
    in an env file becomes a startup error, not silent fallback."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_settings(deployment_mode="async")


# ---------------------------------------------------------------------------
# Derivation when deployment_mode is None
# ---------------------------------------------------------------------------


def test_derives_inline_from_both_flags_true() -> None:
    """(T, T) — OSS local canonical."""
    s = _make_settings(embed_on_hot_path=True, enrich_on_hot_path=True)
    assert s.deployment_mode == "inline"


def test_derives_deferred_from_both_flags_false() -> None:
    """(F, F) — SaaS prod canonical."""
    s = _make_settings(embed_on_hot_path=False, enrich_on_hot_path=False)
    assert s.deployment_mode == "deferred"


def test_derives_deferred_from_asymmetric_embed_off_enrich_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(F, T) — asymmetric. Per Phase 0 finding + user confirmation, no
    real environment uses this. Conservative default: ``deferred`` (worse
    to silently expose un-enriched rows as 'OSS inline' than to under-
    deliver and surface the misconfig in the deprecation warning)."""
    caplog.set_level(logging.WARNING)
    s = _make_settings(embed_on_hot_path=False, enrich_on_hot_path=True)
    assert s.deployment_mode == "deferred"

    msgs = " ".join(rec.message for rec in caplog.records)
    assert "deployment_mode" in msgs.lower() or "asymmetric" in msgs.lower(), (
        f"Asymmetric flag pair must emit a deprecation/derivation warning. "
        f"Caplog: {[r.message for r in caplog.records]!r}"
    )


def test_derives_deferred_from_asymmetric_embed_on_enrich_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(T, F) — also asymmetric, same conservative default + warning."""
    caplog.set_level(logging.WARNING)
    s = _make_settings(embed_on_hot_path=True, enrich_on_hot_path=False)
    assert s.deployment_mode == "deferred"

    msgs = " ".join(rec.message for rec in caplog.records)
    assert "deployment_mode" in msgs.lower() or "asymmetric" in msgs.lower()


def test_explicit_deployment_mode_inline_wins_over_legacy_flags() -> None:
    """Explicit setting beats legacy flags. A deploy setting
    ``DEPLOYMENT_MODE=inline`` with stale ``EMBED_ON_HOT_PATH=false``
    overrides the derivation. This is how Phase 3 migrates ops cleanly."""
    s = _make_settings(
        deployment_mode="inline",
        embed_on_hot_path=False,
        enrich_on_hot_path=False,
    )
    assert s.deployment_mode == "inline"


def test_explicit_deployment_mode_deferred_wins_over_legacy_flags() -> None:
    s = _make_settings(
        deployment_mode="deferred",
        embed_on_hot_path=True,
        enrich_on_hot_path=True,
    )
    assert s.deployment_mode == "deferred"


# ---------------------------------------------------------------------------
# Helper properties — single source of truth for callers
# ---------------------------------------------------------------------------


def test_inline_embedding_true_when_inline() -> None:
    s = _make_settings(embed_on_hot_path=True, enrich_on_hot_path=True)
    assert s.deployment_mode == "inline"
    assert s.inline_embedding is True


def test_inline_embedding_false_when_deferred() -> None:
    s = _make_settings(embed_on_hot_path=False, enrich_on_hot_path=False)
    assert s.deployment_mode == "deferred"
    assert s.inline_embedding is False


def test_inline_enrichment_true_when_inline() -> None:
    s = _make_settings(embed_on_hot_path=True, enrich_on_hot_path=True)
    assert s.inline_enrichment is True


def test_inline_enrichment_false_when_deferred() -> None:
    s = _make_settings(embed_on_hot_path=False, enrich_on_hot_path=False)
    assert s.inline_enrichment is False


def test_helpers_follow_explicit_deployment_mode() -> None:
    """When deployment_mode is set explicitly, the helpers reflect IT,
    not the legacy flags. This is the contract Phase 2 migrates callers
    onto."""
    s = _make_settings(
        deployment_mode="inline",
        embed_on_hot_path=False,
        enrich_on_hot_path=False,
    )
    assert s.inline_embedding is True
    assert s.inline_enrichment is True

    s = _make_settings(
        deployment_mode="deferred",
        embed_on_hot_path=True,
        enrich_on_hot_path=True,
    )
    assert s.inline_embedding is False
    assert s.inline_enrichment is False


# ---------------------------------------------------------------------------
# Legacy flags untouched — readers continue to work as before
# ---------------------------------------------------------------------------


def test_legacy_flags_remain_readable_after_phase1() -> None:
    """Phase 1 does NOT remove the legacy flags. Existing call sites
    (18 branches across 3 files per the Phase 0 inventory) all still
    read them. They are removed in Phase 3."""
    s = _make_settings(embed_on_hot_path=False, enrich_on_hot_path=False)
    assert s.embed_on_hot_path is False
    assert s.enrich_on_hot_path is False

    s = _make_settings(embed_on_hot_path=True, enrich_on_hot_path=True)
    assert s.embed_on_hot_path is True
    assert s.enrich_on_hot_path is True
