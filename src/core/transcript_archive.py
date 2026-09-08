"""Byte-exact transcript archive: ingest, export, verify, and rooting.

WHY THIS EXISTS. The owner's requirement, verbatim: "once we can confirm
all my history is preserved, even without the web interface i can consider
it 100% backed up... id like the ability to export a conversation from the
database and its byte for byte intact." Measured against the real corpus
(``~/.claude/projects`` on mac-mini-m4, 1477 files, 111,061 JSONL lines,
2026-08-29): ``json.dumps(json.loads(line))`` came back byte-identical on
**0 of 111,061 lines** - json.dumps's default ``", "`` / ``": "``
separators never match the source's unspaced ``","`` / ``":"``. A store
that keeps only parsed fields cannot reconstruct a single real line.

THE RESOLUTION. One copy, not two. :func:`ingest_transcript_bytes` writes
the ENTIRE source file's ORIGINAL BYTES into ``transcript_archives.
content_gzip`` (zlib-compressed), and :func:`export_archive` is a pure
decompress - nothing is re-serialized, so CRLF, a missing trailing
newline, a trailing blank line, and a line that fails to parse as JSON all
survive automatically, because none of them are interpreted to
reconstruct the file. ``transcript_records`` holds no second copy of any
line's bytes - only scalar fields DERIVED by parsing at ingest time (type,
uuid, parentUuid, timestamp, and the line's byte range inside the
decompressed blob), so the database is queryable without ever re-parsing
the blob for a list view.

THREE OUTCOMES, NEVER TWO. :func:`verify_against_source` returns
``byte_identical``, ``mismatch`` (with the first differing byte offset and
a short hexdump of both sides), or ``could_not_evaluate`` (the source
could not be read, or the stored archive could not be decompressed). A
file that could not be read is never counted as a pass.

ROOTING. A transcript is not always attributable to a session the moment
it is read. ``root_state`` on ``transcript_archives`` is 'unrooted'
(default, pending human attribution), 'rooted' (a session transcript
points at ``root_session_id``; a subagent transcript points at
``parent_archive_id``), or 'orphaned' (a human looked and found no root -
terminal, so it stops nagging the pending queue, but the row and its
bytes are untouched). Nothing in this module ever guesses a root: every
function that changes ``root_state`` takes the target explicitly from the
caller. See :func:`list_unrooted_archives`, :func:`root_archive`, and
:func:`mark_orphaned`.

STRUCTLOG IS OPTIONAL HERE ON PURPOSE. Every other src/core/db*.py module
imports structlog unconditionally because it only ever runs inside the
app's own venv. This module is also driven, read-only-corpus-side, by
scripts/transcript-archive/corpus_roundtrip_harness.py against a bare
system Python that has no structlog installed (measured: mac-mini-m4 ships
Python 3.9 with no structlog) - so the import is guarded and log calls no-
op rather than raising ImportError the moment this module loads there.
"""

from __future__ import annotations

import hashlib
import json as _json
import sqlite3
import uuid as _uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    import structlog

    logger = structlog.get_logger()
except ImportError:  # pragma: no cover - exercised on the corpus harness host
    class _NoOpLogger:
        def __getattr__(self, _name: str):
            return lambda *a, **k: None

    logger = _NoOpLogger()


#: Zlib compression level used for ``content_gzip``. 9 is "best
#: compression" in zlib's own terms; measured against the real corpus
#: (303,123,025 raw bytes) it produced 90,343,479 bytes at level 6 and
#: was not meaningfully slower at level 9 for JSONL-shaped text, so this
#: module always asks for the maximum rather than exposing a tunable
#: nobody has a reason to lower.
ZLIB_LEVEL = 9

VALID_KINDS = ("session", "subagent")
VALID_ROOT_STATES = ("unrooted", "rooted", "orphaned")
VALID_LINE_ENDINGS = ("LF", "CRLF", "MIXED", "NONE")
VALID_RECORD_STATUSES = ("ok", "invalid_json", "blank")


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string with a Z suffix.

    Description: single source of the timestamp format this module
      writes, so ingested_at/rooted_at/decided_at can never disagree on
      format between two call sites.
    Inputs: none.
    Output: str, e.g. "2026-08-29T12:00:00.000000Z".
    Example: utc_now() -> "2026-08-29T12:00:00.000000Z"
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class ParsedLine:
    """One JSONL line's raw location plus what parsing it derived.

    Description: the in-memory shape produced by :func:`split_jsonl_lines`
      for one line - never holds a second copy of the line's rendered
      text, only its byte range and the small set of scalar fields the
      records table indexes.
    Inputs: constructed only by :func:`split_jsonl_lines`.
    Output: n/a (data holder).
    """

    line_no: int
    byte_offset: int
    byte_length: int
    status: str  # one of VALID_RECORD_STATUSES
    record_type: Optional[str] = None
    record_uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    ts: Optional[str] = None
    claude_session_uuid: Optional[str] = None


