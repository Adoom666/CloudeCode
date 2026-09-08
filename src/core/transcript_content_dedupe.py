"""Content-addressed ingest idempotency - schema v22.

WHY THIS EXISTS, AND WHAT IT COST NOT TO HAVE IT. Until v22 the corpus
ingester's idempotency key was ``(source_path, content_sha256)``
evaluated in that order, and the ordering was the whole defect: the path
lookup came FIRST (``_latest_archive_for_source``), and the content hash
was only ever compared when that lookup HIT. A file whose exact bytes
this database already held, arriving under a ``source_path`` it had never
seen, produced ``existing = None`` and fell straight through to a second
full copy recorded as ``growth_kind='initial'``.

That is not a hypothetical. When ``~/Development`` became a symlink,
Claude Code derived a different slug for every project directory under
``~/.claude/projects``, so every ``source_path`` in the corpus changed at
once. The next pass re-stored 19,294 files whose ``content_sha256`` was
already in the database - 3.78 GB, the entire corpus held twice under two
path encodings - with no error, no warning, and no finding anywhere.

PATH CANONICALISATION IS NOT THE FIX, and reaching for it is the
tempting wrong turn. The two encodings are two different DIRECTORY NAMES
that both exist, side by side, inside ``~/.claude/projects`` - not two
paths to one file. ``Path.resolve()`` has nothing to resolve. Content
addressing is the only key that can recognise them as the same
transcript, which is why this module keys on the hash and nothing else.

THE MECHANISM IS THE ONE PREFIX DEDUPE ALREADY USES, NOT A SECOND ONE.
``transcript_prefix_dedupe`` already established the shape: a row whose
bytes live in ANOTHER row carries a near-empty sentinel in
``content_gzip`` and points at that row through
``superseded_by_archive_id``, and
:func:`~src.core.transcript_archive.export_archive` walks that pointer,
decompresses once at the end of the chain, and slices to the ORIGINALLY
REQUESTED row's own ``raw_byte_length``. A content duplicate is exactly
that shape with the slice being a no-op, because the two rows' contents
are byte-identical and therefore the same length. Nothing in
``export_archive`` needed changing, and nothing here re-implements it.

THE DIRECTION IS THE ONLY DIFFERENCE, AND IT IS SAFE BY CONSTRUCTION.
Prefix dedupe points an OLD row forward at a NEW one. This points a NEW
row backward at an EXISTING one. A cycle would need the target's chain to
reach the new row, and the new row does not exist when the target is
chosen - so it cannot. (``export_archive`` keeps its cycle guard anyway;
this note explains why it never fires here, it does not replace it.)

WHAT A CONTENT-DUPLICATE ROW IS, PRECISELY. A REAL, first-class archive
row for its own ``source_path``, carrying its own ``archive_uuid``, its
own ``ingested_at``, its own copy of the derived line index in
``transcript_records``, and the SAME ``content_sha256`` /
``raw_byte_length`` / line-ending facts as the row it points at (they are
the same bytes; any other value would be a lie). It differs from an
ordinary row in exactly two ways: ``content_gzip`` is the sentinel, and
``dedupe_kind`` says ``'content_duplicate'`` so a reader never has to
infer that from the shape of two other columns.

``growth_kind`` stays ``'initial'``, and that is not a compromise. It IS
the first archive for that source_path; the column answers "how did this
version relate to the previous version of THIS path", and the answer is
"there was no previous version". The reason its bytes are elsewhere is a
different question, and ``dedupe_kind`` is where that question is
answered. (There is also a hard constraint: ``growth_kind`` carries a
CHECK listing three values, SQLite cannot widen a CHECK via ALTER TABLE
ADD COLUMN, and rebuilding this table is forbidden by the migration
chain's additive-only rule.)

THE LINE INDEX IS COPIED, NOT SHARED. ``transcript_records`` rows are
derived scalars - line number, byte offset, byte length, status, type,
uuids, timestamp - and hold no copy of any line's bytes. Their offsets
address the DECOMPRESSED content, which is identical for both rows, so
copying them is correct by construction and costs no blob storage. It is
done rather than skipped because every existing reader that joins
``transcript_records`` on ``archive_id`` keeps working with no special
case for a deduped row, which is the whole point of making the new row a
real row.

MASS RE-ARCHIVE IS NOW LOUD. A handful of content duplicates in a pass is
ordinary (a file copied, a project re-opened from a second checkout).
Nineteen thousand of them means the corpus itself was re-encoded, and the
2026-08-31 incident produced no signal at all. :func:`mass_rearchive_detail`
plus the caller's ``record_finding`` write one ADVISORY
``message_ingest_findings`` row per pass that crosses
:data:`MASS_REARCHIVE_THRESHOLD`. Advisory, not stop: with this module in
place no harm has occurred - the pass stores metadata, not bytes - so
holding anything at a gate would be furniture. What the finding buys is
that the corpus moving under this tool's feet can never again be silent.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not touch, rewrite or
reclaim the 3.78 GB of duplicates the old key already wrote. Those are
full, valid, independent archives; collapsing them is a separate
migration with its own verification, and the owner has not asked for it.
This prevents recurrence, and stops there.
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid
import zlib
from dataclasses import dataclass
from typing import Optional

try:
    import structlog

    logger = structlog.get_logger()
except ImportError:  # pragma: no cover - see transcript_archive.py's own guard
    class _NoOpLogger:
        def __getattr__(self, _name: str):
            return lambda *a, **k: None

    logger = _NoOpLogger()

from src.core.db_models import DEDUPE_KIND_CONTENT_DUPLICATE
from src.core.transcript_archive import ZLIB_LEVEL
from src.core.trail_entry import utc_now

#: The same near-empty placeholder ``transcript_prefix_dedupe`` writes -
#: recomputed here rather than imported so this module does not depend on
#: a private name, but produced from the identical inputs so the two are
#: byte-equal.
_SENTINEL_GZIP = zlib.compress(b"", ZLIB_LEVEL)

#: How many content duplicates in ONE pass stop being ordinary and start
#: being evidence that the corpus was re-encoded. Set at 25 deliberately
#: rather than at a round 1 or 1000: a handful (a copied file, a project
#: opened from a second checkout, a restored backup) is normal shape and
#: gating on it would produce a finding every pass, which is the
#: "furniture" failure - a check that never clears is not a monitor. The
#: incident this exists for produced 19,294 in a single pass, four orders
#: of magnitude above this line, so the threshold has enormous margin in
#: the direction that matters and none of the false-positive cost.
MASS_REARCHIVE_THRESHOLD: int = 25

#: Every column a content-duplicate row copies verbatim from the row that
#: holds its bytes. Each one is a FACT ABOUT THE CONTENT, and the content
#: is byte-identical, so any other value would be a lie - these are not
#: defaults, they are the same measurement.
_COPIED_CONTENT_COLUMNS = (
    "content_sha256",
    "raw_byte_length",
    "line_ending",
    "has_trailing_newline",
    "trailing_blank_line_count",
    "record_count",
    "invalid_json_line_count",
    "claude_session_uuid",
)


@dataclass(frozen=True)
class ContentMatch:
    """An existing archive whose bytes are exactly the bytes about to be stored.

    Description: the return of :func:`find_archive_by_content`. Frozen
      because it is a measurement, not a working value.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    archive_id: int
    source_path: str
    raw_byte_length: int


