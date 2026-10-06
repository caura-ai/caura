"""Rule-set hash: the name a receipt gives the keystones a session received.

Plan rows g0.1 (the definition) and g1.2. The broker computes the same hash in
Go (caura-ai/caura-daemon ``internal/ruleset``). Both test suites read one
vectors file, ``tests/fixtures/ruleset-hash-vectors.json`` here, and each pins
its sha256, so the two copies can't drift apart unnoticed.

The hash is the lowercase hex SHA-256 of one JSON array serialised per RFC 8785
(JCS). The array holds one object per rule, sorted by ``doc_id`` comparing UTF-8
bytes, and each object has exactly the members ``content``, ``doc_id``,
``scope``, ``updated_at`` and ``weight``, in that order:

* strings escape only ``"``, backslash and U+0000..U+001F, and are not
  Unicode-normalised;
* ``weight`` is written as an ECMAScript number: ``1.0`` becomes ``1`` and
  ``1e-7`` stays ``1e-7``, where ``json.dumps`` writes ``1.0`` and ``1e-07``;
* ``updated_at`` is written in UTC with exactly six fraction digits, where
  ``datetime.isoformat`` drops a zero fraction and writes ``+00:00``.

The empty set hashes ``[]``. Anything the hash would have to guess at raises
:class:`RuleSetHashError`: a duplicate ``doc_id``, a weight that is not a finite
number (``bool`` included), a time without an offset or with more than six
fraction digits, a lone surrogate.

:func:`rules_from_keystone_rows` maps ``GET /keystones`` rows (the documents
store's shape) to the rules the hash takes, so core-api's list route (g1.10)
and rule-set versions (g1.12) hash exactly what the broker does.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

SHORT_LEN = 8
"""Length of the display form, as in the ``Caura rules <hash8>`` heading."""


class RuleSetHashError(ValueError):
    """A rule set the hash can't name without guessing."""


def rule_set_hash(rules: Iterable[Mapping[str, Any]]) -> str:
    """Return the rule-set hash of ``rules``, in any order."""
    return hashlib.sha256(canonical_rule_set(rules)).hexdigest()


def short_hash(rule_set_hash: str) -> str:
    """Return the display form of a hash. Receipts carry the whole hash."""
    return rule_set_hash[:SHORT_LEN]


def canonical_rule_set(rules: Iterable[Mapping[str, Any]]) -> bytes:
    """Return the bytes the hash covers: the RFC 8785 JSON of ``rules``.

    Each rule is a mapping with the keys ``doc_id``, ``content``, ``scope``
    (strings), ``updated_at`` (an RFC 3339 string or an aware ``datetime``) and
    ``weight`` (an ``int`` or ``float``). Other keys are ignored.
    """
    if isinstance(rules, str | bytes | Mapping) or not isinstance(rules, Iterable):
        raise RuleSetHashError("rules must be a list of rules")
    members = sorted(_member(rule) for rule in rules)
    for previous, current in itertools.pairwise(members):
        if previous[0] == current[0]:
            raise RuleSetHashError(f"duplicate doc_id {current[0].decode()!r}")
    return ("[" + ",".join(text for _, text in members) + "]").encode("utf-8")


