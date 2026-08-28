"""``session.renamed`` is a LIVE message type. This is the proof.

It was reported on 2026-08-26 as a WSMessageType in src/models.py with no
sender, and therefore as dead code to delete. Measured, it has all three
halves:

  * SENDER  - src/api/routes.py, PATCH /sessions/{session_id}/name, calls
    connection_manager.broadcast_to_session with a SessionRenamedMessage on
    every successful label write.
  * CLIENT  - client/js/terminal.js dispatches on 'session.renamed' and
    updates the header and document.title.
  * COVERAGE for the client half - tests/test_rename_broadcast_surfaces.mjs.

The sender half had NO test, which is how it came to look dead. The route's
own test file could not supply one: its harness has no datastore, so every
rename in it answers 404 and the success path - the only path that
broadcasts - is never reached. That file also still carries a comment
asserting the broadcast "went away" when the endpoint stopped calling tmux
rename-session. The tmux call went; the broadcast did not, because the label
still changes and attached tabs still have to hear about it.

So this file exists to make the sender half fail loudly if anyone acts on
that comment. Deleting WSMessageType.SESSION_RENAMED, or dropping the
broadcast from the route, breaks it.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ren_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ren_logs_"))
os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-a-real-one-32b")

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes_mod
from src.api.auth import require_auth
from src.models import WSMessageType


class _RecordingConnectionManager:
    """Stands in for the real connection manager and keeps every frame.

    Inputs:  none
    Outputs: instance whose ``sent`` list holds (session_id, payload) pairs.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def broadcast_to_session(self, session_id: str, payload: str) -> None:
        """Record one broadcast.

        Inputs:  session_id (str), payload (str) - the JSON frame.
        Outputs: None.
        """
        self.sent.append((session_id, payload))


class _LabelAcceptingManager:
    """A session manager whose label write always succeeds.

    Inputs:  none
    Outputs: instance.

    ``get_session_info`` returns None so the route's response fails its own
    response_model validation. That is deliberate and harmless here: the
    broadcast happens inside the handler, before serialization, so this
    isolates the question to "did the frame go out" without needing a whole
    datastore to build a valid SessionInfo.
    """

    def set_session_label(self, session_id: str, new_name: str) -> bool:
        return True

    async def get_session_info(self, session_id: str | None = None):
        return None


@pytest.fixture()
def recording_app(monkeypatch):
    """A route app with the connection manager replaced by a recorder.

    Inputs:  monkeypatch (pytest fixture).
    Outputs: yields (TestClient, _RecordingConnectionManager).
    """
    recorder = _RecordingConnectionManager()
    monkeypatch.setattr(routes_mod, "connection_manager", recorder)

    app = FastAPI()
    app.state.session_manager = _LabelAcceptingManager()
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True

    # Server exceptions are absorbed rather than raised so the deliberate
    # response_model failure above cannot mask the assertion below.
    yield TestClient(app, raise_server_exceptions=False), recorder


def test_a_successful_rename_broadcasts_session_renamed(recording_app) -> None:
    """The route emits exactly one session.renamed frame, correctly shaped.

    Inputs:  recording_app fixture.
    Outputs: None.
    """
    client, recorder = recording_app

    client.patch(
        "/api/v1/sessions/ses_live/name",
        json={"new_name": "Media Compression"},
    )

    assert len(recorder.sent) == 1, (
        "PATCH /sessions/{id}/name broadcast %d frames, expected exactly 1. "
        "Zero means session.renamed has no sender any more and every "
        "attached tab now shows a stale name until it reloads."
        % len(recorder.sent)
    )

    session_id, payload = recorder.sent[0]
    assert session_id == "ses_live"
    frame = json.loads(payload)
    assert frame["type"] == WSMessageType.SESSION_RENAMED.value
    assert frame["type"] == "session.renamed", (
        "the wire string changed. client/js/terminal.js dispatches on the "
        "literal 'session.renamed' and would silently stop handling it."
    )
    assert frame["session_id"] == "ses_live"
    assert frame["new_name"] == "Media Compression", (
        "the frame carries a different label than the one written, so tabs "
        "would update to the wrong name"
    )


def test_a_rejected_rename_broadcasts_nothing(recording_app) -> None:
    """A 400 must not announce a rename that did not happen.

    Inputs:  recording_app fixture.
    Outputs: None.

    The negative control. Without it, a route that broadcast unconditionally
    would satisfy the test above just as well as a correct one.
    """
    client, recorder = recording_app

    response = client.patch(
        "/api/v1/sessions/ses_live/name",
        json={"new_name": "has\na newline"},
    )

    assert response.status_code == 400, (
        "a control character in a label is supposed to be refused; if this "
        "changed, the assertion below is measuring the wrong path"
    )
    assert recorder.sent == [], (
        "a refused rename still broadcast session.renamed, so every tab "
        "would show a name the server did not store"
    )
