"""The ordered schema steps, and the driver that runs a range of them.

Kept apart from src/core/db_migration.py so the STEPS table stays a short,
readable list of "what each version bump does" rather than being buried
inside the startup-resolution logic that decides WHETHER to run it.

EVERY STEP MUST BE IDEMPOTENT. A step inspects sqlite_master /
PRAGMA table_info before it acts, so re-running it after an interrupted
attempt either finishes the remaining work or finds it already there and
does nothing. This is what makes a retry after an INTERRUPTED trail entry
safe by construction rather than by hoping the crash happened at a
convenient moment.

EVERY STEP MUST BE ADDITIVE. CREATE TABLE, CREATE INDEX, ALTER TABLE ADD
COLUMN. Never a drop, a rename or a retype. The whole rollback design
depends on this: forward is additive, and backward is a RESTORE from a
verified backup rather than a hand-written reversal nobody can prove is
complete.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Dict, List

from src.core.db import ensure_install_id, get_meta, set_meta
from src.core.db_models import (
    DDL_V1,
    DDL_V2,
    DDL_V3,
    DDL_V4,
    DDL_V5,
    DDL_V6,
    DDL_V7,
    DDL_V8,
    META_CREATED_AT,
    META_PROJECT_TOMBSTONES_LEGACY_GAP,
    META_PROJECT_TOMBSTONES_SINCE,
    META_SCHEMA_VERSION,
)
from src.core.migration_trail import utc_now


def _step_v0_to_v1(conn: sqlite3.Connection) -> None:
    """Create the v1 schema: the meta and migration_trail tables.

    Description: idempotent by construction - every statement in DDL_V1
      carries IF NOT EXISTS, so re-running this after an interrupted
      attempt finishes whatever is missing and no-ops on the rest. Also
      seeds meta.created_at and meta.install_id on a genuinely new file,
      never overwriting an existing value.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    """
    for statement in DDL_V1:
        conn.execute(statement)
    if not conn.execute(
        "SELECT 1 FROM meta WHERE key=?", (META_CREATED_AT,)
    ).fetchone():
        stamp = utc_now()
        set_meta(conn, META_CREATED_AT, stamp)
        # A DATABASE CREATED BY THIS CODE HAS DELETION TRACKING FROM BIRTH.
        # It runs the whole chain 0 -> 5 in one transaction, so
        # project_tombstones exists before any project row can be created,
        # let alone deleted - there is no window in which a deletion could
        # have left no trace. Recording it HERE, on the genuinely-new-file
        # path, is what lets _step_v4_to_v5 read the ABSENCE of the marker
        # as proof that the database predates this code. Row counts cannot
        # do that job: a database whose projects were all deleted is
        # indistinguishable from a fresh one by counting, which is the
        # exact false-negative this replaces.
        set_meta(conn, META_PROJECT_TOMBSTONES_SINCE, stamp)
        set_meta(conn, META_PROJECT_TOMBSTONES_LEGACY_GAP, "0")
    ensure_install_id(conn)


def _step_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Create the sessions table and its four indexes (design section 3.3).

    Description: build step S4. Idempotent by construction - every
      statement in DDL_V2 carries IF NOT EXISTS, so re-running this after
      an interrupted attempt creates whatever is missing and no-ops on
      the rest. Purely additive: it creates one table and four indexes
      and touches nothing that shipped in v1, so a v1 reader that has not
      been upgraded keeps working against the same file.

      The unique index it installs, ux_sessions_tmux_instance, is the
      object that makes tmux-name reuse safe: identity is the triple
      (tmux_socket, tmux_name, tmux_created_epoch), never the name alone.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v1_to_v2(conn)  # after _step_v0_to_v1
    """
    for statement in DDL_V2:
        conn.execute(statement)


