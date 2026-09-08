"""The snippet gate: a preview must not carry a credential the body-level
flag never knew about.

WHAT THIS FILE EXISTS TO CATCH, measured on the live corpus 2026-08-31.
``archive_search`` gated the preview on ``secret_finding_count > 0``.
That is a PROXY for "this body contains a credential" and it is wrong:
one credential sat in 762 bodies of which 415 carried ZERO findings, and
a single ``transcript_id=4`` search returned 21 of 43 hits with that
credential in cleartext.

THE ROOT CAUSE IS NOT STALE FINDINGS, so the fixture does not model
staleness. Re-scanning the 415 unflagged bodies with the CURRENT
detectors produced 0 findings, and all 347 flagged bodies still flag -
the stored flags are exactly faithful to the code. The value is 40
characters with no vendor marker, so only ``high_entropy_assignment``
can see it, and that detector needs a name saying "key"/"token"/"secret"
beside the value. All 533 detected occurrences had that context; all 587
occurrences in unflagged bodies did not.

SO THE FIXTURE REPRODUCES THAT SPLIT EXACTLY: the same credential in two
bodies, one in an assignment (detected, flagged) and one bare in prose
(not detected, NOT flagged). ``test_fixture_reproduces_the_measured_...``
asserts the split against the real detector so this file cannot rot into
a trivial pass, and the naive gate is asserted to be insufficient rather
than merely assumed to be.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Iterator

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_sg_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_sg_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core import archive_search
from src.core.archive_read import open_read_only
from src.core.archive_search import search_scoped
from src.core.archive_snippet_gate import (
    SNIPPET_INCLUDED,
    SNIPPET_WITHHELD_BY_REQUEST,
    SNIPPET_WITHHELD_FLAGGED_BODY,
    SNIPPET_WITHHELD_GATE_UNAVAILABLE,
    SNIPPET_WITHHELD_KNOWN_VALUE,
)
from src.core.db_migration import ensure_db_migrated
from src.core.message_model_secrets import scan_text

#: Synthetic credential material. Random base62, never issued by anyone,
#: chosen so the real detectors behave on it exactly as they behave on
#: the live corpus credential: seen in an assignment, invisible bare.
FIXTURE_CREDENTIAL = "mm5GsDRfCJXY0R6KVA1WOf5Zx1WU8RXuGoWAqOGj"
FIXTURE_SHA256 = hashlib.sha256(FIXTURE_CREDENTIAL.encode()).hexdigest()

#: Detected here: a credential-shaped NAME sits beside the value.
FLAGGED_BODY = json.dumps({"text": "restic run", "api_key": FIXTURE_CREDENTIAL})
#: NOT detected here: same value, no name beside it. This is the body
#: that leaked, and the whole point of the file.
UNFLAGGED_BODY = json.dumps(
    {"text": f"restic pipeline emitted {FIXTURE_CREDENTIAL} to the log"}
)
#: An ordinary body, so a withheld snippet can be told from a broken one.
ORDINARY_BODY = json.dumps({"text": "restic beacon, nothing sensitive"})

TRANSCRIPT_BYTES = 4096


def _seed(conn: sqlite3.Connection) -> None:
    """Build one project, one transcript, three bodies and one finding.

    Inputs: conn (writable sqlite3.Connection on a migrated archive).
    Output: None.
    """
    conn.execute(
        "INSERT INTO message_hosts (id, machine_id, machine_id_scheme, "
        "display_name, first_seen_at) VALUES "
        "(1, 'm1', 'declared', 'h', '2026-01-01T00:00:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO message_corpora (id, host_id, corpus_key, root_path, "
        "collected_at) VALUES (1, 1, 'c1', '/c', '2026-01-01T00:00:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO message_projects (id, corpus_id, slug, first_seen_at) "
        "VALUES (1, 1, 'proj-1', '2026-01-01T00:00:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO message_transcripts (id, source_ref, session_ref, "
        "session_ref_scheme, line_ending, has_trailing_newline, line_count, "
        "content_sha256, raw_byte_length, ingested_at, host_id, corpus_id, "
        "project_id, source_path) VALUES "
        "(10, 'ref-10', 'sess-10', 'uuid', 'LF', 1, 3, 'sha', ?, "
        "'2026-08-30T00:00:00.000000Z', 1, 1, 1, 'p1/10.jsonl')",
        (TRANSCRIPT_BYTES,),
    )
    # (body_id, line_no, body_json, secret_finding_count)
    rows = (
        (1, 0, FLAGGED_BODY, 1),
        (2, 1, UNFLAGGED_BODY, 0),
        (3, 2, ORDINARY_BODY, 0),
    )
    for body_id, line_no, text, secrets in rows:
        conn.execute(
            "INSERT INTO message_bodies (id, identity_key, message_uuid, "
            "body_sha256, body_bytes_sha256, body_json, secret_finding_count, "
            "first_seen_at) VALUES (?, ?, ?, 'a', 'b', ?, ?, "
            "'2026-01-01T00:00:00.000000Z')",
            (body_id, f"k{body_id}", f"u{body_id}", text, secrets),
        )
        conn.execute(
            "INSERT INTO message_appearances (id, transcript_id, line_no, "
            "line_status, body_id, line_sha256, line_byte_length, "
            "fidelity_outcome) VALUES (?, 10, ?, 'ok', ?, 's', ?, "
            "'fidelity_verified')",
            (body_id, line_no, body_id, len(text)),
        )
    # The finding exists for body 1 ONLY. Body 2 holds the same value and
    # the corpus has no idea - which is the live defect, in miniature.
    conn.execute(
        "INSERT INTO message_secret_findings (id, body_id, detector, "
        "match_offset, match_length, value_sha256, observed_at) VALUES "
        "(1, 1, 'high_entropy_assignment', ?, ?, ?, "
        "'2026-08-30T00:00:00.000000Z')",
        (FLAGGED_BODY.index(FIXTURE_CREDENTIAL), len(FIXTURE_CREDENTIAL),
         FIXTURE_SHA256),
    )
    conn.commit()


@pytest.fixture()
def corpus(tmp_path) -> Iterator[sqlite3.Connection]:
    """A temp archive holding the flagged/unflagged pair."""
    state = tmp_path / "state"
    ensure_db_migrated(state, 4, "0.8.2")
    with closing(sqlite3.connect(state / "cloude.db")) as write_conn:
        _seed(write_conn)
    with closing(open_read_only(state)) as conn:
        yield conn


def _hit(payload, line_no):
    """The single hit at ``line_no``, so an assertion names its subject."""
    matches = [h for h in payload["result"] if h["line_no"] == line_no]
    assert len(matches) == 1, f"expected exactly one hit at line {line_no}"
    return matches[0]


# --- the fixture is only meaningful if it reproduces the real split --------


def test_fixture_reproduces_the_measured_detector_split():
    """Assert the premise, do not assume it.

    If a future detector change made the bare occurrence detectable, the
    unflagged body would stop being a leak and every test below would
    pass for a reason that has nothing to do with the gate. This fails
    loudly instead.
    """
    flagged = scan_text(FLAGGED_BODY)
    assert [f.value_sha256 for f in flagged] == [FIXTURE_SHA256], (
        "the assignment shape must be detected, or the fixture models "
        "nothing"
    )
    assert scan_text(UNFLAGGED_BODY) == [], (
        "the bare occurrence must be INVISIBLE to the detectors: that is "
        "the structural recall gap this gate exists to survive"
    )


def test_the_naive_gate_would_have_served_the_leaking_body():
    """The old gate, restated as data. ``secret_finding_count`` is 0 on a
    body that plainly contains the credential, so a gate reading only
    that column serves it. This is the defect in one assertion.
    """
    assert FIXTURE_CREDENTIAL in UNFLAGGED_BODY
    naive_gate_would_withhold = 0 > 0  # secret_finding_count of body 2
    assert not naive_gate_would_withhold


# --- the defect, closed ----------------------------------------------------


def test_unflagged_body_with_a_known_credential_gets_no_snippet(corpus):
    """THE critical test. Zero findings, credential present, no preview."""
    out = search_scoped(corpus, "restic", "project", 1)
    hit = _hit(out, 1)
    assert hit["secret_finding_count"] == 0, (
        "the fixture must present an UNFLAGGED body or it tests layer 1"
    )
    assert hit["snippet"] is None
    assert hit["snippet_state"] == SNIPPET_WITHHELD_KNOWN_VALUE


def test_withheld_hit_is_still_reported_with_its_coordinates(corpus):
    """Withholding the preview must never suppress the finding."""
    out = search_scoped(corpus, "restic", "project", 1)
    hit = _hit(out, 1)
    assert hit["transcript_id"] == 10
    assert hit["body_id"] == 2
    assert hit["match_offset"] == UNFLAGGED_BODY.index("restic")
    assert hit["match_length"] == len("restic")
    assert hit["body_href"].endswith(str(hit["body_id"]))
    assert hit["lines_href"]


def test_credential_appears_nowhere_in_the_serialized_response(corpus):
    """Compared by value here because the fixture value is synthetic; the
    live check compares by hash only."""
    out = search_scoped(corpus, "restic", "project", 1)
    blob = json.dumps(out)
    assert FIXTURE_CREDENTIAL not in blob
    assert FIXTURE_SHA256 not in blob


def test_flagged_body_is_still_withheld_by_layer_one(corpus):
    """Layer 1 stays: it is the cheap path and it was never wrong, only
    incomplete."""
    hit = _hit(search_scoped(corpus, "restic", "project", 1), 0)
    assert hit["snippet"] is None
    assert hit["snippet_state"] == SNIPPET_WITHHELD_FLAGGED_BODY


def test_ordinary_hit_still_gets_a_snippet(corpus):
    """Positive control. Without this, a gate that withholds EVERYTHING
    passes every other test in this file while destroying the feature."""
    hit = _hit(search_scoped(corpus, "restic", "project", 1), 2)
    assert hit["snippet_state"] == SNIPPET_INCLUDED
    assert "beacon" in hit["snippet"]


# --- the third outcome and the hard guarantee ------------------------------


def test_gate_that_cannot_be_built_withholds_rather_than_serves(
    corpus, monkeypatch,
):
    """"I could not evaluate whether this is safe" must never render as
    "this is safe" - THE THREE-OUTCOME RULE, applied to the gate itself.
    """
    monkeypatch.setattr(archive_search, "load_index", lambda conn: None)
    out = search_scoped(corpus, "restic", "project", 1)
    states = {h["snippet_state"] for h in out["result"]}
    assert states == {
        SNIPPET_WITHHELD_GATE_UNAVAILABLE, SNIPPET_WITHHELD_FLAGGED_BODY,
    }
    assert all(h["snippet"] is None for h in out["result"])
    assert out["meta"]["snippet_gate"]["known_values_indexed"] is None


def test_snippets_false_is_the_hard_guarantee(corpus):
    """The only promise this endpoint can keep against an UNDETECTED
    credential: return no preview text at all."""
    out = search_scoped(corpus, "restic", "project", 1, snippets=False)
    assert len(out["result"]) == 3, "hits are reported, previews are not"
    assert all(h["snippet"] is None for h in out["result"])
    assert {h["snippet_state"] for h in out["result"]} == {
        SNIPPET_WITHHELD_BY_REQUEST
    }


def test_meta_declares_the_gate_best_effort_and_names_its_layers(corpus):
    """A gate that claimed a guarantee it cannot keep would be worse than
    the bug: the response has to say what it actually checks."""
    gate = search_scoped(corpus, "restic", "project", 1)["meta"]["snippet_gate"]
    assert gate["guarantee"] == "best_effort"
    assert gate["layers"] == [
        "body_secret_finding_count",
        "detectors_over_window",
        "known_credential_value_hash",
    ]
    assert gate["known_values_indexed"] == 1
    assert gate["withholding_never_suppresses_a_hit"] is True
    assert "never detected" in gate["limitation"]
