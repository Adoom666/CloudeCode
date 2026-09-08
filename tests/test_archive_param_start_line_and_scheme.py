"""``start_line`` and ``session_ref_scheme``: three outcomes each, and the
keyset guarantee held under both.

WHY THESE TWO PARAMETERS SHARE A FILE. They are the same defect class
fixed in two places. Before them, a client that wanted line 7,111 of a
30,805-line transcript, or the 77 conversations inside a 3,416-transcript
project, could ONLY get there by doing something dishonest - synthesising
an opaque cursor by hand, or filtering the page it happened to have
fetched and hoping nobody read that as a filter of the project. Both
parameters exist to replace a client-side guess with a server-side
measurement, and both therefore have to prove the SAME thing: that the new
answer says which of the three outcomes it is.

WHAT THESE TESTS REFUSE TO ACCEPT AS A PASS. An empty list. Every
assertion below that touches an empty result also asserts the
``result_status`` and the named subject that goes with it, because
``("ok", [])``, ``("not_found", [])`` and ``("cannot_determine", None)``
render identically to a client reading only ``result`` and mean three
different things. A test that asserted ``len(result) == 0`` would pass on
all three and could not detect the defect it was written for.

THE COUNTS ARE ASSERTED AS LABELLED, NOT JUST AS CORRECT. A filtered count
that arrives without ``counts_are`` is a number somebody will render as a
corpus total, so the label is part of the contract and is asserted as
such.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_archparam_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_archparam_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.api.archive_routes import router as archive_router
from src.api.archive_support import is_client_error
from src.api.auth import require_auth
from src.core.archive_cursor import CURSOR_LINES, encode_cursor
from src.core.archive_hierarchy import transcripts_for_project
from src.core.archive_lines import transcript_lines
from src.core.archive_read import (
    RESULT_CANNOT_DETERMINE,
    RESULT_NOT_FOUND,
    RESULT_OK,
    http_status_for,
    open_read_only,
)
from src.core.archive_start_line import (
    START_LINE_SUBJECT,
    STATE_CONFLICTS_WITH_CURSOR,
    STATE_IN_RANGE,
    STATE_NEGATIVE,
    STATE_NOT_REQUESTED,
    STATE_NO_LINES,
    STATE_PAST_LAST_LINE,
    keyset_bound,
)
from src.core.archive_transcript_page import (
    SCHEME_COUNTS_ARE,
    SCHEME_SUBJECT,
)
from tests.archive_fixture import (
    make_state_dir,
    seed_appearance,
    seed_body,
    seed_corpus,
    seed_host,
    seed_project,
    seed_transcript,
    writable,
)

#: Lines seeded into the paging transcript. Larger than any page size used
#: below so a walk has to take several pages to finish.
SEEDED_LINE_COUNT = 25

#: The 0-based line the deep-link tests aim at. Deliberately NOT 0 and not
#: the last line: an off-by-one that lands on either end is invisible.
TARGET_LINE = 7

#: A line number no seeded transcript reaches, for the out-of-range case.
OUT_OF_RANGE_LINE = 9999

#: A scheme value the schema's CHECK constraint cannot admit, so it is
#: provably absent rather than merely absent from today's fixture.
UNKNOWN_SCHEME = "convo"


@pytest.fixture()
def lines_archive(tmp_path: Path) -> Path:
    """Seed one transcript with SEEDED_LINE_COUNT lines numbered from 0.

    Description: ``line_no`` starts at 0 because the real corpus does -
      measured on transcript 5767, MIN 0 and MAX 30804 over 30805 rows.
      A fixture numbered from 1 would let an off-by-one pass here and
      fail in production.
    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: Path - the state directory.
    Example: with closing(writable(lines_archive)) as conn: ...
    """
    state_dir = make_state_dir(tmp_path)
    with closing(writable(state_dir)) as conn:
        with conn:
            host = seed_host(conn)
            corpus = seed_corpus(conn, host_id=host)
            project = seed_project(conn, corpus, slug="proj")
            transcript = seed_transcript(
                conn, host_id=host, corpus_id=corpus, project_id=project,
                source_path="lines.jsonl", line_count=SEEDED_LINE_COUNT,
            )
            for line_no in range(SEEDED_LINE_COUNT):
                body = seed_body(
                    conn, body_json=f'{{"n":{line_no}}}',
                    identity_key=f'line-{line_no}',
                )
                seed_appearance(
                    conn, transcript_id=transcript, line_no=line_no, body_id=body
                )
    return state_dir


@pytest.fixture()
def empty_lines_archive(tmp_path: Path) -> Path:
    """Seed a transcript that exists and has NO appearance rows at all.

    Description: the state ``start_line`` must not report as out of range,
      because there is no range to be outside of.
    Inputs: tmp_path (Path). Output: Path - the state directory.
    """
    state_dir = make_state_dir(tmp_path, name="empty_state")
    with closing(writable(state_dir)) as conn:
        with conn:
            host = seed_host(conn)
            corpus = seed_corpus(conn, host_id=host)
            project = seed_project(conn, corpus, slug="proj")
            seed_transcript(
                conn, host_id=host, corpus_id=corpus, project_id=project,
                source_path="empty.jsonl", line_count=0,
            )
    return state_dir


@pytest.fixture()
def scheme_archive(tmp_path: Path) -> Path:
    """Seed one project holding both schemes, in a known interleaved order.

    Description: the schemes ALTERNATE and every row shares one
      ``ingested_at``, so the filter has to work through the ``id DESC``
      tie-break rather than over a conveniently contiguous block. A
      fixture with all the uuid rows first would pass even if the filter
      were applied after the page limit instead of inside the query.
    Inputs: tmp_path (Path). Output: Path - the state directory.
    """
    state_dir = make_state_dir(tmp_path, name="scheme_state")
    with closing(writable(state_dir)) as conn:
        with conn:
            host = seed_host(conn)
            corpus = seed_corpus(conn, host_id=host)
            project = seed_project(conn, corpus, slug="proj")
            for index in range(12):
                seed_transcript(
                    conn, host_id=host, corpus_id=corpus, project_id=project,
                    source_path=f"t{index}.jsonl",
                    session_ref_scheme="uuid" if index % 2 == 0 else "agent",
                )
    return state_dir


def _ids(result: List[Dict[str, Any]]) -> List[int]:
    """Pull transcript ids out of a listing page, in order.

    Inputs: result (list of dict). Output: list of int.
    Example: _ids(page["result"]) -> [12, 10, 8]
    """
    return [row["transcript_id"] for row in result]


def _walk(state_dir: Path, *, scheme: Optional[str], limit: int) -> List[int]:
    """Page a project to exhaustion and return every id in visit order.

    Description: returns the CONCATENATION, not a set, so a duplicate and
      a skip are both visible. Refuses to loop forever: a walk that has
      not finished after more pages than there are rows is a paging bug,
      and hanging the suite would hide it as a timeout.
    Inputs: state_dir (Path), scheme (str|None), limit (int).
    Output: list of int - ids in the order they were visited.
    Example: _walk(sd, scheme="uuid", limit=2)
    """
    seen: List[int] = []
    cursor: Optional[str] = None
    for _ in range(100):
        with open_read_only(state_dir) as conn:
            page = transcripts_for_project(
                conn, 1, limit=limit, cursor=cursor, session_ref_scheme=scheme
            )
        assert page["result_status"] == RESULT_OK
        seen.extend(_ids(page["result"]))
        cursor = page["meta"]["paging"]["next_cursor"]
        if page["meta"]["paging"]["has_more"] is not True:
            return seen
    raise AssertionError("walk did not terminate; the pager is not advancing")


# --- start_line: the happy path is an EXACT landing, not an approximate one


def test_start_line_lands_exactly_on_that_line(lines_archive: Path) -> None:
    """start_line=N makes line N the first row, 0-based."""
    with open_read_only(lines_archive) as conn:
        page = transcript_lines(conn, 1, limit=3, start_line=TARGET_LINE)
    assert page["result_status"] == RESULT_OK
    assert [row["line_no"] for row in page["result"]] == [
        TARGET_LINE, TARGET_LINE + 1, TARGET_LINE + 2
    ]
    assert page["meta"]["start_line"]["state"] == STATE_IN_RANGE
    assert page["meta"]["start_line"]["applied"] is True
    assert page["meta"]["start_line"]["requested"] == TARGET_LINE
    assert page["meta"]["start_line"]["max_line_no"] == SEEDED_LINE_COUNT - 1


def test_start_line_zero_returns_the_very_first_line(lines_archive: Path) -> None:
    """0 is a real value, not a synonym for "unset"."""
    with open_read_only(lines_archive) as conn:
        page = transcript_lines(conn, 1, limit=2, start_line=0)
    assert [row["line_no"] for row in page["result"]] == [0, 1]
    assert page["meta"]["start_line"]["applied"] is True
    assert keyset_bound(0) == -1


def test_meta_start_line_is_present_when_none_was_asked_for(
    lines_archive: Path,
) -> None:
    """"I did not ask" must be distinguishable from "this build cannot"."""
    with open_read_only(lines_archive) as conn:
        page = transcript_lines(conn, 1, limit=1)
    assert page["meta"]["start_line"]["state"] == STATE_NOT_REQUESTED
    assert page["meta"]["start_line"]["applied"] is False
    assert page["meta"]["start_line"]["requested"] is None


# --- start_line: the two refusals and the one measured absence -------------


def test_start_line_past_the_end_is_not_found_naming_the_last_line(
    lines_archive: Path,
) -> None:
    """Out of range is a NAMED outcome, never an empty page."""
    with open_read_only(lines_archive) as conn:
        page = transcript_lines(conn, 1, limit=5, start_line=OUT_OF_RANGE_LINE)
    assert page["result_status"] == RESULT_NOT_FOUND
    assert page["meta"]["start_line"]["state"] == STATE_PAST_LAST_LINE
    assert page["meta"]["start_line"]["max_line_no"] == SEEDED_LINE_COUNT - 1
    assert len(page["unevaluated"]) == 1
    reason = page["unevaluated"][0]["reason"]
    assert str(SEEDED_LINE_COUNT - 1) in reason
    assert http_status_for(page["result_status"]) == 404
    # The distinguishing assertion: a walk that simply ran off the end
    # returns ok. This must not.
    with open_read_only(lines_archive) as conn:
        real_end = transcript_lines(
            conn, 1, limit=5,
            cursor=encode_cursor(CURSOR_LINES, {"line_no": SEEDED_LINE_COUNT - 1}),
        )
    assert real_end["result_status"] == RESULT_OK
    assert real_end["result"] == []


def test_start_line_with_a_cursor_is_refused_by_name_as_a_400(
    lines_archive: Path,
) -> None:
    """Two absolute positions in one request is a client error, not a guess."""
    cursor = encode_cursor(CURSOR_LINES, {"line_no": 2})
    with open_read_only(lines_archive) as conn:
        page = transcript_lines(conn, 1, limit=5, start_line=TARGET_LINE, cursor=cursor)
    assert page["result_status"] == RESULT_CANNOT_DETERMINE
    assert page["result"] is None
    assert page["meta"]["start_line"]["state"] == STATE_CONFLICTS_WITH_CURSOR
    assert page["unevaluated"][0]["subject"] == START_LINE_SUBJECT
    assert is_client_error(page) is True
    assert http_status_for(page["result_status"], cursor_error=True) == 400
    # And NEITHER position was quietly applied: no rows were read at all.
    assert page["meta"]["paging"]["returned"] == 0
    assert page["meta"]["paging"]["has_more"] is None


def test_a_negative_start_line_is_a_named_cannot_determine(
    lines_archive: Path,
) -> None:
    """Below zero is refused with the reason, not clamped to zero."""
    with open_read_only(lines_archive) as conn:
        page = transcript_lines(conn, 1, limit=5, start_line=-1)
    assert page["result_status"] == RESULT_CANNOT_DETERMINE
    assert page["result"] is None
    assert page["meta"]["start_line"]["state"] == STATE_NEGATIVE
    assert is_client_error(page) is True


def test_a_transcript_with_no_lines_is_a_genuine_empty_ok(
    empty_lines_archive: Path,
) -> None:
    """No rows at all is not "out of range" - there is no range."""
    with open_read_only(empty_lines_archive) as conn:
        page = transcript_lines(conn, 1, limit=5, start_line=3)
    assert page["result_status"] == RESULT_OK
    assert page["result"] == []
    assert page["meta"]["start_line"]["state"] == STATE_NO_LINES
    assert page["meta"]["paging"]["has_more"] is False
    assert "no line rows" in page["meta"]["note"]


def test_start_line_still_leaves_a_malformed_cursor_a_400(
    lines_archive: Path,
) -> None:
    """The new parameter did not open a path around the cursor refusal."""
    with open_read_only(lines_archive) as conn:
        page = transcript_lines(conn, 1, limit=5, cursor="@@@not-base64@@@")
    assert page["result_status"] == RESULT_CANNOT_DETERMINE
    assert page["result"] is None
    assert page["unevaluated"][0]["subject"] == "cursor"


def test_a_walk_opened_with_start_line_visits_every_later_row_once(
    lines_archive: Path,
) -> None:
    """The keyset guarantee holds when the walk STARTS at start_line."""
    seen: List[int] = []
    cursor: Optional[str] = None
    first = True
    for _ in range(100):
        with open_read_only(lines_archive) as conn:
            page = transcript_lines(
                conn, 1, limit=4,
                start_line=TARGET_LINE if first else None,
                cursor=cursor,
            )
        assert page["result_status"] == RESULT_OK
        seen.extend(row["line_no"] for row in page["result"])
        cursor = page["meta"]["paging"]["next_cursor"]
        first = False
        if page["meta"]["paging"]["has_more"] is not True:
            break
    else:
        raise AssertionError("line walk did not terminate")
    assert seen == list(range(TARGET_LINE, SEEDED_LINE_COUNT))


# --- session_ref_scheme: filters, and says what it filtered on -------------


@pytest.mark.parametrize("scheme,expected", [("uuid", 6), ("agent", 6)])
def test_the_scheme_filter_returns_only_that_scheme(
    scheme_archive: Path, scheme: str, expected: int
) -> None:
    """Every returned row carries the requested scheme, and the count is right."""
    with open_read_only(scheme_archive) as conn:
        page = transcripts_for_project(
            conn, 1, limit=50, session_ref_scheme=scheme
        )
    assert page["result_status"] == RESULT_OK
    assert len(page["result"]) == expected
    assert {row["session_ref_scheme"] for row in page["result"]} == {scheme}


def test_the_filtered_counts_sum_to_the_unfiltered_count(
    scheme_archive: Path,
) -> None:
    """uuid + agent == the whole scope. A filter that loses rows is caught here."""
    with open_read_only(scheme_archive) as conn:
        every = transcripts_for_project(conn, 1, limit=50)
        uuids = transcripts_for_project(conn, 1, limit=50, session_ref_scheme="uuid")
        agents = transcripts_for_project(conn, 1, limit=50, session_ref_scheme="agent")
    total = every["meta"]["filters"]["scope_total_before_filter"]
    assert total == len(every["result"])
    assert (uuids["meta"]["filters"]["matched_in_scope"]
            + agents["meta"]["filters"]["matched_in_scope"]) == total


def test_filtered_counts_are_labelled_as_scoped_and_not_as_totals(
    scheme_archive: Path,
) -> None:
    """The label is part of the contract, not documentation."""
    with open_read_only(scheme_archive) as conn:
        page = transcripts_for_project(conn, 1, limit=2, session_ref_scheme="uuid")
    filters = page["meta"]["filters"]
    assert filters["counts_are"] == SCHEME_COUNTS_ARE
    assert "scope" in filters["counts_are"]
    assert filters["applied"] is True
    # And it must not overclaim what the column proves.
    assert "does NOT" in filters["session_ref_scheme_means"]


def test_the_filters_block_is_present_when_no_filter_was_asked_for(
    scheme_archive: Path,
) -> None:
    """"unfiltered" must be distinguishable from "this build has no filter"."""
    with open_read_only(scheme_archive) as conn:
        page = transcripts_for_project(conn, 1, limit=2)
    filters = page["meta"]["filters"]
    assert filters["applied"] is False
    assert filters["session_ref_scheme"] is None
    assert filters["matched_in_scope"] is None


def test_an_unknown_scheme_is_a_named_cannot_determine_not_an_empty_ok(
    scheme_archive: Path,
) -> None:
    """"there is no such scheme" and "no row has it" are different findings."""
    with open_read_only(scheme_archive) as conn:
        page = transcripts_for_project(
            conn, 1, limit=50, session_ref_scheme=UNKNOWN_SCHEME
        )
    assert page["result_status"] == RESULT_CANNOT_DETERMINE
    assert page["result"] is None
    assert page["unevaluated"][0]["subject"] == SCHEME_SUBJECT
    reason = page["unevaluated"][0]["reason"]
    assert UNKNOWN_SCHEME in reason
    assert "uuid" in reason and "agent" in reason
    assert is_client_error(page) is True
    assert http_status_for(page["result_status"], cursor_error=True) == 400


def test_a_scheme_absent_from_THIS_project_is_an_empty_ok(tmp_path: Path) -> None:
    """The mirror case. Present in the archive, absent from the scope."""
    state_dir = make_state_dir(tmp_path, name="split_state")
    with closing(writable(state_dir)) as conn:
        with conn:
            host = seed_host(conn)
            corpus = seed_corpus(conn, host_id=host)
            uuid_project = seed_project(conn, corpus, slug="uuids")
            agent_project = seed_project(conn, corpus, slug="agents")
            seed_transcript(
                conn, host_id=host, corpus_id=corpus, project_id=uuid_project,
                source_path="u.jsonl", session_ref_scheme="uuid",
            )
            seed_transcript(
                conn, host_id=host, corpus_id=corpus, project_id=agent_project,
                source_path="a.jsonl", session_ref_scheme="agent",
            )
    with open_read_only(state_dir) as conn:
        page = transcripts_for_project(conn, 1, limit=50, session_ref_scheme="agent")
    assert page["result_status"] == RESULT_OK
    assert page["result"] == []
    assert page["meta"]["filters"]["matched_in_scope"] == 0
    assert page["meta"]["filters"]["applied"] is True


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 50])
def test_a_filtered_walk_visits_every_matching_row_exactly_once(
    scheme_archive: Path, limit: int
) -> None:
    """Keyset correctness UNDER the filter, at every page size including ties.

    An ordered-list equality, not a set and not a length: a length check
    passes when one row is duplicated and another skipped.
    """
    with open_read_only(scheme_archive) as conn:
        whole = transcripts_for_project(conn, 1, limit=200, session_ref_scheme="uuid")
    expected = _ids(whole["result"])
    assert len(expected) == 6
    assert _walk(scheme_archive, scheme="uuid", limit=limit) == expected


def test_a_filtered_walk_and_an_unfiltered_walk_agree_on_the_subset(
    scheme_archive: Path,
) -> None:
    """The filter narrows the same ordering; it does not reorder or re-page."""
    unfiltered = _walk(scheme_archive, scheme=None, limit=3)
    filtered = _walk(scheme_archive, scheme="uuid", limit=3)
    with open_read_only(scheme_archive) as conn:
        page = transcripts_for_project(conn, 1, limit=200, session_ref_scheme="uuid")
    uuid_ids = set(_ids(page["result"]))
    assert filtered == [i for i in unfiltered if i in uuid_ids]


def test_a_malformed_cursor_is_still_a_400_with_the_filter_applied(
    scheme_archive: Path,
) -> None:
    """The filter did not open a path around the cursor refusal."""
    with open_read_only(scheme_archive) as conn:
        page = transcripts_for_project(
            conn, 1, limit=2, cursor="@@@", session_ref_scheme="uuid"
        )
    assert page["result_status"] == RESULT_CANNOT_DETERMINE
    assert page["result"] is None
    assert page["unevaluated"][0]["subject"] == "cursor"


# --- the same three outcomes, over real HTTP ------------------------------


def _client(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Mount the archive router over one seeded state directory.

    Description: ``require_auth`` is overridden because these tests are
      about parameter semantics, not about the token gate -
      ``test_archive_read_api.py`` owns that and asserts it on every
      route. The state directory is patched on the settings OBJECT rather
      than only in the environment, because the route reads it through
      ``settings.get_state_dir()`` on every call.
    Inputs: state_dir (Path), monkeypatch (pytest.MonkeyPatch).
    Output: TestClient.
    Example: with _client(sd, mp) as c: c.get("/api/v1/archive/hosts")
    """
    from src.config import settings

    monkeypatch.setenv("CLOUDE_STATE_DIR", str(state_dir))
    monkeypatch.setattr(type(settings), "get_state_dir", lambda self: state_dir)
    app = FastAPI()
    app.include_router(archive_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app)


