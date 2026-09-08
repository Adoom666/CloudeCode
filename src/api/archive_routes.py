"""``/api/v1/archive/*`` - read-only HTTP over the ingested transcript archive.

ITS OWN MODULE, like ``corpus_routes.py`` and ``status_routes.py``, for
the same reason: nothing new lands in ``src/api/routes.py``.

THESE ROUTES ARE THIN AND THAT IS ENFORCED, NOT ASPIRED TO. Every handler
does four things: bound its parameters, hand them to ONE function in
``src.core.archive_*`` inside ``asyncio.to_thread``, log, and return what
came back. There is no SQL in this file and no business logic. The core
functions already return the three-outcome envelope, so a route cannot
invent a response shape - and because it cannot, it cannot quietly lose
its third outcome.

``response_model=None`` ON EVERY ROUTE, WITHOUT EXCEPTION. A FastAPI
``response_model`` is a FILTER, not a passthrough: it silently DELETES
any field the model does not declare. This project has been bitten twice,
most recently by ``ThemeManifest`` dropping ``themeCss`` from every
``/api/v1/themes`` response while the value existed the whole way up to
serialization. The envelope's ``unevaluated`` and ``meta`` blocks are
exactly the optional, route-varying structure a response model eats.
``test_archive_read_api.py`` iterates ``router.routes`` and asserts it, so
a route added later without it fails a test rather than a code review.

``Depends(require_auth)`` ON EVERY ROUTE, WITHOUT EXCEPTION. The archive
is a complete record of the owner's work, including 6,240 bodies that
carry credential material.

SQLITE WORK RUNS IN A THREAD. A ``sqlite3`` call blocks, and a measured
1.2 second search on the event loop would stall every live terminal
WebSocket in the process.

SECRETS ARE FLAGGED, NEVER REDACTED, AND NEVER LOGGED. No handler here
logs a query string's matches, a body, or a snippet. Body-bearing routes
log ``body_id`` and ``secret_finding_count`` only.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from src.api.archive_export_routes import router as export_router
from src.api.archive_messages_routes import router as messages_router
from src.api.archive_search_routes import router as search_router
from src.api.archive_support import respond, state_dir
from src.api.auth import require_auth
from src.core import archive_body, archive_hierarchy, archive_lines
from src.core import archive_merged_tree
from src.core import archive_subagents
from src.core.archive_read import (
    DEFAULT_LINE_LIMIT,
    DEFAULT_PAGE_BYTES,
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_BYTES,
    MIN_PAGE_BYTES,
    run_read,
)

router = APIRouter(tags=["archive"])

#: The documented floor on ``/search?scan_bytes=``. The core module
#: accepts anything from 1 so a test can force budget exhaustion on a
#: fixture corpus; the ROUTE holds the 1 MiB minimum the design document
#: specifies, so a public caller cannot ask for a scan so small it always
#: reports partial and looks like a broken search.
MIN_SCAN_BYTES: int = 1048576

# --- 6.1 / 6.2  Hierarchy: hosts and corpora -------------------------------


@router.get("/archive/hosts", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_hosts() -> JSONResponse:
    """List every host, with its corpus and transcript counts.

    Description: the top of the hierarchy. Not paginated - the table is
        bounded by the number of physical machines the owner owns
        (measured: 2 rows, 0.0006s).

    Returns:
        The three-outcome envelope. ``meta.totals`` always carries
        ``transcripts_with_no_host_id``, including when it is 0, so a
        client can tell "everything is attributed" from "nobody asked".
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_hierarchy.hosts,
        subject="datastore", unreadable_result=None,
    )
    return respond(result, route="hosts")


