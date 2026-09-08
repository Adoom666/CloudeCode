"""Line endings, trailing newlines, and the single-exemplar raw_line branch.

THE GAP THIS CLOSES. The schema permits ``line_ending`` in
(LF, CRLF, MIXED, NONE) and ``has_trailing_newline`` in (0, 1). The corpus
occupies TWO of those eight cells: LF with a trailing newline on 21,021
transcripts, LF without on 18. Six cells have no live exemplar, so a suite
built from corpus data alone is silent about three quarters of a column
the schema advertises. Everything here is synthetic for that reason.

WHAT WAS MEASURED, AND IT CORRECTS THE CENSUS. The shape inventory says
the CRLF path has "no obvious implementation" because ``split_lines`` and
``join_lines`` handle only ``"\\n"``. Measured end to end, all four values
round-trip BYTE-EXACT today, and the mechanism is worth understanding
before anyone tries to improve it:

  The production reader (``scripts/message_model_corpus_run.read_file_lines``)
  splits raw bytes on ``b"\\n"`` and does NOT strip a trailing ``\\r``. So on
  a CRLF file the carriage return travels INSIDE the line text. That text
  parses (``json.loads`` tolerates trailing whitespace) but no registered
  style reproduces it, ``detect_style`` correctly returns None, and ingest
  keeps the exact bytes in ``raw_line``. Export replays them verbatim and
  joins with ``"\\n"``, which puts the ``\\r`` back in front of it.

So ``join_lines`` is right to know only about ``"\\n"``: it is never asked
to emit a ``\\r``, because the ``\\r`` is not a line terminator as far as
this model is concerned - it is a byte at the end of a line. Adding CRLF
handling to ``split_lines``/``join_lines`` would double-handle it and
break the path that currently works.

THE KNOWN GAP THAT REMAINS, stated rather than left implicit. The
``line_ending`` COLUMN is metadata only. The production reader never
passes the argument, so it takes its default of ``"LF"`` for every
transcript including a genuinely CRLF one. Nothing reads the column for a
decision, so this costs no fidelity - but a future check that trusts it to
describe the source file would be trusting a value nobody sets. The second
cost is storage: a CRLF file stores every line twice over in ``raw_line``.
Both are recorded here rather than fixed, because neither is a fidelity
defect and the fix would touch the most load-bearing function in the
model to repair something that is not broken.

THE SINGLE-EXEMPLAR BRANCH. Appearance 1,392,773 is the only row in
3,125,122 that exercises the ``raw_line`` branch of ``_render_row`` - and
the only NULL ``body_id`` and NULL ``serializer_style``. One deletion from
the archive and that entire branch is untested. The synthetic cases below
cover it independently of the corpus.
"""

from __future__ import annotations

import sqlite3
from typing import List, Tuple

import pytest

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_model_export import (
    VERIFY_MATCH,
    export_transcript,
)
from src.core.message_model_ingest import SourceLine, ingest_lines
from src.core.message_model_serialize import join_lines, split_lines

#: A minimal valid record, as raw bytes, reused across the cases below.
RECORD_A: bytes = b'{"type":"user","uuid":"uuid-le-1"}'
RECORD_B: bytes = b'{"type":"user","uuid":"uuid-le-2"}'

#: The four values the schema permits for ``line_ending``.
ALL_LINE_ENDINGS: Tuple[str, ...] = ("LF", "CRLF", "MIXED", "NONE")


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """An in-memory database at the current schema version.

    Inputs: none (pytest fixture).
    Output: sqlite3.Connection.
    Example: conn.execute("SELECT 1").fetchone() -> (1,)
    """
    connection = sqlite3.connect(":memory:")
    with connection:
        run_chain(connection, 0, CURRENT_SCHEMA_VERSION)
    return connection


