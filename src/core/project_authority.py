"""The one place that answers "where do projects come from right now".

THE ANSWER IS ALWAYS cloude.db. Projects used to live in two places -
this table and a mirrored ``projects`` key in config.json kept as a
rollback artifact - and this module's job was to decide which of the two
was speaking, serve the right one, and report when they disagreed. That
second source is gone. ``projects_config_migration`` moves anything the
file still held into the table once, per install, and removes the key.

WHY THE SECOND SOURCE WENT. Two sources need a comparison, a comparison
needs both sides read the same way, and the two sides were NOT read the
same way: the table was read live on every request while config.json came
from ``Settings._auth_config_cache``, which is invalidated only when the
app itself writes the file. So a user who hand-edited config.json - which
the app's own Edit Config menu item invites - got a divergence report
built from a live measurement on one side and a stale one on the other,
and it named disagreements that did not exist on disk. On top of that the
two detectors that fed the UI each formed their own opinion, and shipped
two banners at once that flatly contradicted each other about which
source the user was looking at.

Neither defect is fixed by a better comparison. Both are removed by
having nothing to compare.

TWO OUTCOMES, AND THE SECOND IS NOT "ZERO PROJECTS":

  db              cloude.db opened and answered. Authoritative. Writes
                  allowed. An empty list here is a real, measured empty
                  list.
  db_unreadable   cloude.db could not be opened or could not be read.
                  The list is EMPTY because nothing could be read, not
                  because nothing is there, and the mode plus the message
                  both say so. Writes are refused: a write in this state
                  could not detect a conflict and could not record itself
                  anywhere durable.

That second mode is the reason removing config.json as a fallback did not
remove a state. The old ``config_fallback`` mode was an ANSWER to the
question "what do we show when the datastore is unreachable". Deleting
the answer leaves the question, and answering it with a bare empty list
would be exactly the false green this subsystem exists to kill - a
could-not-look rendered as a nothing-is-wrong.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from src.core.db import DatastoreUnreadableError, connect, db_path_for
from src.core.project_reconcile import reconcile_summary
from src.core.project_writes import list_projects_ordered

logger = structlog.get_logger()

# The named modes. Each is a distinct outcome; neither is a boolean in
# disguise.
MODE_DB = "db"
MODE_DB_UNREADABLE = "db_unreadable"

# Modes in which a project write must be refused. Listed rather than
# derived from "not MODE_DB" so that adding a future mode forces an
# explicit decision about whether it may write.
READONLY_MODES = (MODE_DB_UNREADABLE,)


class ProjectsReadOnlyError(RuntimeError):
    """A project write was attempted while the datastore was unreachable.

    Description: carries the mode and the underlying detail so the HTTP
      layer can render the specific reason rather than a generic 503.
    Inputs (constructor): mode (str), detail (str).
    Output: a ProjectsReadOnlyError instance.
    """

    def __init__(self, mode: str, detail: str) -> None:
        super().__init__(detail)
        self.mode = mode
        self.detail = detail


@dataclass(frozen=True)
class ProjectsView:
    """The resolved answer to one project read, with its provenance.

    Description: the list and the reason the list is what it is travel
      together, always. A caller cannot obtain the projects without also
      obtaining the mode they came from, which is what makes it
      impossible to render a could-not-read as an empty-but-healthy list
      by forgetting a second lookup.
    Inputs (constructor): mode (str - one of the MODE_* constants),
      projects (list[dict] - each ``{"id", "name", "path", "description",
      "root", "agent_type"}``), message (str - one sentence for a user),
      detail (str | None - the underlying error, when there is one),
      reconcile (dict | None - what the last startup project reconcile
      did; None only when the database could not be read).
    Output: a ProjectsView instance.
    """

    mode: str
    projects: List[Dict[str, Any]] = field(default_factory=list)
    message: str = ""
    detail: Optional[str] = None
    reconcile: Optional[Dict[str, Any]] = None

    @property
    def writable(self) -> bool:
        """Whether a project mutation may proceed in this mode.

        Inputs: none.
        Output: bool - False for every READONLY_MODES member.
        """
        return self.mode not in READONLY_MODES

    @property
    def degraded(self) -> bool:
        """Whether this read failed to reach the authoritative source.

        Inputs: none.
        Output: bool.
        """
        return self.mode != MODE_DB

    def to_dict(self) -> Dict[str, Any]:
        """Render the authority block for GET /projects/authority.

        Description: carries no diff and no config path, deliberately.
          There is one source, so there is nothing to compare it
          against, and a ``diff: null`` left behind would leave a client
          rendering "cannot determine" forever about a question nobody
          asks any more - furniture, not a monitor.
        Inputs: none.
        Output: dict.
        """
        return {
            "mode": self.mode,
            "writable": self.writable,
            "degraded": self.degraded,
            "message": self.message,
            "detail": self.detail,
            "project_count": len(self.projects),
            # WHAT THE LAST STARTUP RECONCILE DID. A repair the user
            # cannot see is the same shape as the defect it fixes: a
            # correct-looking screen and no account of what happened to
            # his projects. Carries its own "cannot_determine" so a
            # client can tell "nothing needed doing" from "this has never
            # run" without inferring either.
            "reconcile": self.reconcile,
        }


def _row_to_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project one authoritative table row into the API's project shape.

    Inputs: row (dict) - a ``projects`` table row.
    Output: dict - ``{"id", "name", "path", "description", "root",
      "agent_type"}``.
    """
    return {
        "id": row["id"],
        "name": row["display_name"],
        "path": row["raw_path"],
        "description": row["description"],
        "root": row["root"],
        "agent_type": row["default_agent_type"],
    }