def _step_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Add ``sessions.tmux_session_id``, the instance discriminator.

    Description: build step S4 hardening. Purely additive - one nullable
      column and no index, so a v2 reader that has not been upgraded keeps
      working against the same file and every existing row simply carries
      NULL.

      IDEMPOTENT BY INSPECTION, NOT BY THE STATEMENT. SQLite's
      ``ALTER TABLE ADD COLUMN`` has no ``IF NOT EXISTS``, so re-running
      this after an interrupted attempt would raise "duplicate column
      name". PRAGMA table_info is read first and the step no-ops when the
      column is already present, which is what makes a retry after an
      INTERRUPTED trail entry safe by construction.

      The column holds tmux's ``#{session_id}``. It is NOT part of the
      identity key - see the comment above ``DDL_SESSIONS_ADD_SESSION_ID``
      in db_models for why a per-server-lifetime counter is a worse
      durable key than the creation epoch and a better live discriminator.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v2_to_v3(conn)  # after _step_v1_to_v2
    """
    existing = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")
    }
    if "tmux_session_id" in existing:
        return
    for statement in DDL_V3:
        conn.execute(statement)


def _step_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Add ``projects.last_opened_at``, the launcher's ordering key.

    Description: feat/db-is-authoritative. Purely additive - one nullable
      column on ``projects``, no index - so a v3 reader that has not been
      upgraded keeps working against the same file and every existing row
      simply carries NULL.

      IDEMPOTENT BY INSPECTION, NOT BY THE STATEMENT, exactly as v2 -> v3
      is: SQLite's ``ALTER TABLE ADD COLUMN`` has no ``IF NOT EXISTS``, so
      PRAGMA table_info is read first and the step no-ops when the column
      is already there. That is what makes a retry after an INTERRUPTED
      trail entry safe by construction.

      A database that has never reached v3 has no ``projects`` table at
      all, which cannot happen here - the chain runs in order and v0 -> v1
      creates it - but the PRAGMA on a missing table returns no rows
      rather than raising, so the guard degrades to attempting the ALTER
      and surfacing SQLite's own error inside the caller's transaction
      rather than corrupting anything.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v3_to_v4(conn)  # after _step_v2_to_v3
    """
    existing = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(projects)")
    }
    if "last_opened_at" in existing:
        return
    for statement in DDL_V4:
        conn.execute(statement)


def _step_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Add ``project_tombstones`` and record whether this DB has a legacy gap.

    Description: the schema half of the every-start project reconcile.
      Purely additive - one new table, nothing on ``projects`` altered -
      and idempotent by the statement itself (``CREATE TABLE IF NOT
      EXISTS``) rather than by inspection, so a retry after an
      INTERRUPTED trail entry is safe.

      THE SECOND THING THIS STEP DOES, AND WHY IT BELONGS HERE. The
      reconcile can only tell "never imported" from "deliberately
      deleted" for deletions that happened AFTER this table existed.
      Deletions before it left no trace of any kind, so on a database
      that already held project history those two causes are
      indistinguishable - the third outcome, CANNOT EVALUATE.

      That fact is measurable exactly once, at this instant, and never
      again: a database with project rows or a stamped import latch
      predates tracking, a database created at v5 cannot. So the step
      records the answer rather than leaving a later reader to guess it.
      ``project_reconcile`` reads it to decide whether an unexplained
      absence is safe to import or has to be reported as unknown.

      A fresh install runs the whole chain 0 -> 5 in one transaction with
      no rows and no latch, so it records no gap and reconciles
      automatically from its first start.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v4_to_v5(conn)  # after _step_v3_to_v4
    """
    for statement in DDL_V5:
        conn.execute(statement)

    if get_meta(conn, META_PROJECT_TOMBSTONES_SINCE):
        return

    # NO MARKER MEANS THIS DATABASE PREDATES THIS CODE, so it has a legacy
    # gap - full stop, no heuristic. A file created by this version was
    # stamped in _step_v0_to_v1 during its own 0 -> 5 chain and returned
    # above; reaching this line means the file was created by an earlier
    # version, which had no tombstone table and therefore deleted projects
    # without leaving any trace. Whether it ACTUALLY deleted any is
    # unknowable, and that is precisely the point: the gap records that the
    # question cannot be answered, not that a deletion happened.
    set_meta(conn, META_PROJECT_TOMBSTONES_SINCE, utc_now())
    set_meta(conn, META_PROJECT_TOMBSTONES_LEGACY_GAP, "1")


def _step_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Add ``sessions.user_declined_at``, the durable "leave as external".

    Description: Stage C of the session-attribution import. Purely
      additive - one nullable column, no index - so a v5 reader that has
      not been upgraded keeps working against the same file and every
      existing row simply carries NULL.

      NULL IS "NEVER ANSWERED", NOT "SAID YES". The Stage-D re-run gate
      reads this column together with ``origin``: it re-examines only
      rows still at ``origin='observed'`` whose ``user_declined_at`` is
      NULL. So a later import that adds an admissible tier can PROMOTE a
      row it can now prove, can never demote one, and can never re-ask a
      question the user has already answered.

      IDEMPOTENT BY INSPECTION, NOT BY THE STATEMENT, exactly as v2 -> v3
      and v3 -> v4 are: SQLite's ALTER TABLE ADD COLUMN has no
      IF NOT EXISTS, so PRAGMA table_info is read first and the step
      no-ops when the column is already there.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v5_to_v6(conn)  # after _step_v4_to_v5
    """
    existing = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")
    }
    if "user_declined_at" in existing:
        return
    for statement in DDL_V6:
        conn.execute(statement)