def find_archive_by_content(
    conn: sqlite3.Connection, content_sha256: str
) -> Optional[ContentMatch]:
    """Find any archive already holding these exact bytes, anywhere.

    Description: the read half of the content-addressed idempotency key -
      GLOBAL, deliberately unscoped by source_path, because a path scope
      is precisely what the old key had and precisely what let a renamed
      corpus re-archive itself. Backed by
      ``ix_transcript_archives_content_sha`` (v22); without that index
      this is a full table scan on every file of every pass.

      PREFERS A ROW THAT STILL HOLDS REAL CONTENT
      (``superseded_by_archive_id IS NULL``), lowest id first, so a new
      duplicate attaches to the end of an existing chain rather than
      lengthening it - and falls back to the newest row with this hash
      when every candidate is itself superseded, which still resolves
      correctly because ``export_archive`` walks the whole chain.
    Inputs: conn (sqlite3.Connection). content_sha256 (str) - hex digest
      of the bytes about to be stored.
    Output: ContentMatch | None - None means these bytes are genuinely
      new to this database.
    Example: find_archive_by_content(conn, "ab12...")  # -> ContentMatch(...)
    """
    row = conn.execute(
        "SELECT id, source_path, raw_byte_length FROM transcript_archives"
        " WHERE content_sha256 = ?"
        " ORDER BY (superseded_by_archive_id IS NOT NULL) ASC, id ASC"
        " LIMIT 1",
        (content_sha256,),
    ).fetchone()
    if row is None:
        return None
    return ContentMatch(
        archive_id=int(row["id"]),
        source_path=row["source_path"],
        raw_byte_length=int(row["raw_byte_length"]),
    )


