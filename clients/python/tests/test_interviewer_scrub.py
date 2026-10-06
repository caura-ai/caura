"""Secret-scrub tests — credential shapes must never leave the machine."""

from __future__ import annotations

from caura_client.interviewer.scrub import REDACTED, scrub


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


def test_scrubs_json_quoted_credentials():
    """The JSON spelling closes the key NAME with a quote before the ``:`` —
    the shape of a pasted config file or curl body in a transcript."""
    samples = {
        "api_key": '{"api_key": "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb3d"}',
        "password": '{"user": "ops", "password": "Tr0ub4dor&3-horse+battery"}',
        "aws": '{"aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}',
        "secret": "{'secret': 'Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2'}",
        "client_secret": '"client_secret":"Qm9vdHN0cmFwLXRva2VuLWZvci10"',
    }
    secrets = {
        "api_key": "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb3d",
        "password": "Tr0ub4dor&3-horse+battery",
        "aws": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "secret": "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2",
        "client_secret": "Qm9vdHN0cmFwLXRva2VuLWZvci10",
    }
    for name, sample in samples.items():
        out = scrub(sample)
        assert REDACTED in out, name
        assert secrets[name] not in out, name


def test_scrubs_env_style_names_and_punctuated_values():
    for sample, secret in [
        ("OPENAI_API_KEY=Ab3dEf6hIj9kLm2nOp5qRs8t", "Ab3dEf6hIj9kLm2nOp5qRs8t"),
        ("DB_PASSWORD='p@ss/w0rd+with=punct!uation'", "p@ss/w0rd+with=punct!uation"),
    ]:
        out = scrub(sample)
        assert secret not in out, sample


def test_scrubs_stripe_and_google_keys():
    for sample in [
        "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
        "rk_test_" + "51Hx2Ab3dEf6hIj9kLm2n",
        "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY",
    ]:
        out = scrub(f"key {sample} end")
        assert sample not in out, sample


def test_json_and_url_prose_without_credentials_survive():
    for text in [
        '{"token_type": "bearer", "expires_in": 3600}',
        "The secret: https://docs.example.com/guides/secret-management explains it.",
        '{"password": "short"}',
    ]:
        assert scrub(text) == text, text
