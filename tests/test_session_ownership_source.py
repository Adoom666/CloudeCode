"""Tests for fix/session-ownership-source - what the TMUX/EXTERNAL badge means.

WHAT THE BADGE MEANS. ``created_by_cloude`` distinguishes a tmux session
THIS APP CREATED (via ``POST /sessions``) from one it merely ADOPTED
(started outside the app and picked up). That is a fact about the
session's ORIGIN, so it must not change when the user opens or closes the
session, and it must survive a server restart.

THE BUG THIS PINS. The client had no server value to read on the
``/sessions`` merge path, so it derived one from the session id: an
``adopted:`` prefix meant adopted. Wrong. After a restart the app
re-attaches to still-running tmux sessions through the adopt path and
mints ``adopted:`` ids for sessions whose NAMES are still in the persisted
``owned_tmux_sessions`` set. The server had the right answer the whole
time; the id did not. Observed live: ``id="adopted:cloude_ses_ec5bf2a3"``
with ``owned_tmux_sessions=["cloude_ses_ec5bf2a3", ...]`` -- owned, badged
EXTERNAL.

The fix: ``SessionInfo.created_by_cloude`` is resolved server-side from
``owned_tmux_sessions`` membership (the same source
``AttachableSession.created_by_cloude`` already used), and the client
reads it rather than deriving anything.

Run with:
    python3 -m pytest tests/test_session_ownership_source.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_own_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_own_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.session_manager import SessionManager
from src.models import Session, SessionInfo, SessionStatus


class _StubSettings:
    """Just enough of ``Settings`` for ``SessionManager.__init__``."""

    def __init__(self, log_dir: Path, port: int = 5001) -> None:
        self._log_dir = log_dir
        self.port = port

    def get_pinned_themes_path(self) -> Path:
        return self._log_dir / "pinned_themes.json"

    def get_unread_state_path(self) -> Path:
        return self._log_dir / "unread_state.json"

    @property
    def log_directory(self) -> str:
        return str(self._log_dir)

    def get_session_metadata_path(self) -> Path:
        return self._log_dir / "session_metadata.json"


class _FakeBackend:
    """Bare enough of a ``SessionBackend`` for tmux-name lookups."""

    def __init__(self, tmux_session: str) -> None:
        self.tmux_session = tmux_session

    def is_alive(self) -> bool:
        return True


def _bare_manager(monkeypatch, tmp_path: Path) -> SessionManager:
    """Build a SessionManager whose on-disk state lives under ``tmp_path``.

    Args:
        monkeypatch: pytest fixture, used to swap the module-level settings.
        tmp_path: directory that becomes the manager's log/metadata root.

    Returns:
        SessionManager: freshly constructed, state loaded from tmp_path.
    """
    (tmp_path / "logs").mkdir(exist_ok=True)
    stub = _StubSettings(log_dir=tmp_path / "logs")
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


def _register(
    mgr: SessionManager, sid: str, tmux_name: str, working_dir: Path
) -> Session:
    """Register a live session + fake backend under ``sid``.

    Args:
        mgr: manager to mutate.
        sid: session id (``adopted:<name>`` for an adopted one).
        tmux_name: the literal tmux session name.
        working_dir: cwd recorded on the Session.

    Returns:
        Session: the registered session object.
    """
    sess = Session(
        id=sid,
        pty_pid=None,
        working_dir=str(working_dir),
        status=SessionStatus.RUNNING,
        tmux_session=tmux_name,
    )
    mgr.sessions[sid] = sess
    mgr.backends[sid] = _FakeBackend(tmux_name)
    mgr._subscribers.setdefault(sid, [])
    return sess


def _stub_status(monkeypatch, mgr: SessionManager, *names: str) -> None:
    """Point ``_build_tmux_status_map`` at a canned idle row per name."""
    monkeypatch.setattr(
        mgr,
        "_build_tmux_status_map",
        lambda: {n: {"status": "idle", "pid": 4242} for n in names},
    )


# ---------------------------------------------------------------------
# 1. created vs adopted
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_created_session_is_reported_owned(monkeypatch, tmp_path):
    """A session whose tmux name is in the owned set reports owned."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register(mgr, "ses_abc", "cloude_ses_abc", tmp_path)
    mgr.owned_tmux_sessions.add("cloude_ses_abc")
    _stub_status(monkeypatch, mgr, "cloude_ses_abc")

    info = await mgr.get_session_info(session_id="ses_abc")
    assert info is not None
    assert info.created_by_cloude is True


@pytest.mark.asyncio
async def test_adopted_session_is_reported_external(monkeypatch, tmp_path):
    """A session absent from the owned set reports external, even though
    it is live, open, and has a backend just like an owned one."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register(mgr, "adopted:handmade", "handmade", tmp_path)
    _stub_status(monkeypatch, mgr, "handmade")

    info = await mgr.get_session_info(session_id="adopted:handmade")
    assert info is not None
    assert info.created_by_cloude is False


# ---------------------------------------------------------------------
# 2. the regression: an OWNED session wearing an `adopted:` id
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owned_session_with_adopted_id_still_reports_owned(
    monkeypatch, tmp_path
):
    """THE BUG. After a restart the app re-attaches through the adopt path,
    so a session it created carries ``adopted:<name>``. The name is the
    durable identity; the id is not. Verbatim from the live incident."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register(mgr, "adopted:cloude_ses_ec5bf2a3", "cloude_ses_ec5bf2a3", tmp_path)
    mgr.owned_tmux_sessions.add("cloude_ses_ec5bf2a3")
    _stub_status(monkeypatch, mgr, "cloude_ses_ec5bf2a3")

    info = await mgr.get_session_info(session_id="adopted:cloude_ses_ec5bf2a3")
    assert info is not None
    assert info.created_by_cloude is True, (
        "an `adopted:` id prefix is not evidence of adoption - it is what a "
        "restart-time re-attach mints for a session we still own"
    )


