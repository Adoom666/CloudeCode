"""``/api/v1/archive/transcripts/{id}/export[/verified]`` - the two export forms.

Split out of ``archive_routes.py`` only for the repo's 500-line cap. The
routes are registered on that module's router, so ``src/main.py`` mounts
one router and the contract test walks one ``router.routes``.

WHICH FORM IS CORRECT.

- **Streaming** (``/export``) is the DEFAULT and the only form safe at any
  size. Peak memory is one fetch batch plus the LARGEST SINGLE LINE, and
  that second term is the floor nobody expects: the corpus's largest
  transcript (244,117,661 bytes) holds one 37,404,061-byte line, and
  streaming it measured **556 MB peak RSS** - not the ~78 MB the design
  document quotes, which came from a transcript with no such line. The
  concurrency limit below is therefore sized against ~600 MB per
  in-flight export, not 78 MB. Its cost: bytes reach the client before
  the hash is known, so the client MUST compare.
- **Verify-before-send** (``/export/verified``) buffers, compares hashes,
  and only then responds. Right for a browser download or an automated
  restore - anything that cannot check a hash afterwards. It REFUSES
  above ``VERIFY_BEFORE_SEND_MAX_BYTES`` with a 413 rather than silently
  falling back to streaming, because the caller asked for a guarantee
  this form cannot give at that size.

HTTP TRAILERS: MEASURED UNAVAILABLE ON THIS STACK, AND SAID SO OUT LOUD.
The design document specifies the streamed hash as an HTTP trailer.
uvicorn 0.52.3 declares the ASGI trailer message types in ``_types.py``
but implements them in NEITHER http protocol (h11 or httptools), and
advertises no ``http.response.trailers`` extension - so a trailer cannot
be emitted here. Rather than advertise a ``Trailer:`` header that never
arrives (a verification step that cannot fail, which is worse than no
verification at all), this route sends trailers ONLY when the server
actually advertises the extension, and otherwise sets
``X-Archive-Verification: expected_only`` and names the reason in
``X-Archive-Trailer-Unavailable``. The expected hash and byte count are
on the LEADING headers either way, so a client can always verify what it
received; what it cannot do is mistake a missing trailer for a pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import queue
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from src.api.archive_support import state_dir
from src.api.auth import require_auth
from src.core import archive_export
from src.core.archive_read import (
    API_PREFIX,
    RESULT_CANNOT_DETERMINE,
    SCOPE_RESOLVED,
    VERIFY_BEFORE_SEND_MAX_BYTES,
    cannot_determine_envelope,
    envelope,
    not_found_envelope,
    open_read_only,
)
from src.core.db import DatastoreUnreadableError

logger = structlog.get_logger()

router = APIRouter(tags=["archive"])

#: How many streaming exports may be in flight in this process at once.
#: Sized against a MEASURED ~600 MB peak for the worst transcript in the
#: corpus (one 37 MB line inside a 244 MB file), NOT against the design
#: document's ~78 MB figure, which was measured on a transcript with no
#: such line. Two concurrent worst-case exports is about 1.2 GB; three
#: would be 1.8 GB on a machine also running the app.
MAX_CONCURRENT_EXPORTS: int = 2

#: How long a request waits for a slot before being refused. A named
#: refusal beats an unbounded queue: a client that hangs forever cannot
#: tell a slow export from a wedged one.
EXPORT_SLOT_WAIT_SECONDS: float = 30.0

#: Depth of the hand-off queue between the reader thread and the event
#: loop, in chunks. Bounded so a slow client applies backpressure to the
#: reader instead of letting the whole transcript pile up in memory -
#: which would reintroduce exactly the buffering streaming exists to
#: avoid.
STREAM_QUEUE_CHUNKS: int = 4

#: ASGI extension a server must advertise before a trailer can be sent.
TRAILERS_EXTENSION: str = "http.response.trailers"

_EXPORT_SLOTS: Optional[asyncio.Semaphore] = None


def _slots() -> asyncio.Semaphore:
    """Return the process-wide streaming-export semaphore, creating it once.

    Description: built lazily because there is no running event loop at
      import time. Not a magic number at the call site: the bound and the
      measurement behind it live on ``MAX_CONCURRENT_EXPORTS``.
    Inputs: none. Output: asyncio.Semaphore.
    Example: async with _slots(): ...
    """
    global _EXPORT_SLOTS
    if _EXPORT_SLOTS is None:
        _EXPORT_SLOTS = asyncio.Semaphore(MAX_CONCURRENT_EXPORTS)
    return _EXPORT_SLOTS


def _read_head(transcript_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read the export head on a throwaway read-only connection.

    Description: the expected hash, byte count and filename must be known
      BEFORE the first streamed byte, because they go on the leading
      headers. Runs in a worker thread via ``asyncio.to_thread``.
    Inputs: transcript_id (int).
    Output: (head or None, unreadable reason or None). Both None-head and
      a reason are distinct: no head with no reason means the transcript
      does not exist; a reason means the datastore would not open, which
      is a different finding.
    Example: _read_head(1)[0]["raw_byte_length"] -> 58968
    """
    try:
        conn = open_read_only(state_dir())
    except DatastoreUnreadableError as exc:
        return None, str(exc)
    try:
        return archive_export.export_head(conn, transcript_id), None
    finally:
        conn.close()


