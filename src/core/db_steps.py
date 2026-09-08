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

import json
import sqlite3
from typing import Callable, Dict, List

import structlog

from src.core.db import (
    column_exists,
    ensure_install_id,
    get_meta,
    set_meta,
    table_exists,
)
from src.core.db_models import (
    DDL_SESSIONS_CLAUDE_UUID_PLAIN_INDEX_DROP,
    DDL_SESSIONS_CLAUDE_UUID_UNIQUE_INDEX,
    DDL_V1,
    DDL_V2,
    DDL_V3,
    DDL_V4,
    DDL_V5,
    DDL_V6,
    DDL_V7,
    DDL_V8,
    DDL_V14,
    DDL_V15_TRANSCRIPT_ARCHIVES_GROWTH_KIND,
    DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ID,
    DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_INDEX,
    DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ROOTED_AT,
    DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ROOTED_BY,
    DDL_V15_TRANSCRIPT_ARCHIVES_SUPERSEDED_BY,
    DDL_V15_TRANSCRIPT_ARCHIVES_SUPERSEDED_BY_INDEX,
    DDL_V15_TRANSCRIPT_ROOT_DECISIONS_PROJECT_ID,
    DDL_V22_TRANSCRIPT_ARCHIVES_CONTENT_SHA_INDEX,
    DDL_V22_TRANSCRIPT_ARCHIVES_DEDUPE_KIND,
    DDL_V23_SESSIONS_LAST_WORK_AT,
    DDL_V24,
    DDL_V25_SESSIONS_KIND,
    DDL_V25_SESSIONS_KIND_BACKFILL,
    META_CREATED_AT,
    META_PROJECT_TOMBSTONES_LEGACY_GAP,
    META_PROJECT_TOMBSTONES_SINCE,
    META_SCHEMA_VERSION,
    META_SESSIONS_CLAUDE_UUID_DUPLICATES,
)
from src.core.archive_overlay_ddl import DDL_V19
from src.core.message_activity import install_transcript_activity
from src.core.message_scheme_repair import repair_session_ref_schemes
from src.core.message_block_ddl import DDL_V18
from src.core.message_host_ddl import DDL_V17
from src.core.message_archive_flag import message_archive_enabled
from src.core.message_model_ddl import DDL_V16
from src.core.migration_trail import utc_now
from src.core.session_group_membership_migrate import carry_memberships

logger = structlog.get_logger()


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


def _v12_find_claude_uuid_duplicates(conn: sqlite3.Connection) -> List[Dict]:
    """Find every ``claude_session_uuid`` claimed by more than one row.

    Description: the pre-check that decides which branch
      :func:`_step_v11_to_v12` takes. Read-only - it changes nothing,
      which is what lets it run every time the step runs without being
      guarded by its own idempotence check.
    Inputs: conn (sqlite3.Connection).
    Output: list[dict] - one entry per duplicated uuid, each
      ``{"claude_session_uuid": str, "row_ids": list[int], "count": int}``,
      ordered by uuid. Empty list means none found.
    Example: _v12_find_claude_uuid_duplicates(conn)  # []
    """
    rows = conn.execute(
        "SELECT claude_session_uuid, GROUP_CONCAT(id) AS ids, COUNT(*) AS c "
        "FROM sessions "
        "WHERE claude_session_uuid IS NOT NULL "
        "GROUP BY claude_session_uuid "
        "HAVING COUNT(*) > 1 "
        "ORDER BY claude_session_uuid"
    ).fetchall()
    return [
        {
            "claude_session_uuid": row["claude_session_uuid"],
            "row_ids": [int(x) for x in str(row["ids"]).split(",")],
            "count": int(row["c"]),
        }
        for row in rows
    ]


