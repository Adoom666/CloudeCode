"""feat/db-is-authoritative - mutations against the authoritative table.

Split from tests/test_project_authority.py to keep both files inside this
project's 500-line rule. That file covers the read side and the three
authority outcomes; this one covers create / rename / delete / touch and
the launcher ordering they drive.

ROOT IS THE IDENTITY here, not the display name - his config.json carried
three different names for one folder, so a lookup by name is a lookup that
can legitimately match more than one row, and that is treated as its own
outcome rather than resolved by taking the first.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from src.core.db import connect, db_path_for
from src.core.project_writes import (
    ProjectNameAmbiguous,
    ProjectNotFound,
    ProjectRootConflict,
    create_project,
    delete_project,
    list_projects_ordered,
    resolve_by_name,
    touch_project_by_path,
    update_project,
)
from tests.test_project_authority import (  # noqa: F401 - fixture re-export
    REAL_UNIQUE_ROOT_COUNT,
    duplicate_laden_config,
    seeded,
    state_dir,
)


# --- writes ---------------------------------------------------------------


class TestWrites:
    """Create / rename / delete / touch, against the authoritative table."""

    def test_create_then_read_back(self, state_dir: Path) -> None:
        with closing(connect(db_path_for(state_dir))) as conn:
            row = create_project(conn, name="app", path="~/app", description="d")
            assert row["display_name"] == "app"
            assert row["root"] == str(Path("~/app").expanduser())
            assert row["raw_path"] == "~/app", "the user's own spelling is kept"
            assert list_projects_ordered(conn)[0]["id"] == row["id"]

    def test_create_refuses_a_duplicate_root(self, seeded: Path) -> None:
        """The refusal that keeps one node per folder."""
        with closing(connect(db_path_for(seeded))) as conn:
            with pytest.raises(ProjectRootConflict):
                create_project(
                    conn,
                    name="a-fourth-name-for-it",
                    path="/Users/jsugamele/Development/ses_ec5bf2a3",
                )

    def test_rename_changes_the_label_not_the_root(self, seeded: Path) -> None:
        with closing(connect(db_path_for(seeded))) as conn:
            target = resolve_by_name(conn, "CloudeCode")
            row = update_project(conn, target["id"], new_name="Cloude Code")
            assert row["display_name"] == "Cloude Code"
            assert row["root"] == target["root"]

    def test_rename_to_an_existing_name_is_refused(self, seeded: Path) -> None:
        from src.core.project_writes import ProjectNameConflict

        with closing(connect(db_path_for(seeded))) as conn:
            target = resolve_by_name(conn, "CloudeCode")
            with pytest.raises(ProjectNameConflict):
                update_project(conn, target["id"], new_name="Development")

    def test_empty_description_clears_and_none_leaves_alone(
        self, state_dir: Path
    ) -> None:
        with closing(connect(db_path_for(state_dir))) as conn:
            row = create_project(conn, name="p", path="/p", description="keep")
            assert update_project(conn, row["id"], new_name="p2")["description"] == "keep"
            assert update_project(conn, row["id"], description="")["description"] == ""

    def test_delete_removes_the_row_and_returns_it(self, seeded: Path) -> None:
        with closing(connect(db_path_for(seeded))) as conn:
            target = resolve_by_name(conn, "ai-setup")
            removed = delete_project(conn, target["id"])
            assert removed["root"] == target["root"]
            assert len(list_projects_ordered(conn)) == REAL_UNIQUE_ROOT_COUNT - 1
            with pytest.raises(ProjectNotFound):
                resolve_by_name(conn, "ai-setup")

    def test_an_ambiguous_name_is_its_own_error_not_a_guess(
        self, state_dir: Path
    ) -> None:
        """Two roots can share a name; picking one silently edits the wrong project."""
        with closing(connect(db_path_for(state_dir))) as conn:
            create_project(conn, name="same", path="/one")
            conn.execute(
                "INSERT INTO projects (root, raw_path, display_name, source, "
                "created_at, updated_at) VALUES ('/two','/two','same','user','t','t')"
            )
            conn.commit()
            with pytest.raises(ProjectNameAmbiguous):
                resolve_by_name(conn, "same")

    def test_touch_does_NOT_move_a_project_to_the_top(self, seeded: Path) -> None:
        """SUPERSEDES ``test_touch_moves_a_project_to_the_top``.

        That test pinned the behaviour the user asked to have removed. He
        reads the launcher as a timeline - "it keeps me know what was
        recent and i can look down the list to recap other things" - and
        ``touch_project_by_path`` is what POST /sessions calls when he
        CLICKS a project, so the old contract meant looking at the list
        reordered it.

        ``last_opened_at`` is still written; it is simply no longer the
        sort key. See ``_ORDER_BY`` in src/core/project_writes.py, and
        tests/test_session_work_ordering.py for the key that replaced it.
        """
        with closing(connect(db_path_for(seeded))) as conn:
            before = [r["display_name"] for r in list_projects_ordered(conn)]
            assert before[0] != "ai-setup"
            touched = touch_project_by_path(
                conn, "/Users/jsugamele/Development/ai-setup"
            )
            assert touched is not None
            assert touched["last_opened_at"] is not None, (
                "the column is kept and still written - only the ordering changed"
            )
            assert [r["display_name"] for r in list_projects_ordered(conn)] == before

    def test_touch_on_an_unknown_directory_is_a_miss_not_an_error(
        self, seeded: Path
    ) -> None:
        with closing(connect(db_path_for(seeded))) as conn:
            assert touch_project_by_path(conn, "/tmp/not-a-project") is None

    def test_import_order_is_preserved_before_anything_is_opened(
        self, seeded: Path
    ) -> None:
        """The first render after migrating must match the last one before it."""
        with closing(connect(db_path_for(seeded))) as conn:
            names = [r["display_name"] for r in list_projects_ordered(conn)]
        assert names[0] == "fs2"
        assert names[-1] == "ai-setup"

    def test_a_newly_created_project_sorts_LAST_until_it_is_worked_in(
        self, seeded: Path
    ) -> None:
        """SUPERSEDES ``test_a_newly_created_project_sorts_first``.

        The list is ordered by work now, and a project created a second
        ago has had none done in it - which is a real thing to say, not a
        demotion. It joins the unrecorded tail, in creation order, and its
        row is LABELLED as unrecorded so its position does not read as
        "the stalest thing you own" (see ``_projectWorkAttrs`` in
        client/js/launchpad.js). The first turn run inside it moves it to
        the top, which is what the old rule was really reaching for.
        """
        with closing(connect(db_path_for(seeded))) as conn:
            create_project(conn, name="brand-new", path="/brand/new")
            rows = list_projects_ordered(conn)
            assert rows[-1]["display_name"] == "brand-new"
            assert rows[-1]["work_at"] is None

    def test_a_presence_refresh_does_not_reorder_the_launcher(
        self, seeded: Path
    ) -> None:
        """Nothing a plain page load touches may reorder the launcher.

        refresh_and_list_presence writes updated_at on every row on every
        plain page load, so ordering by updated_at would reshuffle the
        list purely from it being looked at. That was the original reason
        for ``last_opened_at``; the ordering has since moved on again, to
        MAX(sessions.last_work_at), because opening turned out to be a
        look too. The invariant this test guards is the durable one and
        is unchanged: LOOKING CHANGES NOTHING.
        """
        from src.core.project_store import refresh_and_list_presence

        with closing(connect(db_path_for(seeded))) as conn:
            before = [r["display_name"] for r in list_projects_ordered(conn)]

            touch_project_by_path(conn, "/Users/jsugamele/Development/ai-setup")
            refresh_and_list_presence(conn)

            after = [r["display_name"] for r in list_projects_ordered(conn)]

        assert after == before
