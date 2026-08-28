"""The attribution prompt must answer from LIVE rows, not a snapshot.

THE BUG THESE TESTS WERE WRITTEN FOR. The user clicked "adopt all" and
the card did not go away. Measured on his live datastore: all five names
in ``meta.session_import_unattributed`` had ``origin='adopted'`` with an
``adopted_at`` stamped at the second he clicked. The adopts LANDED. The
prompt kept rendering because it read the stored snapshot and never
asked what those rows say NOW.

The decline path did not have this defect - it prunes the snapshot in
the same transaction it writes ``user_declined_at``. That asymmetry is
exactly why the bug looked intermittent: one of the two answers cleared
the card and the other did not.

THE FIX IS NOT "PRUNE ON THE ADOPT PATH TOO". Two sources of truth kept
in sync by hand drift, and the drift is invisible - it requires every
future mutation path to remember. The snapshot is the CANDIDATE SET; the
answer is derived from ``sessions`` every time it is asked.

WHAT MUST NOT BE PRUNED, and why each one is here: a candidate we cannot
cross-reference (no epoch, no rows table) is a question we have not
answered, and a candidate with NO ROW AT ALL is still adoptable, since
the adopt path inserts its own sighting. Dropping either would trade
this false-positive card for a silently-vanished question, which is the
same defect pointed the other way.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp())
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp())
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, set_meta, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    META_SESSION_IMPORT_UNATTRIBUTED,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_identity import record_instance
from src.core.session_import_promote import record_decline

SOCKET = "cloude"
STAMP = "2026-08-24T13:32:31Z"


class _Manager:
    """The one SessionManager method these routes read."""

    def tmux_socket_name(self):
        """The socket the prompt cross-references rows against."""
        return SOCKET


def _client(tmp_path, monkeypatch):
    """A TestClient whose state dir is tmp_path.

    Inputs: tmp_path (Path), monkeypatch (pytest fixture).
    Output: TestClient.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api import routes
    from src.api.auth import require_auth
    from src.api.routes import router

    monkeypatch.setattr(
        type(routes.settings), "get_state_dir", lambda self: tmp_path
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.session_manager = _Manager()
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app)


def _record(name, epoch):
    """One ``session_import_unattributed`` record, in its stored shape."""
    return {
        "tmux_name": name,
        "epoch": epoch,
        "hints": ["its name matches the auto-generated form"],
        "reason": "no_admissible_evidence",
    }


