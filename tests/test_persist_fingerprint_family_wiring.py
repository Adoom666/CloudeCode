"""The wiring, not the write rules - covered separately in
``tests/test_persist_fingerprint_family.py``.

That file proves ``session_agent_provenance.persist_fingerprint_family``
gets the no-overwrite / idempotent / swallow-errors rules right in
isolation. This file proves ``SessionManager._persist_fingerprint_family``
- the method ``adopt_external_session`` actually calls - opens the real
datastore connection, runs it inside a real transaction, commits, and
never raises when there is no datastore to write to at all.

Run with:
    ./venv/bin/python -m pytest tests/test_persist_fingerprint_family_wiring.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_pfw_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_pfw_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    SESSION_FAMILY_SOURCE_FINGERPRINT,
    SESSION_FAMILY_SOURCE_LAUNCHED,
    SESSION_LIFECYCLE_RUNNING,
    SESSION_ORIGIN_ADOPTED,
)
from src.core.session_identity import record_instance
from src.core.session_manager import SessionManager

TEST_SOCKET = "cloude_pfw_test"
EPOCH = 1793100000


class _StubSettings:
    """Just enough of ``Settings`` for ``SessionManager.__init__`` and
    ``_writable_datastore_connection`` (which reads ``get_state_dir()``).
    """

    def __init__(self, root: Path):
        self._root = root

    def get_pinned_themes_path(self) -> Path:
        return self._root / "pinned_themes.json"

    def get_unread_state_path(self) -> Path:
        return self._root / "unread_state.json"

    def get_session_metadata_path(self) -> Path:
        return self._root / "session_metadata.json"

    def get_log_directory(self) -> Path:
        return self._root / "logs"

    def get_state_dir(self) -> Path:
        return self._root

    @property
    def log_directory(self) -> str:
        return str(self._root / "logs")

    @property
    def default_working_dir(self) -> str:
        return str(self._root)

    @property
    def log_buffer_size(self) -> int:
        return 100

    @property
    def agents(self):
        return None


def _manager_with_migrated_db(monkeypatch, tmp_path: Path) -> SessionManager:
    """A SessionManager whose state dir holds a real, migrated cloude.db."""
    (tmp_path / "logs").mkdir(exist_ok=True)
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    stub = _StubSettings(tmp_path)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


def _seed(tmp_path, name, **fields):
    """Insert one row directly against the manager's real db file."""
    conn = connect(db_path_for(tmp_path))
    try:
        result = record_instance(
            conn,
            socket=TEST_SOCKET,
            name=name,
            epoch=EPOCH,
            origin=SESSION_ORIGIN_ADOPTED,
            lifecycle=SESSION_LIFECYCLE_RUNNING,
            **fields,
        )
        conn.commit()
        return result.session_uuid
    finally:
        conn.close()


def _read_row(tmp_path, name):
    conn = connect(db_path_for(tmp_path))
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE tmux_name = ?", (name,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def test_manager_wiring_persists_through_a_real_connection(monkeypatch, tmp_path):
    """The method actually reaches the database file on disk."""
    mgr = _manager_with_migrated_db(monkeypatch, tmp_path)
    uuid = _seed(tmp_path, "cloude_pfw_a")

    mgr._persist_fingerprint_family(session_uuid=uuid, agent_family="claude")

    row = _read_row(tmp_path, "cloude_pfw_a")
    assert row["agent_family"] == "claude"
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_FINGERPRINT


def test_manager_wiring_never_overwrites_a_launched_row(monkeypatch, tmp_path):
    """The manager method inherits the no-overwrite rule, not just the lib."""
    mgr = _manager_with_migrated_db(monkeypatch, tmp_path)
    uuid = _seed(
        tmp_path,
        "cloude_pfw_launched",
        agent_type="claude-code",
        agent_family_source=SESSION_FAMILY_SOURCE_LAUNCHED,
    )

    mgr._persist_fingerprint_family(session_uuid=uuid, agent_family="codex")

    row = _read_row(tmp_path, "cloude_pfw_launched")
    assert row["agent_type"] == "claude-code"
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_LAUNCHED


def test_manager_wiring_swallows_a_missing_datastore(monkeypatch, tmp_path):
    """No cloude.db on disk at all: the method returns quietly, never raises."""
    (tmp_path / "logs").mkdir(exist_ok=True)
    stub = _StubSettings(tmp_path)  # note: no ensure_db_migrated() call
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    mgr = SessionManager()

    # Must not raise.
    mgr._persist_fingerprint_family(session_uuid="whatever", agent_family="claude")


def test_manager_wiring_swallows_a_blank_session_uuid(monkeypatch, tmp_path):
    """A caller regression (empty uuid) must not surface as an exception."""
    mgr = _manager_with_migrated_db(monkeypatch, tmp_path)

    # Must not raise.
    mgr._persist_fingerprint_family(session_uuid="", agent_family="claude")
