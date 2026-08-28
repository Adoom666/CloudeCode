"""THE WIRING: the reconciler unit is worthless if nothing calls it.

Three seams, each of which has silently broken a guarantee in this repo
before. The real ``SessionManager.list_attachable_sessions`` path must
reach the datastore on a healthy probe and must NOT on a failed one; the
manager rewraps the backend's listing and must carry ``refused_rows``
through, or the completeness gate is disarmed from above; and the backend
must actually COUNT the rows its parser refuses, or the signal never
exists in the first place.

Run with:
    ./venv/bin/python3 -m pytest tests/test_session_lifecycle_wiring.py -v
"""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

import pytest

from tests.lifecycle_helpers import (
    ENTRY_FUNCTION,
    MODULE_PATH,
    ROOT,
    SOCKET,
    WRITER_FUNCTION,
    CountingConnection,
    ExplodingConnection,
    add_row,
    conn,
    function_named,
    live,
    module_ast,
    row_by_uuid,
    sql_literals,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402,F401

import tempfile

from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_SOURCE_IMPORT,
    SESSION_LIFECYCLE_STOPPED,
)
from src.core.session_lifecycle import RECONCILE_NO_TABLE
from src.core.session_store import list_sessions
from src.core.tmux_listing import REASON_TIMEOUT, TmuxListing


# ===========================================================================
# PART 6 - THE WIRING
# the unit above is worthless if nothing calls it. These prove the real
# SessionManager.list_attachable_sessions path reaches the datastore.
# ===========================================================================


class _StubSettings:
    """Just enough of ``Settings`` for ``SessionManager.__init__``.

    Inputs (constructor): root (Path) - throwaway state directory.
    Output: an object SessionManager can read like settings.
    """

    def __init__(self, root: Path):
        self._root = root
        self.port = 5001

    def get_pinned_themes_path(self) -> Path:
        """Inputs: none. Output: Path."""
        return self._root / "pinned_themes.json"

    def get_unread_state_path(self) -> Path:
        """Inputs: none. Output: Path."""
        return self._root / "unread_state.json"

    def get_session_metadata_path(self) -> Path:
        """Inputs: none. Output: Path."""
        return self._root / "session_metadata.json"

    def get_log_directory(self) -> Path:
        """Inputs: none. Output: Path."""
        return self._root / "logs"

    def get_state_dir(self) -> Path:
        """Inputs: none. Output: Path."""
        return self._root

    @property
    def log_directory(self) -> str:
        """Inputs: none. Output: str."""
        return str(self._root / "logs")

    @property
    def default_working_dir(self) -> str:
        """Inputs: none. Output: str."""
        return str(self._root)

    @property
    def log_buffer_size(self) -> int:
        """Inputs: none. Output: int."""
        return 100

    @property
    def agents(self):
        """Inputs: none. Output: None."""
        return None


class _ProbeBackend:
    """Stub probe backend returning a canned listing.

    Inputs (constructor): listing (TmuxListing) - what the probe answers.
    Output: an object with the two methods the manager calls.
    """

    def __init__(self, listing):
        self._listing = listing
        self.socket_name = SOCKET

    def list_attachable_sessions(self, owned_names=None, owned_instances=None):
        """Inputs: owned_names, owned_instances (ignored). Output: TmuxListing."""
        return self._listing

    def list_pane_status_all(self):
        """Inputs: none. Output: TmuxListing."""
        return TmuxListing.answered([])


def _wired(monkeypatch, tmp_path, listing):
    """Build a SessionManager over a migrated datastore and a canned probe.

    Inputs: monkeypatch, tmp_path (Path), listing (TmuxListing).
    Output: SessionManager.
    """
    from src.core.session_manager import SessionManager

    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr(
        "src.core.session_manager.settings", _StubSettings(tmp_path)
    )
    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend(listing),
    )
    mgr = SessionManager()
    monkeypatch.setattr(
        mgr, "_detect_agent_type_from_pane", lambda *, socket, name: None
    )
    return mgr


def test_the_home_screen_listing_actually_reaps(monkeypatch, tmp_path):
    """One call to the REAL listing method must move the dead row."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as c:
        add_row(c, uuid="u-dead", name="cloude_gone", epoch=900)
        add_row(c, uuid="u-live", name="cloude_a", epoch=1000)
        c.commit()

    mgr = _wired(
        monkeypatch,
        tmp_path,
        TmuxListing.answered(
            [{"name": "cloude_a", "created_at_epoch": 1000, "window_count": 1}]
        ),
    )
    result = mgr.list_attachable_sessions()
    assert result.ok is True

    with closing(connect(db_path_for(tmp_path), create=False)) as c:
        assert row_by_uuid(c, "u-dead")["lifecycle"] == SESSION_LIFECYCLE_STOPPED
        assert row_by_uuid(c, "u-live")["lifecycle"] == SESSION_LIFECYCLE_RUNNING


def test_the_home_screen_listing_does_not_reap_on_a_failed_probe(
    monkeypatch, tmp_path
):
    """THE WIRING'S MOST IMPORTANT TEST. A failed poll must change nothing."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as c:
        add_row(c, uuid="u-live", name="cloude_a", epoch=1000)
        c.commit()

    mgr = _wired(
        monkeypatch, tmp_path, TmuxListing.unavailable(REASON_TIMEOUT)
    )
    result = mgr.list_attachable_sessions()
    assert result.ok is False
    assert mgr.last_probe_health().ok is False

    with closing(connect(db_path_for(tmp_path), create=False)) as c:
        row = row_by_uuid(c, "u-live")
    assert row["lifecycle"] == SESSION_LIFECYCLE_RUNNING
    assert row["lifecycle_source"] == SESSION_LIFECYCLE_SOURCE_IMPORT


