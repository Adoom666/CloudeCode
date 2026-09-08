"""Relax the ``session_ref_scheme`` CHECK in place, and correct the rows.

WHY THIS TABLE IS NEVER REBUILT. SQLite cannot ALTER a CHECK constraint,
so the ordinary remedy is the documented 12-step rebuild: create a new
table, copy, drop the old one, rename. That is UNSAFE HERE, for a reason
that is measurable rather than stylistic:

  message_appearances.transcript_id
    REFERENCES message_transcripts(id) ON DELETE CASCADE

Dropping ``message_transcripts`` therefore cascade-deletes every
appearance row in the archive - millions of them. The rebuild recipe
guards against exactly that with ``PRAGMA foreign_keys=OFF``, and that
pragma IS A NO-OP INSIDE A TRANSACTION, which is where every migration
step in this codebase runs by construction (db_steps.run_chain is called
inside the caller's transaction so a failure rolls the whole chain back).
So the guard the recipe depends on cannot be armed from a migration step,
and a rebuild attempted anyway would be a silent, total data loss whose
only symptom is an empty archive. The step below touches no row of any
table it does not name and drops nothing.

WHAT IT DOES INSTEAD. It edits the stored CREATE TABLE text through
``PRAGMA writable_schema``, which changes the schema SQLite parses without
moving a single page of data. Measured on sqlite 3.53.4: the FK children
survive untouched, ``integrity_check`` and ``foreign_key_check`` stay
clean, and the relaxed CHECK is still ENFORCED afterwards - a value
outside the new set is still refused, which is the negative control that
distinguishes "relaxed" from "removed".

``PRAGMA writable_schema=RESET`` is what makes the backfill possible in
the same transaction: without it the connection keeps its already-parsed
copy of the OLD constraint and the corrective UPDATE is refused by a
CHECK that no longer exists on disk. RESET forces the reparse.

IDEMPOTENT, AND THE TEST FOR THAT IS THE SCHEMA TEXT ITSELF. A database
whose CHECK already lists the new value is left alone rather than
rewritten, and the backfill is expressed as "rows whose stored scheme
disagrees with what the classifier says today", which is empty on a
second run by construction.
"""

from __future__ import annotations

import sqlite3
from typing import List, Tuple

from src.core.message_model_serialize import (
    OPAQUE_SCHEME,
    UUID_SCHEME,
    session_ref_scheme,
)

#: The table whose CHECK is relaxed.
TRANSCRIPTS_TABLE: str = "message_transcripts"

#: The exact constraint text as v16 wrote it, and what it becomes. Matched
#: as a literal substring rather than by a regex: if the stored text is not
#: EXACTLY this, the safe answer is to change nothing and say so, not to
#: guess at a rewrite of a live table's schema.
OLD_CHECK: str = "CHECK (session_ref_scheme IN ('uuid', 'agent'))"
NEW_CHECK: str = "CHECK (session_ref_scheme IN ('uuid', 'agent', 'opaque'))"


class SchemeRepairError(RuntimeError):
    """The CHECK could not be relaxed and the reason is not a sqlite error.

    Description: raised when the stored schema text is neither the old
      form this step knows how to edit nor the new form it produces, so
      the step cannot act without guessing. Raising propagates out of the
      migration's transaction and rolls the chain back, which is correct:
      a half-relaxed constraint is worse than a refused migration.
    """


