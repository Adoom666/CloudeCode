"""Turn accepted proposals into rows. THE ONLY CODE HERE THAT WRITES.

Everything upstream of this module - ``transcript_import_facts`` and
``transcript_import_plan`` - is a pure read, so a dry run is not a
special mode with its own code path that could disagree with the real
one. It is the same pass with this module not called.

FOUR GUARANTEES, and each is enforced here rather than promised in a
docstring somewhere else.

  1. A uuid a ``sessions`` row already holds is SKIPPED, never
     overwritten. The check is re-taken inside the write transaction,
     not trusted from the plan, because the plan was built against a
     snapshot and this is a live database with a running app on it.
     ``ux_sessions_claude_uuid`` would refuse the insert anyway; the
     check exists so the outcome is a counted skip rather than an
     exception that aborts the batch.
  2. A project root a ``projects`` row already holds is REUSED, never
     re-created. Same reason, same re-check.
  3. Every row an import writes is ARCHIVED - ``archived_at`` set on the
     session AND on any project it creates. The owner's model is
     "archived items are not visible unless the checkbox is checked", so
     1,100 recovered conversations arrive out of the way and he turns
     them on. Importing them VISIBLE would bury the sessions he is
     actually working in, which is the same harm as losing them.
  4. NOTHING IS INVENTED. The columns an imported row does not know are
     left NULL. See ``transcript_import_plan.IMPORTED_ROW_CONSTANTS``.

DISPLAY NAMES ARE DISAMBIGUATED BY PATH, not by a counter. Two of the
owner's directories are both called ``media-cleanup-qnap`` and two are
both called ``document_catalog``; a project list holding
``media-cleanup-qnap`` and ``media-cleanup-qnap (2)`` tells him nothing
about which is which. The rule walks UP the path one segment at a time -
``media-cleanup-qnap``, then ``personal/media-cleanup-qnap`` - until the
name is unique, which is the only disambiguator that carries the
information the user needs to tell them apart.
"""

from __future__ import annotations

import os
import sqlite3
import uuid as _uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import structlog

from src.core.transcript_import_plan import (
    IMPORTED_PROJECT_SOURCE,
    IMPORTED_ROW_CONSTANTS,
    ProposedSession,
)
from src.core.session_kind import classify_transcript
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

#: The row was inserted.
WROTE_SESSION = "wrote"

#: Another writer got there first between the plan and the write. Counted,
#: never an error - see guarantee 1.
SKIPPED_HELD = "skipped_held"

#: How many path segments a display name may grow to before the rule
#: gives up and appends the full canonical root. Three is enough to
#: separate every collision measured on the live corpus, and an unbounded
#: walk would eventually produce a name longer than the sidebar.
DISPLAY_NAME_MAX_SEGMENTS = 3


@dataclass(frozen=True)
class ImportReport:
    """What one apply pass actually did.

    Attributes:
        sessions_written: rows inserted into ``sessions``.
        sessions_skipped_held: proposals whose uuid was already held at
            write time.
        projects_created: roots that got a new ``projects`` row.
        projects_reused: roots that already had one.
    """

    sessions_written: int = 0
    sessions_skipped_held: int = 0
    projects_created: int = 0
    projects_reused: int = 0


def display_name_for(
    root: str, taken: Sequence[str], *, max_segments: int = DISPLAY_NAME_MAX_SEGMENTS
) -> str:
    """A unique, informative label for one project root.

    Description: the basename, widened one parent segment at a time
      until it is not already taken. See the module docstring for why a
      numeric suffix was rejected. A root whose basename is empty (``/``)
      falls straight through to the full path.
    Inputs: root (str) - canonical path. taken (Sequence[str]) - display
      names already in use. max_segments (int) - how wide the name may
      grow before falling back to the whole root.
    Output: str.
    Example: display_name_for('/a/personal/qnap', ['qnap'])
      # 'personal/qnap'
    """
    used = set(taken)
    parts = [p for p in root.split(os.sep) if p]
    if not parts:
        return root
    for width in range(1, min(max_segments, len(parts)) + 1):
        candidate = os.sep.join(parts[-width:])
        if candidate not in used:
            return candidate
    return root if root not in used else f"{root} ({_uuid.uuid4().hex[:6]})"


