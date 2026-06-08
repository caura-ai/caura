"""Entity name blocklist: filter obviously generic names before upsert."""

import pytest

from core_api.constants import ENTITY_NAME_BLOCKLIST, MIN_ENTITY_NAME_LENGTH
from core_api.services.entity_extraction_worker import _is_valid_entity


@pytest.mark.unit
class TestEntityBlocklist:
    """Validate _is_valid_entity filter."""

    @pytest.mark.parametrize("name", sorted(ENTITY_NAME_BLOCKLIST))
    def test_blocklisted_names_rejected(self, name):
        assert not _is_valid_entity(name)

    @pytest.mark.parametrize("name", sorted(ENTITY_NAME_BLOCKLIST))
    def test_blocklist_case_insensitive(self, name):
        assert not _is_valid_entity(name.upper())
        assert not _is_valid_entity(name.capitalize())

    @pytest.mark.parametrize("name", ["a", "x", ""])
    def test_short_names_rejected(self, name):
        assert not _is_valid_entity(name)

    @pytest.mark.parametrize(
        "name",
        ["john smith", "microsoft", "kafka", "seattle", "react", "project alpha"],
    )
    def test_real_entities_pass(self, name):
        assert _is_valid_entity(name)

    def test_min_length_constant(self):
        assert MIN_ENTITY_NAME_LENGTH >= 2

    def test_blocklist_is_nonempty(self):
        assert len(ENTITY_NAME_BLOCKLIST) > 0