def classify_line_ending(data: bytes) -> str:
    """Classify the newline convention used in a byte string.

    Description: informational only - it never affects reconstruction,
      because :func:`export_archive` returns the stored bytes verbatim
      rather than re-joining lines with an assumed terminator. It exists
      so an ingested archive's metadata can say what convention its
      source used.
    Inputs: data (bytes) - the whole file's raw bytes.
    Output: str - one of VALID_LINE_ENDINGS. 'NONE' means no '\\n' byte
      appears anywhere in data (including an empty file).
    Example: classify_line_ending(b'a\\r\\nb\\r\\n') -> "CRLF"
    """
    has_crlf = b"\r\n" in data
    bare_lf = False
    idx = data.find(b"\n")
    while idx != -1:
        if idx == 0 or data[idx - 1 : idx] != b"\r":
            bare_lf = True
            break
        idx = data.find(b"\n", idx + 1)
    if has_crlf and bare_lf:
        return "MIXED"
    if has_crlf:
        return "CRLF"
    if bare_lf:
        return "LF"
    return "NONE"


def split_jsonl_lines(data: bytes) -> Tuple[List[Tuple[int, int, bytes]], bool, int]:
    """Split raw file bytes into JSONL line records with exact byte ranges.

    Description: a "line" is delimited by a bare ``\\n`` (0x0A); any
      preceding ``\\r`` is left as part of the line's own bytes, not
      stripped, because this function never discards a byte - it only
      records where each line begins and ends inside ``data``. A file
      that does not end in ``\\n`` yields its final line with no implicit
      terminator, correctly. A file that ends in ``\\n`` does NOT yield a
      trailing empty line for that final terminator alone - that
      distinction is what ``has_trailing_newline`` is for - but a
      genuine blank line (two consecutive ``\\n`` bytes) DOES yield an
      empty-bytes line, because that blank line is real content the
      source file actually held.
    Inputs: data (bytes) - the whole file's raw bytes.
    Output: (lines, has_trailing_newline, trailing_blank_line_count) where
      lines is a list of (line_no, byte_offset, raw_line_bytes) tuples in
      order, has_trailing_newline (bool) records whether data ends with
      ``\\n``, and trailing_blank_line_count (int) counts consecutive
      empty lines immediately preceding the end of the yielded lines
      (0 when the last yielded line is non-empty or there are no lines).
    Example: split_jsonl_lines(b'{"a":1}\\n\\n')
      -> ([(0, 0, b'{"a":1}'), (1, 8, b'')], True, 1)
    """
    n = len(data)
    lines: List[Tuple[int, int, bytes]] = []
    offset = 0
    line_no = 0
    while offset <= n:
        nl = data.find(b"\n", offset)
        if nl == -1:
            if offset == n:
                break
            lines.append((line_no, offset, data[offset:n]))
            line_no += 1
            break
        lines.append((line_no, offset, data[offset:nl]))
        line_no += 1
        offset = nl + 1

    has_trailing_newline = bool(data) and data.endswith(b"\n")

    trailing_blank = 0
    for _, _, raw in reversed(lines):
        if raw == b"":
            trailing_blank += 1
        else:
            break

    return lines, has_trailing_newline, trailing_blank


def _parse_one_line(raw: bytes) -> Tuple[str, Optional[dict]]:
    """Classify one line's bytes as blank, invalid JSON, or a parsed dict.

    Description: internal helper for :func:`split_jsonl_lines`'s callers.
      Trailing whitespace (including a lone ``\\r`` left over from a CRLF
      source, since :func:`split_jsonl_lines` never strips it) is legal
      JSON whitespace and does not make an otherwise-valid line invalid.
    Inputs: raw (bytes) - one line's raw bytes, exactly as located by
      split_jsonl_lines (never re-encoded).
    Output: (status, parsed) where status is one of VALID_RECORD_STATUSES
      and parsed is the decoded object (only for status == "ok" AND the
      decoded value is a dict; a valid-but-non-dict JSON value is still
      "ok" with parsed=None, since there is nothing to extract from it).
    """
    if raw.strip() == b"":
        return "blank", None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid_json", None
    try:
        obj = _json.loads(text)
    except _json.JSONDecodeError:
        return "invalid_json", None
    return "ok", (obj if isinstance(obj, dict) else None)


