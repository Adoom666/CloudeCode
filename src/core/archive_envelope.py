"""The three-outcome envelope and the status vocabulary it enforces.

Split out of :mod:`src.core.archive_read` so neither file passes the
repo's 500-line cap. The seam is the contract itself: everything here
decides WHAT SHAPE AN ANSWER HAS, while ``archive_read`` owns the
read-only connection and the small query helpers.

THREE OUTCOMES, NEVER TWO. Every response says pass, fail, or COULD NOT
EVALUATE, and the third is never folded into either other. An empty
``result`` is meaningless alone: ``("ok", [])``, ``("partial", [])`` and
``("cannot_determine", None)`` render identically to a client that reads
only ``result``, and they mean three different things. :func:`envelope`
is the SINGLE constructor for that shape, which is what makes omitting
the third state impossible - there is no other way to build a response.

THE result SHAPE IS NORMATIVE, and section 3.1 of
``docs/message-browser-api.md`` states it for clients:

* ``cannot_determine`` carries ``result: null`` on EVERY endpoint, list
  or not. The question was not evaluated, so there is no payload of any
  shape, and ``[]`` would let a client that reads only ``result`` render
  a confident empty state over a question nobody answered.
* ``not_found`` carries the route's SUCCESS shape - ``[]`` for a
  collection route, ``null`` for a single-object route - because "there
  is no project 99999" is a MEASUREMENT and ``scope_status`` carries it.
* ``ok`` and ``partial`` carry the natural payload; ``[]`` under ``ok``
  means GENUINELY EMPTY.

:func:`cannot_determine_envelope` defaults ``result`` to None to make the
first rule the path of least resistance, but it is a default and not a
guard - a caller can still pass ``[]``, which is how two endpoints
drifted out of the rule before it was written down.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.core.archive_cursor import CursorError
from src.core.db import DatastoreUnreadableError

# --- result_status vocabulary ---------------------------------------------

#: Asked and fully answered. An empty result here means GENUINELY EMPTY.
RESULT_OK = "ok"
#: Answered, but some work was not reached. ``unevaluated`` names what.
RESULT_PARTIAL = "partial"
#: Could not be evaluated. NEVER rendered as a healthy empty list.
RESULT_CANNOT_DETERMINE = "cannot_determine"
#: The named subject does not exist - a measurement, unlike the above.
RESULT_NOT_FOUND = "not_found"

RESULT_STATUSES = frozenset(
    {RESULT_OK, RESULT_PARTIAL, RESULT_CANNOT_DETERMINE, RESULT_NOT_FOUND}
)

# --- scope_status vocabulary ----------------------------------------------

SCOPE_RESOLVED = "resolved"
SCOPE_NOT_FOUND = "not_found"
SCOPE_CANNOT_DETERMINE = "cannot_determine"

SCOPE_STATUSES = frozenset({SCOPE_RESOLVED, SCOPE_NOT_FOUND, SCOPE_CANNOT_DETERMINE})

def envelope(
    *,
    result: Any,
    result_status: str,
    scope_status: str = SCOPE_RESOLVED,
    unevaluated: Optional[Sequence[Mapping[str, str]]] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Wrap any route's payload in the three-outcome envelope.

    Description: the ONLY place this shape is constructed. A response
      with no ``result_status`` cannot be distinguished by a client from
      one that was never evaluated. Every key is ALWAYS present:
      ``unevaluated`` is an empty list rather than an omitted field, so
      "nothing was skipped" is a statement, not an absence to guess at.
    Inputs: result (Any) - the payload, may be [], {} or None.
      result_status (str) - one of RESULT_STATUSES. scope_status (str) -
      one of SCOPE_STATUSES. unevaluated (sequence of {subject, reason}).
      meta (Mapping) - route-specific extras: paging, scan, timing.
    Output: dict with exactly result, result_status, scope_status,
      unevaluated, meta.
    Raises: ValueError - an unknown status string, or an ``unevaluated``
      entry that is not a {subject, reason} mapping. Loud, because a
      dropped reason is how a cannot-determine becomes an ok.
    Example: envelope(result=[], result_status=RESULT_OK)["unevaluated"] -> []
    """
    if result_status not in RESULT_STATUSES:
        raise ValueError(
            f"unknown result_status {result_status!r}; "
            f"permitted values are {sorted(RESULT_STATUSES)}"
        )
    if scope_status not in SCOPE_STATUSES:
        raise ValueError(
            f"unknown scope_status {scope_status!r}; "
            f"permitted values are {sorted(SCOPE_STATUSES)}"
        )
    entries: List[Dict[str, str]] = []
    for entry in unevaluated or ():
        if (
            not isinstance(entry, Mapping)
            or "subject" not in entry
            or "reason" not in entry
        ):
            raise ValueError(
                "every unevaluated entry must be a mapping with 'subject' "
                f"and 'reason'; got {entry!r}"
            )
        entries.append(
            {"subject": str(entry["subject"]), "reason": str(entry["reason"])}
        )
    return {
        "result": result,
        "result_status": result_status,
        "scope_status": scope_status,
        "unevaluated": entries,
        "meta": dict(meta) if meta else {},
    }


