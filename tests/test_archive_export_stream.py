"""Tests for the streaming export path, and for the one property that
keeps it honest: it must produce the SAME BYTES as the buffered path.

WHY THIS FILE EXISTS AT ALL. ``export_transcript`` and
``iter_export_lines`` answer the same question - what were this
transcript's original bytes - and the product's core guarantee is that
the answer is byte-identical to the ingested file. Two implementations of
that would diverge, and the divergence would be INVISIBLE, because each
one looks correct read on its own. So the refactor makes
``export_transcript`` a consumer of ``iter_export_lines``, and the
headline test here (:func:`test_streamed_bytes_equal_buffered_bytes`)
compares the two assembled outputs byte for byte on every fixture shape.
It is a positive control on the refactor itself: it was proven capable of
going RED by temporarily breaking one path before it was trusted green.

Everything here runs against throwaway in-memory databases built by
ingesting text. The live corpus is never opened by this file.
"""

from __future__ import annotations

import gc
import hashlib
import json
import sqlite3
import weakref
from typing import Iterator, List, Tuple

import pytest

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_model_export import (
    EXPORT_FETCH_BATCH_ROWS,
    VERIFY_CANNOT_RENDER,
    VERIFY_MATCH,
    export_transcript,
    iter_export_lines,
    subagent_edges,
)
from src.core.message_model_ingest import SourceLine, ingest_lines

#: Encoding of a transcript on disk, so byte comparisons say which
#: bytes they mean rather than implying "whatever str does".
TRANSCRIPT_ENCODING: str = "utf-8"

#: The separator ``join_lines`` puts between lines. The streaming
#: assembler must reproduce it exactly; CRLF sources carry their own
#: trailing "\r" INSIDE the line text, which is why this stays "\n" for
#: every line_ending classification.
LINE_SEPARATOR: str = "\n"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """A throwaway in-memory database at the current schema version.

    Description: matches tests/test_message_model_export.py's fixture
      style deliberately - the two files test two halves of one path.
    Inputs: none (pytest fixture).
    Output: sqlite3.Connection.
    Example: conn.execute("SELECT 1").fetchone() -> (1,)
    """
    connection = sqlite3.connect(":memory:")
    with connection:
        run_chain(connection, 0, CURRENT_SCHEMA_VERSION)
    return connection


def line(**fields: object) -> str:
    """Render one transcript line from ordered keyword fields.

    Inputs: fields (keyword arguments, in emission order).
    Output: str.
    Example: line(a=1) -> '{"a":1}'
    """
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False)


def _sample_lines(count: int) -> List[str]:
    """Build ``count`` distinct, well-formed transcript lines.

    Description: distinct uuids and text so a line-ordering bug cannot
      hide behind identical lines.
    Inputs: count (int).
    Output: list[str].
    Example: len(_sample_lines(2)) -> 2
    """
    out: List[str] = [
        line(type="user", uuid="u0", parentUuid=None,
             timestamp="2026-01-01T00:00:00Z", sessionId="s1")
    ]
    for index in range(1, count):
        out.append(line(
            type="assistant", uuid=f"u{index}", parentUuid=f"u{index - 1}",
            timestamp="2026-01-01T00:00:01Z", sessionId="s1",
            message={"role": "assistant", "model": "m",
                     "content": [{"type": "text", "text": f"body {index}"}]},
        ))
    return out


def _ingest(
    conn: sqlite3.Connection, *, source_ref: str, texts: List[str],
    trailing: bool = True, line_ending: str = "LF",
) -> int:
    """Ingest an explicit list of line texts and return the transcript id.

    Description: uses ingest_lines rather than ingest_text so the test
      controls has_trailing_newline and line_ending directly - ingest_text
      derives the first and hardcodes the second to "LF", which would make
      the CRLF/MIXED/NONE cases untestable.
    Inputs: conn, source_ref (str, unique per transcript), texts
      (list[str] of whole line texts), trailing (bool), line_ending
      (str, one of LF/CRLF/MIXED/NONE).
    Output: int - the transcript id.
    Example: _ingest(conn, source_ref="a", texts=["{}"]) -> 1
    """
    with conn:
        return ingest_lines(
            conn, source_ref=source_ref, session_ref="s1",
            lines=[SourceLine(text=text) for text in texts],
            has_trailing_newline=trailing, line_ending=line_ending,
        ).transcript_id


