"""The owned-set must survive clearing ONE session's stale pointer.

THE MEASUREMENT THIS REPLACES A GUESS WITH. On the live mac-mini-m4
install, ``launchd.log`` records the pair

    session_metadata_loaded          2026-08-20T16:51:55.886161Z
    stale_session_metadata_deleted   2026-08-20T16:51:55.926682Z

four times over, always ~40ms apart, always preceded by
``session_metadata_slug_not_in_backend`` (4 occurrences, while every other
branch that reaches ``_clear_stale_metadata`` recorded 0). After the last
one the file never came back: three later starts logged
``no_existing_session_metadata`` and the file is absent from the machine
today.

WHAT THAT COSTS. ``session_metadata.json`` is the ONLY durable home of
``SessionManager.owned_tmux_sessions``. ``_clear_stale_metadata`` unlinks
the whole file in order to discard ONE session's un-rehydratable pointer,
so N sessions lose their ownership record to clean up 1. The trigger is
not an error path: ``session_metadata_slug_not_in_backend`` is the
ordinary case where the last-active tmux session is simply gone by the
next start. With the owned set empty, ``resolve_ownership`` falls past
its tier-3 legacy set and every session the launcher created reads
EXTERNAL.

WHAT IS AND IS NOT IN SCOPE HERE. This is the narrow fix: stop destroying
the ownership record. It does NOT make ``origin='created'`` get written on
the create path - that is the deeper root cause and it is deliberately
left for design review (see docs/session-attribution-import.md). A green
suite here means one destructive bug is closed, not that the badge is
correct.

SAFETY. Hermetic. Every path is a ``tmp_path`` fixture; nothing here
opens a tmux socket.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Tuple

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_oss_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_oss_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from src.config import settings  # noqa: E402
from src.core.session_manager import SessionManager  # noqa: E402
from src.models import Session  # noqa: E402


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> Tuple[Path, Path]:
    """A throwaway (old LOG_DIRECTORY, new state dir) pair, wired live.

    Inputs: tmp_path, monkeypatch (pytest fixtures).
    Output: (log_dir, state_dir).
    """
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    log_dir.mkdir()
    state_dir.mkdir()
    monkeypatch.setattr(settings, "log_directory", str(log_dir))
    monkeypatch.setattr(settings, "state_dir_override", str(state_dir))
    return log_dir, state_dir


def _write_metadata(state_dir: Path, session_id: str, name: str, owned: list) -> Path:
    """Write a v3 metadata payload the way ``_save_session_metadata`` does.

    Inputs: state_dir (Path). session_id (str). name (str) - the persisted
      session's tmux name. owned (list[str]) - the owned-set to persist.
    Output: Path to the file written.
    """
    payload = Session(
        id=session_id,
        working_dir="/tmp/wd",
        tmux_session=name,
        agent_type="claude",
    ).model_dump()
    payload["owned_tmux_sessions"] = sorted(owned)
    path = state_dir / "session_metadata.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def test_clearing_a_stale_pointer_keeps_the_owned_set(dirs):
    """The exact live sequence: load, then clear one un-rehydratable slug.

    ``lifespan_startup`` calls ``_clear_stale_metadata(persisted.id)`` when
    the persisted slug is not in the live tmux listing. The user's OTHER
    three sessions are not implicated by that, and their ownership record
    must outlive it. Today the whole file is unlinked, so it does not.
    """
    _log_dir, state_dir = dirs
    owned = ["cloude_alpha", "cloude_beta", "cloude_gone"]
    _write_metadata(state_dir, "sess-gone", "cloude_gone", owned)

    mgr = SessionManager()
    mgr._load_session_metadata()
    assert mgr.owned_tmux_sessions == set(owned)

    # The persisted slug is not live. This is the ordinary case, and it is
    # the exact call lifespan_startup makes on it.
    mgr._clear_stale_metadata("sess-gone")

    # A FRESH manager is the honest reader: it can only see what survived
    # to disk, which is the thing the next server start actually gets.
    reloaded = SessionManager()
    reloaded._load_session_metadata()

    assert reloaded.owned_tmux_sessions >= {"cloude_alpha", "cloude_beta"}, (
        "clearing ONE session's stale pointer destroyed the ownership "
        "record for every other session; every launcher-created session "
        "reads EXTERNAL from here on"
    )


def test_cleared_metadata_no_longer_rehydrates_the_dead_session(dirs):
    """The clear must still do its actual job.

    The point of ``_clear_stale_metadata`` is that a dead session is not
    silently auto-rehydrated on the next start; it should surface in the
    Adopt list instead. Preserving the owned-set must not quietly bring
    the stale session pointer back with it.
    """
    _log_dir, state_dir = dirs
    _write_metadata(
        state_dir, "sess-gone", "cloude_gone", ["cloude_alpha", "cloude_gone"]
    )

    mgr = SessionManager()
    mgr._load_session_metadata()
    mgr._clear_stale_metadata("sess-gone")

    reloaded = SessionManager()
    reloaded._load_session_metadata()

    assert reloaded.current_session() is None, (
        "the dead session was rehydrated anyway"
    )
