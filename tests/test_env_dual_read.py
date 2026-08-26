"""Phase 5.1: every server-side env read accepts the new name and the old one.

Rule 3 makes the old spellings permanent, so each case here is asserted in both
directions — a test that only proves the new name works would pass just as well
after someone deleted the fallback.

Each line that has to spell the old name carries its own ``legacy-name-ok``
marker, and they are kept to a minimum: two env-name constants plus the one
helper that touches the settings field.
"""

import re
from pathlib import Path

import pytest

from core_api import constants
from core_api.config import Settings

OLD_API_KEY_ENV = "MEMCLAW_API_KEY"  # legacy-name-ok: rule 3 — the alias under test
OLD_VERSION_ENV = "MEMCLAW_VERSION"  # legacy-name-ok: rule 3 — the alias under test
NEW_API_KEY_ENV = "CAURA_API_KEY"
NEW_VERSION_ENV = "CAURA_VERSION"


@pytest.fixture
def clean_env(monkeypatch):
    for name in (NEW_API_KEY_ENV, OLD_API_KEY_ENV, NEW_VERSION_ENV, OLD_VERSION_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _resolved_api_key(**overrides):
    """The api-key setting, whichever spelling supplied it."""
    return Settings(**overrides).memclaw_api_key  # legacy-name-ok: rule 3 — field the aliases feed


class TestApiKeyResolution:
    def test_old_name_still_resolves(self, clean_env):
        clean_env.setenv(OLD_API_KEY_ENV, "old-key")
        assert _resolved_api_key() == "old-key"

    def test_new_name_resolves(self, clean_env):
        clean_env.setenv(NEW_API_KEY_ENV, "new-key")
        assert _resolved_api_key() == "new-key"

    def test_new_name_wins_when_both_are_set(self, clean_env):
        clean_env.setenv(NEW_API_KEY_ENV, "new-key")
        clean_env.setenv(OLD_API_KEY_ENV, "old-key")
        assert _resolved_api_key() == "new-key"

    def test_unset_stays_none(self, clean_env):
        assert _resolved_api_key() is None

    def test_blank_new_name_does_not_disable_the_perimeter(self, clean_env):
        # The regression this guards, and why it is not cosmetic: auth.py gates
        # Path 2 on ``if mclaw_key:``, so resolving to "" here silently drops the
        # API-key perimeter on a deploy whose template carries a blank new name
        # next to a working old one. ``AliasChoices`` resolves it exactly that
        # way, which is why this field does not use it.
        clean_env.setenv(NEW_API_KEY_ENV, "")
        clean_env.setenv(OLD_API_KEY_ENV, "real-key")
        assert _resolved_api_key() == "real-key"

    def test_field_name_construction_still_works(self, clean_env):
        assert _resolved_api_key(memclaw_api_key="kw") == "kw"  # legacy-name-ok: rule 3 — field-name path


class TestVersionResolution:
    def test_old_name_still_resolves(self, clean_env):
        clean_env.setenv(OLD_VERSION_ENV, "1.2.3-old")
        assert constants._resolve_version() == "1.2.3-old"

    def test_new_name_resolves(self, clean_env):
        clean_env.setenv(NEW_VERSION_ENV, "1.2.3-new")
        assert constants._resolve_version() == "1.2.3-new"

    def test_new_name_wins_when_both_are_set(self, clean_env):
        clean_env.setenv(NEW_VERSION_ENV, "1.2.3-new")
        clean_env.setenv(OLD_VERSION_ENV, "1.2.3-old")
        assert constants._resolve_version() == "1.2.3-new"

    def test_blank_new_name_does_not_shadow_the_old_one(self, clean_env):
        # The regression this guards: an operator who blanks the new name in a
        # deploy template must not lose a working old one.
        clean_env.setenv(NEW_VERSION_ENV, "")
        clean_env.setenv(OLD_VERSION_ENV, "1.2.3-old")
        assert constants._resolve_version() == "1.2.3-old"


class TestComposeImageTags:
    """The compose image tag reads the same pair, one process removed.

    Compose interpolates in its own process, so no dual-read in this codebase
    reaches it — the fallback has to live in the YAML. Asserted here because
    nothing else can: flattening it to one name would either strand installs
    that pin with the old name today, or silently serve ``latest`` to an
    operator who followed the new docs.
    """

    def test_both_names_are_read_in_the_right_order(self):
        compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
        # First-party images only. Third-party ones (pgvector, redis, TEI) carry
        # their own upstream tags and have nothing to do with our version pin.
        # The trailing group absorbs the ratchet's ``legacy-name-ok`` comment.
        refs = re.findall(
            r"^\s*image:\s*(ghcr\.io/caura-ai/\S+?)(?:\s+#.*)?$", compose, re.MULTILINE
        )
        assert refs, "found no first-party image references to check"
        # Split at the tag separator, not the first colon — the tag itself
        # contains colons once it interpolates.
        tags = [ref[ref.index(":", ref.rindex("/")) + 1 :] for ref in refs]
        expected = "${" + NEW_VERSION_ENV + ":-${" + OLD_VERSION_ENV + ":-latest}}"
        for tag in tags:
            assert tag == expected, (
                f"image tag {tag!r} must interpolate {NEW_VERSION_ENV} with "
                f"{OLD_VERSION_ENV} as its fallback, so an operator pinning with "
                "either name gets the pin rather than 'latest'"
            )
