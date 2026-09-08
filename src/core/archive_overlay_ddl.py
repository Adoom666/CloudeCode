"""Schema v19: the PRESENTATION OVERLAY over the immutable archive.

THE OWNER'S REQUIREMENT, AND WHY IT IS ONE MECHANISM AND NOT THREE. He
asked to rename projects, to group them ("if projects are a part of a
larger group") and to soft-delete them ("some look extremely empty or
bullshit"), and he was explicit that renaming is PRESENTATION ONLY. Those
are not three features. They are three STATEMENTS ABOUT a project, and a
statement about a project is exactly what the archive does not hold - the
archive holds what was ingested, byte for byte, and nothing else. So this
is one table carrying one row per project the owner has said something
about, joined at READ time and never written back into the archive.

NOTHING HERE MAY EVER TOUCH message_projects, message_transcripts,
message_bodies, message_appearances OR message_content_blocks. Not a
rename, not a flag, not a backfill. The overlay is a SEPARATE TABLE and
the join happens in Python at read time, so there is no code path in
which a presentation change and an archive write are the same statement.
``tests/test_archive_overlay.py`` asserts this by sha256 of the archive
tables' full contents before and after every overlay operation, because
a comment is an assertion and a hash is a measurement.

--------------------------------------------------------------------------
IDENTITY: WHAT AN OVERLAY ROW ATTACHES TO, AND WHY
--------------------------------------------------------------------------

The obvious key is wrong. ``message_projects`` is ``UNIQUE (corpus_id,
slug)`` and holds 80 rows across 3 corpora on 2 machines (measured
2026-09-01), which merge to 77 LOGICAL projects. Keying the overlay on
``message_projects.id`` would mean renaming a project on one machine and
watching it stay un-renamed on the other, because they are two rows. The
user sees one row in the rail; the overlay must attach to that one row.

The slug is also wrong, and provably so. It replaces ``/`` with ``-`` and
nothing marks which hyphens were separators, so it is NOT invertible:
``Production-bhpp-new-server`` is really ``bhpp_new_server`` and
``Production-dev-tools-scripts`` is three path segments. A slug key would
be a key built on a string nobody can decode.

THE KEY IS THE MERGE KEY - the same discriminated key
``src.core.archive_project_names.merge_projects`` already uses to decide
which rows are one project:

    cwd:<observed_cwd>   when observed_cwd is a non-blank string
    pid:<project_id>     otherwise

That is deliberate and it is the whole design. It means the overlay
cannot disagree with the merge about what one project is, because both
answer the question the same way. It also means an overlay row survives
re-ingest and survives the project appearing on a THIRD machine, since
``observed_cwd`` is the real absolute path and is byte-identical across
hosts for all 3 projects that currently exist on both (checked, not
assumed - see archive_project_names' header).

The two key kinds are NOT the same quality of fact and the API says so
rather than letting a reader guess from the string:

  ``cwd``         portable. Stable across hosts, across re-ingest, and
                  across a rebuild of this database. This is 80 of 80
                  project rows today.
  ``project_id``  LOCAL ONLY. The fallback for a project with no
                  observed_cwd, where there is no evidence it is the same
                  project as anything else. It is stable within this
                  database and meaningless outside it, so a rebuild that
                  renumbers projects orphans the row rather than silently
                  re-attaching it to a different project. Orphaning is the
                  safe direction: a wrong rename is invisible, and a
                  wrongly HIDDEN project is a project the owner cannot
                  find.

WHAT HAPPENS TO AN OVERLAY ROW WHOSE PROJECT IS NOT IN THE CORPUS. It is
an ORPHAN, and it gets all three of the things the three-outcome rule
demands. It is NOT silently dropped, because that would delete the
owner's rename with no notice. It is NOT rendered as a project node,
because a node with no transcripts behind it is a phantom - a project
that does not exist wearing a real one's clothes. It is reported, by key,
in ``meta.overlay.orphans``, so it is visible, restorable and countable.
Nothing is ever deleted to tidy one away; a project that comes back on
re-ingest gets its overlay back automatically, which is the entire point
of keying on identity rather than on a row id.

--------------------------------------------------------------------------
ABSENT IS A STATE, SO A ROW EXISTS ONLY WHEN SOMETHING IS SAID
--------------------------------------------------------------------------

No row means "the owner has never said anything about this project" and
that must stay true, because it is the difference between a project shown
under its real folder name and a project someone deliberately left alone.
So the write path PRUNES a row the moment its last statement is cleared -
a NULL name, a NULL group and hidden=0 together carry no information, and
leaving the row behind would turn "untouched" into "touched, then undone"
for every reader downstream. The distinction is load bearing: it is what
lets the API report ``overlay_status: "none"`` as a measurement.
"""

