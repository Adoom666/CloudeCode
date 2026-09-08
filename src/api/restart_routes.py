"""The READ-ONLY half of the restart path.

``POST /sessions/respawn`` is the action and it MUTATES, so until this
module there was no way to ask which rung of the respawn ladder a session
would land on without committing to it. That gap is why the restart
control could not warn anybody: an empty ``#{pane_start_command}`` lands
on ``RESPAWN_SHELL`` and hands back a LOGIN SHELL, and a picker that
lets a user confidently choose ``claude-chrome`` and then drops them at a
zsh prompt is worse than having no picker.

``GET /sessions/restart/preview`` answers it. One ``tmux list-panes``
(a read), one row read, one config read, and the SAME
``resolve_respawn_plan`` the action calls - once with nothing picked and
once per configured wrapper. Nothing is spawned, nothing is written, and
no session is adopted as a side effect of asking.

WHY IT IS A GET. The method is the contract. A POST here would be
indistinguishable at the call site from the one that restarts, and this
endpoint's entire value is that it is safe to call before the user has
decided anything. The tmux name rides in a query parameter and is
validated by the same ``_safe_target`` rule the action uses, so a name
tmux would read as a window/pane target is a 400 here exactly as it is
there.

WHY A REFUSAL IS STILL A 200, same as the action: ``unchanged.kind`` of
``cannot_determine`` means the SERVER worked and the PANE could not be
read. A 500 would blame the server for a state it correctly detected and
push the client into an error path instead of showing the sentence that
says what is unknown.

Mounted under ``/api/v1`` from ``src/main.py``. Its own module rather
than more lines in ``src/api/routes.py``, which is already far past the
project's 500-line budget.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.auth import require_auth
from src.models import (
    RestartPlanPreview,
    RestartPreviewOption,
    RestartPreviewResponse,
)

logger = structlog.get_logger()

router = APIRouter(tags=["sessions"])


@router.get(
    "/sessions/restart/preview",
    response_model=RestartPreviewResponse,
    dependencies=[Depends(require_auth)],
)
async def restart_preview(
    request: Request,
    session_name: str = Query(
        ..., description="Literal tmux session name to preview a restart for"
    ),
):
    """Report what restarting this session would do, without doing it.

    Returns the baseline plan (nothing picked) plus one predicted
    outcome per configured launch wrapper, all derived from a single
    reading of the pane so every option on screen answers a question
    about the same observed state.

    Raises:
        HTTPException(400): the name contains ``:`` or ``.``.
        HTTPException(500): a genuine, unexpected server fault.
    """
    session_manager = request.app.state.session_manager

    try:
        preview = await session_manager.restart_preview(session_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(
            "restart_preview_failed", name=session_name, error=str(exc)
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to preview a restart of {session_name!r}: {exc}",
        )

    return RestartPreviewResponse(
        name=preview.name,
        current_agent_type=preview.current_agent_type,
        pane_state=preview.pane_state,
        unchanged=RestartPlanPreview(
            kind=preview.unchanged.kind,
            detail=preview.unchanged.detail,
            command=preview.unchanged.command,
            actionable=preview.unchanged.actionable,
            conversation=preview.unchanged.conversation,
        ),
        projected=RestartPlanPreview(
            kind=preview.projected.kind,
            detail=preview.projected.detail,
            command=preview.projected.command,
            actionable=preview.projected.actionable,
            conversation=preview.projected.conversation,
        ),
        options=[
            RestartPreviewOption(
                agent_type=o.agent_type,
                label=o.label,
                is_current=o.is_current,
                resolvable=o.resolvable,
                actionable_now=o.actionable_now,
                kind=o.kind,
                detail=o.detail,
                projected_kind=o.projected_kind,
                projected_detail=o.projected_detail,
                command=o.command,
                conversation=o.conversation,
            )
            for o in preview.options
        ],
        wrappers_status=preview.wrappers_status,
    )
