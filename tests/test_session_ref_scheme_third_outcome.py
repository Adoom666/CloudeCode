"""``session_ref_scheme`` has THREE outcomes, and the corpus proves it.

The classifier used to answer 'uuid' BY ELIMINATION - anything without an
agent prefix was called a uuid, and the uuid claim was never checked
against anything. 19 transcripts in the live corpus carry a ``session_ref``
that is a literal filename stem ('audit', 'journal'), five of them
local-agent permission-and-decision audit trails rather than conversations,
and all 19 were being counted in the owner's own "My sessions" view and in
the per-project session count on the project cards.

The tests here pin four separate things, because each could regress on its
own: the classifier's three answers, the CHECK relax being idempotent and
still ENFORCING (relaxed is not removed), the backfill touching EXACTLY the
rows that disagree and no others, and the sessions count moving by exactly
that many. The "which rows" assertions matter more than the counts: a bug
that reclassified the wrong 19 rows would satisfy a count assertion
perfectly.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.core.archive_project_names import SESSION_SCHEME_OWN
from src.core.message_model_serialize import (
    AGENT_SCHEME,
    OPAQUE_SCHEME,
    SESSION_REF_SCHEMES,
    UUID_SCHEME,
    session_ref_scheme,
)
from src.core.message_scheme_repair import (
    NEW_CHECK,
    OLD_CHECK,
    SchemeRepairError,
    backfill_opaque_scheme,
    check_allows_opaque,
    misclassified_rows,
    relax_scheme_check,
    repair_session_ref_schemes,
    stored_table_sql,
)
from tests.archive_fixture import make_state_dir, writable

#: A real ref from the live corpus, so the happy path is not a shape
#: invented to match the regex.
LIVE_UUID: str = "07e1cc0e-8a47-4029-8cfc-554f883ba28f"

#: The two literal stems the corpus actually holds, measured 2026-09-02.
LIVE_OPAQUE_REFS = ("audit", "journal")


# --- the classifier: three outcomes ---------------------------------------


def test_a_well_formed_uuid_is_the_uuid_scheme() -> None:
    """Description: the uuid answer is now MEASURED, not inferred.
    Inputs: none. Output: None.
    """
    assert session_ref_scheme(LIVE_UUID) == UUID_SCHEME
    # RFC 4122 says a reader accepts either case.
    assert session_ref_scheme(LIVE_UUID.upper()) == UUID_SCHEME


def test_both_agent_prefixes_are_the_agent_scheme() -> None:
    """Description: an agent id is not a malformed uuid; both live
      prefixes keep answering 'agent' and must never reach the new value.
    Inputs: none. Output: None.
    """
    assert session_ref_scheme("agent:a7b0a2e") == AGENT_SCHEME
    assert session_ref_scheme("agent-a00fdb4") == AGENT_SCHEME


@pytest.mark.parametrize("ref", LIVE_OPAQUE_REFS)
def test_a_filename_stem_is_neither_scheme(ref: str) -> None:
    """Description: the two refs that caused this, answered as the third
      outcome rather than silently promoted into the owner's sessions.
    Inputs: ref (str). Output: None.
    """
    assert session_ref_scheme(ref) == OPAQUE_SCHEME


@pytest.mark.parametrize(
    "ref",
    [
        "07e1cc0e8a4740298cfc554f883ba28f",  # undashed 32 hex
        "{07e1cc0e-8a47-4029-8cfc-554f883ba28f}",  # braced
        "urn:uuid:07e1cc0e-8a47-4029-8cfc-554f883ba28f",
        "07e1cc0e-8a47-4029-8cfc-554f883ba28",  # one digit short
        "07e1cc0e-8a47-4029-8cfc-554f883ba28ff",  # one digit long
        "07e1cc0e-8a47-4029-8cfc-554f883ba28g",  # non-hex digit
        "07e1cc0e-8a47-4029-8cfc-554f883ba28f\n",  # trailing newline
        " 07e1cc0e-8a47-4029-8cfc-554f883ba28f",  # leading space
        "",
    ],
)
def test_uuid_ish_but_malformed_is_opaque_not_uuid(ref: str) -> None:
    """Description: every near-miss the classifier could be tempted to
      wave through. The trailing-newline case is the reason the pattern is
      anchored with ``\\Z`` and not ``$`` - ``$`` matches before a final
      newline, so a ``$``-anchored pattern would call that a uuid.
      ``uuid.UUID()`` would accept the first three, which is exactly why
      it is not used.
    Inputs: ref (str). Output: None.
    """
    assert session_ref_scheme(ref) == OPAQUE_SCHEME


def test_the_classifier_can_only_answer_the_declared_schemes() -> None:
    """Description: the tuple the CHECK constraint mirrors is exactly what
      the classifier can produce, so a fourth answer cannot reach a column
      that would refuse it.
    Inputs: none. Output: None.
    """
    assert set(SESSION_REF_SCHEMES) == {UUID_SCHEME, AGENT_SCHEME, OPAQUE_SCHEME}
    for ref in (LIVE_UUID, "agent:x", "audit", "", "zzz"):
        assert session_ref_scheme(ref) in SESSION_REF_SCHEMES


# --- the migration --------------------------------------------------------


def _v19_shaped(tmp_path: Path) -> Path:
    """Build a current-schema state dir and put the OLD CHECK back.

    Description: ``make_state_dir`` migrates all the way to
      CURRENT_SCHEMA_VERSION, which already includes the relax. To test
      the step this reverses just that one edit, by the same in-place
      mechanism, so the fixture is a database that genuinely carries the
      pre-v20 constraint rather than one that merely claims to.
    Inputs: tmp_path (Path).
    Output: Path - the state dir.
    Example: _v19_shaped(tmp_path)
    """
    state_dir = make_state_dir(tmp_path)
    with closing(writable(state_dir)) as conn:
        sql = stored_table_sql(conn, "message_transcripts")
        assert NEW_CHECK in sql, "fixture precondition: migration already ran"
        with conn:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute(
                "UPDATE sqlite_master SET sql = ? WHERE type='table' "
                "AND name='message_transcripts'",
                (sql.replace(NEW_CHECK, OLD_CHECK),),
            )
            conn.execute("PRAGMA writable_schema=RESET")
    with closing(writable(state_dir)) as conn:
        assert OLD_CHECK in stored_table_sql(conn, "message_transcripts")
    return state_dir


def test_the_old_check_really_refuses_the_new_value(tmp_path: Path) -> None:
    """Description: the POSITIVE CONTROL for every migration test below. A
      backfill that "worked" against a database whose CHECK was never
      restrictive would prove nothing at all, so first prove the fixture
      refuses.
    Inputs: tmp_path (Path). Output: None.
    """
    state_dir = _v19_shaped(tmp_path)
    with closing(writable(state_dir)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO message_transcripts (source_ref, session_ref, "
                "session_ref_scheme, line_ending, has_trailing_newline, "
                "content_sha256, raw_byte_length, ingested_at) "
                "VALUES ('r','audit',?,'LF',1,'s',1,'t')",
                (OPAQUE_SCHEME,),
            )


def test_the_relax_is_idempotent_and_reports_it(tmp_path: Path) -> None:
    """Description: the first call edits, the second finds nothing to do
      and says False. False is the idempotent path, not a failure.
    Inputs: tmp_path (Path). Output: None.
    """
    state_dir = _v19_shaped(tmp_path)
    with closing(writable(state_dir)) as conn:
        assert check_allows_opaque(conn) is False
        with conn:
            assert relax_scheme_check(conn) is True
        assert check_allows_opaque(conn) is True
        with conn:
            assert relax_scheme_check(conn) is False
        assert check_allows_opaque(conn) is True


def test_the_relaxed_check_is_still_enforced(tmp_path: Path) -> None:
    """Description: THE NEGATIVE CONTROL. 'Relaxed' must not mean
      'removed'. A value outside the widened set is still refused, and the
      FK child rows are untouched because nothing was rebuilt.
    Inputs: tmp_path (Path). Output: None.
    """
    state_dir = _v19_shaped(tmp_path)
    with closing(writable(state_dir)) as conn:
        with conn:
            relax_scheme_check(conn)
        with conn:
            conn.execute(
                "INSERT INTO message_transcripts (source_ref, session_ref, "
                "session_ref_scheme, line_ending, has_trailing_newline, "
                "content_sha256, raw_byte_length, ingested_at) "
                "VALUES ('r','audit',?,'LF',1,'s',1,'t')",
                (OPAQUE_SCHEME,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO message_transcripts (source_ref, session_ref, "
                "session_ref_scheme, line_ending, has_trailing_newline, "
                "content_sha256, raw_byte_length, ingested_at) "
                "VALUES ('r2','x','not-a-scheme','LF',1,'s',1,'t')"
            )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_an_unrecognised_check_refuses_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """Description: the third outcome of the STEP itself. Faced with a
      constraint it does not recognise it raises, which rolls the caller's
      transaction back, rather than editing a live table's schema on a
      guess.
    Inputs: tmp_path (Path). Output: None.
    """
    state_dir = make_state_dir(tmp_path)
    with closing(writable(state_dir)) as conn:
        sql = stored_table_sql(conn, "message_transcripts")
        with conn:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute(
                "UPDATE sqlite_master SET sql = ? WHERE type='table' "
                "AND name='message_transcripts'",
                (sql.replace(NEW_CHECK, "CHECK (session_ref_scheme IN ('zz'))"),),
            )
            conn.execute("PRAGMA writable_schema=RESET")
        with pytest.raises(SchemeRepairError):
            relax_scheme_check(conn)


# --- the backfill, and the numbers it moves -------------------------------


def _seed_mixed(conn: sqlite3.Connection) -> dict:
    """Seed one transcript per interesting shape and return their ids.

    Description: two stems that must move, three refs that must NOT -
      a real uuid, an agent ref, and a second agent ref whose suffix is
      not uuid-shaped. Every row is written with the scheme the OLD
      two-way classifier would have produced, which is what a real
      pre-migration database holds.
    Inputs: conn (sqlite3.Connection).
    Output: dict - name -> transcript id.
    Example: _seed_mixed(conn)["audit"]
    """
    ids = {}
    rows = (
        ("audit", "audit", UUID_SCHEME),
        ("journal", "journal", UUID_SCHEME),
        ("real_uuid", LIVE_UUID, UUID_SCHEME),
        ("agent_colon", "agent:a7b0a2e", AGENT_SCHEME),
        ("agent_dash", "agent-a00fdb4", AGENT_SCHEME),
    )
    for name, ref, scheme in rows:
        cur = conn.execute(
            "INSERT INTO message_transcripts (source_ref, session_ref, "
            "session_ref_scheme, line_ending, has_trailing_newline, "
            "content_sha256, raw_byte_length, ingested_at, project_id) "
            "VALUES (?,?,?,'LF',1,?,1,'t',1)",
            (f"ref::{name}", ref, scheme, f"sha-{name}"),
        )
        ids[name] = int(cur.lastrowid)
    return ids


def test_the_backfill_moves_exactly_the_disagreeing_rows(
    tmp_path: Path,
) -> None:
    """Description: WHICH rows, not just how many. A bug that reclassified
      the wrong rows would pass a count assertion perfectly, so the two
      agent rows and the genuine uuid row are asserted to be UNCHANGED by
      id, and the two stems asserted to have moved by id.
    Inputs: tmp_path (Path). Output: None.
    """
    state_dir = _v19_shaped(tmp_path)
    with closing(writable(state_dir)) as conn:
        with conn:
            ids = _seed_mixed(conn)
        assert [r[0] for r in misclassified_rows(conn)] == [
            ids["audit"], ids["journal"]
        ]
        with conn:
            relax_scheme_check(conn)
            assert backfill_opaque_scheme(conn) == 2
        after = {
            int(r[0]): str(r[1])
            for r in conn.execute(
                "SELECT id, session_ref_scheme FROM message_transcripts"
            )
        }
    assert after[ids["audit"]] == OPAQUE_SCHEME
    assert after[ids["journal"]] == OPAQUE_SCHEME
    assert after[ids["real_uuid"]] == UUID_SCHEME
    assert after[ids["agent_colon"]] == AGENT_SCHEME
    assert after[ids["agent_dash"]] == AGENT_SCHEME


def test_the_backfill_is_idempotent(tmp_path: Path) -> None:
    """Description: a second run finds nothing to correct, because the
      pass is defined as a disagreement rather than as a list of refs.
    Inputs: tmp_path (Path). Output: None.
    """
    state_dir = _v19_shaped(tmp_path)
    with closing(writable(state_dir)) as conn:
        with conn:
            _seed_mixed(conn)
        with conn:
            assert repair_session_ref_schemes(conn) == (True, 2)
        with conn:
            assert repair_session_ref_schemes(conn) == (False, 0)


def test_the_sessions_count_drops_by_exactly_the_rows_that_moved(
    tmp_path: Path,
) -> None:
    """Description: the consequence the owner actually sees. The per-project
      session count is ``COUNT(session_ref_scheme = 'uuid')``, so it must
      fall by exactly the number of rows the backfill moved - and the
      TOTAL transcript count must not move at all, because nothing was
      deleted.
    Inputs: tmp_path (Path). Output: None.
    """
    state_dir = _v19_shaped(tmp_path)
    with closing(writable(state_dir)) as conn:
        with conn:
            _seed_mixed(conn)
        sessions = (
            "SELECT COUNT(*) FROM message_transcripts "
            f"WHERE session_ref_scheme = '{SESSION_SCHEME_OWN}'"
        )
        total = "SELECT COUNT(*) FROM message_transcripts"
        before_sessions = conn.execute(sessions).fetchone()[0]
        before_total = conn.execute(total).fetchone()[0]
        with conn:
            _relaxed, moved = repair_session_ref_schemes(conn)
        assert before_sessions - conn.execute(sessions).fetchone()[0] == moved
        assert conn.execute(total).fetchone()[0] == before_total


def test_the_step_is_a_no_op_without_the_message_archive(
    tmp_path: Path,
) -> None:
    """Description: an install that crossed v16..v18 with the archive gated
      off has no message_transcripts at all. The step must return quietly
      rather than raising, or that install can never migrate.
    Inputs: tmp_path (Path). Output: None.
    """
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    with closing(conn):
        assert repair_session_ref_schemes(conn) == (False, 0)
        assert misclassified_rows(conn) == []
        assert check_allows_opaque(conn) is False