def _step_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Add ``ix_sessions_claude_uuid``, the lineage lookup index.

    Description: build step for Claude-session identity and fork lineage.
      Purely additive and, unlike every step before it, idempotent BY THE
      STATEMENT: ``CREATE INDEX IF NOT EXISTS`` needs no PRAGMA
      inspection, so a retry after an INTERRUPTED trail entry is safe
      without one. The inspection pattern used by v2..v6 exists because
      SQLite's ALTER TABLE ADD COLUMN has no IF NOT EXISTS; an index does.

      NO COLUMN IS ADDED. ``claude_session_uuid``, ``parent_session_id``
      and ``fork_kind`` have been in the sessions DDL since v2 and were
      never written by anything. This step indexes the first of them
      because src/core/session_lineage.py, added in the same change, asks
      "does any row already carry this uuid" on every Claude SessionStart
      - once as the fork detector and once as the duplicate-delivery
      guard. A v6 reader that has not been upgraded keeps working against
      the same file: an index changes no row and no query result, only
      how fast the answer arrives.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v6_to_v7(conn)  # after _step_v5_to_v6
    """
    for statement in DDL_V7:
        conn.execute(statement)


def _step_v7_to_v8(conn: sqlite3.Connection) -> None:
    """Add ``session_groups`` and ``session_group_members``.

    Description: build step for user-defined sidebar groups. Two new
      tables and two new indexes; no existing table is altered and no
      column is added to one, so - like v7 and unlike v3..v6 - every
      statement carries its own ``IF NOT EXISTS`` and the step is
      idempotent without inspecting ``PRAGMA table_info``. A retry after
      an INTERRUPTED trail entry is therefore safe with no inspection.

      NOTHING IS BACKFILLED, AND THAT IS THE CORRECT EMPTY STATE. Before
      this version no group existed, so every session is ungrouped, and
      "ungrouped" is represented by the ABSENCE of a membership row
      rather than by a row pointing at a default group. There is no
      migration to write because there is no prior state to translate -
      which is different from a migration that had nothing to do, and the
      distinction is why this docstring says so out loud.

      A v7 READER STILL WORKS. It does not know these tables exist and
      never queries them; its sidebar renders the pinned/rest split it
      always did. The groups are not lost, only unread - the same trade
      the projects table already made, and recorded here for the same
      reason.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v7_to_v8(conn)  # after _step_v6_to_v7
    """
    for statement in DDL_V8:
        conn.execute(statement)


#: Columns the v9 merge may carry from the discarded corpse onto the
#: survivor, and ONLY when the survivor has nothing there. Declared as a
#: list rather than "every column" so a future column is opted in
#: deliberately: silently copying a column whose meaning nobody checked
#: is how a repair becomes a corruption.
_V9_CARRY_FORWARD = (
    "project_id",
    "working_dir",
    "legacy_session_id",
    "agent_type",
    "agent_family",
    "model",
    "claude_session_uuid",
    "parent_session_id",
    "fork_kind",
    "pinned_theme",
    "audio_enabled",
    "title",
    "adopted_at",
    "last_seen_running_at",
)


def _v9_merge_rename_splits(conn: sqlite3.Connection) -> int:
    """Heal every pair of rows the rename bug made out of one session.

    Description: the repair half of v9. A rename used to move the one
      field identity was keyed on, so the stored row was reaped as
      ``tmux_missing`` and the same live session returned through the
      adopt path as a stranger and got a second row.

      THE SHAPE IS RECOGNISABLE FROM THE DATA ALONE, which is what makes
      this safe to run unattended. A split pair is: same socket, same
      ``tmux_created_epoch``, same NON-NULL tmux ``#{session_id}``,
      EXACTLY two rows, EXACTLY one of them ``stopped`` and one not. Two
      genuinely different sessions cannot share both an epoch and an id
      on one socket - the id is unique per tmux server lifetime and the
      epoch pins the lifetime - so nothing legitimate has this shape.
      Every clause is a refusal: three rows, two live rows, or a NULL id
      all leave the group untouched, because none of those can be told
      apart from real data.

      THE SURVIVOR IS THE LIVE ROW. It carries the name tmux actually
      reports right now, which is the only name any probe will match.

      ORIGIN COMES FROM THE CORPSE WHEN THE CORPSE SAYS ``created``, and
      this is the one place that rule is broken on purpose. ``origin`` is
      normally written once and never recomputed - but the survivor's
      ``adopted`` is an ARTEFACT OF THE DEFECT, not a user action: the
      session was adopted only because the rename made the app treat its
      own session as a stranger. Restoring ``created`` puts back what was
      true before the bug rewrote it. The reasoning is recorded here
      rather than left implicit precisely because it contradicts the
      general rule.

      CARRY-FORWARD FILLS GAPS AND NEVER OVERRIDES. A value already on
      the survivor is a live answer and wins; the corpse only supplies
      what the survivor has nothing for. Unread flags are ORed, since
      "unread" is a claim that something happened and losing it is worse
      than keeping it.

      IDEMPOTENT. After a run no group has two rows, so a retry after an
      INTERRUPTED trail entry finds nothing to do.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: int - how many corpse rows were removed.
    Example: _v9_merge_rename_splits(conn)  # 1
    """
    groups = conn.execute(
        "SELECT tmux_socket, tmux_created_epoch, tmux_session_id "
        "FROM sessions "
        "WHERE tmux_session_id IS NOT NULL AND tmux_session_id != '' "
        "AND tmux_created_epoch IS NOT NULL "
        "GROUP BY tmux_socket, tmux_created_epoch, tmux_session_id "
        "HAVING COUNT(*) = 2"
    ).fetchall()

    merged = 0
    for group in groups:
        socket, epoch, sid = (
            group["tmux_socket"],
            group["tmux_created_epoch"],
            group["tmux_session_id"],
        )
        pair = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sessions WHERE tmux_socket = ? "
                "AND tmux_created_epoch = ? AND tmux_session_id = ? "
                "ORDER BY id",
                (socket, epoch, sid),
            ).fetchall()
        ]
        living = [r for r in pair if r["lifecycle"] != "stopped"]
        corpses = [r for r in pair if r["lifecycle"] == "stopped"]
        if len(living) != 1 or len(corpses) != 1:
            # Two live rows, or two corpses. Neither is the split this
            # step recognises, and guessing which to keep would be a
            # verdict nobody measured.
            continue
        survivor, corpse = living[0], corpses[0]

        sets, values = [], []
        for column in _V9_CARRY_FORWARD:
            if survivor.get(column) is None and corpse.get(column) is not None:
                sets.append(f"{column} = ?")
                values.append(corpse[column])
        if corpse.get("origin") == "created" and survivor.get("origin") != "created":
            sets.append("origin = ?")
            values.append("created")
        for flag in ("unread_auto", "unread_manual"):
            if int(corpse.get(flag) or 0) and not int(survivor.get(flag) or 0):
                sets.append(f"{flag} = 1")
        earlier = min(
            str(survivor.get("created_at") or ""),
            str(corpse.get("created_at") or ""),
        )
        if earlier and earlier != survivor.get("created_at"):
            sets.append("created_at = ?")
            values.append(earlier)

        if sets:
            values.append(int(survivor["id"]))
            conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", values
            )
        # Group membership is keyed on tmux_name, not on the row, so the
        # corpse's memberships describe the OLD name and are meaningless
        # once the name is gone. Nothing to move.
        conn.execute("DELETE FROM sessions WHERE id = ?", (int(corpse["id"]),))
        merged += 1
    return merged


def _v9_backfill_labels(conn: sqlite3.Connection) -> int:
    """Give every row a human label derived from its tmux name.

    Description: ``sessions.title`` becomes the user-facing LABEL, and a
      row with none would render blank. The derivation reverses two
      things the APP did rather than anything a user chose: the
      ``cloude_`` prefix the launcher adds, and the underscores the
      name filter substitutes for spaces.

      AN EXISTING TITLE IS NEVER TOUCHED. The lineage feature already
      writes this column for forks, and overwriting a title someone or
      something chose in order to install a derived one would be a
      downgrade.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: int - how many rows were given a label.
    Example: _v9_backfill_labels(conn)  # 3
    """
    from src.core.session_label import label_from_tmux_name

    rows = conn.execute(
        "SELECT id, tmux_name FROM sessions "
        "WHERE (title IS NULL OR title = '') AND tmux_name IS NOT NULL"
    ).fetchall()
    filled = 0
    for row in rows:
        label = label_from_tmux_name(row["tmux_name"])
        if not label:
            continue
        conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (label, int(row["id"])),
        )
        filled += 1
    return filled


def _step_v8_to_v9(conn: sqlite3.Connection) -> None:
    """Make ``title`` the session LABEL, and heal the rename splits.

    Description: NO COLUMN IS ADDED. ``sessions.title`` has been in the
      DDL since v2; what changes at v9 is its MEANING - it becomes the
      user-facing label, decoupled from the tmux session name so that
      renaming a session no longer moves the field identity is keyed on.
      This step is therefore entirely data: backfill the column so no row
      renders blank, and repair the rows the old behaviour split.

      ORDER MATTERS. The merge runs FIRST, so a corpse about to be
      deleted is not given a label on the way out and the survivor is
      labelled from the name it will actually keep.

      A v8 READER STILL WORKS against the same file. It does not display
      ``title`` and will show the tmux name as it always did; the labels
      are not lost, only unread. The merged rows it will simply not see,
      which is the point.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v8_to_v9(conn)  # after _step_v7_to_v8
    """
    _v9_merge_rename_splits(conn)
    _v9_backfill_labels(conn)


def _step_v9_to_v10(conn: sqlite3.Connection) -> None:
    """Give Claude's own session name its own column.

    Description: ADDS ``sessions.claude_title``. Until v10, Claude Code's
      ``session_title`` (carried on the SessionStart payload) was written
      into ``title`` - the same column v9 had just made the USER's label.
      One field, two authorities, and every consequence was silent:
      Claude's auto-generated name could become the user's displayed
      label; a user rename discarded Claude's name with no record; and the
      write had to be guarded write-once to stop the first two, which in
      turn meant a LATER Claude-side ``/rename`` could never land at all.

      NO BACKFILL, DELIBERATELY. For an existing row with a non-null
      ``title`` there is no evidence anywhere in the database saying
      whether the user typed it or Claude generated it - the write-once
      guard did not record provenance. Copying ``title`` into
      ``claude_title`` would therefore invent an attribution rather than
      recover one, and would re-create the exact confusion this step
      exists to end. ``claude_title`` starts NULL on every row and fills
      itself from the next SessionStart that carries one, which is the
      only source that can be trusted to say what Claude calls it.

      A v9 READER STILL WORKS against the same file: it does not select
      ``claude_title`` and sees ``title`` exactly as before.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v9_to_v10(conn)  # after _step_v8_to_v9
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "claude_title" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN claude_title TEXT")