def build_parsed_lines(data: bytes) -> Tuple[List[ParsedLine], Dict[str, int]]:
    """Split and parse a whole file's bytes into indexable line records.

    Description: the ingest-time pass that produces everything
      :func:`ingest_transcript_bytes` needs to write ``transcript_records``
      rows, without ever holding a second serialized copy of any line -
      each ParsedLine carries only its byte range plus the handful of
      scalar fields real transcript lines carry (type, uuid, parentUuid,
      timestamp, sessionId).
    Inputs: data (bytes) - the whole file's raw bytes.
    Output: (parsed_lines, stats) where stats is
      {"record_count", "invalid_json_line_count", "blank_line_count",
       "has_trailing_newline" (0/1), "trailing_blank_line_count",
       "line_ending" is NOT included here - see classify_line_ending}.
    Example: build_parsed_lines(b'{"type":"user"}\\n')[1]["record_count"]
      -> 1
    """
    lines, has_trailing_newline, trailing_blank = split_jsonl_lines(data)
    parsed: List[ParsedLine] = []
    invalid_count = 0
    blank_count = 0
    for line_no, offset, raw in lines:
        status, obj = _parse_one_line(raw)
        pl = ParsedLine(
            line_no=line_no,
            byte_offset=offset,
            byte_length=len(raw),
            status=status,
        )
        if status == "invalid_json":
            invalid_count += 1
        elif status == "blank":
            blank_count += 1
        elif obj is not None:
            rt = obj.get("type")
            ru = obj.get("uuid")
            pu = obj.get("parentUuid")
            ts = obj.get("timestamp")
            sid = obj.get("sessionId")
            pl.record_type = str(rt) if isinstance(rt, str) else None
            pl.record_uuid = str(ru) if isinstance(ru, str) else None
            pl.parent_uuid = str(pu) if isinstance(pu, str) else None
            pl.ts = str(ts) if isinstance(ts, str) else None
            pl.claude_session_uuid = str(sid) if isinstance(sid, str) else None
        parsed.append(pl)

    stats = {
        "record_count": len(parsed),
        "invalid_json_line_count": invalid_count,
        "blank_line_count": blank_count,
        "has_trailing_newline": 1 if has_trailing_newline else 0,
        "trailing_blank_line_count": trailing_blank,
    }
    return parsed, stats


def _write_archive_rows(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source_path: str,
    source_mtime: Optional[str],
    compressed: bytes,
    content_sha256: str,
    raw_byte_length: int,
    line_ending: str,
    has_trailing_newline: bool,
    trailing_blank_line_count: int,
    parsed_lines: List[ParsedLine],
) -> int:
    """Write one transcript_archives row plus its transcript_records rows.

    Description: the single INSERT path shared by
      :func:`ingest_transcript_bytes` (whole file already in memory) and
      :func:`ingest_transcript_stream` (chunked), so the two can never
      drift into writing different columns for the same logical ingest.
    Inputs: conn (sqlite3.Connection) - inside a transaction. Remaining
      arguments are exactly the fields ingest_transcript_bytes computes
      up front and ingest_transcript_stream computes incrementally.
    Output: int - the new transcript_archives.id.
    """
    archive_uuid = str(_uuid.uuid4())
    now = utc_now()
    invalid_count = sum(1 for pl in parsed_lines if pl.status == "invalid_json")
    claude_session_uuid = None
    for pl in parsed_lines:
        if pl.claude_session_uuid:
            claude_session_uuid = pl.claude_session_uuid
            break

    cur = conn.execute(
        "INSERT INTO transcript_archives ("
        "  archive_uuid, kind, source_path, content_gzip, content_sha256,"
        "  raw_byte_length, compressed_byte_length, line_ending,"
        "  has_trailing_newline, trailing_blank_line_count, record_count,"
        "  invalid_json_line_count, claude_session_uuid, root_state,"
        "  ingested_at, ingest_source_mtime"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unrooted', ?, ?)",
        (
            archive_uuid,
            kind,
            source_path,
            sqlite3.Binary(compressed),
            content_sha256,
            raw_byte_length,
            len(compressed),
            line_ending,
            1 if has_trailing_newline else 0,
            trailing_blank_line_count,
            len(parsed_lines),
            invalid_count,
            claude_session_uuid,
            now,
            source_mtime,
        ),
    )
    archive_id = int(cur.lastrowid)

    conn.executemany(
        "INSERT INTO transcript_records ("
        "  archive_id, line_no, byte_offset, byte_length, status,"
        "  record_type, record_uuid, parent_uuid, ts"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                archive_id,
                pl.line_no,
                pl.byte_offset,
                pl.byte_length,
                pl.status,
                pl.record_type,
                pl.record_uuid,
                pl.parent_uuid,
                pl.ts,
            )
            for pl in parsed_lines
        ],
    )

    logger.info(
        "transcript_archive_ingested",
        archive_id=archive_id,
        kind=kind,
        raw_bytes=raw_byte_length,
        compressed_bytes=len(compressed),
        record_count=len(parsed_lines),
        invalid_json_line_count=invalid_count,
    )
    return archive_id


