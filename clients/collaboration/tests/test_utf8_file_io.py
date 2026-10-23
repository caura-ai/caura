"""UTF-8 persistence must not depend on the host locale (e.g. Windows cp1252)."""

import json

from caura_bus_cli.hooks import load_settings
from caura_bus_cli.runtime import WakeState
from caura_bus_cli.setup import peer_template, write_private
from caura_bus_core.config import load_config


def test_toml_config_decodes_non_ascii_peer_names(tmp_path):
    config = tmp_path / "caura-bus.toml"
    config.write_bytes(
        'api_url = "https://caura.test"\npeers = ["Café ops — équipe"]\n'
        '[agent]\nagent_id = "alice"\ntenant_id = "team"\n'.encode("utf-8")
    )
    assert load_config(config).peers == ["Café ops — équipe"]


def test_private_config_writer_preserves_utf8_bytes(tmp_path):
    config = tmp_path / "caura-bus.toml"
    write_private(
        config,
        'api_url = "https://caura.test"\npeers = ["Café ops — équipe"]\n'
        '[agent]\nagent_id = "alice"\ntenant_id = "team"\n',
    )
    assert b"Caf\xc3\xa9" in config.read_bytes()
    assert load_config(config).peers == ["Café ops — équipe"]


def test_wake_state_reads_utf8_json(tmp_path):
    state_file = tmp_path / "wake-state.json"
    state_file.write_bytes(json.dumps({"notice": "Café — ≤"}, ensure_ascii=False).encode("utf-8"))
    state = WakeState(state_file)
    assert state.load()["notice"] == "Café — ≤"
    state.save({"notice": "Café — ≤"})
    assert state.load()["notice"] == "Café — ≤"


def test_hook_settings_preserve_unicode(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_bytes(json.dumps({"note": "Café — ≤"}, ensure_ascii=False).encode("utf-8"))
    assert load_settings(settings)["note"] == "Café — ≤"


def test_packaged_peer_template_decodes_utf8():
    assert "—" in peer_template()
