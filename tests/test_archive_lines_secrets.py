"""A /lines row carrying a body must carry the offsets that mask it.

THE DEFECT THIS FILE EXISTS TO CATCH. ``/lines?include_bodies=true`` used
to return ``body_state: "included"`` with a whole credential-bearing
``body_json`` and ``secret_finding_count: 2``, and NO ``secrets`` array.
The offsets existed only on ``/bodies/{id}``, so the bulk path handed a
client a credential it had no way to mask without a second request per
row - and a client that skipped that round trip rendered it. Measured on
the live corpus 2026-08-31 at transcript 4, line 32, body 119.

WHY THE ASSERTIONS ARE SHAPED THIS WAY. Checking that ``secrets`` is
present would pass on an empty list, and an empty list is the exact false
green this is about: it reads as "checked, clean" over a body nobody
examined. So the tests assert the three states apart - a list with
findings, an empty list that was MEASURED, and None where the body was
never included - and assert that the offsets a /lines row hands out are
EQUAL to the ones /bodies gives for the same body, because two
implementations of a masking contract diverge silently and the wrong one
still returns plausible integers.

NO TEST HERE ASSERTS ON A MATCHED VALUE, because no matched value should
exist anywhere to assert on. One test proves that directly.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.core.archive_body import body
from src.core.archive_lines import transcript_lines
from src.core.archive_read import BODY_INCLUDED, BODY_NOT_REQUESTED, open_read_only
from tests.archive_fixture import (
    make_state_dir,
    seed_appearance,
    seed_body,
    seed_corpus,
    seed_host,
    seed_secret_finding,
    seed_transcript,
    writable,
)

#: A body whose credential sits AFTER an astral-plane character, so a
#: client masking with the code-point offset would misalign by one unit
#: per astral character and expose the head of the credential. This is
#: the case the UTF-16 companions exist for; a BMP-only fixture cannot
#: tell a correct conversion from a copied integer.
ASTRAL = "\U0001F511"
SECRET_TEXT = "sk-live-AAAABBBBCCCC"
BODY_WITH_ASTRAL = f'{{"note":"{ASTRAL} key is {SECRET_TEXT} end"}}'
SECRET_OFFSET = BODY_WITH_ASTRAL.index(SECRET_TEXT)
SECRET_LENGTH = len(SECRET_TEXT)

#: A second, clean body on the same page. Its row must render an empty
#: list, not None and not a missing key.
CLEAN_BODY = '{"note":"nothing to see"}'


@pytest.fixture(name="archive")
def archive_fixture(tmp_path: Path) -> Dict[str, Any]:
    """Build a transcript with one secret-bearing line and one clean line.

    Description: line 0 carries a body with two findings, one of them
      after an astral-plane character; line 1 carries a clean body; line
      2 carries no body at all. Three rows, three body states, one page.
    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: dict with state_dir, transcript_id, secret_body_id,
      clean_body_id.
    Example: archive["secret_body_id"] -> 1
    """
    state_dir = make_state_dir(tmp_path)
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id=host_id)
            transcript_id = seed_transcript(
                conn, corpus_id=corpus_id, host_id=host_id, project_id=None,
                source_path="/tmp/secrets.jsonl",
            )
            secret_body_id = seed_body(
                conn, body_json=BODY_WITH_ASTRAL, secret_finding_count=2
            )
            # Two findings on ONE body, ordered by offset, so the batched
            # read's grouping and ordering are both exercised.
            seed_secret_finding(
                conn, body_id=secret_body_id, match_offset=SECRET_OFFSET,
                match_length=SECRET_LENGTH, detector="api_key",
                value_sha256="a" * 64,
            )
            seed_secret_finding(
                conn, body_id=secret_body_id,
                match_offset=SECRET_OFFSET + SECRET_LENGTH + 1,
                match_length=3, detector="trailing", value_sha256="b" * 64,
            )
            clean_body_id = seed_body(
                conn, body_json=CLEAN_BODY, secret_finding_count=0,
                identity_key="clean-body",
            )
            seed_appearance(
                conn, transcript_id=transcript_id, line_no=0, body_id=secret_body_id
            )
            seed_appearance(
                conn, transcript_id=transcript_id, line_no=1, body_id=clean_body_id
            )
            seed_appearance(
                conn, transcript_id=transcript_id, line_no=2, body_id=None,
                line_status="blank",
            )
    return {
        "state_dir": state_dir,
        "transcript_id": transcript_id,
        "secret_body_id": secret_body_id,
        "clean_body_id": clean_body_id,
    }


def _rows(archive: Dict[str, Any], **kwargs: Any) -> List[Dict[str, Any]]:
    """Page the fixture transcript's lines and return the result list.

    Inputs: archive (dict) from the fixture, kwargs forwarded to
      ``transcript_lines``.
    Output: list of line dicts.
    Example: _rows(archive, include_bodies=True)[0]["secrets"]
    """
    with closing(open_read_only(archive["state_dir"])) as conn:
        return transcript_lines(conn, archive["transcript_id"], **kwargs)["result"]


def test_included_row_with_findings_carries_the_secrets_array(
    archive: Dict[str, Any],
) -> None:
    """A row with a body and findings carries offsets, lengths and a hash."""
    row = _rows(archive, include_bodies=True)[0]
    assert row["body_state"] == BODY_INCLUDED
    assert row["secret_finding_count"] == 2
    secrets = row["secrets"]
    assert isinstance(secrets, list) and len(secrets) == 2
    first = secrets[0]
    for field in (
        "detector", "match_offset", "match_length", "value_sha256",
        "match_offset_utf16", "match_length_utf16", "utf16_state",
    ):
        assert field in first, f"{field} missing from a /lines secrets entry"
    assert first["match_offset"] == SECRET_OFFSET
    assert first["match_length"] == SECRET_LENGTH
    assert first["utf16_state"] == "computed"
    # Ordered by match_offset, so a client masking left to right need not sort.
    assert secrets[0]["match_offset"] < secrets[1]["match_offset"]


def test_utf16_companions_differ_from_the_code_point_offsets(
    archive: Dict[str, Any],
) -> None:
    """The UTF-16 offset is CONVERTED, not copied.

    Description: the fixture body carries an astral-plane character
      before the match, so the UTF-16 offset must be exactly one unit
      larger per astral character. If the conversion were a copy, or were
      dropped and the raw offset reused, this is the assertion that
      fails - and a BMP-only body could not tell the two apart.
    """
    secrets = _rows(archive, include_bodies=True)[0]["secrets"]
    assert secrets[0]["match_offset_utf16"] == SECRET_OFFSET + 1
    assert secrets[0]["match_offset_utf16"] != secrets[0]["match_offset"]
    # The match itself is plain ASCII, so its length is unchanged.
    assert secrets[0]["match_length_utf16"] == SECRET_LENGTH


def test_lines_offsets_equal_the_body_route_offsets(archive: Dict[str, Any]) -> None:
    """The bulk path and the single-body path agree, field for field.

    Description: this is the anti-divergence assertion. Two copies of the
      offset arithmetic would both return plausible integers, so equality
      against the route that was already correct is the only check that
      catches a drift.
    """
    line_secrets = _rows(archive, include_bodies=True)[0]["secrets"]
    with closing(open_read_only(archive["state_dir"])) as conn:
        body_secrets = body(conn, archive["secret_body_id"])["result"]["secrets"]
    assert line_secrets == body_secrets


def test_masking_with_the_utf16_offsets_covers_the_whole_secret(
    archive: Dict[str, Any],
) -> None:
    """The offsets actually mask the credential in UTF-16 space.

    Description: asserting the integers are present proves nothing about
      whether they WORK. This performs the mask a JavaScript client would
      perform - slicing the body as UTF-16 code units - and asserts the
      secret is gone from the result and that no character outside the
      match was consumed.
    """
    row = _rows(archive, include_bodies=True)[0]
    units = row["body_json"].encode("utf-16-le")
    entry = row["secrets"][0]
    start = entry["match_offset_utf16"] * 2
    end = start + entry["match_length_utf16"] * 2
    masked = (units[:start] + units[end:]).decode("utf-16-le")
    assert SECRET_TEXT not in masked
    assert masked == BODY_WITH_ASTRAL.replace(SECRET_TEXT, "", 1)


def test_no_matched_value_is_ever_returned(archive: Dict[str, Any]) -> None:
    """No secrets entry carries the matched text, in any field.

    Description: the body itself legitimately contains the secret - it is
      returned WHOLE and unmodified by design - so the assertion is
      scoped to the findings, which is where a leak would be a contract
      violation rather than the documented behaviour.
    """
    for entry in _rows(archive, include_bodies=True)[0]["secrets"]:
        for key, value in entry.items():
            assert SECRET_TEXT not in str(value), f"matched value leaked in {key}"
        assert "value" not in entry
        assert "match_text" not in entry


def test_clean_included_row_reports_an_empty_list_not_none(
    archive: Dict[str, Any],
) -> None:
    """A body with no findings was MEASURED clean, so it reports []."""
    row = _rows(archive, include_bodies=True)[1]
    assert row["body_state"] == BODY_INCLUDED
    assert row["secret_finding_count"] == 0
    assert row["secrets"] == []


def test_row_without_a_body_reports_none_not_an_empty_list(
    archive: Dict[str, Any],
) -> None:
    """An unevaluated row must not claim to be clean.

    Description: the THIRD OUTCOME. ``[]`` here would say "checked, no
      secrets" about a body this response never read, which is the false
      green the whole envelope exists to prevent.
    """
    rows = _rows(archive, include_bodies=False)
    assert rows[0]["body_state"] == BODY_NOT_REQUESTED
    assert rows[0]["secrets"] is None, "a body that was not read is not 'clean'"
    # And the row that genuinely has no body at all.
    assert _rows(archive, include_bodies=True)[2]["secrets"] is None


def test_secrets_are_not_attached_when_bodies_are_not_requested(
    archive: Dict[str, Any],
) -> None:
    """The join is conditional on include_bodies, so the default page is free."""
    assert all(row["secrets"] is None for row in _rows(archive, include_bodies=False))


def test_one_body_on_two_lines_gets_the_offsets_on_both_rows(
    tmp_path: Path,
) -> None:
    """A body appearing twice on one page is masked on BOTH rows.

    Description: the batched read is keyed by body id, not by line, so a
      naive implementation that mapped one finding set to one row would
      leave the second occurrence unmaskable. The corpus really does
      repeat bodies across lines, so this is not a synthetic worry.
    """
    state_dir = make_state_dir(tmp_path, name="shared")
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id=host_id)
            transcript_id = seed_transcript(
                conn, corpus_id=corpus_id, host_id=host_id, project_id=None,
                source_path="/tmp/shared.jsonl",
            )
            body_id = seed_body(
                conn, body_json=BODY_WITH_ASTRAL, secret_finding_count=1
            )
            seed_secret_finding(
                conn, body_id=body_id, match_offset=SECRET_OFFSET,
                match_length=SECRET_LENGTH,
            )
            for line_no in (0, 1):
                seed_appearance(
                    conn, transcript_id=transcript_id, line_no=line_no,
                    body_id=body_id,
                )
    with closing(open_read_only(state_dir)) as conn:
        rows = transcript_lines(conn, transcript_id, include_bodies=True)["result"]
    assert len(rows) == 2
    assert rows[0]["secrets"] == rows[1]["secrets"]
    assert len(rows[0]["secrets"]) == 1