def ingest_transcript_bytes(
    conn: sqlite3.Connection,
    data: bytes,
    *,
    kind: str,
    source_path: str,
    source_mtime: Optional[str] = None,
) -> int:
    """Store a transcript file's original bytes plus a derived line index.

    Description: the WHOLE-FILE-IN-MEMORY ingest path, for callers that
      already hold the bytes (small files, or a caller that already read
      the file for another reason). For a large file, prefer
      :func:`ingest_transcript_stream`, which reads in bounded chunks
      instead of materializing the whole file at once. Writes exactly one
      ``transcript_archives`` row (the compressed original bytes, its
      sha256, and file-level shape flags) and one ``transcript_records``
      row per JSONL line (byte range plus derived scalars only - never a
      re-serialized copy of the line). ``root_state`` starts at
      'unrooted' unconditionally; this function never assigns a root, per
      the owner's stop-case requirement - see the module docstring's
      ROOTING section. Caller owns the transaction (matches every other
      write path in this package: see src/core/db.py's ``transaction()``).
    Inputs: conn (sqlite3.Connection) - inside a transaction. data (bytes)
      - the whole source file's raw bytes, read in binary mode with no
      decoding. kind (str) - "session" or "subagent". source_path (str) -
      informational provenance only, never authoritative for rooting.
      source_mtime (str | None) - the source file's mtime, if known, for
      provenance.
    Output: int - the new transcript_archives.id.
    Raises: ValueError - kind is not a recognized value.
    Example:
        with transaction(conn):
            archive_id = ingest_transcript_bytes(
                conn, data, kind="session", source_path=str(path)
            )
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")

    parsed_lines, stats = build_parsed_lines(data)
    line_ending = classify_line_ending(data)
    content_sha256 = hashlib.sha256(data).hexdigest()
    compressed = zlib.compress(data, ZLIB_LEVEL)

    return _write_archive_rows(
        conn,
        kind=kind,
        source_path=source_path,
        source_mtime=source_mtime,
        compressed=compressed,
        content_sha256=content_sha256,
        raw_byte_length=len(data),
        line_ending=line_ending,
        has_trailing_newline=bool(stats["has_trailing_newline"]),
        trailing_blank_line_count=stats["trailing_blank_line_count"],
        parsed_lines=parsed_lines,
    )


#: Default chunk size for :func:`ingest_transcript_stream`. 4 MiB keeps
#: peak memory for the corpus's largest known file (72.5 MB, measured
#: 2026-08-29) at roughly 1/18th of the file's own size rather than the
#: whole file, while staying large enough that zlib and sha256 are not
#: dominated by per-call overhead.
DEFAULT_STREAM_CHUNK_SIZE = 4 * 1024 * 1024


def ingest_transcript_stream(
    conn: sqlite3.Connection,
    file_path,
    *,
    kind: str,
    source_path: Optional[str] = None,
    source_mtime: Optional[str] = None,
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
) -> int:
    """Ingest a transcript file by streaming it in fixed-size chunks.

    Description: the large-file path. Never holds more than one chunk
      (default 4 MiB) plus at most one in-flight JSONL line in memory at
      once - so ingesting the corpus's largest known file (72.5 MB,
      22,121 records, measured against ~/.claude/projects on
      mac-mini-m4, 2026-08-29) costs O(chunk_size + longest single line)
      rather than O(file size). SHA-256 and zlib compression are both fed
      incrementally (hashlib's ``update()``, zlib's ``compressobj()``),
      and JSONL line boundaries are tracked across chunk boundaries with
      a small carry-over buffer holding only the not-yet-terminated tail
      of the current line - never the whole file.

      Produces byte-for-byte the same transcript_archives /
      transcript_records rows as :func:`ingest_transcript_bytes` given
      the same input - proven in
      tests/test_transcript_archive.py::test_stream_matches_whole_file_ingest_for_various_chunk_sizes
      across multiple chunk sizes, including ones smaller than a single
      line, to force boundary-crossing on every construct this module
      handles (CRLF, no trailing newline, a trailing blank line, invalid
      JSON).
    Inputs: conn (sqlite3.Connection) - inside a transaction. file_path
      (str | Path) - path to read, opened in binary mode. kind (str) -
      "session" or "subagent". source_path (str | None) - defaults to
      str(file_path) when omitted. source_mtime (str | None). chunk_size
      (int) - read granularity in bytes; default DEFAULT_STREAM_CHUNK_SIZE.
    Output: int - the new transcript_archives.id.
    Raises: ValueError - kind is not a recognized value, or chunk_size < 1.
      OSError - the file could not be opened or read.
    Example:
        with transaction(conn):
            aid = ingest_transcript_stream(conn, path, kind="session")
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    resolved_source_path = (
        str(source_path) if source_path is not None else str(file_path)
    )

    hasher = hashlib.sha256()
    compressor = zlib.compressobj(ZLIB_LEVEL)
    compressed_parts: List[bytes] = []
    raw_len = 0

    parsed_lines: List[ParsedLine] = []
    line_no = 0
    carry = b""  # unterminated tail of the current line, carried across chunks
    buf_start_abs = 0  # absolute file offset of byte 0 of (carry + next chunk)
    prev_last_byte = b""  # last byte of the previous chunk, for \r\n across a boundary
    has_crlf = False
    has_bare_lf = False
    trailing_blank_run = 0
    last_byte_seen = b""
    saw_any_bytes = False

    def _consume_line(raw_line: bytes, abs_offset: int) -> None:
        nonlocal line_no, trailing_blank_run
        status, obj = _parse_one_line(raw_line)
        pl = ParsedLine(
            line_no=line_no,
            byte_offset=abs_offset,
            byte_length=len(raw_line),
            status=status,
        )
        if status == "ok" and obj is not None:
            rt = obj.get("type")
            ru = obj.get("uuid")
            pu = obj.get("parentUuid")
            ts = obj.get("timestamp")
            sid = obj.get("sessionId")
            pl.record_type = str(rt) if isinstance(rt, str) else None
            pl.record_uuid = str(ru) if isinstance(ru, str) else None
            pl.parent_uuid = str(pu) if isinstance(pu, str) else None
            pl.ts = str(ts) if isinstance(ts, str) else None
            pl.claude_session_uuid = str(sid) if isinstance(sid, str) else None
        parsed_lines.append(pl)
        trailing_blank_run = trailing_blank_run + 1 if raw_line == b"" else 0
        line_no += 1

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            saw_any_bytes = True
            hasher.update(chunk)
            piece = compressor.compress(chunk)
            if piece:
                compressed_parts.append(piece)
            raw_len += len(chunk)
            last_byte_seen = chunk[-1:]

            # Search only within `chunk` for '\n' bytes - never re-search
            # prev_last_byte itself, or a '\n' carried over as
            # prev_last_byte would be classified a second time here (once
            # already, correctly, when it was the current chunk's own
            # last byte in the prior iteration).
            idx = chunk.find(b"\n")
            while idx != -1:
                preceding = prev_last_byte if idx == 0 else chunk[idx - 1 : idx]
                if preceding == b"\r":
                    has_crlf = True
                else:
                    has_bare_lf = True
                idx = chunk.find(b"\n", idx + 1)
            prev_last_byte = chunk[-1:]

            buf = carry + chunk
            start = 0
            while True:
                nl = buf.find(b"\n", start)
                if nl == -1:
                    carry = buf[start:]
                    break
                _consume_line(buf[start:nl], buf_start_abs + start)
                start = nl + 1
            buf_start_abs += len(buf) - len(carry)

    if carry:
        _consume_line(carry, buf_start_abs)

    has_trailing_newline = saw_any_bytes and last_byte_seen == b"\n"
    if has_crlf and has_bare_lf:
        line_ending = "MIXED"
    elif has_crlf:
        line_ending = "CRLF"
    elif has_bare_lf:
        line_ending = "LF"
    else:
        line_ending = "NONE"

    compressed = b"".join(compressed_parts) + compressor.flush()

    return _write_archive_rows(
        conn,
        kind=kind,
        source_path=resolved_source_path,
        source_mtime=source_mtime,
        compressed=compressed,
        content_sha256=hasher.hexdigest(),
        raw_byte_length=raw_len,
        line_ending=line_ending,
        has_trailing_newline=has_trailing_newline,
        trailing_blank_line_count=trailing_blank_run,
        parsed_lines=parsed_lines,
    )


