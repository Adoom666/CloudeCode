"""Title resolution: precedence, absence, and the could-not-determine path.

WHAT THESE TESTS ARE DEFENDING. Measured on the live 21,039-transcript
corpus 2026-09-01, only 497 transcripts (2.36%) carry any title record,
and on the 256 that carry more than one the sources agree ZERO times. So
the precedence order is not a tie-break nicety - on a majority of titled
transcripts it is the only thing deciding what a person reads. And
because a title is the rare case, the two ways of having no title
(nothing was there / the lookup failed) are exactly the pair a lazy
implementation collapses, which would report a broken lookup as 20,542
perfectly ordinary untitled transcripts.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.archive_titles import (
    MAX_TITLE_CHARS,
    TITLE_PRECEDENCE,
    TITLE_SOURCE_CANNOT_DETERMINE,
    _extract,
    _winner,
    resolve_titles,
    titles_meta,
)


def _corpus() -> sqlite3.Connection:
    """Build a miniature corpus with the real column shapes.

    Description: the three tables ``resolve_titles`` touches, with the
      same column names and the same ``record_type_id`` indirection, so
      a test passing here is evidence about the real query.
    Inputs: none. Output: sqlite3.Connection with row_factory set.
    Example: conn = _corpus()
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE message_record_types (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
        CREATE TABLE message_bodies (
            id INTEGER PRIMARY KEY, record_type_id INTEGER, body_json TEXT NOT NULL);
        CREATE TABLE message_appearances (
            id INTEGER PRIMARY KEY, transcript_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL, body_id INTEGER);
        INSERT INTO message_record_types (id, value) VALUES
            (1, 'custom-title'), (2, 'ai-title'), (3, 'summary'),
            (4, 'last-prompt'), (5, 'user');
        """
    )
    return conn


def _add(conn, transcript_id, line_no, record_type_id, body_json):
    """Attach one body to one transcript at one line.

    Inputs: conn, transcript_id (int), line_no (int),
      record_type_id (int), body_json (str). Output: None.
    """
    cur = conn.execute(
        "INSERT INTO message_bodies (record_type_id, body_json) VALUES (?, ?)",
        (record_type_id, body_json),
    )
    conn.execute(
        "INSERT INTO message_appearances (transcript_id, line_no, body_id) "
        "VALUES (?, ?, ?)",
        (transcript_id, line_no, cur.lastrowid),
    )


def test_precedence_order_is_the_documented_one():
    """custom-title outranks ai-title outranks summary outranks last-prompt."""
    assert TITLE_PRECEDENCE == (
        "custom-title", "ai-title", "summary", "last-prompt",
    )


def test_custom_title_beats_every_other_source():
    """The name a human typed outranks all three generated ones."""
    conn = _corpus()
    _add(conn, 1, 10, 4, '{"lastPrompt": "yes"}')
    _add(conn, 1, 11, 3, '{"summary": "A summary"}')
    _add(conn, 1, 12, 2, '{"aiTitle": "An AI title"}')
    _add(conn, 1, 13, 1, '{"customTitle": "The chosen name"}')
    assert resolve_titles(conn, [1])[1] == ("The chosen name", "custom-title")


def test_each_rung_of_the_ladder_wins_when_the_ones_above_are_absent():
    """Every precedence level is reachable, not just the first and last.

    A precedence list is only honest if each entry can actually win; a
    test that checks only the top and bottom would pass with the middle
    two dead.
    """
    conn = _corpus()
    # ai-title wins over summary and last-prompt.
    _add(conn, 1, 1, 4, '{"lastPrompt": "no"}')
    _add(conn, 1, 2, 3, '{"summary": "S"}')
    _add(conn, 1, 3, 2, '{"aiTitle": "AI"}')
    # summary wins over last-prompt.
    _add(conn, 2, 1, 4, '{"lastPrompt": "no"}')
    _add(conn, 2, 2, 3, '{"summary": "S2"}')
    # last-prompt is all there is.
    _add(conn, 3, 1, 4, '{"lastPrompt": "LP"}')
    got = resolve_titles(conn, [1, 2, 3])
    assert got[1] == ("AI", "ai-title")
    assert got[2] == ("S2", "summary")
    assert got[3] == ("LP", "last-prompt")


def test_within_one_source_the_last_line_wins():
    """A retitled session shows its CURRENT name, not its first.

    Measured on the live corpus: a single custom-title transcript carries
    up to ~192 title records, one per rename. Taking any but the highest
    line_no shows a name the owner already replaced.
    """
    conn = _corpus()
    _add(conn, 1, 5, 1, '{"customTitle": "the old name"}')
    _add(conn, 1, 900, 1, '{"customTitle": "the current name"}')
    _add(conn, 1, 40, 1, '{"customTitle": "a middle name"}')
    assert resolve_titles(conn, [1])[1] == ("the current name", "custom-title")


def test_a_tie_between_two_sources_is_broken_by_precedence_not_by_line_order():
    """A LATER summary does not beat an EARLIER custom-title.

    Precedence is between sources; line_no only chooses within one
    source. Letting a late summary win would make the rendered name
    depend on write order, so the owner's chosen title would vanish the
    next time the session was compacted.
    """
    conn = _corpus()
    _add(conn, 1, 10, 1, '{"customTitle": "chosen"}')
    _add(conn, 1, 9999, 3, '{"summary": "generated much later"}')
    assert resolve_titles(conn, [1])[1] == ("chosen", "custom-title")


def test_no_title_is_a_real_answer_with_a_null_source():
    """The ordinary case: 20,542 of 21,039 transcripts look like this."""
    conn = _corpus()
    _add(conn, 1, 1, 5, '{"role": "user"}')  # a non-title record type
    assert resolve_titles(conn, [1])[1] == (None, None)


def test_every_requested_id_is_a_key_even_with_no_rows_at_all():
    """A caller never has to guess what a missing key meant."""
    conn = _corpus()
    got = resolve_titles(conn, [7, 8, 9])
    assert set(got) == {7, 8, 9}
    assert all(v == (None, None) for v in got.values())


def test_a_failed_lookup_says_cannot_determine_and_never_looks_untitled():
    """THE THIRD OUTCOME. A broken lookup must not read as 'no title'.

    This is the whole reason the failure is caught and re-shaped rather
    than raised or swallowed: with 97.6% of transcripts legitimately
    untitled, a failure that returned (None, None) would be invisible.
    """
    conn = _corpus()
    conn.execute("DROP TABLE message_appearances")
    got = resolve_titles(conn, [1, 2])
    assert got[1] == (None, TITLE_SOURCE_CANNOT_DETERMINE)
    assert got[2] == (None, TITLE_SOURCE_CANNOT_DETERMINE)
    # And it is emphatically NOT the same value as a genuine absence.
    assert got[1] != (None, None)


def test_a_malformed_body_is_skipped_and_does_not_fail_the_whole_page():
    """One unparseable record must not turn a page into cannot_determine.

    Measured: 1,131 last-prompt appearances carry no usable field. They
    are simply not candidates - which is different from the LOOKUP
    failing, and must stay different.
    """
    conn = _corpus()
    _add(conn, 1, 1, 1, "{not json at all")
    _add(conn, 1, 2, 3, '{"summary": "still fine"}')
    assert resolve_titles(conn, [1])[1] == ("still fine", "summary")


@pytest.mark.parametrize(
    "body", ['{"customTitle": ""}', '{"customTitle": "   "}',
             '{"customTitle": null}', '{"customTitle": 42}', '[]', 'null'],
)
def test_blank_and_wrongly_typed_titles_are_not_titles(body):
    """A whitespace-only or non-string title is absence, not a title."""
    assert _extract("custom-title", body) is None


def test_a_runaway_title_is_truncated_but_still_has_its_source():
    """A pasted essay as a last-prompt must not bloat every page."""
    conn = _corpus()
    _add(conn, 1, 1, 4, '{"lastPrompt": "%s"}' % ("x" * 5000))
    title, source = resolve_titles(conn, [1])[1]
    assert len(title) == MAX_TITLE_CHARS
    assert source == "last-prompt"


def test_winner_is_pure_and_testable_without_a_database():
    """The precedence contract holds independently of any SQL."""
    assert _winner({}) == (None, None)
    assert _winner({"summary": "S", "custom-title": "C"}) == ("C", "custom-title")
    assert _winner({"last-prompt": "L", "ai-title": "A"}) == ("A", "ai-title")


def test_meta_names_the_weak_source_so_a_ui_can_style_it_differently():
    """last-prompt is not a chosen name and the response has to say so."""
    meta = titles_meta()
    assert meta["precedence"][0] == "custom-title"
    assert meta["weak_source"] == "last-prompt"
    assert meta["cannot_determine_source"] == TITLE_SOURCE_CANNOT_DETERMINE
    assert "last-prompt" in meta["titles_mean"]


def test_non_integer_ids_are_ignored_rather_than_crashing_the_page():
    """A junk id contributes nothing and does not take the page with it."""
    conn = _corpus()
    _add(conn, 1, 1, 1, '{"customTitle": "T"}')
    got = resolve_titles(conn, [1, None, "abc"])
    assert got == {1: ("T", "custom-title")}
