"""Every session belongs to a project, and no row may say two things.

Two live defects drove this file, both measured on the owner's box on
2026-09-08 and both invisible to the 4874 tests that already passed.

ONE. Rows 7 and 8 carried ``project_id`` 1 and 2 beside
``project_attribution = 'none'``. The client resolves that contradiction
attribution-first, so two sessions with perfectly good projects rendered
under "no project". The cause is a HALF-WRITE: the adopt path derived
``(None, 'none')`` and handed both to ``claim_instance``, which applies
each column "only when not None" - so the None id was skipped and the
'none' attribution landed alone.

TWO. The derivation was ``'none'`` in the first place because
``~/Development`` is a symlink into iCloud Drive and the lexical matcher
correctly refuses to resolve symlinks. The project was declared at the
long spelling; the session was probed at the short one.

The negative control is not optional here and it is the last class in
this file: a matcher that always finds something is worse than useless,
so a directory that genuinely cannot be read must still come back
``unknown``, and an ``unknown`` must still write nothing.
"""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import pytest

from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    SESSION_ATTRIBUTION_DERIVED_DEEPEST,
    SESSION_ATTRIBUTION_NONE,
    SESSION_ATTRIBUTION_UNKNOWN,
)
from src.core.project_writes import create_project
from src.core.session_project_binding import (
    BINDING_CREATED,
    BINDING_MATCHED,
    BINDING_MATCHED_CANONICAL,
    BINDING_NONE,
    BINDING_UNKNOWN,
    backfill_sessions_under_root,
    columns_to_write,
    ensure_project_row,
    resolve_project_binding,
)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """A migrated, empty datastore directory.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: Path - the state directory, with cloude.db at CURRENT schema.
    """
    d = tmp_path / "state"
    d.mkdir()
    state = ensure_db_migrated(d, 4, "0.0.0")
    assert state.status == "ok", state.message
    return d


def _conn(state_dir: Path):
    """Open the migrated datastore.

    Inputs: state_dir (Path).
    Output: sqlite3.Connection - caller closes it.
    """
    return connect(db_path_for(state_dir))


def _add_project(conn, root: str, name: str) -> int:
    """Insert one project row directly, at the root as spelled.

    Description: bypasses ``create_project`` on purpose where a test
      needs the row without the session backfill it now performs.
    Inputs: conn, root (str), name (str).
    Output: int - the new project id.
    """
    cur = conn.execute(
        "INSERT INTO projects (root, raw_path, display_name, source, "
        "presence, created_at, updated_at) "
        "VALUES (?, ?, ?, 'user', 'unchecked', '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z')",
        (root, root, name),
    )
    return int(cur.lastrowid)


def _add_session(conn, *, name: str, epoch: int, working_dir: str,
                 project_id=None, attribution=SESSION_ATTRIBUTION_UNKNOWN):
    """Insert one live session row on the default socket.

    Inputs: conn, name (str), epoch (int), working_dir (str),
      project_id (int | None), attribution (str).
    Output: int - the new session row id.
    """
    cur = conn.execute(
        "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
        "tmux_created_epoch, origin, lifecycle, working_dir, project_id, "
        "project_attribution, created_at, updated_at) "
        "VALUES (?, 'cloude', ?, ?, 'created', 'running', ?, ?, ?, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (f"u-{name}-{epoch}", name, epoch, working_dir, project_id,
         attribution),
    )
    return int(cur.lastrowid)