def _existing_project_ids(conn: sqlite3.Connection) -> Dict[str, int]:
    """Every project's canonical root mapped to its row id.

    Description: keyed on ``realpath`` of the stored root, because a
      project stored under the short symlinked spelling and a proposal
      canonicalised to the long one are the SAME directory and must not
      produce two rows. That confusion already manufactured two junk
      project rows on this machine.
    Inputs: conn (sqlite3.Connection).
    Output: dict[str, int] - canonical root -> projects.id.
    """
    out: Dict[str, int] = {}
    for row in conn.execute("SELECT id, root FROM projects"):
        try:
            key = os.path.realpath(str(row[1]))
        except OSError:
            key = str(row[1])
        out.setdefault(key, int(row[0]))
    return out


def _display_names(conn: sqlite3.Connection) -> List[str]:
    """Every display name already in use.

    Inputs: conn (sqlite3.Connection).
    Output: list[str].
    """
    return [str(r[0]) for r in conn.execute("SELECT display_name FROM projects")]


def _held_uuids(conn: sqlite3.Connection) -> set:
    """Every ``claude_session_uuid`` a sessions row currently holds.

    Inputs: conn (sqlite3.Connection).
    Output: set[str].
    """
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT claude_session_uuid FROM sessions "
            "WHERE claude_session_uuid IS NOT NULL"
        )
    }


def create_archived_project(
    conn: sqlite3.Connection,
    root: str,
    *,
    display_name: str,
    now: str,
) -> int:
    """Insert one ARCHIVED project row for a directory, and return its id.

    Description: ``presence`` is left at the schema default
      ``'unchecked'`` DELIBERATELY. This importer does not stat the
      directory, so it has not measured whether it is there; writing
      ``present`` would be a claim it did not make and writing
      ``missing`` would be a claim about a path it never looked at. The
      presence checker owns that column and will fill it the first time
      the user asks.

      ``raw_path`` is the canonical root, same as ``root``: there is no
      user-typed spelling to preserve, because no user typed it.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
      root (str) - canonical. display_name (str). now (str) - ISO-8601.
    Output: int - the new projects.id.
    Example: create_archived_project(conn, '/a/b', display_name='b', now=t)
    """
    cursor = conn.execute(
        "INSERT INTO projects "
        "(root, raw_path, display_name, source, archived_at, "
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (root, root, display_name, IMPORTED_PROJECT_SOURCE, now, now, now),
    )
    return int(cursor.lastrowid)



def kind_for_plan(plan: ProposedSession) -> Optional[str]:
    """Classify one proposal's conversation as the owner's or machinery's.

    Description: reads the transcript the proposal was built from and
      returns one of ``session_kind.SESSION_KINDS``. A proposal carrying
      no ``transcript_path`` returns None, which stores as NULL - and
      NULL keeps the row in every list, because "we never looked" and
      "a machine ran this" are different facts and only the second one
      may hide a session.
    Inputs: plan (ProposedSession).
    Output: str | None - 'interactive', 'automated', 'unknown', or None
      when there was no file to read.
    Example: kind_for_plan(plan)  # 'automated'
    """
    if not plan.transcript_path:
        return None
    return classify_transcript(plan.transcript_path).kind