def _step_v11_to_v12(conn: sqlite3.Connection) -> None:
    """Make ``ux_sessions_claude_uuid`` UNIQUE, unless the data disagrees.

    Description: build step for the owner's 1:1 requirement - "everything
      should be stored and parented by id... if we do 1 for 1, this should
      never be an issue." Up to v11 that was enforced only by
      src/core/session_lineage.py checking before every write; this step
      makes SQLite refuse a second row for a uuid it already knows.

      THREE OUTCOMES, NEVER TWO, AND NEITHER COLLAPSE IS SAFE HERE.
      Checked FIRST, before any DDL runs, via
      :func:`_v12_find_claude_uuid_duplicates`:

        no duplicates    ux_sessions_claude_uuid is created and
                          ix_sessions_claude_uuid (the v7 plain index,
                          now redundant) is dropped. This is the SUCCESS
                          path - the common case, since v7's own comment
                          notes claude_session_uuid was NULL on every
                          adopted session on the owner's live machine.

        duplicates found COULD NOT EVALUATE, not a failure of THIS step
                          and not silently ignored. Neither index
                          statement runs: ix_sessions_claude_uuid (v7)
                          stays exactly as it was, so the database is not
                          left with a dangling reference to an index that
                          was dropped without its replacement existing.
                          The exact uuid/row-id groups are recorded under
                          META_SESSIONS_CLAUDE_UUID_DUPLICATES so a human
                          can see precisely what to reconcile - never a
                          silently-picked winner, per this project's own
                          standing rule against inventing a verdict
                          nobody measured.

        CREATE UNIQUE INDEX itself fails despite the pre-check finding
                          nothing (a defensive branch, not one the tests
                          below expect to hit under normal SQLite
                          behaviour) - caught narrowly as
                          ``sqlite3.Error``, recorded the same way as a
                          found duplicate, and NOT re-raised.

      NEVER RAISES. This is the one property that matters most: the
      caller (db_steps.run_chain, driven from db_migration.ensure_db_
      migrated) commits every step from the database's current version up
      to CURRENT_SCHEMA_VERSION as ONE transaction. An exception here
      would roll back every step before it in the same run and drop the
      whole app into DEGRADED_MIGRATION_FAILED / read-only - exactly the
      "never block boot" posture db_migration.py already holds for every
      other step, and a live database holding a duplicate uuid is a
      finding to record, not a reason to take the app down.

      DROPPING AN INDEX IS NOT THE "ADDITIVE ONLY" RULE THIS FILE'S
      MODULE DOCSTRING WARNS AGAINST. That rule protects STORED DATA -
      tables, columns, rows a restore could not reconstruct. An index is
      derived, not stored fact (REVERSAL_SQL_V7's own comment says so),
      and this step only ever drops ix_sessions_claude_uuid on the branch
      where ux_sessions_claude_uuid has just been created to replace it -
      the column stays exactly as indexable as before, only faster and
      now constrained.

      IDEMPOTENT. Both DDL statements carry IF NOT EXISTS / IF EXISTS, so
      a retry after an INTERRUPTED trail entry re-runs cleanly whichever
      branch applies; the duplicate check is a pure SELECT and costs
      nothing to repeat.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v11_to_v12(conn)  # after _step_v10_to_v11
    """
    duplicates = _v12_find_claude_uuid_duplicates(conn)
    if duplicates:
        set_meta(
            conn, META_SESSIONS_CLAUDE_UUID_DUPLICATES, json.dumps(duplicates)
        )
        logger.warning(
            "sessions_claude_uuid_unique_blocked",
            duplicate_uuid_count=len(duplicates),
            detail=(
                "one or more claude_session_uuid values are claimed by "
                "more than one sessions row; ux_sessions_claude_uuid was "
                "NOT created and ix_sessions_claude_uuid (plain) stays in "
                "place - see meta key "
                f"{META_SESSIONS_CLAUDE_UUID_DUPLICATES!r} for the exact "
                "uuid/row-id groups"
            ),
        )
        return

    try:
        conn.execute(DDL_SESSIONS_CLAUDE_UUID_UNIQUE_INDEX)
        conn.execute(DDL_SESSIONS_CLAUDE_UUID_PLAIN_INDEX_DROP)
    except sqlite3.Error as exc:
        # Defensive only - the pre-check above should make this
        # unreachable in practice. Recorded the same way as a found
        # duplicate rather than re-raised, for the same never-block-boot
        # reason.
        set_meta(
            conn,
            META_SESSIONS_CLAUDE_UUID_DUPLICATES,
            json.dumps([{"error": f"{type(exc).__name__}: {exc}"}]),
        )
        logger.warning(
            "sessions_claude_uuid_unique_index_failed", error=str(exc)
        )
        return

    set_meta(conn, META_SESSIONS_CLAUDE_UUID_DUPLICATES, "[]")


