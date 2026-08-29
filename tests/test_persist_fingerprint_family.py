"""Persisting a scrollback fingerprint onto the row, honestly.

WHAT THIS COVERS. ``src/core/session_agent_provenance.py``'s
``persist_fingerprint_family`` is the fix for a specific defect: an
adopted session's agent is correctly identified by scrollback
fingerprinting (``src/core/agent_fingerprint.detect_agent_type``), the
scan result is used to render ONE response, and then thrown away - the
``sessions`` row it describes keeps ``agent_type = NULL``,
``agent_family = NULL``, ``agent_family_source = 'unknown'`` forever, so
every OTHER surface that reads the row directly (Projects,
``/sessions/records``) renders "unknown family" about a session the app
can plainly identify.

THE RULES THIS TEST FILE ENFORCES, taken from the module's own
docstring and the repo's THREE-OUTCOME RULE:

  1. A fingerprinted family is written with
     ``agent_family_source = 'fingerprint'`` - never ``'wrapper'`` or
     ``'launched'``, because it is an inference, not a fact.
  2. A row that already carries a launched fact (``agent_type`` set, or
     ``agent_family_source`` in ``{'launched', 'not_launched'}``) is
     never touched.
  3. A fingerprint that found nothing writes nothing - there is no
     "unknown" value to invent, the row simply keeps whatever it had.
  4. The write is idempotent.
  5. A datastore failure is swallowed, not raised.

Run with:
    ./venv/bin/python -m pytest tests/test_persist_fingerprint_family.py -v
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_models import (
    SESSION_FAMILY_SOURCE_FINGERPRINT,
    SESSION_FAMILY_SOURCE_LAUNCHED,
    SESSION_FAMILY_SOURCE_NOT_LAUNCHED,
    SESSION_FAMILY_SOURCE_UNKNOWN,
    SESSION_LIFECYCLE_RUNNING,
    SESSION_ORIGIN_ADOPTED,
)
from src.core.session_agent_provenance import persist_fingerprint_family
from src.core.session_identity import record_instance
from tests.s7_helpers import TEST_SOCKET, migrated_connection, session_row

EPOCH = 1793000000


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection, closed on teardown."""
    with closing(migrated_connection(tmp_path)):
        pass
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _seed(conn, name, **fields):
    """Insert one row via the real write path and return its session_uuid.

    Inputs: conn (sqlite3.Connection). name (str) - tmux session name,
      unique per test. **fields - forwarded to record_instance
      (agent_type / agent_family / agent_family_source, etc).
    Output: str - the new row's session_uuid.
    """
    result = record_instance(
        conn,
        socket=TEST_SOCKET,
        name=name,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_ADOPTED,
        lifecycle=SESSION_LIFECYCLE_RUNNING,
        **fields,
    )
    return result.session_uuid


# --- the success path -------------------------------------------------


def test_fingerprinted_family_is_persisted_with_source_fingerprint(conn):
    """An adopted row with no agent info gets the fingerprint's answer."""
    uuid = _seed(conn, "cloudes7_fp_a")

    wrote = persist_fingerprint_family(
        conn, session_uuid=uuid, agent_family="claude"
    )

    assert wrote is True
    row = session_row(conn, "cloudes7_fp_a")
    assert row["agent_family"] == "claude"
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_FINGERPRINT
    # And the write never invents an agent_type - that column stays
    # exactly what it was, because the fingerprint answered "family",
    # not "wrapper id".
    assert row["agent_type"] is None


def test_row_started_unknown_before_the_write(conn):
    """Sanity check on the fixture: the defect this fixes is real here."""
    uuid = _seed(conn, "cloudes7_fp_presanity")
    row = session_row(conn, "cloudes7_fp_presanity")
    assert row["agent_type"] is None
    assert row["agent_family"] is None
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_UNKNOWN
    assert uuid  # the seed actually landed a row


# --- rule 2: never overwrite a launched fact ---------------------------


def test_a_launched_agent_type_is_never_overwritten(conn):
    """A row the app launched (agent_type set) is left completely alone."""
    uuid = _seed(
        conn,
        "cloudes7_fp_launched",
        agent_type="claude-code",
        agent_family_source=SESSION_FAMILY_SOURCE_LAUNCHED,
    )

    wrote = persist_fingerprint_family(
        conn, session_uuid=uuid, agent_family="codex"
    )

    assert wrote is False
    row = session_row(conn, "cloudes7_fp_launched")
    assert row["agent_type"] == "claude-code"
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_LAUNCHED


