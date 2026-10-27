"""Cross-run lock files belong to the current user's private config directory."""

import errno
import getpass
import os
import stat
import tempfile
from types import SimpleNamespace

import pytest

from caura_client.interviewer import cli, installer

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix file locking")


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(shared))


def test_lock_is_private_and_second_run_contends():
    handle = cli._acquire_lock()
    try:
        path = installer.config_dir() / "run.lock"
        assert path.is_file()
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert cli._acquire_lock() is None
    finally:
        handle.close()
    again = cli._acquire_lock()
    assert again is not None
    again.close()


def test_symlink_lock_does_not_truncate_the_target(tmp_path, capsys):
    directory = installer.config_dir()
    directory.mkdir(parents=True)
    target = tmp_path / "keep.txt"
    target.write_text("keep this", encoding="utf-8")
    (directory / "run.lock").symlink_to(target)
    result = cli._acquire_lock()
    assert result is not None
    if hasattr(result, "close"):
        result.close()
    assert target.read_text(encoding="utf-8") == "keep this"
    assert "proceeding without it" in capsys.readouterr().err


def test_foreign_lock_is_not_reported_as_contention(monkeypatch, capsys):
    import fcntl

    real_fstat = os.fstat
    monkeypatch.setattr(
        os,
        "fstat",
        lambda fd: SimpleNamespace(st_uid=os.getuid() + 1, st_mode=real_fstat(fd).st_mode, st_nlink=1),
    )

    def contended(*args):
        raise OSError(errno.EAGAIN, "busy")

    monkeypatch.setattr(fcntl, "flock", contended)
    result = cli._acquire_lock()
    assert result is not None
    assert "proceeding without it" in capsys.readouterr().err


def _legacy_path():
    from pathlib import Path

    name = f"memclaw-interviewer-{getpass.getuser()}.lock"  # legacy-name-floor: installed lock path
    return Path(tempfile.gettempdir()) / name


def test_an_older_run_still_prevents_an_overlapping_new_run():
    import fcntl

    with _legacy_path().open("w") as older:
        fcntl.flock(older, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = cli._acquire_lock()
        try:
            assert result is None
        finally:
            if hasattr(result, "close"):
                result.close()


def test_a_new_run_still_blocks_an_older_run():
    import fcntl

    result = cli._acquire_lock()
    try:
        with _legacy_path().open("r+") as older, pytest.raises(BlockingIOError):
            fcntl.flock(older, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        result.close()


def test_a_symlink_in_the_compatibility_location_keeps_the_private_guard(tmp_path, capsys):
    target = tmp_path / "keep-legacy.txt"
    target.write_text("keep this", encoding="utf-8")
    _legacy_path().symlink_to(target)
    result = cli._acquire_lock()
    try:
        assert result is not None
        assert target.read_text(encoding="utf-8") == "keep this"
        assert "using private lock" in capsys.readouterr().err
        assert cli._acquire_lock() is None
    finally:
        result.close()


def test_preexisting_private_lock_keeps_content_and_tightens_permissions():
    directory = installer.config_dir()
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    path = directory / "run.lock"
    path.write_text("keep content", encoding="utf-8")
    path.chmod(0o644)
    result = cli._acquire_lock()
    try:
        assert path.read_text(encoding="utf-8") == "keep content"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    finally:
        result.close()


def test_hardlinked_lock_does_not_modify_the_target(tmp_path, capsys):
    directory = installer.config_dir()
    directory.mkdir(parents=True)
    target = tmp_path / "keep-hardlink.txt"
    target.write_text("keep content", encoding="utf-8")
    target.chmod(0o644)
    os.link(target, directory / "run.lock")
    result = cli._acquire_lock()
    assert result is not None
    assert target.read_text(encoding="utf-8") == "keep content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert "proceeding without it" in capsys.readouterr().err


def test_owned_symlinked_config_directory_still_coordinates_runs(tmp_path):
    directory = installer.config_dir()
    directory.parent.mkdir(parents=True)
    target = tmp_path / "dotfiles" / "interviewer"
    target.mkdir(parents=True)
    target.chmod(0o755)
    directory.symlink_to(target, target_is_directory=True)
    result = cli._acquire_lock()
    try:
        assert (target / "run.lock").is_file()
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        assert cli._acquire_lock() is None
    finally:
        if hasattr(result, "close"):
            result.close()
