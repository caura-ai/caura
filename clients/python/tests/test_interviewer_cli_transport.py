"""caura-interviewer treats a plain-HTTP base URL as a configuration error (L-232).

``Caura()`` refuses to send the API key over plain HTTP to another host unless
``CAURA_ALLOW_INSECURE_HTTP`` is set (L-66). ``run`` and ``status`` built their
client unguarded, so such a URL printed a traceback and exited 1, where this CLI
documents exit 2 for a configuration error. ``install`` never built a client, so
it scheduled a cron job that failed on every tick. Installs against a plain-HTTP
LAN server meet this on upgrade, so the message names the opt-in.
"""

from __future__ import annotations

import pytest

from caura_client.interviewer import cli, installer

LAN_HTTP = "http://caura.lan:8000"
CONFIG = ["--api-key", "mc_k", "--tenant-id", "t1", "--base-url", LAN_HTTP]


@pytest.fixture(autouse=True)
def _no_opt_in(monkeypatch):
    monkeypatch.delenv("CAURA_ALLOW_INSECURE_HTTP", raising=False)


@pytest.fixture
def cron(monkeypatch, tmp_path):
    """The install IO seams: a crontab held in memory, files under ``tmp_path``."""
    table = {"text": ""}
    monkeypatch.setattr(installer, "cron_available", lambda: True)
    monkeypatch.setattr(installer, "read_crontab", lambda: table["text"])
    monkeypatch.setattr(installer, "write_crontab", lambda text: table.update(text=text))
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(installer, "env_file_path", lambda: tmp_path / "env")
    monkeypatch.setattr(installer, "log_file_path", lambda: tmp_path / "cron.log")
    monkeypatch.setattr(installer, "resolve_cmd", lambda: "caura-interviewer")
    return table


def test_run_refuses_a_plain_http_url_with_exit_2(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_transcripts", lambda **_kw: [object()])
    monkeypatch.setattr(cli, "_acquire_lock", lambda: object())

    rc = cli.main(["run", "--all-projects", *CONFIG])

    assert rc == 2
    assert "CAURA_ALLOW_INSECURE_HTTP" in capsys.readouterr().err


def test_status_refuses_a_plain_http_url_with_exit_2(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_transcripts", lambda **_kw: [])

    rc = cli.main(["status", "--all-projects", *CONFIG])

    assert rc == 2
    assert "CAURA_ALLOW_INSECURE_HTTP" in capsys.readouterr().err


def test_install_does_not_schedule_a_job_that_would_fail_every_tick(cron, tmp_path, capsys):
    rc = cli.main(["install", "--all-projects", *CONFIG])

    assert rc == 2
    assert cron["text"] == ""
    assert not (tmp_path / "env").exists()
    assert "CAURA_ALLOW_INSECURE_HTTP" in capsys.readouterr().err


def test_install_with_the_opt_in_schedules_the_job_with_it(cron, monkeypatch, tmp_path):
    """Control: the opt-in at install time is the one the job runs with."""
    monkeypatch.setenv("CAURA_ALLOW_INSECURE_HTTP", "true")

    rc = cli.main(["install", "--all-projects", *CONFIG])

    assert rc == 0
    assert cron["text"]
    assert "export CAURA_ALLOW_INSECURE_HTTP='true'" in (tmp_path / "env").read_text()
