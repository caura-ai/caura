"""The keystones list names its rules by their rule-set hash (plan row g1.10).

A dashboard compares a receipt's hash with the envelope's ``rule_set_hash`` to
tell whether a session got the current rules, and the broker computes the same
hash over the rules it receives (caura-ai/caura-daemon ``internal/ruleset``). So
the route is held to the vectors both languages share, fed through as the
documents store returns keystone rows.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Response

from core_api.constants import KEYSTONES_EMPTY_HINT
from core_api.routes import keystones

pytestmark = pytest.mark.unit

VECTORS_PATH = Path(__file__).parent / "fixtures" / "ruleset-hash-vectors.json"
TENANT = "test-tenant-g110"


def _vectors() -> list[dict]:
    """The shared vectors a keystone list can carry: a list of objects.

    Two refusals ("rules is not an array", "a rule is not an object") test the
    hash function's input contract, which storage rows can't reach.
    """
    vectors = json.loads(VECTORS_PATH.read_bytes())["vectors"]
    return [
        v
        for v in vectors
        if isinstance(v["rules"], list) and all(isinstance(r, dict) for r in v["rules"])
    ]


def _vector(name: str) -> dict:
    [vector] = [v for v in _vectors() if v["name"] == name]
    return vector


def _row(rule: dict) -> dict:
    """A vector's rule as the documents store returns it: ``doc_id`` and
    ``updated_at`` at the top, everything else under ``data``. A key the rule
    lacks stays missing."""
    row = {"id": "row-id", "tenant_id": TENANT, "collection": "_keystones"}
    row |= {k: rule[k] for k in ("doc_id", "updated_at") if k in rule}
    row["data"] = {k: v for k, v in rule.items() if k not in ("doc_id", "updated_at")}
    return row


async def _list(
    monkeypatch, rows: list[dict], *, envelope: bool, truncated: bool = False
):
    """Call the list route on ``rows`` as storage's answer; return
    ``(body, response)``."""
    storage = MagicMock(name="storage_client")
    storage.list_keystones = AsyncMock(return_value=(rows, truncated))
    monkeypatch.setattr(keystones, "get_storage_client", lambda: storage)
    response = Response()
    body = await keystones.list_keystones(
        response,
        tenant_id=TENANT,
        fleet_id=None,
        agent_id=None,
        envelope=envelope,
        auth=MagicMock(name="auth"),
    )
    return body, response


@pytest.mark.parametrize(
    "vector", [v for v in _vectors() if not v.get("error")], ids=lambda v: v["name"]
)
async def test_the_envelope_hash_matches_each_vector(monkeypatch, vector: dict) -> None:
    rows = [_row(rule) for rule in vector["rules"]]
    body, _ = await _list(monkeypatch, rows, envelope=True)
    assert body["rule_set_hash"] == vector["hash"]
    assert body["items"] == rows


@pytest.mark.parametrize(
    "vector", [v for v in _vectors() if v.get("error")], ids=lambda v: v["name"]
)
async def test_a_set_the_hash_refuses_is_listed_without_one(
    monkeypatch, caplog: pytest.LogCaptureFixture, vector: dict
) -> None:
    rows = [_row(rule) for rule in vector["rules"]]
    with caplog.at_level(logging.WARNING, logger=keystones.logger.name):
        body, _ = await _list(monkeypatch, rows, envelope=True)
    # The rules still go out: a session needs them whatever the hash says.
    assert body["rule_set_hash"] is None
    assert (body["count"], body["items"]) == (len(rows), rows)
    [record] = [r for r in caplog.records if r.name == keystones.logger.name]
    assert TENANT in record.getMessage()


async def test_a_capped_list_hashes_the_rules_it_returns(monkeypatch) -> None:
    """The broker hashes the rules it received, so after the cap the two must
    cover the same rules: the ones returned, not the ones that matched."""
    vector = _vector("each scope")
    rows = [_row(rule) for rule in vector["rules"]]
    body, response = await _list(monkeypatch, rows, envelope=True, truncated=True)
    assert response.headers["X-Truncated"] == "true"
    assert body["rule_set_hash"] == vector["hash"]


async def test_an_empty_set_has_the_empty_set_hash_and_the_hint(monkeypatch) -> None:
    body, _ = await _list(monkeypatch, [], envelope=True)
    assert body["rule_set_hash"] == _vector("empty set")["hash"]
    assert body["hint"] == KEYSTONES_EMPTY_HINT


async def test_the_bare_array_is_unchanged(monkeypatch) -> None:
    """The default shape is a bare array, which can't carry the hash."""
    rows = [_row(rule) for rule in _vector("one rule")["rules"]]
    body, _ = await _list(monkeypatch, rows, envelope=False)
    assert body == rows