class TestTheSymlinkSpellingRung:
    """A path that matches nothing as written may still match resolved."""

    def test_short_spelling_matches_a_project_declared_at_the_long_one(
        self, state_dir: Path, tmp_path: Path
    ):
        """The live defect: /Users/x/Development/App vs its iCloud target.

        Modelled with a real symlink rather than a string, because the
        rung under test is a filesystem operation and a stubbed path
        would prove only that the stub was called.
        """
        target = tmp_path / "iCloud" / "Assistants" / "Media"
        target.mkdir(parents=True)
        link = tmp_path / "Development"
        link.symlink_to(tmp_path / "iCloud")
        with closing(_conn(state_dir)) as conn:
            with conn:
                pid = _add_project(conn, str(target), "Media")
                binding = resolve_project_binding(
                    conn, str(link / "Assistants" / "Media")
                )
        assert binding.rule == BINDING_MATCHED_CANONICAL
        assert binding.project_id == pid
        assert binding.attribution == SESSION_ATTRIBUTION_DERIVED_DEEPEST

    def test_the_as_written_match_still_wins(self, state_dir: Path,
                                             tmp_path: Path):
        """Canonicalising is the FALLBACK, never the primary comparison.

        A project declared at the symlink must keep collecting the
        sessions declared at that same symlink, which is the whole
        reason project_attribution refuses to resolve.
        """
        target = tmp_path / "iCloud" / "App"
        target.mkdir(parents=True)
        link = tmp_path / "Development"
        link.symlink_to(tmp_path / "iCloud")
        with closing(_conn(state_dir)) as conn:
            with conn:
                at_link = _add_project(conn, str(link / "App"), "App at link")
                _add_project(conn, str(target), "App at target")
                binding = resolve_project_binding(conn, str(link / "App"))
        assert binding.rule == BINDING_MATCHED
        assert binding.project_id == at_link


class TestTheArchivedCatchAll:
    """A hidden project rooted at $HOME must not swallow every session."""

    def test_an_archived_home_root_does_not_win_the_as_written_rung(
        self, state_dir: Path, tmp_path: Path
    ):
        """CAUGHT BY THE REPAIR'S OWN DRY RUN, ON LIVE, 2026-09-08.

        The owner's install carries an archived catch-all project rooted
        at his home directory. It contains every session on the machine,
        so it matched as-written for rows 7 and 8 - and matching there
        meant the canonical rung, the one that finds the project they
        actually belong to, never ran. The repair refused to relocate
        them, which is how this was seen at all.
        """
        home = tmp_path / "home"
        target = home / "iCloud" / "Media"
        target.mkdir(parents=True)
        link = home / "Development"
        link.symlink_to(home / "iCloud")
        with closing(_conn(state_dir)) as conn:
            with conn:
                catch_all = _add_project(conn, str(home), "home")
                conn.execute(
                    "UPDATE projects SET archived_at = '2026-01-02T00:00:00Z' "
                    "WHERE id = ?", (catch_all,)
                )
                media = _add_project(conn, str(target), "Media")
                binding = resolve_project_binding(
                    conn, str(link / "Media"), stored_project_id=media
                )
        assert binding.project_id == media
        assert binding.rule == BINDING_MATCHED_CANONICAL

    def test_a_live_catch_all_still_matches(self, state_dir: Path,
                                            tmp_path: Path):
        """Only ARCHIVED roots are excluded. A real one still counts."""
        home = tmp_path / "home"
        (home / "loose").mkdir(parents=True)
        with closing(_conn(state_dir)) as conn:
            with conn:
                catch_all = _add_project(conn, str(home), "home")
                binding = resolve_project_binding(conn, str(home / "loose"))
        assert binding.project_id == catch_all
        assert binding.rule == BINDING_MATCHED