def export_archive(conn: sqlite3.Connection, archive_id: int) -> bytes:
    """Reconstruct a transcript's original bytes from the database.

    Description: a pure decompress of the stored blob - no re-
      serialization of any line happens here, which is the whole basis of
      the byte-exact guarantee. See the module docstring for why a parsed
      re-serialization cannot make this promise.

      PREFIX-DEDUPE AWARE (schema v15). A row whose bytes were proven, at
      ingest time, to be a strict byte-prefix of a LATER version's bytes
      (see src/core/transcript_prefix_dedupe.py) has its own
      ``content_gzip`` replaced with a near-empty sentinel and
      ``superseded_by_archive_id`` set to that later row - so a row's own
      ``content_gzip`` is no longer necessarily where its bytes live. This
      function walks ``superseded_by_archive_id`` forward until it reaches
      the row that still holds real content (``superseded_by_archive_id``
      IS NULL there), decompresses THAT row's blob exactly once, and
      slices the result to the ORIGINALLY REQUESTED row's own
      ``raw_byte_length`` - captured before the walk starts, never the
      byte length of whichever row ends up holding the content. Slicing
      once from the end of the chain is equivalent to slicing at every
      link, because supersession is only ever recorded when the whole
      chain has been proven, link by link, to be one strict growing
      prefix relationship (see the module docstring's THREE-VERSION CHAIN
      note in transcript_prefix_dedupe.py). A row with no
      ``superseded_by_archive_id`` (the common case, and every pre-v15
      row) takes zero extra steps - this is the same single decompress
      as before.
    Inputs: conn (sqlite3.Connection). archive_id (int).
    Output: bytes - identical to what was passed to
      ingest_transcript_bytes for this archive, or the process is broken.
    Raises: LookupError - no archive with this id, or the
      superseded_by_archive_id chain points at a row that does not exist.
      zlib.error - the stored blob is corrupt. ValueError - a cycle was
      detected in the supersession chain (defensive; nothing in this
      codebase writes one).
    Example: export_archive(conn, 1) -> b'{"type":"user"}\\n'
    """
    row = conn.execute(
        "SELECT content_gzip, raw_byte_length, superseded_by_archive_id"
        " FROM transcript_archives WHERE id = ?",
        (archive_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no transcript_archives row with id={archive_id}")

    target_len = row["raw_byte_length"]
    visited = {archive_id}
    current = row
    while current["superseded_by_archive_id"] is not None:
        next_id = int(current["superseded_by_archive_id"])
        if next_id in visited:
            raise ValueError(
                f"supersession cycle detected starting at archive_id={archive_id}"
            )
        visited.add(next_id)
        current = conn.execute(
            "SELECT content_gzip, raw_byte_length, superseded_by_archive_id"
            " FROM transcript_archives WHERE id = ?",
            (next_id,),
        ).fetchone()
        if current is None:
            raise LookupError(
                f"superseded_by_archive_id chain broken: archive_id={next_id}"
                f" (superseding archive_id={archive_id}) does not exist"
            )

    full = zlib.decompress(current["content_gzip"])
    return full[:target_len]


@dataclass
class VerifyResult:
    """The three-outcome verdict of comparing a reconstruction to a source.

    Description: never collapses "could not check" into either pass or
      fail. See the module docstring's THREE OUTCOMES section.
    Inputs: constructed only by :func:`verify_against_source`.
    Output: n/a (data holder).
    """

    outcome: str  # "byte_identical" | "mismatch" | "could_not_evaluate"
    archive_id: Optional[int] = None
    source_path: Optional[str] = None
    reason: Optional[str] = None
    first_diff_offset: Optional[int] = None
    source_hexdump: Optional[str] = None
    reconstructed_hexdump: Optional[str] = None
    source_byte_length: Optional[int] = None
    reconstructed_byte_length: Optional[int] = None


def _hexdump_window(data: bytes, offset: int, width: int = 16) -> str:
    """Render a short hex+ASCII window of ``data`` centered on ``offset``.

    Description: helper for VerifyResult's mismatch detail - gives a
      human enough context to see what actually differs without dumping
      the whole file into a report.
    Inputs: data (bytes). offset (int) - the byte index to center on.
      width (int) - bytes shown on each side.
    Output: str, e.g. "7b226179...  |{"a..."|".
    """
    start = max(0, offset - width)
    end = min(len(data), offset + width)
    chunk = data[start:end]
    hexpart = chunk.hex()
    asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    return f"offset {start}..{end}: {hexpart}  |{asciipart}|"


def verify_against_source(
    conn: sqlite3.Connection, archive_id: int, source_path: str
) -> VerifyResult:
    """Compare a database reconstruction to the live source file, by bytes.

    Description: the THREE-OUTCOME check this task exists to prove.
      Never compares line counts or parsed equality - only raw bytes,
      via full-content comparison (a mismatched sha256 alone would tell
      you THAT they differ; this also locates WHERE, which is what
      "byte for byte intact" needs).
    Inputs: conn (sqlite3.Connection). archive_id (int). source_path
      (str) - path to the original file on disk, read fresh (not the
      value stored on the archive row, which is provenance only).
    Output: VerifyResult with outcome in {"byte_identical", "mismatch",
      "could_not_evaluate"}. A source that cannot be read (missing,
      permission denied, or any other OSError) or a stored archive that
      cannot be decompressed both produce "could_not_evaluate", never
      "mismatch" - the two are not the same finding and must not be
      conflated per this project's three-outcome rule.
    Example: verify_against_source(conn, 1, "/path/to/x.jsonl").outcome
      -> "byte_identical"
    """
    try:
        with open(source_path, "rb") as f:
            source_bytes = f.read()
    except OSError as exc:
        return VerifyResult(
            outcome="could_not_evaluate",
            archive_id=archive_id,
            source_path=source_path,
            reason=f"could not read source: {type(exc).__name__}: {exc}",
        )

    try:
        reconstructed = export_archive(conn, archive_id)
    except (LookupError, zlib.error) as exc:
        return VerifyResult(
            outcome="could_not_evaluate",
            archive_id=archive_id,
            source_path=source_path,
            reason=f"could not reconstruct from database: {type(exc).__name__}: {exc}",
        )

    if reconstructed == source_bytes:
        return VerifyResult(
            outcome="byte_identical",
            archive_id=archive_id,
            source_path=source_path,
            source_byte_length=len(source_bytes),
            reconstructed_byte_length=len(reconstructed),
        )

    m = min(len(source_bytes), len(reconstructed))
    off = 0
    while off < m and source_bytes[off] == reconstructed[off]:
        off += 1
    return VerifyResult(
        outcome="mismatch",
        archive_id=archive_id,
        source_path=source_path,
        first_diff_offset=off,
        source_hexdump=_hexdump_window(source_bytes, off),
        reconstructed_hexdump=_hexdump_window(reconstructed, off),
        source_byte_length=len(source_bytes),
        reconstructed_byte_length=len(reconstructed),
    )


# ---------------------------------------------------------------------
# Rooting: pending queue, and the two terminal human decisions.
# ---------------------------------------------------------------------


def list_unrooted_archives(
    conn: sqlite3.Connection, limit: int = 200
) -> List[Dict]:
    """List archives awaiting human attribution, with hints, not guesses.

    Description: what a human needs to "point to where it belongs" -
      the extracted claude_session_uuid hint, source_path, record counts,
      and any candidate sessions whose claude_session_uuid matches this
      archive's extracted hint exactly. A candidate is surfaced, never
      applied - see :func:`root_archive` for the only thing that assigns
      a root.
    Inputs: conn (sqlite3.Connection). limit (int) - max rows.
    Output: list[dict], newest-ingested first, each with keys:
      archive_id, archive_uuid, kind, source_path, claude_session_uuid,
      record_count, invalid_json_line_count, ingested_at,
      candidate_session_ids (list[int], possibly empty - sessions whose
      claude_session_uuid equals this archive's extracted hint).
    Example: list_unrooted_archives(conn, limit=10) -> [...]
    """
    rows = conn.execute(
        "SELECT id, archive_uuid, kind, source_path, claude_session_uuid,"
        " record_count, invalid_json_line_count, ingested_at"
        " FROM transcript_archives WHERE root_state = 'unrooted'"
        " ORDER BY ingested_at DESC LIMIT ?",
        (limit,),
    ).fetchall()

    out = []
    for row in rows:
        candidates: List[int] = []
        if row["claude_session_uuid"]:
            crows = conn.execute(
                "SELECT id FROM sessions WHERE claude_session_uuid = ?",
                (row["claude_session_uuid"],),
            ).fetchall()
            candidates = [int(r["id"]) for r in crows]
        out.append(
            {
                "archive_id": int(row["id"]),
                "archive_uuid": row["archive_uuid"],
                "kind": row["kind"],
                "source_path": row["source_path"],
                "claude_session_uuid": row["claude_session_uuid"],
                "record_count": int(row["record_count"]),
                "invalid_json_line_count": int(row["invalid_json_line_count"]),
                "ingested_at": row["ingested_at"],
                "candidate_session_ids": candidates,
            }
        )
    return out


def root_archive(
    conn: sqlite3.Connection,
    archive_id: int,
    *,
    root_session_id: Optional[int] = None,
    parent_archive_id: Optional[int] = None,
    decided_by: str,
    note: Optional[str] = None,
) -> None:
    """Apply a human's attribution decision to one archive.

    Description: the ONLY function in this module that sets
      root_session_id or parent_archive_id - always from an explicit
      caller-supplied value, never inferred. Records an append-only
      decision row in transcript_root_decisions before updating the
      archive, so the audit trail exists even if the update step were to
      fail (caller wraps both in one transaction; either both apply or
      neither does).
    Inputs: conn (sqlite3.Connection) - inside a transaction. archive_id
      (int). root_session_id (int | None) - for a 'session' kind archive.
      parent_archive_id (int | None) - for a 'subagent' kind archive,
      must reference another transcript_archives row. decided_by (str) -
      who made the call, e.g. "human" or a username. note (str | None) -
      free-text context for why this root was chosen.
    Output: None.
    Raises: ValueError - neither root_session_id nor parent_archive_id
      given, or the archive_id does not exist.
    Example: root_archive(conn, 5, root_session_id=12, decided_by="human")
    """
    if root_session_id is None and parent_archive_id is None:
        raise ValueError(
            "root_archive requires root_session_id or parent_archive_id"
        )
    row = conn.execute(
        "SELECT id FROM transcript_archives WHERE id = ?", (archive_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no transcript_archives row with id={archive_id}")

    now = utc_now()
    conn.execute(
        "INSERT INTO transcript_root_decisions ("
        "  archive_id, decided_at, decided_by, action, root_session_id,"
        "  parent_archive_id, note"
        ") VALUES (?, ?, ?, 'rooted', ?, ?, ?)",
        (archive_id, now, decided_by, root_session_id, parent_archive_id, note),
    )
    conn.execute(
        "UPDATE transcript_archives SET root_state = 'rooted',"
        " root_session_id = ?, parent_archive_id = ?, rooted_at = ?,"
        " rooted_by = ? WHERE id = ?",
        (root_session_id, parent_archive_id, now, decided_by, archive_id),
    )
    logger.info(
        "transcript_archive_rooted",
        archive_id=archive_id,
        root_session_id=root_session_id,
        parent_archive_id=parent_archive_id,
        decided_by=decided_by,
    )


def mark_orphaned(
    conn: sqlite3.Connection,
    archive_id: int,
    *,
    decided_by: str,
    note: Optional[str] = None,
) -> None:
    """Record a human's decision that an archive has no findable root.

    Description: terminal, but non-destructive - the row and its bytes
      are untouched, only root_state moves to 'orphaned' so it stops
      appearing in :func:`list_unrooted_archives`. Per this project's own
      rule that a check which never clears is furniture: an unrootable
      item that keeps nagging the pending queue forever is the same
      defect in the other direction, so this state exists specifically
      to let a human close the question without discarding anything.
    Inputs: conn (sqlite3.Connection) - inside a transaction. archive_id
      (int). decided_by (str). note (str | None) - should say why no
      root could be found, since there is no automated retry.
    Output: None.
    Raises: ValueError - the archive_id does not exist.
    Example: mark_orphaned(conn, 9, decided_by="human", note="no match")
    """
    row = conn.execute(
        "SELECT id FROM transcript_archives WHERE id = ?", (archive_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no transcript_archives row with id={archive_id}")

    now = utc_now()
    conn.execute(
        "INSERT INTO transcript_root_decisions ("
        "  archive_id, decided_at, decided_by, action, note"
        ") VALUES (?, ?, ?, 'orphaned', ?)",
        (archive_id, now, decided_by, note),
    )
    conn.execute(
        "UPDATE transcript_archives SET root_state = 'orphaned',"
        " rooted_at = ?, rooted_by = ? WHERE id = ?",
        (now, decided_by, archive_id),
    )
    logger.info(
        "transcript_archive_orphaned", archive_id=archive_id, decided_by=decided_by
    )