@pytest.mark.asyncio
async def test_external_session_with_plain_id_still_reports_external(
    monkeypatch, tmp_path
):
    """The mirror of the case above: a plain id is not evidence of ownership
    either. Only owned-set membership is."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register(mgr, "some-plain-id", "not_ours", tmp_path)
    _stub_status(monkeypatch, mgr, "not_ours")

    info = await mgr.get_session_info(session_id="some-plain-id")
    assert info is not None
    assert info.created_by_cloude is False


@pytest.mark.asyncio
async def test_adopting_an_owned_session_does_not_disown_it(monkeypatch, tmp_path):
    """``adopt_external_session`` neither adds to nor removes from the owned
    set. Re-adopting one of our own (what a restart does) leaves it owned."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    mgr.owned_tmux_sessions.add("cloude_ses_abc")

    # Session re-registered under the adopt path's id shape.
    _register(mgr, "adopted:cloude_ses_abc", "cloude_ses_abc", tmp_path)
    _stub_status(monkeypatch, mgr, "cloude_ses_abc")

    info = await mgr.get_session_info(session_id="adopted:cloude_ses_abc")
    assert info.created_by_cloude is True
    assert "cloude_ses_abc" in mgr.owned_tmux_sessions


# ---------------------------------------------------------------------
# 3. survives a restart, and is pruned when a session genuinely dies
# ---------------------------------------------------------------------


def test_owned_set_survives_a_restart(monkeypatch, tmp_path):
    """Persist the owned set, throw the manager away, build a new one over
    the same metadata file. This is the restart the badge has to survive."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register(mgr, "ses_abc", "cloude_ses_abc", tmp_path)
    mgr.owned_tmux_sessions.update({"cloude_ses_abc", "cloude_test pause"})
    mgr._save_session_metadata()

    on_disk = json.loads(
        (tmp_path / "logs" / "session_metadata.json").read_text()
    )
    assert on_disk["owned_tmux_sessions"] == [
        "cloude_ses_abc",
        "cloude_test pause",
    ]

    reborn = _bare_manager(monkeypatch, tmp_path)
    assert reborn.owned_tmux_sessions == {"cloude_ses_abc", "cloude_test pause"}


@pytest.mark.asyncio
async def test_badge_unchanged_across_a_restart(monkeypatch, tmp_path):
    """End to end: same tmux session, same badge, before and after a restart
    that re-attaches it through the adopt path (so the id changes shape)."""
    before = _bare_manager(monkeypatch, tmp_path)
    _register(before, "ses_abc", "cloude_ses_abc", tmp_path)
    before.owned_tmux_sessions.add("cloude_ses_abc")
    _stub_status(monkeypatch, before, "cloude_ses_abc")
    before._save_session_metadata()
    pre = await before.get_session_info(session_id="ses_abc")

    after = _bare_manager(monkeypatch, tmp_path)
    _register(after, "adopted:cloude_ses_abc", "cloude_ses_abc", tmp_path)
    _stub_status(monkeypatch, after, "cloude_ses_abc")
    post = await after.get_session_info(session_id="adopted:cloude_ses_abc")

    assert pre.created_by_cloude is True
    assert post.created_by_cloude == pre.created_by_cloude


@pytest.mark.asyncio
async def test_owned_set_is_pruned_when_a_session_dies(monkeypatch, tmp_path):
    """The startup reconciler drops owned names tmux no longer lists, so a
    dead session cannot leave a permanent ownership record behind."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register(mgr, "ses_alive", "cloude_alive", tmp_path)
    mgr.owned_tmux_sessions.update({"cloude_alive", "cloude_dead"})
    mgr._save_session_metadata()

    reborn = _bare_manager(monkeypatch, tmp_path)
    assert reborn.owned_tmux_sessions == {"cloude_alive", "cloude_dead"}

    class _Probe:
        tmux_session = "cloude_alive"

        def discover_existing(self):
            return ["cloude_alive"]

        async def attach_existing(self):
            return None

    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _Probe(),
    )
    monkeypatch.setattr(reborn, "_sweep_orphan_uploads", _noop_async)
    await reborn.lifespan_startup()

    assert reborn.owned_tmux_sessions == {"cloude_alive"}


async def _noop_async(*_args, **_kwargs) -> None:
    """No-op stand-in for an async method we do not want firing in a test."""
    return None


# ---------------------------------------------------------------------
# 4. the wire contract itself
# ---------------------------------------------------------------------


def test_session_info_defaults_to_not_owned(tmp_path):
    """Absence of evidence is not ownership. Any synthesized SessionInfo
    (e.g. routes.py's pinned-theme echo) defaults to external rather than
    claiming an origin nobody measured."""
    info = SessionInfo(
        session=Session(id="pinned:x", working_dir=str(tmp_path)),
    )
    assert info.created_by_cloude is False