def _pump(
    state_dir: Path,
    transcript_id: int,
    has_trailing_newline: bool,
    out: "queue.Queue",
) -> None:
    """Read and render a transcript on ONE dedicated thread, feeding a queue.

    Description: a ``sqlite3`` connection is bound to the thread that
      created it, and ``asyncio.to_thread`` hands consecutive calls to
      DIFFERENT pool threads - so the connection is opened, used and
      closed here, on one thread, rather than being reached across the
      pool. The queue is bounded, so a slow client blocks this thread
      instead of letting the transcript accumulate in memory.
    Inputs: state_dir (Path), transcript_id (int), has_trailing_newline
      (bool), out (queue.Queue) - receives ``("chunk", bytes)`` then
      exactly one terminal ``("done", None)`` or ``("error", str)``.
    Output: None.
    Example: threading.Thread(target=_pump, args=(...)).start()
    """
    terminal: Tuple[str, Optional[str]] = ("done", None)
    try:
        conn = open_read_only(state_dir)
    except DatastoreUnreadableError as exc:
        out.put(("error", f"datastore would not open: {exc}"))
        return
    try:
        chunks: Iterator[bytes] = archive_export.iter_export_bytes(
            conn, transcript_id, has_trailing_newline=has_trailing_newline
        )
        for chunk in chunks:
            out.put(("chunk", chunk))
    except archive_export.ExportRenderError as exc:
        # A line that cannot be rendered ABORTS the stream. Skipping it
        # would emit a shorter file that fails the whole-file hash for a
        # reason nobody could read off the failure.
        terminal = ("error", str(exc))
    finally:
        conn.close()
        out.put(terminal)


