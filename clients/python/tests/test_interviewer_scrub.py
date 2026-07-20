"""Secret-scrub tests — credential shapes must never leave the machine."""

from __future__ import annotations

from memclaw_client.interviewer.scrub import REDACTED, scrub


def test_scrubs_common_token_shapes():
    samples = [
        "sk-" + "a" * 48,
        "sk-proj-" + "b" * 60,
        "mc_" + "c" * 20,
        "mca_" + "d" * 20,
        "ghp_" + "e" * 36,
        "github_pat_" + "k" * 30,
        "xoxb-1234567890-abcdefghij",
        "AKIA" + "F" * 16,
        "aws_secret_access_key = '" + "m" * 40 + "'",
        "AWS-SECRET-ACCESS-KEY: " + "n/+" * 13 + "nn",
        "Bearer " + "g" * 32,
        "api_key = 'hijklmnopqrstuvwx1234'",
        "eyJ" + "h" * 20 + "." + "i" * 20 + "." + "j" * 10,
    ]
    for sample in samples:
        out = scrub(f"context {sample} more context")
        assert REDACTED in out, sample
        assert sample not in out, sample


def test_scrubs_pem_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    out = scrub(f"here is the key\n{pem}\ndone")
    assert "PRIVATE KEY" not in out


def test_leaves_normal_prose_alone():
    text = "We decided to use Postgres for the watermark store, keyed per file."
    assert scrub(text) == text


def test_short_assignment_values_are_not_false_positives():
    """The broad assignment pattern requires >= 20 value chars — short
    technical identifiers must survive untouched."""
    text = "set token = 'abc123def456' in the config"  # 12 chars: below threshold
    assert scrub(text) == text
