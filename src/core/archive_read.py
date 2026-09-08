"""Read-only archive access, the tuning constants, and the query helpers.

Foundation for the message browser API. Plain Python over ``sqlite3``: no
FastAPI types, no async, no server. A route parses its parameters, calls
one function in :mod:`src.core.archive_hierarchy` or
:mod:`src.core.archive_lines`, and returns what it gets back.

THREE OUTCOMES, NEVER TWO, and that contract now lives in
:mod:`src.core.archive_envelope` - split out when this file reached the
repo's 500-line cap. :func:`envelope` and the whole status vocabulary are
RE-EXPORTED here, so every existing import keeps working and there is
still exactly one definition of each. Import them from either name; they
are the same objects.

WHY ``query_only`` AND NOT ``mode=ro``. These are WAL databases, and a
``file:...?mode=ro`` open FAILS when no ``-shm`` sidecar exists: a
read-only connection cannot create the shared-memory index a WAL reader
legitimately needs. ``PRAGMA query_only=ON`` forbids every content write
while still permitting that ``-shm``. :func:`open_read_only` READS THE
PRAGMA BACK rather than trusting that setting it worked.

``message_bodies.ts`` IS NULL ON 33,480 OF 2,447,028 ROWS, and a keyset
predicate on a NULL column evaluates to NULL - not true - so every such
row is silently invisible to a page ordered on ``ts``. Nothing here
orders or pages on ``ts``: lines key on ``line_no`` (UNIQUE per
transcript, never NULL), transcripts on ``(ingested_at, id)`` (both NOT
NULL). ``ts`` is a FIELD only, so no row can go missing - and because
that is a claim, ``transcript_lines`` COUNTS the NULL-``ts`` rows it
returns.

SECRETS ARE FLAGGED, NEVER REDACTED. Nothing here returns, logs, or puts
into an exception any matched value. Bodies come back WHOLE with
``secret_finding_count``; offsets live on the body route so a client
masks locally.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core.archive_envelope import (  # noqa: F401  ONE definition, there.
    RESULT_CANNOT_DETERMINE, RESULT_NOT_FOUND, RESULT_OK, RESULT_PARTIAL,
    RESULT_STATUSES, SCOPE_CANNOT_DETERMINE, SCOPE_NOT_FOUND, SCOPE_RESOLVED,
    SCOPE_STATUSES, cannot_determine_envelope, cursor_error_envelope,
    datastore_unreadable_envelope, envelope, http_status_for,
    not_found_envelope, paging_meta, unread_paging)
from src.core.archive_units import (  # noqa: F401  ONE definition, there.
    BODY_SIZE_UNITS, OFFSET_UNITS_CODE_POINTS, OFFSET_UNITS_UTF16,
    offset_units_meta)
from src.core.db import DatastoreUnreadableError, connect, db_path_for

# --- Constants. Section 4 of docs/message-browser-api.md carries the
# --- measurement behind each number.

#: Hierarchy and transcript pages.
MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 50
#: Line pages. 501 rows of metadata from the largest transcript in the
#: corpus measured 0.0016s, so this cap costs nothing.
MAX_LINE_LIMIT = 500
DEFAULT_LINE_LIMIT = 100
#: 1 MiB. Soft cap on a /lines page carrying bodies. The page stops
#: early and SAYS so; it never trims a body to fit.
DEFAULT_PAGE_BYTES = 1048576
MIN_PAGE_BYTES = 1024
MAX_PAGE_BYTES = 8388608
#: 64 MiB. Above this a body is withheld with a href. Measured: NO body
#: in the corpus reaches it (largest 54,376,859 bytes), so the path is
#: unreachable on real data and its test must be synthetic.
MAX_BODY_BYTES = 67108864
#: Transcripts. SECONDARY search cap; MAX_SCAN_BYTES is primary.
MAX_SCAN_BUDGET = 2000
#: 512 MiB, primary search governor: about 1.2s at the measured rate.
MAX_SCAN_BYTES = 536870912
#: 8 MiB. Above this verify-before-send is refused and the caller must
#: stream. 223 of 21,039 transcripts exceed it.
VERIFY_BEFORE_SEND_MAX_BYTES = 8388608
#: Measured scan rate, used ONLY to render a predicted cost in meta and
#: never to decide anything.
SCAN_BYTES_PER_SECOND = 440000000

# --- body_state vocabulary ------------------------------------------------

BODY_NOT_REQUESTED = "not_requested"
BODY_INCLUDED = "included"
#: A body exists and exceeds MAX_BODY_BYTES; follow ``body_href``.
BODY_WITHHELD_TOO_LARGE = "withheld_too_large"
#: There IS no body - a blank or invalid-JSON line. Distinct from
#: withheld_too_large: one means nothing exists, the other means
#: something exists and is not in this response.
BODY_ABSENT = "absent"

BODY_STATES = frozenset(
    {BODY_NOT_REQUESTED, BODY_INCLUDED, BODY_WITHHELD_TOO_LARGE, BODY_ABSENT}
)

# --- attribution_state, DERIVED by this API and never stored --------------

ATTRIBUTION_EVIDENCED = "evidenced"
ATTRIBUTION_CLAIMED = "claimed"
ATTRIBUTION_CANNOT_DETERMINE = "cannot_determine"

_ATTRIBUTION_BY_HOST_VALUE: Dict[str, str] = {
    "manifest_verified": ATTRIBUTION_EVIDENCED,
    "declared": ATTRIBUTION_CLAIMED,
    "cannot_determine": ATTRIBUTION_CANNOT_DETERMINE,
}

#: Route prefix, so hrefs are built in one place rather than by string
#: literal at eleven call sites that would drift apart.
API_PREFIX = "/api/v1/archive"


def body_href(body_id: int) -> str:
    """Build the canonical href for one body.

    Inputs: body_id (int). Output: str.
    Example: body_href(88) -> '/api/v1/archive/bodies/88'
    """
    return f"{API_PREFIX}/bodies/{body_id}"


# --- The connection -------------------------------------------------------


def open_read_only(state_dir: Path) -> sqlite3.Connection:
    """Open cloude.db for reading and REFUSE every write on this connection.

    Description: ``connect(create=False)`` so a typo'd state directory
      raises instead of manufacturing an empty database that renders as a
      healthy install with no data. Then ``PRAGMA query_only=ON``, READ
      BACK: setting a pragma is a request, reading 1 is a measurement.
    Inputs: state_dir (Path) - as resolved by Settings.get_state_dir().
    Output: sqlite3.Connection, ``row_factory = sqlite3.Row``.
    Raises: DatastoreUnreadableError - missing, unopenable, or the
      read-only pragma would not take.
    Example: with closing(open_read_only(sd)) as c: c.execute("SELECT 1")
    """
    path = db_path_for(Path(state_dir))
    conn = connect(path, create=False)
    try:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute("PRAGMA query_only").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise DatastoreUnreadableError(
            f"could not set query_only on {path.name}: {exc}", path
        ) from exc
    if row is None or int(row[0]) != 1:
        conn.close()
        raise DatastoreUnreadableError(
            f"query_only did not take on {path.name}; refusing to hand out a "
            f"connection that could write to the archive",
            path,
        )
    return conn


def run_read(
    state_dir: Path,
    operation: Callable[..., Dict[str, Any]],
    /,
    *args: Any,
    subject: str = "datastore",
    unreadable_result: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Open the archive read-only, run one read, and always close it.

    Description: the seam where "the database would not open" becomes a
      cannot_determine envelope instead of an unexplained 500 or - far
      worse - an empty list. ONLY DatastoreUnreadableError is caught; a
      sqlite error from the operation itself is a query defect and stays
      loud.
    Inputs: state_dir (Path), operation (callable taking a connection
      first, returning an envelope), *args/**kwargs forwarded to it,
      subject (str), unreadable_result (Any) - ``result`` when the open
      fails. Output: dict envelope.
    Example: run_read(state_dir, transcript_header, 4)
    """
    try:
        conn = open_read_only(Path(state_dir))
    except DatastoreUnreadableError as exc:
        return datastore_unreadable_envelope(
            exc, subject=subject, result=unreadable_result
        )
    try:
        return operation(conn, *args, **kwargs)
    finally:
        conn.close()


