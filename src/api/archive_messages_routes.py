"""``/api/v1/archive/transcripts/{id}/messages`` - the READING view.

ITS OWN MODULE ONLY FOR THE REPO'S 500-LINE CAP, exactly like
``archive_search_routes`` and ``archive_export_routes``, and its route
belongs to the SAME router so ``src/main.py`` still has one
include_router. It is copied in with ``router.routes.extend`` rather
than ``include_router`` for the reason recorded at the bottom of
``archive_routes``: FastAPI 0.141 wraps an included router in an
``_IncludedRouter`` entry instead of flattening it, so this route would
NOT appear in the flat ``router.routes`` walk that the contract test
uses to assert ``response_model is None`` and ``require_auth`` on every
route. A structural test that cannot see the route it is guarding is a
verification step that cannot fail.

WHAT THIS SERVES AND WHAT IT LEAVES ALONE. ``/lines`` is UNCHANGED: it
is the Raw view, one row per physical line carrying the byte-exact
``body_json``, and the export path is untouched and reads none of this.
This route serves the same rows already decomposed into typed blocks,
with subagent runs resolved and the envelope detail parked behind an
``info`` block. All the logic lives in ``src.core.archive_turns``; this
file binds parameters and returns what came back, like every other
handler in this API.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from src.api.archive_support import respond, state_dir
from src.api.auth import require_auth
from src.core import archive_turns
from src.core.archive_read import DEFAULT_LINE_LIMIT, run_read

router = APIRouter(tags=["archive"])


@router.get("/archive/transcripts/{transcript_id}/messages", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_transcript_messages(
    transcript_id: int,
    limit: int = Query(DEFAULT_LINE_LIMIT, description="Page size, clamped to 1..500."),
    cursor: Optional[str] = Query(
        None,
        description=(
            "Opaque keyset cursor on line_no. The SAME cursor kind /lines "
            "uses, so a position is portable between the raw and reading "
            "views of one transcript."
        ),
    ),
    start_line: Optional[int] = Query(
        None,
        description=(
            "Open the page at this 0-BASED line_no. MUTUALLY EXCLUSIVE "
            "with cursor: sending both is a 400. Past the last line is a "
            "404 naming that line, never an empty page."
        ),
    ),
    include_text: bool = Query(
        True,
        description=(
            "False withholds every block preview BY REQUEST while still "
            "returning every block, its type and its full text_length."
        ),
    ),
    max_block_chars: Optional[int] = Query(
        None,
        description=(
            "Per-block preview ceiling, clamped to 1..MAX_BLOCK_CHARS. "
            "Default is the shared BLOCK_PREVIEW_MAX_CHARS."
        ),
    ),
) -> JSONResponse:
    """Page one transcript as conversation turns, ready to render.

    Description: the reading view. The PATH is ``/messages`` and the
        payload is ``turns`` - the route is named for what a reader asks
        for and the rows are named for what they are, which is the
        contract the client codes against. ``/lines`` is untouched and remains
        the Raw view; this route serves the same rows already decomposed
        into typed blocks, with subagent runs resolved and the envelope
        detail parked in each turn's ``info`` block. The client no
        longer parses JSON per row.

        EVERY FIELD CARRIES ITS THIRD OUTCOME. ``role_state`` says
        whether the label came from the role column, from
        ``record_type`` as a fallback (the COMMON case - role is NULL on
        44.93 percent of bodies), or from neither. ``blocks_state``
        separates a body with genuinely no content from one never
        processed from one that could not be parsed. ``subagents_state``
        separates "spawned none" from "spawned some and could not
        identify them": the spawn linkage is an ``agentId`` printed in
        the tool_result and it resolves 96.04 percent of the corpus's
        19,629 spawns, so the remaining 3.96 percent are reported as
        unresolved entries rather than dropped.

        BLOCK TEXT CROSSES THE EXISTING GATE, once, in
        ``src.core.message_block_preview``. There is no second policy
        here. A withheld block is still returned with its type and its
        full ``text_length``.

    Args:
        transcript_id: the transcript to page.
        limit: page size, clamped to 1..``MAX_LINE_LIMIT``.
        cursor: opaque keyset cursor on ``line_no``, kind ``lines``.
        start_line: 0-based line to open at. Deliberately NOT declared
            ``ge=0``, for the reason ``/lines`` documents: a FastAPI
            bound answers 422 with a body that is not an envelope, and
            every outcome on this route must be renderable by the same
            client code.
        include_text: False asks for no block previews at all.
        max_block_chars: per-block preview ceiling.

    Returns:
        The envelope; ``result`` is a list of turns. Each turn's
        ``subagents`` is ordered with an explicit 1-based ``order`` and
        an ``order_basis`` naming whether that order came from a
        timestamp or from position in the file.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_turns.transcript_turns, transcript_id,
        subject=f"transcript:{transcript_id}", unreadable_result=None,
        limit=limit, cursor=cursor, start_line=start_line,
        include_text=include_text, max_block_chars=max_block_chars,
    )
    return respond(
        result, route="turns", transcript_id=transcript_id,
        start_line=start_line,
    )