def _stream_bytes(conn: sqlite3.Connection, transcript_id: int) -> bytes:
    """Assemble a transcript's bytes the way a streaming route would.

    Description: an INDEPENDENT assembler, deliberately not a call to
      join_lines, so the headline equality test compares two real
      assemblies rather than one function against itself.
    Inputs: conn (sqlite3.Connection), transcript_id (int).
    Output: bytes - the whole transcript.
    Raises: whatever iter_export_lines raises, unchanged.
    Example: _stream_bytes(conn, 1) -> b'{"a":1}\\n'
    """
    return b"".join(_stream_chunks(conn, transcript_id))


def _stream_chunks(
    conn: sqlite3.Connection, transcript_id: int,
) -> Iterator[bytes]:
    """Yield a transcript's bytes chunk by chunk, lazily.

    Description: separated from :func:`_stream_bytes` so a test can
      observe how many bytes reached the wire BEFORE a mid-stream failure,
      which is the only way to assert the documented truncation
      behaviour honestly.
    Inputs: conn (sqlite3.Connection), transcript_id (int).
    Output: Iterator[bytes].
    Raises: whatever iter_export_lines raises, unchanged.
    Example: b"".join(_stream_chunks(conn, 1)) -> b'{"a":1}\\n'
    """
    has_trailing = _has_trailing_newline(conn, transcript_id)
    pending_separator = False
    emitted_any = False
    for export in iter_export_lines(conn, transcript_id):
        if export.text is None:
            raise ValueError(
                f"line {export.line_no} cannot be rendered: {export.detail}"
            )
        if pending_separator:
            yield LINE_SEPARATOR.encode(TRANSCRIPT_ENCODING)
        yield export.text.encode(TRANSCRIPT_ENCODING)
        pending_separator = True
        emitted_any = True
    if emitted_any and has_trailing:
        yield LINE_SEPARATOR.encode(TRANSCRIPT_ENCODING)