def _step_v12_to_v13(conn: sqlite3.Connection) -> None:
    """Add ``claude_session_uuid_source`` and backfill it for existing rows.

    Description: the provenance column for the adopted-session correlator
      (``src/core/claude_transcript_correlate.py``). Before this step
      ``session_lineage.record_claude_session`` - reached only from the
      Claude Code SessionStart hook - was the ONLY writer of
      ``claude_session_uuid`` that has ever shipped. So every row that
      already carries a non-NULL uuid at migration time got it from the
      hook, as a matter of history, not inference: the backfill sets
      ``claude_session_uuid_source = 'hook'`` on exactly those rows and
      leaves every other row NULL, because a NULL uuid has no provenance
      to record and inventing one would be the same false-fact problem
      this column exists to prevent.

      IDEMPOTENT. The ADD COLUMN is guarded by ``PRAGMA table_info``, and
      the backfill's ``WHERE claude_session_uuid_source IS NULL`` makes a
      second run touch nothing - it targets exactly the rows this step
      has not already labelled, so re-running it after an interrupted
      attempt cannot mislabel a row a later, different writer ('correlated')
      may have since claimed.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v12_to_v13(conn)  # after _step_v11_to_v12
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "claude_session_uuid_source" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN claude_session_uuid_source TEXT"
        )
    conn.execute(
        "UPDATE sessions SET claude_session_uuid_source = 'hook' "
        "WHERE claude_session_uuid IS NOT NULL "
        "AND claude_session_uuid_source IS NULL"
    )


def _step_v13_to_v14(conn: sqlite3.Connection) -> None:
    """Create the transcript-archive tables: archives, records, decisions.

    Description: build step for the byte-exact transcript fidelity store
      (src/core/transcript_archive.py). Three new tables and five new
      indexes, no existing table altered and no column added to one - so,
      like v7/v8, every statement carries its own ``IF NOT EXISTS`` and
      this step is idempotent without inspecting ``PRAGMA table_info``.

      NO BACKFILL, AND THAT IS THE CORRECT EMPTY STATE. Nothing before
      this version ever ingested a transcript, so there is no prior
      ``transcript_archives`` data to translate - the same reasoning
      v7 -> v8 already used for session_groups. Bulk ingestion of the
      owner's existing transcripts is a separate, later task that writes
      through this schema; this step only makes the schema exist.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v13_to_v14(conn)  # after _step_v12_to_v13
    """
    for statement in DDL_V14:
        conn.execute(statement)


def _step_v14_to_v15(conn: sqlite3.Connection) -> None:
    """Add prefix-dedupe columns and project-rooting columns.

    Description: see db_models.py's "schema v14 -> v15" comment block for
      the full design rationale (prefix dedupe for growing files, project-
      level rooting as a distinct weaker root). Six ADD COLUMN statements
      across two existing tables plus two new indexes - no existing
      column altered, no table rebuilt. Each ADD COLUMN is guarded by
      PRAGMA table_info, same idiom as v10/v11/v13, since SQLite's
      ALTER TABLE ADD COLUMN has no IF NOT EXISTS.

      NO BACKFILL NEEDED for growth_kind's DEFAULT 'initial': every row
      that exists before this migration WAS the only version of its
      source_path at the time it was ingested, so 'initial' is not a
      placeholder for those rows, it is the same true fact this column
      would have recorded for them had it existed then. superseded_by_
      archive_id, project_id, project_rooted_at and project_rooted_by
      all default to NULL, correctly meaning "not superseded" / "not yet
      project-rooted" for every pre-existing row - nothing before this
      version ever superseded a row or resolved a project.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v14_to_v15(conn)  # after _step_v13_to_v14
    """
    archive_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(transcript_archives)")
    }
    if "superseded_by_archive_id" not in archive_cols:
        conn.execute(DDL_V15_TRANSCRIPT_ARCHIVES_SUPERSEDED_BY)
    if "growth_kind" not in archive_cols:
        conn.execute(DDL_V15_TRANSCRIPT_ARCHIVES_GROWTH_KIND)
    if "project_id" not in archive_cols:
        conn.execute(DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ID)
    if "project_rooted_at" not in archive_cols:
        conn.execute(DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ROOTED_AT)
    if "project_rooted_by" not in archive_cols:
        conn.execute(DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ROOTED_BY)

    decision_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(transcript_root_decisions)")
    }
    if "project_id" not in decision_cols:
        conn.execute(DDL_V15_TRANSCRIPT_ROOT_DECISIONS_PROJECT_ID)

    conn.execute(DDL_V15_TRANSCRIPT_ARCHIVES_SUPERSEDED_BY_INDEX)
    conn.execute(DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_INDEX)


# ---------------------------------------------------------------------------
# THE MESSAGE-ARCHIVE GATE (feat/message-archive-flag)
# ---------------------------------------------------------------------------
# Steps 15->16, 16->17 and 17->18 build the message archive's schema, and
# the message archive is off by default. So each of those three steps asks
# src.core.message_archive_flag.message_archive_enabled() first and returns
# without executing a single DDL statement when the answer is no.
#
# THE VERSION COUNTER STILL ADVANCES, AND THAT IS DELIBERATE. meta.schema_version
# is one linear number for the whole datastore, not a per-feature ledger. If a
# gated-off step refused to advance it, an install with the archive off would
# park at v15 forever and every LATER step - none of which has anything to do
# with messages - would become unreachable for exactly the users who never
# opted in. So the step runs, applies nothing, and hands the counter on. The
# honest reading of "schema_version = 18" is therefore "this install has been
# offered every migration up to 18", not "every table any version ever
# described is present" - which was already true of this chain (v17's ALTERs
# are conditional, v14 and v16 are IF NOT EXISTS) and is now true on purpose.
#
# WHICH MAKES THE MATERIALIZER BELOW LOAD-BEARING. An install that migrated to
# v18 with the flag off has no message tables and no remaining steps that would
# create them, so turning the flag on could never take effect through the chain.
# apply_message_model_schema() is the path that closes that: src/main.py calls
# it once at startup whenever the flag is ON, it is idempotent by construction
# (every CREATE carries IF NOT EXISTS, every ALTER is guarded against a column
# already present), and it applies v16, v17 and v18 together rather than only
# the newest - because the off-to-on transition can be crossing any of them.
#
# OFF IS DORMANT, NEVER DESTRUCTIVE. Nothing here drops, truncates or alters a
# message table when the flag is off. An existing install that turns the
# feature off keeps every row it had.


def _apply_v16_ddl(conn: sqlite3.Connection) -> None:
    """Execute the v16 message-model DDL.

    Description: nine CREATE statements, each carrying its own
      IF NOT EXISTS, so this is safe to run against a database that
      already has them.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _apply_v16_ddl(conn)
    """
    for statement in DDL_V16:
        conn.execute(statement)


def _apply_v17_ddl(conn: sqlite3.Connection) -> None:
    """Execute the v17 host/corpus/project DDL, skipping columns already present.

    Description: SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT
      EXISTS``, so the six ALTERs are guarded against the live column
      list read from ``PRAGMA table_info``. Every other statement carries
      its own IF NOT EXISTS. Idempotent either way.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _apply_v17_ddl(conn)
    """
    existing = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(message_transcripts)")
    }
    for statement in DDL_V17:
        if statement.startswith("ALTER TABLE message_transcripts ADD COLUMN"):
            column = statement.split("ADD COLUMN", 1)[1].split()[0]
            if column in existing:
                continue
        conn.execute(statement)


def _apply_v18_ddl(conn: sqlite3.Connection) -> None:
    """Execute the v18 content-block DDL.

    Description: three CREATE TABLE and three CREATE INDEX statements,
      each with IF NOT EXISTS. Touches no row of ``message_bodies``.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _apply_v18_ddl(conn)
    """
    for statement in DDL_V18:
        conn.execute(statement)


def apply_message_model_schema(conn: sqlite3.Connection) -> None:
    """Materialize the whole message-archive schema, idempotently.

    Description: the off-to-on path. Applies v16, v17 and v18 together so
      that an install which crossed ANY of those versions with the flag
      off ends up with the complete schema the first time it starts with
      the flag on. Purely additive: no DROP, no UPDATE, no rewrite of
      ``message_bodies``, so it is O(1) against a multi-gigabyte corpus
      and safe to run on every startup.

      IT DOES NOT CONSULT THE FLAG. The caller has already decided; a
      second read here would be a second gate that could disagree with
      the first. src/main.py is the only caller and it calls this only
      when the flag resolved to enabled.
    Inputs: conn (sqlite3.Connection) - the caller owns the transaction.
    Output: None.
    Raises: sqlite3.Error - propagated so the caller's transaction rolls
      the whole thing back rather than leaving a half-built schema.
    Example: apply_message_model_schema(conn)
    """
    _apply_v16_ddl(conn)
    _apply_v17_ddl(conn)
    _apply_v18_ddl(conn)


def _step_v15_to_v16(conn: sqlite3.Connection) -> None:
    """Create the message identity / appearance model tables.

    Description: build step for the v16 message model - four lookup
      tables (record_type, role, model, compact_subtype), the transcript
      container, the message identity table, the appearance table, and
      the two findings tables. See src/core/message_model_ddl.py's module
      docstring for why a message uuid is not a row key and why identity
      and appearance are stored apart.

      Nine CREATE TABLE / CREATE INDEX statements, every one carrying its
      own IF NOT EXISTS, so - like v7/v8/v14 - this step is idempotent
      without inspecting PRAGMA table_info. No existing table is altered
      and no column is added to one.

      NO BACKFILL, AND THAT IS THE CORRECT EMPTY STATE. Nothing before
      this version wrote a message identity row, so there is no prior
      data to translate; the same reasoning v13 -> v14 already used for
      the transcript archive tables. Populating the model from the
      owner's existing corpus is a separate operation that writes THROUGH
      this schema.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v15_to_v16(conn)  # after _step_v14_to_v15
    """
    if not message_archive_enabled():
        return
    _apply_v16_ddl(conn)


def _step_v16_to_v17(conn: sqlite3.Connection) -> None:
    """Create the host / corpus / project dimension.

    Description: build step for v17 - three dimension tables
      (message_hosts, message_corpora, message_projects), six columns
      added to message_transcripts, four indexes and two views. See
      src/core/message_host_ddl.py's module docstring for why the host
      identity is the platform uuid, why a corpus sits between host and
      project, and why a body gets a VIEW rather than a host_id column.

      Every CREATE carries its own IF NOT EXISTS. The six ALTER TABLE
      ADD COLUMN statements do NOT - SQLite has no IF NOT EXISTS for
      them - so each is guarded against the columns already present,
      which keeps this step idempotent on a retry exactly as v7/v8/v14
      are. SQLite's ADD COLUMN rewrites only the table header, so this
      is O(1) even against the owner's 11 GB corpus database.

      NO BACKFILL, AND THE EMPTY STATE IS THE HONEST ONE. Existing v16
      transcripts get NULL in all six columns, which reads as "not yet
      attributed to a host" - a real third outcome that the reporting
      names as CANNOT DETERMINE. Defaulting them to a host would invent
      an attribution nobody measured, on the exact dimension this step
      exists to stop inventing. Attributing them is a separate operation
      that writes THROUGH this schema.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v16_to_v17(conn)  # after _step_v15_to_v16
    """
    if not message_archive_enabled():
        return
    _apply_v17_ddl(conn)


def _step_v17_to_v18(conn: sqlite3.Connection) -> None:
    """Create the derived content-block index.

    Description: build step for v18 - three tables
      (message_block_types, message_content_blocks,
      message_body_block_status) and three indexes. See
      src/core/message_block_ddl.py's module docstring for the measured
      shape of the corpus this indexes and for why the status table is
      separate from the block table.

      Every CREATE carries its own IF NOT EXISTS, so the step is
      idempotent on a retry with no PRAGMA inspection, exactly as
      v7/v8/v14/v16 are.

      IT DOES NOT TOUCH message_bodies. No ALTER, no UPDATE, no rewrite,
      not even a read. The 7.23 GiB column this index is derived FROM is
      not rewritten by the step that creates the index, so the migration
      is O(1) against an 11 GB corpus rather than a multi-hour rebuild
      inside startup.

      NO BACKFILL HERE, AND THE EMPTY STATE IS THE HONEST ONE. An
      existing v17 database gets three empty tables, and every body then
      has NO row in message_body_block_status - which reads as NEVER
      PROCESSED, a real third outcome, and not as "this body has no
      content blocks". Populating 2.4M bodies takes about 20 minutes
      measured, which is not something a startup migration may do to a
      user who just wanted the app to open. The backfill is a separate,
      resumable operation: scripts/message_block_backfill.py.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v17_to_v18(conn)  # after _step_v16_to_v17
    """
    if not message_archive_enabled():
        return
    _apply_v18_ddl(conn)


def _step_v18_to_v19(conn: sqlite3.Connection) -> None:
    """Create the presentation overlay table.

    Description: build step for v19 - one table
      (archive_project_overlay) and two partial indexes. See
      src/core/archive_overlay_ddl.py's module docstring for the identity
      key an overlay row attaches to and why it is the MERGE key rather
      than message_projects.id or the slug.

      Every statement carries its own IF NOT EXISTS, so the step is
      idempotent on a retry with no PRAGMA inspection, exactly as
      v7/v8/v14/v16/v18 are.

      IT DOES NOT READ, LET ALONE WRITE, ANY ARCHIVE TABLE. No ALTER, no
      UPDATE, no backfill, and not one SELECT against message_projects,
      message_transcripts, message_bodies, message_appearances or
      message_content_blocks. On the owner's 18.7 GB database this
      migration is three CREATE statements against an empty table, which
      is O(1) - the overlay is a statement ABOUT the archive and creating
      somewhere to put those statements cannot require touching it.

      NO BACKFILL, AND THE EMPTY STATE IS THE HONEST ONE. Every existing
      project starts with NO overlay row, which reads as "the owner has
      never said anything about this project" - a real third state, not a
      default, and distinguishable in the API from a project he renamed
      to its own folder name.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v18_to_v19(conn)  # after _step_v17_to_v18
    """
    for statement in DDL_V19:
        conn.execute(statement)


def _step_v19_to_v20(conn: sqlite3.Connection) -> None:
    """Admit the 'opaque' session_ref_scheme, and correct the rows.

    Description: ``session_ref_scheme`` used to be answered by
      elimination - anything without an agent prefix was called a uuid -
      so 19 transcripts whose ref is a literal filename stem ('audit',
      'journal') were stored as uuid and counted in the owner's own
      sessions. This step widens the column's CHECK to admit the third,
      MEASURED value and rewrites exactly those rows.

      IT REWRITES NO TABLE. message_appearances references this table
      ON DELETE CASCADE and PRAGMA foreign_keys is a no-op inside the
      transaction every step runs in, so the standard rebuild recipe
      cannot be made safe here. The CHECK is edited in place instead -
      see src/core/message_scheme_repair.py for the full argument and
      the measurements behind it.

      IDEMPOTENT ON BOTH HALVES. The relax is skipped when the stored
      constraint already lists the value, and the backfill is defined as
      "rows whose stored scheme disagrees with the classifier", which is
      empty on a second run. It is also a no-op on an install that
      crossed v16..v18 with the message archive gated off and therefore
      has no message_transcripts table at all.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v19_to_v20(conn)  # after _step_v18_to_v19
    """
    repair_session_ref_schemes(conn)


def _step_v20_to_v21(conn: sqlite3.Connection) -> None:
    """Give every transcript the timestamp its PROJECT is ordered by.

    Description: adds ``message_transcripts.newest_message_ts`` and fills
      it with the newest ``message_bodies.ts`` reachable from that
      transcript, so the project rail can sort by when the owner last
      WORKED in a project rather than by when this tool collected the
      files. Those are different facts, and measured on the live corpus
      the collection date puts all 80 projects on two days while the work
      date spreads across nine months.

      THIS STEP IS THE EXPENSIVE ONE, ON PURPOSE. It scans
      ``message_appearances`` once (3.1M rows, about 15s cold on a 22 GB
      archive). That is the whole point: paying it once here is what
      lets the route stay at ~10ms instead of paying 3.9s on every
      paint. See src/core/message_activity.py for the numbers and for
      why no index removes the cost.

      ADD COLUMN ONLY - no table is rewritten and nothing cascades, so
      the hazard that forced step 19 -> 20 to edit the schema text in
      place does not apply. Idempotent, and a no-op on an install with
      the message archive gated off and therefore no transcripts table.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v20_to_v21(conn)  # after _step_v19_to_v20
    """
    install_transcript_activity(conn)


def _step_v21_to_v22(conn: sqlite3.Connection) -> None:
    """Make transcript ingest idempotent on CONTENT, not on path.

    Description: adds the index on ``transcript_archives.content_sha256``
      that the content-addressed idempotency check reads on every file of
      every pass, and the nullable ``dedupe_kind`` column that names why
      a row stores a sentinel instead of its own bytes. See
      src/core/transcript_content_dedupe.py for the mechanism and
      db_models' v22 block for the 3.78 GB re-archive that made a
      path-keyed check untenable.

      ADD COLUMN plus CREATE INDEX. No table is rewritten, nothing is
      backfilled, and every pre-existing row keeps NULL ``dedupe_kind``,
      which reads as "this row holds its own bytes" - the truth for every
      row written before this step.

      IDEMPOTENT ON BOTH HALVES: the index carries its own IF NOT EXISTS,
      and the ALTER is guarded by ``column_exists`` because SQLite's
      ALTER TABLE ADD COLUMN has none - same idiom as v3/v10/v11/v13/v15.
      It is also a no-op on an install whose transcript tables were never
      created.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v21_to_v22(conn)  # after _step_v20_to_v21
    """
    conn.execute(DDL_V22_TRANSCRIPT_ARCHIVES_CONTENT_SHA_INDEX)
    if not column_exists(conn, "transcript_archives", "dedupe_kind"):
        conn.execute(DDL_V22_TRANSCRIPT_ARCHIVES_DEDUPE_KIND)


def _step_v22_to_v23(conn: sqlite3.Connection) -> None:
    """Add ``sessions.last_work_at``, the session/project ordering key.

    Description: one nullable column, no backfill and no index. See
      db_models' v23 block for why work needed a column of its own rather
      than a reuse of ``updated_at``, ``activity_state_at`` or
      ``last_seen_running_at``.

      NOTHING IS BACKFILLED, DELIBERATELY. Every pre-existing row keeps
      NULL, and NULL means "no work has been recorded for this session",
      which is the truth for all of them - nothing was measuring work
      before this step. Seeding them from ``created_at`` or
      ``last_seen_running_at`` would manufacture a work history that never
      happened, and it would do it in exactly the column the lists are
      about to be ordered by.

      IDEMPOTENT: guarded by ``column_exists`` because SQLite's ALTER
      TABLE ADD COLUMN has no IF NOT EXISTS - same idiom as
      v3/v10/v11/v13/v15/v22. A no-op on an install whose sessions table
      was never created (pre-v2).
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v22_to_v23(conn)  # after _step_v21_to_v22
    """
    if not table_exists(conn, "sessions"):
        return
    if not column_exists(conn, "sessions", "last_work_at"):
        conn.execute(DDL_V23_SESSIONS_LAST_WORK_AT)



def _step_v23_to_v24(conn: sqlite3.Connection) -> None:
    """Re-key group membership on ``session_uuid``, and give it an order.

    Description: creates ``session_group_membership`` and carries every
      v8 ``session_group_members`` row onto it, resolving each tmux NAME
      to the ``sessions`` row the sidebar was drawing when the user filed
      it. The resolution rule, its three outcomes and why the seed order
      is what it is all live in
      src/core/session_group_membership_migrate.py; db_models' v24 block
      carries the argument for the key itself.

      THE OLD TABLE IS NOT TOUCHED. Not dropped, not renamed, not
      retyped - read once, here, and never again. That keeps this step
      additive like every other one, so a rollback stays a RESTORE from a
      verified backup rather than a hand-written reversal, and it leaves
      the pre-migration filing readable if anybody ever needs to check
      what this step decided.

      IDEMPOTENT ON BOTH HALVES. Each DDL statement carries its own
      IF NOT EXISTS, and ``carry_memberships`` skips any session_uuid the
      new table already holds - so a re-run after an interrupted attempt
      finishes the remainder and never overwrites a filing the user has
      made since.

      A NO-OP ON AN INSTALL WITHOUT GROUPS. Both source tables are
      checked: an install that crossed v8 with the tables absent (or a
      database predating v2's ``sessions``) gets the new table and no
      backfill, which is the truth for it.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v23_to_v24(conn)  # after _step_v22_to_v23
    """
    for statement in DDL_V24:
        conn.execute(statement)
    if not table_exists(conn, "session_group_members"):
        return
    if not table_exists(conn, "sessions"):
        return
    carried = carry_memberships(conn)
    outcomes: Dict[str, int] = {}
    for entry in carried:
        outcomes[entry.outcome] = outcomes.get(entry.outcome, 0) + 1
    logger.info(
        "session_group_membership_rekeyed",
        legacy_rows=len(carried),
        **outcomes,
    )


def _step_v24_to_v25(conn: sqlite3.Connection) -> None:
    """Add ``sessions.kind``, and stamp the rows this app made itself.

    Description: gives every session row a place to record whether a
      HUMAN drove the conversation or machinery did. The owner's rule,
      verbatim: "lists should always just be mine. the rest can be found
      in the archive explorer." src/core/session_kind.py owns the
      three-word vocabulary and the evidence ladder behind it.

      THE COLUMN IS NULLABLE WITH NO SQL DEFAULT, ON PURPOSE. SQLite
      backfills a column default into every existing row, which would
      have written 'interactive' onto all 895 imported rows and destroyed
      the only thing that distinguishes "classified" from "never looked
      at". NULL is that distinction, and because every reader excludes on
      ``kind = 'automated'`` alone, NULL keeps a row in the lists. An
      unwritten value can never hide a session.

      THE ONE BACKFILL IS A MEASUREMENT, NOT A GUESS. Rows with
      ``origin`` 'created' or 'adopted' were launched or attached by the
      owner through this app; it has no other way to make one. Rows with
      ``origin = 'imported'`` are left NULL for
      scripts/classify_session_kind.py, which reads the transcript rather
      than assuming.

      IDEMPOTENT ON BOTH HALVES: the ALTER is guarded by
      ``column_exists`` because SQLite's ADD COLUMN has no IF NOT EXISTS,
      and the UPDATE carries its own ``kind IS NULL`` clause so a re-run
      after an interrupted attempt never overwrites a classification made
      since. A no-op on an install whose sessions table was never created
      (pre-v2).
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: None.
    Example: _step_v24_to_v25(conn)  # after _step_v23_to_v24
    """
    if not table_exists(conn, "sessions"):
        return
    if not column_exists(conn, "sessions", "kind"):
        conn.execute(DDL_V25_SESSIONS_KIND)
    conn.execute(DDL_V25_SESSIONS_KIND_BACKFILL)

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
    11: _step_v11_to_v12,
    12: _step_v12_to_v13,
    13: _step_v13_to_v14,
    14: _step_v14_to_v15,
    15: _step_v15_to_v16,
    16: _step_v16_to_v17,
    17: _step_v17_to_v18,
    18: _step_v18_to_v19,
    19: _step_v19_to_v20,
    20: _step_v20_to_v21,
    21: _step_v21_to_v22,
    22: _step_v22_to_v23,
    23: _step_v23_to_v24,
    24: _step_v24_to_v25,
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
