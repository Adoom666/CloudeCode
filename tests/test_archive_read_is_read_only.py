"""The archive read connection must refuse writes AND still work.

THE POSITIVE CONTROL IS THE POINT OF THIS FILE. A test that only asserts
"an INSERT raises" cannot tell a read-only connection from a broken one:
a connection to a corrupt file, to the wrong path, or one never opened at
all refuses writes too. Asserting the refusal alone is a check that
passes for the wrong reason - the same defect class this API is built to
avoid. So the refusal is always asserted alongside a SELECT that returns
real rows ON THE SAME CONNECTION.

The second thing proved here is that the connection survives a WAL
database with NO ``-shm`` sidecar. That is the case a ``mode=ro`` URI
open fails on, and it is why ``open_read_only`` uses
``PRAGMA query_only=ON`` instead.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_archro_logs_"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.archive_read import open_read_only
from src.core.db import DatastoreUnreadableError
from tests.archive_fixture import (
    make_state_dir,
    seed_corpus,
    seed_host,
    seed_project,
    seed_transcript,
    writable,
)


@pytest.fixture()
def seeded_state_dir(tmp_path: Path) -> Path:
    """A state directory holding one host, corpus, project and transcript.

    Description: seeded through an ordinary read-write connection, so the
      rows the positive control counts are genuinely there.
    Inputs: tmp_path (Path) - pytest per-test directory.
    Output: Path - the state directory.
    Example: conn = open_read_only(seeded_state_dir)
    """
    state_dir = make_state_dir(tmp_path)
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id)
            project_id = seed_project(conn, corpus_id, slug="-project-a")
            seed_transcript(
                conn,
                host_id=host_id,
                corpus_id=corpus_id,
                project_id=project_id,
                source_path="a.jsonl",
            )
    return state_dir


def test_select_returns_rows_and_insert_is_refused_on_one_connection(
    seeded_state_dir: Path,
) -> None:
    """The pair, on ONE connection: reads work, writes raise."""
    with closing(open_read_only(seeded_state_dir)) as conn:
        # POSITIVE CONTROL. Without this, the refusal below proves nothing.
        rows = conn.execute("SELECT id FROM message_transcripts").fetchall()
        assert len(rows) == 1, "positive control failed: the connection reads nothing"
        assert (
            conn.execute("SELECT COUNT(*) FROM message_hosts").fetchone()[0] == 1
        ), "positive control failed: host row not visible"

        # NEGATIVE CONTROL, same connection object, same moment.
        with pytest.raises(sqlite3.OperationalError) as caught:
            conn.execute(
                "INSERT INTO message_roles (value) VALUES ('smuggled')"
            )
        assert "readonly" in str(caught.value).lower()

        # And the connection is still usable afterwards, which proves the
        # refusal did not simply break it.
        assert conn.execute("SELECT COUNT(*) FROM message_transcripts").fetchone()[0] == 1


def test_query_only_pragma_reads_back_as_one(seeded_state_dir: Path) -> None:
    """The pragma is measured, not assumed. Setting it is only a request."""
    with closing(open_read_only(seeded_state_dir)) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_update_and_delete_are_refused_too(seeded_state_dir: Path) -> None:
    """Every write verb, not just INSERT, with the read control alongside."""
    with closing(open_read_only(seeded_state_dir)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_transcripts").fetchone()[0] == 1
        for statement in (
            "UPDATE message_transcripts SET line_count = 999",
            "DELETE FROM message_transcripts",
            "CREATE TABLE smuggled (a INTEGER)",
        ):
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(statement)
        assert conn.execute("SELECT COUNT(*) FROM message_transcripts").fetchone()[0] == 1


def test_opens_a_wal_database_with_no_shm_sidecar(seeded_state_dir: Path) -> None:
    """A WAL file with no -shm opens fine, which is what mode=ro would fail.

    Description: the seeding connection is closed before this runs, so
      the sidecars are gone from disk. The assertion is that the read
      path still opens AND still reads - a connection that opened and
      returned nothing would be the false green this file exists to stop.
    """
    db = seeded_state_dir / "cloude.db"
    for sidecar in (db.with_name(db.name + "-shm"), db.with_name(db.name + "-wal")):
        if sidecar.exists():
            sidecar.unlink()
    assert not db.with_name(db.name + "-shm").exists()

    with closing(open_read_only(seeded_state_dir)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_transcripts").fetchone()[0] == 1


def test_a_missing_database_raises_rather_than_being_created(tmp_path: Path) -> None:
    """A typo'd state directory must not manufacture an empty archive.

    Description: an empty database renders to a user as a healthy install
      with no data. ``connect(create=False)`` is what stops that, and the
      assertion that the file still does not exist afterwards is what
      proves nothing was created on the way to the exception.
    """
    missing = tmp_path / "nowhere"
    missing.mkdir()
    with pytest.raises(DatastoreUnreadableError):
        open_read_only(missing)
    assert not (missing / "cloude.db").exists()
