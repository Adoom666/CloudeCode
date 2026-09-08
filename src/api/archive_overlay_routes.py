"""The presentation overlay API: the archive's first WRITE routes.

WHAT THIS ADDS AND WHAT IT DELIBERATELY DOES NOT TOUCH.
``GET /archive/projects`` is left exactly as it was and still returns the
archive's own names, unmodified, so the raw truth stays addressable. This
module adds ``GET /archive/overlay/projects``, which is that same read
with the owner's presentation layered over it. Two routes, two questions:
"what is in the archive" and "how does the owner want it shown". Folding
the second into the first would have made the raw list unreachable, and a
presentation layer you cannot switch off is indistinguishable from an
edit.

HOW THE READ-ONLY GUARANTEE SURVIVES ADDING WRITES. Four properties, each
structural rather than remembered, and each asserted by a test:

* Every GET here goes through ``run_read``/``open_read_only``, the same
  path export, bodies and lines use, on which SQLite itself refuses a
  write because ``PRAGMA query_only=ON`` was set AND read back. That
  function is not weakened, not parameterised and not bypassed.
* Every write is a POST or a DELETE. There is no GET in this module that
  reaches :mod:`src.core.archive_overlay_write`, and the read module
  imports nothing from the write module, so a read handler has no writing
  function in scope at all.
* No write touches an archive table. Every statement in the write module
  targets ``archive_project_overlay``. ``message_projects``,
  ``message_transcripts``, ``message_bodies``, ``message_appearances``
  and ``message_content_blocks`` are hashed before and after every
  operation by ``tests/test_archive_overlay.py`` and must be byte
  identical.
* The writes are ADDRESSED BY IDENTITY KEY, not by a row id, so there is
  no route parameter that could be pointed at an archive row even by a
  caller trying to.

``response_model=None`` ON EVERY ROUTE, WITHOUT EXCEPTION, for the reason
``archive_routes`` states: a FastAPI ``response_model`` is a FILTER, and
it would silently delete ``unevaluated`` and ``meta`` - the two fields
that carry the third outcome. This project has been bitten by that twice.
A test in this module's suite walks ``router.routes`` and asserts it
rather than trusting the decorator to have been typed correctly.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from src.api.archive_support import respond, state_dir
from src.api.auth import require_auth
from src.core import archive_merged_tree
from src.core.archive_overlay import (
    OVERLAY_MEANS,
    OVERLAY_SUBJECT,
    apply_overlay,
    identity_key,
    load_overlay,
)
from src.core.archive_overlay_write import OverlayWriteError, write_one
from src.core.archive_read import (
    RESULT_CANNOT_DETERMINE,
    RESULT_OK,
    envelope,
    run_read,
)
from src.core.db import DatastoreUnreadableError

router = APIRouter(tags=["archive-overlay"])

#: Route prefix for this feature, so an href is built in one place.
OVERLAY_PREFIX: str = "/archive/overlay"


def _cannot_determine(reason: str) -> Dict[str, Any]:
    """Build the overlay's cannot_determine envelope.

    Description: ``result`` is None, never ``[]``. An empty list would let
      a client that reads only ``result`` render a confident "no
      projects" over a question nobody answered - and worse here than
      elsewhere, because the same client would render the ARCHIVE names
      as though the owner had never renamed anything.
    Inputs: reason (str) - what could not be evaluated, and why.
    Output: dict - a three-outcome envelope.
    Example: _cannot_determine('no such table')['result'] is None
    """
    return envelope(
        result=None,
        result_status=RESULT_CANNOT_DETERMINE,
        unevaluated=[{"subject": OVERLAY_SUBJECT, "reason": reason}],
        meta={"overlay": {"overlay_means": OVERLAY_MEANS}},
    )


def _presented(conn, *, include_hidden: bool, hidden_only: bool) -> Dict[str, Any]:
    """Read the merged projects and layer the overlay over them.

    Description: runs on a READ-ONLY connection. The archive read is the
      existing ``merged_projects`` call, unchanged and unwrapped, so the
      overlay cannot alter what the archive reports - it can only decide
      how it is presented. If the archive read itself did not come back
      ``ok`` its envelope is returned untouched rather than being
      overlaid, because layering a presentation over a list nobody
      measured would launder a cannot_determine into an ok.
    Inputs: conn (sqlite3.Connection) - read-only. include_hidden (bool)
      - keep hidden projects in the main list. hidden_only (bool) -
      return ONLY the hidden projects, which is the restore view.
    Output: dict - a three-outcome envelope.
    Example: _presented(conn, include_hidden=False, hidden_only=False)
    """
    base = archive_merged_tree.merged_projects(conn)
    if base.get("result_status") != RESULT_OK or not isinstance(base.get("result"), list):
        return base

    load = load_overlay(conn)
    if not load.ok:
        return _cannot_determine(
            f"the overlay table could not be read, so the archive's own "
            f"names cannot be presented as either overridden or "
            f"untouched: {load.reason}"
        )

    applied = apply_overlay(base["result"], load, include_hidden=include_hidden)
    nodes = applied["hidden"] if hidden_only else applied["nodes"]

    meta = dict(base.get("meta") or {})
    meta["overlay"] = {
        "overlay_means": OVERLAY_MEANS,
        "rows": len(load.rows or {}),
        "applied_count": applied["applied_count"],
        "hidden_count": applied["hidden_count"],
        "include_hidden": bool(include_hidden),
        "hidden_only": bool(hidden_only),
        "groups": applied["groups"],
        "orphans": applied["orphans"],
        "orphans_mean": (
            "an overlay row whose project is not in this corpus. It is "
            "NOT rendered as a project node, because a node with no "
            "transcripts behind it is a phantom, and it is NOT deleted, "
            "because that would discard the owner's rename. It reattaches "
            "by itself if the project is re-ingested."
        ),
    }
    return envelope(
        result=nodes,
        result_status=RESULT_OK,
        scope_status=base.get("scope_status", "resolved"),
        unevaluated=base.get("unevaluated"),
        meta=meta,
    )


# --- Reads -----------------------------------------------------------------


@router.get(f"{OVERLAY_PREFIX}/projects", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_overlaid_projects(
    include_hidden: bool = Query(False),
) -> JSONResponse:
    """List projects with the owner's presentation overlay applied.

    Description: the same 77 merged nodes ``/archive/projects`` returns,
      with an override name substituted where one exists, the group
      attached, and hidden projects filtered out by default. Every node
      carries ``archive_display_name`` with the archive's own derived
      name unchanged, and an ``overlay`` block whose ``status`` is
      'none', 'applied' or 'cannot_determine' - three states, so a client
      is never left inferring "untouched" from an unchanged name.
    Inputs: include_hidden (bool) - keep hidden projects in the list.
    Output: JSONResponse - envelope; ``result`` is the node list.
    Example: GET /api/v1/archive/overlay/projects?include_hidden=true
    """
    result = await run_in_read(include_hidden=include_hidden, hidden_only=False)
    return respond(result, route="overlay-projects", include_hidden=include_hidden)


@router.get(f"{OVERLAY_PREFIX}/hidden", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_hidden_projects() -> JSONResponse:
    """List the projects the owner has hidden, so they can be restored.

    Description: the restore view, and the reason a soft delete here is
      reversible in practice and not just in principle. It is the SAME
      filter the default list applies, read from the same overlay load,
      so the two cannot disagree about which projects are hidden. Every
      node is complete - counts, hosts, members - so a client can show
      what it is about to restore.
    Inputs: none.
    Output: JSONResponse - envelope; ``result`` is the hidden nodes.
    Example: GET /api/v1/archive/overlay/hidden
    """
    result = await run_in_read(include_hidden=True, hidden_only=True)
    return respond(result, route="overlay-hidden")


@router.get(f"{OVERLAY_PREFIX}/groups", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_groups() -> JSONResponse:
    """List every group and how many projects are in it.

    Description: a group exists exactly as long as a project names it, so
      this is derived from the overlay rows rather than from a group
      table that could hold a group nobody is in. HIDDEN PROJECTS ARE
      COUNTED, because a group whose members are all hidden still exists
      and a count that omitted them would make it vanish from the group
      list while its projects were still assigned to it.
    Inputs: none.
    Output: JSONResponse - envelope; ``result`` is [{group,
      project_count}], ordered case-insensitively by name.
    Example: GET /api/v1/archive/overlay/groups
    """
    result = await run_in_read(include_hidden=True, hidden_only=False)
    if result.get("result_status") != RESULT_OK:
        return respond(result, route="overlay-groups")
    groups = (result.get("meta") or {}).get("overlay", {}).get("groups", [])
    return respond(
        envelope(
            result=groups,
            result_status=RESULT_OK,
            meta={"overlay": {"overlay_means": OVERLAY_MEANS}},
        ),
        route="overlay-groups",
    )


async def run_in_read(*, include_hidden: bool, hidden_only: bool) -> Dict[str, Any]:
    """Open the archive read-only and produce the presented list.

    Description: the ONE place a read route opens a connection, so
      "reads use open_read_only" is a property of this module rather than
      a habit repeated at three call sites. ``run_read`` turns a database
      that would not open into a cannot_determine envelope instead of a
      500 or, far worse, an empty list.
    Inputs: include_hidden (bool), hidden_only (bool).
    Output: dict - a three-outcome envelope.
    Example: await run_in_read(include_hidden=False, hidden_only=False)
    """
    return run_read(
        state_dir(),
        _presented,
        subject="archive:overlay",
        unreadable_result=None,
        include_hidden=include_hidden,
        hidden_only=hidden_only,
    )


# --- Writes ----------------------------------------------------------------


def _write(key: str, kind: str, field: str, value: Any) -> JSONResponse:
    """Apply one overlay change and answer with the resulting row.

    Description: the single write seam every mutating route funnels
      through, so the transaction, the error mapping and the response
      shape are defined once. An ``OverlayWriteError`` is a refused
      request and comes back as cannot_determine naming the field, not as
      a 500 - the server DID evaluate it, and it answered no.
    Inputs: key (str) - identity key. kind (str) - identity kind.
      field (str) - 'display_name', 'group' or 'hidden'. value (Any).
    Output: JSONResponse - envelope; ``result`` is the overlay row, or
      None when the row was pruned for carrying no statement.
    Example: _write('cwd:/x', 'cwd', 'hidden', True)
    """
    try:
        outcome = write_one(state_dir(), key, kind, field=field, value=value)
    except OverlayWriteError as exc:
        return respond(
            envelope(
                result=None,
                result_status=RESULT_CANNOT_DETERMINE,
                unevaluated=[{"subject": f"overlay:{field}", "reason": str(exc)}],
            ),
            route="overlay-write",
            field=field,
        )
    except DatastoreUnreadableError as exc:
        return respond(
            envelope(
                result=None,
                result_status=RESULT_CANNOT_DETERMINE,
                unevaluated=[{"subject": OVERLAY_SUBJECT, "reason": str(exc)}],
            ),
            route="overlay-write",
            field=field,
        )
    return respond(
        envelope(
            result=outcome["row"],
            result_status=RESULT_OK,
            meta={
                "overlay": {
                    "identity_key": key,
                    "identity_kind": kind,
                    "field": field,
                    "outcome": outcome["outcome"],
                    "outcome_means": (
                        "'ok' wrote the row; 'pruned' REMOVED it because it "
                        "no longer said anything - no name, no group, not "
                        "hidden. A pruned row is the same state as a "
                        "project that was never touched, which is what the "
                        "list reports as overlay_status 'none'. Nothing in "
                        "the archive was written either way."
                    ),
                }
            },
        ),
        route="overlay-write",
        field=field,
    )


def _key_from_body(body: Dict[str, Any]) -> tuple:
    """Resolve the identity key a write should target.

    Description: a caller may send the ``identity_key`` it read off a
      node, which is the normal path, or the ``observed_cwd`` and
      ``project_id`` it has, in which case the key is computed by the
      SAME function the read path uses so the two cannot disagree. An
      explicit key wins, and it is not parsed or validated against a
      corpus: an overlay row for a project that is not present is an
      ORPHAN, which is a supported state, not an error.
    Inputs: body (dict) - the request body.
    Output: tuple[str, str] - (identity_key, identity_kind).
    Raises: OverlayWriteError - neither form was supplied.
    Example: _key_from_body({'identity_key': 'cwd:/x'}) -> ('cwd:/x', 'cwd')
    """
    key = body.get("identity_key")
    if isinstance(key, str) and key.strip():
        kind = "cwd" if key.startswith("cwd:") else "project_id"
        return key, kind
    cwd = body.get("observed_cwd")
    project_id = body.get("project_id")
    if cwd is None and project_id is None:
        raise OverlayWriteError(
            "a write needs identity_key, or observed_cwd/project_id to "
            "compute one from; nothing was supplied"
        )
    try:
        return identity_key(cwd, project_id)
    except ValueError as exc:
        raise OverlayWriteError(str(exc)) from exc


def _resolve_or_refuse(body: Dict[str, Any], field: str, value: Any) -> JSONResponse:
    """Resolve the identity key, then write, or refuse with a reason.

    Inputs: body (dict), field (str), value (Any).
    Output: JSONResponse.
    Example: _resolve_or_refuse({'identity_key': 'cwd:/x'}, 'hidden', True)
    """
    try:
        key, kind = _key_from_body(body)
    except OverlayWriteError as exc:
        return respond(
            envelope(
                result=None,
                result_status=RESULT_CANNOT_DETERMINE,
                unevaluated=[{"subject": "overlay:identity", "reason": str(exc)}],
            ),
            route="overlay-write",
            field=field,
        )
    return _write(key, kind, field, value)


@router.post(f"{OVERLAY_PREFIX}/name", response_model=None,
             dependencies=[Depends(require_auth)])
async def post_display_name(body: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Set or clear a project's presentation display name.

    Description: a PRESENTATION statement. ``message_projects`` is not
      written, the slug is not written, and the archive's own derived
      name stays in every response as ``archive_display_name``. Send
      ``display_name: null`` to clear the override; the archive name then
      renders again, restored rather than reconstructed because it was
      never overwritten.
    Inputs: body - {identity_key | observed_cwd/project_id,
      display_name: str|null}.
    Output: JSONResponse - envelope; ``result`` is the overlay row.
    Example: POST {"identity_key": "cwd:/Users/j/x", "display_name": "Ops"}
    """
    return _resolve_or_refuse(body, "display_name", body.get("display_name"))


