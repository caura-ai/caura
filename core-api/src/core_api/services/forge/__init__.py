"""Skill Factory — Forge resident package.

Lake-side skill production pipeline. Phase 0 ships only the
:mod:`~core_api.services.forge.sentinel_scan` stub (so the SF-002
``caura_doc`` skills-write adjustments can wire the call site
end-to-end). Phase 1 lands :mod:`forge_service`,
:mod:`fingerprint`, and :mod:`distill_prompt`; Phase 2 fills in the
real :mod:`sentinel_scan` checks; Phase 3 adds
:mod:`harness_install`; Phase 5 adds :mod:`openclaw_bridge`.

See ``skill-factory-implementation-plan.md`` — archived from HEAD with the
rest of ``docs/live-memory-pitch/`` (W2 of the sunset programme); recover it
with ``git show 691659a:docs/live-memory-pitch/skill-factory-implementation-plan.md``.
"""
