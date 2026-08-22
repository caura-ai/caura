"""Tests for `memclaw-interviewer install` / `uninstall` (cron scheduling)."""

from __future__ import annotations

import os
import stat

import pytest

from caura_client.interviewer import installer
from caura_client.interviewer.installer import (
    CRON_MARKER,
    build_cron_line,
    build_run_command,
    interval_to_cron,
    merge_crontab,
    render_env_file,
)


def test_interval_to_cron_variants():
    assert interval_to_cron("30m") == "*/30 * * * *"
    assert interval_to_cron("5m") == "*/5 * * * *"
    assert interval_to_cron("1h") == "0 * * * *"
    assert interval_to_cron("2h") == "0 */2 * * *"
    assert interval_to_cron(" 15m ") == "*/15 * * * *"


@pytest.mark.parametrize("bad", ["", "30", "0m", "90m", "0h", "24h", "abc", "-5m", "1d"])
def test_interval_to_cron_rejects(bad):
    with pytest.raises(ValueError):
        interval_to_cron(bad)


def test_merge_crontab_is_idempotent_and_preserves_others():
    user = "0 9 * * * /usr/bin/backup\n# my note\n"
    line = build_cron_line("*/30 * * * *", "run-thing")
    once = merge_crontab(user, line)
    assert user.strip() in once  # the user's own lines survive
    assert once.count(CRON_MARKER) == 1
    # Re-installing replaces, never duplicates, our managed line.
    twice = merge_crontab(once, build_cron_line("0 * * * *", "run-thing"))
    assert twice.count(CRON_MARKER) == 1
    assert "0 * * * *" in twice and "*/30 * * * *" not in twice
    assert "/usr/bin/backup" in twice


def test_merge_crontab_uninstall_strips_only_ours():
    user = "0 9 * * * /usr/bin/backup\n"
    installed = merge_crontab(user, build_cron_line("*/30 * * * *", "x"))
    removed = merge_crontab(installed, None)
    assert CRON_MARKER not in removed
    assert "/usr/bin/backup" in removed


def test_merge_crontab_empty_stays_empty():
    assert merge_crontab("", None) == ""


def test_build_run_command_sources_env_and_redirects(tmp_path):
    cmd = build_run_command(
        harness="cursor", all_projects=False,
        env_file=tmp_path / "env", log_file=tmp_path / "log", cmd="mclaw",
    )
    # Grouped so the log redirect covers the source step too.
    assert cmd == f"{{ . {tmp_path/'env'} && mclaw run --harness cursor; }} >> {tmp_path/'log'} 2>&1"
    assert "--all-projects" not in cmd


def test_build_run_command_quotes_harness_defense_in_depth(tmp_path):
    """The helper must not trust the caller to have validated `harness` —
    a shell metacharacter must be quoted, never injected."""
    cmd = build_run_command(
        harness="cursor; rm -rf /", all_projects=False,
        env_file=tmp_path / "env", log_file=tmp_path / "log", cmd="mclaw",
    )
    assert "--harness 'cursor; rm -rf /'" in cmd  # the ; is inside quotes, inert


def test_build_run_command_all_projects_flag(tmp_path):
    cmd = build_run_command(
        harness="claude-code", all_projects=True,
        env_file=tmp_path / "env", log_file=tmp_path / "log", cmd="mclaw",
    )
    assert "--harness claude-code --all-projects" in cmd


def test_render_env_file_only_set_keys_and_quotes():
    out = render_env_file({
        "MEMCLAW_BASE_URL": "https://memclaw.corp.internal",
        "MEMCLAW_API_KEY": "mc_secret",
        "MEMCLAW_TENANT_ID": "t1",
        "MEMCLAW_AGENT_ID": "",          # falsy → omitted
        "MEMCLAW_INTERVIEWER_PROJECTS": "app-*,foo",
    })
    assert "export MEMCLAW_BASE_URL='https://memclaw.corp.internal'" in out
    assert "export MEMCLAW_API_KEY='mc_secret'" in out
    assert "export MEMCLAW_INTERVIEWER_PROJECTS='app-*,foo'" in out
    assert "MEMCLAW_AGENT_ID" not in out  # empty value not written
    assert "MEMCLAW_FLEET_ID" not in out  # absent key not written


def test_render_env_file_escapes_single_quotes():
    out = render_env_file({"MEMCLAW_API_KEY": "a'b"})
    assert r"'a'\''b'" in out


