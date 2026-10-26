"""Size budgets for caller-supplied JSON and memory text fields."""

import json
from typing import Any


class RequestFieldSizeError(ValueError):
    """A write would exceed a field's serialized size budget."""


MAX_JSON_FIELD_BYTES = 65536
TEXT_FIELD_CAPS = {
    "title": 500,
    "source_uri": 2048,
    "run_id": 200,
    "predicate": 200,
    "object_value": 10000,
}


def bounded_json(value: Any) -> Any:
    if (
        value is not None
        and len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        > MAX_JSON_FIELD_BYTES
    ):
        raise RequestFieldSizeError(f"JSON field must be <= {MAX_JSON_FIELD_BYTES} UTF-8 bytes")
    return value


def validate_write_fields(body: dict) -> None:
    for key in ("metadata", "metadata_", "metadata_patch", "attributes", "data"):
        if key in body:
            bounded_json(body[key])
    for key, cap in TEXT_FIELD_CAPS.items():
        value = body.get(key)
        if isinstance(value, str) and len(value) > cap:
            raise RequestFieldSizeError(f"{key} must be <= {cap} characters")


def bound_metadata_merge(stored: dict | None, patch: dict) -> None:
    """Cap caller metadata growth while allowing legacy repair and maintenance.

    The platform's deep-merged ``_system`` namespace is not caller metadata.
    Incoming patches still have their own size guard at the storage edge.
    """
    caller_stored = {k: v for k, v in (stored or {}).items() if k != "_system"}
    caller_merged = {**caller_stored, **{k: v for k, v in patch.items() if k != "_system"}}

    def size(value: dict) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    if size(caller_merged) > max(MAX_JSON_FIELD_BYTES, size(caller_stored)):
        raise RequestFieldSizeError(f"JSON field must be <= {MAX_JSON_FIELD_BYTES} UTF-8 bytes")
