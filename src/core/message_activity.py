"""When a transcript was last WORKED ON, as opposed to when we collected it.

WHY THIS COLUMN EXISTS AT ALL, AND WHY IT IS DENORMALISED.
A project has no timestamp of its own. Its transcripts do, and the
transcripts' messages do, and the two answer different questions:

  - ``message_transcripts.ingested_at`` is when THIS TOOL read the file.
  - ``message_bodies.ts`` is when the OWNER was typing.

Measured on the live corpus 2026-09-02, 80 project rows / 21,039
transcripts / 2,447,028 bodies:

  ingested_at, newest per project   -> lands on exactly TWO days,
                                       2026-08-29 (24 projects) and
                                       2026-08-30 (56 projects).
  message ts,  newest per project   -> spreads across NINE months,
                                       2025-12 through 2026-08, with the
                                       biggest single day holding 5.

So ``ingested_at`` is not a weaker answer to "when did I last work on
this" - it is an answer to a different question, and sorting by it would
put 56 projects in a single tie and call the result an ordering. The
message ``ts`` is the one this module bubbles up.

THE COST IS WHY THE VALUE IS STORED RATHER THAN COMPUTED.
``ts`` lives on ``message_bodies``, which is content-addressed and
deduplicated, so it reaches a transcript only through
``message_appearances`` - 3,125,122 rows. Computing the per-project
maximum on the fly means a full scan of that table plus a row fetch into
a 22 GB ``message_bodies`` for every one of those rows. Measured, same
corpus:

  warm page cache                    3,929 ms
  cold (fresh copy of the database) 14,867 ms
  + covering index on bodies(id,ts) 13,690 ms   (SQLite declines it -
                                                 ``id`` IS the rowid, so
                                                 the PK is already the
                                                 cheapest path)
  + covering index on
      appearances(transcript_id,
                  body_id)          12,528 ms

Indexes do not rescue it, because the cost is 3.1M random row fetches
and no index removes those - only denormalisation does. Against a route
that renders in about 10 ms, none of those numbers is shippable. The
maximum is therefore computed ONCE per transcript, at ingest, and stored
on ``message_transcripts.newest_message_ts``; the project rail then
reads a ``MAX()`` over 21k transcript rows, which folds into the grouped
statement ``fetch_project_rows`` already issues.

THREE OUTCOMES, AND THE COLUMN CANNOT EXPRESS THE THIRD ON ITS OWN.
A NULL in ``newest_message_ts`` means "this transcript's messages carry
no timestamp" - a MEASURED absence, because the backfill and the ingest
path both write the column for every transcript they touch, NULL
included. It does NOT mean "nobody looked". The did-anybody-look
question is a fact about the DATABASE, not about the row: an archive
whose schema predates the column, or a statement that failed, cannot
answer it for any row. That is carried alongside the value as a boolean,
exactly as ``session_counted`` already is - see
``archive_project_names.fetch_project_rows``. The two are never rendered
as each other: a NULL with ``counted=True`` is an answer, and any value
with ``counted=False`` is the absence of one.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: The project has a real newest-activity timestamp.
ACTIVITY_KNOWN = "known"

#: MEASURED absence: the project's transcripts were read and not one of
#: their messages carries a timestamp. A real case, not a hypothetical -
#: 33,480 body rows have a NULL ``ts`` on the live corpus.
ACTIVITY_NONE = "none"

#: NOT MEASURED. The database could not answer the question at all, so
#: this project's position in a time ordering is unknown rather than old.
ACTIVITY_UNKNOWN = "unknown"

#: Every status this module can return, so a caller can assert the set
#: rather than hardcoding three strings it would then have to keep in
#: step.
ACTIVITY_STATUSES = (ACTIVITY_KNOWN, ACTIVITY_NONE, ACTIVITY_UNKNOWN)

#: Prose for the envelope's meta, so a client renders the distinction in
#: the server's words instead of inventing its own.
ACTIVITY_MEANS = (
    "newest_activity_at is the newest message timestamp inside the "
    "project - when the owner was last working in it, NOT when this tool "
    "collected the files. activity_status is 'known' when that produced "
    "a timestamp, 'none' when the project's messages were read and none "
    "carried one, and 'unknown' when the database could not answer at "
    "all. 'none' and 'unknown' are different facts and must not be "
    "sorted as though they were the same."
)

#: The ONE derivation. Both the backfill and the per-transcript ingest
#: update read ``message_bodies.ts`` through ``message_appearances``, so
#: there is no second definition of "newest message" that could drift
#: away from this one.
_GROUPED_ACTIVITY_SQL = """
SELECT a.transcript_id AS transcript_id, MAX(b.ts) AS newest
  FROM message_appearances a
  JOIN message_bodies b ON b.id = a.body_id
 WHERE b.ts IS NOT NULL
 GROUP BY a.transcript_id
