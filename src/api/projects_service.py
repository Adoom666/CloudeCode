"""HTTP-facing glue between the project routes and the authority layer.

Kept out of src/api/auth.py so the routes stay thin and this logic - which
is where the authority inversion actually bites - is readable on its own.
Every function here does the same two things in the same order:

  1. resolve the current authority mode (``resolve_projects``),
  2. refuse the operation if that mode is not writable, otherwise
     mutate the database.

THERE IS NO THIRD STEP ANY MORE. There used to be: every mutation also
rewrote config.json's ``projects`` key as a rollback snapshot. Projects
are DB-only now, so the commit IS the whole write. Nothing mirrors it,
nothing can fall out of step with it, and there is no second store whose
staleness has to be reported alongside a successful change.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from fastapi import HTTPException

from src.core import project_authority as authority
from src.core.project_authority import (
    ProjectsReadOnlyError,
    ProjectsView,
    require_writable,
    resolve_projects,
)

logger = structlog.get_logger()




def current_view(settings: Any) -> ProjectsView:
    """Resolve the project list and the authority mode it came from.

    Inputs: settings (Any) - the Settings singleton.
    Output: ProjectsView.
    Example: current_view(settings).mode -> "db"
    """
    return resolve_projects(settings.get_state_dir())



def readonly_http_error(exc: ProjectsReadOnlyError) -> HTTPException:
    """Translate a refused write into the HTTP response for it.

    Description: 503, not 500. The server is fine; the datastore it needs
      is not, and the condition is expected to clear. The body names the
      mode so a client can render "read-only rollback mode" rather than a
      generic outage.
    Inputs: exc (ProjectsReadOnlyError).
    Output: HTTPException - status 503.
    """
    return HTTPException(
        status_code=503,
        detail=(
            "cloude.db is unreachable, so project changes are refused. "
            "Projects live only in cloude.db, so there is nothing else to "
            f"apply this to. ({exc.detail})"
        ),
    )


def open_db_or_503(settings: Any):
    """Open the authoritative database for a write, or raise 503.

    Description: the write path opens its own connection rather than
      reusing the one ``resolve_projects`` used and closed, because
      sqlite3 connections are thread-affine and the two calls can land on
      different worker threads.
    Inputs: settings (Any) - the Settings singleton.
    Output: sqlite3.Connection - caller must close it.
    Raises: HTTPException 503 - the database could not be opened.
    """
    from src.core.db import DatastoreUnreadableError, connect, db_path_for

    try:
        return connect(db_path_for(settings.get_state_dir()), create=False)
    except DatastoreUnreadableError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"cloude.db is unreachable, so the change was refused. ({exc})",
        )


def guard_writable(settings: Any) -> ProjectsView:
    """Resolve the authority mode and refuse the request if it is read-only.

    Inputs: settings (Any) - the Settings singleton.
    Output: ProjectsView - always writable when this returns.
    Raises: HTTPException 503 - the mode forbids writes.
    """
    view = current_view(settings)
    try:
        require_writable(view)
    except ProjectsReadOnlyError as exc:
        raise readonly_http_error(exc)
    return view



def authority_payload(settings: Any) -> Dict[str, Any]:
    """Render the full authority + disagreement report.

    Description: the body of GET /projects/authority. Carries the mode
      and whether writes are allowed. It no longer carries a
      DB-versus-config diff or a config path, because projects live in
      exactly one place and there is nothing to compare that place
      against.
    Inputs: settings (Any) - the Settings singleton.
    Output: dict.
    Example: authority_payload(settings)["mode"] -> "db"
    """
    return current_view(settings).to_dict()


def views_to_responses(view: ProjectsView, response_cls: Any) -> List[Any]:
    """Render a ProjectsView's projects as the API's response models.

    Inputs: view (ProjectsView). response_cls (type) - ProjectResponse.
    Output: list - one response model per project, order preserved.
    """
    return [
        response_cls(
            id=item["id"],
            name=item["name"],
            path=item["path"],
            description=item["description"],
            root=item["root"],
        )
        for item in view.projects
    ]


def resolve_target(conn: Any, name: str) -> Dict[str, Any]:
    """Turn a display name from a URL path into exactly one project row.

    Description: translates the write layer's two distinct lookup
      failures into two distinct HTTP statuses. An ambiguous name is 409,
      not 404 and not a silent pick of the first row - the project the
      user meant does exist, the server just cannot tell which one, and
      that is a conflict the user has to resolve by renaming.
    Inputs: conn (sqlite3.Connection). name (str).
    Output: dict - the matching row.
    Raises: HTTPException 404 or 409.
    """
    from src.core.project_writes import (
        ProjectNameAmbiguous,
        ProjectNotFound,
        resolve_by_name,
    )

    try:
        return resolve_by_name(conn, name)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    except ProjectNameAmbiguous as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{name}' does not identify one project: {exc}. "
                "Rename one of them before editing either."
            ),
        )


def touch_project_best_effort(settings: Any, working_dir: str) -> Optional[Dict]:
    """Mark a project most-recently-opened in the datastore.

    Description: the replacement for ``Settings.move_project_to_top``,
      called when a session starts in a directory. Best-effort in the
      same way the original was - a session must never fail to start
      because the launcher's ordering could not be updated - but unlike
      the original it distinguishes "no project at that path" (a normal
      miss, returns None) from "the datastore could not be reached" (a
      warning in the log), rather than swallowing both in one bare
      ``except Exception``.
    Inputs: settings (Any) - the Settings singleton. working_dir (str).
    Output: dict | None - the touched row, or None if nothing matched or
      the update could not be attempted.
    """
    from contextlib import closing

    from src.core.project_writes import touch_project_by_path

    try:
        with closing(open_db_or_503(settings)) as conn:
            row = touch_project_by_path(conn, working_dir)
    except HTTPException as exc:
        logger.warning(
            "project_touch_datastore_unreachable",
            working_dir=working_dir,
            detail=exc.detail,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - never breaks session creation
        logger.warning(
            "project_touch_failed", working_dir=working_dir, error=str(exc)
        )
        return None

    return row


def agent_type_for(settings: Any, project_name: str) -> Optional[str]:
    """Look up a project's default agent type from the authoritative source.

    Description: feat/db-is-authoritative. Reads ``projects``, falling
      back to config.json when the datastore is unreachable, so a session
      launched during a datastore outage still gets the right agent
      rather than silently dropping to the "claude" default. Returns None
      both when the project is unknown and when it has no default; the
      caller treats both the same way, so splitting them here would be a
      distinction with no consumer.
    Inputs: settings (Any) - the Settings singleton. project_name (str) -
      display name as sent with the create-session request.
    Output: str | None - the project's ``default_agent_type``.
    Example: agent_type_for(settings, "CloudeCode") -> "claude"
    """
    view = current_view(settings)
    for item in view.projects:
        if item["name"] == project_name:
            return item["agent_type"]
    return None


__all__ = [
    "agent_type_for",
    "authority_payload",
    "current_view",
    "guard_writable",
    "open_db_or_503",
    "readonly_http_error",
    "resolve_target",
    "touch_project_best_effort",
    "views_to_responses",
    "authority",
]
