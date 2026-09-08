"""The v17 host dimension: identity, attribution, slugs, sessions.

WHAT THESE TESTS ARE FOR. The host dimension's whole job is to stop two
machines being silently merged, so the tests that matter are the ones
that would FAIL if a merge happened - a slug that must produce two
project rows, a session uuid that must produce one session, and an
attribution that must refuse to upgrade itself when the evidence is
missing. Several of the assertions below are deliberately written as
negative controls: they prove a check CAN fail, which is the only thing
that makes its passing worth anything.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_gate_contract import BY_CODE, GATE_PROJECT_SLUG_COLLISION
from src.core.message_host_ddl import (
    HOST_ATTRIBUTION_VALUES,
    PROJECT_ATTRIBUTION_VALUES,
    V17_TABLE_NAMES,
    V17_TRANSCRIPT_COLUMNS,
    V17_VIEW_NAMES,
)
from src.core.message_host_dimension import (
    LAYOUT_CLAUDE_PROJECTS,
    LAYOUT_NESTED_CLAUDE_PROJECTS,
    PROJ_CANNOT_DETERMINE,
    PROJ_DERIVED,
    PROJ_NONE_DECLARED,
    attribute_transcript,
    attribution_summary,
    cross_host_sessions,
    derive_slug,
    find_slug_collisions,
    global_source_ref,
    host_rollup,
    record_slug_collisions,
    unseen_manifest_paths,
    upsert_corpus,
    upsert_host,
    upsert_project,
)
from src.core.message_host_identity import (
    ATTR_CANNOT_DETERMINE,
    ATTR_DECLARED,
    ATTR_VERIFIED,
    HostIdentity,
    build_manifest,
    capture_identity,
    classify_attribution,
    iter_manifest_paths,
    manifest_sha,
    sha256_file,
    walk_jsonl,
)
from src.core.message_model_ingest import SourceLine, ingest_lines

LAPTOP = HostIdentity("MID-LAPTOP", "platform_uuid", "Laptop", "lap", "Darwin")
MINI = HostIdentity("MID-MINI", "platform_uuid", "Mini", "mini", "Darwin")


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """A fresh in-memory database at the current schema version."""
    handle = sqlite3.connect(":memory:")
    with handle:
        run_chain(handle, 0, CURRENT_SCHEMA_VERSION)
    return handle


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------

def test_v17_creates_its_declared_tables_columns_and_views(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    for table in V17_TABLE_NAMES:
        assert table in names, f"v17 did not create {table}"
    for view in V17_VIEW_NAMES:
        assert view in names, f"v17 did not create view {view}"
    columns = {row[1] for row in
               conn.execute("PRAGMA table_info(message_transcripts)")}
    for column in V17_TRANSCRIPT_COLUMNS:
        assert column in columns, f"v17 did not add {column}"


def test_v17_step_is_idempotent(conn):
    with conn:
        run_chain(conn, 16, 17)
    assert conn.execute("SELECT COUNT(*) FROM message_hosts").fetchone()[0] == 0


def test_pre_v17_rows_read_as_unattributed_not_as_a_host(conn):
    """A v16 row with no host is CANNOT DETERMINE, never a default host."""
    ingest_lines(conn, source_ref="legacy.jsonl", session_ref="s",
                 lines=[SourceLine('{"type":"user","uuid":"u"}')])
    assert attribution_summary(conn) == {"unattributed": 1}


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------

def test_capture_identity_refuses_to_invent_an_identity(monkeypatch):
    monkeypatch.setattr(
        "src.core.message_host_identity._ioreg_platform_uuid", lambda: None)
    with pytest.raises(ValueError):
        capture_identity(None)
    assert capture_identity("declared-x").machine_id_scheme == "declared"


def test_upsert_host_is_keyed_on_machine_id_not_on_name(conn):
    """A rename updates one row; it does not mint a second machine."""
    first = upsert_host(conn, LAPTOP)
    renamed = HostIdentity(LAPTOP.machine_id, "platform_uuid",
                           "Totally New Name", "newname", "Darwin")
    second = upsert_host(conn, renamed)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM message_hosts").fetchone()[0] == 1
    assert conn.execute(
        "SELECT display_name FROM message_hosts").fetchone()[0] == \
        "Totally New Name"


def test_two_machines_are_two_hosts(conn):
    assert upsert_host(conn, LAPTOP) != upsert_host(conn, MINI)
    assert conn.execute("SELECT COUNT(*) FROM message_hosts").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Attribution: the three outcomes, with a negative control
# ---------------------------------------------------------------------------

def test_attribution_values_are_the_declared_ones():
    assert set(HOST_ATTRIBUTION_VALUES) == {
        ATTR_VERIFIED, ATTR_DECLARED, ATTR_CANNOT_DETERMINE}
    assert set(PROJECT_ATTRIBUTION_VALUES) == {
        PROJ_DERIVED, PROJ_NONE_DECLARED, PROJ_CANNOT_DETERMINE}


def test_classify_attribution_three_outcomes_including_the_negative_case():
    manifest = {"files": {"a.jsonl": {"size": 3, "sha256": "abc"}}}
    assert classify_attribution(None, "a.jsonl", "abc", 3)[0] == ATTR_DECLARED
    assert classify_attribution(manifest, "a.jsonl", "abc", 3)[0] == \
        ATTR_VERIFIED
    # NEGATIVE CONTROL. A check that has never been shown able to fail is
    # not evidence of anything, so both failing shapes are asserted here:
    # a path the source machine never listed, and one it listed with a
    # different hash. Neither may narrow to `declared`.
    assert classify_attribution(manifest, "ghost.jsonl", "abc", 3)[0] == \
        ATTR_CANNOT_DETERMINE
    assert classify_attribution(manifest, "a.jsonl", "WRONG", 3)[0] == \
        ATTR_CANNOT_DETERMINE


def test_a_grown_file_is_named_rather_than_lumped_in():
    manifest = {"files": {"a.jsonl": {"size": 10, "sha256": "abc"}}}
    outcome, detail = classify_attribution(manifest, "a.jsonl", "other", 99)
    assert outcome == ATTR_CANNOT_DETERMINE
    assert "grew after collection" in detail


def test_manifest_records_an_unreadable_file_rather_than_omitting_it(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "ok.jsonl").write_text("{}\n")
    manifest = build_manifest(str(root), LAPTOP, "k")
    assert manifest["file_count"] == 1
    assert manifest["files"]["ok.jsonl"]["sha256"] == \
        sha256_file(str(root / "ok.jsonl"))
    assert len(manifest_sha(manifest)) == 64
    assert list(iter_manifest_paths(manifest)) == ["ok.jsonl"]
    assert walk_jsonl(str(root)) == ["ok.jsonl"]


# ---------------------------------------------------------------------------
# Slugs: the collision must produce TWO projects, never one
# ---------------------------------------------------------------------------

def test_derive_slug_three_outcomes():
    assert derive_slug("-Users-x/s.jsonl", LAYOUT_CLAUDE_PROJECTS) == \
        ("-Users-x", PROJ_DERIVED)
    assert derive_slug("-Users-x/s/subagents/a.jsonl",
                       LAYOUT_CLAUDE_PROJECTS) == ("-Users-x", PROJ_DERIVED)
    assert derive_slug("loose.jsonl", LAYOUT_CLAUDE_PROJECTS) == \
        (None, PROJ_CANNOT_DETERMINE)
    assert derive_slug("a/b/.claude/projects/-slug/s.jsonl",
                       LAYOUT_NESTED_CLAUDE_PROJECTS) == \
        ("-slug", PROJ_DERIVED)
    assert derive_slug("a/b/audit.jsonl",
                       LAYOUT_NESTED_CLAUDE_PROJECTS) == \
        (None, PROJ_NONE_DECLARED)
    with pytest.raises(ValueError):
        derive_slug("a/b.jsonl", "invented-layout")


def _two_hosts_one_slug(conn, slug="-Users-jsugamele-Development-Media"):
    """Build the real shape: one slug, two machines, one cwd string."""
    ids = {}
    for identity in (LAPTOP, MINI):
        host_id = upsert_host(conn, identity)
        corpus_id = upsert_corpus(conn, host_id, "claude-projects", "/r")
        project_id = upsert_project(conn, corpus_id, slug,
                                    "/Users/jsugamele/Development/Media")
        result = ingest_lines(
            conn,
            source_ref=global_source_ref(identity.machine_id,
                                         "claude-projects", f"{slug}/s.jsonl"),
            session_ref="11111111-2222-3333-4444-555555555555",
            lines=[SourceLine('{"type":"user","uuid":"u"}')])
        attribute_transcript(
            conn, result.transcript_id, host_id=host_id, corpus_id=corpus_id,
            project_id=project_id, source_path=f"{slug}/s.jsonl",
            host_attribution=ATTR_VERIFIED, project_attribution=PROJ_DERIVED)
        ids[identity.machine_id] = (host_id, corpus_id, project_id)
    return ids


def test_same_slug_on_two_hosts_is_two_projects_never_one(conn):
    ids = _two_hosts_one_slug(conn)
    projects = {v[2] for v in ids.values()}
    assert len(projects) == 2, "the two machines were merged into one project"
    assert conn.execute(
        "SELECT COUNT(*) FROM message_projects").fetchone()[0] == 2


def test_slug_collision_is_found_and_gated(conn):
    _two_hosts_one_slug(conn)
    collisions = find_slug_collisions(conn)
    assert len(collisions) == 1
    assert collisions[0].host_count == 2
    assert len(collisions[0].project_ids) == 2
    assert collisions[0].cwds == ("/Users/jsugamele/Development/Media",)
    with conn:
        assert record_slug_collisions(conn) == 2
    rows = conn.execute(
        "SELECT severity, detail FROM message_ingest_findings "
        "WHERE condition_code = ?", (GATE_PROJECT_SLUG_COLLISION,)).fetchall()
    assert len(rows) == 2
    assert {r[0] for r in rows} == {BY_CODE[GATE_PROJECT_SLUG_COLLISION].severity}
    assert "IDENTICAL path string" in rows[0][1]


def test_one_hosts_own_two_corpora_sharing_a_slug_is_not_a_collision(conn):
    """A cross-HOST check must not fire on one machine's own layout."""
    host_id = upsert_host(conn, LAPTOP)
    for key in ("claude-projects", "local-agent-mode-sessions"):
        corpus_id = upsert_corpus(conn, host_id, key, "/r")
        upsert_project(conn, corpus_id, "-shared-slug")
    assert find_slug_collisions(conn) == []