@router.get("/archive/hosts/{host_id}/corpora", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_corpora_for_host(host_id: int) -> JSONResponse:
    """List the corpora collected from one host.

    Description: not paginated - measured 3 rows across the whole fleet,
        bounded by the number of directories Claude writes to.

    Args:
        host_id: the host to list corpora for.

    Returns:
        The envelope. Every row carries
        ``unattributed_transcript_count`` so a client paging projects can
        never finish believing it has seen the whole corpus.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_hierarchy.corpora_for_host, host_id,
        subject=f"host:{host_id}", unreadable_result=None,
    )
    return respond(result, route="corpora", host_id=host_id)


@router.get("/archive/projects", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_merged_projects() -> JSONResponse:
    """Every project in the archive as ONE list, machine demoted to a field.

    The host -> corpus -> project routes are unchanged and still serve
    the physical shape. This serves the shape a person navigates: one
    node per distinct ``observed_cwd``, carrying ``display_name`` (the
    folder name, widened leftward only where it would otherwise
    collide), ``full_path`` (the slug, unparsed), ``hosts`` (every
    machine it appears on) and ``members`` (the underlying per-corpus
    rows, so nothing the merge folded up is unreachable).

    Deliberately NOT paginated - 80 project rows merge to 77 nodes, and
    a page of a merged tree would let a client conclude a project lives
    on one machine because the row proving otherwise fell on page 2.

    Returns:
        The envelope; ``result`` is the merged node list, and
        ``meta.unattributed.by_corpus`` carries the project-less counts
        with an explicit ``counted`` flag, because the rail hides that
        node only on a count it can prove is zero.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_merged_tree.merged_projects,
        subject="archive:projects", unreadable_result=None,
    )
    return respond(result, route="merged-projects")


@router.get("/archive/corpora/{corpus_id}/projects", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_projects_for_corpus(
    corpus_id: int,
    limit: int = Query(DEFAULT_PAGE_LIMIT, description="Page size, clamped to 1..200."),
    cursor: Optional[str] = Query(None, description="Opaque keyset cursor."),
) -> JSONResponse:
    """Page one corpus's projects, ordered by slug.

    Args:
        corpus_id: the corpus to page.
        limit: requested page size. CLAMPED to ``1..MAX_PAGE_LIMIT``
            rather than rejected, and the effective value is reported in
            ``meta.paging.limit`` so the caller sees what it got.
        cursor: an opaque cursor from a previous page. A malformed one is
            a 400 ``cannot_determine``, never a silent restart at page 1.

    Returns:
        The envelope; ``result`` is a list of project rows.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_hierarchy.projects_for_corpus, corpus_id,
        subject=f"corpus:{corpus_id}", unreadable_result=None,
        limit=limit, cursor=cursor,
    )
    return respond(result, route="projects", corpus_id=corpus_id)


@router.get("/archive/corpora/{corpus_id}/unattributed", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_unattributed_for_corpus(
    corpus_id: int,
    limit: int = Query(DEFAULT_PAGE_LIMIT, description="Page size, clamped to 1..200."),
    cursor: Optional[str] = Query(None, description="Opaque keyset cursor."),
) -> JSONResponse:
    """Page the transcripts that belong to a corpus but to NO project.

    Description: without this route those transcripts are unreachable by
        navigation, because every other path into a transcript goes
        through a project. An empty ``ok`` here means genuinely empty and
        ``meta.note`` says so in words.

    Args:
        corpus_id: the corpus to page.
        limit: requested page size, clamped to 1..200.
        cursor: opaque keyset cursor.

    Returns:
        The envelope; ``result`` is a list of transcript rows.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_hierarchy.unattributed_for_corpus,
        corpus_id, subject=f"corpus:{corpus_id}", unreadable_result=None,
        limit=limit, cursor=cursor,
    )
    return respond(result, route="unattributed", corpus_id=corpus_id)


