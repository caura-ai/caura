"""Pure merge/diff helpers for organization-settings overrides.

Shared by core-api (the settings service + display) and core-storage-api
(the transactional ``POST /organization-settings`` upsert, which must compute
the diff against the FOR-UPDATE'd row server-side so the read and write stay
in one transaction). Kept dependency-free so both services can import it
without pulling in a service's config.
"""

from __future__ import annotations

from typing import Any


def deep_merge(old: Any, new: Any) -> Any:
    """Return ``old`` with ``new`` merged recursively for nested dicts.

    Non-dict values in ``new`` overwrite ``old`` wholesale (including lists).
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new
    out = dict(old)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def diff_settings(old: dict, new: dict, prefix: str = "") -> dict:
    """Flat diff: ``{"enrichment.provider": [old, new], ...}``.

    Recurses into nested dicts; treats non-dict values as leaves. Only records
    keys present in ``new`` whose value differs from ``old``; does not record
    deletions (updates are additive).
    """
    out: dict = {}
    for k, new_v in new.items():
        path = f"{prefix}{k}"
        old_v = old.get(k) if isinstance(old, dict) else None
        if isinstance(new_v, dict):
            # Recurse even when the old side is absent, so we always emit flat
            # leaf keys (e.g. "security_audit.schedule_enabled") rather than a
            # whole-dict diff ["enrichment": [None, {...}]].
            old_dict = old_v if isinstance(old_v, dict) else {}
            out.update(diff_settings(old_dict, new_v, prefix=f"{path}."))
        elif new_v != old_v:
            out[path] = [old_v, new_v]
    return out