def insert_imported_session(
    conn: sqlite3.Connection,
    plan: ProposedSession,
    *,
    project_id: int,
    now: str,
) -> str:
    """Insert one ARCHIVED session row for a recovered conversation.

    Description: the THIRD ``INSERT INTO sessions`` in this codebase,
      alongside ``session_identity.record_instance`` (a tmux instance we
      have just seen) and ``session_lineage.record_claude_session`` (a
      conversation inside one). It is its own path because neither of
      those can honestly describe this row: there is no instance to
      record and no parent to hang lineage off.

      ``kind`` IS MEASURED FROM THE TRANSCRIPT, not assumed. The
      importer walks this machine's whole corpus, and that corpus holds
      scheduler runs and headless ``claude -p`` probes alongside the
      owner's own work; giving them all the same kind would put 270
      machine-run conversations (measured 2026-09-08) onto lists whose
      whole purpose is to show the owner HIS sessions. When no path is
      carried, or no marker in the file answers, the column stays NULL -
      and NULL lists, because not having looked is not evidence of
      automation. See src/core/session_kind.py.

      ``created_at`` and ``last_work_at`` come from the TRANSCRIPT, not
      from the clock. A recovered conversation from March belongs in
      March; stamping it now would put 1,100 rows at the top of a list
      ordered by when work happened and destroy the recall that list is
      read for. ``updated_at`` IS now, because that is when this row was
      written, and the two are different questions.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
      plan (ProposedSession) - must have ``writes`` True. project_id
      (int). now (str) - ISO-8601, for ``updated_at`` and
      ``archived_at``.
    Output: str - the new row's ``session_uuid``.
    Raises: sqlite3.IntegrityError - the uuid is already held. The caller
      is expected to have checked; the constraint is the backstop.
    Example: insert_imported_session(conn, plan, project_id=4, now=t)
    """
    session_uuid = str(_uuid.uuid4())
    stamp = plan.created_at or now
    conn.execute(
        "INSERT INTO sessions "
        "(session_uuid, project_id, project_attribution, working_dir, "
        " origin, lifecycle, lifecycle_source, lifecycle_checked_at, "
        " claude_session_uuid, claude_session_uuid_source, title, "
        " archived_at, created_at, updated_at, last_work_at, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_uuid,
            project_id,
            IMPORTED_ROW_CONSTANTS["project_attribution"],
            plan.working_dir,
            IMPORTED_ROW_CONSTANTS["origin"],
            IMPORTED_ROW_CONSTANTS["lifecycle"],
            IMPORTED_ROW_CONSTANTS["lifecycle_source"],
            now,
            plan.claude_session_uuid,
            IMPORTED_ROW_CONSTANTS["claude_session_uuid_source"],
            plan.title,
            now,
            stamp,
            now,
            plan.last_work_at,
            kind_for_plan(plan),
        ),
    )
    return session_uuid


def apply_plans(
    conn: sqlite3.Connection,
    plans: Sequence[ProposedSession],
    *,
    now: Optional[str] = None,
) -> ImportReport:
    """Write every proposal that says ``import``, in ONE transaction.

    Description: all or nothing. A partially applied import is the state
      nobody can reason about - some conversations recovered, some not,
      and no way to tell which pass stopped where - and the whole batch
      is a few thousand small inserts against a database whose own daily
      integrity check walks every page, so there is no size argument for
      batching it.

      HELD UUIDS AND EXISTING PROJECTS ARE RE-CHECKED HERE, against the
      live table inside the transaction, not trusted from the plan. The
      plan was built from a read-only snapshot taken minutes earlier on
      a machine with a running app.
    Inputs: conn (sqlite3.Connection) - the caller owns nothing; this
      opens and commits its own transaction. plans (Sequence[
      ProposedSession]). now (str | None) - ISO-8601, for tests.
    Output: ImportReport.
    Example: apply_plans(conn, plans).sessions_written  # 1126
    """
    stamp = now or utc_now()
    writable = [p for p in plans if p.writes]
    report = ImportReport()
    if not writable:
        return report

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        held = _held_uuids(conn)
        project_ids = _existing_project_ids(conn)
        names = _display_names(conn)
        created = 0
        reused = 0
        written = 0
        skipped = 0
        for plan in writable:
            if plan.claude_session_uuid in held:
                skipped += 1
                continue
            root = plan.project_root
            project_id = project_ids.get(root)
            if project_id is None:
                name = display_name_for(root, names)
                project_id = create_archived_project(
                    conn, root, display_name=name, now=stamp
                )
                project_ids[root] = project_id
                names.append(name)
                created += 1
            else:
                reused += 1
            insert_imported_session(conn, plan, project_id=project_id, now=stamp)
            held.add(plan.claude_session_uuid)
            written += 1
        report = ImportReport(
            sessions_written=written,
            sessions_skipped_held=skipped,
            projects_created=created,
            projects_reused=reused,
        )
    logger.info(
        "transcript_sessions_imported",
        sessions_written=report.sessions_written,
        sessions_skipped_held=report.sessions_skipped_held,
        projects_created=report.projects_created,
        projects_reused=report.projects_reused,
    )
    return report