# --- Shared helpers -------------------------------------------------------


def attribution_state(host_attribution: Optional[str]) -> str:
    """Derive the client-facing attribution state from the stored value.

    Description: DERIVED, never stored. ``cannot_determine`` is never
      upgraded to ``claimed`` - a transcript can be attributed to a host
      AND unevidenced at once, the most easily missed detail in the
      hierarchy. An unrecognised or NULL value maps to cannot_determine.
    Inputs: host_attribution (str | None) - the stored column.
    Output: str - one of the ATTRIBUTION_* constants.
    Example: attribution_state("manifest_verified") -> 'evidenced'
    """
    if host_attribution is None:
        return ATTRIBUTION_CANNOT_DETERMINE
    return _ATTRIBUTION_BY_HOST_VALUE.get(
        str(host_attribution), ATTRIBUTION_CANNOT_DETERMINE
    )


def clamp_limit(limit: Optional[int], *, default: int, maximum: int) -> int:
    """Clamp a caller's page size into the permitted range.

    Description: clamps rather than rejects; the effective value is
      reported in ``meta.paging.limit`` so the client sees what it got.
    Inputs: limit (int | None), default (int), maximum (int).
    Output: int in 1..maximum. Example: clamp_limit(9999, default=50,
      maximum=200) -> 200
    """
    if limit is None:
        return default
    return max(1, min(int(limit), maximum))


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert one sqlite3.Row to a plain dict.

    Inputs: row (sqlite3.Row). Output: dict. Example:
      row_to_dict(conn.execute("SELECT 1 AS a").fetchone()) -> {'a': 1}
    """
    return {key: row[key] for key in row.keys()}


def scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    """Run a single-value query and return that value, or None for no row.

    Inputs: conn (sqlite3.Connection), sql (str), params (sequence).
    Output: first column of the first row, or None. Example:
      scalar(conn, "SELECT COUNT(*) FROM message_hosts") -> 2
    """
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else row[0]


def count_int(value: Any) -> int:
    """Read a SQL aggregate that may be NULL as an int.

    Description: ``SUM(...)`` over zero rows is NULL, not 0. Converted
      explicitly at the one place it is read.
    Inputs: value (Any). Output: int. Example: count_int(None) -> 0
    """
    return int(value or 0)


def paged_rows(rows: List[sqlite3.Row], limit: int) -> Tuple[List[sqlite3.Row], bool]:
    """Split a ``limit + 1`` fetch into the page and a has_more flag.

    Description: fetching one extra row is the only way to compute
      ``has_more`` without lying. ``returned == limit`` reports true on a
      page landing exactly on the end, so the client renders a "load
      more" that yields nothing; a ``COUNT(*)`` is a second query racing
      the first for a number the client never needed.
    Inputs: rows (list of sqlite3.Row) - up to limit + 1, limit (int).
    Output: (page rows, has_more). The extra row is DISCARDED; the next
      cursor is built from the last RETURNED row.
    Example: paged_rows([r1, r2, r3], 2) -> ([r1, r2], True)
    """
    if len(rows) > limit:
        return rows[:limit], True
    return rows, False
