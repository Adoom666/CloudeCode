"""Byte-exact transcript export, in the two forms a caller can actually use.

ONE RENDERING PATH, TWO DELIVERY SHAPES. Everything here reassembles
through :func:`src.core.message_model_export.iter_export_lines`, which is
the single place a stored ``(body_json, envelope_json, key_order_json,
serializer_style)`` tuple becomes bytes. Nothing in this module renders a
line itself, so a streamed export and a verified export cannot disagree
about what a line's bytes are - there is one implementation, not two that
happen to match today.

WHICH FORM IS CORRECT, AND THE MEASUREMENT BEHIND IT.

- :func:`iter_export_bytes` (streaming) is the DEFAULT and the only form
  that is safe at any size. Peak memory is one ``fetchmany`` batch plus
  the LARGEST SINGLE LINE, and that second term is the real floor: the
  corpus's largest transcript (244,117,661 bytes) contains one
  37,404,061-byte line, and streaming it was measured at 556 MB peak
  RSS, not the ~78 MB the design document quotes from a smaller
  transcript. Size any concurrency limit against ~600 MB per in-flight
  export. Its cost is that bad bytes reach the client BEFORE the hash is
  known, so the caller must compare the delivered bytes against
  ``expected_content_sha256`` itself.
- :func:`verified_export` (verify-before-send) buffers the whole
  transcript, compares hashes, and only then hands anything over. It is
  the right form for a browser download or an automated restore - any
  consumer that cannot check a hash after the fact. It REFUSES above
  ``VERIFY_BEFORE_SEND_MAX_BYTES`` rather than silently falling back to
  streaming, because the caller asked for a guarantee that this form
  cannot provide at that size. Buffered peak was measured at about 12x
  the transcript, so an 8 MiB cap is roughly 100 MB of process memory.

There is no third form that is both bounded in memory and verified
before the first byte, so both are offered and each says which it is.

NO ``?redact=``, EVER. A redacted transcript is not the transcript, and a
guarantee with a mode is not a guarantee.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Dict, Iterator, List, Optional

from src.core.message_model_export import (
    VERIFY_CANNOT_RENDER,
    export_transcript,
    iter_export_lines,
)

#: Line separator the archive was split on. Matches
#: :func:`src.core.message_model_serialize.join_lines`, which is the
#: inverse this module streams. Named rather than inlined so the two
#: cannot drift apart silently.
EXPORT_LINE_SEPARATOR: str = "\n"

#: Smallest chunk :func:`iter_export_bytes` hands to its consumer. Lines
#: are accumulated until they reach this, so a 980-line transcript costs
#: a handful of writes rather than 980. A single line larger than this is
#: emitted whole - a line is never split, because splitting one is how a
#: multi-byte UTF-8 character gets cut in half.
STREAM_CHUNK_MIN_BYTES: int = 262144

#: Delivery states :func:`verified_export` can report. Four, not two:
#: ``mismatch`` and ``cannot_render`` are distinct findings and neither
#: is an empty success.
EXPORT_OK: str = "ok"
EXPORT_NOT_FOUND: str = "not_found"
EXPORT_TOO_LARGE: str = "too_large"
EXPORT_MISMATCH: str = "mismatch"
EXPORT_CANNOT_RENDER: str = "cannot_render"


class ExportRenderError(RuntimeError):
    """A line could not be rendered at all, so the stream cannot continue.

    Carries the line number and the stored detail so the abort names
    WHERE it stopped. Skipping the line instead would emit a shorter file
    that then fails the whole-file hash for a reason nobody could read
    off the failure.
    """

    def __init__(self, transcript_id: int, line_no: int, detail: str) -> None:
        super().__init__(
            f"transcript {transcript_id} line {line_no} cannot be rendered: "
            f"{detail}"
        )
        self.transcript_id = transcript_id
        self.line_no = line_no
        self.detail = detail


def export_head(
    conn: sqlite3.Connection, transcript_id: int
) -> Optional[Dict[str, Any]]:
    """Read everything an export needs BEFORE it starts producing bytes.

    Description: the expected hash, the expected byte count and the
      filename all have to be on the response's leading headers, which
      means they must be known before the first byte is streamed. Read in
      one primary-key lookup.
    Inputs: conn (sqlite3.Connection, read-only), transcript_id (int).
    Output: dict with transcript_id, session_ref, content_sha256,
      raw_byte_length, has_trailing_newline, line_count - or None when
      there is no such transcript. None is a MEASUREMENT ("no row"), not
      a failure to look.
    Example: export_head(conn, 4)["raw_byte_length"] -> 3181330
    """
    row = conn.execute(
        "SELECT id, session_ref, content_sha256, raw_byte_length, "
        "       has_trailing_newline, line_count "
        "FROM message_transcripts WHERE id = ?",
        (transcript_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "transcript_id": int(row["id"]),
        "session_ref": row["session_ref"],
        "content_sha256": row["content_sha256"],
        "raw_byte_length": int(row["raw_byte_length"] or 0),
        "has_trailing_newline": bool(row["has_trailing_newline"]),
        "line_count": int(row["line_count"] or 0),
    }


def iter_export_bytes(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    has_trailing_newline: bool,
    chunk_min_bytes: int = STREAM_CHUNK_MIN_BYTES,
) -> Iterator[bytes]:
    """Yield a transcript's exact original bytes, in order, without buffering it.

    Description: the streaming inverse of
      :func:`src.core.message_model_serialize.join_lines`, which is
      ``"\\n".join(lines) + ("\\n" if has_trailing_newline else "")`` and
      returns ``""`` for no lines. Reproduced here as: first line bare,
      every later line prefixed with the separator, one trailing
      separator only if at least one line was emitted and the transcript
      stored one. Each line is encoded on its own, so no chunk boundary
      can ever land inside a multi-byte character.
    Inputs: conn (sqlite3.Connection), transcript_id (int),
      has_trailing_newline (bool, from :func:`export_head`),
      chunk_min_bytes (int) - accumulate until at least this many bytes.
    Output: Iterator[bytes]. Concatenating every chunk yields exactly the
      ingested file's bytes.
    Raises: ExportRenderError - a line's stored row cannot be rendered.
      A VERIFY_MISMATCH line is NOT fatal here and is emitted: the
      whole-file hash the caller compares is what reports it, and
      aborting would turn a detectable bad byte into an unexplained
      truncation.
    Example: b"".join(iter_export_bytes(conn, 1,
             has_trailing_newline=True)) -> the file's bytes
    """
    pending: List[bytes] = []
    pending_bytes = 0
    emitted_any = False
    for export in iter_export_lines(conn, transcript_id):
        if export.outcome == VERIFY_CANNOT_RENDER:
            raise ExportRenderError(transcript_id, export.line_no, export.detail)
        piece = str(export.text)
        if emitted_any:
            piece = EXPORT_LINE_SEPARATOR + piece
        emitted_any = True
        encoded = piece.encode("utf-8")
        pending.append(encoded)
        pending_bytes += len(encoded)
        if pending_bytes >= chunk_min_bytes:
            yield b"".join(pending)
            pending, pending_bytes = [], 0
    if emitted_any and has_trailing_newline:
        pending.append(EXPORT_LINE_SEPARATOR.encode("utf-8"))
        pending_bytes += 1
    if pending:
        yield b"".join(pending)


def verified_export(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    max_bytes: int,
) -> Dict[str, Any]:
    """Reconstruct a whole transcript and verify it BEFORE returning any bytes.

    Description: refuses first, then builds. The size refusal is read off
      the stored ``raw_byte_length`` so an oversized transcript is never
      buffered in order to discover it is oversized. On a hash mismatch
      the payload is DISCARDED and the two hashes are returned instead:
      the caller can re-run the comparison, which a boolean would not
      allow.
    Inputs: conn (sqlite3.Connection), transcript_id (int), max_bytes
      (int) - normally VERIFY_BEFORE_SEND_MAX_BYTES.
    Output: dict with ``status`` (one of the EXPORT_* constants),
      ``head`` (the :func:`export_head` record, or None when not_found),
      ``payload`` (bytes, only when status is EXPORT_OK),
      ``actual_sha256`` / ``actual_bytes`` (None when nothing was built),
      and ``detail`` (str, why a non-ok status happened).
    Example: verified_export(conn, 1, max_bytes=8388608)["status"] -> 'ok'
    """
    head = export_head(conn, transcript_id)
    if head is None:
        return {
            "status": EXPORT_NOT_FOUND, "head": None, "payload": None,
            "actual_sha256": None, "actual_bytes": None,
            "detail": f"no row in message_transcripts with id {transcript_id}",
        }
    if head["raw_byte_length"] > max_bytes:
        return {
            "status": EXPORT_TOO_LARGE, "head": head, "payload": None,
            "actual_sha256": None, "actual_bytes": None,
            "detail": (
                f"raw_byte_length {head['raw_byte_length']} exceeds "
                f"VERIFY_BEFORE_SEND_MAX_BYTES {max_bytes}; buffering it "
                f"would peak near {head['raw_byte_length'] * 12 // 1048576} "
                f"MB. Stream it and check the expected sha256 instead."
            ),
        }
    try:
        result = export_transcript(conn, transcript_id, strict=True)
    except ValueError as exc:
        # strict=True raises on a line that cannot be rendered. Specific,
        # and re-shaped rather than swallowed: the message names the line,
        # never a body value.
        return {
            "status": EXPORT_CANNOT_RENDER, "head": head, "payload": None,
            "actual_sha256": None, "actual_bytes": None, "detail": str(exc),
        }
    payload = result.text.encode("utf-8")
    if not result.verified:
        return {
            "status": EXPORT_MISMATCH, "head": head, "payload": None,
            "actual_sha256": result.actual_content_sha256,
            "actual_bytes": len(payload),
            "detail": (
                "the reconstructed bytes do not hash to the transcript's "
                "stored content_sha256; the payload was discarded rather "
                "than delivered. Both hashes are reported so the "
                "comparison can be re-run."
            ),
        }
    return {
        "status": EXPORT_OK, "head": head, "payload": payload,
        "actual_sha256": result.actual_content_sha256,
        "actual_bytes": len(payload), "detail": "",
    }


def joined_export_text(conn: sqlite3.Connection, transcript_id: int) -> str:
    """Join a streamed export back into one string, for tests and small callers.

    Description: exists so a test can assert the STREAMED bytes equal the
      buffered ``export_transcript(...).text`` without duplicating the
      join rule. Never call this on a route: it defeats the entire point
      of streaming.
    Inputs: conn (sqlite3.Connection), transcript_id (int).
    Output: str.
    Example: joined_export_text(conn, 1) == export_transcript(conn, 1).text
    """
    head = export_head(conn, transcript_id)
    if head is None:
        return ""
    chunks = iter_export_bytes(
        conn, transcript_id, has_trailing_newline=head["has_trailing_newline"]
    )
    return b"".join(chunks).decode("utf-8")


def stream_sha256(chunks: Iterator[bytes]) -> str:
    """Hash a streamed export without retaining it. Helper for tests.

    Inputs: chunks (Iterator[bytes]).
    Output: str - lowercase hex sha256, comparable to content_sha256
      because :func:`src.core.message_model_serialize.sha256_text` hashes
      UTF-8 bytes and these chunks ARE those bytes.
    Example: stream_sha256(iter([b"a"])) == sha256_text("a")
    """
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()