class TestThePairMovesTogether:
    """A derivation may never overwrite half a row."""

    def test_none_beside_a_stored_project_writes_nothing(self,
                                                         state_dir: Path):
        """The exact shape of live rows 7 and 8.

        The row holds project 4; the probe matches nothing. The old code
        wrote the 'none' and skipped the None id, leaving a row that
        says two things. Nothing may be written now.
        """
        with closing(_conn(state_dir)) as conn:
            with conn:
                binding = resolve_project_binding(
                    conn, "/nowhere/at/all", stored_project_id=4
                )
        assert binding.rule == BINDING_NONE
        assert columns_to_write(binding, 4) == (None, None)

    def test_none_with_no_stored_project_writes_the_bare_none(
        self, state_dir: Path
    ):
        """A complete answer on a row with nothing to contradict."""
        with closing(_conn(state_dir)) as conn:
            with conn:
                binding = resolve_project_binding(conn, "/nowhere/at/all")
        assert columns_to_write(binding, None) == (
            None, SESSION_ATTRIBUTION_NONE,
        )

    def test_a_match_writes_both_halves(self, state_dir: Path,
                                        tmp_path: Path):
        """The only case that touches ``project_id`` at all."""
        root = tmp_path / "repo"
        root.mkdir()
        with closing(_conn(state_dir)) as conn:
            with conn:
                pid = _add_project(conn, str(root), "repo")
                binding = resolve_project_binding(
                    conn, str(root / "sub"), stored_project_id=9
                )
        assert columns_to_write(binding, 9) == (
            pid, SESSION_ATTRIBUTION_DERIVED_DEEPEST,
        )


class TestTheNoProjectFallback:
    """No project contains it, so one is created. Once."""

    def test_a_directory_outside_every_root_gets_a_project_of_its_own(
        self, state_dir: Path, nonscratch_tmp_path: Path
    ):
        """The owner's invariant, stated as a test.

        This is also the negative-shaped case that must NOT return NULL:
        a working directory outside every root still ends up with a
        project, never with nothing.
        """
        lonely = nonscratch_tmp_path / "lonely"
        lonely.mkdir()
        with closing(_conn(state_dir)) as conn:
            with conn:
                binding = resolve_project_binding(
                    conn, str(lonely), allow_create=True
                )
                rows = conn.execute(
                    "SELECT COUNT(*) FROM projects"
                ).fetchone()[0]
        assert binding.rule == BINDING_CREATED
        assert binding.created_project is True
        assert binding.project_id is not None
        assert binding.attribution == SESSION_ATTRIBUTION_DERIVED_DEEPEST
        assert rows == 1

    def test_minting_is_idempotent(self, state_dir: Path,
                                   nonscratch_tmp_path: Path):
        """A second resolve reuses the project rather than colliding.

        ``projects.root`` is UNIQUE, so a non-idempotent mint would not
        merely duplicate - it would raise on the second session opened
        in the same folder.
        """
        lonely = nonscratch_tmp_path / "lonely"
        lonely.mkdir()
        with closing(_conn(state_dir)) as conn:
            with conn:
                first = resolve_project_binding(
                    conn, str(lonely), allow_create=True
                )
                second = resolve_project_binding(
                    conn, str(lonely), allow_create=True
                )
                rows = conn.execute(
                    "SELECT COUNT(*) FROM projects"
                ).fetchone()[0]
        assert second.project_id == first.project_id
        assert second.created_project is False
        assert rows == 1

    def test_a_project_registered_at_the_symlink_is_not_duplicated(
        self, state_dir: Path, tmp_path: Path
    ):
        """Both spellings are checked BEFORE anything is inserted."""
        target = tmp_path / "iCloud" / "App"
        target.mkdir(parents=True)
        link = tmp_path / "Development"
        link.symlink_to(tmp_path / "iCloud")
        with closing(_conn(state_dir)) as conn:
            with conn:
                pid, created, _ = ensure_project_row(conn, str(link / "App"))
                rows = conn.execute(
                    "SELECT COUNT(*) FROM projects"
                ).fetchone()[0]
        # Nothing existed, so one row was minted - at the canonical form.
        assert created is True and rows == 1
        with closing(_conn(state_dir)) as conn:
            with conn:
                again, created_again, _ = ensure_project_row(
                    conn, str(target)
                )
                rows = conn.execute(
                    "SELECT COUNT(*) FROM projects"
                ).fetchone()[0]
        assert again == pid and created_again is False and rows == 1

    def test_a_scratch_directory_is_the_one_exclusion(self,
                                                      state_dir: Path):
        """A per-run temp folder must never become a launcher entry."""
        with closing(_conn(state_dir)) as conn:
            with conn:
                binding = resolve_project_binding(
                    conn, "/private/tmp/claude-501/x", allow_create=True
                )
                rows = conn.execute(
                    "SELECT COUNT(*) FROM projects"
                ).fetchone()[0]
        assert binding.rule == BINDING_NONE
        assert rows == 0