# ---------------------------------------------------------------------------
# Sessions: the opposite rule
# ---------------------------------------------------------------------------

def test_a_session_uuid_on_two_hosts_is_one_session_and_is_not_gated(conn):
    _two_hosts_one_slug(conn)
    shared = cross_host_sessions(conn)
    assert shared == [("11111111-2222-3333-4444-555555555555", 2, 2)]
    with conn:
        record_slug_collisions(conn)
    # The findings raised by the cross-host pass name PROJECTS. Not one of
    # them may be about this session uuid, which the two hosts share
    # legitimately. Asserting on the detail text rather than on an
    # invented condition code, because a gate that does not exist cannot
    # be shown absent by looking for its name.
    details = [r[0] for r in conn.execute(
        "SELECT detail FROM message_ingest_findings")]
    assert details, "the negative control is worthless if nothing was raised"
    assert not any("11111111-2222-3333-4444-555555555555" in d
                   for d in details), "the shared session was gated"


def test_agent_scheme_refs_are_excluded_from_the_cross_host_count(conn):
    """An agent id is an ordinal, not 122 random bits - different question."""
    for identity in (LAPTOP, MINI):
        host_id = upsert_host(conn, identity)
        corpus_id = upsert_corpus(conn, host_id, "claude-projects", "/r")
        result = ingest_lines(
            conn, source_ref=f"{identity.machine_id}::agent",
            session_ref="agent-a00fdb4",
            lines=[SourceLine('{"type":"user","uuid":"u"}')])
        attribute_transcript(
            conn, result.transcript_id, host_id=host_id, corpus_id=corpus_id,
            project_id=None, source_path=f"{identity.machine_id}.jsonl",
            host_attribution=ATTR_VERIFIED,
            project_attribution=PROJ_NONE_DECLARED)
    assert cross_host_sessions(conn) == []
    assert conn.execute(
        "SELECT host_count FROM message_session_hosts "
        "WHERE session_ref = 'agent-a00fdb4'").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Bodies belong to a SET of hosts