def _seed(tmp_path, records, rows=()):
    """Create a migrated db holding a snapshot and some session rows.

    Inputs: tmp_path (Path). records (list | None) - the snapshot. rows
      (iterable of (name, epoch, origin)).
    Output: None.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            for name, epoch, origin in rows:
                record_instance(
                    conn,
                    socket=SOCKET,
                    name=name,
                    epoch=epoch,
                    origin=origin,
                    now=STAMP,
                )
            if records is not None:
                set_meta(
                    conn,
                    META_SESSION_IMPORT_UNATTRIBUTED,
                    json.dumps(records, sort_keys=True),
                )


def _prompt(client):
    """GET the prompt and return (state, [tmux_name, ...])."""
    body = client.get("/api/v1/sessions/attribution-prompt").json()
    return body["state"], [s["tmux_name"] for s in body.get("sessions") or []]


def test_adopting_every_candidate_empties_the_prompt(tmp_path, monkeypatch):
    """THE REPORTED BUG. Five adopted rows, one stale snapshot, no card.

    This is the exact shape measured on the user's datastore on
    2026-08-24: every snapshot name at origin='adopted', the snapshot
    itself untouched.
    """
    names = [
        "cloude_Test",
        "cloude_asd",
        "cloude_claude-config-sync-2",
        "cloude_scrolltest",
        "cloude_test pause",
    ]
    records = [_record(n, 1755000000 + i) for i, n in enumerate(names)]
    rows = [
        (n, 1755000000 + i, SESSION_ORIGIN_ADOPTED) for i, n in enumerate(names)
    ]
    _seed(tmp_path, records, rows)
    state, shown = _prompt(_client(tmp_path, monkeypatch))
    assert shown == []
    assert state == "none"


def test_adopting_some_leaves_only_the_rest(tmp_path, monkeypatch):
    """"Adopt the ticked ones" must shrink the card, not clear it."""
    records = [_record("cloude_a", 1), _record("cloude_b", 2)]
    _seed(
        tmp_path,
        records,
        [
            ("cloude_a", 1, SESSION_ORIGIN_ADOPTED),
            ("cloude_b", 2, SESSION_ORIGIN_OBSERVED),
        ],
    )
    state, shown = _prompt(_client(tmp_path, monkeypatch))
    assert shown == ["cloude_b"]
    assert state == "pending"


def test_a_row_proved_ours_by_a_rerun_also_leaves_the_prompt(
    tmp_path, monkeypatch
):
    """Stage D can promote a row to 'created' without the user answering.

    The snapshot cannot know that happened either, so the same derivation
    has to cover it - otherwise the card asks about a session the app has
    since proved it started itself.
    """
    _seed(
        tmp_path,
        [_record("cloude_a", 1)],
        [("cloude_a", 1, SESSION_ORIGIN_CREATED)],
    )
    assert _prompt(_client(tmp_path, monkeypatch)) == ("none", [])


def test_a_declined_row_is_gone_even_if_the_snapshot_still_lists_it(
    tmp_path, monkeypatch
):
    """Belt and braces on the path that already prunes.

    The decline route prunes the snapshot itself, so this can only
    happen if that write is interrupted - but a half-applied decline
    must not resurrect the question either.
    """
    _seed(
        tmp_path,
        [_record("cloude_a", 1)],
        [("cloude_a", 1, SESSION_ORIGIN_OBSERVED)],
    )
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            record_decline(
                conn, socket=SOCKET, name="cloude_a", epoch=1, now=STAMP
            )
    assert _prompt(_client(tmp_path, monkeypatch)) == ("none", [])


def test_an_unanswered_observed_row_still_asks(tmp_path, monkeypatch):
    """The control. Filtering must not empty a prompt that IS pending."""
    _seed(
        tmp_path,
        [_record("cloude_a", 1)],
        [("cloude_a", 1, SESSION_ORIGIN_OBSERVED)],
    )
    assert _prompt(_client(tmp_path, monkeypatch)) == ("pending", ["cloude_a"])


def test_a_candidate_with_no_row_is_kept_not_silently_dropped(
    tmp_path, monkeypatch
):
    """No row is NOT proof the question is answered.

    The adopt path records its own sighting before claiming it, so a
    candidate without a row is still adoptable. Pruning it would trade a
    card that will not clear for a question that vanished unanswered.
    """
    _seed(tmp_path, [_record("cloude_ghost", 1)], [])
    assert _prompt(_client(tmp_path, monkeypatch)) == (
        "pending",
        ["cloude_ghost"],
    )


def test_a_candidate_with_no_epoch_is_kept_not_silently_dropped(
    tmp_path, monkeypatch
):
    """CANNOT DETERMINE. Rows are keyed on the instance triple, so a
    record with no epoch cannot be matched against one either way."""
    _seed(
        tmp_path,
        [_record("cloude_a", None)],
        [("cloude_a", 1, SESSION_ORIGIN_ADOPTED)],
    )
    assert _prompt(_client(tmp_path, monkeypatch)) == ("pending", ["cloude_a"])


def test_a_row_on_another_socket_does_not_answer_for_ours(
    tmp_path, monkeypatch
):
    """The cross-reference is keyed on the socket the manager reports."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            record_instance(
                conn,
                socket="other",
                name="cloude_a",
                epoch=1,
                origin=SESSION_ORIGIN_ADOPTED,
                now=STAMP,
            )
            set_meta(
                conn,
                META_SESSION_IMPORT_UNATTRIBUTED,
                json.dumps([_record("cloude_a", 1)], sort_keys=True),
            )
    assert _prompt(_client(tmp_path, monkeypatch)) == ("pending", ["cloude_a"])
