"""Pin the ``mc_`` API-key prefix — decided permanent, now enforced.

The rebrand's do-not-touch list froze ``mc_`` forever: every issued key
starts with it, and keys live in customers' config files, secret managers
and CI. The legacy-name ratchet (#863) cannot protect it — the constant
contains no old-brand text — so until this test existed, nothing did.
Wave 5's mass rewrite is the most likely thing to sweep it up by accident;
this is the tripwire (handover §04, "pin mc_ with a test, not prose").
"""

from __future__ import annotations

import pytest

from core_api.constants import API_KEY_PREFIX

pytestmark = pytest.mark.unit


def test_api_key_prefix_is_mc_forever():
    assert API_KEY_PREFIX == "mc_", (
        "API_KEY_PREFIX is permanently 'mc_' — every issued key embeds it, "
        "and customers' stored keys stop authenticating if it drifts. This "
        "was decided in the rebrand's never-rename list; changing it is not "
        "a rename, it is a key-revocation event. Revert the change."
    )
