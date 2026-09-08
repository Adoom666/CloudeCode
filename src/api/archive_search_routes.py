"""``/api/v1/archive/search`` - the scoped, budgeted substring search.

SCOPED, ALWAYS. There is no unscoped form, it is not a missing feature,
and adding one is a regression: ``body_json LIKE '%x%'`` across the
corpus is a measured 7.76 GB scan, about 17.6 seconds per request, on a
shared event loop with no bound on concurrency. One impatient user with a
reload key is a self-inflicted denial of service.

A SECRET-BEARING HIT IS STILL A HIT. When a matched body carries secret
findings the SNIPPET is withheld and the hit is reported anyway, with its
transcript, line, offset and length. Dropping it would make the corpus's
most sensitive material the least findable, which is backwards. The query
string itself is never logged: an operator hunting for a leaked
credential types that credential into ``q``, and a log line is a second
copy of it.

FOUR SCAN STATUSES, NOT TWO. ``complete``, ``budget_exhausted``,
``limit_reached`` and ``not_run``. ``not_run`` means no scan happened and
its counts are ``None`` - not 0, because 0 is a measurement.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from src.api.archive_support import respond, state_dir
from src.api.auth import require_auth
from src.core.archive_read import (
    MAX_SCAN_BUDGET,
    MAX_SCAN_BYTES,
    SCOPE_RESOLVED,
    cannot_determine_envelope,
    run_read,
)
from src.core.archive_search import (
    DEFAULT_SEARCH_LIMIT,
    SCOPE_PROJECT,
    SCOPE_TRANSCRIPT,
    search_scoped,
)

router = APIRouter(tags=["archive"])

#: The documented floor on ``?scan_bytes=``. ``archive_search`` accepts
#: anything from 1 so a unit test can force budget exhaustion on a tiny
#: fixture corpus; the ROUTE holds the 1 MiB minimum the design document
#: specifies, so a public caller cannot ask for a scan window smaller
#: than a single body and get a permanent ``partial`` that reads as a
#: broken search rather than as a budget.
MIN_SCAN_BYTES: int = 1048576


@router.get("/archive/search", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_search(
    q: str = Query(..., description="Substring to find. 2 to 200 characters."),
    project_id: Optional[int] = Query(None, description="Scope: exactly one of these."),
    transcript_id: Optional[int] = Query(None, description="Scope: exactly one of these."),
    limit: int = Query(DEFAULT_SEARCH_LIMIT, description="Hits per page, 1 to 200."),
    cursor: Optional[str] = Query(None, description="Opaque resume cursor."),
    scan_budget: int = Query(
        MAX_SCAN_BUDGET, description="SECONDARY cap, in transcripts."
    ),
    scan_bytes: int = Query(
        MAX_SCAN_BYTES,
        description=(
            "PRIMARY governor, in bytes. 1 MiB minimum. Measured scan "
            "rate is 0.44 GB/s, so the 512 MiB default is about 1.2s."
        ),
    ),
    case_sensitive: bool = Query(False, description="Skip the LOWER() calls."),
    snippets: bool = Query(
        True,
        description="false withholds every preview. The default gate is "
                    "best effort; see meta.snippet_gate.",
    ),
) -> JSONResponse:
    """Substring search inside ONE project or ONE transcript.

    Description: SCOPED, ALWAYS. There is no unscoped form, it is not a
        missing feature, and adding one is a regression: an unscoped
        ``LIKE`` across the corpus is a measured 7.76 GB scan, about 17.6
        seconds per request, on a shared event loop with no bound on
        concurrency. A hit on a secret-bearing body is STILL REPORTED,
        with its transcript, line, offset and length - only the SNIPPET
        is withheld, because dropping the hit would make the corpus's
        most sensitive material the least findable.

    Args:
        q: the substring. Two characters minimum.
        project_id: project scope. Mutually exclusive with transcript_id.
        transcript_id: transcript scope. Mutually exclusive with
            project_id. Exactly one is required; neither or both is a 400.
        limit: hits per page.
        cursor: an opaque resume cursor from ``meta.scan.resume_cursor``.
        scan_budget: secondary cap in transcripts.
        scan_bytes: primary byte governor, at least ``MIN_SCAN_BYTES``.
        case_sensitive: drop the LOWER() calls, measurably faster.
        snippets: false returns NO preview text on any hit. The
            default gate is best effort (see meta.snippet_gate);
            this is the only hard no-disclosure guarantee.

    Returns:
        The envelope. A zero-hit COMPLETE scan and a zero-hit EXHAUSTED
        scan are structurally distinguishable on five independent fields,
        so a client cannot render "no results" for the second.
    """
    scope, scope_id, refusal = _resolve_search_scope(project_id, transcript_id)
    if refusal is not None:
        return respond(refusal, route="search")
    if scan_bytes < MIN_SCAN_BYTES:
        return respond(
            _search_refusal(
                "scan_bytes",
                f"scan_bytes must be at least {MIN_SCAN_BYTES} bytes (1 MiB); "
                f"got {scan_bytes}. A scan window smaller than one body "
                f"reports partial on every request and reads as a broken "
                f"search rather than as a budget.",
            ),
            route="search",
        )
    result = await asyncio.to_thread(
        run_read, state_dir(), search_scoped, q, scope, scope_id,
        subject="datastore", unreadable_result=None,
        limit=limit, scan_budget=scan_budget, cursor=cursor,
        scan_bytes=scan_bytes, case_sensitive=case_sensitive,
        snippets=snippets,
    )
    scan = (result.get("meta") or {}).get("scan") or {}
    return respond(
        result, route="search", scope=scope, scope_id=scope_id,
        # q is NEVER logged: a search term can be a credential the
        # operator is hunting for, and a log line is a second copy.
        query_length=len(q or ""),
        scan_status=scan.get("status"),
        transcripts_scanned=scan.get("transcripts_scanned"),
    )


def _search_refusal(subject: str, reason: str) -> Dict[str, Any]:
    """Build the search envelope for a parameter the route itself refuses.

    Description: built from the SHARED ``cannot_determine_envelope``
      constructor, never by hand, so this refusal carries exactly the
      keys every other response carries. The route refuses BEFORE
      opening the archive, because a parameter the route itself rejects
      is not worth a connection.
    Inputs: subject (str), reason (str).
    Output: dict envelope, cannot_determine.
    Example: _search_refusal("scan_bytes", "...")["result_status"]
    """
    return cannot_determine_envelope(
        subject, reason, result=None, scope_status=SCOPE_RESOLVED,
        meta={"paging": {"limit": 0, "returned": 0, "has_more": None,
                         "next_cursor": None}},
    )


def _resolve_search_scope(
    project_id: Optional[int], transcript_id: Optional[int]
) -> Tuple[str, int, Optional[Dict[str, Any]]]:
    """Pick the single search scope, or refuse.

    Description: neither and both are BOTH refusals, and they are given
      different reasons, because "you named no scope" and "you named two"
      are different mistakes to make.
    Inputs: project_id (int|None), transcript_id (int|None).
    Output: (scope, scope_id, refusal envelope or None).
    Example: _resolve_search_scope(1, None) -> ('project', 1, None)
    """
    if (project_id is None) == (transcript_id is None):
        named = "neither" if project_id is None else "both"
        return "", 0, _search_refusal(
            "scope",
            f"exactly one of project_id or transcript_id is required; "
            f"{named} was given. There is no unscoped search: it is a "
            f"measured 17.6 seconds per request.",
        )
    if project_id is not None:
        return SCOPE_PROJECT, project_id, None
    return SCOPE_TRANSCRIPT, int(transcript_id or 0), None