@router.get("/archive/projects/{project_id}/transcripts", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_transcripts_for_project(
    project_id: int,
    limit: int = Query(DEFAULT_PAGE_LIMIT, description="Page size, clamped to 1..200."),
    cursor: Optional[str] = Query(None, description="Opaque keyset cursor."),
    session_ref_scheme: Optional[str] = Query(
        None,
        description=(
            "Post-filter on the session_ref_scheme COLUMN, inside this "
            "project. A scheme no transcript in the archive carries is a "
            "cannot_determine naming the schemes that exist, not an empty "
            "ok. Counts in meta.filters are within this project only."
        ),
    ),
) -> JSONResponse:
    """Page one project's transcripts, newest ingest first.

    Args:
        project_id: the project to page.
        limit: requested page size, clamped to 1..200.
        cursor: opaque keyset cursor. Keyed on ``(ingested_at, id)``; the
            ``id`` tie-break is load-bearing, not decorative, because all
            21,039 transcripts were ingested in a small number of batches
            and the timestamps repeat at microsecond resolution.
        session_ref_scheme: filter to one scheme value. A POST-FILTER
            inside the already-indexed project range, the same shape as
            the ``/lines`` role/record_type/model filters; measured, the
            plan is unchanged and one filtered page of project 12 cost
            0.0012s. IT FILTERS ON THE COLUMN AND NOTHING ELSE - 19 of
            the 1,451 ``uuid``-scheme transcripts carry a ``session_ref``
            that is not a UUID, so this is not a guarantee of
            conversation-ness and ``meta.filters`` says so.

    Returns:
        The envelope. Every row carries a derived ``attribution_state``:
        a transcript can be attributed to a host AND unevidenced at the
        same time, and a client must render that distinctly.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_hierarchy.transcripts_for_project,
        project_id, subject=f"project:{project_id}", unreadable_result=None,
        limit=limit, cursor=cursor, session_ref_scheme=session_ref_scheme,
    )
    return respond(
        result, route="transcripts", project_id=project_id,
        session_ref_scheme=session_ref_scheme,
    )


# --- 6.6 / 6.7 / 6.8  One transcript, its lines, one body ------------------


@router.get("/archive/transcripts/{transcript_id}", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_transcript(transcript_id: int) -> JSONResponse:
    """Read one transcript's header and counts, without its lines.

    Args:
        transcript_id: the transcript to read.

    Returns:
        The envelope. ``result.export.verified_available`` is advertised
        here so a client never has to discover the verify-before-send
        refusal by making the request, and
        ``verified_unavailable_reason`` names the size and the cap.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_lines.transcript_header, transcript_id,
        subject=f"transcript:{transcript_id}", unreadable_result=None,
    )
    return respond(result, route="transcript", transcript_id=transcript_id)


@router.get("/archive/transcripts/{transcript_id}/lines", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_transcript_lines(
    transcript_id: int,
    limit: int = Query(DEFAULT_LINE_LIMIT, description="Page size, clamped to 1..500."),
    cursor: Optional[str] = Query(None, description="Opaque keyset cursor."),
    include_bodies: bool = Query(False, description="Attach whole bodies."),
    max_page_bytes: int = Query(
        DEFAULT_PAGE_BYTES,
        ge=MIN_PAGE_BYTES,
        le=MAX_PAGE_BYTES,
        description=(
            "Soft byte cap when include_bodies is true. The page stops "
            "EARLY and reports partial; a body is never cut to fit."
        ),
    ),
    role: Optional[str] = Query(None, description="Post-filter: 'user' or 'assistant'."),
    record_type: Optional[str] = Query(None, description="Post-filter: record type."),
    model: Optional[str] = Query(None, description="Post-filter: model name."),
    start_line: Optional[int] = Query(
        None,
        description=(
            "Open the page at this 0-BASED line_no instead of at line 0. "
            "MUTUALLY EXCLUSIVE with cursor: sending both is a 400. A "
            "value past the transcript's highest line_no is a 404 naming "
            "that value, never an empty page."
        ),
    ),
) -> JSONResponse:
    """Page one transcript's lines, optionally carrying whole bodies.

    Description: the conversation reader. The three filters are
        POST-FILTERS inside an already-indexed scope, so their counts are
        labelled ``scanned_within_this_transcript_only`` and must never
        be rendered as corpus totals. A filter value that does not exist
        in its lookup table is a ``cannot_determine``, not an empty
        ``ok``: "there is no model called gpt-4" and "no line here used
        that model" are different findings.

    Args:
        transcript_id: the transcript to page.
        limit: page size, clamped to 1..``MAX_LINE_LIMIT``.
        cursor: opaque keyset cursor on ``line_no``.
        include_bodies: attach each line's WHOLE body. Never a prefix; a
            body over ``MAX_BODY_BYTES`` is withheld with a ``body_href``.
        max_page_bytes: soft byte budget for the attached bodies.
        role: filter by role value.
        record_type: filter by record type value.
        model: filter by model value.
        start_line: 0-based line number to open the page at. Deliberately
            NOT declared ``ge=0`` here: a FastAPI bound answers 422 with
            a validation body that is not an envelope, and every outcome
            on this route must be renderable by the same client code. A
            negative value is a named cannot_determine instead.
            Rejected outright when ``cursor`` is also sent - see
            :mod:`src.core.archive_start_line` for why picking one
            silently is worse than refusing both.

    Returns:
        The envelope; ``result`` is a list of line rows.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_lines.transcript_lines, transcript_id,
        subject=f"transcript:{transcript_id}", unreadable_result=None,
        limit=limit, cursor=cursor, include_bodies=include_bodies,
        max_page_bytes=max_page_bytes, role=role, record_type=record_type,
        model=model, start_line=start_line,
    )
    return respond(
        result, route="lines", transcript_id=transcript_id,
        include_bodies=include_bodies, start_line=start_line,
    )


@router.get("/archive/bodies/{body_id}", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_body(
    body_id: int,
    with_appearances: bool = Query(
        False, description="Also report where else this body appears."
    ),
) -> JSONResponse:
    """Read one WHOLE body, plus its secret findings as offsets.

    Description: SECRETS ARE FLAGGED, NEVER REDACTED. ``body_json`` comes
        back whole and unmodified; the ``secrets`` block carries
        ``{detector, match_offset, match_length, value_sha256}`` and the
        CLIENT masks using those offsets. The server never cuts the
        string - cutting it would return a prefix of a body, and an
        offset-masking client can be verified while a server-side
        redaction cannot. No matched value is returned, logged, or put in
        an exception message anywhere on this path; the log line below
        carries the id and the finding COUNT only.

    Args:
        body_id: the body to read.
        with_appearances: when true, also report every appearance of this
            body, which is the payoff of the identity/appearance split.

    Returns:
        The envelope; ``result`` is one body object.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_body.body, body_id,
        subject=f"body:{body_id}", unreadable_result=None,
        with_appearances=with_appearances,
    )
    payload = result.get("result") or {}
    findings = payload.get("secret_finding_count") if isinstance(payload, dict) else None
    return respond(
        result, route="body", body_id=body_id,
        secret_finding_count=findings,
    )


