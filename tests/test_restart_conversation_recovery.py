"""The 2026-09-07 incident: a restart that resumed the WRONG conversation.

WHAT HAPPENED, from the live database and the server log on mac-mini-m4.

  13:31:21  row 42 is created for tmux instance ``cloude_CloudeCode``. A
            Claude SessionStart binds ``c33e4ce2`` to it.
  13:39:06  a NEW Claude conversation, ``2629dba5``, starts in that same
            pane. ``session_lineage.record_claude_session`` does what it
            is designed to do: it INSERTS row 43 for the new uuid and
            hangs it off row 42. Row 42 keeps ``c33e4ce2``, forever.
  20:43:31  the owner clicks RESTART on the only row he can see, row 42.
            ``resolve_restart_source`` reads ``claude_session_uuid``
            straight off that row and launches ``--resume c33e4ce2``.
            No transcript with that id exists anywhere under
            ~/.claude/projects (verified: the file is absent, while
            ``2629dba5.jsonl`` is present and 1.77 MB). Claude exits on
            its first tick, tmux keeps the corpse under remain-on-exit,
            and ``rebind_instance`` has already stamped the row
            ``lifecycle='running'``. The route answered 201,
            ``conversation='resumed'``.

Two independent defects, one lost session, and each gets a test here.

  1. THE ANCHOR'S UUID IS STALE THE MOMENT A LINEAGE ROW EXISTS. The
     restart has to resume the lineage HEAD - the conversation the pane
     was last having - not the first uuid the row ever learned.
  2. A ``--resume`` TARGET THAT IS NOT ON DISK MUST BE REFUSED, LOUDLY.
     Spawning it produces a pane that dies instantly and a row that reads
     running, which is worse than any error message.

And one negative control, because a repair that over-refuses is worse
than the defect: a corpus that CANNOT BE READ must not refuse anything.
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
from src.core import claude_transcript_correlate
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.session_restart import (
    RESTART_CONVERSATION_MISSING,
    RESTART_RESUMABLE,
    resolve_restart_source,
)
from src.core.session_transcript_presence import (
    CONVERSATION_ABSENT,
    CONVERSATION_PRESENT,
    CONVERSATION_UNCHECKED,
    conversation_presence,
)
from src.core.trail_entry import utc_now

#: The real uuids from the incident, kept verbatim so the test names the
#: thing it is about rather than a sanitised stand-in.
ANCHOR_UUID = "c33e4ce2-17ea-4320-aa28-36204c4d13c5"
HEAD_UUID = "2629dba5-234e-44d2-be54-ddaf69c8db4b"
WORKING_DIR = "/Users/x/Development/CloudeCode"


@pytest.fixture
def conn(tmp_path):
    """One migrated database per test."""
    state = tmp_path / "state"
    state.mkdir()
    ensure_db_migrated(state, 4, "0.8.2")
    c = connect(db_path_for(state))
    yield c
    c.close()


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A transcript corpus this test owns, never the developer's own.

    Points ``claude_transcript_correlate.default_projects_dir`` at a
    temporary tree so nothing here reads ~/.claude/projects.
    """
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(
        claude_transcript_correlate, "default_projects_dir", lambda: root
    )
    return root


def _write_transcript(corpus_root: Path, uuid: str, slug: str = "-Users-x-proj"):
    """Put a transcript for ``uuid`` in the corpus and return its path."""
    project = corpus_root / slug
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{uuid}.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