def test_write_env_file_no_fd_leak_on_fsync_error(tmp_path, monkeypatch):
    """If fsync raises, the fd must still be closed (no leak) and the 0600
    temp file (holding the key) must be removed — and the error propagates."""
    import tempfile

    opened_fd = {}
    real_mkstemp = tempfile.mkstemp
    def tracking_mkstemp(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        opened_fd["fd"] = fd
        return fd, path
    monkeypatch.setattr(installer.tempfile, "mkstemp", tracking_mkstemp)

    closed = []
    real_close = os.close
    monkeypatch.setattr(installer.os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    monkeypatch.setattr(installer.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("EIO")))

    with pytest.raises(OSError, match="EIO"):
        installer.write_env_file(tmp_path / "env", "export X='1'\n")

    assert opened_fd["fd"] in closed, "fd leaked — never closed after fsync error"
    assert not (tmp_path / "env").exists()  # target not created
    assert not list(tmp_path.glob(".env-*")), "temp file with secrets left behind"


def test_write_env_file_is_0600(tmp_path):
    p = tmp_path / "sub" / "env"
    installer.write_env_file(p, "export X='1'\n")
    assert p.read_text() == "export X='1'\n"
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, oct(mode)


# ---- end-to-end install/uninstall with IO seams monkeypatched --------------

class _FakeCron:
    def __init__(self):
        self.table = ""
    def read(self):
        return self.table
    def write(self, text):
        self.table = text


def _patch(monkeypatch, tmp_path, cron, available=True):
    monkeypatch.setattr(installer, "cron_available", lambda: available)
    monkeypatch.setattr(installer, "read_crontab", cron.read)
    monkeypatch.setattr(installer, "write_crontab", cron.write)
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(installer, "env_file_path", lambda: tmp_path / "cfg" / "env")
    monkeypatch.setattr(installer, "log_file_path", lambda: tmp_path / "cfg" / "cron.log")
    monkeypatch.setattr(installer, "resolve_cmd", lambda: "memclaw-interviewer")


def test_install_writes_cron_and_env_then_uninstall_removes(monkeypatch, tmp_path, capsys):
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)

    rc = main([
        "install", "--interval", "30m",
        "--base-url", "https://memclaw.corp.internal",
        "--api-key", "mc_k", "--tenant-id", "t1",
        "--projects", "app-*",
    ])
    assert rc == 0
    assert cron.table.count(CRON_MARKER) == 1
    assert "*/30 * * * *" in cron.table
    assert "run --harness claude-code" in cron.table
    env_txt = (tmp_path / "cfg" / "env").read_text()
    assert "export MEMCLAW_API_KEY='mc_k'" in env_txt
    assert "export MEMCLAW_INTERVIEWER_PROJECTS='app-*'" in env_txt
    assert stat.S_IMODE(os.stat(tmp_path / "cfg" / "env").st_mode) == 0o600

    # re-install (hourly) replaces, does not duplicate
    rc = main(["install", "--interval", "1h", "--base-url", "u", "--api-key", "k", "--tenant-id", "t", "--all-projects"])
    assert rc == 0
    assert cron.table.count(CRON_MARKER) == 1
    assert "--all-projects" in cron.table and "0 * * * *" in cron.table

    rc = main(["uninstall"])
    assert rc == 0
    assert CRON_MARKER not in cron.table
    assert not (tmp_path / "cfg" / "env").exists()


def test_read_crontab_forces_c_locale(monkeypatch):
    """The empty-crontab sentinel is English, so the subprocess must run under
    LANG=C/LC_ALL=C regardless of the user's locale."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    installer.read_crontab()
    assert captured["env"]["LANG"] == "C"
    assert captured["env"]["LC_ALL"] == "C"
    assert "PATH" in captured["env"]  # base environment preserved (merged, not replaced)


def test_read_crontab_converts_oserror_to_runtimeerror(monkeypatch):
    """A crontab binary that can't exec (TOCTOU vs cron_available) must surface
    as RuntimeError, which the command-level except clauses catch."""
    def boom(*a, **k):
        raise FileNotFoundError("crontab: not found")
    monkeypatch.setattr(installer.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="could not be executed"):
        installer.read_crontab()


def test_install_cleans_up_on_crontab_oserror(monkeypatch, tmp_path, capsys):
    """write_crontab raising OSError (un-executable binary) must hit the clean
    path AND remove the orphaned 0600 env file — not escape as a traceback."""
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)

    def boom(_text):
        raise PermissionError("crontab: permission denied")
    monkeypatch.setattr(installer, "write_crontab", boom)

    rc = main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t", "--base-url", "u"])
    assert rc == 1
    assert not (tmp_path / "cfg" / "env").exists()  # secret cleaned up
    assert "failed to update crontab" in capsys.readouterr().err


def test_install_removes_orphaned_env_file_on_crontab_failure(monkeypatch, tmp_path, capsys):
    """If the crontab write fails, no job is scheduled — the 0600 env file
    (with the API key) must not be left orphaned."""
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)

    def boom(_text):
        raise RuntimeError("crontab -l failed (exit 1): host busy")
    monkeypatch.setattr(installer, "write_crontab", boom)

    rc = main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t", "--base-url", "u"])
    assert rc == 1
    assert not (tmp_path / "cfg" / "env").exists()  # cleaned up, no stray secret
    err = capsys.readouterr().err
    assert "failed to update crontab" in err and "has been removed" in err


def test_uninstall_survives_unremovable_env_file(monkeypatch, tmp_path, capsys):
    """An OSError removing the env file must warn, not crash with a traceback."""
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)
    main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t", "--base-url", "u"])

    import pathlib
    real_unlink = pathlib.Path.unlink
    def deny(self, *a, **k):
        if self.name == "env":
            raise PermissionError("read-only fs")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "unlink", deny)

    rc = main(["uninstall"])
    assert rc == 0  # cron still removed; env-file failure is a warning
    assert CRON_MARKER not in cron.table
    assert "could not remove env file" in capsys.readouterr().err


def test_install_env_file_write_failure_is_clean(monkeypatch, tmp_path, capsys):
    """An OSError writing the env file must exit 1 cleanly (no traceback) and
    leave the crontab untouched — it's written before the crontab call."""
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)

    def boom(_path, _content):
        raise OSError("disk full")
    monkeypatch.setattr(installer, "write_env_file", boom)

    rc = main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t", "--base-url", "u"])
    assert rc == 1
    assert cron.table == ""  # crontab never touched
    assert "failed to write env file" in capsys.readouterr().err