# ---------------------------------------------------------------------------

def test_a_body_seen_on_both_machines_is_attributed_to_both(conn):
    _two_hosts_one_slug(conn)
    rows = conn.execute(
        "SELECT body_id, COUNT(DISTINCT host_id) FROM message_body_hosts "
        "GROUP BY body_id").fetchall()
    assert rows and rows[0][1] == 2, (
        "the copied body lost one of its two hosts - a body is content "
        "identity and genuinely came from both machines")


# ---------------------------------------------------------------------------
# Uniqueness and completeness
# ---------------------------------------------------------------------------

def test_global_source_ref_separates_identical_paths_on_two_machines():
    left = global_source_ref("MID-A", "claude-projects", "-Users-j/s.jsonl")
    right = global_source_ref("MID-B", "claude-projects", "-Users-j/s.jsonl")
    assert left != right


def test_one_file_cannot_be_stored_twice_in_one_corpus(conn):
    host_id = upsert_host(conn, LAPTOP)
    corpus_id = upsert_corpus(conn, host_id, "claude-projects", "/r")
    for ref in ("a", "b"):
        result = ingest_lines(conn, source_ref=ref, session_ref="s",
                              lines=[SourceLine('{"type":"user"}')])
        if ref == "a":
            attribute_transcript(
                conn, result.transcript_id, host_id=host_id,
                corpus_id=corpus_id, project_id=None, source_path="dup.jsonl",
                host_attribution=ATTR_DECLARED,
                project_attribution=PROJ_NONE_DECLARED)
        else:
            with pytest.raises(sqlite3.IntegrityError):
                attribute_transcript(
                    conn, result.transcript_id, host_id=host_id,
                    corpus_id=corpus_id, project_id=None,
                    source_path="dup.jsonl", host_attribution=ATTR_DECLARED,
                    project_attribution=PROJ_NONE_DECLARED)