def test_a_deliberate_bare_shell_is_never_overwritten(conn):
    """``not_launched`` has agent_type=NULL too - that NULL is a fact.

    agent_type and agent_family are both NULL on a bare shell, exactly
    as they are on a genuinely unresolved row - the only thing that
    tells them apart is agent_family_source, which is why the write's
    WHERE clause must check it explicitly rather than just the two
    NULL-able columns.
    """
    uuid = _seed(
        conn,
        "cloudes7_fp_bareshell",
        agent_family_source=SESSION_FAMILY_SOURCE_NOT_LAUNCHED,
    )

    wrote = persist_fingerprint_family(
        conn, session_uuid=uuid, agent_family="claude"
    )

    assert wrote is False
    row = session_row(conn, "cloudes7_fp_bareshell")
    assert row["agent_family"] is None
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_NOT_LAUNCHED


def test_an_already_stored_family_is_never_overwritten(conn):
    """agent_family alone (no agent_type) is still a stored fact to keep."""
    uuid = _seed(
        conn,
        "cloudes7_fp_storedfamily",
        agent_family="hermes",
        agent_family_source=SESSION_FAMILY_SOURCE_LAUNCHED,
    )

    wrote = persist_fingerprint_family(
        conn, session_uuid=uuid, agent_family="codex"
    )

    assert wrote is False
    assert session_row(conn, "cloudes7_fp_storedfamily")["agent_family"] == "hermes"


# --- rule 3: a scan that found nothing writes nothing ------------------


def test_a_blank_agent_family_writes_nothing():
    """The caller must never pass a blank family - defensive backstop.

    ``persist_fingerprint_family`` takes an already-resolved family
    name; the "fingerprint found nothing" outcome is a None the caller
    (``adopt_external_session``) checks BEFORE calling this at all. This
    asserts the backstop still holds even if a caller regresses that
    check, using an in-memory connection since a real write must never
    be attempted for a blank value.
    """
    empty = sqlite3.connect(":memory:")
    try:
        assert persist_fingerprint_family(
            empty, session_uuid="whatever", agent_family=""
        ) is False
        assert persist_fingerprint_family(
            empty, session_uuid="", agent_family="claude"
        ) is False
    finally:
        empty.close()


def test_no_matching_row_writes_nothing(conn):
    """A session_uuid nobody claimed is a no-op, not an invented row."""
    wrote = persist_fingerprint_family(
        conn, session_uuid="no-such-uuid-at-all", agent_family="claude"
    )
    assert wrote is False


# --- rule 4: idempotent --------------------------------------------------


def test_write_is_idempotent(conn):
    """Calling it twice with the same value writes once and stays put."""
    uuid = _seed(conn, "cloudes7_fp_idempotent")

    first = persist_fingerprint_family(conn, session_uuid=uuid, agent_family="claude")
    second = persist_fingerprint_family(conn, session_uuid=uuid, agent_family="claude")

    assert first is True
    assert second is False, "a row that already carries the fingerprint must not rewrite"
    row = session_row(conn, "cloudes7_fp_idempotent")
    assert row["agent_family"] == "claude"
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_FINGERPRINT


def test_a_later_different_fingerprint_does_not_flip_the_stored_answer(conn):
    """Once written, the row is a fact for THIS module's own purposes too.

    A second, different-looking scan (e.g. a noisier scrollback capture)
    must not overwrite the first fingerprint's verdict - the same
    no-overwrite discipline that protects a launched fact also protects
    the first fingerprint write against a later, possibly worse one.
    """
    uuid = _seed(conn, "cloudes7_fp_stable")
    persist_fingerprint_family(conn, session_uuid=uuid, agent_family="claude")

    wrote_again = persist_fingerprint_family(
        conn, session_uuid=uuid, agent_family="codex"
    )

    assert wrote_again is False
    assert session_row(conn, "cloudes7_fp_stable")["agent_family"] == "claude"


# --- rule 5: a DB failure is swallowed, never raised --------------------


def test_a_sqlite_error_is_swallowed_and_returns_false():
    """No ``sessions`` table at all raises sqlite3.OperationalError inside
    the UPDATE - a genuine sqlite3.Error subclass - and the function must
    catch it and answer False rather than let it propagate.
    """
    bare = sqlite3.connect(":memory:")
    try:
        result = persist_fingerprint_family(
            bare, session_uuid="abc", agent_family="claude"
        )
        assert result is False
    finally:
        bare.close()
