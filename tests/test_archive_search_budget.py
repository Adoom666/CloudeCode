"""Tests for ``src/core/archive_search.py`` - the budgeted scan loop.

THE POINT OF THIS FILE is the pair of zero-hit answers. "I searched the
whole scope and found nothing" and "I ran out of budget before I
finished" are the same empty list to a client that reads only
``result``, and they mean opposite things. Every assertion here is on the
DISCRIMINATING field, never on the emptiness of the list, because an
assertion that only checks ``result == []`` passes for both cases and
therefore cannot detect the defect it exists to catch.

The other three properties asserted:

  * a resume cursor reaches a hit the first request PROVABLY could not
    see. The fixture puts that hit in a transcript the first request's
    budget stopped short of, so the test would fail if resume silently
    restarted at the beginning.
  * ``%``, ``_`` and ``\\`` are literal. Each case includes a decoy row
    that a naive ``LIKE '%' || q || '%'`` implementation WOULD return, so
    the test fails on the wrong implementation rather than passing on
    both.
  * a secret-bearing hit is REPORTED with a withheld snippet, and the
    credential text appears nowhere in the serialized response.

Fixtures are temp databases built through the real migration, never the
live corpus.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_as_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_as_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.archive_cursor import CURSOR_SEARCH, encode_cursor
from src.core.archive_read import open_read_only
from src.core.archive_search import (
    LINE_DONE,
    RESULT_CANNOT_DETERMINE,
    RESULT_NOT_FOUND,
    RESULT_OK,
    RESULT_PARTIAL,
    SCAN_BUDGET_EXHAUSTED,
    SCAN_COMPLETE,
    SCOPE_CANNOT_DETERMINE,
    SCOPE_NOT_FOUND,
    SNIPPET_WITHHELD_SECRET,
    search_scoped,
)
from src.core.db_migration import ensure_db_migrated

#: A fake credential. It is not a real secret and never was, but the test
#: treats it as one so the "never appears in the response" assertion is
#: meaningful.
FAKE_SECRET = "ZZZZfakecredentialnotrealZZZZ0123456789a"

#: Transcript sizes are what the byte budget spends, so they are declared
#: rather than derived: the resume test depends on knowing exactly which
#: transcript the first request can reach.
TRANSCRIPT_BYTES = 4096

#: One literal backslash, named so the escaping in this file's own source
#: cannot be mistaken for the escaping under test.
BACKSLASH = chr(92)


def _insert(conn: sqlite3.Connection, project_id: int, transcripts) -> None:
    """Seed one project and its transcripts, newest first.

    Inputs: conn (writable), project_id (int), transcripts (sequence of
      ``(transcript_id, ingested_at, [(line_no, body_text, secrets), ...])``).
    Output: None.
    """
    conn.execute(
        "INSERT OR IGNORE INTO message_hosts (id, machine_id, "
        "machine_id_scheme, display_name, first_seen_at) VALUES "
        "(1, 'm1', 'declared', 'h', '2026-01-01T00:00:00.000000Z')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO message_corpora (id, host_id, corpus_key, "
        "root_path, collected_at) VALUES "
        "(1, 1, 'c1', '/c', '2026-01-01T00:00:00.000000Z')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO message_projects (id, corpus_id, slug, "
        "first_seen_at) VALUES (?, 1, ?, '2026-01-01T00:00:00.000000Z')",
        (project_id, f"proj-{project_id}"),
    )
    body_id = 1000 * project_id
    for tid, ingested_at, lines in transcripts:
        conn.execute(
            "INSERT INTO message_transcripts (id, source_ref, session_ref, "
            "session_ref_scheme, line_ending, has_trailing_newline, "
            "line_count, content_sha256, raw_byte_length, ingested_at, "
            "host_id, corpus_id, project_id, source_path) VALUES "
            "(?, ?, ?, 'uuid', 'LF', 1, ?, 'sha', ?, ?, 1, 1, ?, ?)",
            (tid, f"ref-{tid}", f"sess-{tid}", len(lines), TRANSCRIPT_BYTES,
             ingested_at, project_id, f"p{project_id}/{tid}.jsonl"),
        )
        for line_no, text, secrets in lines:
            body_id += 1
            conn.execute(
                "INSERT INTO message_bodies (id, identity_key, message_uuid, "
                "body_sha256, body_bytes_sha256, body_json, "
                "secret_finding_count, first_seen_at) VALUES "
                "(?, ?, ?, 'a', 'b', ?, ?, '2026-01-01T00:00:00.000000Z')",
                (body_id, f"k{body_id}", f"u{body_id}", text, secrets),
            )
            conn.execute(
                "INSERT INTO message_appearances (id, transcript_id, line_no, "
                "line_status, body_id, line_sha256, line_byte_length, "
                "fidelity_outcome) VALUES (?, ?, ?, 'ok', ?, 's', ?, "
                "'fidelity_verified')",
                (body_id, tid, line_no, body_id, len(text)),
            )
    conn.commit()


@pytest.fixture()
def corpus(tmp_path) -> Iterator[sqlite3.Connection]:
    """A temp archive: project 1 (three transcripts) and empty project 2.

    Transcript 30 is NEWEST and holds no target term. Transcript 20 holds
    the resume target, and transcript 10 is oldest. Scan order is
    ingested_at DESC, so a one-transcript budget reaches 30 and nothing
    else - which is what makes the resume assertion real rather than
    decorative.
    """
    state = tmp_path / "state"
    ensure_db_migrated(state, 4, "0.8.2")
    with closing(sqlite3.connect(state / "cloude.db")) as write_conn:
        _insert(write_conn, 1, [
            (30, "2026-08-30T00:00:00.000000Z", [
                (0, '{"text":"nothing of interest here"}', 0),
                (1, '{"text":"1000 items and a decoy"}', 0),
                (2, '{"text":"axb decoy for underscore"}', 0),
            ]),
            (20, "2026-08-20T00:00:00.000000Z", [
                (0, '{"text":"the restic beacon lives here"}', 0),
                (1, '{"text":"discount 100% off today"}', 0),
                (2, '{"text":"literal a_b token"}', 0),
                (3, '{"text":"path C:' + BACKSLASH + 'pdata written"}', 0),
                (4, '{"text":"restic near ' + FAKE_SECRET + ' end"}', 2),
            ]),
            (10, "2026-08-10T00:00:00.000000Z", [
                (0, '{"text":"oldest transcript, quiet"}', 0),
            ]),
        ])
        # Project 2 exists and holds nothing. An empty scope is an ``ok``
        # measurement, not a not_found.
        write_conn.execute(
            "INSERT INTO message_projects (id, corpus_id, slug, first_seen_at)"
            " VALUES (2, 1, 'proj-2', '2026-01-01T00:00:00.000000Z')"
        )
        write_conn.commit()
    with closing(open_read_only(state)) as conn:
        yield conn


def _hit_lines(payload) -> List[Tuple[int, int]]:
    """(transcript_id, line_no) for every hit, for identity assertions."""
    return [(h["transcript_id"], h["line_no"]) for h in payload["result"]]


# --- the two zero-hit answers ----------------------------------------------


def test_zero_hits_complete_and_zero_hits_budget_exhausted_differ(corpus):
    """The whole reason this file exists: five independent discriminators.

    Both requests return an empty ``result``. A client that branches on
    the list alone cannot tell them apart, so every assertion below is on
    a field that DIFFERS.
    """
    complete = search_scoped(corpus, "zzzznotpresent", "project", 1)
    exhausted = search_scoped(
        corpus, "zzzznotpresent", "project", 1, scan_budget=1
    )

    assert complete["result"] == [] and exhausted["result"] == []

    assert complete["result_status"] == RESULT_OK
    assert exhausted["result_status"] == RESULT_PARTIAL

    assert complete["meta"]["scan"]["status"] == SCAN_COMPLETE
    assert exhausted["meta"]["scan"]["status"] == SCAN_BUDGET_EXHAUSTED

    assert complete["meta"]["scan"]["transcripts_not_scanned"] == 0
    assert exhausted["meta"]["scan"]["transcripts_not_scanned"] == 2

    assert complete["unevaluated"] == []
    assert len(exhausted["unevaluated"]) == 1
    assert exhausted["unevaluated"][0]["subject"] == "project:1"
    assert "were not scanned" in exhausted["unevaluated"][0]["reason"]

    assert complete["meta"]["scan"]["resume_cursor"] is None
    assert exhausted["meta"]["scan"]["resume_cursor"]

    # has_more is False only when a list was actually read to its end.
    assert complete["meta"]["paging"]["has_more"] is False
    assert exhausted["meta"]["paging"]["has_more"] is None

    # And the two payloads are not merely different in one place.
    assert complete != exhausted


def test_scanned_plus_not_scanned_always_equals_the_scope(corpus):
    """A checkable arithmetic invariant, not a vibe."""
    for budget in (1, 2, 3):
        scan = search_scoped(
            corpus, "restic", "project", 1, scan_budget=budget
        )["meta"]["scan"]
        assert (
            scan["transcripts_scanned"] + scan["transcripts_not_scanned"] == 3
        )


def test_empty_project_is_ok_not_not_found(corpus):
    """A real but empty scope is a measurement: ok, complete, nothing."""
    out = search_scoped(corpus, "restic", "project", 2)
    assert out["result_status"] == RESULT_OK
    assert out["scope_status"] == "resolved"
    assert out["meta"]["scan"]["status"] == SCAN_COMPLETE
    assert out["meta"]["scope"]["transcripts_in_scope"] == 0


# --- resume ----------------------------------------------------------------


def test_resume_cursor_reaches_a_hit_the_first_request_could_not(corpus):
    """The hit is beyond the first budget by construction, not by luck."""
    first = search_scoped(corpus, "restic", "project", 1, scan_budget=1)
    # Positive control: prove the first request genuinely could not see
    # it. Without this the test would pass against an implementation that
    # simply re-ran the whole scan from the beginning.
    assert first["result"] == []
    assert first["meta"]["scan"]["status"] == SCAN_BUDGET_EXHAUSTED
    assert first["meta"]["scan"]["transcripts_scanned"] == 1

    cursor = first["meta"]["scan"]["resume_cursor"]
    second = search_scoped(
        corpus, "restic", "project", 1, scan_budget=1, cursor=cursor
    )
    assert second["result_status"] == RESULT_PARTIAL
    assert (20, 0) in _hit_lines(second)
    # It resumed, it did not restart: transcript 30 is never revisited.
    assert all(tid != 30 for tid, _ in _hit_lines(second))
    assert second["meta"]["scan"]["transcripts_scanned"] == 2

    third = search_scoped(
        corpus, "restic", "project", 1, scan_budget=1,
        cursor=second["meta"]["scan"]["resume_cursor"],
    )
    assert third["meta"]["scan"]["status"] == SCAN_COMPLETE
    assert third["result_status"] == RESULT_OK
    assert third["meta"]["scan"]["transcripts_not_scanned"] == 0


def test_paging_a_full_scan_visits_every_hit_exactly_once(corpus):
    """limit=1 walks the same hits as one unlimited request, in order."""
    whole = _hit_lines(search_scoped(corpus, "restic", "project", 1))
    walked: List[Tuple[int, int]] = []
    cursor = None
    for _ in range(10):
        page = search_scoped(corpus, "restic", "project", 1, limit=1,
                             cursor=cursor)
        walked.extend(_hit_lines(page))
        cursor = page["meta"]["paging"]["next_cursor"]
        if cursor is None:
            break
    assert walked == whole
    assert len(walked) == len(set(walked))


# --- literal matching ------------------------------------------------------


@pytest.mark.parametrize(
    "query, expect_line, decoy_line",
    [
        # A naive LIKE '%100%%' matches "1000 items" too.
        ("100%", 1, 1),
        # A naive LIKE '%a_b%' matches "axb" too.
        ("a_b", 2, 2),
        # A backslash is the LIKE escape character in many dialects.
        (f"C:{BACKSLASH}p", 3, None),
    ],
)
def test_like_metacharacters_match_literally(corpus, query, expect_line,
                                             decoy_line):
    """Each case carries a decoy a wildcard implementation would return."""
    out = search_scoped(corpus, query, "project", 1)
    assert out["result_status"] == RESULT_OK
    hits = _hit_lines(out)
    assert (20, expect_line) in hits, f"{query!r} did not match its own row"
    if decoy_line is not None:
        assert (30, decoy_line) not in hits, (
            f"{query!r} matched the decoy row: the metacharacter was treated "
            "as a wildcard"
        )
    assert len(hits) == 1


def test_a_bare_percent_does_not_match_everything(corpus):
    """The performance half of the same defect, asserted as a count."""
    out = search_scoped(corpus, "%%", "project", 1)
    assert out["result_status"] == RESULT_OK
    assert out["result"] == []
    assert out["meta"]["scan"]["status"] == SCAN_COMPLETE


# --- secrets ---------------------------------------------------------------


def test_secret_bearing_hit_is_reported_with_the_snippet_withheld(corpus):
    """Withholding the snippet must never withhold the hit."""
    out = search_scoped(corpus, "restic", "project", 1)
    secret_hits = [h for h in out["result"] if h["secret_finding_count"] > 0]
    assert len(secret_hits) == 1, "the secret-bearing hit was dropped"

    hit = secret_hits[0]
    assert hit["snippet"] is None
    assert hit["snippet_state"] == SNIPPET_WITHHELD_SECRET
    # The finding itself is intact: an operator can still locate it.
    assert hit["transcript_id"] == 20
    assert hit["line_no"] == 4
    # 9 = len('{"text":"'), the literal prefix before the match.
    assert hit["match_offset"] == 9
    assert hit["match_length"] == len("restic")
    assert hit["body_href"].endswith(str(hit["body_id"]))

    # The credential appears NOWHERE in the serialized response.
    assert FAKE_SECRET not in json.dumps(out)


def test_a_non_secret_hit_still_gets_its_snippet(corpus):
    """Positive control: a withheld snippet must mean something.

    A test that only asserts the withheld case cannot tell correct
    withholding from a snippet builder that is simply broken.
    """
    out = search_scoped(corpus, "beacon", "project", 1)
    assert len(out["result"]) == 1
    hit = out["result"][0]
    assert hit["snippet_state"] == "included"
    assert "beacon" in hit["snippet"]


# --- scope is mandatory ----------------------------------------------------


@pytest.mark.parametrize("scope", ["", "global", "corpus", "all", None])
def test_an_unscoped_or_unknown_scope_is_cannot_determine(corpus, scope):
    """Never silently run globally: that is a 17.6 second full scan."""
    out = search_scoped(corpus, "restic", scope, 1)
    assert out["result_status"] == RESULT_CANNOT_DETERMINE
    assert out["scope_status"] == SCOPE_CANNOT_DETERMINE
    assert out["result"] is None
    assert out["unevaluated"][0]["subject"] == "scope"
    # Nothing was measured, so no count may be reported.
    assert out["meta"]["scan"]["status"] == "not_run"
    assert out["meta"]["scan"]["transcripts_scanned"] is None
    assert out["meta"]["paging"]["has_more"] is None


def test_an_unknown_scope_id_is_not_found_not_an_empty_ok(corpus):
    """There is no project 99999 to have results."""
    out = search_scoped(corpus, "restic", "project", 99999)
    assert out["result_status"] == RESULT_NOT_FOUND
    assert out["scope_status"] == SCOPE_NOT_FOUND
    assert out["result"] == []
    assert out["meta"]["paging"]["has_more"] is None


def test_transcript_scope_searches_only_that_transcript(corpus):
    """The other legal scope, and it must not leak into its neighbours."""
    out = search_scoped(corpus, "restic", "transcript", 20)
    assert out["result_status"] == RESULT_OK
    assert {tid for tid, _ in _hit_lines(out)} == {20}
    assert out["meta"]["scope"]["transcript_id"] == 20


# --- cursors ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "!!!not-base64!!!",
        "e30",                                    # valid base64, empty payload
        encode_cursor("lines", {"v": 1, "line_no": 3}),   # wrong kind
    ],
)
def test_a_malformed_cursor_is_cannot_determine_never_a_restart(corpus, bad):
    """A silent restart renders duplicates forever and never finishes."""
    out = search_scoped(corpus, "restic", "project", 1, cursor=bad)
    assert out["result_status"] == RESULT_CANNOT_DETERMINE
    assert out["result"] is None
    assert out["unevaluated"][0]["subject"] == "cursor"
    assert out["meta"]["scan"]["status"] == "not_run"
    # The scope resolved fine; it is the cursor that did not.
    assert out["scope_status"] == "resolved"


def test_a_cursor_from_another_transcript_is_refused(corpus):
    """Replaying a position against a different scope means nothing."""
    alien = encode_cursor(CURSOR_SEARCH, {
        "v": 1, "t_ingested_at": "2026-08-30T00:00:00.000000Z", "t_id": 30,
        "line_no": LINE_DONE, "scanned": 1, "bytes": TRANSCRIPT_BYTES,
    })
    out = search_scoped(corpus, "restic", "transcript", 20, cursor=alien)
    assert out["result_status"] == RESULT_CANNOT_DETERMINE
    assert out["unevaluated"][0]["subject"] == "cursor"


def test_bad_bounds_are_refused_rather_than_clamped(corpus):
    """A clamped limit produces a short page that reads as the end."""
    for kwargs, subject in (
        ({"limit": 0}, "limit"),
        ({"limit": 10_000}, "limit"),
        ({"scan_budget": 0}, "scan_budget"),
        ({"scan_bytes": 0}, "scan_bytes"),
    ):
        out = search_scoped(corpus, "restic", "project", 1, **kwargs)
        assert out["result_status"] == RESULT_CANNOT_DETERMINE
        assert out["unevaluated"][0]["subject"] == subject

    short = search_scoped(corpus, "r", "project", 1)
    assert short["unevaluated"][0]["subject"] == "q"


def test_the_first_transcript_is_always_scanned(corpus):
    """A byte budget below one transcript must still make progress.

    Charging the budget BEFORE the scan would mint a resume cursor that
    never advances - a loop that looks exactly like paging.
    """
    out = search_scoped(corpus, "restic", "project", 1, scan_bytes=1)
    assert out["meta"]["scan"]["transcripts_scanned"] == 1
    assert out["meta"]["scan"]["bytes_scanned"] == TRANSCRIPT_BYTES
    assert out["meta"]["scan"]["resume_cursor"]