def store_content_duplicate(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source_path: str,
    match: ContentMatch,
    source_mtime: Optional[str] = None,
    now: Optional[str] = None,
) -> int:
    """Record a new archive row for a path whose bytes are already stored.

    Description: writes ONE metadata-only ``transcript_archives`` row plus
      a copy of the source row's derived ``transcript_records`` index, and
      stores no content bytes at all - ``content_gzip`` is the sentinel and
      ``superseded_by_archive_id`` points at ``match.archive_id``, which is
      the exact shape ``export_archive`` already knows how to read.

      NOT INSIDE ITS OWN TRANSACTION, unlike ``ingest_with_prefix_dedupe``:
      the caller opens one, because the archive row and its record index
      must land together or not at all. A half-written duplicate with no
      line index would be a row every ``transcript_records`` join silently
      under-reports.
    Inputs: conn (sqlite3.Connection) - INSIDE a transaction. kind (str) -
      "session" or "subagent". source_path (str) - the NEW path. match
      (ContentMatch) - from :func:`find_archive_by_content`. source_mtime
      (str | None). now (str | None) - ISO-8601; defaults to utc_now().
    Output: int - the new transcript_archives.id.
    Raises: LookupError - ``match.archive_id`` names no row (it was read
      moments ago, so this means a concurrent delete, and inventing a row
      from partial knowledge is not an option).
    Example:
        with transaction(conn):
            new_id = store_content_duplicate(
                conn, kind="session", source_path="slug2/x.jsonl",
                match=match,
            )
    """
    columns = ", ".join(_COPIED_CONTENT_COLUMNS)
    src = conn.execute(
        f"SELECT {columns} FROM transcript_archives WHERE id = ?",
        (match.archive_id,),
    ).fetchone()
    if src is None:
        raise LookupError(
            f"no transcript_archives row with id={match.archive_id}"
        )

    stamp = now or utc_now()
    values = [src[name] for name in _COPIED_CONTENT_COLUMNS]
    cur = conn.execute(
        "INSERT INTO transcript_archives ("
        " archive_uuid, kind, source_path, content_gzip,"
        f" compressed_byte_length, {columns},"
        " root_state, growth_kind, dedupe_kind,"
        " superseded_by_archive_id, ingested_at, ingest_source_mtime"
        ") VALUES (?, ?, ?, ?, ?, "
        + ", ".join("?" for _ in _COPIED_CONTENT_COLUMNS)
        + ", 'unrooted', 'initial', ?, ?, ?, ?)",
        [
            str(_uuid.uuid4()),
            kind,
            source_path,
            _SENTINEL_GZIP,
            len(_SENTINEL_GZIP),
            *values,
            DEDUPE_KIND_CONTENT_DUPLICATE,
            match.archive_id,
            stamp,
            source_mtime,
        ],
    )
    new_id = int(cur.lastrowid)

    # THE LINE INDEX IS DERIVED SCALARS, NOT BYTES. Offsets address the
    # decompressed content, which is identical for both rows, so this copy
    # is correct by construction and costs no blob storage. Copying it is
    # what keeps every existing transcript_records reader working against
    # a deduped row with no special case.
    conn.execute(
        "INSERT INTO transcript_records"
        " (archive_id, line_no, byte_offset, byte_length, status,"
        "  record_type, record_uuid, parent_uuid, ts)"
        " SELECT ?, line_no, byte_offset, byte_length, status,"
        "  record_type, record_uuid, parent_uuid, ts"
        " FROM transcript_records WHERE archive_id = ?"
        " ORDER BY line_no",
        (new_id, match.archive_id),
    )

    logger.info(
        "transcript_content_duplicate_stored",
        source_path=source_path,
        new_archive_id=new_id,
        holds_content_archive_id=match.archive_id,
        holds_content_source_path=match.source_path,
        raw_byte_length=match.raw_byte_length,
    )
    return new_id


def mass_rearchive_detail(
    *, duplicate_count: int, bytes_not_restored: int, sample_paths
) -> str:
    """Compose the finding sentence for a pass that met the threshold.

    Description: separated from the write so the sentence is testable
      without a database, and so the caller cannot invent a different
      wording on some other path. States what was measured, what it means
      and what it would have cost - the numbers a human needs to decide
      whether their corpus moved on purpose.
    Inputs: duplicate_count (int) - files this pass recognised by content
      at a new path. bytes_not_restored (int) - the raw bytes NOT written
      a second time because of it. sample_paths (Sequence[str]) - up to a
      few of the new paths, for orientation.
    Output: str - never blank (record_finding refuses a blank detail).
    Example: mass_rearchive_detail(duplicate_count=30,
      bytes_not_restored=99, sample_paths=["a"])
    """
    sample = ", ".join(list(sample_paths)[:3])
    return (
        f"{duplicate_count} files in one pass had a content_sha256 already "
        f"stored under a DIFFERENT source_path (threshold "
        f"{MASS_REARCHIVE_THRESHOLD}). This is the signature of the corpus "
        f"being re-encoded under new path names - the 2026-08-31 symlink "
        f"case. Content addressing stored metadata rows instead of bytes, "
        f"so {bytes_not_restored} bytes were NOT archived a second time. "
        f"Check whether the corpus root moved on purpose. "
        f"Examples: {sample or '(none recorded)'}"
    )