def _has_trailing_newline(
    conn: sqlite3.Connection, transcript_id: int,
) -> bool:
    """Read one transcript's stored trailing-newline flag.

    Inputs: conn (sqlite3.Connection), transcript_id (int).
    Output: bool.
    Raises: LookupError - no such transcript.
    Example: _has_trailing_newline(conn, 1) -> True
    """
    row = conn.execute(
        "SELECT has_trailing_newline FROM message_transcripts WHERE id = ?",
        (transcript_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no transcript with id {transcript_id}")
    return bool(row[0])


def _fixture_shapes() -> List[Tuple[str, List[str], bool, str]]:
    """Every transcript shape the equality test is run against.

    Description: the fields that decide assembly are the line texts and
      the trailing-newline flag, so all four line_ending classifications
      appear with both trailing states, plus the batch boundary.
    Inputs: none.
    Output: list of (label, line texts, has_trailing_newline, line_ending).
    Example: _fixture_shapes()[0][0] -> "lf-trailing"
    """
    lf = _sample_lines(4)
    crlf = [text + "\r" for text in lf]
    mixed = [lf[0], lf[1] + "\r", lf[2], lf[3] + "\r"]
    shapes: List[Tuple[str, List[str], bool, str]] = []
    for label, texts, ending in (
        ("lf", lf, "LF"), ("crlf", crlf, "CRLF"), ("mixed", mixed, "MIXED"),
    ):
        shapes.append((f"{label}-trailing", texts, True, ending))
        shapes.append((f"{label}-no-trailing", texts, False, ending))
    # NONE means the file held no newline at all, which is exactly one
    # line and no trailing newline. The trailing=True variant is still
    # ingested so the assembler's flag handling is exercised on it.
    shapes.append(("none-no-trailing", _sample_lines(1), False, "NONE"))
    shapes.append(("none-trailing", _sample_lines(1), True, "NONE"))
    # Straddle the fetchmany boundary: a batching bug that drops or
    # duplicates the seam is invisible on a 4-line transcript.
    shapes.append(("batch-boundary",
                   _sample_lines(EXPORT_FETCH_BATCH_ROWS * 2 + 3), True, "LF"))
    return shapes


# ---- THE TEST THAT KEEPS THE TWO PATHS FROM DIVERGING -------------------

#: Built once: a second call would build two 515-line transcripts.
_SHAPES = _fixture_shapes()


@pytest.mark.parametrize("label,texts,trailing,line_ending", _SHAPES,
                         ids=[shape[0] for shape in _SHAPES])
def test_streamed_bytes_equal_buffered_bytes(
    conn: sqlite3.Connection, label: str, texts: List[str], trailing: bool,
    line_ending: str,
) -> None:
    """The headline invariant, on every fixture shape.

    b"".join of the streaming path, assembled with the stored
    trailing-newline flag, must equal export_transcript(...).text encoded
    - byte for byte, not "equivalent", not "same after normalisation".
    """
    transcript_id = _ingest(conn, source_ref=label, texts=texts,
                            trailing=trailing, line_ending=line_ending)
    buffered = export_transcript(conn, transcript_id)
    streamed = _stream_bytes(conn, transcript_id)
    assert streamed == buffered.text.encode(TRANSCRIPT_ENCODING)
    # And both must equal what was actually ingested, so a shared bug
    # cannot make them agree on the wrong answer.
    tail = LINE_SEPARATOR if trailing else ""
    original = LINE_SEPARATOR.join(texts) + tail
    assert streamed == original.encode(TRANSCRIPT_ENCODING)
    assert buffered.verified


def test_streamed_bytes_match_the_stored_hash_and_byte_length(
    conn: sqlite3.Connection,
) -> None:
    """The streaming path reproduces the stored content_sha256 and
    raw_byte_length, which is the guarantee stated in spec section 1."""
    texts = _sample_lines(EXPORT_FETCH_BATCH_ROWS + 7)
    transcript_id = _ingest(conn, source_ref="hashcheck", texts=texts)
    stored_sha, stored_len = conn.execute(
        "SELECT content_sha256, raw_byte_length FROM message_transcripts "
        "WHERE id = ?", (transcript_id,)
    ).fetchone()
    streamed = _stream_bytes(conn, transcript_id)
    assert hashlib.sha256(streamed).hexdigest() == stored_sha
    assert len(streamed) == stored_len


def test_line_order_and_count_match_the_buffered_path(
    conn: sqlite3.Connection,
) -> None:
    """Same lines, same order, same per-line verdicts and hashes."""
    transcript_id = _ingest(conn, source_ref="order",
                            texts=_sample_lines(EXPORT_FETCH_BATCH_ROWS + 1))
    streamed = list(iter_export_lines(conn, transcript_id))
    buffered = export_transcript(conn, transcript_id).lines
    assert [ln.line_no for ln in streamed] == list(range(len(streamed)))
    assert [ln.line_no for ln in streamed] == [ln.line_no for ln in buffered]
    assert [ln.text for ln in streamed] == [ln.text for ln in buffered]
    assert [ln.actual_sha256 for ln in streamed] == \
        [ln.actual_sha256 for ln in buffered]
    assert all(ln.outcome == VERIFY_MATCH for ln in streamed)


def test_a_raw_line_row_streams_identically(conn: sqlite3.Connection) -> None:
    """The 1-in-3,125,122 raw_line branch is live in both paths. Spec
    10.8: a lopsided corpus will not exercise it by accident."""
    transcript_id = _ingest(conn, source_ref="rawline",
                            texts=_sample_lines(3))
    stored = conn.execute(
        "SELECT line_sha256 FROM message_appearances "
        "WHERE transcript_id = ? AND line_no = 1", (transcript_id,)
    ).fetchone()[0]
    original = export_transcript(conn, transcript_id).lines[1].text
    with conn:
        # Store the identical text as a raw line; the hash still matches,
        # so this exercises the branch without changing any byte.
        conn.execute(
            "UPDATE message_appearances SET raw_line = ? "
            "WHERE transcript_id = ? AND line_no = 1",
            (original, transcript_id),
        )
    assert conn.execute(
        "SELECT line_sha256 FROM message_appearances "
        "WHERE transcript_id = ? AND line_no = 1", (transcript_id,)
    ).fetchone()[0] == stored
    buffered = export_transcript(conn, transcript_id)
    assert _stream_bytes(conn, transcript_id) == \
        buffered.text.encode(TRANSCRIPT_ENCODING)
    assert buffered.lines[1].detail == "raw line as stored"


def test_an_unknown_transcript_streams_nothing_rather_than_guessing(
    conn: sqlite3.Connection,
) -> None:
    """iter_export_lines has no transcript row to consult, so it yields
    nothing; export_transcript, which does consult one, still raises.
    Two different questions, two different answers, neither invented."""
    assert list(iter_export_lines(conn, 999)) == []
    with pytest.raises(LookupError):
        export_transcript(conn, 999)


# ---- unrenderable and corrupt rows -------------------------------------

def test_an_unrenderable_row_is_yielded_as_its_own_outcome(
    conn: sqlite3.Connection,
) -> None:
    """Per the spec, iter_export_lines does NOT decide that a row which
    cannot be rendered is fatal - it names the condition and hands the
    verdict to the caller. That is what lets strict and non-strict share
    one rendering path."""
    transcript_id = _ingest(conn, source_ref="unrenderable",
                            texts=_sample_lines(3))
    with conn:
        conn.execute(
            "UPDATE message_appearances SET serializer_style = NULL, "
            "raw_line = NULL WHERE transcript_id = ? AND line_no = 1",
            (transcript_id,))
    streamed = list(iter_export_lines(conn, transcript_id))
    assert [ln.outcome for ln in streamed] == [
        VERIFY_MATCH, VERIFY_CANNOT_RENDER, VERIFY_MATCH]
    assert streamed[1].text is None
    assert streamed[1].actual_sha256 is None
    assert streamed[1].detail
    # And the caller that decided it IS fatal still says so, by line.
    with pytest.raises(ValueError, match="line 1 cannot be rendered"):
        export_transcript(conn, transcript_id)


def test_a_corrupted_body_json_aborts_the_stream_and_names_the_line(
    conn: sqlite3.Connection,
) -> None:
    """A body_json that is not JSON at all is a broken STORE, not a
    render verdict, so it propagates as json.JSONDecodeError from both
    paths identically - this asserts the behaviour that exists rather
    than a nicer one that does not.

    The spec (6.9) documents streams-then-fails: bytes for the lines
    BEFORE the bad one are already on the wire, and the consumer learns
    of the truncation from the failure plus the trailer. So the assertion
    is the documented one - zero bytes of the FAILING line's body are
    emitted, the preceding lines' bytes are, and the caller can tell the
    output is short.
    """
    texts = _sample_lines(4)
    transcript_id = _ingest(conn, source_ref="corrupt", texts=texts)
    whole = export_transcript(conn, transcript_id).text
    body_id = conn.execute(
        "SELECT body_id FROM message_appearances "
        "WHERE transcript_id = ? AND line_no = 2", (transcript_id,)
    ).fetchone()[0]
    with conn:
        conn.execute("UPDATE message_bodies SET body_json = ? WHERE id = ?",
                     ("{not json at all", body_id))

    emitted = bytearray()
    with pytest.raises(json.JSONDecodeError):
        for chunk in _stream_chunks(conn, transcript_id):
            emitted.extend(chunk)
    # Lines 0 and 1 plus their separators are on the wire; nothing of the
    # failing line's body is, and the whole file is short.
    expected_prefix = (texts[0] + LINE_SEPARATOR + texts[1]).encode(
        TRANSCRIPT_ENCODING)
    assert bytes(emitted).startswith(expected_prefix)
    assert len(emitted) <= len(expected_prefix) + len(LINE_SEPARATOR)
    assert b"body 2" not in bytes(emitted)
    assert len(emitted) < len(whole.encode(TRANSCRIPT_ENCODING))
    # The buffered path fails the same way, so neither is a softer mode.
    with pytest.raises(json.JSONDecodeError):
        export_transcript(conn, transcript_id)


def test_the_iterator_does_not_retain_rendered_text(
    conn: sqlite3.Connection,
) -> None:
    """The 12x memory bug was retention of every rendered line. A
    consumer that keeps no reference must be able to have each line
    collected, so nothing the iterator holds may keep them alive."""
    transcript_id = _ingest(conn, source_ref="retention",
                            texts=_sample_lines(EXPORT_FETCH_BATCH_ROWS + 5))
    refs: List[weakref.ReferenceType] = []
    for index, export in enumerate(iter_export_lines(conn, transcript_id)):
        if index % 50 == 0:
            refs.append(weakref.ref(export))
        del export
    gc.collect()
    assert refs, "fixture produced no sampled lines"
    assert all(ref() is None for ref in refs)


# ---- subagent_edges scoping --------------------------------------------

def _ingest_subagent(
    conn: sqlite3.Connection, *, source_ref: str, agent_id: str,
    uuid: str, session_id: str,
) -> int:
    """Ingest a one-line subagent transcript and return its id.

    Inputs: conn, source_ref (str), agent_id (str), uuid (str),
      session_id (str - the ORIGIN session the body itself names).
    Output: int - the transcript id.
    Example: _ingest_subagent(conn, source_ref="s", agent_id="a",
      uuid="u", session_id="s1") -> 1
    """
    text = line(type="user", uuid=uuid, parentUuid=None,
                timestamp="2026-01-01T00:00:00Z", sessionId=session_id,
                isSidechain=True, agentId=agent_id)
    with conn:
        return ingest_lines(
            conn, source_ref=source_ref, session_ref=f"agent:{agent_id}",
            lines=[SourceLine(text=text)],
        ).transcript_id


def test_unscoped_subagent_edges_is_unchanged(
    conn: sqlite3.Connection,
) -> None:
    """The default of None must behave exactly as it did before the
    parameter existed: every edge in the database, ordered by id."""
    first = _ingest_subagent(conn, source_ref="s1", agent_id="a1",
                             uuid="ua1", session_id="orig1")
    second = _ingest_subagent(conn, source_ref="s2", agent_id="a2",
                              uuid="ua2", session_id="orig2")
    _ingest(conn, source_ref="main", texts=_sample_lines(2))
    edges = subagent_edges(conn)
    assert len(edges) == 2
    assert [edge["agent_id"] for edge in edges] == ["a1", "a2"]
    assert [edge["appearance_id"] for edge in edges] == \
        sorted(edge["appearance_id"] for edge in edges)
    assert edges[0]["transcript_session_ref"] == "agent:a1"
    assert edges[0]["origin_session_ref"] == "orig1"
    assert edges[0]["is_sidechain"] is True
    assert first != second


def test_scoped_subagent_edges_returns_only_that_transcript(
    conn: sqlite3.Connection,
) -> None:
    first = _ingest_subagent(conn, source_ref="s1", agent_id="a1",
                             uuid="ua1", session_id="orig1")
    second = _ingest_subagent(conn, source_ref="s2", agent_id="a2",
                              uuid="ua2", session_id="orig2")
    def agents(tid: int) -> List[str]:
        return [str(e["agent_id"])
                for e in subagent_edges(conn, transcript_id=tid)]

    assert agents(first) == ["a1"]
    assert agents(second) == ["a2"]


def test_the_scoped_union_equals_the_unscoped_set(
    conn: sqlite3.Connection,
) -> None:
    """Scoping must PARTITION the unscoped set, not sample it - an edge
    that belongs to no transcript's page would be invisible to every API
    caller while still counting in the unscoped total."""
    for index in range(4):
        _ingest_subagent(conn, source_ref=f"s{index}", agent_id=f"a{index}",
                         uuid=f"ua{index}", session_id=f"orig{index}")
    _ingest(conn, source_ref="main", texts=_sample_lines(3))
    unscoped = subagent_edges(conn)
    ids = [row[0] for row in conn.execute(
        "SELECT id FROM message_transcripts ORDER BY id")]
    union: List[dict] = []
    for transcript_id in ids:
        union.extend(subagent_edges(conn, transcript_id=transcript_id))
    assert union == unscoped
    assert len(unscoped) == 4


def test_scoping_a_transcript_with_no_edges_returns_empty(
    conn: sqlite3.Connection,
) -> None:
    main = _ingest(conn, source_ref="main", texts=_sample_lines(2))
    _ingest_subagent(conn, source_ref="s1", agent_id="a1", uuid="ua1",
                     session_id="orig1")
    assert subagent_edges(conn, transcript_id=main) == []
    assert subagent_edges(conn, transcript_id=99999) == []
    assert len(subagent_edges(conn)) == 1