from __future__ import annotations

from typing import Tuple

#: The table. TEXT primary key on the identity described above; SQLite
#: gives it a unique index for free, which is the lookup this join makes.
#:
#: ``hidden`` is an INTEGER 0/1 with a CHECK rather than a bare integer,
#: so "2" cannot enter the column and force a reader to invent a meaning
#: for it. It is a SOFT delete in the strict sense: it changes which list
#: a project appears in and nothing else. No transcript, body, block or
#: appearance is touched, export still works, and unhide is a single
#: UPDATE that restores the previous state exactly.
DDL_ARCHIVE_PROJECT_OVERLAY = """
CREATE TABLE IF NOT EXISTS archive_project_overlay (
  identity_key   TEXT PRIMARY KEY,
  identity_kind  TEXT NOT NULL
                  CHECK (identity_kind IN ('cwd', 'project_id')),
  display_name   TEXT,
  group_name     TEXT,
  hidden         INTEGER NOT NULL DEFAULT 0
                  CHECK (hidden IN (0, 1)),
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  CHECK (display_name IS NULL OR TRIM(display_name) <> ''),
  CHECK (group_name IS NULL OR TRIM(group_name) <> '')
)
"""

#: Group listing reads ``WHERE group_name IS NOT NULL``; the partial index
#: keeps it off the ungrouped majority. Hidden is the same shape: the
#: interesting rows are the few that are 1, so the index is partial rather
#: than covering a column that is almost entirely one value.
DDL_ARCHIVE_PROJECT_OVERLAY_GROUP_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_archive_project_overlay_group "
    "ON archive_project_overlay (group_name) "
    "WHERE group_name IS NOT NULL"
)

DDL_ARCHIVE_PROJECT_OVERLAY_HIDDEN_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_archive_project_overlay_hidden "
    "ON archive_project_overlay (hidden) "
    "WHERE hidden = 1"
)

#: Everything schema v19 creates, in the order the step applies it. Each
#: statement carries its own IF NOT EXISTS, so re-running the step after
#: an interrupted attempt is a no-op rather than an error - the same
#: idempotence v7/v8/v14/v16/v18 rely on, with no PRAGMA inspection
#: needed. The step CREATEs and does nothing else: no ALTER, no UPDATE,
#: no backfill, and not one read of an archive table. On an 18.7 GB
#: database this migration is O(1).
DDL_V19: Tuple[str, ...] = (
    DDL_ARCHIVE_PROJECT_OVERLAY,
    DDL_ARCHIVE_PROJECT_OVERLAY_GROUP_INDEX,
    DDL_ARCHIVE_PROJECT_OVERLAY_HIDDEN_INDEX,
)

#: The one table this feature writes. Named as a constant so the write
#: path, the read path and the immutability test all say the same string.
OVERLAY_TABLE: str = "archive_project_overlay"

#: The archive tables no overlay operation may write, ever. The
#: immutability test hashes exactly these, and it reads the list from
#: here rather than retyping it, so adding a table to the archive without
#: adding it to this tuple is the only way to escape the check - which is
#: a code review a human can actually perform.
ARCHIVE_TABLES_NEVER_WRITTEN: Tuple[str, ...] = (
    "message_projects",
    "message_transcripts",
    "message_bodies",
    "message_appearances",
    "message_content_blocks",
)
