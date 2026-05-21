"""F3 deployment_mode — the only per-deploy control after Phase 3.

Originally added in Phase 1 alongside the legacy ``embed_on_hot_path``
+ ``enrich_on_hot_path`` flags with a derivation validator that
populated ``deployment_mode`` from the legacy pair. Phase 3 deleted
the legacy flags + the validator; this file's tests were trimmed to
match. What stays pinned:

- ``deployment_mode`` is a ``Literal["inline","deferred"]`` field on
  ``Settings``.
- Default is ``"inline"`` (OSS-friendly — no worker fleet required).
- Garbage values are rejected at construction (typos in the deploy
  env become startup errors, not silent fallbacks).
- ``inline_embedding`` and ``inline_enrichment`` properties read
  directly from ``deployment_mode``.

The 18-call-site migration onto the helpers is exercised by
``test_write_mode_dispatch.py``, ``test_embed_off_hot_path.py``,
``test_enrich_off_hot_path.py``, ``test_fast_branch_fan_out.py``.
"""

from __future__ import annotations

import pytest


def _make_settings(**overrides):
    """Construct a fresh Settings() with the given fields overridden."""
    from core_api.config import Settings

    return Settings(**overrides)


def test_deployment_mode_field_exists() -> None:
    s = _make_settings()
    assert hasattr(s, "deployment_mode")


def test_deployment_mode_default_is_inline() -> None:
    """OSS-friendly default. Phase 3 changed this from ``None`` (derive
    from legacy flags) to a concrete ``"inline"`` literal once the
    legacy flags were removed. SaaS deploys set ``DEPLOYMENT_MODE=
    deferred`` explicitly via the deploy YAML."""
    s = _make_settings()
    assert s.deployment_mode == "inline"


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
# Helper properties — single source of truth for callers
# ---------------------------------------------------------------------------


def test_inline_embedding_true_when_inline() -> None:
    s = _make_settings(deployment_mode="inline")
    assert s.inline_embedding is True


def test_inline_embedding_false_when_deferred() -> None:
    s = _make_settings(deployment_mode="deferred")
    assert s.inline_embedding is False


def test_inline_enrichment_true_when_inline() -> None:
    s = _make_settings(deployment_mode="inline")
    assert s.inline_enrichment is True


def test_inline_enrichment_false_when_deferred() -> None:
    s = _make_settings(deployment_mode="deferred")
    assert s.inline_enrichment is False