def test_http_start_line_lands_and_out_of_range_is_404(
    lines_archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route carries the parameter through, and the statuses match."""
    with _client(lines_archive, monkeypatch) as client:
        good = client.get(
            f"/api/v1/archive/transcripts/1/lines?limit=2&start_line={TARGET_LINE}"
        )
        gone = client.get(
            f"/api/v1/archive/transcripts/1/lines?start_line={OUT_OF_RANGE_LINE}"
        )
        negative = client.get("/api/v1/archive/transcripts/1/lines?start_line=-1")
    assert good.status_code == 200
    assert [r["line_no"] for r in good.json()["result"]] == [TARGET_LINE, TARGET_LINE + 1]
    assert gone.status_code == 404
    assert gone.json()["result_status"] == RESULT_NOT_FOUND
    assert gone.json()["meta"]["start_line"]["state"] == STATE_PAST_LAST_LINE
    # A FastAPI ``ge=0`` bound would make this a 422 with a validation
    # body that is NOT an envelope. It must stay an envelope.
    assert negative.status_code == 400
    assert negative.json()["result_status"] == RESULT_CANNOT_DETERMINE
    assert negative.json()["unevaluated"][0]["subject"] == START_LINE_SUBJECT


def test_http_start_line_plus_cursor_is_400(
    lines_archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented composition rule, asserted at the wire."""
    cursor = encode_cursor(CURSOR_LINES, {"line_no": 2})
    with _client(lines_archive, monkeypatch) as client:
        both = client.get(
            f"/api/v1/archive/transcripts/1/lines?start_line=5&cursor={cursor}"
        )
    assert both.status_code == 400
    assert both.json()["result"] is None
    assert both.json()["meta"]["start_line"]["state"] == STATE_CONFLICTS_WITH_CURSOR


def test_http_scheme_filter_and_unknown_scheme(
    scheme_archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter narrows over HTTP; an unknown value is a 400, not an empty ok."""
    with _client(scheme_archive, monkeypatch) as client:
        uuids = client.get(
            "/api/v1/archive/projects/1/transcripts?session_ref_scheme=uuid"
        )
        junk = client.get(
            f"/api/v1/archive/projects/1/transcripts?session_ref_scheme={UNKNOWN_SCHEME}"
        )
    assert uuids.status_code == 200
    body = uuids.json()
    assert {r["session_ref_scheme"] for r in body["result"]} == {"uuid"}
    assert body["meta"]["filters"]["counts_are"] == SCHEME_COUNTS_ARE
    assert junk.status_code == 400
    assert junk.json()["result"] is None
    assert junk.json()["unevaluated"][0]["subject"] == SCHEME_SUBJECT
