"""API routes for user-defined session-sidebar groups.

Deliberately thin: request/response shape, auth, error translation, and
the datastore connection dance. Every rule about what a group IS lives
in ``src/core/session_group_store.py``, and this module never re-decides
one.

Mounted under ``/api/v1`` from ``src/main.py`` alongside the other
routers. Its own routes are a separate module rather than more lines in
``src/api/routes.py`` because that file is already 2687 lines, well past
the project's 500-line budget, and adding to it makes an existing
problem worse.

THREE OUTCOMES ON EVERY READ, and it is the whole reason ``status``
exists on the response instead of the route just returning a list:

  'ok'          the tables were read. ``groups`` reflects them, and an
                empty list genuinely means this install has no groups.
  'unavailable' the datastore is absent, predates v8, or would not open.
                ``groups`` is ``[]`` AND ``status`` says why. A client
                that renders this as "no groups" is drawing a conclusion
                nobody measured, so the client is required to read the
                field - see client/js/session-sidebar-group-store.js,
                which refuses to draw group chrome at all in this state
                rather than drawing an empty group list.

A 200 with an empty list and a 200 with ``status='unavailable'`` are the
same HTTP status on purpose: the sidebar must keep working - ungrouped -
when groups cannot be read, and turning that into a 503 would make an
unreadable group table break the session list itself. The distinction
lives in the body, where the client can act on it, rather than in a
status code that would take the panel down.

WRITES ARE THE OPPOSITE. A write that could not happen returns 503, not
a cheerful 200, because a silently-dropped assignment is a group the
user watched themselves make that is not there after a reload.
"""

from __future__ import annotations

from contextlib import closing
from typing import List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from src.api.auth import require_auth
from src.core import session_group_store as store
from src.core.db import DatastoreUnreadableError, connect, db_path_for

logger = structlog.get_logger()

router = APIRouter(prefix="/session-groups", tags=["session-groups"])

#: Read outcome: the tables answered.
STATUS_OK = "ok"

#: Read outcome: CANNOT DETERMINE. Never collapsed into an empty list.
STATUS_UNAVAILABLE = "unavailable"


class SessionGroupModel(BaseModel):
    """One group as the sidebar receives it.

    ``members`` carries tmux names, which is the key the sidebar's rows,
    its pin set and its manual order all already use.
    """

    group_uuid: str = Field(..., description="Stable public identity")
    name: str = Field(..., description="User's label, already trimmed")
    position: int = Field(..., description="Sort key, ascending")
    members: List[str] = Field(
        default_factory=list, description="tmux names filed in this group"
    )


class SessionGroupsResponse(BaseModel):
    """Every group, plus an explicit verdict about whether that is known.

    ``status='unavailable'`` with ``groups=[]`` is NOT the same claim as
    ``status='ok'`` with ``groups=[]``, and the client is required to
    tell them apart.
    """

    status: str = Field(..., description="'ok' | 'unavailable'")
    groups: List[SessionGroupModel] = Field(default_factory=list)
    detail: Optional[str] = Field(
        default=None, description="Why, whenever status != 'ok'"
    )


class CreateGroupRequest(BaseModel):
    name: str = Field(..., description="Label, trimmed and bounded server-side")


class RenameGroupRequest(BaseModel):
    name: str


class AssignRequest(BaseModel):
    """Move one session into a group, or out of every group.

    ``group_uuid=None`` means ungrouped, which is expressed as the
    ABSENCE of a membership row rather than as a row naming a sentinel
    group. This one body is what the drag, the row menu and the keyboard
    picker all send, so the three cannot drift apart in what a move
    means.
    """

    tmux_name: str
    group_uuid: Optional[str] = None


class ReorderRequest(BaseModel):
    """The WHOLE desired group order, not a move-one-step delta.

    A delta lets the client and the database disagree about the result of
    a sequence of moves; a full list cannot.
    """

    group_uuids: List[str]


def _db_path():
    """Where this install's datastore lives.

    Inputs: none. Output: pathlib.Path.
    """
    from src.config import settings

    return db_path_for(settings.get_state_dir())


def _read_groups() -> List[store.SessionGroup]:
    """Open, list, close - all on ONE worker thread.

    Description: sqlite3 connections are thread-affine
      (``check_same_thread`` defaults to True in ``src.core.db.connect``),
      so connect/use/close must happen inside a single
      ``run_in_threadpool`` call rather than three separate ones.
    Inputs: none.
    Output: list[store.SessionGroup].
    Raises: DatastoreUnreadableError, store.GroupsUnavailable, OSError.
    """
    with closing(connect(_db_path(), create=False)) as conn:
        return store.list_groups(conn)


def _to_models(groups: List[store.SessionGroup]) -> List[SessionGroupModel]:
    """Map store rows onto the wire shape.

    Inputs: groups (list[store.SessionGroup]).
    Output: list[SessionGroupModel].
    """
    return [
        SessionGroupModel(
            group_uuid=g.group_uuid,
            name=g.name,
            position=g.position,
            members=list(g.members),
        )
        for g in groups
    ]