def _insert(conn, **overrides):
    """Insert one sessions row and return its id."""
    row = {
        "session_uuid": "row-uuid",
        "origin": "created",
        "tmux_socket": "cloude",
        "tmux_name": "cloude_CloudeCode",
        "tmux_created_epoch": 1788787880,
        "working_dir": WORKING_DIR,
        "agent_type": "claude-skip-permissions",
        "title": "Agent - Cloude Code",
        "lifecycle": "stopped",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with transaction(conn):
        cur = conn.execute(
            f"INSERT INTO sessions ({cols}) VALUES ({marks})", list(row.values())
        )
    return int(cur.lastrowid)


def test_restart_resumes_the_lineage_head_not_the_anchors_first_uuid(
    conn, corpus
):
    """DEFECT 1a. The row the user clicks is not the conversation.

    The anchor keeps the FIRST uuid its tmux instance ever saw; every
    later conversation in that pane lives on its own lineage row. A
    restart that reads the anchor resumes a superseded conversation - on
    the owner's box, one whose transcript no longer existed at all.
    """
    _write_transcript(corpus, ANCHOR_UUID)
    _write_transcript(corpus, HEAD_UUID)

    anchor_id = _insert(
        conn, session_uuid="anchor", claude_session_uuid=ANCHOR_UUID
    )
    _insert(
        conn,
        session_uuid="head",
        claude_session_uuid=HEAD_UUID,
        # A lineage row: same tmux name for context, NULL epoch so it can
        # never be mistaken for a live tmux instance.
        tmux_created_epoch=None,
        parent_session_id=anchor_id,
        fork_kind="unknown",
    )

    source = resolve_restart_source(conn, session_uuid="anchor")

    assert source.outcome == RESTART_RESUMABLE
    assert source.claude_session_uuid == HEAD_UUID, (
        "restart resumed the anchor's first conversation instead of the "
        "one the pane was actually having"
    )
    # The row being REUSED is still the anchor - only the conversation
    # came from the head. Getting this backwards would rebind the wrong
    # row and lose the tmux identity.
    assert source.parent_id == anchor_id


def test_restart_refuses_a_conversation_with_no_transcript_on_disk(
    conn, corpus
):
    """DEFECT 1b. ``--resume <gone>`` must fail loudly, not spawn a corpse.

    The corpus is readable and does NOT contain the uuid, which is a
    measured absence, not an unknown. Resuming it produces a pane that
    exits immediately while the row reads running.
    """
    # Positive control in the same corpus: another transcript IS here, so
    # a later ABSENT verdict cannot be "the corpus was empty/unreadable".
    _write_transcript(corpus, HEAD_UUID)

    _insert(conn, session_uuid="anchor", claude_session_uuid=ANCHOR_UUID)

    source = resolve_restart_source(conn, session_uuid="anchor")

    assert source.outcome == RESTART_CONVERSATION_MISSING
    assert source.claude_session_uuid == ANCHOR_UUID
    assert ANCHOR_UUID in (source.detail or "")


def test_an_unreadable_corpus_never_refuses_a_restart(conn, tmp_path, monkeypatch):
    """NEGATIVE CONTROL. Could-not-evaluate is not a refusal.

    A machine whose corpus lives somewhere this app was not told about
    must keep restarting sessions. Collapsing UNCHECKED into ABSENT would
    break every restart on such a box - a repair strictly worse than the
    defect it fixes.
    """
    missing = tmp_path / "no-such-corpus"
    monkeypatch.setattr(
        claude_transcript_correlate, "default_projects_dir", lambda: missing
    )
    _insert(conn, session_uuid="anchor", claude_session_uuid=ANCHOR_UUID)

    source = resolve_restart_source(conn, session_uuid="anchor")

    assert source.outcome == RESTART_RESUMABLE


def test_presence_probe_reports_three_distinct_outcomes(tmp_path):
    """The probe's own vocabulary, asserted directly.

    PRESENT, ABSENT and UNCHECKED have to be three values, not two plus a
    synonym, because the caller's refusal is built on exactly one of them.
    """
    root = tmp_path / "projects"
    root.mkdir()
    _write_transcript(root, HEAD_UUID)

    found = conversation_presence(HEAD_UUID, corpus_root=root)
    assert found.outcome == CONVERSATION_PRESENT
    assert found.missing is False

    gone = conversation_presence(ANCHOR_UUID, corpus_root=root)
    assert gone.outcome == CONVERSATION_ABSENT
    assert gone.missing is True

    unreadable = conversation_presence(
        ANCHOR_UUID, corpus_root=tmp_path / "nope"
    )
    assert unreadable.outcome == CONVERSATION_UNCHECKED
    # The whole point: not-looked must never spell itself the same way as
    # looked-and-gone.
    assert unreadable.missing is False


def test_presence_probe_finds_a_transcript_the_slug_function_would_miss(
    tmp_path,
):
    """The corpus scan is the authority, not ``slugify_project_dir``.

    Measured on the owner's box 2026-09-07: Claude Code rewrites the SPACE
    in "Mobile Documents" and the ``~`` in "com~apple~CloudDocs" to ``-``,
    and ``slugify_project_dir`` rewrites neither, so the slug names a
    directory that does not exist. A probe that trusted it would answer
    ABSENT for every session in that tree and refuse every restart.
    """
    root = tmp_path / "projects"
    root.mkdir()
    real_slug = (
        "-Users-x-Library-Mobile-Documents-com-apple-CloudDocs-Sync-"
        "Development-CloudeCode"
    )
    _write_transcript(root, HEAD_UUID, slug=real_slug)

    result = conversation_presence(
        HEAD_UUID,
        working_dir=(
            "/Users/x/Library/Mobile Documents/com~apple~CloudDocs/Sync/"
            "Development/CloudeCode"
        ),
        corpus_root=root,
    )

    assert result.outcome == CONVERSATION_PRESENT
