"""Prefix dedupe for growing transcripts - schema v15, built on transcript_archive.py.

WHY THIS EXISTS. transcript_archive.py's ``ingest_transcript_stream`` writes
one full ``content_gzip`` copy per ingest. A live Claude Code session's
transcript grows continuously while the conversation runs, and before this
module every re-ingest of a grown file wrote a SECOND full copy of a file
that already shares almost all its bytes with the first - the dominant
storage cost for a daily-growing transcript (measured: the corpus's largest
single file is 72.7 MB).

THE KEY PROPERTY, VERIFIED PER FILE, EVERY TIME - NEVER ASSUMED. A file that
is genuinely appended to has newer bytes that start with the older bytes.
This module never infers that from size, mtime, or line count: it reads
exactly ``len(old_bytes)`` bytes from the front of the new file and compares
them, byte for byte, against the old version's reconstructed bytes (which
themselves came through :func:`export_archive`, so an already-superseded old
row is compared correctly too). Only a real, measured prefix match is ever
treated as an append.

TWO OUTCOMES, BOTH RECORDED, NEITHER SILENT:

  - PREFIX MATCHES (append). The new row is ingested in full, as always.
    The OLD row's ``content_gzip`` is then replaced with a near-empty
    sentinel and ``superseded_by_archive_id`` is set to the new row's id -
    the old row's bytes are still addressable (as "the first N bytes of
    the new row", N being the old row's own ``raw_byte_length``, already
    stored) but no longer stored twice. See transcript_archive.py's
    updated :func:`~src.core.transcript_archive.export_archive` for the
    read side of this.
  - PREFIX DOES NOT MATCH (a truncation, a mid-file edit, or a tool that
    rewrites history rather than appending to it). Both full copies are
    kept, exactly as before this module existed. The new row's
    ``growth_kind`` is set to ``'non_append_rewrite'`` and a warning is
    logged - a finding surfaced to whoever reads it, never a fallback that
    happens quietly.

THREE-VERSION CHAIN. Supersession only ever points a row at the NEXT row in
its own source_path's history, never rewritten to point at the eventual
latest row. So after three growth cycles (A1 -> A2 -> A3, each proven a
strict append over its predecessor at the moment it was ingested), A1's
``superseded_by_archive_id`` is A2, and A2's is A3 - not both A3. Reading
A1's bytes therefore walks A1 -> A2 -> A3, decompresses A3's real content
exactly once, and slices to A1's OWN ``raw_byte_length``. This is correct
precisely because supersession is only ever recorded when the byte
comparison at that specific step proved a strict prefix relationship: by
induction, A1's bytes are a prefix of A2's bytes are a prefix of A3's bytes,
so slicing A3's full content directly to A1's length reproduces A1 exactly,
with no need to keep every intermediate row's own content around simply to
re-slice it. This is exactly where an off-by-one would live if the walk
sliced at every link instead of once at the end - it does not, and
tests/test_transcript_prefix_dedupe.py asserts a three-version chain
explicitly, not just the two-version case.

WHAT THIS MODULE NEVER DOES: it never deletes a row, never drops a
``transcript_records`` line index, and never changes ``content_sha256`` -
each row's originally-computed hash is exactly what
:func:`verify_stored_hash` checks the reconstruction against, forever,
regardless of how many later growth cycles have since superseded it.
"""

from __future__ import annotations

import hashlib
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

from src.core.db import transaction
from src.core.transcript_archive import (
    ZLIB_LEVEL,
    export_archive,
    ingest_transcript_stream,
)

#: Compressed placeholder written into a superseded row's ``content_gzip``.
#: zlib.compress(b"") at ZLIB_LEVEL - a handful of bytes, satisfies the
#: column's NOT NULL constraint without holding a second real copy.
_SENTINEL_GZIP = zlib.compress(b"", ZLIB_LEVEL)

VALID_GROWTH_KINDS = ("initial", "append", "non_append_rewrite")


