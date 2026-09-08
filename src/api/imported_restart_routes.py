"""Preview and perform a restart for a session that has NO tmux identity.

``GET /sessions/restart/preview`` and ``POST /sessions/respawn`` both
address a session by its tmux NAME, so neither can reach an IMPORTED row -
one rebuilt from a transcript, which has no name, no epoch and no pane and
never had one. That is 895 of the 938 rows on the owner's box as of
2026-09-08. This module is the pair of endpoints that CAN reach them,
keyed on the durable ``sessions.session_uuid``.

WHY THEY ARE THEIR OWN ROUTES AND NOT A SECOND MODE ON THE EXISTING ONES.
Every parameter, every gate and every failure mode differs. The pane path
answers "what would respawning this pane do", gated on ``pane_dead``,
``#{pane_start_command}`` and a live-kill confirmation. This one answers
"what would creating a session for this conversation do", gated on
whether the transcript is still on disk and which SPELLING of the
directory holds it. Folding them together would put four inapplicable
gates in front of a path that has none of them, and a caller could not
tell which set it was subject to.

THE RESPONSE SHAPE IS THE SAME ON PURPOSE, though. The preview returns
``RestartPreviewResponse``, the model the picker already renders, with
``pane_state`` reporting the honest ``'unknown'``: there is no pane, so
its state is not a fact anybody measured. A client needs no second
renderer, only a second URL.

NOTHING HERE DECIDES ANYTHING. Every rule lives in
``src/core/session_imported_restart.py``; this module reads the row,
calls the validators and translates outcomes into HTTP.
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.api.auth import require_auth
from src.core.db import DatastoreUnreadableError, connect, db_path_for
from src.core.session_agent_choice import (
    resolve_wrapper_offers,
    validate_agent_choice,
)
from src.core.session_imported_restart import (
    is_imported_row,
    imported_extra_args,
    plan_imported_restart,
    resume_directory,
)
from src.core.session_respawn import (
    PANE_UNKNOWN,
    RESPAWN_AGENT,
    RESPAWN_CANNOT_DETERMINE,
)
from src.models import (
    RestartPlanPreview,
    RestartPreviewOption,
    RestartPreviewResponse,
)

logger = structlog.get_logger()

router = APIRouter(tags=["sessions"])


class ImportedRestartRequest(BaseModel):
    """Restart one imported conversation under a chosen wrapper.

    ``agent_type`` is REQUIRED and there is no unpicked path, unlike the
    pane restart. A pane at least has a recorded start command to fall
    back on; an imported row has nothing, so an unpicked restart could
    only ever hand back a bare login shell - the exact outcome the pane
    path's whole warning apparatus exists to stop a user walking into.
    Rather than warn about it, this refuses to offer it.
    """

    session_uuid: str = Field(..., description="sessions.session_uuid")
    agent_type: str = Field(..., description="wrapper id from the preview")


class ImportedRestartResponse(BaseModel):
    """What the restart did, or why it did nothing."""

    status: str = Field(
        ...,
        description=(
            "'started' when a tmux session was created for this "
            "conversation, 'refused' when a gate declined. A refusal is a "
            "200 with this field, never a silent success"
        ),
    )
    session_uuid: str = Field(..., description="the row that was acted on")
    session_id: Optional[str] = Field(
        None, description="the live session id, on 'started' only"
    )
    conversation: str = Field(
        ...,
        description=(
            "'resumed' | 'none_recorded' | 'unknown', derived from the "
            "argv actually run so the claim cannot outrun the command"
        ),
    )
    command: Optional[str] = Field(None, description="what was run")
    working_dir: Optional[str] = Field(
        None, description="the spelling the session was created in"
    )
    detail: str = Field("", description="one sentence, fit to show verbatim")


def _db_path():
    """Where this install's datastore lives.

    Inputs: none. Output: pathlib.Path.
    """
    from src.config import settings

    return db_path_for(settings.get_state_dir())


def _read_row(session_uuid: str) -> Dict[str, Any]:
    """Read one sessions row by its durable key, on a worker thread.

    Description: returns a dict with ``row`` and ``row_read_ok`` rather
      than a bare row, because "the datastore answered and holds no such
      row" and "the datastore could not be read" are different facts and
      every consumer here has to tell them apart.
    Inputs: session_uuid (str).
    Output: dict - ``{'row': dict|None, 'row_read_ok': bool}``.
    """
    try:
        with closing(connect(_db_path(), create=False)) as conn:
            found = conn.execute(
                "SELECT * FROM sessions WHERE session_uuid = ?", (session_uuid,)
            ).fetchone()
        return {
            "row": dict(found) if found is not None else None,
            "row_read_ok": True,
        }
    except (DatastoreUnreadableError, OSError) as exc:
        logger.warning(
            "imported_restart_row_unreadable",
            session_uuid=session_uuid,
            error=str(exc),
        )
        return {"row": None, "row_read_ok": False}


def _require_imported(state: Dict[str, Any], session_uuid: str) -> Dict[str, Any]:
    """The row, or the right HTTP error.

    Description: a row that HAS a tmux identity is a 409 and not a 404 -
      it exists, and the caller used the wrong endpoint for it. Saying
      "not found" would send them looking for a row that is right there.
    Inputs: state (dict) - from :func:`_read_row`. session_uuid (str).
    Output: dict - the row.
    Raises: HTTPException 404, 409, 503.
    """
    if not state["row_read_ok"]:
        raise HTTPException(
            status_code=503,
            detail="the datastore could not be read, so this session's restart could not be evaluated",
        )
    row = state["row"]
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"no session carries the uuid {session_uuid!r}"
        )
    if not is_imported_row(row):
        raise HTTPException(
            status_code=409,
            detail=(
                f"session {session_uuid!r} has a tmux identity "
                f"({row.get('tmux_name')!r}), so it restarts through "
                "GET /sessions/restart/preview and POST /sessions/respawn"
            ),
        )
    return row


@router.get(
    "/sessions/imported/restart/preview",
    response_model=RestartPreviewResponse,
    dependencies=[Depends(require_auth)],
)
async def imported_restart_preview(
    session_uuid: str = Query(..., description="sessions.session_uuid"),
) -> RestartPreviewResponse:
    """What restarting this imported conversation would do, changing nothing.

    Description: reads the row, measures which spelling of its working
      directory holds the transcript, and runs the SAME plan builder the
      action runs, once per configured wrapper. Every offer is resolved
      with the SAME ``--resume`` fragment the action will pass, so the
      command shown and the command run are one string by construction.

      ``pane_state`` is ``'unknown'`` and ``unchanged`` is
      ``cannot_determine``: there is no pane, so neither its state nor
      the outcome of an unpicked restart is a fact anybody measured. That
      is the honest answer, and it is what keeps the picker from enabling
      a button for an option that does not exist here.
    Raises: HTTPException 404 (no such session), 409 (it has a tmux
      identity), 503 (the datastore could not be read).
    """
    from src.config import settings

    state = await run_in_threadpool(_read_row, session_uuid)
    row = _require_imported(state, session_uuid)

    extra_args = imported_extra_args(row, row_read_ok=True)
    directory = await run_in_threadpool(
        resume_directory, row.get("working_dir"), row.get("claude_session_uuid")
    )
    offers = resolve_wrapper_offers(settings, extra_args=extra_args)

    unchanged_plan = plan_imported_restart(
        row,
        row_read_ok=True,
        choice_verdict=None,
        choice_command=None,
        choice_detail=(
            "an imported conversation has no recorded start command, so "
            "there is nothing to restart until a wrapper is picked"
        ),
        directory=directory,
    )

    options = []
    for offer in offers or []:
        choice = validate_agent_choice(
            settings, offer.agent_type, extra_args=extra_args
        )
        plan = plan_imported_restart(
            row,
            row_read_ok=True,
            choice_verdict=choice.verdict,
            choice_command=choice.command,
            choice_detail=choice.detail,
            agent_type=offer.agent_type,
            directory=directory,
        )
        options.append(
            RestartPreviewOption(
                agent_type=offer.agent_type,
                label=offer.label,
                is_current=False,
                resolvable=bool(offer.command),
                actionable_now=plan.actionable,
                kind=plan.kind,
                detail=plan.detail,
                projected_kind=plan.kind,
                projected_detail=plan.detail,
                command=plan.command,
                conversation=plan.conversation,
            )
        )

    plan_preview = RestartPlanPreview(
        kind=unchanged_plan.kind,
        detail=unchanged_plan.detail,
        command=None,
        actionable=False,
        conversation=unchanged_plan.conversation,
    )
    return RestartPreviewResponse(
        name=str(row.get("title") or session_uuid),
        current_agent_type=row.get("agent_type"),
        pane_state=PANE_UNKNOWN,
        unchanged=plan_preview,
        projected=plan_preview,
        options=options,
        wrappers_status="unavailable" if offers is None else "ok",
    )


@router.post(
    "/sessions/imported/restart",
    response_model=ImportedRestartResponse,
    dependencies=[Depends(require_auth)],
)
async def imported_restart(
    request: Request, body: ImportedRestartRequest
) -> ImportedRestartResponse:
    """Create a tmux session for an imported conversation and resume it.

    Description: A RESTART MEANS A RESUME. There is no pane to kill, so
      this creates one - in the spelling of the working directory that
      actually holds the transcript, under the wrapper the caller picked,
      with ``--resume <uuid>``. ``reuse_session_id`` records the new tmux
      instance ONTO the imported row, so the conversation keeps its
      ``session_uuid``, title, project, group membership and archive
      state instead of a second row appearing beside it.

      A REFUSAL IS A 200 WITH ``status='refused'``, matching the pane
      path: the SERVER worked and a GATE declined, and a 500 would blame
      the server for a state it correctly detected.
    Raises: HTTPException 404, 409, 503, and 500 only on a genuine fault.
    """
    from src.config import settings

    state = await run_in_threadpool(_read_row, body.session_uuid)
    row = _require_imported(state, body.session_uuid)

    extra_args = imported_extra_args(row, row_read_ok=True)
    directory = await run_in_threadpool(
        resume_directory, row.get("working_dir"), row.get("claude_session_uuid")
    )
    choice = validate_agent_choice(
        settings, body.agent_type, extra_args=extra_args
    )
    plan = plan_imported_restart(
        row,
        row_read_ok=True,
        choice_verdict=choice.verdict,
        choice_command=choice.command,
        choice_detail=choice.detail,
        agent_type=body.agent_type,
        directory=directory,
    )
    if plan.kind != RESPAWN_AGENT or not plan.actionable:
        status = 400 if plan.kind == RESPAWN_CANNOT_DETERMINE and not row else 200
        logger.info(
            "imported_restart_refused",
            session_uuid=body.session_uuid,
            kind=plan.kind,
        )
        return ImportedRestartResponse(
            status="refused",
            session_uuid=body.session_uuid,
            conversation=plan.conversation,
            command=plan.command,
            working_dir=plan.working_dir,
            detail=plan.detail,
        )

    manager = request.app.state.session_manager
    try:
        session = await manager.create_session(
            session_id=body.session_uuid,
            working_dir=plan.working_dir,
            auto_start_claude=True,
            agent_type=body.agent_type,
            agent_extra_args=extra_args,
            label=plan.label,
            reuse_session_id=plan.reuse_session_id,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as a 500 below
        logger.error(
            "imported_restart_failed",
            session_uuid=body.session_uuid,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500,
            detail=f"failed to start a session for {body.session_uuid!r}: {exc}",
        )

    logger.info(
        "imported_restart_started",
        session_uuid=body.session_uuid,
        agent_type=body.agent_type,
        conversation=plan.conversation,
    )
    return ImportedRestartResponse(
        status="started",
        session_uuid=body.session_uuid,
        session_id=getattr(session, "id", None),
        conversation=plan.conversation,
        command=plan.command,
        working_dir=plan.working_dir,
        detail=plan.detail,
    )
