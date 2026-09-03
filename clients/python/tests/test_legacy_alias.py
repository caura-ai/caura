"""Permanent-alias contract for the 2026-08 client rename.

The canonical spelling is Caura / caura_client. The separate legacy import
package and the two legacy package-forwarder distributions that once
depended on this one were retired (no transition owed to pre-rename installs
— see docs/plans/rebrand-alias-retirement-policy.md).
What remains permanent is the in-package class-level alias: the old class
and exception names are the same objects as their Caura-spelled counterparts,  # legacy-name-ok: rule 3 permanent class/exception aliases
so existing code catching the old exception names or importing the old
class name directly keeps working. These tests are the tripwire for that
narrower guarantee.
"""

from __future__ import annotations

import caura_client


def test_class_alias_is_identity_not_subclass():
    assert caura_client.MemClaw is caura_client.Caura


def test_legacy_exception_catches_new_raises():
    err = caura_client.CauraAPIError(500, "boom")
    try:
        raise err
    except caura_client.MemClawAPIError as caught:
        assert caught.status_code == 500
