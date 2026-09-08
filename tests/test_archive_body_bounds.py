"""A body is returned WHOLE, or not at all. Never a prefix.

EVERY EQUALITY HERE IS DELIBERATE AND ``startswith`` IS BANNED. A prefix
check passes for a full string too, so a test written with
``returned.startswith(stored)`` cannot detect the one defect it exists to
catch. Every assertion below compares the returned ``body_json`` to the
stored value read independently out of the database, by ``==``.

THE OVERSIZED PATH CANNOT BE REACHED WITH REAL DATA. No body in the real
corpus exceeds MAX_BODY_BYTES - the largest is 54,376,859 against a
67,108,864 cap - so the withheld path has never executed against the
archive. A code path that has never run and has no test is a path that
does not work, and reality cannot supply the input, so the fixture builds
a synthetic body just over the cap. That test is slow on purpose; it is
the only way this branch is ever exercised.

The byte-budget path is the other way a body could get silently cut. It
does not cut: the page STOPS, says ``partial``, and hands back a resume
cursor. Rows past the stop are dropped rather than returned body-less,
because a row returned without the body the caller asked for is
indistinguishable from a row that never had one.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Tuple

import pytest

os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_archbody_logs_"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.archive_body import body
from src.core.archive_lines import transcript_lines
from src.core.archive_read import (
    BODY_ABSENT,
    BODY_INCLUDED,
    BODY_NOT_REQUESTED,
    BODY_WITHHELD_TOO_LARGE,
    MAX_BODY_BYTES,
    RESULT_OK,
    RESULT_PARTIAL,
    open_read_only,
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

#: Four ordinary bodies, deliberately different lengths so a
#: fixed-window truncation bug cannot pass by coincidence.
NORMAL_BODIES = [
    json.dumps({"n": n, "text": "x" * (500 * (n + 1))}, separators=(",", ":"))
    for n in range(4)
]


def stored_body_json(state_dir: Path, body_id: int) -> str:
    """Read a body straight from the database, as the reference value.

    Description: an INDEPENDENT read. Comparing the API's output against
      the API's own earlier output would prove only that it is
      consistent, not that it is complete.
    Inputs: state_dir (Path), body_id (int).
    Output: str - the stored body_json.
    Example: stored_body_json(sd, 1)
    """
    with closing(writable(state_dir)) as conn:
        row = conn.execute(
            "SELECT body_json FROM message_bodies WHERE id = ?", (body_id,)
        ).fetchone()
    return str(row["body_json"])


@pytest.fixture()
def archive(tmp_path: Path) -> Path:
    """One transcript: four normal-bodied lines and one with NO body.

    Inputs: tmp_path (Path).
    Output: Path - the state directory.
    """
    state_dir = make_state_dir(tmp_path, "bodies")
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id)
            project_id = seed_project(conn, corpus_id, slug="-p")
            transcript_id = seed_transcript(
                conn,
                host_id=host_id,
                corpus_id=corpus_id,
                project_id=project_id,
                source_path="t.jsonl",
                line_count=5,
            )
            for n, payload in enumerate(NORMAL_BODIES, start=1):
                # Line 2's body has NO ts, which 33,480 real bodies also
                # lack. It must still be returned and still be counted.
                body_id = seed_body(
                    conn,
                    body_json=payload,
                    ts=None if n == 2 else "2025-12-29T06:50:35.600Z",
                )
                seed_appearance(
                    conn, transcript_id=transcript_id, line_no=n, body_id=body_id
                )
            # The absent case: 1 of 3,125,122 real appearance rows.
            seed_appearance(
                conn,
                transcript_id=transcript_id,
                line_no=5,
                body_id=None,
                line_status="invalid_json",
            )
    return state_dir


# --- whole bodies, asserted by equality ------------------------------------


def test_lines_return_bodies_whole_not_prefixed(archive: Path) -> None:
    """Every included body EQUALS the stored value, character for character."""
    with closing(open_read_only(archive)) as conn:
        page = transcript_lines(conn, 1, include_bodies=True, max_page_bytes=8388608)
    assert page["result_status"] == RESULT_OK
    included = [row for row in page["result"] if row["body_state"] == BODY_INCLUDED]
    assert len(included) == len(NORMAL_BODIES), "no bodies were actually included"
    for row in included:
        expected = stored_body_json(archive, row["body_id"])
        # EQUALITY, never startswith - startswith passes for a full string
        # and so could never fail on the defect it would be testing for.
        assert row["body_json"] == expected
        assert len(row["body_json"]) == len(expected)


def test_body_route_returns_the_whole_body(archive: Path) -> None:
    """The single-body route matches the stored value exactly."""
    with closing(open_read_only(archive)) as conn:
        result = body(conn, 1)
    assert result["result_status"] == RESULT_OK
    assert result["result"]["body_state"] == BODY_INCLUDED
    assert result["result"]["body_json"] == stored_body_json(archive, 1)


def test_no_body_json_anywhere_is_a_proper_prefix_of_the_stored_value(
    archive: Path,
) -> None:
    """The negative stated directly: nothing returned is a truncation.

    Description: scans every body_json this API can emit and asserts each
      is either None or exactly equal. The explicit proper-prefix check
      is what names the defect, so a failure reads as "this was
      truncated" rather than "two strings differed".
    """
    with closing(open_read_only(archive)) as conn:
        page = transcript_lines(conn, 1, include_bodies=True, max_page_bytes=8388608)
        singles = [body(conn, n)["result"] for n in range(1, len(NORMAL_BODIES) + 1)]

    for row in list(page["result"]) + singles:
        returned = row["body_json"]
        if returned is None:
            continue
        expected = stored_body_json(archive, row["body_id"])
        assert returned == expected
        assert not (
            len(returned) < len(expected) and expected.startswith(returned)
        ), f"body {row['body_id']} was returned as a PREFIX of the stored value"


def test_bodies_are_not_sent_unless_asked_for(archive: Path) -> None:
    """not_requested is its own state, and carries a href instead."""
    with closing(open_read_only(archive)) as conn:
        page = transcript_lines(conn, 1)
    with_bodies = [row for row in page["result"] if row["body_id"] is not None]
    assert with_bodies
    for row in with_bodies:
        assert row["body_state"] == BODY_NOT_REQUESTED
        assert row["body_json"] is None
        assert row["body_href"] == f"/api/v1/archive/bodies/{row['body_id']}"


def test_absent_is_not_withheld(archive: Path) -> None:
    """body_id IS NULL means there is NO body; it is not a withheld one."""
    with closing(open_read_only(archive)) as conn:
        page = transcript_lines(conn, 1, include_bodies=True)
    absent = [row for row in page["result"] if row["line_no"] == 5][0]
    assert absent["body_state"] == BODY_ABSENT
    assert absent["body_json"] is None
    assert absent["body_href"] is None
    assert absent["body_bytes"] is None


# --- the synthetic oversized body ------------------------------------------


@pytest.fixture()
def oversized(tmp_path: Path) -> Tuple[Path, int]:
    """A body one byte over MAX_BODY_BYTES, which real data cannot supply.

    Description: slow and memory-hungry by necessity - about 64 MiB of
      text. It is the only way the withheld branch is ever executed.
    Inputs: tmp_path (Path).
    Output: (state directory, the oversized body id).
    """
    state_dir = make_state_dir(tmp_path, "oversized")
    payload = "z" * (MAX_BODY_BYTES + 1)
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id)
            project_id = seed_project(conn, corpus_id, slug="-p")
            transcript_id = seed_transcript(
                conn,
                host_id=host_id,
                corpus_id=corpus_id,
                project_id=project_id,
                source_path="big.jsonl",
                line_count=1,
            )
            body_id = seed_body(conn, body_json=payload, identity_key="oversized")
            seed_appearance(
                conn, transcript_id=transcript_id, line_no=1, body_id=body_id
            )
    return state_dir, body_id


def test_oversized_body_is_withheld_with_a_href_on_the_lines_route(
    oversized: Tuple[Path, int],
) -> None:
    """body_json None, withheld_too_large, and a href to follow."""
    state_dir, body_id = oversized
    with closing(open_read_only(state_dir)) as conn:
        page = transcript_lines(conn, 1, include_bodies=True)
    row = page["result"][0]
    assert row["body_bytes"] == MAX_BODY_BYTES + 1
    assert row["body_state"] == BODY_WITHHELD_TOO_LARGE
    assert row["body_json"] is None
    assert row["body_href"] == f"/api/v1/archive/bodies/{body_id}"
    # Withheld is not the same as absent, and the fixture proves the row
    # genuinely has a body.
    assert row["body_id"] == body_id
    # It cost the page no bytes, because it was not sent.
    assert page["meta"]["bodies"]["page_bytes"] == 0
    assert page["result_status"] == RESULT_OK


def test_oversized_body_is_withheld_on_the_body_route_too(
    oversized: Tuple[Path, int],
) -> None:
    """The single-body route withholds as well, and says why."""
    state_dir, body_id = oversized
    with closing(open_read_only(state_dir)) as conn:
        result = body(conn, body_id)
    assert result["result"]["body_state"] == BODY_WITHHELD_TOO_LARGE
    assert result["result"]["body_json"] is None
    assert result["result"]["body_bytes"] == MAX_BODY_BYTES + 1
    assert result["result"]["body_href"] == f"/api/v1/archive/bodies/{body_id}"
    assert str(MAX_BODY_BYTES) in result["meta"]["withheld_note"]


# --- the byte budget stops, it does not truncate ---------------------------


def test_a_spent_byte_budget_stops_the_page_and_reports_partial(
    archive: Path,
) -> None:
    """Partial, a resume cursor, and every body still whole."""
    with closing(open_read_only(archive)) as conn:
        page = transcript_lines(conn, 1, include_bodies=True, max_page_bytes=1024)

    assert page["result_status"] == RESULT_PARTIAL
    assert page["meta"]["bodies"]["stopped_early"] is True
    assert page["meta"]["paging"]["has_more"] is True
    assert page["meta"]["paging"]["next_cursor"] is not None
    assert page["unevaluated"], "a partial page must name what it did not return"
    assert len(page["result"]) < 5

    # Whatever it DID return is whole. It stopped; it did not trim.
    for row in page["result"]:
        if row["body_json"] is not None:
            assert row["body_json"] == stored_body_json(archive, row["body_id"])


def test_resuming_from_the_budget_cursor_finishes_the_transcript(
    archive: Path,
) -> None:
    """The stop is a pause, not a loss: the walk still sees every line once."""
    seen = []
    cursor = None
    for _ in range(10):
        with closing(open_read_only(archive)) as conn:
            page = transcript_lines(
                conn, 1, include_bodies=True, max_page_bytes=1024, cursor=cursor
            )
        seen.extend(row["line_no"] for row in page["result"])
        if not page["meta"]["paging"]["has_more"]:
            break
        cursor = page["meta"]["paging"]["next_cursor"]
    assert seen == [1, 2, 3, 4, 5]


def test_a_body_over_the_budget_is_still_returned_when_it_is_first(
    archive: Path,
) -> None:
    """A page can never be empty for want of budget."""
    with closing(open_read_only(archive)) as conn:
        page = transcript_lines(conn, 1, include_bodies=True, max_page_bytes=1024)
    first = page["result"][0]
    assert first["body_state"] == BODY_INCLUDED
    assert first["body_json"] == stored_body_json(archive, first["body_id"])


def test_a_null_ts_row_is_returned_and_counted(archive: Path) -> None:
    """A NULL ts must not make a row invisible, and the count says so.

    Description: ordering here is on ``line_no``, so a NULL ``ts`` cannot
      hide a row the way it would under a ts-keyed page. This asserts
      both halves: the row IS present, and the page publishes how many
      such rows it returned rather than leaving a reader to assume none.
    """
    with closing(open_read_only(archive)) as conn:
        page = transcript_lines(conn, 1, include_bodies=True, max_page_bytes=8388608)
    by_line = {row["line_no"]: row for row in page["result"]}
    assert by_line[2]["ts"] is None
    assert by_line[2]["body_json"] == stored_body_json(archive, by_line[2]["body_id"])
    assert by_line[1]["ts"] is not None
    # 1 body with no ts, plus the body-less line 5 which also has no ts.
    assert page["meta"]["lines_with_null_ts"] == 2


# --- secret offsets: character offsets, and they must round-trip -----------


def test_secret_offsets_index_the_returned_body_by_characters(
    tmp_path: Path,
) -> None:
    """Slicing the RETURNED body by the reported offsets reproduces the hash.

    Description: this is the client's masking contract, asserted end to
      end. The fixture puts multi-byte characters BEFORE the secret, so a
      byte-offset implementation and a character-offset one disagree -
      without that, both would pass and the test would prove nothing.
      ``docs/message-browser-api.md`` section 2 calls these byte offsets;
      measured against the live corpus they are character offsets, and
      this test pins the behaviour that actually ships.

      The matched value is never printed or asserted on directly, only
      its sha256, so the test cannot leak it into CI output.
    """
    import hashlib

    secret = "A" * 40
    prefix = '{"note":"' + "éü—" * 20 + '","token":"'
    payload = prefix + secret + '"}'
    offset = len(prefix)
    assert len(payload.encode("utf-8")) != len(payload), (
        "fixture has no multi-byte characters, so byte and character "
        "offsets would agree and this test could not fail"
    )

    state_dir = make_state_dir(tmp_path, "secrets")
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id)
            project_id = seed_project(conn, corpus_id, slug="-p")
            transcript_id = seed_transcript(
                conn,
                host_id=host_id,
                corpus_id=corpus_id,
                project_id=project_id,
                source_path="s.jsonl",
                line_count=1,
            )
            body_id = seed_body(
                conn, body_json=payload, secret_finding_count=1, identity_key="sec"
            )
            seed_appearance(
                conn, transcript_id=transcript_id, line_no=1, body_id=body_id
            )
            conn.execute(
                "INSERT INTO message_secret_findings "
                "(body_id, detector, match_offset, match_length, value_sha256, "
                " observed_at) VALUES (?, 'high_entropy_assignment', ?, ?, ?, ?)",
                (
                    body_id,
                    offset,
                    len(secret),
                    hashlib.sha256(secret.encode()).hexdigest(),
                    "2026-08-29T22:17:03.086206Z",
                ),
            )

    with closing(open_read_only(state_dir)) as conn:
        response = body(conn, body_id)
    result = response["result"]

    finding = result["secrets"][0]
    returned = result["body_json"]
    assert returned == payload, "the body must come back whole to be sliceable"

    masked_slice = returned[
        finding["match_offset"] : finding["match_offset"] + finding["match_length"]
    ]
    assert (
        hashlib.sha256(masked_slice.encode()).hexdigest() == finding["value_sha256"]
    ), "the reported offsets do not index the returned body by characters"

    # And the response says which unit it means, so no client has to guess.
    assert response["meta"]["offset_units"] == "unicode_code_points"

    # The matched value itself appears nowhere except inside the whole body.
    assert secret not in json.dumps(result["secrets"])
    assert result["secret_finding_count"] == 1
