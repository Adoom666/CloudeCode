"""Keyset paging visits every row exactly once, ties included.

A pager has three ways to be wrong and only one of them is loud. It can
DUPLICATE (the user sees the same row twice and complains), it can SKIP
(the user never sees a row and never knows), and it can RESTART silently
when handed a bad cursor, which produces an infinite loop that looks
exactly like it is working. The skip and the silent restart are the two
that ship.

So the walks here assert the concatenated ids against the full ordered
list by EQUALITY - not a length, not a set, an ordered list - because a
length check passes when one row is duplicated and another dropped, and a
set check passes when the order is wrong.

``ingested_at`` TIES ARE NOT HYPOTHETICAL. All 21,039 transcripts in the
real corpus were ingested in a few batch runs, so the timestamps repeat
at microsecond resolution and the ``id DESC`` tie-break carries real
weight. Every walk below runs over a fixture with real ties in it.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import List, Optional

import pytest

os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_archpage_logs_"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.archive_cursor import (
    CURSOR_LINES,
    CURSOR_PROJECTS,
    CURSOR_TRANSCRIPTS,
    CursorError,
    decode_cursor,
    encode_cursor,
)
from src.core.archive_hierarchy import transcripts_for_project
from src.core.archive_read import (
    RESULT_CANNOT_DETERMINE,
    RESULT_OK,
    http_status_for,
    open_read_only,
)
from tests.archive_fixture import (
    make_state_dir,
    seed_corpus,
    seed_host,
    seed_project,
    seed_transcript,
    writable,
)

#: Nine transcripts across three timestamps, so every timestamp carries a
#: three-way tie that ``id DESC`` alone resolves.
TIMESTAMPS = (
    "2026-08-29T22:17:03.000001Z",
    "2026-08-29T22:17:03.000002Z",
    "2026-08-29T22:17:03.000003Z",
)
ROWS_PER_TIMESTAMP = 3
TOTAL_ROWS = len(TIMESTAMPS) * ROWS_PER_TIMESTAMP


@pytest.fixture()
def tied_archive(tmp_path: Path) -> Path:
    """Nine transcripts in one project, three per identical ingested_at.

    Inputs: tmp_path (Path).
    Output: Path - the state directory.
    """
    state_dir = make_state_dir(tmp_path, "tied")
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id)
            project_id = seed_project(conn, corpus_id, slug="-p")
            for stamp in TIMESTAMPS:
                for n in range(ROWS_PER_TIMESTAMP):
                    seed_transcript(
                        conn,
                        host_id=host_id,
                        corpus_id=corpus_id,
                        project_id=project_id,
                        source_path=f"{stamp}-{n}.jsonl",
                        ingested_at=stamp,
                    )
    return state_dir


def expected_order(state_dir: Path) -> List[int]:
    """Read the full ordered id list the pager must reproduce.

    Description: one direct query, unpaged, as the reference. Comparing a
      walk against itself would prove nothing.
    Inputs: state_dir (Path).
    Output: list of transcript ids in (ingested_at DESC, id DESC) order.
    Example: expected_order(sd) -> [9, 8, 7, 6, 5, 4, 3, 2, 1]
    """
    with closing(open_read_only(state_dir)) as conn:
        return [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM message_transcripts "
                "ORDER BY ingested_at DESC, id DESC"
            ).fetchall()
        ]


def walk(state_dir: Path, limit: int, *, project_id: int = 1) -> List[int]:
    """Page the whole project with the given limit and collect ids in order.

    Description: follows ``next_cursor`` until ``has_more`` is false, and
      opens a FRESH connection per page so the walk cannot accidentally
      depend on one long-lived read snapshot - a real client does not
      have one.
    Inputs: state_dir (Path), limit (int), project_id (int).
    Output: list of transcript ids in the order they were returned.
    Example: walk(sd, 3) -> [9, 8, 7, 6, 5, 4, 3, 2, 1]
    """
    seen: List[int] = []
    cursor: Optional[str] = None
    for _ in range(TOTAL_ROWS + 10):  # a hard stop, so a looping pager fails
        with closing(open_read_only(state_dir)) as conn:
            page = transcripts_for_project(
                conn, project_id, limit=limit, cursor=cursor
            )
        assert page["result_status"] == RESULT_OK
        seen.extend(row["transcript_id"] for row in page["result"])
        if not page["meta"]["paging"]["has_more"]:
            assert page["meta"]["paging"]["next_cursor"] is None
            return seen
        cursor = page["meta"]["paging"]["next_cursor"]
        assert cursor is not None, "has_more is true but no cursor was minted"
    raise AssertionError("pager did not terminate; it is looping or restarting")


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 8, 9, 10, 200])
def test_walk_visits_every_row_exactly_once(tied_archive: Path, limit: int) -> None:
    """Ordered equality against the reference list, at every page size."""
    expected = expected_order(tied_archive)
    assert len(expected) == TOTAL_ROWS
    walked = walk(tied_archive, limit)
    assert walked == expected
    assert len(walked) == len(set(walked)), "a row was returned twice"


def test_the_fixture_really_contains_ties(tied_archive: Path) -> None:
    """Positive control: a walk over unique timestamps proves nothing here."""
    with closing(open_read_only(tied_archive)) as conn:
        rows = conn.execute(
            "SELECT ingested_at, COUNT(*) AS n FROM message_transcripts "
            "GROUP BY ingested_at HAVING n > 1"
        ).fetchall()
    assert rows, "fixture has no ingested_at ties, so the tie-break is untested"
    assert all(row["n"] == ROWS_PER_TIMESTAMP for row in rows)


@pytest.mark.parametrize("limit", [3, 9])
def test_has_more_is_false_on_an_exactly_full_final_page(
    tied_archive: Path, limit: int
) -> None:
    """The classic off-by-one: 9 rows at limit 3 and at limit 9.

    Description: with ``returned == limit`` as the test, the last page
      would claim has_more and the client would render a "load more" that
      fetches nothing. Fetching limit+1 is what makes this honest.
    """
    cursor: Optional[str] = None
    pages = 0
    while True:
        with closing(open_read_only(tied_archive)) as conn:
            page = transcripts_for_project(conn, 1, limit=limit, cursor=cursor)
        pages += 1
        if not page["meta"]["paging"]["has_more"]:
            break
        cursor = page["meta"]["paging"]["next_cursor"]
    assert pages == TOTAL_ROWS // limit
    assert page["meta"]["paging"]["returned"] == limit
    assert page["meta"]["paging"]["has_more"] is False
    assert page["meta"]["paging"]["next_cursor"] is None


def test_a_row_inserted_mid_walk_shifts_nothing(tied_archive: Path) -> None:
    """A concurrent insert must not shift or hide any later page.

    Description: this is the defect OFFSET paging has and keyset does
      not. The new row carries a later ``ingested_at``, so it sorts ahead
      of the cursor's position and is simply not part of this walk. What
      matters is that every ORIGINAL row still arrives, exactly once, in
      the original order.
    """
    expected = expected_order(tied_archive)
    seen: List[int] = []
    cursor: Optional[str] = None
    inserted = False
    while True:
        with closing(open_read_only(tied_archive)) as conn:
            page = transcripts_for_project(conn, 1, limit=2, cursor=cursor)
        seen.extend(row["transcript_id"] for row in page["result"])
        if not inserted:
            with closing(writable(tied_archive)) as writer:
                with writer:
                    seed_transcript(
                        writer,
                        host_id=1,
                        corpus_id=1,
                        project_id=1,
                        source_path="inserted-mid-walk.jsonl",
                        ingested_at="2026-08-30T00:00:00.000000Z",
                    )
            inserted = True
        if not page["meta"]["paging"]["has_more"]:
            break
        cursor = page["meta"]["paging"]["next_cursor"]
    assert seen == expected, "a concurrent insert shifted or hid a page"


# --- malformed cursors: a 400, never a silent restart ----------------------


def _raw(payload: dict) -> str:
    """Encode a payload WITHOUT going through encode_cursor's validation.

    Description: encode_cursor refuses to mint a cursor its own decoder
      would reject, which is correct and makes it useless for building
      the defective inputs this file needs. This bypasses it so the
      malformed cases are genuinely malformed rather than merely odd.
    Inputs: payload (dict) - any JSON-serializable mapping.
    Output: str - unpadded base64url, exactly the wire format.
    Example: _raw({"v": 9}) -> 'eyJ2Ijo5fQ'
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


