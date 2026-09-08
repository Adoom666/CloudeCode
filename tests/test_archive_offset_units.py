"""Offsets are UNICODE CODE POINTS, and the JS masking recipe must use
the UTF-16 pair.

WHY EVERY BODY HERE IS NON-ASCII. A pure-ASCII fixture cannot tell a
character implementation from a byte implementation - the two agree on
every offset - so an ASCII test of this property asserts nothing and
passes forever while the contract rots. Two agents reached opposite
conclusions about this field on 2026-08-31 and the ASCII tests were
green for both. Every body below therefore carries multi-byte text
BEFORE the secret, and the astral cases carry emoji before it so the
code-point and UTF-16 answers differ too.
"""

from __future__ import annotations

import hashlib
from contextlib import closing

from src.core.archive_body import body
from src.core.archive_read import (
    BODY_SIZE_UNITS,
    OFFSET_UNITS_CODE_POINTS,
    offset_units_meta,
    open_read_only,
)
from src.core.archive_search import search_scoped
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

SECRET = "Q7xLm2Wp9RtVzB4kNc6JhY8dFgA3sEuT"


def _seed(tmp_path, prefix, name):
    """Seed one body of ``prefix + 'token=' + SECRET`` and its finding.

    Inputs: tmp_path, prefix (str - text placed BEFORE the secret),
      name (str - unique state dir name).
    Output: (state_dir, body_id, payload, offset, transcript_id).
    """
    payload = f'{prefix}token={SECRET}'
    offset = payload.index(SECRET)
    assert len(payload.encode("utf-8")) != len(payload), (
        "fixture must be multi-byte or it cannot discriminate"
    )
    state_dir = make_state_dir(tmp_path, name)
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id)
            project_id = seed_project(conn, corpus_id, slug="-p")
            transcript_id = seed_transcript(
                conn, host_id=host_id, corpus_id=corpus_id,
                project_id=project_id, source_path="s.jsonl", line_count=1,
            )
            body_id = seed_body(
                conn, body_json=payload, secret_finding_count=1,
                identity_key=name,
            )
            seed_appearance(
                conn, transcript_id=transcript_id, line_no=1, body_id=body_id
            )
            conn.execute(
                "INSERT INTO message_secret_findings "
                "(body_id, detector, match_offset, match_length, "
                " value_sha256, observed_at) "
                "VALUES (?, 'high_entropy_assignment', ?, ?, ?, ?)",
                (body_id, offset, len(SECRET),
                 hashlib.sha256(SECRET.encode()).hexdigest(),
                 "2026-08-31T00:00:00Z"),
            )
    return state_dir, body_id, payload, offset, transcript_id


def test_offset_is_code_points_not_bytes(tmp_path):
    """The stored offset slices the body as CHARACTERS. The byte
    interpretation is asserted to FAIL, so this test can only pass for
    the right reason."""
    state_dir, body_id, payload, offset, _ = _seed(
        tmp_path, "é" * 12 + " ", "cp")
    with closing(open_read_only(state_dir)) as conn:
        finding = body(conn, body_id)["result"]["secrets"][0]

    off, ln = finding["match_offset"], finding["match_length"]
    want = finding["value_sha256"]

    char_slice = payload[off:off + ln]
    assert hashlib.sha256(char_slice.encode()).hexdigest() == want

    byte_slice = payload.encode("utf-8")[off:off + ln]
    assert hashlib.sha256(byte_slice).hexdigest() != want, (
        "byte interpretation must NOT reproduce the hash; if it does the "
        "fixture stopped discriminating"
    )


def test_utf16_pair_is_what_a_js_client_must_slice(tmp_path):
    """The client-side masking recipe, executed. An astral character
    sits before the secret, so the code-point offset and the UTF-16
    offset genuinely differ and only one of them masks correctly."""
    state_dir, body_id, payload, offset, _ = _seed(
        tmp_path, "🔑🔑🔑 é ", "utf16")
    with closing(open_read_only(state_dir)) as conn:
        finding = body(conn, body_id)["result"]["secrets"][0]

    assert finding["utf16_state"] == "computed"
    u_off = finding["match_offset_utf16"]
    u_len = finding["match_length_utf16"]
    assert u_off != finding["match_offset"], (
        "astral prefix must make the two units differ, else no discrimination"
    )

    # Exactly what JavaScript String.prototype.slice does.
    units = payload.encode("utf-16-le")
    masked = units[u_off * 2:(u_off + u_len) * 2].decode("utf-16-le")
    assert hashlib.sha256(masked.encode()).hexdigest() == finding["value_sha256"]

    # And the naive recipe the docs used to imply is WRONG here.
    wrong = units[offset * 2:(offset + u_len) * 2].decode("utf-16-le")
    assert wrong != masked


def test_body_chars_counts_characters_and_body_bytes_is_the_same_number(
    tmp_path,
):
    """``body_bytes`` is a code-point count. The truthful name carries
    the same value; both are asserted so a future divergence is loud."""
    state_dir, body_id, payload, _, _ = _seed(tmp_path, "é" * 20 + " ", "size")
    with closing(open_read_only(state_dir)) as conn:
        result = body(conn, body_id)["result"]

    assert result["body_chars"] == len(payload)
    assert result["body_chars"] != len(payload.encode("utf-8"))
    assert result["body_bytes"] == result["body_chars"]


def test_search_and_secret_offsets_are_the_same_unit(tmp_path):
    """Two fields named ``match_offset`` in one API must mean one thing.
    The search hit comes from SQLite INSTR; the finding comes from
    Python re. This asserts they agree on a multi-byte body."""
    state_dir, body_id, payload, offset, transcript_id = _seed(
        tmp_path, "é" * 9 + " ", "agree")
    with closing(open_read_only(state_dir)) as conn:
        hit = search_scoped(
            conn, SECRET, "transcript", transcript_id)["result"][0]
        finding = body(conn, body_id)["result"]["secrets"][0]

    assert hit["match_offset"] == finding["match_offset"] == offset
    assert payload[hit["match_offset"]:
                   hit["match_offset"] + hit["match_length"]] == SECRET
    assert hit["body_chars"] == len(payload) != len(payload.encode("utf-8"))


def test_every_offset_bearing_response_declares_its_unit(tmp_path):
    """A client must never infer a unit, and the two sides must quote
    the SAME declaration."""
    state_dir, body_id, _, _, transcript_id = _seed(
        tmp_path, "é" * 5 + " ", "meta")
    with closing(open_read_only(state_dir)) as conn:
        body_meta = body(conn, body_id)["meta"]
        search_meta = search_scoped(
            conn, SECRET, "transcript", transcript_id)["meta"]

    shared = offset_units_meta()
    assert shared["offset_units"] == OFFSET_UNITS_CODE_POINTS
    assert shared["body_size_units"] == BODY_SIZE_UNITS
    for key, value in shared.items():
        assert body_meta[key] == value
        assert search_meta[key] == value, f"search disagrees on {key}"


def test_withheld_body_reports_cannot_determine_not_a_guess(tmp_path):
    """No body means the UTF-16 conversion could not be performed. That
    is the third outcome, not an offset of zero."""
    from src.core import archive_body

    out = archive_body._utf16_offsets(None, 5, 3)
    assert out["utf16_state"] == "cannot_determine"
    assert out["match_offset_utf16"] is None
    assert out["match_length_utf16"] is None
    assert "withheld" in out["utf16_reason"]
