"""Transient receipt state on authorized reads; never rewrite stored metadata.

A match confirms a stored broker assertion, not rules fetch reconciliation or
model obedience. Legacy or incomplete origins cannot supply a positive claim.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, TypeVar, cast
from uuid import UUID

from fastapi import HTTPException

from core_api.clients.session_receipts import MAX_CHECKS, SessionReceiptMatchClient, validate_results
from core_api.config import settings
from core_api.errors import AUTH_TENANT_MISMATCH, coded_detail
from core_api.services.system_metadata import extract_system_metadata

_STATE = "rules_receipt_validation"
_MAX_REQUESTS = 10
_HASH = re.compile(r"[0-9a-f]{64}")
_client: SessionReceiptMatchClient | None = None
T = TypeVar("T")


def get_session_receipt_client() -> SessionReceiptMatchClient:
    global _client
    if _client is None:
        _client = SessionReceiptMatchClient(
            base_url=settings.session_receipt_match_url,
            audience=settings.session_receipt_match_audience,
        )
    return _client


def _get(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _metadata(row: Any) -> dict:
    raw = _get(row, "metadata_")
    if raw is None:
        raw = _get(row, "metadata")
    return raw if isinstance(raw, dict) else {}


def _check(row: Any, system: dict) -> dict | None:
    receipt = system.get("rules_receipt")
    origin = system.get("rules_receipt_origin")
    if not isinstance(receipt, dict) or not isinstance(origin, dict):
        return None
    session = origin.get("session_id")
    digest = receipt.get("rule_set_hash")
    if (
        type(origin.get("version")) is not int
        or origin["version"] != 1
        or origin.get("tenant_id") != _get(row, "tenant_id")
        or not isinstance(session, str)
        or not session.strip()
        or len(session) > 4096
        or not isinstance(digest, str)
        or not _HASH.fullmatch(digest)
    ):
        return None
    try:
        bindings = {
            "event_id": receipt.get("event_id"),
            "install_id": origin.get("install_id"),
            "agent_id": origin.get("agent_id"),
        }
        if not all(isinstance(value, str) for value in bindings.values()):
            return None
        return {
            **{key: str(UUID(value)) for key, value in bindings.items()},
            "session_id": session,
            "rule_set_hash": digest,
        }
    except (ValueError, TypeError, AttributeError):
        return None


async def annotate_memory_receipts(
    memories: Sequence[T],
    *,
    authorized_tenant_ids: set[str],
    client: SessionReceiptMatchClient | None = None,
    timeout_seconds: float = 0.5,
) -> list[T]:
    # Validate the complete set before any external call. Row scope/visibility
    # authorization belongs to the read boundary that supplies this tenant set.
    if any(_get(row, "tenant_id") not in authorized_tenant_ids for row in memories):
        raise HTTPException(
            status_code=403,
            detail=coded_detail(AUTH_TENANT_MISMATCH, "Memory tenant is not readable"),
        )
    copies: list[T] = []
    states: list[dict] = []
    grouped: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for index, row in enumerate(memories):
        metadata = _metadata(row)
        nested = metadata.get("_system")
        nested = nested if isinstance(nested, dict) else {}
        previous = _get(row, "system_metadata")
        system = dict(previous if isinstance(previous, dict) else extract_system_metadata(metadata) or {})
        system.pop(_STATE, None)
        check = _check(row, nested)
        state = {"status": "evidence_unavailable" if check else "unverifiable"}
        states.append(state)
        if check is not None:
            grouped[_get(row, "tenant_id")].append((index, check))
        if "rules_receipt" in nested or "rules_receipt" in metadata:
            system[_STATE] = state
        if isinstance(row, dict):
            copy = {**row, "system_metadata": system or None}
        else:
            copy = row.model_copy(update={"system_metadata": system or None})  # type: ignore[attr-defined]
        copies.append(cast("T", copy))
    if not grouped:
        return copies
    lookup = client if client is not None else get_session_receipt_client()
    requests = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            for tenant, entries in grouped.items():
                for start in range(0, len(entries), MAX_CHECKS):
                    if requests >= _MAX_REQUESTS:
                        return copies
                    batch = entries[start : start + MAX_CHECKS]
                    checks = [check for _, check in batch]
                    requests += 1
                    try:
                        results = validate_results({"results": await lookup.match(tenant, checks)}, checks)
                    except Exception:
                        continue
                    for (index, _), result in zip(batch, results, strict=True):
                        states[index].update(
                            {key: value for key, value in result.items() if key != "event_id"}
                        )
    except TimeoutError:
        pass
    return copies
