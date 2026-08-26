"""Phase 5.1: the interviewer CLI reads both env spellings.

Rule 3 makes the old names permanent, so every case is asserted in both
directions — asserting only the new name would pass after someone deleted the
fallback and stranded every crontab an older installer wrote.

Old names are confined to the constants below so the ratchet has one marked
line per name rather than one per assertion.
"""

import pytest

from caura_client.interviewer.cli import _build_parser, _read_env, _resolve_allowlist

OLD_PREFIX = "MEMCLAW_"  # legacy-name-ok: rule 3 — the alias prefix under test
NEW_PREFIX = "CAURA_"

ALIASED = [
    ("api_key", "API_KEY", "k-value"),
    ("tenant_id", "TENANT_ID", "t-value"),
    ("agent_id", "AGENT_ID", "a-value"),
    ("fleet_id", "FLEET_ID", "f-value"),
    ("base_url", "BASE_URL", "https://example.invalid"),
]


SUFFIXES = [suffix for _, suffix, _ in ALIASED] + ["INTERVIEWER_PROJECTS", "INTERVIEWER_HARNESS"]


@pytest.fixture
def clean_env(monkeypatch):
    for suffix in SUFFIXES:
        monkeypatch.delenv(NEW_PREFIX + suffix, raising=False)
        monkeypatch.delenv(OLD_PREFIX + suffix, raising=False)
    return monkeypatch


def _parse(argv):
    return _build_parser().parse_args(argv)


@pytest.mark.parametrize(("dest", "suffix", "value"), ALIASED)
def test_old_name_still_read(clean_env, dest, suffix, value):
    clean_env.setenv(OLD_PREFIX + suffix, value)
    assert getattr(_parse(["status"]), dest) == value


@pytest.mark.parametrize(("dest", "suffix", "value"), ALIASED)
def test_new_name_read(clean_env, dest, suffix, value):
    clean_env.setenv(NEW_PREFIX + suffix, value)
    assert getattr(_parse(["status"]), dest) == value


@pytest.mark.parametrize(("dest", "suffix", "value"), ALIASED)
def test_new_name_wins_when_both_are_set(clean_env, dest, suffix, value):
    clean_env.setenv(NEW_PREFIX + suffix, value)
    clean_env.setenv(OLD_PREFIX + suffix, "old-" + value)
    assert getattr(_parse(["status"]), dest) == value


@pytest.mark.parametrize(("dest", "suffix", "value"), ALIASED)
def test_blank_new_name_does_not_shadow_the_old_one(clean_env, dest, suffix, value):
    clean_env.setenv(NEW_PREFIX + suffix, "")
    clean_env.setenv(OLD_PREFIX + suffix, value)
    assert getattr(_parse(["status"]), dest) == value


def test_harness_reads_either_name(clean_env):
    clean_env.setenv(OLD_PREFIX + "INTERVIEWER_HARNESS", "cursor")
    assert _parse(["status"]).harness == "cursor"
    clean_env.setenv(NEW_PREFIX + "INTERVIEWER_HARNESS", "claude-code")
    assert _parse(["status"]).harness == "claude-code"


def test_allowlist_reads_either_name(clean_env):
    clean_env.setenv(OLD_PREFIX + "INTERVIEWER_PROJECTS", "a/*, b/*")
    args = _parse(["status"])
    assert _resolve_allowlist(args) == ["a/*", "b/*"]
    clean_env.setenv(NEW_PREFIX + "INTERVIEWER_PROJECTS", "c/*")
    assert _resolve_allowlist(args) == ["c/*"]


def test_explicit_flag_still_beats_both_env_names(clean_env):
    clean_env.setenv(NEW_PREFIX + "API_KEY", "from-new-env")
    clean_env.setenv(OLD_PREFIX + "API_KEY", "from-old-env")
    assert _parse(["status", "--api-key", "from-flag"]).api_key == "from-flag"


class TestReadEnv:
    def test_default_applies_only_when_nothing_is_set(self):
        assert _read_env("CAURA_ABSENT_NEW", "CAURA_ABSENT_OLD", default="fallback") == "fallback"

    def test_blank_only_alias_is_honoured(self, monkeypatch):
        # ``KEY=`` in a sourced env file is how an operator blanks a value, and
        # that has to keep meaning "empty" rather than falling back to default.
        monkeypatch.setenv("CAURA_BLANK_PROBE", "")
        assert _read_env("CAURA_BLANK_PROBE", default="fallback") == ""
