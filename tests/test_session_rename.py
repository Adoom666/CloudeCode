"""v0.7.1 — tests for session rename (PATCH /sessions/{id}/name).

Covers:
  * Happy path — owned session renames; tmux backend method called; in-memory
    state re-keyed (owned_tmux_sessions, pinned_themes, Session.tmux_session).
  * Adopted session rename — owned set NOT mutated (adopted sessions don't
    enter owned_tmux_sessions in the first place).
  * Name validation (400) — disallowed chars (':', '.', '/', '\\', spaces),
    empty, > 64 chars.
  * Uniqueness (409) — collision against active backends + owned-but-detached.
  * 404 — unknown session id.
  * WS broadcast — ``connection_manager.broadcast_to_session`` called with
    a ``session.renamed`` envelope.

Mirrors the test_toast_lifecycle.py / test_themes_endpoint.py shape: bare
SessionManager + a fake TmuxBackend stub so we never touch a real tmux
server. The PATCH route is exercised via TestClient with the auth
dependency overridden.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
# pydantic Settings loader sys.exit(1)s if these are missing. Set safe
# defaults BEFORE any ``src.*`` import.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_rn_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_rn_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes_mod
from src.api.auth import require_auth
from src.api.websocket import connection_manager
from src.core.session_manager import SessionManager
from src.models import Session, SessionStatus


# --------------------------------------------------------------------------- #
# Stub settings + bare SessionManager (no tmux side-effects).                 #
# --------------------------------------------------------------------------- #


class _StubSettings:
    """Just enough of ``Settings`` for SessionManager.__init__ to load."""

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


class _FakeBackend:
    """Stand-in for ``TmuxBackend`` — records the rename call without
    talking to a real tmux server. Mirrors the public surface
    ``SessionManager.rename_session`` and ``_session_info_for`` touch.
    """

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session
        self.rename_calls: list[str] = []
        self.fail_next: bool = False

    async def rename_session(self, new_name: str) -> None:
        self.rename_calls.append(new_name)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated tmux failure")
        self.tmux_session = new_name

    def is_alive(self) -> bool:  # consumed by ``_session_info_for``
        return True


def _bare_manager(monkeypatch, tmp_path: Path) -> SessionManager:
    stub = _StubSettings(
        pin_path=tmp_path / "pinned_themes.json",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


def _register(
    mgr: SessionManager,
    sid: str,
    working_dir: Path,
    tmux_session: str,
    owned: bool = True,
) -> tuple[Session, _FakeBackend]:
    """Wire a Session + fake backend into the manager."""
    sess = Session(
        id=sid,
        pty_pid=None,
        working_dir=str(working_dir),
        status=SessionStatus.RUNNING,
        tmux_session=tmux_session,
    )
    backend = _FakeBackend(tmux_session=tmux_session)
    mgr.sessions[sid] = sess
    mgr.backends[sid] = backend
    mgr._subscribers.setdefault(sid, [])
    mgr._last_session_id = sid
    if owned:
        mgr.owned_tmux_sessions.add(tmux_session)
    return sess, backend


# --------------------------------------------------------------------------- #
# 1. Happy path — SessionManager-level (sync-coroutine, no route).           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_persists_in_session_object(monkeypatch, tmp_path):
    """Manager-level: SUCCESS → backend called, Session.tmux_session updated,
    owned set re-keyed."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    sess, backend = _register(mgr, "ses_a", work, "cloude_old", owned=True)

    info = await mgr.rename_session("ses_a", "newname")

    assert backend.rename_calls == ["newname"]
    assert sess.tmux_session == "newname"
    assert "cloude_old" not in mgr.owned_tmux_sessions
    assert "newname" in mgr.owned_tmux_sessions
    # SessionInfo carries the new name at the top level.
    assert info.tmux_session == "newname"


