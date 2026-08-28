"""Server-side supersession of unacked ``Stop`` toasts.

The defect: ``SessionManager.record_toast`` appended unconditionally, so a
session that ran twelve assistant turns without the user dismissing
anything held twelve ``Stop`` records saying the identical thing ("your
turn"), all of them replayed on the next attach backfill. The unacked
half of the per-session list has no cap at all - ``_prune_toasts`` bounds
only the acked tail - so that list grew for the life of the session.

The fix: when a session already holds an UNACKED ``Stop`` with the same
title, the new event REPLACES that record IN PLACE, keeping its id.

These tests assert on stored state (``get_toasts``), never on a call
having happened.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ts_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ts_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.session_manager import SessionManager
from src.models import Session, SessionStatus


class _StubSettings:
    """Just enough of ``Settings`` for ``SessionManager.__init__``."""

    def __init__(self, pin_path: Path, log_dir: Path):
        self._pin_path = pin_path
        self._log_dir = log_dir

    def get_pinned_themes_path(self) -> Path:
        return self._pin_path

    def get_unread_state_path(self) -> Path:
        return self._pin_path.parent / "unread_state.json"

    @property
    def log_directory(self) -> str:
        return str(self._log_dir)

    def get_session_metadata_path(self) -> Path:
        return self._log_dir / "session_metadata.json"


@pytest.fixture()
def mgr(monkeypatch, tmp_path) -> SessionManager:
    """A SessionManager with no tmux side effects and one live session."""
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr(
        "src.core.session_manager.settings",
        _StubSettings(tmp_path / "pinned_themes.json", tmp_path / "logs"),
    )
    manager = SessionManager()
    work = tmp_path / "proj"
    work.mkdir()
    for sid in ("ses_a", "ses_b"):
        manager.sessions[sid] = Session(
            id=sid,
            pty_pid=None,
            working_dir=str(work),
            status=SessionStatus.RUNNING,
            tmux_session=None,
        )
        manager._subscribers.setdefault(sid, [])
    return manager


# --- the decisive one ------------------------------------------------------


def test_twelve_unacked_stops_leave_exactly_one_record(mgr):
    """Twelve turns, nothing dismissed -> the server holds ONE Stop."""
    for i in range(12):
        mgr.record_toast("ses_a", "Stop", "Your turn", body=f"turn {i}")

    stored = mgr.get_toasts("ses_a")
    assert len(stored) == 1, f"expected 1 record, got {len(stored)}"
    assert stored[0].kind == "Stop"


def test_superseding_keeps_the_original_id(mgr):
    """The id is STABLE across supersession.

    The client dedupes by id and a coalesced card acks every member id on
    dismiss. Minting a new id per turn would leave the client holding ids
    the server no longer has (they ack into nothing) while the server
    holds an id the client never saw (it returns on the next backfill).
    """
    first = mgr.record_toast("ses_a", "Stop", "Your turn", body="one")
    later = mgr.record_toast("ses_a", "Stop", "Your turn", body="two")

    assert later.id == first.id
    assert [t.id for t in mgr.get_toasts("ses_a")] == [first.id]


def test_surviving_record_carries_the_newest_content(mgr):
    mgr.record_toast("ses_a", "Stop", "Your turn", body="oldest")
    mgr.record_toast("ses_a", "Stop", "Your turn", body="middle")
    mgr.record_toast("ses_a", "Stop", "Your turn", body="newest")

    stored = mgr.get_toasts("ses_a")
    assert len(stored) == 1
    assert stored[0].body == "newest"


def test_superseded_record_timestamp_advances(mgr):
    first = mgr.record_toast("ses_a", "Stop", "Your turn", body="one")
    stamp_before = first.created_at
    later = mgr.record_toast("ses_a", "Stop", "Your turn", body="two")
    assert later.created_at >= stamp_before


# --- what must NOT be superseded -------------------------------------------


def test_acked_stop_is_never_superseded(mgr):
    """Superseding an acked record would resurrect a dismissed toast."""
    first = mgr.record_toast("ses_a", "Stop", "Your turn", body="dismissed")
    assert mgr.ack_toast("ses_a", first.id) is True

    second = mgr.record_toast("ses_a", "Stop", "Your turn", body="fresh")

    stored = mgr.get_toasts("ses_a")
    assert len(stored) == 2
    assert second.id != first.id
    acked = [t for t in stored if t.id == first.id]
    assert len(acked) == 1
    assert acked[0].acknowledged is True
    assert acked[0].body == "dismissed", "the acked record was mutated"
    assert mgr.get_toasts("ses_a", unacked_only=True)[0].body == "fresh"


def test_notification_is_never_superseded(mgr):
    """Each Notification body is a distinct thing to read."""
    for i in range(4):
        mgr.record_toast("ses_a", "Notification", "Waiting", body=f"msg {i}")
    assert len(mgr.get_toasts("ses_a")) == 4


def test_identical_notifications_are_never_superseded(mgr):
    """Even byte-identical Notifications stay distinct records."""
    for _ in range(3):
        mgr.record_toast("ses_a", "Notification", "Waiting", body="same")
    assert len(mgr.get_toasts("ses_a")) == 3


def test_permission_request_is_never_superseded(mgr):
    """Each is a distinct decision about a distinct command."""
    for cmd in ("rm -rf /tmp/x", "curl example.com", "git push"):
        mgr.record_toast("ses_a", "PermissionRequest", "Allow?", body=cmd)
    stored = mgr.get_toasts("ses_a")
    assert len(stored) == 3
    assert {t.body for t in stored} == {
        "rm -rf /tmp/x",
        "curl example.com",
        "git push",
    }


def test_stop_with_a_different_title_does_not_supersede(mgr):
    """Supersession keys on title, exactly like the client coalesce key."""
    mgr.record_toast("ses_a", "Stop", "Your turn", body="a")
    mgr.record_toast("ses_a", "Stop", "Something else", body="b")
    assert len(mgr.get_toasts("ses_a")) == 2


def test_supersession_does_not_cross_sessions(mgr):
    mgr.record_toast("ses_a", "Stop", "Your turn", body="a1")
    mgr.record_toast("ses_b", "Stop", "Your turn", body="b1")
    mgr.record_toast("ses_a", "Stop", "Your turn", body="a2")

    assert len(mgr.get_toasts("ses_a")) == 1
    assert len(mgr.get_toasts("ses_b")) == 1
    assert mgr.get_toasts("ses_a")[0].body == "a2"
    assert mgr.get_toasts("ses_b")[0].body == "b1"


# --- ordering + growth -----------------------------------------------------


def test_superseded_stop_moves_to_the_front(mgr):
    """Newest-first ordering survives supersession."""
    mgr.record_toast("ses_a", "Stop", "Your turn", body="stop-1")
    mgr.record_toast("ses_a", "Notification", "Waiting", body="note")
    mgr.record_toast("ses_a", "Stop", "Your turn", body="stop-2")

    stored = mgr.get_toasts("ses_a")
    assert [t.kind for t in stored] == ["Stop", "Notification"]
    assert stored[0].body == "stop-2"


def test_long_session_unacked_stops_stay_bounded(mgr):
    """200 turns used to be 200 records. The unacked list has no cap."""
    for i in range(200):
        mgr.record_toast("ses_a", "Stop", "Your turn", body=f"t{i}")
    assert len(mgr.get_toasts("ses_a", unacked_only=True)) == 1