# --- 6.12  Subagent lineage ------------------------------------------------


@router.get("/archive/transcripts/{transcript_id}/subagents", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_subagents(
    transcript_id: int,
    limit: int = Query(DEFAULT_LINE_LIMIT, description="Page size, clamped to 1..200."),
    cursor: Optional[str] = Query(None, description="Opaque keyset cursor."),
) -> JSONResponse:
    """Page one transcript's subagent appearances, with lineage resolved.

    Description: SCOPED, ALWAYS, and it does NOT call
        ``message_model_export.subagent_edges``: that helper returns no
        ``line_no``, is not paged, and unscoped returns a measured
        1,627,995 rows, which is not a response, it is an outage.
        ``src.core.archive_subagents`` runs the route's own documented
        query instead, which already selects ``a.line_no``.

    Args:
        transcript_id: the transcript to page. There is no form of this
            route that omits it.
        limit: page size, clamped to 1..``MAX_PAGE_LIMIT``.
        cursor: opaque keyset cursor on ``appearance_id``.

    Returns:
        The envelope. ``meta.lineage.parent_transcripts`` is a LIST
        because one ``origin_session_ref`` can legitimately resolve to
        two transcripts - the same session copied between the owner's two
        machines - and that is not a collision.
    """
    result = await asyncio.to_thread(
        run_read, state_dir(), archive_subagents.subagents_for_transcript,
        transcript_id, subject=f"transcript:{transcript_id}",
        unreadable_result=None, limit=limit, cursor=cursor,
    )
    return respond(result, route="subagents", transcript_id=transcript_id)


# --- 6.9 / 6.10  Export, both forms ---------------------------------------
# The export routes live in their own module ONLY for the repo's
# 500-line cap, and they belong to THIS router so ``src/main.py`` has
# exactly one include_router.
#
# Copied in rather than ``router.include_router(export_router)``:
# FastAPI 0.141 wraps an included router in an ``_IncludedRouter`` entry
# instead of flattening it, so the export routes would NOT appear in a
# flat ``router.routes`` walk - and the contract test that asserts
# ``response_model is None`` and ``require_auth`` on every route walks
# exactly that. A structural test that silently cannot see two of the
# routes it is guarding is the verification-step-that-cannot-fail shape
# this whole API is written against. Both modules already carry the
# full path, so there is no prefix to apply.
router.routes.extend(search_router.routes)
router.routes.extend(export_router.routes)
router.routes.extend(messages_router.routes)
