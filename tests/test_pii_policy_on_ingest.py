"""The tenant's PII policy applies to ingest before the extraction model runs.

``POST /ingest/preview`` (pasted text or a URL) and ``POST /ingest/file`` send
the document's sections to the tenant's LLM and return the facts it extracts.
The policy now applies to the whole text first, as it does to a memory write:
``drop`` refuses the ingest before any section is sent, ``mask`` sends and
extracts from the redacted text, and ``flag`` sends it as received. Each action
leaves one ``pii_*`` audit row with ``write_mode="ingest"``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import core_api.services.organization_settings as ts_svc
from core_api.services import ingest_service
from tests.test_pii_policy_on_documents_and_keystones import (
    _CARD,
    _EMAIL,
    _pii_audit,
    _tenant_with_pii_action,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_RUNBOOK = (
    "# Billing runbook\n\n"
    f"Escalate refunds over the limit to {_EMAIL} before noon.\n\n"
    f"The refund test card is {_CARD}; it is never charged.\n"
)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """The settings cache is per-process: clear it so the policy a test seeds is
    the one the route reads."""
    ts_svc._settings_cache.clear()
    yield
    ts_svc._settings_cache.clear()


@pytest.fixture
def extractor(monkeypatch) -> list[str]:
    """Stands in for the extraction model: records every section it is sent and
    answers with that section as the one fact."""
    sent: list[str] = []

    async def _extract(text, focus=None, tenant_config=None, breadcrumb=None):
        sent.append(text)
        return [{"content": text, "suggested_type": "fact"}]

    monkeypatch.setattr(ingest_service, "_chunk_content", _extract)
    return sent


async def _preview(client, headers, tenant_id, **body):
    return await client.post(
        "/api/v1/ingest/preview",
        json={"tenant_id": tenant_id, **body},
        headers=headers,
    )


def _assert_redacted(sent: list[str], facts: list[dict]) -> None:
    assert sent, "nothing was sent for extraction"
    text = "\n".join(sent)
    assert _EMAIL not in text and _CARD not in text
    assert "«EMAIL»" in text and "«CARD»" in text
    returned = "\n".join(f["content"] for f in facts)
    assert _EMAIL not in returned and _CARD not in returned


async def test_drop_refuses_an_ingest_before_extraction(client, extractor):
    tenant_id, headers = await _tenant_with_pii_action("drop")

    resp = await _preview(client, headers, tenant_id, content=_RUNBOOK)

    assert resp.status_code == 422, resp.text
    assert "content policy" in resp.text
    assert extractor == []
    rows = await _pii_audit(tenant_id, "ingest")
    assert [r["action"] for r in rows] == ["pii_drop"]


async def test_mask_extracts_from_the_redacted_text(client, extractor):
    tenant_id, headers = await _tenant_with_pii_action("mask")

    resp = await _preview(client, headers, tenant_id, content=_RUNBOOK)

    assert resp.status_code == 200, resp.text
    _assert_redacted(extractor, resp.json()["facts"])
    rows = await _pii_audit(tenant_id, "ingest")
    assert [r["action"] for r in rows] == ["pii_mask"]
    assert _EMAIL not in str(rows[0]["detail"])


async def test_flag_extracts_the_text_as_received_and_audits_it(client, extractor):
    tenant_id, headers = await _tenant_with_pii_action("flag")

    resp = await _preview(client, headers, tenant_id, content=_RUNBOOK)

    assert resp.status_code == 200, resp.text
    assert _EMAIL in "\n".join(extractor)
    rows = await _pii_audit(tenant_id, "ingest")
    assert [r["action"] for r in rows] == ["pii_flag"]


async def test_url_ingest_is_masked_before_extraction(client, extractor, monkeypatch):
    monkeypatch.setattr(
        ingest_service, "_fetch_url_text", AsyncMock(return_value=_RUNBOOK)
    )
    tenant_id, headers = await _tenant_with_pii_action("mask")

    resp = await _preview(
        client, headers, tenant_id, url="https://example.com/billing-runbook"
    )

    assert resp.status_code == 200, resp.text
    _assert_redacted(extractor, resp.json()["facts"])
    rows = await _pii_audit(tenant_id, "ingest")
    assert [r["action"] for r in rows] == ["pii_mask"]


async def test_file_ingest_is_masked_before_extraction(client, extractor):
    tenant_id, headers = await _tenant_with_pii_action("mask")

    resp = await client.post(
        "/api/v1/ingest/file",
        files={"file": ("runbook.md", _RUNBOOK.encode(), "text/markdown")},
        data={"tenant_id": tenant_id},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    _assert_redacted(extractor, resp.json()["facts"])
    rows = await _pii_audit(tenant_id, "ingest")
    assert [r["action"] for r in rows] == ["pii_mask"]