class TestTheCreateRace:
    """Punchlist 16, from both directions."""

    def test_creating_a_project_adopts_the_sessions_already_under_it(
        self, state_dir: Path, tmp_path: Path
    ):
        """FAILS ON THE OLD ORDERING.

        Row 45 on live: the session row was written 15 ms before its own
        project row, attributed against a table that did not yet contain
        it, and landed ``project_id NULL / attribution 'none'`` with
        nothing to re-probe it. This is that sequence exactly.
        """
        folder = tmp_path / "ses_5a756046"
        folder.mkdir()
        with closing(_conn(state_dir)) as conn:
            with conn:
                row_id = _add_session(
                    conn, name="cloude_Punchlist Test", epoch=1788878841,
                    working_dir=str(folder), project_id=None,
                    attribution=SESSION_ATTRIBUTION_NONE,
                )
            created = create_project(
                conn, name="Punchlist Test", path=str(folder)
            )
            after = conn.execute(
                "SELECT project_id, project_attribution FROM sessions "
                "WHERE id = ?", (row_id,)
            ).fetchone()
        assert after[0] == created["id"]
        assert after[1] == SESSION_ATTRIBUTION_DERIVED_DEEPEST

    def test_the_backfill_never_moves_a_session_off_its_project(
        self, state_dir: Path, tmp_path: Path
    ):
        """Purely additive. A row that HAS a project is not reconsidered."""
        folder = tmp_path / "repo"
        (folder / "deep").mkdir(parents=True)
        with closing(_conn(state_dir)) as conn:
            with conn:
                held = _add_project(conn, str(folder / "deep"), "deep")
                row_id = _add_session(
                    conn, name="a", epoch=1, working_dir=str(folder / "deep"),
                    project_id=held,
                    attribution=SESSION_ATTRIBUTION_DERIVED_DEEPEST,
                )
                moved = backfill_sessions_under_root(
                    conn, root=str(folder), project_id=999
                )
                after = conn.execute(
                    "SELECT project_id FROM sessions WHERE id = ?", (row_id,)
                ).fetchone()[0]
        assert moved == 0
        assert after == held

    def test_the_backfill_leaves_archived_rows_alone(self, state_dir: Path,
                                                     tmp_path: Path):
        """Archiving is a decision about the user's list, not a gap."""
        folder = tmp_path / "repo"
        folder.mkdir()
        with closing(_conn(state_dir)) as conn:
            with conn:
                row_id = _add_session(
                    conn, name="a", epoch=1, working_dir=str(folder),
                    project_id=None, attribution=SESSION_ATTRIBUTION_NONE,
                )
                conn.execute(
                    "UPDATE sessions SET archived_at = '2026-01-02T00:00:00Z' "
                    "WHERE id = ?", (row_id,)
                )
                moved = backfill_sessions_under_root(
                    conn, root=str(folder), project_id=42
                )
        assert moved == 0