def test_install_refuses_without_allowlist(monkeypatch, tmp_path, capsys):
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)
    rc = main(["install", "--base-url", "u", "--api-key", "k", "--tenant-id", "t"])
    assert rc == 2  # default-deny: would schedule a no-op
    assert cron.table == ""  # nothing written


def test_install_refuses_without_credentials(monkeypatch, tmp_path):
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)
    rc = main(["install", "--all-projects", "--tenant-id", "t"])  # no api-key
    assert rc == 2
    assert cron.table == ""


def test_install_no_cron_binary_is_clean_error(monkeypatch, tmp_path, capsys):
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron, available=False)
    rc = main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t"])
    assert rc == 2
    assert "crontab" in capsys.readouterr().err.lower()


def test_uninstall_keep_env(monkeypatch, tmp_path, capsys):
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)
    main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t", "--base-url", "u"])
    assert (tmp_path / "cfg" / "env").exists()
    rc = main(["uninstall", "--keep-env"])
    assert rc == 0
    assert (tmp_path / "cfg" / "env").exists()  # preserved
    assert CRON_MARKER not in cron.table
    assert "env file kept" in capsys.readouterr().out


def test_install_locks_config_dir_to_0700(monkeypatch, tmp_path):
    """The config dir holds the world-readable cron log, so it must be owner-
    only (0700) — not the default 0755."""
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)
    main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t", "--base-url", "u"])
    mode = stat.S_IMODE(os.stat(tmp_path / "cfg").st_mode)
    assert mode == 0o700, oct(mode)


def test_install_tightens_preexisting_loose_config_dir(monkeypatch, tmp_path):
    """mkdir(exist_ok) won't change an existing dir's mode — the explicit
    chmod must still lock a pre-existing 0755 config dir down to 0700."""
    from caura_client.interviewer.cli import main
    cfg = tmp_path / "cfg"
    cfg.mkdir(mode=0o755)
    os.chmod(cfg, 0o755)  # ensure loose regardless of umask
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o755
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)
    main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t", "--base-url", "u"])
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o700


def test_uninstall_keep_env_silent_when_no_env_file(monkeypatch, tmp_path, capsys):
    """--keep-env must not claim 'env file kept' when there is no env file."""
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)
    # Nothing installed → no env file exists.
    rc = main(["uninstall", "--keep-env"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "env file kept" not in out and "env file deleted" not in out


def test_uninstall_reports_deleted_only_when_a_file_existed(monkeypatch, tmp_path, capsys):
    """Wording must reflect reality: don't claim 'deleted' with no file."""
    from caura_client.interviewer.cli import main
    cron = _FakeCron()
    _patch(monkeypatch, tmp_path, cron)

    # Nothing installed: no cron entry, no env file → neither claim.
    rc = main(["uninstall"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no cron entry found" in out
    assert "env file deleted" not in out and "env file kept" not in out

    # After a real install, uninstall reports the actual deletion.
    main(["install", "--all-projects", "--api-key", "k", "--tenant-id", "t", "--base-url", "u"])
    main(["uninstall"])
    assert "env file deleted" in capsys.readouterr().out