def http_status_for(result_status: str, *, cursor_error: bool = False) -> int:
    """Map a result_status to the HTTP status the route should return.

    Description: ``cannot_determine`` is a 200 carrying an honest
      refusal - the server DID answer, it answered "I could not evaluate
      this". The exception is a malformed cursor: a client error, so 400,
      so the client's tooling notices instead of the failure hiding in a
      200 body.
    Inputs: result_status (str), cursor_error (bool).
    Output: int - 200, 400 or 404. Raises ValueError on an unknown one.
    Example: http_status_for("cannot_determine", cursor_error=True) -> 400
    """
    if result_status not in RESULT_STATUSES:
        raise ValueError(f"unknown result_status {result_status!r}")
    if result_status == RESULT_NOT_FOUND:
        return 404
    if result_status == RESULT_CANNOT_DETERMINE and cursor_error:
        return 400
    return 200


def paging_meta(
    *, limit: int, returned: int, has_more: Optional[bool], next_cursor: Optional[str]
) -> Dict[str, Any]:
    """Build the ``meta.paging`` block.

    Description: ``has_more`` is Optional on purpose. ``False`` is a
      CLAIM that the end of the list was reached, and a route that never
      read the list has no basis for it - so not_found and
      cannot_determine report None, never False.
    Inputs: limit (int), returned (int), has_more (bool | None),
      next_cursor (str | None). Output: dict.
    Example: paging_meta(limit=50, returned=1, has_more=False,
             next_cursor=None)
    """
    return {
        "limit": limit,
        "returned": returned,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def unread_paging(limit: int) -> Dict[str, Any]:
    """Build ``meta.paging`` for a page that was never read.

    Description: shorthand for every failure path, so no call site has to
      remember that ``has_more`` must be None rather than False.
    Inputs: limit (int). Output: dict.
    Example: unread_paging(50)["has_more"] is None -> True
    """
    return paging_meta(limit=limit, returned=0, has_more=None, next_cursor=None)


def not_found_envelope(
    subject: str, reason: str, *, result: Any, meta: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Build the envelope for a scope that provably does not exist.

    Description: ``result`` is the caller's choice - ``[]`` for a list
      route, None for a single-object route - because "there is no
      project 99999" is a MEASUREMENT and ``scope_status`` carries it. A
      client must not render it as "no transcripts".
    Inputs: subject (str) e.g. "project:99999", reason (str), result
      (Any), meta (Mapping | None).
    Output: envelope, result_status and scope_status both not_found.
    Example: not_found_envelope("project:9", "no row", result=[])
    """
    return envelope(
        result=result,
        result_status=RESULT_NOT_FOUND,
        scope_status=SCOPE_NOT_FOUND,
        unevaluated=[{"subject": subject, "reason": reason}],
        meta=meta,
    )


def cannot_determine_envelope(
    subject: str,
    reason: str,
    *,
    result: Any = None,
    scope_status: str = SCOPE_RESOLVED,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the envelope for a question that could not be evaluated.

    Description: ``result`` defaults to None rather than ``[]`` so a
      client that ignores ``result_status`` and iterates ``result``
      crashes instead of rendering a confident empty state over a
      question nobody answered.
    Inputs: subject (str), reason (str) - what could not be evaluated and
      why. result (Any), scope_status (str), meta (Mapping | None).
    Output: dict envelope. Example: ("cursor", "invalid base64url")
    """
    return envelope(
        result=result,
        result_status=RESULT_CANNOT_DETERMINE,
        scope_status=scope_status,
        unevaluated=[{"subject": subject, "reason": reason}],
        meta=meta,
    )


def cursor_error_envelope(
    exc: CursorError, *, limit: int, result: Any = None
) -> Dict[str, Any]:
    """Build the 400 envelope for a cursor that would not parse.

    Description: pair with ``http_status_for(..., cursor_error=True)``.
      NOT a silent restart at page 1: a client paging thousands of rows
      that silently restarts renders duplicates forever, never finishes,
      and raises no error for anyone to see.
    Inputs: exc (CursorError), limit (int) - the requested page size, so
      the paging block still describes what was asked for. result (Any).
    Output: envelope, cannot_determine, scope still resolved - the scope
      was fine, the cursor was not.
    Example: cursor_error_envelope(err, limit=50)["result_status"]
    """
    return cannot_determine_envelope(
        "cursor", str(exc), result=result, meta={"paging": unread_paging(limit)}
    )


def datastore_unreadable_envelope(
    exc: DatastoreUnreadableError, *, subject: str = "datastore", result: Any = None
) -> Dict[str, Any]:
    """Build the envelope for a datastore that would not open.

    Description: the scope itself could not be resolved, so
      ``scope_status`` is cannot_determine too. An empty archive answers
      ``("ok", [])``; an unopenable one answers
      ``("cannot_determine", None)``. The two cannot be confused.
    Inputs: exc (DatastoreUnreadableError), subject (str), result (Any).
    Output: dict envelope. Example: ...["result"] is None -> True
    """
    return envelope(
        result=result,
        result_status=RESULT_CANNOT_DETERMINE,
        scope_status=SCOPE_CANNOT_DETERMINE,
        unevaluated=[{"subject": subject, "reason": str(exc)}],
        meta={},
    )