@router.post(f"{OVERLAY_PREFIX}/group", response_model=None,
             dependencies=[Depends(require_auth)])
async def post_group(body: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Assign a project to a group, or remove it from one.

    Description: the group is a label on the project, so a group exists
      exactly as long as a project names it. Send ``group: null`` to
      remove the project from its group; when that was the only thing
      said about the project the overlay row is pruned and the project
      goes back to reporting ``overlay_status: 'none'``.
    Inputs: body - {identity_key | observed_cwd/project_id,
      group: str|null}.
    Output: JSONResponse - envelope; ``result`` is the overlay row.
    Example: POST {"identity_key": "cwd:/Users/j/x", "group": "Client work"}
    """
    return _resolve_or_refuse(body, "group", body.get("group"))


@router.post(f"{OVERLAY_PREFIX}/hidden", response_model=None,
             dependencies=[Depends(require_auth)])
async def post_hidden(body: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Hide a project from the default list, or restore it.

    Description: a SOFT delete. One flag moves. Every transcript, body,
      appearance and content block is untouched, ``/archive/projects``
      still lists the project, export still works, and
      ``GET /archive/overlay/hidden`` plus this call with
      ``hidden: false`` is the restore path. The project's name and group
      are carried through the hide, so unhiding returns it to exactly the
      presentation it had rather than to a default.
    Inputs: body - {identity_key | observed_cwd/project_id,
      hidden: bool}.
    Output: JSONResponse - envelope; ``result`` is the overlay row, or
      None when unhiding pruned a row that then said nothing.
    Example: POST {"identity_key": "cwd:/Users/j/x", "hidden": true}
    """
    return _resolve_or_refuse(body, "hidden", bool(body.get("hidden")))


@router.get(f"{OVERLAY_PREFIX}/rows", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_overlay_rows() -> JSONResponse:
    """List the raw overlay rows, including orphans, for inspection.

    Description: the overlay's own contents, unjoined - what the owner
      has said, whether or not the project it refers to is currently in
      the corpus. This is the route that makes an orphan recoverable by
      hand, and it is a READ: it opens through ``open_read_only``, so
      SQLite would refuse a write on this connection.
    Inputs: none.
    Output: JSONResponse - envelope; ``result`` is the overlay rows.
    Example: GET /api/v1/archive/overlay/rows
    """
    def _read(conn) -> Dict[str, Any]:
        load = load_overlay(conn)
        if not load.ok:
            return _cannot_determine(f"overlay table unreadable: {load.reason}")
        return envelope(
            result=sorted((load.rows or {}).values(), key=lambda r: r["identity_key"]),
            result_status=RESULT_OK,
            meta={"overlay": {"overlay_means": OVERLAY_MEANS}},
        )

    result = run_read(
        state_dir(), _read, subject="archive:overlay", unreadable_result=None
    )
    return respond(result, route="overlay-rows")