MALFORMED = [
    ("empty string", ""),
    ("not base64", "!!!!not-base64!!!!"),
    ("base64 of junk", "bm90IGpzb24"),  # "not json"
    ("base64 of a JSON array", "WzEsMiwzXQ"),  # "[1,2,3]"
    ("wrong version", _raw({"ingested_at": "x", "id": 1, "v": 9})),
    ("missing key", encode_cursor(CURSOR_LINES, {"line_no": 1})),
    ("unknown key", _raw({"ingested_at": "x", "id": 1, "nope": 1, "v": 1})),
    ("boolean position", _raw({"ingested_at": "x", "id": True, "v": 1})),
    ("string id", _raw({"ingested_at": "x", "id": "1", "v": 1})),
]


@pytest.mark.parametrize("label,raw", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_decode_cursor_raises_on_every_defect(label: str, raw: str) -> None:
    """Every malformed shape raises CursorError rather than returning a dict."""
    with pytest.raises(CursorError):
        decode_cursor(CURSOR_TRANSCRIPTS, raw)


@pytest.mark.parametrize("label,raw", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_a_bad_cursor_is_400_cannot_determine_and_not_page_one(
    tied_archive: Path, label: str, raw: str
) -> None:
    """The refusal is asserted AGAINST what page 1 would have returned.

    Description: the dangerous failure is not an error, it is a cheerful
      first page. So this compares the response to a real page-1 response
      and asserts they differ, rather than only checking a status string.
    """
    with closing(open_read_only(tied_archive)) as conn:
        page_one = transcripts_for_project(conn, 1, limit=3)
        bad = transcripts_for_project(conn, 1, limit=3, cursor=raw)

    assert page_one["result_status"] == RESULT_OK and page_one["result"]
    assert bad["result_status"] == RESULT_CANNOT_DETERMINE
    assert bad["result"] != page_one["result"], "a bad cursor silently restarted"
    assert bad["result"] in (None, [])
    assert bad["unevaluated"][0]["subject"] == "cursor"
    assert bad["meta"]["paging"]["has_more"] is None
    assert http_status_for(bad["result_status"], cursor_error=True) == 400


def test_cursor_round_trips_and_is_stable(tied_archive: Path) -> None:
    """The same position always encodes to the same opaque string."""
    payload = {"ingested_at": TIMESTAMPS[0], "id": 4}
    first = encode_cursor(CURSOR_TRANSCRIPTS, payload)
    assert first == encode_cursor(CURSOR_TRANSCRIPTS, dict(payload))
    assert decode_cursor(CURSOR_TRANSCRIPTS, first) == {**payload, "v": 1}


def test_a_cursor_for_one_endpoint_is_refused_by_another() -> None:
    """A lines cursor replayed against transcripts must not page from nowhere."""
    lines_cursor = encode_cursor(CURSOR_LINES, {"line_no": 15000})
    with pytest.raises(CursorError):
        decode_cursor(CURSOR_TRANSCRIPTS, lines_cursor)
    with pytest.raises(CursorError):
        decode_cursor(CURSOR_PROJECTS, lines_cursor)


def test_a_boolean_is_not_an_integer_position() -> None:
    """bool is a subclass of int in Python; a cursor must not accept one.

    Description: without an explicit guard, ``isinstance(True, int)`` is
      True and ``{"line_no": true}`` pages from line 1 while looking
      entirely valid. The positive control alongside proves the same
      cursor shape with a real integer IS accepted, so this is not
      passing because the whole kind is broken.
    """
    with pytest.raises(CursorError):
        decode_cursor(CURSOR_LINES, _raw({"line_no": True, "v": 1}))
    assert decode_cursor(CURSOR_LINES, _raw({"line_no": 1, "v": 1}))["line_no"] == 1
