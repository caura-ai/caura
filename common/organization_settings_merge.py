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


def merge_settings_update(old: Any, new: Any) -> Any:
    """Apply a settings UPDATE payload onto stored overrides.

    Same as ``deep_merge`` except that an explicit ``None`` in ``new`` DELETES
    the key instead of storing ``null``. ``null`` is the documented reset shape
    ("return this setting to its default"), and removing the override is what
    that means. Storing ``null`` instead breaks it in two places: a key whose
    default is concrete (``skills_factory.body_max_bytes`` = 40000) reads back
    as ``None`` once ``DEFAULT_SETTINGS`` is merged over it, and a
    ``search.default_profile`` knob set back to ``null`` sat in the stored
    profile as ``{"top_k": null}`` rather than being removed (SIDE-61).

    Only the WRITE path uses this. ``deep_merge`` keeps storing ``None`` as a
    value, because the display merge (``DEFAULT_SETTINGS`` with the overrides
    merged over it) must not lose a schema key when a row still holds a
    legacy ``null``.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new
    out = dict(old)
    for k, v in new.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_settings_update(out[k], v)
        elif isinstance(v, dict):
            # A new subtree: run it through the same rule so a null nested
            # inside it is not stored either.
            out[k] = merge_settings_update({}, v)
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
            if _is_secret_path(path):
                # The audit row says that a secret changed, never what it was
                # or became (plaintext or ciphertext).
                out[path] = [_mask(old_v), _mask(new_v)]
            else:
                out[path] = [old_v, new_v]
    return out


_SECRET_SECTION_PREFIXES = ("api_keys.",)
_SECRET_LEAF_SUFFIXES = ("_token", "_secret", "_api_key", "_password")


def _is_secret_path(path: str) -> bool:
    """Provider keys, and any leaf named as a credential (e.g. the telemetry
    ``deployment_token`` kept in the ``__deployment__`` settings row)."""
    return path.startswith(_SECRET_SECTION_PREFIXES) or path.rsplit(".", 1)[
        -1
    ].lower().endswith(_SECRET_LEAF_SUFFIXES)


def _mask(value: object) -> object:
    """Every value but ``None`` and ``""``: ``api_keys`` takes any value under it,
    so a key can arrive in a list or an object and is still a key."""
    return value if value is None or value == "" else "****"
