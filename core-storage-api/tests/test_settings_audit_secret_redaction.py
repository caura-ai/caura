"""Historical settings audit rows stop holding provider keys (M-99).

``diff_settings`` masks a secret path on both sides of every new audit row, but
rows written before it record ``api_keys.*`` and credential-named leaves as the
``[old, new]`` values that were submitted, plaintext keys included, and nothing
prunes ``organization_settings_audit``. Migration 056 rewrites those rows with
the same mask: at a secret path, every value but ``null`` and ``""`` becomes
``****``. The change record stays (who, when, which key); the key does not.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import select, text

from common.models.organization_settings import OrganizationSettingsAudit
from common.organization_settings_merge import diff_settings
from core_storage_api.services.postgres_service import get_session

pytestmark = pytest.mark.asyncio

_VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "src/core_storage_api/database/migrations/versions"


def _migration_056():
    """By path: the versions directory is not a package and the name starts with a digit."""
    path = _VERSIONS / "056_redact_settings_audit_secrets.py"
    spec = importlib.util.spec_from_file_location("migration_056", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _audit_row(diff: dict) -> int:
    org = f"test-tenant-{uuid.uuid4().hex[:8]}"
    async with get_session() as s:
        row = OrganizationSettingsAudit(org_id=org, changed_by="before-m99", diff=diff)
        s.add(row)
        await s.flush()
        return row.id


async def _diff(row_id: int) -> dict:
    async with get_session() as s:
        stmt = select(OrganizationSettingsAudit.diff).where(OrganizationSettingsAudit.id == row_id)
        return (await s.execute(stmt)).scalar_one()


async def _redact() -> None:
    async with get_session() as s:
        await s.execute(text(_migration_056().REDACT_SQL))


async def test_plaintext_keys_in_old_audit_rows_are_masked() -> None:
    row = await _audit_row(
        {
            "api_keys.openai_api_key": ["sk-old-plaintext", "sk-new-plaintext"],
            "api_keys.gemini_api_key": [None, "AIza-plaintext"],
            "api_keys.anthropic_api_key": ["sk-ant-plaintext", ""],
            "telemetry.deployment_token": ["tok-plaintext", "tok-2"],
            "enrichment.provider": ["openai", "anthropic"],
        }
    )

    await _redact()

    assert await _diff(row) == {
        "api_keys.openai_api_key": ["****", "****"],
        "api_keys.gemini_api_key": [None, "****"],
        "api_keys.anthropic_api_key": ["****", ""],
        "telemetry.deployment_token": ["****", "****"],
        "enrichment.provider": ["openai", "anthropic"],
    }


async def test_the_rewrite_masks_exactly_what_diff_settings_masks_today() -> None:
    """Old rows and new rows must agree, so the migration's copy of the rule is
    held to the live one: an old row's raw diff, once rewritten, equals the diff
    ``diff_settings`` would write for the same change now."""
    old = {
        "api_keys": {"openai_api_key": "sk-a", "openrouter_api_key": "", "gemini_api_key": {"key": "sk-c"}},
        "ops": {"admin_password": "p", "Webhook_SECRET": "s", "GitHub_TOKEN": "t", "limit": 1},
    }
    new = {
        "api_keys": {"openai_api_key": "sk-b", "openrouter_api_key": "or-b", "gemini_api_key": ["sk-d"]},
        "ops": {"admin_password": "q", "Webhook_SECRET": "", "GitHub_TOKEN": "u", "limit": 2},
    }
    raw = {
        "api_keys.openai_api_key": ["sk-a", "sk-b"],
        "api_keys.openrouter_api_key": ["", "or-b"],
        "api_keys.gemini_api_key": [{"key": "sk-c"}, ["sk-d"]],
        "ops.admin_password": ["p", "q"],
        "ops.Webhook_SECRET": ["s", ""],
        "ops.GitHub_TOKEN": ["t", "u"],
        "ops.limit": [1, 2],
    }
    row = await _audit_row(raw)

    await _redact()

    assert await _diff(row) == diff_settings(old, new)


async def test_a_key_that_is_not_a_string_is_masked_too() -> None:
    """``api_keys`` takes any value under it, so an old row can hold a key inside a
    list or an object, or as a number. Only ``null`` and ``""`` stay as they are."""
    row = await _audit_row(
        {
            "api_keys.openai_api_key": [{"key": "sk-in-an-object"}, ["sk-in-a-list"]],
            "api_keys.gemini_api_key": ["", 12345],
            "ops.limit": [[1], {"n": 2}],
        }
    )

    await _redact()

    assert await _diff(row) == {
        "api_keys.openai_api_key": ["****", "****"],
        "api_keys.gemini_api_key": ["", "****"],
        "ops.limit": [[1], {"n": 2}],
    }


async def test_new_audit_rows_mask_a_key_that_is_not_a_string() -> None:
    """New rows follow the same rule: ``diff_settings`` masks a key stored as an
    object or submitted as a list, not only a string."""
    diff = diff_settings(
        {"api_keys": {"openai_api_key": {"key": "sk-in-an-object"}, "gemini_api_key": ""}},
        {"api_keys": {"openai_api_key": "sk-new", "gemini_api_key": ["sk-in-a-list"]}},
    )

    assert diff == {
        "api_keys.openai_api_key": ["****", "****"],
        "api_keys.gemini_api_key": ["", "****"],
    }


async def test_the_rewrite_is_idempotent_and_leaves_other_rows_alone() -> None:
    masked = await _audit_row({"api_keys.openai_api_key": ["****", "****"]})
    plain = await _audit_row({"search.top_k": [5, 10], "governance.mode": [None, "strict"]})
    secret = await _audit_row({"api_keys.openai_api_key": ["sk-x", "sk-y"]})

    await _redact()
    await _redact()

    assert await _diff(masked) == {"api_keys.openai_api_key": ["****", "****"]}
    assert await _diff(plain) == {"search.top_k": [5, 10], "governance.mode": [None, "strict"]}
    assert await _diff(secret) == {"api_keys.openai_api_key": ["****", "****"]}
