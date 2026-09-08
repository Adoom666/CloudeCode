"""Tests for the app-side incremental corpus ingester and its liveness.

Covers, in the order the feature's guarantees were stated:
  * the scan cache skips only when the cache, the filesystem AND the
    database all agree, and can therefore only ever cause extra work;
  * a second run over an unchanged corpus writes nothing and hashes
    nothing;
  * a file that GREW is picked up and stored as an append, not a
    duplicate;
  * an unreadable file is counted in its own bucket and never fails the
    run;
  * every failure path resolves to a NAMED status - absent corpus,
    absent datastore, too-old schema - and none of them raises;
  * the liveness artifact is published on every terminating path, and
    its age (never its absence) is the freshness signal;
  * the four freshness outcomes are all reachable and ``current`` is
    only ever returned when an age was actually measured.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core import corpus_ingest_state as state_io
from src.core import corpus_status
from src.core.corpus_ingest_service import (
    STATUS_CANCELLED,
    STATUS_CORPUS_ABSENT,
    STATUS_DATASTORE_UNAVAILABLE,
    STATUS_OK,
    STATUS_SCHEMA_TOO_OLD,
    CorpusIngestReport,
    resolve_corpus_root,
    run_ingest_once,
)
from src.core.corpus_ingest_scan import plan_scan
from src.core.corpus_ingest_task import (
    CorpusIngestScheduler,
    ingest_enabled,
    resolve_interval_seconds,
)
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_steps import run_chain
from src.core.transcript_corpus_discover import discover_corpus

SESSION_UUID = "11111111-1111-1111-1111-111111111111"


def _make_state(tmp_path: Path) -> Path:
    """Create a state dir holding a datastore at the current schema.

    Inputs: tmp_path (Path).
    Output: Path - the state dir.
    Example: _make_state(tmp_path) / "cloude.db"
    """
    state = tmp_path / "state"
    ensure_db_migrated(state, 4, "0.8.2")
    return state


def _make_corpus(root: Path) -> Path:
    """Write a small corpus: one session plus one of its subagents.

    Inputs: root (Path) - corpus root, created here.
    Output: Path - the session transcript's path.
    Example: _make_corpus(tmp_path / "corpus")
    """
    slug = root / "-Users-x-proj"
    sub = slug / SESSION_UUID / "subagents"
    sub.mkdir(parents=True)
    session_file = slug / f"{SESSION_UUID}.jsonl"
    session_file.write_bytes(
        b'{"type":"user","uuid":"u1","sessionId":"'
        + SESSION_UUID.encode()
        + b'","timestamp":"2026-08-29T00:00:00.000Z"}\n'
    )
    (sub / "agent-aaa.jsonl").write_bytes(b'{"type":"user","uuid":"a1"}\n')
    return session_file


# --------------------------------------------------------------------------
# The happy path, and what "incremental" actually means
# --------------------------------------------------------------------------


def test_first_run_ingests_and_publishes_liveness(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)

    report = run_ingest_once(state, corpus_root=corpus)

    assert report.status == STATUS_OK
    assert report.discovered == 2
    assert report.ingested == 2
    assert report.skipped_unchanged == 0
    assert report.could_not_read == 0
    record = state_io.read_liveness(state)
    assert record is not None
    assert record["status"] == STATUS_OK
    assert record["ingested"] == 2


def test_second_run_is_a_no_op_and_skips_by_cache(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    run_ingest_once(state, corpus_root=corpus)

    second = run_ingest_once(state, corpus_root=corpus)

    assert second.status == STATUS_OK
    assert second.ingested == 0
    # The point of the cache: nothing was hashed, so nothing reached the
    # already_present branch either. Both counts matter.
    assert second.skipped_unchanged == 2
    assert second.already_present == 0


def test_a_grown_file_is_picked_up_as_an_append(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    session_file = _make_corpus(corpus)
    run_ingest_once(state, corpus_root=corpus)

    with open(session_file, "ab") as handle:
        handle.write(b'{"type":"assistant","uuid":"u2"}\n')

    third = run_ingest_once(state, corpus_root=corpus)

    assert third.ingested == 1
    assert third.skipped_unchanged == 1
    assert third.growth_kinds.get("append") == 1


def test_a_deleted_file_drops_out_of_the_cache(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    session_file = _make_corpus(corpus)
    run_ingest_once(state, corpus_root=corpus)
    session_file.unlink()

    report = run_ingest_once(state, corpus_root=corpus)

    assert report.discovered == 1
    cache = state_io.load_scan_cache(state)
    assert str(session_file.name) not in " ".join(cache)


def test_an_unreadable_file_is_counted_not_fatal(tmp_path):
    if os.geteuid() == 0:  # pragma: no cover - root ignores mode bits
        pytest.skip("running as root: chmod 000 does not deny reads")
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    bad = corpus / "-Users-x-proj" / "99999999-9999-9999-9999-999999999999.jsonl"
    bad.write_bytes(b'{"type":"user","uuid":"z"}\n')
    os.chmod(bad, 0o000)
    try:
        report = run_ingest_once(state, corpus_root=corpus)
    finally:
        os.chmod(bad, 0o600)

    assert report.status == STATUS_OK
    assert report.could_not_read == 1
    assert report.ingested == 2
    assert report.could_not_read_detail[0]["source_path"].endswith(bad.name)


def test_cancel_stops_between_files_and_is_named(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    cancel = Event()
    cancel.set()

    report = run_ingest_once(state, corpus_root=corpus, cancel=cancel)

    assert report.status == STATUS_CANCELLED
    assert report.ingested == 0
    assert report.not_reached == 2
    # A cancelled run still publishes: its silence would otherwise be
    # indistinguishable from a dead scheduler.
    assert state_io.read_liveness(state)["status"] == STATUS_CANCELLED


# --------------------------------------------------------------------------
# Every failure path is a named status, and none of them raises
# --------------------------------------------------------------------------


def test_missing_corpus_directory_is_named_not_fatal(tmp_path):
    state = _make_state(tmp_path)

    report = run_ingest_once(state, corpus_root=tmp_path / "no-such-corpus")

    assert report.status == STATUS_CORPUS_ABSENT
    assert "does not exist" in report.reason
    assert state_io.read_liveness(state)["status"] == STATUS_CORPUS_ABSENT


def test_missing_datastore_is_named_not_fatal(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    state = tmp_path / "empty-state"
    state.mkdir()

    report = run_ingest_once(state, corpus_root=corpus)

    assert report.status == STATUS_DATASTORE_UNAVAILABLE
    assert state_io.read_liveness(state)["status"] == STATUS_DATASTORE_UNAVAILABLE


def test_schema_below_the_archive_tables_is_named(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    state = tmp_path / "old-state"
    state.mkdir()
    conn = connect(db_path_for(state))
    with conn:
        run_chain(conn, 0, 11)
    conn.close()

    report = run_ingest_once(state, corpus_root=corpus)

    assert report.status == STATUS_SCHEMA_TOO_OLD
    assert report.schema_version == 11


# --------------------------------------------------------------------------
# plan_scan: the cache may only ever cost extra work
# --------------------------------------------------------------------------


def test_plan_scan_needs_all_three_to_agree(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    entries = discover_corpus(corpus)
    entry = next(e for e in entries if e.kind == "session")
    info = entry.abs_path.stat()
    good = {entry.source_path: (info.st_size, info.st_mtime_ns, "sha-x")}

    assert plan_scan([entry], good, {entry.source_path: "sha-x"}) == ([], 1)
    # cache absent
    assert plan_scan([entry], {}, {entry.source_path: "sha-x"})[1] == 0
    # stat disagrees
    stale = {entry.source_path: (info.st_size + 1, info.st_mtime_ns, "sha-x")}
    assert plan_scan([entry], stale, {entry.source_path: "sha-x"})[1] == 0
    # database disagrees (the row was lost, restored, or never written)
    assert plan_scan([entry], good, {})[1] == 0
    assert plan_scan([entry], good, {entry.source_path: "other"})[1] == 0


def test_resolve_corpus_root_honours_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDE_CORPUS_ROOT", str(tmp_path / "elsewhere"))
    assert resolve_corpus_root() == tmp_path / "elsewhere"
    monkeypatch.delenv("CLOUDE_CORPUS_ROOT")
    assert resolve_corpus_root().name == "projects"


# --------------------------------------------------------------------------
# Liveness and freshness: four outcomes, and age is the signal
# --------------------------------------------------------------------------


def test_freshness_has_four_outcomes(tmp_path):
    now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

    assert state_io.classify_freshness(None, now=now)[0] == (
        state_io.FRESHNESS_NEVER_RAN
    )
    assert state_io.classify_freshness({"finished_at": "nonsense"}, now=now)[0] == (
        state_io.FRESHNESS_CANNOT_DETERMINE
    )
    fresh = {"finished_at": (now - timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")}
    assert state_io.classify_freshness(fresh, now=now)[0] == (
        state_io.FRESHNESS_CURRENT
    )
    old = {"finished_at": (now - timedelta(days=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")}
    assert state_io.classify_freshness(old, now=now)[0] == (
        state_io.FRESHNESS_STALE
    )
    ahead = {"finished_at": (now + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")}
    verdict, age, _ = state_io.classify_freshness(ahead, now=now)
    assert verdict == state_io.FRESHNESS_CANNOT_DETERMINE
    assert age is not None and age < 0


def test_a_corrupt_scan_cache_reads_as_empty_never_as_coverage(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    path = state_io.scan_cache_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")

    assert state_io.load_scan_cache(state) == {}


def test_scan_cache_round_trips(tmp_path):
    state = tmp_path / "state"
    assert state_io.save_scan_cache(state, {"a/b.jsonl": (1, 2, "sha")})
    assert state_io.load_scan_cache(state) == {"a/b.jsonl": (1, 2, "sha")}


def test_liveness_write_keeps_a_dated_copy(tmp_path):
    state = tmp_path / "state"
    record = CorpusIngestReport(finished_at="2026-08-30T12:00:00Z").to_record()

    assert state_io.write_liveness(state, record)
    assert state_io.read_liveness(state)["finished_at"] == "2026-08-30T12:00:00Z"
    assert state_io.dated_records(state) == ["run-20260830T120000Z.json"]


def test_dated_records_are_pruned_to_the_retention_cap(tmp_path):
    state = tmp_path / "state"
    for minute in range(state_io.DATED_RETENTION + 5):
        state_io.write_liveness(
            state, {"finished_at": f"2026-08-30T12:{minute:02d}:00Z"},
        )
    assert len(state_io.dated_records(state)) <= state_io.DATED_RETENTION


# --------------------------------------------------------------------------
# The status surface
# --------------------------------------------------------------------------


def test_status_never_ran_is_attention_not_ok(tmp_path):
    state = _make_state(tmp_path)

    status = corpus_status.build_status(state)

    assert status["freshness"]["verdict"] == state_io.FRESHNESS_NEVER_RAN
    assert status["overall"]["verdict"] == corpus_status.OVERALL_ATTENTION
    assert status["archive"]["status"] == "measured"
    assert status["scheduler"]["enabled"] is None


def test_status_after_a_run_is_ok_and_reports_counts(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    run_ingest_once(state, corpus_root=corpus)

    status = corpus_status.build_status(state)

    assert status["freshness"]["verdict"] == state_io.FRESHNESS_CURRENT
    assert status["overall"]["verdict"] == corpus_status.OVERALL_OK
    assert status["archive"]["archive_rows"] == 2
    assert status["last_run"]["ingested"] == 2


def test_status_on_an_unopenable_datastore_cannot_determine(tmp_path):
    state = tmp_path / "nothing-here"

    status = corpus_status.build_status(state)

    assert status["archive"]["status"] == corpus_status.OVERALL_CANNOT_DETERMINE
    assert status["overall"]["verdict"] == corpus_status.OVERALL_CANNOT_DETERMINE


def test_empty_findings_on_an_unpopulated_model_is_not_a_pass(tmp_path):
    state = _make_state(tmp_path)

    block = corpus_status.build_status(state)["gate_findings"]

    assert block["status"] == "model_not_populated"
    assert block["by_condition"] == []
    assert "evidence of nothing" in block["reason"]


def test_gate_findings_roll_up_by_condition(tmp_path):
    state = _make_state(tmp_path)
    conn = connect(db_path_for(state))
    with conn:
        conn.execute(
            "INSERT INTO message_transcripts (source_ref, session_ref,"
            " session_ref_scheme, line_ending, has_trailing_newline,"
            " line_count, content_sha256, raw_byte_length, ingested_at)"
            " VALUES ('r', 's', 'uuid', 'LF', 1, 0, 'x', 0, 'now')"
        )
        for code in ("dangling_parent", "dangling_parent", "ordering_anomaly"):
            conn.execute(
                "INSERT INTO message_ingest_findings (observed_at,"
                " condition_code, severity, subject_kind, subject_id, detail)"
                " VALUES ('now', ?, 'advisory', 'transcript', 1, 'd')",
                (code,),
            )
    conn.close()

    block = corpus_status.build_status(state)["gate_findings"]

    assert block["status"] == "measured"
    assert block["total_findings"] == 3
    counts = {row["condition_code"]: row["count"] for row in block["by_condition"]}
    assert counts == {"dangling_parent": 2, "ordering_anomaly": 1}


# --------------------------------------------------------------------------
# The scheduler
# --------------------------------------------------------------------------


def test_ingest_is_off_by_default_under_test_mode(monkeypatch):
    monkeypatch.delenv("CLOUDE_CORPUS_INGEST", raising=False)
    monkeypatch.setenv("CLOUDE_TEST_MODE", "1")
    assert ingest_enabled() is False
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST", "1")
    assert ingest_enabled() is True
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST", "off")
    assert ingest_enabled() is False
    monkeypatch.delenv("CLOUDE_CORPUS_INGEST")
    monkeypatch.delenv("CLOUDE_TEST_MODE")
    assert ingest_enabled() is True


def test_a_bad_interval_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST_INTERVAL", "not-a-number")
    assert resolve_interval_seconds() == 900
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST_INTERVAL", "0")
    assert resolve_interval_seconds() == 900
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST_INTERVAL", "60")
    assert resolve_interval_seconds() == 60


def test_a_disabled_scheduler_reports_disabled_not_current(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST", "0")
    scheduler = CorpusIngestScheduler(tmp_path)

    assert scheduler.start() is False
    assert scheduler.status()["enabled"] is False
    assert scheduler.status()["running"] is False


@pytest.mark.asyncio
async def test_scheduler_runs_a_pass_and_stops_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST", "1")
    monkeypatch.setenv("CLOUDE_CORPUS_ROOT", str(tmp_path / "corpus"))
    state = _make_state(tmp_path)
    _make_corpus(tmp_path / "corpus")
    scheduler = CorpusIngestScheduler(state, interval_seconds=3600)

    assert scheduler.start() is True
    for _ in range(200):
        if scheduler.runs_completed:
            break
        await _tick()
    assert scheduler.runs_completed >= 1
    assert scheduler.last_report.ingested == 2
    await scheduler.aclose(timeout=5.0)
    assert scheduler.status()["running"] is False


async def _tick() -> None:
    """Yield to the event loop for 50 ms.

    Description: the scheduler's pass runs in a worker thread, so the
      test has to give the loop real time rather than a bare
      ``asyncio.sleep(0)``.
    Inputs: none.
    Output: None.
    Example: await _tick()
    """
    import asyncio

    await asyncio.sleep(0.05)


def test_report_record_is_json_serialisable(tmp_path):
    record = CorpusIngestReport().to_record()
    assert json.loads(json.dumps(record))["status"] == STATUS_OK


# --------------------------------------------------------------------------
# The two cost optimisations, and the cases where they must refuse
# --------------------------------------------------------------------------


def test_steady_state_goes_incremental_and_skips_rooting(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    first = run_ingest_once(state, corpus_root=corpus)
    assert first.scan_mode == "full_scan"
    assert first.rooting["status"] == "ran"

    second = run_ingest_once(state, corpus_root=corpus)

    assert second.scan_mode == "incremental"
    # NOT zeros: a skipped rooting pass says so in its own words.
    assert second.rooting["status"] == "skipped_unchanged"
    assert second.project_rooting["status"] == "skipped_unchanged"


def test_a_new_file_re_runs_rooting(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    run_ingest_once(state, corpus_root=corpus)
    run_ingest_once(state, corpus_root=corpus)
    (corpus / "-Users-x-proj" / "cccccccc-0000-0000-0000-000000000000.jsonl"
     ).write_bytes(b'{"type":"user","uuid":"new"}\n')

    report = run_ingest_once(state, corpus_root=corpus)

    assert report.ingested == 1
    assert report.rooting["status"] == "ran"


def test_a_different_install_id_forces_a_full_scan(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    run_ingest_once(state, corpus_root=corpus)
    cache = state_io.load_scan_cache(state)
    meta = state_io.load_scan_meta(state)
    meta["install_id"] = "a-different-database-entirely"
    state_io.save_scan_cache(state, cache, meta)

    report = run_ingest_once(state, corpus_root=corpus)

    assert report.scan_mode == "full_scan"


def test_a_shrunk_archive_forces_a_full_scan(tmp_path):
    state = _make_state(tmp_path)
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    run_ingest_once(state, corpus_root=corpus)
    cache = state_io.load_scan_cache(state)
    meta = state_io.load_scan_meta(state)
    # The fingerprint claims the archive was further along than it is,
    # which means rows disappeared: something other than an append
    # happened, so the incremental premise no longer holds.
    meta["max_archive_id"] = int(meta["max_archive_id"]) + 1000
    state_io.save_scan_cache(state, cache, meta)

    report = run_ingest_once(state, corpus_root=corpus)

    assert report.scan_mode == "full_scan"


def test_rooting_gate_notices_a_uuid_learned_without_a_new_row(tmp_path):
    from src.core.corpus_ingest_scan import _db_signature, _rooting_needed

    state = _make_state(tmp_path)
    conn = connect(db_path_for(state))
    before = _db_signature(conn)
    # A session row that LEARNS its uuid later moves no id, which is why
    # the gate counts non-null uuids rather than comparing ids alone.
    after = dict(before)
    after["sessions_with_uuid"] = int(before["sessions_with_uuid"]) + 1
    conn.close()

    assert _rooting_needed(before, before) is False
    assert _rooting_needed(before, after) is True
    assert _rooting_needed({}, after) is True
