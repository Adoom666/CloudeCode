"""Tests for UploadSweeper — TTL-based pruner for ``.cloude_uploads/``.

Exercises the sweep core (``sweep_now``/``_sweep_once``), the run-loop
cancellation path, and the per-base error isolation. No network, no
real filesystem outside ``tmp_path``, no real tmux.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ---- env bootstrap (matches sibling tests) -----------------------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_sw_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_sw_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.upload_sweeper import UPLOAD_DIR_NAME, UploadSweeper  # noqa: E402


def _make_sweeper(
    base_paths: list[str],
    *,
    ttl_seconds: int = 60,
    interval_seconds: int = 3600,
    default_dir: Path | None = None,
) -> UploadSweeper:
    """Build a sweeper with default_dir set to a guaranteed-empty temp dir
    so it never accidentally sweeps the test host's real working dir."""
    if default_dir is None:
        default_dir = Path(tempfile.mkdtemp(prefix="cc_sw_default_"))
    return UploadSweeper(
        ttl_seconds=ttl_seconds,
        interval_seconds=interval_seconds,
        project_paths=base_paths,
        default_dir=default_dir,
    )


def _seed_file(base: Path, name: str, *, age_seconds: float) -> Path:
    """Create ``<base>/.cloude_uploads/<name>`` and back-date its mtime."""
    bucket = base / UPLOAD_DIR_NAME
    bucket.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = bucket / name
    target.write_bytes(b"x" * 16)
    target.chmod(0o600)
    now = time.time()
    os.utime(target, (now - age_seconds, now - age_seconds))
    return target


# --------------------------------------------------------------------------- #
# Tests — sweep_now / _sweep_once core
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sweeper_prunes_files_older_than_ttl(tmp_path):
    ttl = 60
    target = _seed_file(tmp_path, "old.png", age_seconds=2 * ttl)
    sweeper = _make_sweeper([str(tmp_path)], ttl_seconds=ttl)

    result = await sweeper.sweep_now()

    assert not target.exists(), "expired file must be pruned"
    assert result["files_pruned"] == 1
    assert result["bytes_freed"] == 16


@pytest.mark.asyncio
async def test_sweeper_keeps_recent_files(tmp_path):
    ttl = 3600
    target = _seed_file(tmp_path, "fresh.png", age_seconds=10)
    sweeper = _make_sweeper([str(tmp_path)], ttl_seconds=ttl)

    result = await sweeper.sweep_now()

    assert target.exists(), "fresh file must survive"
    assert result["files_pruned"] == 0
    assert result["bytes_freed"] == 0


@pytest.mark.asyncio
async def test_sweeper_removes_empty_dir_after_prune(tmp_path):
    ttl = 60
    _seed_file(tmp_path, "a.png", age_seconds=2 * ttl)
    _seed_file(tmp_path, "b.png", age_seconds=2 * ttl)
    bucket = tmp_path / UPLOAD_DIR_NAME
    assert bucket.exists()

    sweeper = _make_sweeper([str(tmp_path)], ttl_seconds=ttl)
    result = await sweeper.sweep_now()

    assert not bucket.exists(), "empty bucket must be removed"
    assert result["files_pruned"] == 2


@pytest.mark.asyncio
async def test_sweeper_handles_missing_project_path(tmp_path):
    """Non-existent base path → no crash, returns zeros."""
    bogus = tmp_path / "does_not_exist"
    sweeper = _make_sweeper([str(bogus)], ttl_seconds=60)

    result = await sweeper.sweep_now()

    assert result["files_pruned"] == 0
    assert result["bytes_freed"] == 0


@pytest.mark.asyncio
async def test_sweeper_handles_missing_uploads_dir(tmp_path):
    """Base exists but no ``.cloude_uploads/`` inside → no-op."""
    sweeper = _make_sweeper([str(tmp_path)], ttl_seconds=60)
    result = await sweeper.sweep_now()
    assert result == {"files_pruned": 0, "bytes_freed": 0}


# --------------------------------------------------------------------------- #
# Tests — run-loop cancellation + error isolation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sweeper_run_loop_cancellation(tmp_path, capfd):
    """``run()`` must propagate CancelledError and emit shutdown log.

    structlog is configured with PrintLoggerFactory (see src/main.py) so
    log events land on stdout, not stdlib logging — capfd is the right
    capture fixture here.
    """
    sweeper = _make_sweeper(
        [str(tmp_path)],
        ttl_seconds=60,
        interval_seconds=0.01,
    )

    task = asyncio.create_task(sweeper.run())
    await asyncio.sleep(0)  # let the task start
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert task.cancelled() or task.done()
    out, _ = capfd.readouterr()
    assert "upload_sweeper_stopping" in out


@pytest.mark.asyncio
async def test_sweeper_per_base_error_does_not_kill_loop(tmp_path, monkeypatch, capfd):
    """A raising base path must log warning + the loop continues other bases."""
    good = tmp_path / "good"
    good.mkdir()
    target = _seed_file(good, "good.png", age_seconds=999_999)
    bad_path = "/__cloude_test_bad_path__"

    sweeper = _make_sweeper([bad_path, str(good)], ttl_seconds=60)

    # Force the bad path to raise inside the to_thread-wrapped helper. We
    # patch the module-level _prune_base so we exercise the per-base
    # try/except inside _sweep_once without depending on a chmod hack.
    import src.core.upload_sweeper as sweeper_mod
    real_prune = sweeper_mod._prune_base

    def fake_prune(base: str, cutoff: float):
        if base == bad_path:
            raise PermissionError("simulated prune failure")
        return real_prune(base, cutoff)

    monkeypatch.setattr(sweeper_mod, "_prune_base", fake_prune)

    result = await sweeper.sweep_now()

    # Good base should still have been swept successfully.
    assert not target.exists(), "good base must still be pruned"
    assert result["files_pruned"] == 1
    out, _ = capfd.readouterr()
    assert "upload_sweep_partial_error" in out
