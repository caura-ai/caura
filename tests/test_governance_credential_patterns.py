"""Credential shapes the governance scanner used to miss, and look-alikes it
must keep ignoring.

* Caura's own credentials (``mc_`` and the other minted / still-accepted
  prefixes) had no rule at all, so a tenant's key pasted into a memory was
  stored unmasked under a drop or mask policy.
* Current OpenAI keys (``sk-proj-…`` / ``sk-svcacct-…`` / ``sk-admin-…``) have
  ``-`` and ``_`` in the body, which the OpenAI rule did not allow.
* ``OPENAI_API_KEY=…``-style env assignments had no cue the generic secret
  rule could see (``_`` is a word character, so no ``\\b`` before ``API_KEY``).
* The JSON spelling ``"api_key": "…"`` put the name's closing quote where the
  key/value rules required ``:`` or ``=``.
* L-227: the token-shape gate took any mixed-case, high-entropy body for a
  minted one, so PascalCase and Title-Case identifiers after ``mc_``, ``ca_``
  or ``sk-`` scanned as keys, and the ``sk-`` rule gated its whole match, so the
  prefix's own lower case let an upper-case body through.

False positives here are not free — the drop policy 422s the write and the
mask policy rewrites stored content — so every widening carries negatives.
"""

from __future__ import annotations

import random
import string
import time

import pytest

from common.governance import PIICategory, mask, scan

pytestmark = pytest.mark.unit

# token_urlsafe(32)-shaped bodies (43 chars, A-Za-z0-9_-), fixed for stability.
_BODY = "Qm9vdHN0cmFwLXRva2VuLWZvci10ZXN0cy0x_Ab9-Zq"
_BODY2 = "x7Kp2Lm9Qr4Tv8Wz1Ab5Cd3Ef6Gh0Jk-Mn_Pq2Rs4Tu"


def _cats(text: str) -> set[PIICategory]:
    return {f.category for f in scan(text)}


@pytest.mark.parametrize(
    "prefix",
    [
        "mc_",  # current API credential
        "ca_",  # canonical API credential spelling
        "mca_",  # pre-unification agent key
        "mcx_",
        "mco_",
        "mcrk_",  # registration keys
        "cark_",
        "mcft_",  # fleet join tokens
        "caft_",
        "mci_v1_",  # install credentials
        "cai_v2_",
    ],
)
def test_caura_credentials_are_api_keys(prefix: str):
    key = prefix + _BODY
    text = f"set it to {key} on the new node"
    findings = scan(text)
    assert [f.category for f in findings] == [PIICategory.API_KEY]
    assert key not in mask(text, findings)


def test_caura_key_in_the_installer_env_line():
    text = f"CAURA_API_KEY={'mc_' + _BODY2}"
    findings = scan(text)
    assert PIICategory.API_KEY in {f.category for f in findings}
    assert _BODY2 not in mask(text, findings)


@pytest.mark.parametrize(
    "text",
    [
        "ca_certificate_bundle_path_for_the_cluster is mounted",
        "the mc_memory_consolidation_scheduler_backoff_seconds knob",
        "export CA_ROOT=ca_SOME_LONG_CONSTANT_NAME_FOR_THE_CLUSTER",
        "mc_" + "a" * 40,
        "short mc_Ab3dEf6hIj9kLm2n key",  # under the 32-char body floor
        "macro mcp_server_transport_streamable_http_mode",  # not a Caura prefix
    ],
)
def test_caura_prefix_look_alikes_are_not_flagged(text: str):
    assert PIICategory.API_KEY not in _cats(text)


@pytest.mark.parametrize(
    "key",
    [
        "sk-proj-" + _BODY + _BODY2,
        "sk-svcacct-" + _BODY,
        "sk-admin-" + _BODY2,
    ],
)
def test_current_openai_key_formats(key: str):
    text = f"my openai key is {key}"
    findings = scan(text)
    assert [f.category for f in findings] == [PIICategory.API_KEY]
    assert mask(text, findings) == "my openai key is «API_KEY»"