@dataclass
class GrowthOutcome:
    """What happened when one file was ingested through prefix dedupe.

    Description: the return value of :func:`ingest_with_prefix_dedupe` -
      distinguishes all three growth_kind cases explicitly, so a caller
      never has to re-derive "was this an append" from row state.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    source_path: str
    kind: str
    archive_id: int
    growth_kind: str  # one of VALID_GROWTH_KINDS
    previous_archive_id: Optional[int] = None
    previous_raw_byte_length: Optional[int] = None
    raw_byte_length: int = 0
    superseded_previous: bool = False


def ingest_with_prefix_dedupe(
    conn,
    file_path,
    *,
    kind: str,
    source_path: str,
    existing_archive_id: Optional[int],
    source_mtime: Optional[str] = None,
) -> GrowthOutcome:
    """Ingest one file, superseding its predecessor's storage if it grew by append.

    Description: the replacement for a bare :func:`ingest_transcript_stream`
      call at the "content changed" branch of corpus ingest. Caller has
      already established (by content hash, e.g. via
      transcript_corpus_ingest._latest_archive_for_source) that either no
      prior archive exists for this source_path (``existing_archive_id``
      is None) or a prior archive exists and its content differs from the
      current file (``existing_archive_id`` names it) - this function
      never re-checks "did anything change", only "HOW did it change".

      When ``existing_archive_id`` is given, the comparison reads only
      ``old_raw_byte_length`` bytes from the front of the new file (never
      the whole new file) and compares them against the old row's
      reconstructed bytes (via :func:`export_archive`, so a doubly-grown
      file's earlier steps are still handled correctly) - bounding the
      extra memory this check costs by the OLDER, smaller version's size,
      not the new one.
    Inputs: conn - sqlite3.Connection, NOT already inside a transaction
      (opens its own, matching ingest_transcript_stream's caller
      convention). file_path (str | Path). kind (str) - "session" or
      "subagent". source_path (str). existing_archive_id (int | None) -
      the newest known transcript_archives row for this source_path, or
      None if this is the first ingest ever seen for it. source_mtime
      (str | None).
    Output: GrowthOutcome.
    Raises: OSError - the file could not be opened or read. LookupError -
      existing_archive_id does not name a real row.
    Example:
        outcome = ingest_with_prefix_dedupe(
            conn, path, kind="session", source_path="slug/x.jsonl",
            existing_archive_id=None,
        )
        outcome.growth_kind  # "initial"
    """
    if existing_archive_id is None:
        with transaction(conn):
            new_id = ingest_transcript_stream(
                conn,
                file_path,
                kind=kind,
                source_path=source_path,
                source_mtime=source_mtime,
            )
        raw_len = conn.execute(
            "SELECT raw_byte_length FROM transcript_archives WHERE id = ?",
            (new_id,),
        ).fetchone()["raw_byte_length"]
        return GrowthOutcome(
            source_path=source_path,
            kind=kind,
            archive_id=new_id,
            growth_kind="initial",
            raw_byte_length=int(raw_len),
        )

    old_row = conn.execute(
        "SELECT raw_byte_length FROM transcript_archives WHERE id = ?",
        (existing_archive_id,),
    ).fetchone()
    if old_row is None:
        raise LookupError(
            f"no transcript_archives row with id={existing_archive_id}"
        )
    old_len = int(old_row["raw_byte_length"])
    old_bytes = export_archive(conn, existing_archive_id)

    with open(file_path, "rb") as f:
        prefix = f.read(old_len)
    is_append = prefix == old_bytes

    with transaction(conn):
        new_id = ingest_transcript_stream(
            conn,
            file_path,
            kind=kind,
            source_path=source_path,
            source_mtime=source_mtime,
        )
        new_raw_len = int(
            conn.execute(
                "SELECT raw_byte_length FROM transcript_archives WHERE id = ?",
                (new_id,),
            ).fetchone()["raw_byte_length"]
        )

        if is_append:
            conn.execute(
                "UPDATE transcript_archives SET growth_kind = 'append'"
                " WHERE id = ?",
                (new_id,),
            )
            conn.execute(
                "UPDATE transcript_archives SET content_gzip = ?,"
                " compressed_byte_length = ?, superseded_by_archive_id = ?"
                " WHERE id = ?",
                (
                    _SENTINEL_GZIP,
                    len(_SENTINEL_GZIP),
                    new_id,
                    existing_archive_id,
                ),
            )
            logger.info(
                "transcript_prefix_dedupe_superseded",
                source_path=source_path,
                previous_archive_id=existing_archive_id,
                new_archive_id=new_id,
                previous_raw_byte_length=old_len,
            )
        else:
            conn.execute(
                "UPDATE transcript_archives SET growth_kind ="
                " 'non_append_rewrite' WHERE id = ?",
                (new_id,),
            )
            logger.warning(
                "transcript_prefix_dedupe_non_append_rewrite",
                source_path=source_path,
                previous_archive_id=existing_archive_id,
                new_archive_id=new_id,
                previous_raw_byte_length=old_len,
                new_raw_byte_length=new_raw_len,
            )

    return GrowthOutcome(
        source_path=source_path,
        kind=kind,
        archive_id=new_id,
        growth_kind="append" if is_append else "non_append_rewrite",
        previous_archive_id=existing_archive_id,
        previous_raw_byte_length=old_len,
        raw_byte_length=new_raw_len,
        superseded_previous=is_append,
    )


@dataclass
class StoredHashVerifyResult:
    """Three-outcome verdict of a reconstruction against its OWN stored hash.

    Description: unlike :func:`~src.core.transcript_archive.verify_against_source`,
      this never touches a live file on disk - it checks that
      :func:`export_archive` still reproduces exactly the bytes whose
      sha256 was recorded when THIS row was originally ingested, however
      many later growth cycles have since superseded its storage. This is
      the regression guard prefix dedupe must never fail: an old version
      export weakening is a bug, not an optimisation.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    outcome: str  # "hash_verified" | "hash_mismatch" | "could_not_evaluate"
    archive_id: int
    expected_sha256: Optional[str] = None
    actual_sha256: Optional[str] = None
    reason: Optional[str] = None


def verify_stored_hash(conn, archive_id: int) -> StoredHashVerifyResult:
    """Reconstruct one archive and check it against its OWN stored sha256.

    Description: the three-outcome check this module's regression
      guarantee rests on. A row that cannot be found, or whose
      supersession chain cannot be walked (broken link, corrupt blob),
      is ``could_not_evaluate`` - never silently treated as a pass.
    Inputs: conn - sqlite3.Connection. archive_id (int).
    Output: StoredHashVerifyResult.
    Example: verify_stored_hash(conn, 5).outcome -> "hash_verified"
    """
    row = conn.execute(
        "SELECT content_sha256 FROM transcript_archives WHERE id = ?",
        (archive_id,),
    ).fetchone()
    if row is None:
        return StoredHashVerifyResult(
            outcome="could_not_evaluate",
            archive_id=archive_id,
            reason="no such transcript_archives row",
        )
    expected = row["content_sha256"]

    try:
        data = export_archive(conn, archive_id)
    except (LookupError, zlib.error, ValueError) as exc:
        return StoredHashVerifyResult(
            outcome="could_not_evaluate",
            archive_id=archive_id,
            expected_sha256=expected,
            reason=f"could not reconstruct: {type(exc).__name__}: {exc}",
        )

    actual = hashlib.sha256(data).hexdigest()
    if actual == expected:
        return StoredHashVerifyResult(
            outcome="hash_verified",
            archive_id=archive_id,
            expected_sha256=expected,
            actual_sha256=actual,
        )
    return StoredHashVerifyResult(
        outcome="hash_mismatch",
        archive_id=archive_id,
        expected_sha256=expected,
        actual_sha256=actual,
        reason="reconstructed bytes do not match the sha256 stored at ingest",
    )