def rules_from_keystone_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Map keystone rows to the rules the hash takes.

    A row is a document as core-storage returns it: ``doc_id`` and
    ``updated_at`` at the top, ``content``, ``scope`` and ``weight`` under
    ``data``. A missing field maps to ``None``, which hashing then refuses.
    """
    rules = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("data"), Mapping):
            raise RuleSetHashError(
                "a keystone row must be a mapping with a data mapping"
            )
        data = row["data"]
        rules.append(
            {
                "doc_id": row.get("doc_id"),
                "content": data.get("content"),
                "scope": data.get("scope"),
                "weight": data.get("weight"),
                "updated_at": row.get("updated_at"),
            }
        )
    return rules


def _member(rule: object) -> tuple[bytes, str]:
    """Return ``(doc_id as UTF-8, the rule's canonical JSON object)``."""
    if not isinstance(rule, Mapping):
        raise RuleSetHashError("each rule must be a mapping")
    doc_id = _string(rule, "doc_id")
    content = _string(rule, "content")
    scope = _string(rule, "scope")
    if "updated_at" not in rule or "weight" not in rule:
        raise RuleSetHashError(f"rule {doc_id!r} needs updated_at and weight")
    updated_at = canonical_timestamp(rule["updated_at"])
    weight = _number(rule["weight"])
    text = (
        f'{{"content":{_quote(content)},"doc_id":{_quote(doc_id)},'
        f'"scope":{_quote(scope)},"updated_at":"{updated_at}","weight":{weight}}}'
    )
    return doc_id.encode("utf-8"), text


def _string(rule: Mapping[str, Any], key: str) -> str:
    value = rule.get(key)
    if not isinstance(value, str):
        raise RuleSetHashError(f"{key} must be a string, got {type(value).__name__}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:  # a lone surrogate
        raise RuleSetHashError(f"{key} is not valid Unicode: {exc.reason}") from exc
    return value


_ESCAPED = re.compile(r'["\\\x00-\x1f]')
_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _quote(text: str) -> str:
    """Write ``text`` as an RFC 8785 string.

    Only ``"``, backslash and the C0 controls are escaped: the short forms
    where JSON has one, otherwise ``\\u00xx`` in lower case. Everything else
    stays as it is, including ``<``, ``>``, ``&``, U+2028 and U+2029.
    """

    def escape(match: re.Match[str]) -> str:
        ch = match.group()
        return _SHORT_ESCAPES.get(ch) or f"\\u{ord(ch):04x}"

    return '"' + _ESCAPED.sub(escape, text) + '"'


def _number(value: object) -> str:
    """Write ``value`` as ECMAScript's Number::toString does (RFC 8785).

    That is the shortest digits that round-trip, plain notation from 1e-6 up to
    1e21, exponent notation outside it, and 0 for negative zero. An ``int`` is
    converted to the nearest double first, as JSON readers in other languages
    do.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuleSetHashError(f"weight must be a number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise RuleSetHashError("weight does not fit in a double") from exc
    if not math.isfinite(number):
        raise RuleSetHashError("weight must be a finite number")
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    # repr() gives the shortest round-trip digits; Decimal splits them out.
    _, digit_tuple, exponent = Decimal(repr(abs(number))).as_tuple()
    assert isinstance(exponent, int)  # finite, so never 'n', 'N' or 'F'
    digits = "".join(map(str, digit_tuple))
    n = exponent + len(digits)  # the value is 0.<digits> * 10**n
    digits = digits.rstrip("0")
    k = len(digits)
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * -n + digits
    else:
        mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
        text = f"{mantissa}e{'+' if n - 1 >= 0 else '-'}{abs(n - 1)}"
    return sign + text


_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,6}))?(Z|[+-][0-9]{2}:[0-9]{2})"
)


def canonical_timestamp(value: object) -> str:
    """Convert a time to the form the hash covers, as ``2026-10-06T07:55:14.123456Z``.

    ``value`` is an RFC 3339 string with upper-case ``T``, an offset (``Z`` or
    ``+HH:MM``) and at most six fraction digits, or an aware ``datetime``.
    Postgres keeps microseconds, so six digits lose nothing. Anything else is
    refused: no offset, a seventh digit, a space for ``T``, lower case, or a field
    out of range (February 30, second 60, offset hour 24).
    """
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise RuleSetHashError("updated_at must carry a time zone")
        moment = value
    elif isinstance(value, str):
        match = _TIMESTAMP.fullmatch(value)
        if match is None:
            raise RuleSetHashError(
                f"updated_at {value!r} is not an RFC 3339 time with an offset and at "
                "most six fraction digits"
            )
        year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
        micro = int((match.group(7) or "").ljust(6, "0"))
        zone = match.group(8)
        offset = timedelta(0)
        if zone != "Z":
            zone_hours, zone_minutes = int(zone[1:3]), int(zone[4:6])
            if zone_hours > 23 or zone_minutes > 59:
                raise RuleSetHashError(
                    f"updated_at {value!r} has an out-of-range offset"
                )
            offset = timedelta(hours=zone_hours, minutes=zone_minutes)
            if zone[0] == "-":
                offset = -offset
        try:
            moment = datetime(
                year, month, day, hour, minute, second, micro, tzinfo=timezone(offset)
            )
        except ValueError as exc:
            raise RuleSetHashError(f"updated_at {value!r} is out of range") from exc
    else:
        raise RuleSetHashError(
            f"updated_at must be a string, got {type(value).__name__}"
        )
    try:
        utc = moment.astimezone(UTC)
    except OverflowError as exc:
        raise RuleSetHashError(
            f"updated_at {value!r} is outside years 1-9999 in UTC"
        ) from exc
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}Z"
    )