def read_like_production(data: bytes) -> Tuple[List[str], bool]:
    """Split raw file bytes into lines the way the production reader does.

    Description: mirrors
      ``scripts/message_model_corpus_run.read_file_lines`` - split on
      ``b"\\n"``, decode each part strictly, and DO NOT strip a trailing
      ``\\r``. Reproduced here rather than imported because the point is to
      pin the behaviour a CRLF file actually meets; importing would make
      this test follow that function wherever it went.
    Inputs: data (bytes) - a whole synthetic file.
    Output: (lines, has_trailing_newline).
    Example: read_like_production(b"a\\n") -> (["a"], True)
    """
    if data == b"":
        return [], False
    parts = data.split(b"\n")
    trailing = parts[-1] == b""
    if trailing:
        parts = parts[:-1]
    return [part.decode("utf-8") for part in parts], trailing


def _round_trip(conn: sqlite3.Connection, data: bytes,
                line_ending: str) -> bytes:
    """Ingest raw bytes and export them again.

    Inputs: conn (sqlite3.Connection), data (bytes - the synthetic file),
      line_ending (str - the metadata value to record).
    Output: bytes - what export produced.
    Raises: AssertionError - export reported any line unverified.
    Example: _round_trip(conn, b"{}\\n", "LF") -> b"{}\\n"
    """
    lines, trailing = read_like_production(data)
    with conn:
        result = ingest_lines(
            conn, source_ref=f"src-{line_ending}-{len(data)}",
            session_ref="session-le",
            lines=[SourceLine(text=text) for text in lines],
            has_trailing_newline=trailing, line_ending=line_ending,
        )
    exported = export_transcript(conn, result.transcript_id)
    assert all(line.outcome == VERIFY_MATCH for line in exported.lines), (
        f"unverified lines for {line_ending}: {exported.failures()}"
    )
    assert exported.verified, f"{line_ending} export reported unverified"
    return exported.text.encode("utf-8")


@pytest.mark.parametrize(
    "label,data,line_ending",
    [
        ("LF_trailing", RECORD_A + b"\n" + RECORD_B + b"\n", "LF"),
        ("LF_no_trailing", RECORD_A + b"\n" + RECORD_B, "LF"),
        ("CRLF_trailing", RECORD_A + b"\r\n" + RECORD_B + b"\r\n", "CRLF"),
        ("CRLF_no_trailing", RECORD_A + b"\r\n" + RECORD_B, "CRLF"),
        ("MIXED", RECORD_A + b"\r\n" + RECORD_B + b"\n", "MIXED"),
        ("NONE", RECORD_A, "NONE"),
        ("blank_line_in_middle", RECORD_A + b"\n\n" + RECORD_B + b"\n", "LF"),
        ("trailing_blank_line", RECORD_A + b"\n\n", "LF"),
    ],
)
def test_every_line_ending_shape_round_trips_byte_exact(
    conn, label: str, data: bytes, line_ending: str,
):
    """All four line_ending values reproduce their source bytes exactly.

    Description: six of these eight cells have NO corpus exemplar, so
      this is their only coverage anywhere. A regression here is a
      fidelity defect, not a metadata one.
    """
    produced = _round_trip(conn, data, line_ending)
    assert produced == data, (
        f"{label}: export did not reproduce the source bytes\n"
        f"  source:   {data!r}\n"
        f"  produced: {produced!r}"
    )


def test_crlf_is_carried_by_the_raw_line_branch_not_by_the_joiner(conn):
    """CRLF survives because the bytes are stored raw, and that is pinned.

    Description: pins the MECHANISM, not only the outcome. If someone
      later teaches ``detect_style`` to ignore trailing whitespace, these
      lines would stop being stored raw and would come back without their
      carriage returns - a fidelity regression that the byte assertion
      above would catch but not explain. This says why.
    """
    data = RECORD_A + b"\r\n" + RECORD_B + b"\r\n"
    lines, trailing = read_like_production(data)
    assert all(text.endswith("\r") for text in lines), (
        "the production reader began stripping carriage returns; CRLF "
        "fidelity now depends on something else entirely"
    )
    with conn:
        result = ingest_lines(
            conn, source_ref="crlf-mechanism", session_ref="session-le",
            lines=[SourceLine(text=text) for text in lines],
            has_trailing_newline=trailing, line_ending="CRLF",
        )
    rows = conn.execute(
        "SELECT raw_line IS NOT NULL, serializer_style, body_id "
        "FROM message_appearances WHERE transcript_id = ? ORDER BY line_no",
        (result.transcript_id,),
    ).fetchall()
    assert all(row[0] == 1 for row in rows), (
        "a CRLF line was NOT stored raw, so its carriage return is being "
        "reconstructed by something rather than replayed"
    )
    assert all(row[1] is None for row in rows), (
        "a CRLF line claims a serializer style, but no registered style "
        "can reproduce a trailing carriage return"
    )