def test_anthropic_key_still_one_finding():
    text = "key sk-ant-api03-" + _BODY + " rotated"
    findings = scan(text)
    assert [f.category for f in findings] == [PIICategory.API_KEY]
    assert mask(text, findings) == "key «API_KEY» rotated"


@pytest.mark.parametrize(
    "text",
    [
        "the sk-learn-compatible-pipeline-wrapper module",
        "see sk-hynix-memory-module-roadmap-2026 deck",
    ],
)
def test_sk_kebab_words_are_not_keys(text: str):
    assert PIICategory.API_KEY not in _cats(text)


@pytest.mark.parametrize(
    "line",
    [
        "OPENAI_API_KEY=" + _BODY,
        "export GITHUB_TOKEN=" + _BODY2,
        "STRIPE_SECRET_KEY: " + _BODY,
        "DB_PASSWORD='" + _BODY2 + "'",
    ],
)
def test_env_style_names_cue_the_secret_rule(line: str):
    findings = scan(line)
    assert findings, line
    masked = mask(line, findings)
    assert _BODY not in masked and _BODY2 not in masked
    # The variable name survives: only the value is redacted.
    assert masked.split("=")[0].split(":")[0].strip().split()[-1] in masked


@pytest.mark.parametrize(
    "text",
    [
        "next_page_token=" + _BODY,  # lower-case identifier, not an env var
        "MAX_TOKENS=4096",
        "OPENAI_API_KEY=changeme",  # low-entropy placeholder
        "SESSION_TOKEN=<your-token-here>",
    ],
)
def test_env_style_negatives(text: str):
    assert PIICategory.SECRET not in _cats(text)


@pytest.mark.parametrize(
    "text",
    [
        '{"api_key": "' + _BODY + '"}',
        '{"password": "' + _BODY2 + '"}',
        '{"client_secret":"' + _BODY + '"}',
        '{"aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}',
    ],
)
def test_json_quoted_key_names(text: str):
    findings = scan(text)
    assert PIICategory.SECRET in {f.category for f in findings}
    masked = mask(text, findings)
    assert masked.count("«SECRET»") == 1
    # The JSON key name stays readable.
    assert masked.split('"')[1] in masked


@pytest.mark.parametrize(
    "text",
    [
        "the mc_PlayerInventorySerializationHandler class",
        "set ca_CertificateBundlePathForProduction in the chart",
        "see the sk-Hynix-Memory-Roadmap-2026-Plan deck",
        # Upper-case only once the prefix is set aside: a part number, not a key.
        "order sk-X9K2-PQ7R-ZT4M-WB8N-HC3J today",
    ],
)
def test_identifiers_after_a_key_prefix_are_not_keys(text: str):
    """L-227: these were refused under drop and rewritten under mask."""
    assert PIICategory.API_KEY not in _cats(text)


_URLSAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


@pytest.mark.parametrize("prefix", ["mc_", "ca_", "sk-proj-", "sk-svcacct-"])
def test_minted_bodies_are_still_keys(prefix: str):
    """Control: a body that reads as words is rare in a random one, so the
    gate that sets identifiers aside still flags every minted key."""
    rng = random.Random(227)
    keys = [prefix + "".join(rng.choices(_URLSAFE, k=43)) for _ in range(2000)]
    missed = [key for key in keys if PIICategory.API_KEY not in _cats(key)]
    assert missed == []


def test_the_word_test_is_linear_on_a_long_lower_case_run():
    """Control: the body reaches the word test (mixed case, high entropy), and a
    nested word pattern would backtrack exponentially on its lower-case run.
    This scans user content."""
    body = string.ascii_lowercase + string.ascii_lowercase[:18] + "Z"
    started = time.perf_counter()
    scan("mc_" + body)
    assert time.perf_counter() - started < 1.0
