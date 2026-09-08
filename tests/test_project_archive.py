"""Archiving a project: the shelf, not the delete.

Covers src/core/project_archive.py and the read paths that honour
``archived_at``. The properties that matter here are not "does the column
get set" - they are the ones that would let archive quietly become a
suppression mechanism or a bulk delete:

  * an archived project LEAVES the default list and COMES BACK when asked
    for, and the round trip is lossless;
  * archiving a project touches NOTHING about its sessions;
  * it is idempotent with the FIRST stamp winning, so a second archive
    cannot rewrite the moment the decision was made;
  * an archived project is still addressable by name, or unarchive could
    never reach the row it exists to restore.

Fixtures are re-exported from tests/test_project_authority.py, matching
tests/test_project_writes.py.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from src.core.db import connect, db_path_for
from src.core.project_archive import archive_project, unarchive_project
from src.core.project_store import list_projects
from src.core.project_writes import (
    ProjectNotFound,
    create_project,
    list_projects_ordered,
    resolve_by_name,
)
from tests.test_project_authority import (  # noqa: F401 - fixture re-export
    duplicate_laden_config,
    seeded,
    state_dir,
)


def _names(rows) -> set:
    """Display names in a project row list.

    Inputs: rows (list[dict]).
    Output: set[str].
    """
    return {r["display_name"] for r in rows}


class TestArchiveRoundTrip:
    """Leaves the list, comes back, loses nothing."""

    def test_archive_hides_from_default_list_and_include_shows_it(
        self, state_dir: Path
    ) -> None:
        with closing(connect(db_path_for(state_dir))) as conn:
            create_project(conn, name="live", path="~/live")
            row = create_project(conn, name="dormant", path="~/dormant")

            assert _names(list_projects_ordered(conn)) == {"live", "dormant"}

            assert archive_project(conn, row["id"]) is True

            # The default read - the one every existing caller makes.
            assert _names(list_projects_ordered(conn)) == {"live"}
            assert _names(list_projects(conn)) == {"live"}

            # Asked for explicitly, it is back, and it is MIXED IN with
            # the live rows rather than served as a separate list.
            both = list_projects_ordered(conn, include_archived=True)
            assert _names(both) == {"live", "dormant"}
            archived = [r for r in both if r["archived_at"] is not None]
            assert _names(archived) == {"dormant"}

    def test_unarchive_restores_it(self, state_dir: Path) -> None:
        with closing(connect(db_path_for(state_dir))) as conn:
            row = create_project(conn, name="dormant", path="~/dormant")
            archive_project(conn, row["id"])
            assert _names(list_projects_ordered(conn)) == set()

            assert unarchive_project(conn, row["id"]) is True
            back = list_projects_ordered(conn)
            assert _names(back) == {"dormant"}
            assert back[0]["archived_at"] is None
            # Lossless: everything else about the row survived the trip.
            assert back[0]["root"] == row["root"]
            assert back[0]["raw_path"] == row["raw_path"]

    def test_archived_project_is_still_addressable_by_name(
        self, state_dir: Path
    ) -> None:
        """Or unarchive could never reach the row it restores."""
        with closing(connect(db_path_for(state_dir))) as conn:
            row = create_project(conn, name="dormant", path="~/dormant")
            archive_project(conn, row["id"])
            found = resolve_by_name(conn, "dormant")
            assert found["id"] == row["id"]
            assert found["archived_at"] is not None


class TestIdempotence:
    """First stamp wins, and 'already' is its own answer."""

    def test_second_archive_reports_false_and_keeps_the_first_stamp(
        self, state_dir: Path
    ) -> None:
        with closing(connect(db_path_for(state_dir))) as conn:
            row = create_project(conn, name="p", path="~/p")
            assert archive_project(conn, row["id"], now="2026-01-01T00:00:00Z") is True
            assert archive_project(conn, row["id"], now="2026-06-06T00:00:00Z") is False
            [only] = list_projects_ordered(conn, include_archived=True)
            assert only["archived_at"] == "2026-01-01T00:00:00Z", (
                "the second archive must not rewrite when the user decided"
            )

    def test_unarchiving_a_live_project_reports_false(
        self, state_dir: Path
    ) -> None:
        with closing(connect(db_path_for(state_dir))) as conn:
            row = create_project(conn, name="p", path="~/p")
            assert unarchive_project(conn, row["id"]) is False

    def test_missing_row_raises_rather_than_reporting_success(
        self, state_dir: Path
    ) -> None:
        with closing(connect(db_path_for(state_dir))) as conn:
            with pytest.raises(ProjectNotFound):
                archive_project(conn, 9999)
            with pytest.raises(ProjectNotFound):
                unarchive_project(conn, 9999)


class TestSessionsAreNotTouched:
    """The design constraint, asserted rather than asserted-about.

    Archiving a project is a statement about a SHELF, not about the work
    on it. If this ever cascades, a user retiring a dormant project takes
    out live session history he never asked to lose and cannot undo in
    one action.
    """

    @staticmethod
    def _seed_session(conn, uuid: str, project_id: int, lifecycle: str) -> None:
        """Insert one sessions row attributed to a project.

        Inputs: conn (sqlite3.Connection). uuid (str). project_id (int).
          lifecycle (str).
        Output: None.
        """
        from src.core.db import transaction
        from src.core.trail_entry import utc_now

        with transaction(conn):
            conn.execute(
                "INSERT INTO sessions (session_uuid, origin, tmux_socket,"
                " tmux_name, tmux_created_epoch, lifecycle, project_id,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid, "created", "cloude", f"cloude_{uuid}", 1000,
                 lifecycle, project_id, utc_now(), utc_now()),
            )

    def test_archiving_a_project_archives_none_of_its_sessions(
        self, state_dir: Path
    ) -> None:
        from src.core.session_store import list_sessions

        with closing(connect(db_path_for(state_dir))) as conn:
            project = create_project(conn, name="dormant", path="~/dormant")
            self._seed_session(conn, "sess-1", project["id"], "running")

            before = list_sessions(conn, include_archived=False)
            assert len(before) == 1
            assert before[0]["archived_at"] is None

            archive_project(conn, project["id"])

            after = list_sessions(conn, include_archived=False)
            assert len(after) == 1, (
                "the session must still be listed by the DEFAULT read - "
                "the one that excludes archived sessions"
            )
            assert after[0]["archived_at"] is None, (
                "archiving a project must not stamp its sessions"
            )
            assert after[0]["session_uuid"] == "sess-1"

    def test_unarchiving_does_not_resurrect_a_deleted_session(
        self, state_dir: Path
    ) -> None:
        from src.core.session_store import archive_session, list_sessions

        with closing(connect(db_path_for(state_dir))) as conn:
            project = create_project(conn, name="dormant", path="~/dormant")
            self._seed_session(conn, "sess-1", project["id"], "stopped")
            # A session the USER deleted, independently.
            archive_session(conn, "sess-1")

            archive_project(conn, project["id"])
            unarchive_project(conn, project["id"])

            rows = list_sessions(conn, include_archived=True)
            assert len(rows) == 1
            assert rows[0]["archived_at"] is not None, (
                "unarchiving a project must not resurrect a session the "
                "user deleted - they are separate decisions"
            )


class TestNoTombstone:
    """An archive is not a deletion, so it leaves no deletion record."""

    def test_archive_writes_no_tombstone(self, state_dir: Path) -> None:
        from src.core.project_tombstones import tombstoned_roots

        with closing(connect(db_path_for(state_dir))) as conn:
            row = create_project(conn, name="p", path="~/p")
            archive_project(conn, row["id"])
            assert row["root"] not in tombstoned_roots(conn), (
                "a tombstone means 'deliberately removed, do not "
                "re-import'; the row is still here"
            )
