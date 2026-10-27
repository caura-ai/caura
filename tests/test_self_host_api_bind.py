"""Both standalone Compose entrypoints bind the API to loopback by default."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.dev.yml"])
def test_compose_api_bind_defaults_to_loopback(filename):
    compose = yaml.safe_load((_ROOT / filename).read_text())
    assert compose["services"]["core-api"]["ports"] == [
        "${API_BIND:-127.0.0.1}:${API_PORT:-8000}:8000"
    ]