def resolve_projects(state_dir: Path) -> ProjectsView:
    """Read the project list from the datastore, or say it could not be.

    Description: the single read entry point. Takes no config list and
      has no fallback source - that is the enforcement, not an omission.
      While the signature accepted a config list, any caller could hand
      one in and this module could grow a second opinion about where
      projects come from, which is the exact condition that produced two
      contradictory banners.

      Opens cloude.db with ``create=False`` - never create=True, for the
      reason db_health.py gives: opening with creation turns "your
      database is missing" into a brand-new empty file that renders as a
      healthy install containing none of your work.
    Inputs: state_dir (Path) - where cloude.db lives.
    Output: ProjectsView.
    Example: resolve_projects(settings.get_state_dir()).mode -> "db"
    """
    db_file = db_path_for(state_dir)

    try:
        with closing(connect(db_file, create=False)) as conn:
            rows = list_projects_ordered(conn)
            reconcile = reconcile_summary(conn)
    except DatastoreUnreadableError as exc:
        return _unreadable(str(exc), db_file)
    except Exception as exc:  # noqa: BLE001 - a read surface must not 500
        return _unreadable(f"{type(exc).__name__}: {exc}", db_file)

    return ProjectsView(
        mode=MODE_DB,
        projects=[_row_to_view(row) for row in rows],
        message="projects are served from cloude.db, which is authoritative.",
        reconcile=reconcile,
    )


def _unreadable(detail: str, db_file: Path) -> ProjectsView:
    """Build the degraded read-only view for an unreachable datastore.

    Description: the empty list here means "nothing could be read", and
      the message says that in words rather than leaving the reader to
      infer it from an empty screen. There is no longer a second copy to
      serve instead, so the honest thing is to name the failure and
      refuse writes until it clears.
    Inputs: detail (str) - the underlying failure. db_file (Path).
    Output: ProjectsView in MODE_DB_UNREADABLE.
    """
    logger.warning(
        "projects_datastore_unreadable", db_path=str(db_file), error=detail
    )
    return ProjectsView(
        mode=MODE_DB_UNREADABLE,
        projects=[],
        message=(
            "cloude.db is UNREACHABLE, so your projects CANNOT BE READ "
            "right now. This is NOT a claim that you have no projects - "
            "the list is empty because nothing could be read, not because "
            "nothing is there. Adding, renaming and removing projects is "
            "refused until the datastore answers again."
        ),
        detail=detail,
    )


def require_writable(view: ProjectsView) -> None:
    """Refuse a mutation unless the authoritative source is answering.

    Description: called at the top of every write path. Raises rather
      than returning a flag so a caller cannot proceed by forgetting to
      check the result.
    Inputs: view (ProjectsView) - from a fresh ``resolve_projects`` call.
    Output: None.
    Raises: ProjectsReadOnlyError - when ``view.writable`` is False.
    Example: require_writable(resolve_projects(state_dir))
    """
    if not view.writable:
        raise ProjectsReadOnlyError(view.mode, view.detail or view.message)
