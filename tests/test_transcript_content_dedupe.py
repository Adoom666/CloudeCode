"""Tests for src/core/transcript_content_dedupe.py and the content-addressed
idempotency key it gives src/core/transcript_corpus_ingest.py.

THE DEFECT THESE LOCK DOWN. The ingester's key was ``(source_path,
content_sha256)`` evaluated PATH FIRST, so the hash was only ever compared
when the path lookup hit. A file whose bytes were already stored, arriving
under a new path, produced ``existing = None`` and was copied in full. When
``~/Development`` became a symlink every corpus slug changed at once and
19,294 files / 3.78 GB were archived a second time, silently.

Covered here: a known hash at a NEW path is recognised; the row it writes is
metadata-only and its shape is asserted field by field; the bytes still
export byte-exactly through the supersession chain; the index exists; the
mass-re-archive finding is emitted past the threshold and NOT below it; and
prefix dedupe still works for a genuinely GROWN file, including a grown file
whose predecessor was itself a content duplicate (the two mechanisms share
``superseded_by_archive_id`` and must compose).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import DEDUPE_KIND_CONTENT_DUPLICATE
from src.core.message_gate_contract import (
    BY_CODE,
    GATE_CONTENT_DUPLICATE_MASS_REARCHIVE,
)
from src.core.transcript_archive import export_archive
from src.core.transcript_content_dedupe import (
    MASS_REARCHIVE_THRESHOLD,
    find_archive_by_content,
    mass_rearchive_detail,
)
from src.core.transcript_corpus_discover import CorpusEntry
from src.core.transcript_corpus_ingest import (
    RunReport,
    _record_mass_rearchive_finding,
    ingest_one,
)
from src.core.transcript_prefix_dedupe import verify_stored_hash

CONTENT = (
    b'{"type":"user","uuid":"a","sessionId":"sess-1"}\n'
    b'{"type":"assistant","uuid":"b","sessionId":"sess-1"}\n'
)


@pytest.fixture
def conn(tmp_path):
    """A migrated datastore at the current schema version."""
    state = tmp_path / "state"
    state.mkdir()
    ensure_db_migrated(state, 4, "0.8.2")
    c = connect(db_path_for(state))
    yield c
    c.close()


def _entry(root: Path, slug: str, name: str, content: bytes) -> CorpusEntry:
    """Write one transcript into a slug directory and describe it."""
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_bytes(content)
    return CorpusEntry(
        abs_path=path, source_path=f"{slug}/{name}", kind="session"
    )


# ---------------------------------------------------------------------
# 1. The index. Without it every pass is a full table scan per file.
# ---------------------------------------------------------------------


def test_content_sha256_is_indexed(conn):
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='transcript_archives'"
        )
    }
    assert "ix_transcript_archives_content_sha" in names


def test_the_index_actually_covers_a_content_lookup(conn):
    """An index that exists and is not USED buys nothing - assert the plan."""
    plan = " ".join(
        str(r[3])
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM transcript_archives"
            " WHERE content_sha256 = ?",
            ("deadbeef",),
        )
    )
    assert "ix_transcript_archives_content_sha" in plan, plan


# ---------------------------------------------------------------------
# 2. THE FIX ITSELF: a known hash at a NEW path is recognised.
# ---------------------------------------------------------------------


def test_a_known_hash_at_a_new_path_is_recognised_not_recopied(conn, tmp_path):
    root = tmp_path / "corpus"
    first = ingest_one(conn, _entry(root, "slugA", "s.jsonl", CONTENT))
    assert first.outcome == "ingested"
    assert first.dedupe_kind is None, "the first ingest is not a duplicate"

    second = ingest_one(conn, _entry(root, "slugB", "s.jsonl", CONTENT))
    assert second.outcome == "ingested", (
        "a new source_path IS a new archive row - it is not 'already_present'"
    )
    assert second.dedupe_kind == DEDUPE_KIND_CONTENT_DUPLICATE
    assert second.bytes_not_restored == len(CONTENT)
    assert second.archive_id != first.archive_id


def test_the_duplicate_row_stores_no_second_copy_of_the_bytes(conn, tmp_path):
    root = tmp_path / "corpus"
    first = ingest_one(conn, _entry(root, "slugA", "s.jsonl", CONTENT))
    second = ingest_one(conn, _entry(root, "slugB", "s.jsonl", CONTENT))

    a = dict(
        conn.execute(
            "SELECT * FROM transcript_archives WHERE id = ?",
            (first.archive_id,),
        ).fetchone()
    )
    b = dict(
        conn.execute(
            "SELECT * FROM transcript_archives WHERE id = ?",
            (second.archive_id,),
        ).fetchone()
    )

    # The whole point: the second row's blob is a sentinel, not a copy.
    assert len(b["content_gzip"]) < 20, (
        f"the duplicate must store a sentinel, got "
        f"{len(b['content_gzip'])} bytes"
    )
    assert len(b["content_gzip"]) < len(a["content_gzip"])
    assert b["compressed_byte_length"] == len(b["content_gzip"])
    assert b["superseded_by_archive_id"] == first.archive_id
    assert b["dedupe_kind"] == DEDUPE_KIND_CONTENT_DUPLICATE
    # It IS the first archive for its own source_path, so 'initial' is the
    # honest growth_kind - see the module docstring.
    assert b["growth_kind"] == "initial"
    assert b["source_path"] == "slugB/s.jsonl"
    assert b["archive_uuid"] != a["archive_uuid"]
    # Facts about the CONTENT are the same measurement, not a default.
    for column in (
        "content_sha256",
        "raw_byte_length",
        "line_ending",
        "has_trailing_newline",
        "record_count",
        "claude_session_uuid",
    ):
        assert b[column] == a[column], column
    # The FIRST row must be untouched - a dedupe that mutates the row it
    # points at is a dedupe that can lose the only copy.
    assert a["superseded_by_archive_id"] is None
    assert a["dedupe_kind"] is None


def test_the_duplicate_row_still_exports_byte_exactly(conn, tmp_path):
    root = tmp_path / "corpus"
    ingest_one(conn, _entry(root, "slugA", "s.jsonl", CONTENT))
    second = ingest_one(conn, _entry(root, "slugB", "s.jsonl", CONTENT))
    assert export_archive(conn, second.archive_id) == CONTENT
    assert verify_stored_hash(conn, second.archive_id).outcome == "hash_verified"


def test_the_duplicate_row_carries_its_own_line_index(conn, tmp_path):
    """Every transcript_records reader must work against a deduped row with
    no special case - which is only true if the index was copied."""
    root = tmp_path / "corpus"
    first = ingest_one(conn, _entry(root, "slugA", "s.jsonl", CONTENT))
    second = ingest_one(conn, _entry(root, "slugB", "s.jsonl", CONTENT))
    rows_a = conn.execute(
        "SELECT line_no, byte_offset, byte_length, status, record_uuid"
        " FROM transcript_records WHERE archive_id = ? ORDER BY line_no",
        (first.archive_id,),
    ).fetchall()
    rows_b = conn.execute(
        "SELECT line_no, byte_offset, byte_length, status, record_uuid"
        " FROM transcript_records WHERE archive_id = ? ORDER BY line_no",
        (second.archive_id,),
    ).fetchall()
    assert len(rows_a) == 2
    assert [tuple(r) for r in rows_a] == [tuple(r) for r in rows_b]


def test_re_running_the_same_path_is_still_a_plain_no_op(conn, tmp_path):
    root = tmp_path / "corpus"
    ingest_one(conn, _entry(root, "slugA", "s.jsonl", CONTENT))
    entry_b = _entry(root, "slugB", "s.jsonl", CONTENT)
    ingest_one(conn, entry_b)
    again = ingest_one(conn, entry_b)
    assert again.outcome == "already_present", (
        "the path-scoped fast path must still settle an unchanged file"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM transcript_archives"
    ).fetchone()[0] == 2


def test_genuinely_new_content_is_not_a_duplicate(conn, tmp_path):
    """The negative control. A key that calls everything a duplicate is
    just as broken as one that calls nothing a duplicate."""
    root = tmp_path / "corpus"
    ingest_one(conn, _entry(root, "slugA", "s.jsonl", CONTENT))
    other = ingest_one(
        conn, _entry(root, "slugC", "s.jsonl", b'{"type":"user","uuid":"z"}\n')
    )
    assert other.dedupe_kind is None
    assert other.bytes_not_restored == 0
    row = conn.execute(
        "SELECT superseded_by_archive_id, length(content_gzip) AS n"
        " FROM transcript_archives WHERE id = ?",
        (other.archive_id,),
    ).fetchone()
    assert row["superseded_by_archive_id"] is None
    assert row["n"] > 20, "genuinely new content must be stored in full"


def test_find_archive_by_content_returns_none_for_an_unknown_hash(conn):
    assert find_archive_by_content(conn, "0" * 64) is None


def test_find_archive_by_content_prefers_a_row_that_holds_real_content(
    conn, tmp_path
):
    """A duplicate must attach to the row holding the bytes, not to another
    sentinel - otherwise every duplicate lengthens the chain by one."""
    root = tmp_path / "corpus"
    first = ingest_one(conn, _entry(root, "slugA", "s.jsonl", CONTENT))
    second = ingest_one(conn, _entry(root, "slugB", "s.jsonl", CONTENT))
    third = ingest_one(conn, _entry(root, "slugC", "s.jsonl", CONTENT))
    for archive_id in (second.archive_id, third.archive_id):
        assert conn.execute(
            "SELECT superseded_by_archive_id FROM transcript_archives"
            " WHERE id = ?",
            (archive_id,),
        ).fetchone()[0] == first.archive_id
    assert export_archive(conn, third.archive_id) == CONTENT


# ---------------------------------------------------------------------
# 3. PREFIX DEDUPE MUST STILL WORK. A grown file has a hash nothing has
#    seen, so it must never take the content-duplicate branch.
# ---------------------------------------------------------------------


def test_a_genuinely_grown_file_still_takes_the_prefix_dedupe_path(
    conn, tmp_path
):
    root = tmp_path / "corpus"
    entry = _entry(root, "slugA", "s.jsonl", CONTENT)
    first = ingest_one(conn, entry)
    grown = CONTENT + b'{"type":"user","uuid":"c"}\n'
    entry.abs_path.write_bytes(grown)
    second = ingest_one(conn, entry)

    assert second.growth_kind == "append"
    assert second.dedupe_kind is None, (
        "growth is not duplication - a grown file must be stored in full"
    )
    # The OLD row is now the sentinel, pointing FORWARD at the new one.
    old = conn.execute(
        "SELECT superseded_by_archive_id, length(content_gzip) AS n"
        " FROM transcript_archives WHERE id = ?",
        (first.archive_id,),
    ).fetchone()
    assert old["superseded_by_archive_id"] == second.archive_id
    assert old["n"] < 20
    assert export_archive(conn, first.archive_id) == CONTENT
    assert export_archive(conn, second.archive_id) == grown


def test_the_two_mechanisms_compose_over_a_shared_chain(conn, tmp_path):
    """A content duplicate whose target LATER grows, and a content duplicate
    that ITSELF later grows. Both walk the same supersession pointer, so
    either one composing wrongly loses history."""
    root = tmp_path / "corpus"
    a = _entry(root, "slugA", "s.jsonl", CONTENT)
    b = _entry(root, "slugB", "s.jsonl", CONTENT)
    first = ingest_one(conn, a)
    dup = ingest_one(conn, b)
    assert dup.dedupe_kind == DEDUPE_KIND_CONTENT_DUPLICATE

    grown_a = CONTENT + b'{"type":"user","uuid":"c"}\n'
    a.abs_path.write_bytes(grown_a)
    third = ingest_one(conn, a)
    assert third.growth_kind == "append"

    # dup -> first -> third, sliced back to dup's own length.
    assert export_archive(conn, dup.archive_id) == CONTENT
    assert export_archive(conn, first.archive_id) == CONTENT
    assert verify_stored_hash(conn, dup.archive_id).outcome == "hash_verified"

    grown_b = CONTENT + b'{"type":"user","uuid":"d"}\n'
    b.abs_path.write_bytes(grown_b)
    fourth = ingest_one(conn, b)
    assert fourth.growth_kind == "append", (
        "a content duplicate must still be a usable prefix baseline"
    )
    assert export_archive(conn, fourth.archive_id) == grown_b
    assert export_archive(conn, dup.archive_id) == CONTENT


# ---------------------------------------------------------------------
# 4. THE FINDING. A mass re-archive was completely silent; now it is not.
# ---------------------------------------------------------------------


def test_the_gate_condition_is_registered_and_advisory():
    assert GATE_CONTENT_DUPLICATE_MASS_REARCHIVE in BY_CODE
    assert BY_CODE[GATE_CONTENT_DUPLICATE_MASS_REARCHIVE].severity == "advisory"


def _findings(conn):
    return conn.execute(
        "SELECT condition_code, severity, subject_kind, subject_id, detail"
        " FROM message_ingest_findings"
    ).fetchall()


def test_a_mass_rearchive_emits_exactly_one_advisory_finding(conn):
    report = RunReport(
        content_duplicates=MASS_REARCHIVE_THRESHOLD + 1,
        bytes_not_restored=4_057_960_448,
    )
    assert _record_mass_rearchive_finding(conn, report, ["slugB/s.jsonl"]) is True
    rows = _findings(conn)
    assert len(rows) == 1
    assert rows[0]["condition_code"] == GATE_CONTENT_DUPLICATE_MASS_REARCHIVE
    assert rows[0]["severity"] == "advisory"
    assert rows[0]["subject_kind"] == "transcript"
    assert rows[0]["subject_id"] == MASS_REARCHIVE_THRESHOLD + 1
    assert str(MASS_REARCHIVE_THRESHOLD + 1) in rows[0]["detail"]
    assert "slugB/s.jsonl" in rows[0]["detail"]


def test_a_handful_of_duplicates_emits_NOTHING(conn):
    """A finding on every pass is furniture, not a monitor. A copied file or
    a second checkout is ordinary shape and must stay quiet."""
    report = RunReport(content_duplicates=1, bytes_not_restored=59)
    assert _record_mass_rearchive_finding(conn, report, ["x"]) is False
    assert _findings(conn) == []


def test_the_threshold_boundary_is_exclusive_and_measured(conn):
    at = RunReport(content_duplicates=MASS_REARCHIVE_THRESHOLD)
    assert _record_mass_rearchive_finding(conn, at, []) is False
    over = RunReport(content_duplicates=MASS_REARCHIVE_THRESHOLD + 1)
    assert _record_mass_rearchive_finding(conn, over, []) is True
    assert len(_findings(conn)) == 1


def test_a_pass_with_no_duplicates_at_all_emits_nothing(conn):
    assert _record_mass_rearchive_finding(conn, RunReport(), []) is False
    assert _findings(conn) == []


def test_the_finding_detail_is_never_blank_and_names_the_numbers():
    detail = mass_rearchive_detail(
        duplicate_count=19294,
        bytes_not_restored=4_057_960_448,
        sample_paths=["a/b.jsonl", "c/d.jsonl", "e/f.jsonl", "g/h.jsonl"],
    )
    assert detail
    assert "19294" in detail
    assert "4057960448" in detail
    # At most three examples, so a 19,294-file pass does not write an essay.
    assert detail.count(".jsonl") == 3


def test_an_unrecordable_finding_never_kills_the_ingest(conn):
    """The storage this would abort is the thing being protected. A database
    that cannot record an observation must not lose the pass that made it."""
    conn.execute("DROP TABLE message_ingest_findings")
    report = RunReport(content_duplicates=MASS_REARCHIVE_THRESHOLD + 1)
    assert _record_mass_rearchive_finding(conn, report, []) is False
