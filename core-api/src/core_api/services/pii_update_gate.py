"""Deterministic PII policy for writes outside the memory pipeline.

The create and bulk paths apply the tenant's ``governance.pii`` action before
anything is stored (``pipeline/steps/write/governance_scan_content.py`` and the
bulk inline gate). ``PATCH /memories/{id}`` and MCP ``op=update`` did not, so
content a create would have masked or refused was stored as sent on an edit,
with no governance audit row. Nor did the document and keystone writes, which
store a caller's text in their own tables.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException

from common.governance import Finding, PIICategory, mask, scan
from core_api.services import organization_settings
from core_api.services.governance_gate import (
    ACTION_PII_DROP,
    ACTION_PII_FLAG,
    ACTION_PII_MASK,
    emit_governance_audit,
    mark_pii_flagged,
    pii_audit_detail,
)

_WRITE_MODE = "update"


async def apply_pii_policy_to_update(
    *, tenant_id: str, agent_id: str | None, content: str, gov
) -> tuple[str, dict]:
    """Return ``(content_to_store, metadata_flags)`` for an edited memory.

    ``drop`` raises 422 (nothing is persisted); ``mask`` returns redacted
    content; ``flag`` returns the metadata keys to merge into the row. Every
    action is written to the governance audit, as on create.
    """
    if gov is None or not gov.enabled or not content:
        return content, {}
    findings = scan(content, enabled_categories=gov.enabled_categories)
    if not findings:
        return content, {}
    if gov.action == "drop":
        await emit_governance_audit(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action=ACTION_PII_DROP,
            detail=pii_audit_detail(ACTION_PII_DROP, findings, content, _WRITE_MODE),
            critical=True,
        )
        raise HTTPException(
            status_code=422, detail="Memory rejected by content policy: sensitive data detected"
        )
    if gov.action == "mask":
        await emit_governance_audit(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action=ACTION_PII_MASK,
            detail=pii_audit_detail(ACTION_PII_MASK, findings, content, _WRITE_MODE),
        )
        return mask(content, findings), {}
    flags: dict = {}
    mark_pii_flagged(flags, findings)
    await emit_governance_audit(
        tenant_id=tenant_id,
        agent_id=agent_id,
        action=ACTION_PII_FLAG,
        detail=pii_audit_detail(ACTION_PII_FLAG, findings, content, _WRITE_MODE),
    )
    return content, flags


async def apply_pii_policy_to_fields(
    *, tenant_id: str, agent_id: str | None, data: Any, write_mode: str, subject: str
) -> Any:
    """Return ``data`` as the tenant's PII policy lets it be stored.

    ``data`` is any JSON value: a document's ``data``, a keystone's title and
    text, or the text of a document being ingested. Every string in it is
    scanned; keys are field names, not content, and are not. ``drop`` raises
    422 (``subject`` names what was refused); ``mask`` returns a copy with each
    string masked where it sits; ``flag`` returns ``data`` as sent, since these
    stores have no metadata to carry the flag, and the audit row records it. One audit row per write, whose spans index the
    strings joined by newlines. A tenant config that cannot be read fails the
    write, as it fails a memory write.
    """
    config = await organization_settings.resolve_config(tenant_id)
    gov = getattr(config, "governance_pii", None)
    if gov is None or not gov.enabled:
        return data
    texts = list(_strings(data))
    # In a worker thread: ingest passes a whole document, up to 3 MB, and every
    # pattern runs over all of it.
    found = await asyncio.to_thread(_scan_all, texts, gov.enabled_categories)
    if not any(found):
        return data
    joined = "\n".join(texts)
    action = {"drop": ACTION_PII_DROP, "mask": ACTION_PII_MASK}.get(gov.action, ACTION_PII_FLAG)
    await emit_governance_audit(
        tenant_id=tenant_id,
        agent_id=agent_id,
        action=action,
        detail=pii_audit_detail(action, _joined_findings(texts, found), joined, write_mode),
        critical=action == ACTION_PII_DROP,
    )
    if action == ACTION_PII_DROP:
        raise HTTPException(
            status_code=422, detail=f"{subject} rejected by content policy: sensitive data detected"
        )
    if action == ACTION_PII_MASK:
        return _with_strings(data, iter(await asyncio.to_thread(_mask_all, texts, found)))
    return data


def _scan_all(texts: list[str], categories: frozenset[PIICategory] | None) -> list[list[Finding]]:
    return [scan(t, enabled_categories=categories) for t in texts]


def _mask_all(texts: list[str], found: list[list[Finding]]) -> list[str]:
    return [mask(t, f) if f else t for t, f in zip(texts, found, strict=True)]


def _strings(value: Any) -> Iterator[str]:
    """Every string in a JSON value, depth first, in order."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _with_strings(value: Any, strings: Iterator[str]) -> Any:
    """A copy of ``value`` with its strings replaced, in ``_strings`` order."""
    if isinstance(value, str):
        return next(strings)
    if isinstance(value, dict):
        return {key: _with_strings(item, strings) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_strings(item, strings) for item in value]
    return value


def _joined_findings(texts: list[str], found: list[list[Finding]]) -> list[Finding]:
    """Each string's findings, offset to where it sits in the newline join."""
    out: list[Finding] = []
    offset = 0
    for text, findings in zip(texts, found, strict=True):
        out += [dataclasses.replace(f, start=f.start + offset, end=f.end + offset) for f in findings]
        offset += len(text) + 1
    return out