@pytest.mark.asyncio
async def test_rename_repins_themes(monkeypatch, tmp_path):
    """If the OLD name had an entry in pinned_themes, it's moved under the
    new name (so v0.6.x downgrade still finds the pin)."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "themed"
    work.mkdir()
    _register(mgr, "ses_t", work, "cloude_themed", owned=True)

    # Seed a pin under the old name.
    mgr.pinned_themes["cloude_themed"] = "matrix"
    mgr._save_pinned_themes()

    await mgr.rename_session("ses_t", "themed2")

    assert "cloude_themed" not in mgr.pinned_themes
    assert mgr.pinned_themes.get("themed2") == "matrix"


# --------------------------------------------------------------------------- #
# 2. Adopted session rename — owned set untouched.                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_adopted_external_session(monkeypatch, tmp_path):
    """Adopted sessions aren't in owned_tmux_sessions; renaming one must NOT
    add the new name to the owned set."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "ext"
    work.mkdir()
    _register(
        mgr,
        "adopted:external_a",
        work,
        "external_a",
        owned=False,
    )

    await mgr.rename_session("adopted:external_a", "external_b")

    assert "external_a" not in mgr.owned_tmux_sessions
    assert "external_b" not in mgr.owned_tmux_sessions
    # Session's tmux_session still updates.
    assert mgr.sessions["adopted:external_a"].tmux_session == "external_b"


# --------------------------------------------------------------------------- #
# 3. Uniqueness — collision against another active session.                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_conflict_with_existing_session(monkeypatch, tmp_path):
    """Renaming session A to session B's existing tmux name → FileExistsError."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "a"
    work.mkdir()
    work2 = tmp_path / "b"
    work2.mkdir()
    _register(mgr, "ses_a", work, "name_a", owned=True)
    _, b_backend = _register(mgr, "ses_b", work2, "name_b", owned=True)

    with pytest.raises(FileExistsError):
        await mgr.rename_session("ses_a", "name_b")

    # State unchanged on conflict.
    assert mgr.sessions["ses_a"].tmux_session == "name_a"
    assert b_backend.tmux_session == "name_b"


@pytest.mark.asyncio
async def test_rename_conflict_with_owned_but_detached(monkeypatch, tmp_path):
    """A name in ``owned_tmux_sessions`` but not currently a live backend
    (detached session) still blocks the rename."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "a"
    work.mkdir()
    _register(mgr, "ses_a", work, "name_a", owned=True)
    # Simulate a detached but still-owned name.
    mgr.owned_tmux_sessions.add("ghost_name")

    with pytest.raises(FileExistsError):
        await mgr.rename_session("ses_a", "ghost_name")


# --------------------------------------------------------------------------- #
# 4. Unknown session id.                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_unknown_session_id_raises(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        await mgr.rename_session("does_not_exist", "newname")