def test_the_listing_rewrap_preserves_the_refused_row_count(
    monkeypatch, tmp_path
):
    """The manager rewraps the backend's listing; completeness must survive.

    Dropping ``refused_rows`` here would hand every downstream
    absence-based caller a partial list that claims to be whole - and
    would silently disarm the incomplete-listing refusal.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as c:
        add_row(c, uuid="u-live", name="cloude_a", epoch=1000)
        c.commit()

    mgr = _wired(
        monkeypatch,
        tmp_path,
        TmuxListing.answered(
            [{"name": "cloude_b", "created_at_epoch": 2000, "window_count": 1}],
            refused_rows=1,
        ),
    )
    result = mgr.list_attachable_sessions()
    assert result.ok is True
    assert result.refused_rows == 1
    assert result.complete is False

    with closing(connect(db_path_for(tmp_path), create=False)) as c:
        assert row_by_uuid(c, "u-live")["lifecycle"] == SESSION_LIFECYCLE_RUNNING


def test_a_missing_datastore_never_breaks_the_listing(monkeypatch, tmp_path):
    """No cloude.db is a reason to skip, never a reason to fail the launcher."""
    mgr = _wired(
        monkeypatch,
        tmp_path,
        TmuxListing.answered(
            [{"name": "cloude_a", "created_at_epoch": 1000, "window_count": 1}]
        ),
    )
    result = mgr.list_attachable_sessions()
    assert result.ok is True
    assert result.sessions[0]["name"] == "cloude_a"
    outcome = mgr.reconcile_lifecycle(result)
    assert outcome.evaluated is False
    assert outcome.outcome == RECONCILE_NO_TABLE


def test_a_reap_is_committed_and_survives_reopening(monkeypatch, tmp_path):
    """An uncommitted reap is not a reap. Asserted on a fresh connection."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as c:
        add_row(c, uuid="u-dead", name="cloude_gone", epoch=900)
        c.commit()

    mgr = _wired(monkeypatch, tmp_path, TmuxListing.answered([]))
    mgr.list_attachable_sessions()

    with closing(connect(db_path_for(tmp_path), create=False)) as c:
        recent = list_sessions(
            c, lifecycle=SESSION_LIFECYCLE_STOPPED, include_archived=False
        )
    assert [r["session_uuid"] for r in recent] == ["u-dead"]


# ===========================================================================
# PART 7 - the source of the completeness signal
# ``refused_rows`` is only trustworthy if the backend actually counts.
# ===========================================================================


def test_the_backend_counts_the_rows_its_parser_refuses(monkeypatch):
    """A malformed tmux row must make the listing ok BUT NOT complete.

    Without this count, one unparseable row yields ``ok=True`` with a
    LIVE session silently missing, and the reaper would stop a session
    that is running. The refusal is already logged; logging is not a
    signal any caller can act on.
    """
    from src.core.tmux_backend import TmuxBackend

    backend = TmuxBackend.for_external(
        session_name="cloude_pytest_refused",
        working_dir=Path(tempfile.mkdtemp(prefix="cc_rec_be_")),
        socket_name="cloude_pytest_refused",
    )
    stdout = (
        "$0|1786913176|1|cloude_good\n"
        "this row is not the listing format at all\n"
        "$1|notanepoch|1|cloude_bad\n"
    )
    monkeypatch.setattr(
        backend, "_run_listing", lambda *a: (None, stdout)
    )

    listing = backend.list_attachable_sessions(owned_names=set())

    assert listing.ok is True
    assert [r["name"] for r in listing.sessions] == ["cloude_good"]
    assert listing.refused_rows == 2
    assert listing.complete is False, (
        "two rows were refused, so this listing cannot support an "
        "argument from absence"
    )


def test_a_clean_backend_listing_is_complete(monkeypatch):
    """The positive half, so the guard above is not vacuously strict."""
    from src.core.tmux_backend import TmuxBackend

    backend = TmuxBackend.for_external(
        session_name="cloude_pytest_clean",
        working_dir=Path(tempfile.mkdtemp(prefix="cc_rec_be2_")),
        socket_name="cloude_pytest_clean",
    )
    monkeypatch.setattr(
        backend,
        "_run_listing",
        lambda *a: (None, "$0|1786913176|1|cloude_good\n"),
    )
    listing = backend.list_attachable_sessions(owned_names=set())
    assert listing.refused_rows == 0
    assert listing.complete is True



def test_the_datastore_connection_is_autocommit(tmp_path):
    """Pins the assumption that makes the reap durable without a commit.

    ``src.core.db.connect`` opens with ``isolation_level=None``, so every
    UPDATE is committed as it executes. That is WHY the manager's
    ``conn.commit()`` is provably redundant and why removing it is an
    equivalent mutant rather than a bug. If this ever changes, the
    equivalence claim is wrong and this test says so before anyone
    trusts it again.
    """
    conn = connect(db_path_for(tmp_path))
    try:
        assert conn.isolation_level is None
        conn.execute("CREATE TABLE pin (x INTEGER)")
        conn.execute("INSERT INTO pin VALUES (1)")
    finally:
        conn.close()
    with closing(connect(db_path_for(tmp_path), create=False)) as reopened:
        assert reopened.execute("SELECT count(*) FROM pin").fetchone()[0] == 1
