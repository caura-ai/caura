"""Preserve a broker receipt's original binding without trusting later readers.

Writes use the real REST and storage paths. Trusted storage seeding in lifecycle
tests isolates preservation from admission, so a missing admission hook cannot
hide replacement, release or duplicate-write regressions.
"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import text

from common.constants import QUARANTINED_MEMORY_STATUS
from core_api.services.system_metadata import sanitize_caller_metadata
from tests.test_rules_receipt import as_auth as as_auth

INSTALL = "e435b91d-202b-4da4-9e21-dee620b033f8"
OTHER_INSTALL = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
AGENT = "11111111-2222-4333-8444-555555555555"
OTHER_AGENT = "aaaaaaaa-2222-4333-8444-555555555555"
SESSION = "opaque-session/one"
ORIGIN_KEY = "rules_receipt_origin"
RECEIPT = {
    "event_id": "3b241101-e2bb-5255-8caf-4136c566a962",
    "rule_set_hash": "a" * 64,
}


def _origin(tenant, *, install=INSTALL, agent=AGENT, session=SESSION):
    return {
        "version": 1,
        "tenant_id": tenant,
        "install_id": install,
        "session_id": session,
        "agent_id": agent,
    }


def _metadata(**changes):
    return {
        "session_id": SESSION,
        "agent_id": AGENT,
        "rules_receipt": dict(RECEIPT),
        **changes,
    }


def _item(metadata=None, **changes):
    return {
        "content": f"The maintenance window moved to Thursday {uuid4().hex}",
        "metadata": _metadata() if metadata is None else metadata,
        **changes,
    }


def _broker(as_auth, tenant, install=INSTALL):
    as_auth(tenant, is_install_credential=True, install_uuid=install)


async def _bulk(client, tenant, *items, attempt=None, author=None):
    body = {"tenant_id": tenant, "items": list(items)}
    if author is not None:
        body["agent_id"] = author
    response = await client.post(
        "/api/v1/memories/bulk",
        json=deepcopy(body),
        headers={"X-Bulk-Attempt-Id": attempt or f"origin-{uuid4().hex}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["results"]


async def _created(client, tenant, item=None, **kwargs):
    [result] = await _bulk(
        client, tenant, item if item is not None else _item(), **kwargs
    )
    assert result["status"] == "created", result
    return result["id"]


async def _row(sc, tenant, memory_id):
    row = await sc.get_memory(memory_id, tenant, include_held=True, read=False)
    assert row is not None
    return row


def _system(row):
    return (row.get("metadata_") or {}).get("_system") or {}


async def _seed_origin(sc, tenant, memory_id):
    # Synthetic trusted storage fixture, not a public write or legacy backfill.
    row = await _row(sc, tenant, memory_id)
    metadata = deepcopy(row["metadata_"])
    metadata.setdefault("_system", {})[ORIGIN_KEY] = _origin(tenant)
    await sc.update_memory(memory_id, tenant, {"metadata_": metadata})
    assert _system(await _row(sc, tenant, memory_id))[ORIGIN_KEY] == _origin(tenant)


@pytest.mark.unit
def test_caller_metadata_cannot_supply_a_receipt_origin():
    forged = _origin("forged-tenant", install=OTHER_INSTALL)
    assert sanitize_caller_metadata(
        {ORIGIN_KEY: forged, "_system": {ORIGIN_KEY: forged}, "session_id": SESSION}
    ) == {"session_id": SESSION}


async def test_origin_uses_authenticated_install_and_ignores_supplied_origin(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id)
    forged = _origin("forged-tenant", install=OTHER_INSTALL, agent=OTHER_AGENT)
    memory_id = await _created(
        client,
        tenant_id,
        _item(_metadata(**{ORIGIN_KEY: forged, "_system": {ORIGIN_KEY: forged}})),
    )
    row = await _row(sc, tenant_id, memory_id)
    assert _system(row).get(ORIGIN_KEY) == _origin(tenant_id)
    assert _system(row)["rules_receipt"] == RECEIPT
    assert ORIGIN_KEY not in row["metadata_"]


async def test_mixed_batch_retains_each_items_original_agent_and_session(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id)
    results = await _bulk(
        client,
        tenant_id,
        _item(),
        _item(_metadata(agent_id=OTHER_AGENT, session_id="opaque-session/two")),
    )
    expected = [
        _origin(tenant_id),
        _origin(tenant_id, agent=OTHER_AGENT, session="opaque-session/two"),
    ]
    for result, origin in zip(results, expected, strict=True):
        assert result["status"] == "created", result
        row = await _row(sc, tenant_id, result["id"])
        assert row["agent_id"] == f"broker:{INSTALL}"
        assert _system(row).get(ORIGIN_KEY) == origin


async def test_batch_author_is_not_substituted_for_the_receipt_agent(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id)
    memory_id = await _created(client, tenant_id, author=OTHER_AGENT)
    row = await _row(sc, tenant_id, memory_id)
    assert row["agent_id"] == OTHER_AGENT
    assert _system(row).get(ORIGIN_KEY) == _origin(tenant_id)


async def test_item_without_agent_does_not_inherit_a_siblings_origin(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id)
    results = await _bulk(
        client,
        tenant_id,
        _item(),
        _item(_metadata(agent_id=None, session_id="other-session")),
    )
    for result in results:
        assert result["status"] == "created", result
    first, second = [await _row(sc, tenant_id, result["id"]) for result in results]
    assert first["agent_id"] == second["agent_id"] == AGENT
    assert _system(first).get(ORIGIN_KEY) == _origin(tenant_id)
    assert _system(second).get(ORIGIN_KEY) is None


@pytest.mark.parametrize(
    "session", ["  opaque session / one\t", "会話/記録", "s" * 4096]
)
async def test_origin_preserves_the_exact_opaque_session(
    client, as_auth, sc, tenant_id, session
):
    _broker(as_auth, tenant_id)
    memory_id = await _created(client, tenant_id, _item(_metadata(session_id=session)))
    assert _system(await _row(sc, tenant_id, memory_id)).get(ORIGIN_KEY) == _origin(
        tenant_id, session=session
    )


async def test_uuid_binding_is_normalized_without_resolving_agent_labels(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id, install=INSTALL.upper())
    memory_id = await _created(
        client, tenant_id, _item(_metadata(agent_id=OTHER_AGENT.upper()))
    )
    assert _system(await _row(sc, tenant_id, memory_id)).get(ORIGIN_KEY) == _origin(
        tenant_id, agent=OTHER_AGENT
    )


async def test_a_null_hash_receipt_keeps_origin_without_claiming_validation(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id)
    receipt = RECEIPT | {"rule_set_hash": None}
    memory_id = await _created(
        client, tenant_id, _item(_metadata(rules_receipt=receipt))
    )
    system = _system(await _row(sc, tenant_id, memory_id))
    assert system.get(ORIGIN_KEY) == _origin(tenant_id)
    assert system["rules_receipt"] == receipt
    assert "rules_receipt_validation" not in system


@pytest.mark.parametrize(
    "changes",
    [
        {"agent_id": None},
        {"agent_id": "claude-code"},
        {"agent_id": "not-a-uuid"},
        {"agent_id": [AGENT]},
        {"session_id": None},
        {"session_id": "   "},
        {"session_id": 42},
        {"session_id": "s" * 4097},
    ],
    ids=[
        "missing-agent",
        "agent-label",
        "malformed-agent",
        "agent-list",
        "missing-session",
        "blank-session",
        "numeric-session",
        "long-session",
    ],
)
async def test_incomplete_origin_does_not_drop_the_write_or_guess_a_binding(
    client, as_auth, sc, tenant_id, changes
):
    _broker(as_auth, tenant_id)
    memory_id = await _created(client, tenant_id, _item(_metadata(**changes)))
    system = _system(await _row(sc, tenant_id, memory_id))
    assert system["rules_receipt"] == RECEIPT
    assert system.get(ORIGIN_KEY) is None


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"event_id": "not-a-uuid", "rule_set_hash": "a" * 64},
        {"event_id": RECEIPT["event_id"]},
    ],
    ids=["no-receipt", "malformed-receipt", "missing-hash"],
)
async def test_complete_origin_fields_without_a_valid_receipt_are_not_kept(
    client, as_auth, sc, tenant_id, receipt
):
    _broker(as_auth, tenant_id)
    memory_id = await _created(
        client, tenant_id, _item(_metadata(rules_receipt=receipt))
    )
    system = _system(await _row(sc, tenant_id, memory_id))
    assert system.get("rules_receipt") is None
    assert system.get(ORIGIN_KEY) is None


async def test_missing_authenticated_install_cannot_be_replaced_by_metadata(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id, install=None)
    forged = _origin(tenant_id)
    memory_id = await _created(
        client, tenant_id, _item(_metadata(**{ORIGIN_KEY: forged}))
    )
    row = await _row(sc, tenant_id, memory_id)
    assert _system(row).get(ORIGIN_KEY) is None
    assert ORIGIN_KEY not in row["metadata_"]


@pytest.mark.parametrize("kind", ["agent", "person"])
async def test_ordinary_credentials_cannot_create_origin(
    client, as_auth, sc, tenant_id, kind
):
    if kind == "agent":
        as_auth(tenant_id, agent_id=AGENT)
    else:
        as_auth(tenant_id, is_person=True, user_id="person-origin", org_role="admin")
    forged = _origin(tenant_id)
    memory_id = await _created(
        client,
        tenant_id,
        _item(_metadata(**{ORIGIN_KEY: forged, "_system": {ORIGIN_KEY: forged}})),
        author=AGENT,
    )
    row = await _row(sc, tenant_id, memory_id)
    assert _system(row).get(ORIGIN_KEY) is None
    assert _system(row).get("rules_receipt") is None
    assert ORIGIN_KEY not in row["metadata_"]


async def test_single_write_cannot_create_a_broker_origin(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id)
    forged = _origin(tenant_id)
    response = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": AGENT,
            **_item(_metadata(**{ORIGIN_KEY: forged})),
        },
    )
    assert response.status_code == 201, response.text
    row = await _row(sc, tenant_id, response.json()["id"])
    assert _system(row).get(ORIGIN_KEY) is None
    assert ORIGIN_KEY not in row["metadata_"]


@pytest.mark.parametrize(
    "mode", ["merge", "replace", "clear", "merge-owned", "replace-owned"]
)
async def test_later_metadata_edits_preserve_original_binding(
    client, as_auth, sc, tenant_id, mode
):
    _broker(as_auth, tenant_id)
    memory_id = await _created(client, tenant_id)
    await _seed_origin(sc, tenant_id, memory_id)
    as_auth(tenant_id, is_person=True, user_id="editor", org_role="admin")
    forged = _origin(
        tenant_id, install=OTHER_INSTALL, agent=OTHER_AGENT, session="later-session"
    )
    edit = {
        "metadata_mode": "merge" if mode.startswith("merge") else "replace",
        "metadata": None
        if mode == "clear"
        else _metadata(
            session_id="later-session",
            agent_id=OTHER_AGENT,
            **{ORIGIN_KEY: forged, "_system": {ORIGIN_KEY: forged}},
        ),
    }
    if mode.endswith("owned"):
        edit["metadata"].update(summary="Caller summary", tags=["caller-tag"])
    response = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json=edit,
    )
    assert response.status_code == 200, response.text
    row = await _row(sc, tenant_id, memory_id)
    assert _system(row).get(ORIGIN_KEY) == _origin(tenant_id)
    assert _system(row).get("rules_receipt") == RECEIPT
    assert ORIGIN_KEY not in (row.get("metadata_") or {})
    if mode.endswith("owned"):
        assert row["metadata_"]["summary"] == "Caller summary"
        assert row["metadata_"]["tags"] == ["caller-tag"]


@pytest.mark.parametrize("mode", ["merge", "replace", "clear"])
async def test_content_edits_preserve_origin_through_platform_metadata_updates(
    client, as_auth, sc, tenant_id, mode
):
    _broker(as_auth, tenant_id)
    memory_id = await _created(client, tenant_id)
    as_auth(tenant_id, is_person=True, user_id="editor", org_role="admin")
    response = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={
            "content": f"The maintenance window moved to Friday {uuid4().hex}",
            "metadata_mode": "merge" if mode == "merge" else "replace",
            "metadata": None if mode == "clear" else {"session_id": "edited-session"},
        },
    )
    assert response.status_code == 200, response.text
    row = await _row(sc, tenant_id, memory_id)
    assert _system(row).get(ORIGIN_KEY) == _origin(tenant_id)
    assert _system(row).get("rules_receipt") == RECEIPT


@pytest.mark.parametrize("status", ["active", "cancelled"])
async def test_held_row_decisions_preserve_original_binding(
    client, as_auth, sc, tenant_id, status
):
    _broker(as_auth, tenant_id)
    memory_id = await _created(
        client, tenant_id, _item(status=QUARANTINED_MEMORY_STATUS)
    )
    await _seed_origin(sc, tenant_id, memory_id)
    as_auth(tenant_id, is_person=True, user_id="reviewer", org_role="admin")
    held = await client.get(
        f"/api/v1/memories/held?tenant_id={tenant_id}&session_id={SESSION}"
    )
    assert held.status_code == 200, held.text
    [item] = held.json()["items"]
    assert item["system_metadata"][ORIGIN_KEY] == _origin(tenant_id)
    response = await client.patch(
        f"/api/v1/memories/{memory_id}/status?tenant_id={tenant_id}",
        json={"status": status},
    )
    assert response.status_code == 200, response.text
    # A rejected row is hidden from ordinary reads; verify its stored
    # provenance without weakening the production read filter.
    from core_storage_api.services.postgres_service import get_session

    async with get_session() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT status, metadata AS metadata_ FROM memories WHERE tenant_id=:t AND id=:id"
                    ),
                    {"t": tenant_id, "id": memory_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == status
    assert _system(row)[ORIGIN_KEY] == _origin(tenant_id)


@pytest.mark.parametrize(
    "same_attempt", [False, True], ids=["duplicate-content", "duplicate-attempt"]
)
async def test_duplicate_writes_do_not_replace_the_first_origin(
    client, as_auth, sc, tenant_id, same_attempt
):
    _broker(as_auth, tenant_id)
    attempt = f"origin-{uuid4().hex}"
    item = _item()
    memory_id = await _created(client, tenant_id, item, attempt=attempt)
    await _seed_origin(sc, tenant_id, memory_id)
    _broker(as_auth, tenant_id, install=OTHER_INSTALL if same_attempt else INSTALL)
    later = item | {
        "metadata": _metadata(agent_id=OTHER_AGENT, session_id="later-session")
    }
    [result] = await _bulk(
        client,
        tenant_id,
        later,
        attempt=attempt if same_attempt else None,
        author=AGENT,
    )
    assert result["status"] == (
        "duplicate_attempt" if same_attempt else "duplicate_content"
    ), result
    assert result.get("id") == memory_id or result.get("duplicate_of") == memory_id
    row = await _row(sc, tenant_id, memory_id)
    assert _system(row)[ORIGIN_KEY] == _origin(tenant_id)
    assert _system(row)["rules_receipt"] == RECEIPT


async def test_reading_legacy_receipt_does_not_infer_or_backfill_origin(
    client, as_auth, sc, tenant_id
):
    _broker(as_auth, tenant_id)
    memory_id = await _created(client, tenant_id)
    row = await _row(sc, tenant_id, memory_id)
    metadata = deepcopy(row["metadata_"])
    metadata.get("_system", {}).pop(ORIGIN_KEY, None)
    await sc.update_memory(memory_id, tenant_id, {"metadata_": metadata})
    _broker(as_auth, tenant_id, install=OTHER_INSTALL)
    response = await client.get(f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}")
    assert response.status_code == 200, response.text
    assert (response.json().get("system_metadata") or {}).get(ORIGIN_KEY) is None
    assert _system(await _row(sc, tenant_id, memory_id)).get(ORIGIN_KEY) is None
