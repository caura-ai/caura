"""Platform-edge tests: Windows lock degradation, malformed ioreg output."""

from __future__ import annotations

import builtins
import subprocess

from caura_client.interviewer import machine
from caura_client.interviewer.cli import _acquire_lock


def test_acquire_lock_proceeds_when_fcntl_unavailable(monkeypatch):
    """No fcntl (Windows) must mean 'run without a lock', NOT 'locked'."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert _acquire_lock() is not None  # proceed, don't fake "already locked"


def test_acquire_lock_returns_handle_on_unix():
    handle = _acquire_lock()
    assert handle is not None


def test_machine_id_survives_malformed_ioreg(monkeypatch):
    """A quoteless IOPlatformUUID line must fall through, not IndexError."""

    class FakeResult:
        # Quoteless AND single-stray-quote variants: neither may crash,
        # and the stray quote (2 split parts) must NOT return the raw
        # line as a fake UUID — the >= 4 guard requires two quote pairs.
        stdout = (
            "  IOPlatformUUID = malformed-without-quotes\n"
            '  IOPlatformUUID = "\n'
        )

    monkeypatch.setattr(machine, "_MACHINE_ID_SHORT", None)  # bust the per-process cache
    monkeypatch.setattr(machine.sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: FakeResult()
    )
    # Falls through ioreg → /etc/machine-id (absent on macOS test) → host fallback.
    mid = machine.machine_id_short()
    assert len(mid) == 12 and all(c in "0123456789abcdef" for c in mid)


def test_get_document_percent_encodes_doc_id():
    """A doc_id with '/' or '?' must stay ONE path segment."""
    import httpx

    from caura_client import Caura

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # url.path is the DECODED view; raw_path is what goes on the wire.
        seen["raw_path"] = request.url.raw_path.decode()
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"doc_id": "x", "data": {}})

    mc = Caura("mc_test", tenant_id="t", transport=httpx.MockTransport(handler))
    mc.get_document("a/b?c=d", collection="col")
    assert seen["raw_path"].startswith("/api/v1/documents/a%2Fb%3Fc%3Dd")
    assert seen["params"] == {"tenant_id": "t", "collection": "col"}


def test_pem_scrub_terminates_fast_on_begin_without_end():
    """A large input with a BEGIN marker but no END must not blow up."""
    import time

    from caura_client.interviewer.scrub import scrub

    text = "-----BEGIN RSA PRIVATE KEY-----\n" + ("A" * 200_000)
    start = time.monotonic()
    out = scrub(text)
    assert time.monotonic() - start < 2.0
    assert out.startswith("-----BEGIN")  # unmatched: left as-is, no hang


def test_acquire_lock_closes_fd_when_flock_fails(monkeypatch):
    """A flock failure must not leak the just-opened file handle."""
    import fcntl as real_fcntl

    from caura_client.interviewer import cli as cli_mod

    import errno as errno_mod

    def failing_flock(handle, flags):
        raise OSError(errno_mod.EAGAIN, "locked by someone else")

    monkeypatch.setattr(real_fcntl, "flock", failing_flock)
    opened = []
    real_open = open

    def tracking_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr("builtins.open", tracking_open)
    assert cli_mod._acquire_lock() is None
    assert opened and all(h.closed for h in opened)


def test_run_transcript_respects_allowlist(tmp_path):
    """--transcript must not bypass the project allowlist."""
    from caura_client.interviewer.cli import main

    project_dir = tmp_path / "secret-project"
    project_dir.mkdir()
    transcript = project_dir / "abc.jsonl"
    transcript.write_text("", encoding="utf-8")

    rc = main(
        [
            "run",
            "--transcript", str(transcript),
            "--projects", "allowed-*",
            "--api-key", "k",
            "--tenant-id", "t",
            "--dry-run",
        ]
    )
    assert rc == 2  # gated: project not in allowlist

    rc_ok = main(
        [
            "run",
            "--transcript", str(transcript),
            "--projects", "secret-*",
            "--api-key", "k",
            "--tenant-id", "t",
            "--dry-run",
        ]
    )
    assert rc_ok == 0  # allowlisted project passes

    rc_empty = main(
        [
            "run",
            "--transcript", str(transcript),
            "--api-key", "k",
            "--tenant-id", "t",
            "--dry-run",
        ]
    )
    assert rc_empty == 2  # EMPTY allowlist: default-deny has no carve-outs

    rc_all = main(
        [
            "run",
            "--transcript", str(transcript),
            "--all-projects",
            "--api-key", "k",
            "--tenant-id", "t",
            "--dry-run",
        ]
    )
    assert rc_all == 0  # explicit --all-projects opt-in passes


def test_run_closes_lock_handle_deterministically(tmp_path, monkeypatch):
    """The lock handle must be closed by the caller, not left to GC."""
    from caura_client.interviewer import cli as cli_mod

    lock_file = tmp_path / "test.lock"
    handle = open(lock_file, "w")
    monkeypatch.setattr(cli_mod, "_acquire_lock", lambda: handle)

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "abc.jsonl"
    transcript.write_text("", encoding="utf-8")

    rc = cli_mod.main(
        [
            "run",
            "--transcript", str(transcript),
            "--all-projects",
            "--api-key", "k",
            "--tenant-id", "t",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert handle.closed  # released in the finally, not by refcount luck


def test_status_survives_vanished_transcript(tmp_path, monkeypatch):
    """A transcript deleted between discovery and count_lines must not
    crash status — per-file isolation covers the whole loop body."""
    from caura_client.interviewer import cli as cli_mod

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "abc.jsonl"
    transcript.write_text("", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "read_watermark", lambda mc, node_id: -1)

    def vanishing_count(path):
        raise FileNotFoundError(f"{path} deleted mid-status")

    monkeypatch.setattr(cli_mod, "count_lines", vanishing_count)
    rc = cli_mod.main(
        [
            "status",
            "--projects-root", str(tmp_path),
            "--all-projects",
            "--since-hours", "0",
            "--api-key", "k",
            "--tenant-id", "t",
        ]
    )
    assert rc == 0  # file skipped with a message, command completes


def test_zeroed_machine_id_falls_through_to_host_fallback(tmp_path, monkeypatch):
    """An all-zeros /etc/machine-id (common container default) must NOT
    become the identity — two such containers would share a watermark."""
    import builtins
    import hashlib
    import io

    zeros = "0" * 32
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/etc/machine-id":
            return io.StringIO(zeros + "\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(machine, "_MACHINE_ID_SHORT", None)
    monkeypatch.setattr(machine.sys, "platform", "linux")
    monkeypatch.setattr(builtins, "open", fake_open)

    mid = machine.machine_id_short()
    zeros_digest = hashlib.sha1(zeros.encode(), usedforsecurity=False).hexdigest()[:12]
    assert mid != zeros_digest  # fell through to the host-based fallback


def test_watermark_doc_id_digest_unchanged_by_fips_kwarg():
    """usedforsecurity=False must not change the digest — the client and
    server compute the SAME watermark doc id."""
    import hashlib

    from caura_client.interviewer.runner import watermark_doc_id

    node = "cc:abcdef123456:some-file"
    expected = f"wm_{hashlib.sha1(node.encode()).hexdigest()[:40]}"
    assert watermark_doc_id(node) == expected


def test_acquire_lock_proceeds_lockless_on_permission_error(monkeypatch, capsys):
    """EACCES on the lock file is an environment problem, not contention:
    the CLI must warn and proceed, never read it as 'already locked'."""
    import errno as errno_mod
    import fcntl as real_fcntl

    from caura_client.interviewer import cli as cli_mod

    def denied_flock(handle, flags):
        raise OSError(errno_mod.EACCES, "permission denied")

    monkeypatch.setattr(real_fcntl, "flock", denied_flock)
    result = cli_mod._acquire_lock()
    assert result is not None  # proceeds lockless
    assert "proceeding without it" in capsys.readouterr().err