class TestTheNegativeControl:
    """A matcher that always finds something is worse than useless."""

    def test_an_unreadable_directory_is_unknown_and_writes_nothing(
        self, state_dir: Path, tmp_path: Path
    ):
        """Not having looked is never evidence of absence.

        A relative path cannot be situated lexically at all, so it must
        come back ``unknown`` - NOT ``none``, and NOT quietly attached
        to whichever project happens to be closest - and it must not
        mint a project even when minting is permitted.
        """
        root = tmp_path / "repo"
        root.mkdir()
        with closing(_conn(state_dir)) as conn:
            with conn:
                _add_project(conn, str(root), "repo")
                binding = resolve_project_binding(
                    conn, "relative/path", stored_project_id=3,
                    allow_create=True,
                )
                rows = conn.execute(
                    "SELECT COUNT(*) FROM projects"
                ).fetchone()[0]
        assert binding.rule == BINDING_UNKNOWN
        assert binding.attribution == SESSION_ATTRIBUTION_UNKNOWN
        assert binding.determined is False
        assert columns_to_write(binding, 3) == (None, None)
        assert rows == 1

    def test_a_none_probe_is_unknown_not_none(self, state_dir: Path):
        """The probe did not answer. That is not "belongs to nothing"."""
        with closing(_conn(state_dir)) as conn:
            with conn:
                binding = resolve_project_binding(conn, None)
        assert binding.rule == BINDING_UNKNOWN

    def test_an_empty_projects_table_does_not_invent_a_match(
        self, state_dir: Path, tmp_path: Path
    ):
        """With no projects at all, a real directory must not match one."""
        real = tmp_path / "work"
        real.mkdir()
        with closing(_conn(state_dir)) as conn:
            with conn:
                binding = resolve_project_binding(conn, str(real))
        assert binding.rule == BINDING_NONE
        assert binding.project_id is None


class TestRestoreOnAdopt:
    """Re-entering a session must not downgrade what its row already says."""

    def test_persist_adoption_keeps_a_stored_project_the_probe_cannot_see(
        self, state_dir: Path, tmp_path: Path
    ):
        """Live rows 7 and 8, end to end through the adopt path.

        The UI re-opens sessions through this path routinely, so before
        the fix every reopen of a short-spelling session rewrote its
        attribution to 'none' while leaving the id in place.
        """
        from src.core.session_adopt_persist import persist_adoption
        from src.core.tmux_listing import TmuxListing

        target = tmp_path / "iCloud" / "Media"
        target.mkdir(parents=True)
        link = tmp_path / "Development"
        link.symlink_to(tmp_path / "iCloud")
        short = str(link / "Media")

        listing = TmuxListing.answered([{
                "name": "cloude_Media",
                "created_at_epoch": 1788444837,
                "working_dir": short,
                "tmux_session_id": "$1",
        }])
        with closing(_conn(state_dir)) as conn:
            with conn:
                pid = _add_project(conn, str(target), "Media")
                row_id = _add_session(
                    conn, name="cloude_Media", epoch=1788444837,
                    working_dir=short, project_id=pid,
                    attribution=SESSION_ATTRIBUTION_NONE,
                )
            with conn:
                result = persist_adoption(
                    conn, socket="cloude", name="cloude_Media",
                    listing=listing,
                )
            after = conn.execute(
                "SELECT project_id, project_attribution FROM sessions "
                "WHERE id = ?", (row_id,)
            ).fetchone()
        assert result.persisted, result.detail
        # The canonical rung recovers the project the lexical rule could
        # not see, so the contradiction is RESOLVED rather than frozen.
        assert after[0] == pid
        assert after[1] == SESSION_ATTRIBUTION_DERIVED_DEEPEST

    def test_persist_adoption_never_writes_none_over_a_stored_project(
        self, state_dir: Path, tmp_path: Path
    ):
        """The half-write, guarded directly.

        Here no spelling can rescue the match - the directory really is
        outside every project - and the stored id must still survive
        untouched rather than acquiring a contradicting 'none'.
        """
        from src.core.session_adopt_persist import persist_adoption
        from src.core.tmux_listing import TmuxListing

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        listing = TmuxListing.answered([{
                "name": "cloude_X",
                "created_at_epoch": 500,
                "working_dir": str(elsewhere),
                "tmux_session_id": "$2",
        }])
        with closing(_conn(state_dir)) as conn:
            with conn:
                pid = _add_project(conn, str(other), "other")
                row_id = _add_session(
                    conn, name="cloude_X", epoch=500,
                    working_dir=str(elsewhere), project_id=pid,
                    attribution=SESSION_ATTRIBUTION_DERIVED_DEEPEST,
                )
            with conn:
                persist_adoption(
                    conn, socket="cloude", name="cloude_X", listing=listing,
                )
            after = conn.execute(
                "SELECT project_id, project_attribution FROM sessions "
                "WHERE id = ?", (row_id,)
            ).fetchone()
        assert after[0] == pid
        assert after[1] == SESSION_ATTRIBUTION_DERIVED_DEEPEST