def stored_table_sql(conn: sqlite3.Connection, table: str) -> str:
    """Read a table's CREATE statement exactly as sqlite_master holds it.

    Description: the authority on what constraint is actually enforced.
      Never inferred from the DDL module, which describes what a FRESH
      database gets, not what this one has.
    Inputs: conn (sqlite3.Connection), table (str) - the table name.
    Output: str - the CREATE TABLE text, or '' when the table is absent.
    Example: stored_table_sql(conn, "message_transcripts")[:12] -> 'CREATE TABLE'
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or row[0] is None:
        return ""
    return str(row[0])


def check_allows_opaque(conn: sqlite3.Connection) -> bool:
    """Say whether the live CHECK already permits the opaque scheme.

    Description: reads the enforced constraint text, not a version number
      and not a code constant. The migration is idempotent because this
      answers True on a second run.
    Inputs: conn (sqlite3.Connection).
    Output: bool.
    Example: check_allows_opaque(conn) -> False
    """
    return NEW_CHECK in stored_table_sql(conn, TRANSCRIPTS_TABLE)


def relax_scheme_check(conn: sqlite3.Connection) -> bool:
    """Widen the session_ref_scheme CHECK in place, rewriting no row.

    Description: edits sqlite_master's CREATE TABLE text under
      ``PRAGMA writable_schema`` and forces a reparse with RESET so the
      SAME transaction can go on to write the new value. No DROP, no
      CREATE, no INSERT SELECT, so the FK children described in this
      module's header are never at risk. Does nothing at all when the
      table is absent (a build with the message archive gated off) or
      when the constraint has already been widened.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: bool - True when this call performed the edit, False when
      there was nothing to do. False is not a failure; it is the
      idempotent path and the archive-disabled path.
    Raises: SchemeRepairError - the table exists but its stored text
      matches neither the old constraint nor the new one, so the step
      cannot edit it without guessing.
    Example: relax_scheme_check(conn) -> True
    """
    sql = stored_table_sql(conn, TRANSCRIPTS_TABLE)
    if sql == "":
        return False
    if NEW_CHECK in sql:
        return False
    if OLD_CHECK not in sql:
        raise SchemeRepairError(
            f"{TRANSCRIPTS_TABLE} does not carry the expected "
            f"session_ref_scheme CHECK. Found: {sql!r}"
        )
    conn.execute("PRAGMA writable_schema=ON")
    try:
        conn.execute(
            "UPDATE sqlite_master SET sql = ? "
            "WHERE type = 'table' AND name = ?",
            (sql.replace(OLD_CHECK, NEW_CHECK), TRANSCRIPTS_TABLE),
        )
    finally:
        # RESET, not OFF: it both clears the writable flag and forces this
        # connection to reparse the schema it just changed. With OFF the
        # backfill below is refused by a constraint that is no longer on
        # disk, and that refusal looks exactly like a real CHECK violation.
        conn.execute("PRAGMA writable_schema=RESET")
    return True


def misclassified_rows(conn: sqlite3.Connection) -> List[Tuple[int, str, str]]:
    """Find rows whose stored scheme disagrees with the classifier.

    Description: the corrective pass is expressed as a disagreement, not
      as a hardcoded list of refs. Scoped to rows currently recorded as
      UUID_SCHEME, because that is the only value the old two-way
      classifier could produce by elimination: an 'agent' row was reached
      by a positive prefix match that has not changed, so re-deriving
      those would put this step's blast radius over 19,588 rows it has no
      reason to touch. Deliberately NOT a rewrite of every row.
    Inputs: conn (sqlite3.Connection).
    Output: list of (id, session_ref, correct_scheme), id-ordered. Empty
      when the table is absent or nothing disagrees.
    Example: misclassified_rows(conn) -> [(19551, 'audit', 'opaque')]
    """
    if stored_table_sql(conn, TRANSCRIPTS_TABLE) == "":
        return []
    found: List[Tuple[int, str, str]] = []
    for row in conn.execute(
        f"SELECT id, session_ref FROM {TRANSCRIPTS_TABLE} "
        "WHERE session_ref_scheme = ? ORDER BY id",
        (UUID_SCHEME,),
    ).fetchall():
        correct = session_ref_scheme(str(row[1]))
        if correct != UUID_SCHEME:
            found.append((int(row[0]), str(row[1]), correct))
    return found


def backfill_opaque_scheme(conn: sqlite3.Connection) -> int:
    """Correct every row the old two-way classifier got wrong.

    Description: writes OPAQUE_SCHEME onto exactly the rows
      :func:`misclassified_rows` names, by id, one statement per row so
      the set written is the set measured rather than a WHERE clause that
      could drift from it. Requires the CHECK to have been relaxed first;
      calling it before that raises sqlite3.IntegrityError rather than
      silently skipping, which is the honest failure.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: int - how many rows were corrected. 0 on a second run.
    Raises: sqlite3.IntegrityError - the CHECK still refuses the value.
    Example: backfill_opaque_scheme(conn) -> 19
    """
    rows = misclassified_rows(conn)
    for transcript_id, _ref, correct in rows:
        conn.execute(
            f"UPDATE {TRANSCRIPTS_TABLE} SET session_ref_scheme = ? "
            "WHERE id = ?",
            (correct, transcript_id),
        )
    return len(rows)


def repair_session_ref_schemes(conn: sqlite3.Connection) -> Tuple[bool, int]:
    """Relax the CHECK and correct the rows, in that order, in one call.

    Description: the whole of schema step 19 -> 20, and the body the
      off-to-on materializer can also call. Order is load-bearing: the
      backfill writes a value the old constraint forbids.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
    Output: (relaxed, corrected) - whether this call widened the CHECK,
      and how many rows it moved to OPAQUE_SCHEME.
    Example: repair_session_ref_schemes(conn) -> (True, 19)
    """
    relaxed = relax_scheme_check(conn)
    if stored_table_sql(conn, TRANSCRIPTS_TABLE) == "":
        return relaxed, 0
    return relaxed, backfill_opaque_scheme(conn)