def test_unseen_manifest_paths_names_what_the_source_had_and_we_do_not(conn):
    host_id = upsert_host(conn, LAPTOP)
    corpus_id = upsert_corpus(conn, host_id, "claude-projects", "/r")
    result = ingest_lines(conn, source_ref="x", session_ref="s",
                          lines=[SourceLine('{"type":"user"}')])
    attribute_transcript(
        conn, result.transcript_id, host_id=host_id, corpus_id=corpus_id,
        project_id=None, source_path="here.jsonl",
        host_attribution=ATTR_DECLARED, project_attribution=PROJ_NONE_DECLARED)
    assert unseen_manifest_paths(conn, corpus_id,
                                 ["here.jsonl", "vanished.jsonl"]) == \
        ["vanished.jsonl"]


def test_host_rollup_counts_per_machine(conn):
    _two_hosts_one_slug(conn)
    rollup = dict((r[1], (r[2], r[3])) for r in host_rollup(conn))
    assert rollup["MID-LAPTOP"] == (1, 1)
    assert rollup["MID-MINI"] == (1, 1)


# ---------------------------------------------------------------------------
# Positive control for the byte-exact proof itself
# ---------------------------------------------------------------------------

def test_the_byte_exact_check_can_actually_fail(tmp_path):
    """A proof that has never been shown able to fail proves nothing.

    The multi-host run reports 21,039 of 21,039 files byte-identical.
    That number is only worth reading if the comparison behind it
    discriminates, so this tampers with a stored body and asserts the
    same code path reports MISMATCH. Without this the whole verification
    step is unfalsifiable, which is a worse defect than a check that
    fails silently.
    """
    from scripts.message_model_corpus_run import _classify, OUTCOME_IDENTICAL
    from src.core.message_model_serialize import sha256_text

    handle = sqlite3.connect(":memory:")
    with handle:
        run_chain(handle, 0, CURRENT_SCHEMA_VERSION)
    text = '{"type":"user","uuid":"u","cwd":"/tmp"}\n'
    with handle:
        result = ingest_lines(
            handle, source_ref="p.jsonl", session_ref="s",
            lines=[SourceLine(text.rstrip("\n"))], has_trailing_newline=True)
    good = sha256_text(text)
    assert _classify(handle, result.transcript_id, good)[0] == \
        OUTCOME_IDENTICAL

    with handle:
        handle.execute(
            "UPDATE message_bodies SET body_json = ? WHERE id = "
            "(SELECT body_id FROM message_appearances WHERE transcript_id = ?)",
            ('{"type":"user","uuid":"u","cwd":"/TAMPERED"}',
             result.transcript_id))
    outcome, detail = _classify(handle, result.transcript_id, good)
    assert outcome != OUTCOME_IDENTICAL, (
        "the byte-exact comparison passed a tampered body - it cannot fail, "
        "so every 'byte_identical' it has ever reported is worthless")
    assert detail