class TestRestoreOnBootReadopt:
    """A row's project must survive the pass that re-holds it at boot."""

    def test_a_row_with_project_18_comes_back_holding_project_18(
        self, state_dir: Path, tmp_path: Path
    ):
        """The boot re-adopt writes nothing, and that is the guarantee.

        It re-attaches a live pane and registers an in-memory Session;
        it never re-derives attribution, so the stored pair is exactly
        what a later read gets back. This asserts the pass is not a
        writer, which is what makes "the row is the single source of
        truth for a session's project" true across a restart.
        """
        import inspect

        import src.core.session_boot_readopt as readopt

        source = inspect.getsource(readopt)
        assert "project_attribution" not in source
        assert "resolve_project_binding" not in source

        root = tmp_path / "CloudeCode"
        root.mkdir()
        with closing(_conn(state_dir)) as conn:
            with conn:
                pid = _add_project(conn, str(root), "CloudeCode")
                row_id = _add_session(
                    conn, name="cloude_Agent", epoch=1788813811,
                    working_dir=str(root), project_id=pid,
                    attribution=SESSION_ATTRIBUTION_DERIVED_DEEPEST,
                )
            after = conn.execute(
                "SELECT project_id, project_attribution FROM sessions "
                "WHERE id = ?", (row_id,)
            ).fetchone()
        assert after[0] == pid
        assert after[1] == SESSION_ATTRIBUTION_DERIVED_DEEPEST


class TestTheCreatePathMintsRatherThanLandingNull:
    """A session created in an unregistered folder still gets a project."""

    def test_persist_creation_gives_a_new_folder_its_own_project(
        self, state_dir: Path, nonscratch_tmp_path: Path
    ):
        """FAILS ON THE OLD ORDERING.

        The old body called ``attribute`` against whatever the projects
        table happened to hold at that instant and recorded the answer,
        so a folder with no project yet produced ``project_id NULL``.
        """
        from src.core.session_create_persist import persist_creation
        from src.core.tmux_listing import TmuxListing

        folder = nonscratch_tmp_path / "brand_new"
        folder.mkdir()
        listing = TmuxListing.answered([{
                "name": "cloude_New",
                "created_at_epoch": 900,
                "working_dir": str(folder),
                "tmux_session_id": "$3",
        }])
        with closing(_conn(state_dir)) as conn:
            with conn:
                result = persist_creation(
                    conn, socket="cloude", name="cloude_New",
                    listing=listing, working_dir=str(folder),
                )
            row = conn.execute(
                "SELECT project_id, project_attribution FROM sessions "
                "WHERE tmux_name = ?", ("cloude_New",)
            ).fetchone()
        assert result.recorded, result.detail
        assert row[0] is not None
        assert row[1] == SESSION_ATTRIBUTION_DERIVED_DEEPEST


def test_realpath_is_what_canonical_means(tmp_path: Path):
    """Guard the one assumption the spelling rung rests on.

    If ``canonical`` ever stopped resolving symlinks, every test above
    would still pass on a machine with no symlink in the path and the
    live defect would silently return.
    """
    from src.core.transcript_import_paths import canonical

    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert canonical(str(link)) == os.path.realpath(str(target))