def test_the_raw_line_branch_is_covered_without_the_corpus(conn):
    """An unparseable line is kept verbatim and replayed verbatim.

    Description: the corpus holds exactly ONE row exercising this branch
      (appearance 1,392,773). This is its synthetic replacement, so the
      branch stays covered if that row is ever deleted or the archive is
      rebuilt.
    """
    broken = b"not json at all"
    data = RECORD_A + b"\n" + broken + b"\n" + RECORD_B + b"\n"
    produced = _round_trip(conn, data, "LF")
    assert produced == data
    row = conn.execute(
        "SELECT a.raw_line, a.serializer_style, a.body_id, a.line_status "
        "FROM message_appearances a WHERE a.line_no = 1"
    ).fetchone()
    assert row[0] == broken.decode("utf-8"), "the raw bytes were not kept"
    assert row[1] is None, "an unparseable line claims a serializer style"
    assert row[2] is None, "an unparseable line has a body row"
    assert row[3] == "invalid_json"


def test_split_and_join_lines_are_exact_inverses():
    """The line splitter and joiner invert each other on every shape.

    Description: pins the documented LF-only contract of both functions
      directly, including the cases where a blank line is content rather
      than a terminator.
    """
    for text in ("", "a\n", "a", "a\nb\n", "a\nb", "a\n\nb\n", "a\n\n",
                 "\n", "\n\n", "a\r\nb\r\n"):
        lines, trailing = split_lines(text)
        assert join_lines(lines, trailing) == text, (
            f"split/join is not an inverse for {text!r}"
        )


def test_line_ending_is_metadata_and_does_not_steer_export(conn):
    """A wrong line_ending value cannot corrupt the exported bytes.

    Description: pins the KNOWN GAP named in this module's docstring. The
      production reader never sets this column, so it is frequently
      "LF" on files that are not. That is only harmless as long as no
      export decision reads it - which this proves by recording a
      deliberately wrong value and requiring the bytes to be unaffected.
    """
    data = RECORD_A + b"\r\n" + RECORD_B + b"\r\n"
    lines, trailing = read_like_production(data)
    with conn:
        result = ingest_lines(
            conn, source_ref="mislabelled", session_ref="session-le",
            lines=[SourceLine(text=text) for text in lines],
            has_trailing_newline=trailing,
            line_ending="LF",  # deliberately wrong for this file
        )
    exported = export_transcript(conn, result.transcript_id)
    assert exported.text.encode("utf-8") == data, (
        "a mislabelled line_ending changed the exported bytes, so the "
        "column is NOT inert and the production reader's failure to set "
        "it is a live fidelity bug rather than a documentation gap"
    )
    stored = conn.execute(
        "SELECT line_ending FROM message_transcripts WHERE id = ?",
        (result.transcript_id,),
    ).fetchone()[0]
    assert stored == "LF", "the wrong value was not even stored as given"


def test_all_permitted_line_ending_values_are_accepted_by_the_schema(conn):
    """Every value the CHECK constraint permits can actually be stored.

    Description: three of the four have no corpus row, so without this
      nothing anywhere proves the schema accepts them.
    """
    for index, ending in enumerate(ALL_LINE_ENDINGS):
        with conn:
            ingest_lines(
                conn, source_ref=f"accepts-{ending}", session_ref="s-accept",
                lines=[SourceLine(text=RECORD_A.decode("utf-8"))],
                has_trailing_newline=bool(index % 2), line_ending=ending,
            )
    stored = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT line_ending FROM message_transcripts"
        )
    }
    assert stored == set(ALL_LINE_ENDINGS)
