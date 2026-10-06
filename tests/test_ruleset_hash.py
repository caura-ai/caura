"""Rule-set hash (plan row g1.2): Python half of the shared vectors.

The broker's Go suite (caura-ai/caura-daemon ``internal/ruleset``) runs the same
vectors file, so both languages are held to identical canonical bytes and hashes
for every case, unicode and the empty set included. The file is copied byte for
byte between the repos and both suites pin its sha256.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from common.governance.ruleset_hash import (
    RuleSetHashError,
    canonical_rule_set,
    canonical_timestamp,
    rule_set_hash,
    rules_from_keystone_rows,
    short_hash,
)

pytestmark = pytest.mark.unit

VECTORS_PATH = Path(__file__).parent / "fixtures" / "ruleset-hash-vectors.json"
# Same bytes as caura-ai/caura-daemon internal/ruleset/testdata/
# ruleset-hash-vectors.json, whose Go suite pins the same digest (vectorsSHA256 in
# hash_test.go). Changing either copy fails its suite until both copies and both
# pins move together.
VECTORS_SHA256 = "0165bef1af2ae98c3ce94c318d8fe03c153c238876e81786c454656d0e3efc10"

EMPTY_SET_HASH = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def _vectors() -> list[dict]:
    return json.loads(VECTORS_PATH.read_bytes())["vectors"]


def test_vectors_file_is_pinned() -> None:
    digest = hashlib.sha256(VECTORS_PATH.read_bytes()).hexdigest()
    assert digest == VECTORS_SHA256, (
        f"tests/fixtures/{VECTORS_PATH.name} changed (sha256 {digest}). Copy the same "
        "bytes to caura-ai/caura-daemon internal/ruleset/testdata/ and update both pins."
    )


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_vector(vector: dict) -> None:
    if vector.get("error"):
        with pytest.raises(RuleSetHashError):
            rule_set_hash(vector["rules"])
        return
    assert canonical_rule_set(vector["rules"]).decode("utf-8") == vector["canonical"]
    assert rule_set_hash(vector["rules"]) == vector["hash"]


def test_empty_set_hashes_the_empty_array() -> None:
    assert rule_set_hash([]) == EMPTY_SET_HASH
    assert rule_set_hash(iter(())) == EMPTY_SET_HASH


def test_rules_may_come_from_any_iterable() -> None:
    rules = next(v for v in _vectors() if v["name"] == "each scope")
    assert rule_set_hash(r for r in rules["rules"]) == rules["hash"]


def test_keystone_rows_hash_like_their_rules() -> None:
    """A documents-store row (what GET /keystones returns) hashes like the flat rule."""
    one_rule = next(v for v in _vectors() if v["name"] == "one rule")
    row = {
        "id": "0b5c1d2e-0000-4000-8000-000000000000",
        "tenant_id": "t1",
        "fleet_id": None,
        "collection": "_keystones",
        "doc_id": "no-force-push",
        "data": {
            "title": "No force-push",
            "content": "Never force-push to main.",
            "weight": 100,
            "scope": "tenant",
        },
        "agent_id": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-10-06T07:55:14.123456+00:00",
    }
    assert rule_set_hash(rules_from_keystone_rows([row])) == one_rule["hash"]


@pytest.mark.parametrize(
    "row",
    [
        {"doc_id": "r", "updated_at": "2026-10-06T07:55:14Z"},
        {"doc_id": "r", "data": "not a mapping", "updated_at": "2026-10-06T07:55:14Z"},
        {
            "doc_id": "r",
            "data": {"content": "C.", "scope": "tenant"},
            "updated_at": "2026-10-06T07:55:14Z",
        },
    ],
    ids=["no data", "data not a mapping", "no weight"],
)
def test_malformed_keystone_rows_are_refused(row: dict) -> None:
    with pytest.raises(RuleSetHashError):
        rule_set_hash(rules_from_keystone_rows([row]))


def test_aware_datetimes_convert_like_strings() -> None:
    """core-storage holds datetimes before it serialises them; both forms agree."""
    moment = datetime(2026, 10, 7, 1, 30, 0, 1, tzinfo=timezone(timedelta(hours=2)))
    assert canonical_timestamp(moment) == canonical_timestamp(
        "2026-10-07T01:30:00.000001+02:00"
    )
    assert canonical_timestamp(moment.astimezone(UTC)) == "2026-10-06T23:30:00.000001Z"


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(RuleSetHashError):
        canonical_timestamp(datetime(2026, 10, 6, 7, 55, 14))


def test_trailing_newline_is_refused() -> None:
    """``$`` in a Python regex matches before a final newline; fullmatch doesn't."""
    with pytest.raises(RuleSetHashError):
        canonical_timestamp("2026-10-06T07:55:14Z\n")


def test_huge_integer_weight_is_refused() -> None:
    """json.loads keeps big integers exact; a double can't hold this one."""
    rule = {
        "doc_id": "r",
        "content": "C.",
        "scope": "tenant",
        "updated_at": "2026-10-06T07:55:14Z",
    }
    with pytest.raises(RuleSetHashError):
        rule_set_hash([{**rule, "weight": 10**400}])


def test_short_hash() -> None:
    assert short_hash(EMPTY_SET_HASH) == "4f53cda1"