class ExportStreamResponse(Response):
    """Stream a transcript's exact bytes, hashing as it goes.

    Description: a raw ASGI response rather than a ``StreamingResponse``
      because the actual hash is not known until the last byte is sent,
      and it has to be delivered as an HTTP trailer where the server
      supports one. It computes the digest over the SAME bytes it puts on
      the wire, so the reported hash is a measurement of what was
      delivered, not of what was intended.
    Inputs: head (dict from ``archive_export.export_head``), state_dir
      (Path), leading (dict of leading headers), trailers_ok (bool).
    Output: an ASGI callable.
    Example: ExportStreamResponse(head=h, state_dir=sd, leading={},
             trailers_ok=False)
    """

    def __init__(
        self,
        *,
        head: Dict[str, Any],
        state_dir: Path,
        leading: Dict[str, str],
        trailers_ok: bool,
        on_finish: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(status_code=200, headers=leading,
                         media_type="application/x-ndjson")
        # MEASURED BUG, and it is silent from the server side.
        # ``Response.__init__`` has no content, so it stamps
        # ``content-length: 0``. The server then streams 182 MB, logs
        # bytes_sent=182077926 and verified=true, and the CLIENT keeps
        # ZERO bytes, because it stopped reading at the declared length.
        # Every server-side signal was green. The length is unknowable
        # before the last line is rendered, so the header must not be
        # there at all: without it the response is chunked and the
        # client reads to the end.
        self.raw_headers = [
            (name, value) for name, value in self.raw_headers
            if name.lower() != b"content-length"
        ]
        self._head = head
        self._state_dir = state_dir
        self._trailers_ok = trailers_ok
        self._on_finish = on_finish

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the export, then its verification trailer where possible.

        Args:
            scope: the ASGI scope.
            receive: the ASGI receive callable.
            send: the ASGI send callable.
        """
        try:
            await self._send(scope, send)
        finally:
            if self._on_finish is not None:
                self._on_finish()

    async def _send(self, scope: Scope, send: Send) -> None:
        """Do the actual streaming. Split out so ``__call__`` owns cleanup.

        Args:
            scope: the ASGI scope.
            send: the ASGI send callable.
        """
        supports = self._trailers_ok and bool(
            (scope.get("extensions") or {}).get(TRAILERS_EXTENSION)
        )
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": self.raw_headers,
            "trailers": supports,
        })
        out: "queue.Queue" = queue.Queue(maxsize=STREAM_QUEUE_CHUNKS)
        worker = threading.Thread(
            target=_pump,
            args=(self._state_dir, self._head["transcript_id"],
                  self._head["has_trailing_newline"], out),
            name=f"archive-export-{self._head['transcript_id']}",
            daemon=True,
        )
        worker.start()
        digest = hashlib.sha256()
        sent = 0
        failure: Optional[str] = None
        try:
            while True:
                kind, value = await asyncio.to_thread(out.get)
                if kind == "chunk":
                    digest.update(value)
                    sent += len(value)
                    await send({"type": "http.response.body", "body": value,
                                "more_body": True})
                    continue
                failure = value if kind == "error" else None
                break
        finally:
            worker.join(timeout=5.0)
        await send({"type": "http.response.body", "body": b"",
                    "more_body": bool(supports)})
        verified = _stream_verdict(self._head, digest.hexdigest(), sent, failure)
        logger.info(
            "archive_export_stream",
            transcript_id=self._head["transcript_id"],
            bytes_sent=sent, verified=verified,
            trailers_sent=supports,
        )
        if supports:
            await send({
                "type": "http.response.trailers",
                "headers": [
                    (b"x-archive-actual-sha256", digest.hexdigest().encode()),
                    (b"x-archive-actual-bytes", str(sent).encode()),
                    (b"x-archive-verified", verified.encode()),
                ],
                "more_trailers": False,
            })


def _stream_verdict(
    head: Dict[str, Any], actual_sha: str, sent: int, failure: Optional[str]
) -> str:
    """Name the streamed export's outcome. Three values, never two.

    Description: ``cannot_determine`` is not a flavour of failure - it
      means the comparison never ran, because the stream aborted partway
      and the bytes on the wire are a truncation, not a transcript.
    Inputs: head (dict), actual_sha (str), sent (int), failure (str|None).
    Output: str - 'true', 'false' or 'cannot_determine'.
    Example: _stream_verdict(h, h["content_sha256"], h["raw_byte_length"],
             None) -> 'true'
    """
    if failure is not None:
        return "cannot_determine"
    if actual_sha == head["content_sha256"] and sent == head["raw_byte_length"]:
        return "true"
    return "false"


def _export_headers(head: Dict[str, Any], *, verification: str) -> Dict[str, str]:
    """Build the leading headers both export forms share.

    Description: the EXPECTED hash and byte count go out before the first
      byte, so a client can verify what it received no matter which form
      it used and no matter whether a trailer ever arrives.
    Inputs: head (dict), verification (str) - 'trailer', 'expected_only'
      or 'before_send'.
    Output: dict of header name to value.
    Example: _export_headers(h, verification="before_send")["X-Archive-Expected-Bytes"]
    """
    return {
        "Content-Disposition":
            f'attachment; filename="{head["session_ref"]}.jsonl"',
        "X-Archive-Transcript-Id": str(head["transcript_id"]),
        "X-Archive-Expected-Sha256": str(head["content_sha256"]),
        "X-Archive-Expected-Bytes": str(head["raw_byte_length"]),
        "X-Archive-Verification": verification,
    }


@router.get("/archive/transcripts/{transcript_id}/export", response_model=None,
            dependencies=[Depends(require_auth)])
async def get_export_stream(transcript_id: int) -> Response:
    """Stream a transcript's exact original bytes.

    Description: the DEFAULT export and the only one that works on a
        large transcript. There is no ``?redact=`` parameter and there
        never will be: a redacted transcript is not the transcript, and a
        guarantee with a mode is not a guarantee.

    Args:
        transcript_id: the transcript to export.

    Returns:
        ``200`` with ``application/x-ndjson`` and the raw bytes, carrying
        ``X-Archive-Expected-Sha256`` / ``-Bytes`` on the leading headers.
        A missing transcript is a ``404`` envelope; an unopenable
        datastore is a ``200`` ``cannot_determine`` envelope; too many
        concurrent exports is a ``503`` ``cannot_determine`` naming the
        limit rather than an unbounded wait.
    """
    head, unreadable = await asyncio.to_thread(_read_head, transcript_id)
    if unreadable is not None:
        return JSONResponse(status_code=200, content=envelope(
            result=None, result_status=RESULT_CANNOT_DETERMINE,
            scope_status="cannot_determine",
            unevaluated=[{"subject": "datastore", "reason": unreadable}],
        ))
    if head is None:
        return JSONResponse(status_code=404, content=not_found_envelope(
            f"transcript:{transcript_id}",
            f"no row in message_transcripts with id {transcript_id}",
            result=None,
        ))
    try:
        await asyncio.wait_for(_slots().acquire(), timeout=EXPORT_SLOT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=503, content=cannot_determine_envelope(
            f"transcript:{transcript_id}",
            f"{MAX_CONCURRENT_EXPORTS} streaming exports are already in "
            f"flight and no slot came free within {EXPORT_SLOT_WAIT_SECONDS}s. "
            f"The bound exists because one worst-case export was measured at "
            f"about 600 MB peak RSS. Retry.",
            result=None, scope_status=SCOPE_RESOLVED,
            meta={"limit": {"max_concurrent_exports": MAX_CONCURRENT_EXPORTS}},
        ))
    leading = _export_headers(head, verification="expected_only")
    leading["X-Archive-Trailer-Unavailable"] = (
        "uvicorn implements no http.response.trailers extension; compare the "
        "bytes you received against X-Archive-Expected-Sha256 yourself"
    )
    # The slot is released inside the response's own ``__call__``
    # finally-block, NOT by wrapping the instance: Python resolves an
    # implicit ``await response(...)`` on the TYPE, so an instance-level
    # ``__call__`` attribute is never consulted and the slot would leak
    # permanently - the endpoint would then refuse every later export
    # with a concurrency message that is no longer true.
    return ExportStreamResponse(
        head=head, state_dir=state_dir(), leading=leading, trailers_ok=True,
        on_finish=_slots().release,
    )


@router.get("/archive/transcripts/{transcript_id}/export/verified",
            response_model=None, dependencies=[Depends(require_auth)])
async def get_export_verified(transcript_id: int) -> Response:
    """Reconstruct a transcript, verify it, and only THEN send it.

    Description: refuses above ``VERIFY_BEFORE_SEND_MAX_BYTES`` with a
        ``413`` and a ``cannot_determine`` naming the streaming href. It
        does NOT fall back to streaming: the caller asked for a guarantee
        this form cannot provide at that size, and quietly giving them
        the other form would be an answer to a question they did not ask.
        On a hash mismatch the payload is DISCARDED and both hashes are
        returned, so the comparison can be re-run - a boolean would not
        allow that.

    Args:
        transcript_id: the transcript to export.

    Returns:
        ``200`` with the bytes and a real ``X-Archive-Actual-Sha256``
        HEADER (not a trailer - it is known before the response starts),
        or an envelope at ``404`` / ``413`` / ``200``.
    """
    result = await asyncio.to_thread(
        _verified_export_in_thread, transcript_id,
    )
    status = result["status"]
    head = result["head"]
    if status == "unreadable":
        return JSONResponse(status_code=200, content=envelope(
            result=None, result_status=RESULT_CANNOT_DETERMINE,
            scope_status="cannot_determine",
            unevaluated=[{"subject": "datastore", "reason": result["detail"]}],
        ))
    if status == archive_export.EXPORT_NOT_FOUND:
        return JSONResponse(status_code=404, content=not_found_envelope(
            f"transcript:{transcript_id}", result["detail"], result=None,
        ))
    stream_href = f"{API_PREFIX}/transcripts/{transcript_id}/export"
    if status == archive_export.EXPORT_TOO_LARGE:
        logger.info("archive_export_verified_refused",
                    transcript_id=transcript_id,
                    raw_byte_length=head["raw_byte_length"],
                    cap=VERIFY_BEFORE_SEND_MAX_BYTES)
        return JSONResponse(status_code=413, content=cannot_determine_envelope(
            f"transcript:{transcript_id}", result["detail"], result=None,
            scope_status=SCOPE_RESOLVED, meta={"stream_href": stream_href},
        ))
    if status != archive_export.EXPORT_OK:
        logger.info("archive_export_verified_failed",
                    transcript_id=transcript_id, status=status)
        return JSONResponse(status_code=200, content=cannot_determine_envelope(
            f"transcript:{transcript_id}", result["detail"], result=None,
            scope_status=SCOPE_RESOLVED,
            meta={
                "stream_href": stream_href,
                "expected_sha256": head["content_sha256"],
                "actual_sha256": result["actual_sha256"],
                "expected_bytes": head["raw_byte_length"],
                "actual_bytes": result["actual_bytes"],
            },
        ))
    headers = _export_headers(head, verification="before_send")
    headers["X-Archive-Actual-Sha256"] = str(result["actual_sha256"])
    headers["X-Archive-Actual-Bytes"] = str(result["actual_bytes"])
    headers["X-Archive-Verified"] = "true"
    logger.info("archive_export_verified", transcript_id=transcript_id,
                bytes_sent=result["actual_bytes"])
    return Response(content=result["payload"], status_code=200,
                    media_type="application/x-ndjson", headers=headers)


def _verified_export_in_thread(transcript_id: int) -> Dict[str, Any]:
    """Open the archive, run a verify-before-send export, and always close it.

    Description: kept out of the route so the handler stays parse,
      delegate, return. Turns "the database would not open" into a named
      status instead of an unexplained 500.
    Inputs: transcript_id (int).
    Output: the ``archive_export.verified_export`` record, or one with
      ``status='unreadable'``.
    Example: _verified_export_in_thread(1)["status"] -> 'ok'
    """
    try:
        conn = open_read_only(state_dir())
    except DatastoreUnreadableError as exc:
        return {"status": "unreadable", "head": None, "payload": None,
                "actual_sha256": None, "actual_bytes": None,
                "detail": str(exc)}
    try:
        return archive_export.verified_export(
            conn, transcript_id, max_bytes=VERIFY_BEFORE_SEND_MAX_BYTES,
        )
    finally:
        conn.close()