"""

#: SQLite refuses more than 999 bound parameters by default, and a
#: transcript can carry thousands of lines, so the ingest lookup is
#: chunked. 500 leaves headroom under that limit without pretending the
#: limit is not there.
_ID_CHUNK = 500


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Description: is this table present? Inputs: conn, table (str).
    Output: bool. Example: _table_exists(conn, 'message_transcripts')"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def has_activity_column(conn: sqlite3.Connection) -> bool:
    """Description: does message_transcripts carry newest_message_ts?
      Read from PRAGMA rather than from a version number, because the
      version says what a migration INTENDED and this says what the
      database actually has. Inputs: conn. Output: bool.
    Example: has_activity_column(conn) -> True"""
    if not _table_exists(conn, "message_transcripts"):
        return False
    return any(
        row[1] == "newest_message_ts"
        for row in conn.execute("PRAGMA table_info(message_transcripts)")
    )


def install_transcript_activity(conn: sqlite3.Connection) -> Optional[int]:
    """Add newest_message_ts if absent, then fill it. Idempotent.

    Description: ``ALTER TABLE ... ADD COLUMN`` is one of the few schema
      edits SQLite performs without rewriting the table, so unlike the
      CHECK relaxation next door (see message_scheme_repair) this needs
      no ``writable_schema`` trickery and cannot cascade anything.

      A NO-OP ON AN ARCHIVE-LESS INSTALL. An install that crossed the
      message-archive versions with the feature gated off has no
      ``message_transcripts`` table at all; that returns None - "there
      was nothing to do" - rather than raising, which is the same shape
      the surrounding steps use.

      THE BACKFILL RUNS EVEN WHEN THE COLUMN ALREADY EXISTS, because the
      expensive half is the only half that can have been interrupted,
      and re-running it is idempotent by construction: it recomputes
      every row from the appearances that are there now.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: int transcripts written, or None if there is no archive here.
    Example: install_transcript_activity(conn)  # -> 21039
    """
    if not _table_exists(conn, "message_transcripts"):
        return None
    if not has_activity_column(conn):
        conn.execute(
            "ALTER TABLE message_transcripts ADD COLUMN newest_message_ts TEXT"
        )
    return backfill_transcript_activity(conn)


def backfill_transcript_activity(conn: sqlite3.Connection) -> int:
    """Write newest_message_ts for EVERY transcript, in two statements.

    Description: one grouped scan of ``message_appearances`` into a temp
      table, then one UPDATE joined against it. The obvious spelling - a
      correlated subquery per transcript - was rejected on measurement,
      not on taste: ``message_appearances`` has no index on
      ``transcript_id``, so that shape is 21,039 full scans of a 3.1M row
      table. The grouped scan is ONE pass, which is the same work the
      route used to do per request and now does once per ingest.

      IT WRITES THE NULLS TOO. A transcript with no timestamped message
      is set to NULL deliberately, so that afterwards a NULL is a
      measured absence rather than a row nobody visited. Skipping them
      would make "no timestamps in this transcript" and "never
      backfilled" the same value, which is the exact collapse this
      module exists to prevent.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: int - transcripts whose column was written (i.e. all of them).
    Example: backfill_transcript_activity(conn)  # -> 21039
    """
    conn.execute("DROP TABLE IF EXISTS temp.transcript_activity")
    conn.execute(
        "CREATE TEMP TABLE transcript_activity AS " + _GROUPED_ACTIVITY_SQL
    )
    # The UPDATE below looks this up once per transcript. Without the
    # index that is 21k scans of the temp table; with it, 21k b-tree
    # probes.
    conn.execute(
        "CREATE INDEX temp.ix_transcript_activity "
        "ON transcript_activity (transcript_id)"
    )
    cur = conn.execute(
        "UPDATE message_transcripts SET newest_message_ts = ("
        "  SELECT newest FROM temp.transcript_activity"
        "   WHERE transcript_id = message_transcripts.id)"
    )
    written = int(cur.rowcount if cur.rowcount is not None else 0)
    conn.execute("DROP TABLE IF EXISTS temp.transcript_activity")
    return written


def newest_ts_for_bodies(
    conn: sqlite3.Connection, body_ids: Iterable[Optional[int]]
) -> Optional[str]:
    """The newest stored ts across these body rows, or None.

    Description: reads the STORED ``message_bodies.ts`` column rather
      than re-deriving the value from the payload, which is what keeps
      the ingest path and :func:`backfill_transcript_activity` from ever
      disagreeing - both read the same column, so there is one
      definition of the timestamp and not two that happen to match
      today. Lookups are by rowid, so a transcript costs one b-tree
      probe per line and nothing else.

      None is returned for BOTH an empty id list and a set of bodies
      that all carry a NULL ts. That collapse is deliberate and safe
      here: at this level they are the same fact - this transcript
      contributes no timestamp - and the distinction that matters
      (measured versus not measured) is made one level up, where it is a
      property of the database rather than of the row.
    Inputs: conn (sqlite3.Connection); body_ids (iterable of int|None -
      Nones are dropped, so a caller can pass the raw per-line values).
    Output: str | None - an ISO-8601 timestamp as stored, never parsed.
    Example: newest_ts_for_bodies(conn, [1, 2, None]) # '2026-08-30T...'
    """
    ids: List[int] = []
    seen = set()
    for value in body_ids:
        if value is None:
            continue
        as_int = int(value)
        if as_int not in seen:
            seen.add(as_int)
            ids.append(as_int)
    newest: Optional[str] = None
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        marks = ",".join("?" * len(chunk))
        row = conn.execute(
            "SELECT MAX(ts) FROM message_bodies "
            f"WHERE id IN ({marks}) AND ts IS NOT NULL",
            chunk,
        ).fetchone()
        value = row[0] if row else None
        # Compared as strings on purpose. These are ISO-8601 UTC stamps
        # written by the producer and stored byte-exactly; lexical order
        # IS chronological order for that format, and parsing them here
        # would invent a datetime the archive never claimed.
        if isinstance(value, str) and (newest is None or value > newest):
            newest = value
    return newest


def classify_activity(newest: Optional[str], counted: bool) -> str:
    """Turn a value plus a did-we-measure flag into one of three tokens.

    Description: the single place the three outcomes are named, so no
      caller re-derives them and gets the boundary wrong. A falsy
      ``counted`` wins over any value, because a timestamp produced by a
      statement that did not run is not a timestamp.
    Inputs: newest (str|None); counted (bool).
    Output: str - one of ACTIVITY_STATUSES.
    Example: classify_activity(None, True) -> 'none'
    """
    if not counted:
        return ACTIVITY_UNKNOWN
    return ACTIVITY_KNOWN if newest else ACTIVITY_NONE


def merge_activity(members: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Bubble several project rows' activity up into one merged node.

    Description: a merged node is one folder that exists on more than one
      machine, so its activity is the NEWEST across its members - the
      owner worked in that folder then, whichever box he was sitting at.

      A SINGLE UNMEASURED MEMBER MAKES THE NODE UNMEASURED. If one row's
      timestamp could not be established, the maximum over the rest is a
      lower bound and not the answer, so reporting it as the answer would
      be a verdict nobody measured. The node keeps whatever value the
      measured members produced - it is still the best evidence there is,
      and the rail shows it - but the status says the number is not
      settled, and the ordering treats it accordingly.
    Inputs: members (sequence of dicts carrying ``newest_activity_at``
      and ``activity_counted``).
    Output: {'newest_activity_at': str|None, 'activity_status': str,
             'activity_counted': bool}
    Example: merge_activity([{'newest_activity_at': 'x',
                              'activity_counted': True}])
    """
    newest: Optional[str] = None
    counted = True
    for member in members:
        if not member.get("activity_counted", False):
            counted = False
        value = member.get("newest_activity_at")
        if isinstance(value, str) and value and (newest is None or value > newest):
            newest = value
    return {
        "newest_activity_at": newest,
        "activity_counted": counted,
        "activity_status": classify_activity(newest, counted),
    }