@router.get("", response_model=SessionGroupsResponse, dependencies=[Depends(require_auth)])
async def list_session_groups() -> SessionGroupsResponse:
    """Every group and its membership, or an explicit CANNOT DETERMINE.

    Description: THE READ THE SIDEBAR MAKES ON EVERY POLL. Returns 200 in
      both outcomes on purpose - see the module docblock. An unreadable
      group table must leave the conversation list working, and a 503
      here would take the whole panel down over a feature the user may
      not even be using.
    Inputs: none.
    Output: SessionGroupsResponse.
    """
    path = _db_path()
    if not path.exists():
        return SessionGroupsResponse(
            status=STATUS_UNAVAILABLE,
            groups=[],
            detail="datastore has not been created yet",
        )
    try:
        groups = await run_in_threadpool(_read_groups)
    except (DatastoreUnreadableError, store.GroupsUnavailable) as exc:
        logger.info("session_groups_unavailable", reason=str(exc))
        return SessionGroupsResponse(
            status=STATUS_UNAVAILABLE, groups=[], detail=str(exc)
        )
    except OSError as exc:
        logger.warning("session_groups_read_failed", error=str(exc))
        return SessionGroupsResponse(
            status=STATUS_UNAVAILABLE, groups=[], detail=str(exc)
        )
    return SessionGroupsResponse(status=STATUS_OK, groups=_to_models(groups))


async def _write(operation, *args, **kwargs):
    """Run one store write on a worker thread and translate its failures.

    Description: THE ONE PLACE EVERY WRITE'S ERRORS ARE MAPPED, so a new
      endpoint cannot invent a different status code for the same
      condition. Unlike the read above, an unavailable datastore is a
      503: a write that could not happen must never answer 200, because
      a silently-dropped assignment is a group the user watched
      themselves make that is gone after a reload.
    Inputs: operation (callable) - takes a live connection first.
      args, kwargs - passed through after the connection.
    Output: whatever ``operation`` returns.
    Raises: HTTPException - 400 invalid name, 404 no such group,
      409 group limit reached, 503 datastore unavailable.
    """

    def _run():
        with closing(connect(_db_path(), create=False)) as conn:
            return operation(conn, *args, **kwargs)

    try:
        return await run_in_threadpool(_run)
    except store.GroupNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except store.GroupNameInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except store.GroupLimitReached as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (DatastoreUnreadableError, store.GroupsUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except store.SessionGroupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        logger.warning("session_groups_write_failed", error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("", response_model=SessionGroupsResponse, dependencies=[Depends(require_auth)])
async def create_session_group(body: CreateGroupRequest) -> SessionGroupsResponse:
    """Create a group and return the whole list.

    Description: returns the FULL list rather than just the new group, so
      the client re-renders from one authoritative payload instead of
      splicing a row into a list it already held and hoping the two
      agree. Every write endpoint here does the same.
    Raises: HTTPException 400 (bad name), 409 (limit), 503.
    """
    await _write(store.create_group, body.name)
    return await list_session_groups()


@router.patch(
    "/{group_uuid}",
    response_model=SessionGroupsResponse,
    dependencies=[Depends(require_auth)],
)
async def rename_session_group(
    group_uuid: str, body: RenameGroupRequest
) -> SessionGroupsResponse:
    """Rename a group. Membership and position are untouched.

    Raises: HTTPException 400 (bad name), 404, 503.
    """
    await _write(store.rename_group, group_uuid, body.name)
    return await list_session_groups()


class DeleteGroupResponse(SessionGroupsResponse):
    """The list after the delete, plus how many sessions were freed.

    ``freed`` exists so the UI can say "3 conversations moved to other"
    rather than reporting a deletion whose consequence it cannot name.
    """

    freed: int = Field(
        default=0, description="Memberships cleared; those sessions are ungrouped"
    )


@router.delete(
    "/{group_uuid}",
    response_model=DeleteGroupResponse,
    dependencies=[Depends(require_auth)],
)
async def delete_session_group(group_uuid: str) -> DeleteGroupResponse:
    """Delete a group. ITS CONVERSATIONS ARE NOT DELETED.

    Description: the members become ungrouped and render in OTHER from
      the next paint. See ``session_group_store.delete_group`` - the
      membership rows go with the group both explicitly and by
      ``ON DELETE CASCADE``, and neither table holds a reference to a
      conversation that could cascade into one.
    Raises: HTTPException 404, 503.
    """
    freed = await _write(store.delete_group, group_uuid)
    listed = await list_session_groups()
    return DeleteGroupResponse(
        status=listed.status,
        groups=listed.groups,
        detail=listed.detail,
        freed=int(freed),
    )


@router.post(
    "/assign",
    response_model=SessionGroupsResponse,
    dependencies=[Depends(require_auth)],
)
async def assign_session_group(body: AssignRequest) -> SessionGroupsResponse:
    """File one session into a group, or return it to ungrouped.

    Description: the single write behind ALL THREE ways to move a
      session - the pointer drag, the row menu's group picker, and the
      keyboard picker - so they cannot come to mean different things.
    Raises: HTTPException 400 (empty tmux_name), 404 (no such group), 503.
    """
    await _write(store.assign, body.tmux_name, body.group_uuid)
    return await list_session_groups()


@router.post(
    "/order",
    response_model=SessionGroupsResponse,
    dependencies=[Depends(require_auth)],
)
async def reorder_session_groups(body: ReorderRequest) -> SessionGroupsResponse:
    """Rewrite the group order from a full list of uuids.

    Raises: HTTPException 404 (a uuid naming no group), 503.
    """
    await _write(store.set_group_order, body.group_uuids)
    return await list_session_groups()