def _step_v10_to_v11(conn: sqlite3.Connection) -> None:
    """Make the hook-derived activity status durable.

    Description: ADDS ``activity_state`` and ``activity_state_at``. The
      status was previously held only in ``SessionActivityTracker``, an
      in-memory dict deliberately never persisted on the reasoning that a
      restart legitimately forgets what a process was doing.

      That reasoning was wrong in one specific way, measured 2026-08-28:
      the fallback it degrades to is the tmux tier, and under this app's
      own launch path ``pane_current_command`` is a CONSTANT (`zsh`, for
      every session, thinking or idle alike). So a forgotten state does
      not become "unknown", it becomes a confident `idle` - a session
      mid-turn and a session at a prompt render identically.

      NO BACKFILL. Nothing in the database records what any session was
      doing before this column existed, and inventing `idle` for every
      row would be exactly the false green the column exists to end. NULL
      means "no state recorded", which the reader must render as
      not-measured rather than as idle.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v10_to_v11(conn)  # after _step_v9_to_v10
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "activity_state" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN activity_state TEXT")
    if "activity_state_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN activity_state_at TEXT")


# from_version -> the function that advances it by one. Adding a key here
# without bumping CURRENT_SCHEMA_VERSION in db_models (or vice versa) is
# caught by tests/test_db_migration.py, because a bumped constant with no
# step is a database that can never reach the version the code demands.
STEPS: Dict[int, Callable[[sqlite3.Connection], None]] = {
    0: _step_v0_to_v1,
    1: _step_v1_to_v2,
    2: _step_v2_to_v3,
    3: _step_v3_to_v4,
    4: _step_v4_to_v5,
    5: _step_v5_to_v6,
    6: _step_v6_to_v7,
    7: _step_v7_to_v8,
    8: _step_v8_to_v9,
    9: _step_v9_to_v10,
    10: _step_v10_to_v11,
}


def run_chain(conn: sqlite3.Connection, from_version: int, to_version: int) -> List[str]:
    """Apply every step from from_version up to to_version, in order.

    Description: the caller owns the transaction, so a failure anywhere
      in the chain rolls back EVERY step in it and leaves
      meta.schema_version untouched. Forward jumping is not a separate
      mechanism: v1 straight to v4 is simply steps 2, 3 and 4 back to
      back inside that one transaction.
    Inputs: conn (sqlite3.Connection) - already inside a transaction.
      from_version (int), to_version (int).
    Output: list[str] - labels of the steps applied, e.g. ["0->1"].
    Raises: KeyError - a version in the range has no registered step.
      Anything a step itself raises, unmodified, so the caller's
      transaction context manager rolls the whole chain back.
    Example: run_chain(conn, 0, 1) -> ["0->1"]
    """
    applied: List[str] = []
    for version in range(from_version, to_version):
        step = STEPS.get(version)
        if step is None:
            raise KeyError(
                f"no migration step registered for schema v{version} -> "
                f"v{version + 1}"
            )
        step(conn)
        applied.append(f"{version}->{version + 1}")
    set_meta(conn, META_SCHEMA_VERSION, str(to_version))
    return applied
