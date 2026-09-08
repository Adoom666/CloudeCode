"""Archive and unarchive one project - the soft retirement of a shelf.

Split out of project_writes.py so that file stays inside the 500-line
rule, and because these two are a different VERB from everything in it.
project_writes.py mutates what a project IS (its name, its description,
its existence). This module changes only whether the user is currently
being shown it.

THE VOCABULARY IS DELIBERATELY THE SESSIONS' ONE. ``archived_at``, a
nullable ISO stamp, nullable-means-live, first-stamp-wins, idempotent,
returns bool - identical to ``session_store.archive_session`` down to the
guard in the UPDATE. Inventing a second word (``retired_at``,
``hidden``, a ``status`` enum) for the same idea would leave two archive
concepts in one datastore, and the next reader would have to learn which
tables speak which.

WHAT THEY SHARE IS THE SHAPE, NOT THE MEANING, and the two must not be
confused:

  a session's archive is a DELETE - "take this off my screen", the row
  kept only because transcripts are built on it;

  a project's archive is a SHELF - "I am done with this for now", the
  project still real, still findable, still restorable in one action.

That difference is why this module cascades to nothing. See
``archive_project`` for the full reasoning.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import structlog

from src.core.db import transaction
from src.core.project_writes import ProjectNotFound
from src.core.trail_entry import utc_now

logger = structlog.get_logger()


def archive_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    now: Optional[str] = None,
) -> bool:
    """Retire one project from the default list. Keep the row, and the work.

    Description: the SOFT counterpart to ``delete_project``, and the
      first writer of ``projects.archived_at`` - the column has been in
      the v1 DDL and read by ``list_projects_ordered`` since the table
      existed, with nothing ever setting it.

      IT ARCHIVES THE PROJECT AND NOTHING ELSE. It does not touch the
      folder, does not stop a process, and - the part that matters -
      DOES NOT ARCHIVE THE PROJECT'S SESSIONS. Sessions carry their own
      independent ``archived_at`` written only by
      ``session_store.archive_session``, and nothing here writes it.
      That is deliberate rather than an omission: "I am done with this
      project" is a statement about a SHELF, not about the work sitting
      on it. A dormant project with a live session must stay usable, and
      cascading would turn one reversible click into a bulk delete of
      session history the user never asked for and could not undo in one
      action. A session of an archived project therefore still appears in
      RUNNING and in RECENT exactly as before.

      NO TOMBSTONE, unlike ``delete_project``. A tombstone means "this
      root was deliberately removed, do not re-import it". The row is
      still here and still occupies its UNIQUE root, so the reconcile
      cannot re-import a duplicate and there is nothing for a tombstone
      to prevent. Writing one would additionally make an unarchive
      ambiguous - a live row sitting behind a marker that says it was
      deleted.

      IDEMPOTENT, AND THE FIRST STAMP WINS, matching
      ``session_store.archive_session`` exactly. A second archive returns
      False rather than rewriting the moment the user made the decision.
      False is "already archived", which is a different fact from the
      exception below.
    Inputs: conn (sqlite3.Connection) - opens its own transaction.
      project_id (int). now (str | None) - ISO-8601 stamp; defaults to
      the current UTC time.
    Output: bool - True when this call performed the archive, False when
      the row was already archived.
    Raises: ProjectNotFound - no row with that id, so nothing was
      archived and the caller must not report success.
    Example: archive_project(conn, 3)  # True
    """
    stamp = now or utc_now()

    with transaction(conn):
        row = conn.execute(
            "SELECT archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ProjectNotFound(str(project_id))
        if row["archived_at"] is not None:
            return False
        conn.execute(
            "UPDATE projects SET archived_at = ?, updated_at = ? "
            "WHERE id = ? AND archived_at IS NULL",
            (stamp, stamp, project_id),
        )

    logger.info("project_archived", project_id=project_id, archived_at=stamp)
    return True


def unarchive_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    now: Optional[str] = None,
) -> bool:
    """Bring an archived project back into the default list.

    Description: clears ``archived_at``. THE REVERSE EXISTS ON PURPOSE.
      An archive with no way back is not an archive, it is a delete with
      a friendlier label - and a one-way hide is how a list turns into a
      place work goes missing. Sessions have no unarchive endpoint today
      and reach the same state only as a side effect of being restarted
      (``session_restart.py``); projects do not repeat that gap.

      Like its counterpart it writes NOTHING but this row. It does not
      unarchive sessions, because it did not archive any.

      IDEMPOTENT in the same shape: False means "was already live", which
      the caller must be able to tell apart from "restored just now".
    Inputs: conn (sqlite3.Connection) - opens its own transaction.
      project_id (int). now (str | None) - fixed clock for tests; used
      only for ``updated_at``, since ``archived_at`` is being cleared.
    Output: bool - True when this call performed the restore, False when
      the row was not archived.
    Raises: ProjectNotFound - no row with that id.
    Example: unarchive_project(conn, 3)  # True
    """
    stamp = now or utc_now()

    with transaction(conn):
        row = conn.execute(
            "SELECT archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ProjectNotFound(str(project_id))
        if row["archived_at"] is None:
            return False
        conn.execute(
            "UPDATE projects SET archived_at = NULL, updated_at = ? "
            "WHERE id = ? AND archived_at IS NOT NULL",
            (stamp, project_id),
        )

    logger.info("project_unarchived", project_id=project_id, at=stamp)
    return True


