"""Throwaway archive databases for the archive browse tests.

Every archive test builds its own database in a pytest ``tmp_path``. The
real corpus is never opened by the suite: it is 11 GB, it is the owner's
complete working record, and a test that reads it would be measuring
whatever happens to be in it today rather than the condition it was
written to catch.

The seeders below take explicit arguments for the things the tests care
about - a NULL ``project_id``, an ``ingested_at`` tie, a
``cannot_determine`` attribution, a body with no appearance body_id - so
each test states the shape it needs instead of inheriting a blob of
setup and hoping.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.message_model_serialize import (
    SESSION_REF_SCHEMES as _PRODUCTION_SCHEMES,
)
from src.core.db_steps import run_chain

#: One valid value from each CHECK-constrained column, so a seeder that
#: does not care about a field still writes something the schema accepts.
DEFAULT_LINE_STATUS = "ok"
DEFAULT_FIDELITY = "fidelity_verified"
DEFAULT_INGESTED_AT = "2026-08-29T22:17:03.086206Z"

#: The values ``message_transcripts.session_ref_scheme`` accepts, per the
#: CHECK constraint in ``src/core/message_model_ddl.py``. Named here so a
#: seeder cannot write a scheme the real ingest could never produce, and
#: so a test asserting "unknown scheme" has a value it can prove is
#: outside the domain rather than merely absent today.
#:
#: DERIVED from the classifier's own tuple rather than retyped, so a
#: fourth scheme cannot be added to production while the seeder quietly
#: keeps refusing it.
SESSION_REF_SCHEMES = frozenset(_PRODUCTION_SCHEMES)



def make_state_dir(tmp_path: Path, name: str = "state") -> Path:
    """Create a state directory holding an empty, current-schema cloude.db.

    Description: builds the file with an ordinary read-write connection
      and migrates it to CURRENT_SCHEMA_VERSION, then closes. Tests then
      reopen it through ``open_read_only`` so the read path under test is
      the one that ships.
    Inputs: tmp_path (Path) - pytest's per-test directory, name (str).
    Output: Path - the state directory containing cloude.db.
    Example: state_dir = make_state_dir(tmp_path)
    """
    state_dir = tmp_path / name
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "cloude.db"))
    try:
        with conn:
            run_chain(conn, 0, CURRENT_SCHEMA_VERSION)
    finally:
        conn.close()
    return state_dir


def writable(state_dir: Path) -> sqlite3.Connection:
    """Open a normal read-write connection for seeding.

    Description: deliberately NOT ``open_read_only``. A seeder that used
      the read path would either fail or, worse, quietly prove that the
      read path is not read-only.
    Inputs: state_dir (Path).
    Output: sqlite3.Connection with ``row_factory = sqlite3.Row``.
    Example: with closing(writable(sd)) as conn: seed_host(conn)
    """
    conn = sqlite3.connect(str(Path(state_dir) / "cloude.db"))
    conn.row_factory = sqlite3.Row
    return conn


def seed_host(
    conn: sqlite3.Connection, *, machine_id: str = "MACHINE-1", name: str = "test-host"
) -> int:
    """Insert one host and return its id.

    Inputs: conn (sqlite3.Connection), machine_id (str), name (str).
    Output: int - the host id.
    Example: host_id = seed_host(conn)
    """
    cur = conn.execute(
        "INSERT INTO message_hosts "
        "(machine_id, machine_id_scheme, display_name, hostname, platform, "
        " first_seen_at) VALUES (?, 'platform_uuid', ?, ?, 'Darwin', ?)",
        (machine_id, name, name, DEFAULT_INGESTED_AT),
    )
    return int(cur.lastrowid)


def seed_corpus(
    conn: sqlite3.Connection,
    host_id: int,
    *,
    corpus_key: str = "claude-projects",
    manifest: bool = True,
) -> int:
    """Insert one corpus under a host and return its id.

    Inputs: conn, host_id (int), corpus_key (str), manifest (bool) -
      whether a manifest_sha is recorded.
    Output: int - the corpus id.
    Example: corpus_id = seed_corpus(conn, host_id)
    """
    cur = conn.execute(
        "INSERT INTO message_corpora "
        "(host_id, corpus_key, root_path, manifest_sha, collected_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            host_id,
            corpus_key,
            f"/tmp/{corpus_key}",
            "sha-of-manifest" if manifest else None,
            DEFAULT_INGESTED_AT,
        ),
    )
    return int(cur.lastrowid)


def seed_project(conn: sqlite3.Connection, corpus_id: int, *, slug: str) -> int:
    """Insert one project in a corpus and return its id.

    Inputs: conn, corpus_id (int), slug (str) - unique per corpus.
    Output: int - the project id.
    Example: project_id = seed_project(conn, corpus_id, slug="-a")
    """
    cur = conn.execute(
        "INSERT INTO message_projects (corpus_id, slug, observed_cwd, first_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (corpus_id, slug, slug.replace("-", "/"), DEFAULT_INGESTED_AT),
    )
    return int(cur.lastrowid)


def seed_transcript(
    conn: sqlite3.Connection,
    *,
    host_id: Optional[int],
    corpus_id: Optional[int],
    project_id: Optional[int],
    source_path: str,
    ingested_at: str = DEFAULT_INGESTED_AT,
    host_attribution: str = "manifest_verified",
    project_attribution: str = "derived",
    line_count: int = 0,
    raw_byte_length: int = 100,
    session_ref_scheme: str = "uuid",
    newest_message_ts: Optional[str] = None,
) -> int:
    """Insert one transcript and return its id.

    Description: ``project_id=None`` and
      ``host_attribution="cannot_determine"`` are BOTH reachable from
      here, separately, because the tests must prove those two conditions
      are not the same thing. ``session_ref_scheme`` is a parameter and
      not a constant because the scheme FILTER's tests need both values
      in one fixture; the schema CHECK admits 'uuid' and 'agent' only,
      so anything else raises here rather than seeding a row the real
      ingest could never produce.
    Inputs: conn, host_id/corpus_id/project_id (int|None), source_path
      (str, unique per corpus), ingested_at (str) - pass the same value
      twice to build a keyset tie, host_attribution (str),
      project_attribution (str), line_count (int), raw_byte_length (int),
      session_ref_scheme (str) - 'uuid' or 'agent',
      newest_message_ts (str|None) - the newest message timestamp in this
      transcript. None is a MEASURED absence here exactly as it is in
      production (see src/core/message_activity.py), so a test seeding
      None is seeding "this transcript has no dated message", never
      "this test did not bother".
    Output: int - the transcript id.
    Raises: ValueError - session_ref_scheme is outside the schema's
      CHECK domain.
    Example: seed_transcript(conn, host_id=1, corpus_id=1,
             project_id=None, source_path="a.jsonl")
    """
    if session_ref_scheme not in SESSION_REF_SCHEMES:
        raise ValueError(
            f"session_ref_scheme {session_ref_scheme!r} is outside the schema's "
            f"CHECK domain {sorted(SESSION_REF_SCHEMES)}"
        )
    cur = conn.execute(
        "INSERT INTO message_transcripts "
        "(source_ref, session_ref, session_ref_scheme, line_ending, "
        " has_trailing_newline, line_count, content_sha256, raw_byte_length, "
        " ingested_at, host_id, corpus_id, project_id, source_path, "
        " host_attribution, project_attribution, newest_message_ts) "
        "VALUES (?, ?, ?, 'LF', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"ref::{source_path}",
            f"session-{source_path}",
            session_ref_scheme,
            line_count,
            f"sha-{source_path}",
            raw_byte_length,
            ingested_at,
            host_id,
            corpus_id,
            project_id,
            source_path,
            host_attribution,
            project_attribution,
            newest_message_ts,
        ),
    )
    return int(cur.lastrowid)


def seed_body(
    conn: sqlite3.Connection,
    *,
    body_json: str,
    ts: Optional[str] = "2025-12-29T06:50:35.600Z",
    secret_finding_count: int = 0,
    identity_key: Optional[str] = None,
) -> int:
    """Insert one body and return its id.

    Description: ``ts=None`` is offered explicitly, because 33,480 real
      bodies have no ``ts`` and a fixture that cannot express that cannot
      prove those rows stay visible.
    Inputs: conn, body_json (str) - stored verbatim, ts (str|None),
      secret_finding_count (int), identity_key (str|None).
    Output: int - the body id.
    Example: seed_body(conn, body_json='{"a":1}', ts=None)
    """
    key = identity_key or f"identity-{len(body_json)}-{ts}-{secret_finding_count}"
    cur = conn.execute(
        "INSERT INTO message_bodies "
        "(identity_key, message_uuid, body_json, body_sha256, body_bytes_sha256, "
        " parent_uuid, ts, origin_session_ref, is_compact_boundary, "
        " secret_finding_count, first_seen_at) "
        "VALUES (?, ?, ?, 'sha', 'shab', NULL, ?, 'origin', 0, ?, ?)",
        (key, f"uuid-{key}", body_json, ts, secret_finding_count, DEFAULT_INGESTED_AT),
    )
    return int(cur.lastrowid)


def seed_appearance(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    line_no: int,
    body_id: Optional[int],
    line_status: str = DEFAULT_LINE_STATUS,
) -> int:
    """Insert one appearance row and return its id.

    Description: ``body_id=None`` is the ``absent`` body state - exactly
      1 of 3,125,122 real rows - and it must not render like a withheld
      body.
    Inputs: conn, transcript_id (int), line_no (int), body_id (int|None),
      line_status (str) - 'ok', 'blank' or 'invalid_json'.
    Output: int - the appearance id.
    Example: seed_appearance(conn, transcript_id=1, line_no=1, body_id=None)
    """
    cur = conn.execute(
        "INSERT INTO message_appearances "
        "(transcript_id, line_no, seq_in_file, line_status, body_id, "
        " serializer_style, line_sha256, line_byte_length, fidelity_outcome, "
        " is_sidechain, agent_id) "
        "VALUES (?, ?, ?, ?, ?, 'compact', 'linesha', 10, ?, 0, NULL)",
        (transcript_id, line_no, line_no, line_status, body_id, DEFAULT_FIDELITY),
    )
    return int(cur.lastrowid)


def seed_secret_finding(
    conn: sqlite3.Connection,
    *,
    body_id: int,
    match_offset: int,
    match_length: int,
    detector: str = "test_detector",
    value_sha256: str = "0" * 64,
) -> int:
    """Insert one secret finding against a body and return its id.

    Description: offsets are CODE POINT offsets into ``body_json``, the
      same unit ``message_model_secrets.scan_text`` produces, so a test
      can assert the UTF-16 companions are converted rather than copied.
      The seeder does NOT update the body's ``secret_finding_count``:
      that is the caller's to set on ``seed_body``, so a test can also
      express the two disagreeing.
    Inputs: conn, body_id (int), match_offset (int), match_length (int),
      detector (str), value_sha256 (str).
    Output: int - the finding id.
    Example: seed_secret_finding(conn, body_id=b, match_offset=0,
             match_length=4)
    """
    cur = conn.execute(
        "INSERT INTO message_secret_findings "
        "(body_id, detector, match_offset, match_length, value_sha256, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (body_id, detector, match_offset, match_length, value_sha256,
         DEFAULT_INGESTED_AT),
    )
    return int(cur.lastrowid)