# --------------------------------------------------------------------------- #
# 5. Idempotent no-op rename.                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_noop_same_name(monkeypatch, tmp_path):
    """Renaming a session to its existing name is a no-op (no backend call,
    no state mutation, success)."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "p"
    work.mkdir()
    _, backend = _register(mgr, "ses_a", work, "samename", owned=True)

    info = await mgr.rename_session("ses_a", "samename")

    assert backend.rename_calls == []
    assert info.tmux_session == "samename"
    assert "samename" in mgr.owned_tmux_sessions


# --------------------------------------------------------------------------- #
# 6. FastAPI route layer.                                                     #
# --------------------------------------------------------------------------- #


def _build_route_app(monkeypatch, tmp_path):
    """Stand up a FastAPI app wired to a real SessionManager with stub
    sessions. Auth dependency overridden so we hit the route w/o JWT.
    """
    project = tmp_path / "routeproj"
    project.mkdir()
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register(mgr, "ses_route", project, "cloude_route", owned=True)

    app = FastAPI()
    app.state.session_manager = mgr
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True

    return app, mgr


# THE ROUTE'S CONTRACT CHANGED, AND THIS SECTION RECORDS THE NEW ONE.
#
# ``PATCH /sessions/{id}/name`` used to call ``tmux rename-session``.
# That moves ``tmux_name``, which is the field ``(socket, name, epoch)``
# identity is keyed on, so the stored row matched no live listing row,
# was reaped as ``tmux_missing``, and the same live session came back
# through the adopt path as a stranger and got a SECOND row. Measured on
# a live v1.0.4 install: one session, three rows, two of them corpses.
#
# The endpoint now writes ``sessions.title``, a free-form LABEL, and the
# tmux name never moves. Three contracts went away with the tmux call
# and their tests are gone rather than weakened:
#
#   409 CONFLICT   two sessions may now carry the same label, because a
#                  label identifies nothing. There is no name to collide.
#   500 TMUX FAIL  no tmux command runs, so it cannot fail.
# SUPERSEDED 2026-08-26. This block used to list a third departure:
#
#   WS BROADCAST   ``session.renamed`` announced a tmux name change to
#                  every attached tab. Nothing renames, so there is
#                  nothing to announce; the label is read from the
#                  session payload like any other field.
#
# That is WRONG and it is the dangerous direction. The tmux call went; the
# broadcast did not. src/api/routes.py still sends a SessionRenamedMessage on
# every successful label write, client/js/terminal.js still dispatches on it,
# and tests/test_rename_broadcast_surfaces.node.mjs covers the client half.
# What changed is what the frame ANNOUNCES - a label, not a tmux name - and a
# tab that is already attached still has to hear about it or it shows a stale
# name until reload.
#
# This comment nearly cost the message type its life: it was read on
# 2026-08-26 as evidence that WSMessageType.SESSION_RENAMED had no sender and
# could be deleted. The reason it was believed is structural and worth
# naming - the harness in this file has no datastore, so every rename here
# answers 404 and the success path that broadcasts is never reached. There
# was no failing test to contradict the prose. There is now:
# tests/test_session_renamed_broadcast_is_live.py measures the sender.
#
# ``SessionManager.rename_session`` is NOT gone and its tests above still
# pass - an external ``tmux rename-session`` is still possible, and the
# lifecycle reconciler heals it. What is gone is any USER path to it.


def test_a_label_with_spaces_is_no_longer_rejected(monkeypatch, tmp_path):
    """The user asked for this by name: "allow any chars including spaces".

    The old route enforced ``^[A-Za-z0-9_-]{1,64}$`` and answered 400.
    This harness has no datastore, so the write cannot land and the
    honest answer is 404 - but 400 would mean the charset gate is still
    there, and that is what this asserts against.
    """
    app, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": "Media Compression"},
    )
    assert resp.status_code != 400, (
        "a label containing a space must not be refused as malformed"
    )


@pytest.mark.parametrize(
    "label",
    ["has:colon", "has.dot", "has/slash", "has!bang", "client (v2) 50%"],
)
def test_punctuation_is_no_longer_rejected(monkeypatch, tmp_path, label):
    """Every one of these was a 400. None of them reaches tmux any more."""
    app, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/ses_route/name", json={"new_name": label}
    )
    assert resp.status_code != 400


@pytest.mark.parametrize(
    "label",
    ["", "   ", "line\nbreak", "tab\there", "a" * 201],
)
def test_a_label_that_cannot_be_rendered_is_still_a_400(
    monkeypatch, tmp_path, label
):
    """Permissive is not unbounded.

    Empty has nothing to show, a control character breaks every
    single-line surface that renders it, and 200 characters is the
    declared bound. All three are refused BEFORE any read or write.
    """
    app, mgr = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/ses_route/name", json={"new_name": label}
    )
    assert resp.status_code == 400, resp.text
    assert mgr.sessions["ses_route"].tmux_session == "cloude_route"


def test_labelling_never_moves_the_tmux_name(monkeypatch, tmp_path):
    """THE PROPERTY THE WHOLE CHANGE EXISTS FOR.

    Whatever the endpoint answers - success or failure - the tmux name
    the session was created with must be the tmux name it still has.
    That is what makes a rename structurally incapable of splitting one
    session into two rows.
    """
    app, mgr = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": "Something Entirely Different"},
    )

    assert mgr.sessions["ses_route"].tmux_session == "cloude_route"
    assert "cloude_route" in mgr.owned_tmux_sessions
    assert "Something Entirely Different" not in mgr.owned_tmux_sessions


def test_rename_unknown_session_id_404(monkeypatch, tmp_path):
    """A definite negative: no such session to label."""
    app, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/does_not_exist/name",
        json={"new_name": "Whatever"},
    )
    assert resp.status_code == 404
