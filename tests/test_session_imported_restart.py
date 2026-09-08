"""Restarting a conversation that has no tmux identity.

THE CLAIM: an imported row - one rebuilt from a transcript, with no tmux
name, no epoch and no pane - can be restarted, and a restart RESUMES it.
CLAUDE.md's rule is "A RESTART MEANS A RESUME, ON EVERY RUNG THAT CAN";
this is that rule taken one step further, where there is no pane to
respawn either, so the restart creates one.

THE NEGATIVE CONTROLS ARE THE POINT, as everywhere else in this feature:

  * a MEASURED missing transcript refuses, because `--resume` against a
    deleted file exits instantly and leaves a dead pane the row still
    calls running - the incident this project has already paid for;
  * an UNCHECKED corpus never refuses, because not having looked is not
    evidence of absence;
  * the directory a resume runs in is MEASURED across every spelling,
    not assumed from the row - resuming from the wrong spelling of the
    right directory finds nothing;
  * a row that HAS a tmux identity is refused by these endpoints, so the
    two paths cannot silently swap.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ir_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ir_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.auth import require_auth  # noqa: E402
from src.api.imported_restart_routes import router as imported_router  # noqa: E402
from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_migration import ensure_db_migrated  # noqa: E402
from src.core.session_imported_restart import (  # noqa: E402
    DIRECTORY_MEASURED,
    DIRECTORY_NOT_FOUND,
    DIRECTORY_UNCHECKED,
    ResumeDirectory,
    as_respawn_plan,
    imported_extra_args,
    is_imported_row,
    plan_imported_restart,
    resume_directory,
)
from src.core.session_respawn import (  # noqa: E402
    RESPAWN_AGENT,
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_TRANSCRIPT_MISSING,
)

UUID = "aaaaaaaa-1111-2222-3333-444444444444"
ROW_UUID = "row-uuid-1"


def imported_row(**overrides):
    """A sessions row as the importer writes one."""
    row = {
        "id": 7,
        "session_uuid": ROW_UUID,
        "tmux_name": None,
        "tmux_created_epoch": None,
        "working_dir": "/Users/x/proj",
        "claude_session_uuid": UUID,
        "title": "fix the deploy script",
        "claude_title": None,
        "agent_type": None,
        "origin": "imported",
    }
    row.update(overrides)
    return row


# --- which directory a resume actually runs in -----------------------------


def test_the_spelling_that_holds_the_transcript_is_the_one_used(tmp_path: Path):
    """MEASURED, not assumed. This is the whole reason the function exists.

    `claude --resume <uuid>` looks for <uuid>.jsonl under the project
    directory Claude Code derives from the LITERAL cwd string. The row
    stores the canonical long spelling; 211 of the owner's Media
    transcripts were recorded under the short symlinked one. Launching in
    the wrong spelling of the right directory finds nothing.
    """
    real = tmp_path / "iCloud" / "Development" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "Development"
    link.symlink_to(tmp_path / "iCloud" / "Development")

    corpus = tmp_path / "corpus"
    from src.core.claude_transcript_correlate import slugify_project_dir

    short = str(link / "proj")
    slug = corpus / slugify_project_dir(short)
    slug.mkdir(parents=True)
    (slug / f"{UUID}.jsonl").write_text("{}\n", encoding="utf-8")

    found = resume_directory(short, UUID, corpus_root=corpus)
    assert found.outcome == DIRECTORY_MEASURED
    assert found.working_dir == short
    assert found.refuses is False


def test_a_measured_absence_refuses(tmp_path: Path):
    """NEGATIVE CONTROL: a resume against a deleted transcript exits at once."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    found = resume_directory("/Users/x/proj", UUID, corpus_root=corpus)
    assert found.outcome == DIRECTORY_NOT_FOUND
    assert found.refuses is True
    assert found.working_dir is None, (
        "a caller falling back to the row's own spelling on a refusal "
        "would be doing exactly what this exists to stop"
    )


def test_an_unreadable_corpus_never_refuses(tmp_path: Path):
    """NEGATIVE CONTROL: not having looked is not evidence of absence.

    Refusing here would break restart on every machine whose corpus lives
    somewhere the checker was not told about.
    """
    for outcome in (
        resume_directory(None, UUID, corpus_root=tmp_path),
        resume_directory("/Users/x/proj", None, corpus_root=tmp_path),
    ):
        assert outcome.outcome == DIRECTORY_UNCHECKED
        assert outcome.refuses is False


# --- which rows this path is for -------------------------------------------


def test_the_test_is_the_absence_of_an_identity_not_the_origin_label():
    """A row that lost its instance is in the same position, whatever it says."""
    assert is_imported_row(imported_row()) is True
    assert is_imported_row(imported_row(origin="created")) is True
    assert is_imported_row(imported_row(tmux_name="cloude_x")) is False
    assert is_imported_row(imported_row(tmux_created_epoch=1000)) is False
    assert is_imported_row(None) is False


# --- the plan --------------------------------------------------------------


def _measured(directory="/Users/x/proj"):
    return ResumeDirectory(outcome=DIRECTORY_MEASURED, working_dir=directory)


def test_a_picked_wrapper_resumes_the_conversation():
    plan = plan_imported_restart(
        imported_row(),
        row_read_ok=True,
        choice_verdict="accepted",
        choice_command=f"zsh -c 'cld --resume {UUID}'",
        agent_type="claude-chrome",
        directory=_measured(),
    )
    assert plan.kind == RESPAWN_AGENT
    assert plan.actionable is True
    assert plan.conversation == "resumed"
    assert plan.reuse_session_id == 7, (
        "the new tmux instance must be recorded onto the IMPORTED row, or "
        "a second row appears beside the conversation"
    )
    assert plan.label == "fix the deploy script"
    assert plan.working_dir == "/Users/x/proj"


def test_the_conversation_claim_is_derived_from_the_argv():
    """A command with no --resume can never claim to resume."""
    plan = plan_imported_restart(
        imported_row(),
        row_read_ok=True,
        choice_verdict="accepted",
        choice_command="zsh -c 'cld'",
        agent_type="claude",
        directory=_measured(),
    )
    assert plan.kind == RESPAWN_AGENT
    assert plan.conversation != "resumed"


def test_a_missing_transcript_refuses_ahead_of_the_wrapper():
    """No wrapper choice can make a deleted conversation resumable."""
    plan = plan_imported_restart(
        imported_row(),
        row_read_ok=True,
        choice_verdict="accepted",
        choice_command=f"zsh -c 'cld --resume {UUID}'",
        agent_type="claude-chrome",
        directory=ResumeDirectory(
            outcome=DIRECTORY_NOT_FOUND, detail="no transcript is filed"
        ),
    )
    assert plan.kind == RESPAWN_TRANSCRIPT_MISSING
    assert plan.actionable is False
    assert plan.command is None


def test_an_unresolved_wrapper_is_cannot_determine_not_a_shell():
    """An imported restart with no wrapper is NOT offered as a login shell.

    The pane path warns about the shell rung because a pane at least has
    a recorded start command to fall back on. Here there is nothing, so
    the outcome is refused rather than warned about.
    """
    plan = plan_imported_restart(
        imported_row(),
        row_read_ok=True,
        choice_verdict="unknown_agent",
        choice_command=None,
        choice_detail="'nope' is not configured",
        agent_type="nope",
        directory=_measured(),
    )
    assert plan.kind == RESPAWN_CANNOT_DETERMINE
    assert plan.actionable is False
    assert "not configured" in plan.detail


def test_an_unreadable_row_is_cannot_determine():
    plan = plan_imported_restart(
        None,
        row_read_ok=False,
        choice_verdict="accepted",
        choice_command="zsh -c 'cld'",
    )
    assert plan.kind == RESPAWN_CANNOT_DETERMINE
    assert plan.conversation == "unknown"


def test_a_row_with_no_conversation_says_so_rather_than_pretending():
    plan = plan_imported_restart(
        imported_row(claude_session_uuid=None),
        row_read_ok=True,
        choice_verdict="accepted",
        choice_command="zsh -c 'cld'",
        agent_type="claude",
        directory=_measured(),
    )
    assert plan.conversation == "none_recorded"
    assert "WITHOUT its history" in plan.detail


def test_the_resume_fragment_is_built_in_one_place():
    assert imported_extra_args(imported_row(), row_read_ok=True) == [
        "--resume",
        UUID,
    ]
    assert imported_extra_args(imported_row(), row_read_ok=False) is None
    assert (
        imported_extra_args(imported_row(claude_session_uuid=None), row_read_ok=True)
        is None
    )


def test_a_projection_can_never_kill_a_pane():
    """There is no live pane here, and no route to `-k` that could exist."""
    plan = plan_imported_restart(
        imported_row(),
        row_read_ok=True,
        choice_verdict="accepted",
        choice_command=f"zsh -c 'cld --resume {UUID}'",
        agent_type="claude",
        directory=_measured(),
    )
    assert as_respawn_plan(plan).kills_live_pane is False


# --- over HTTP -------------------------------------------------------------


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """A TestClient over the imported-restart routes and a real datastore."""
    from src.config import settings

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(
        type(settings), "get_state_dir", lambda self: state_dir, raising=True
    )
    assert ensure_db_migrated(state_dir).status == "ok"

    app = FastAPI()
    app.include_router(imported_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    with TestClient(app) as client:
        yield client, state_dir


def _insert(state_dir, **overrides):
    """Write one sessions row and return its session_uuid."""
    row = {
        "session_uuid": ROW_UUID,
        "tmux_name": None,
        "tmux_created_epoch": None,
        "working_dir": "/Users/x/proj",
        "claude_session_uuid": UUID,
        "title": "fix the deploy script",
    }
    row.update(overrides)
    with connect(db_path_for(state_dir), create=False) as conn:
        conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, working_dir, claude_session_uuid, title, "
            "origin, lifecycle, archived_at, created_at, updated_at) "
            "VALUES (?, 'cloude', ?, ?, ?, ?, ?, 'imported', 'stopped', "
            "'2026-09-08T00:00:00Z', '2026-03-01T00:00:00Z', "
            "'2026-09-08T00:00:00Z')",
            (
                row["session_uuid"],
                row["tmux_name"],
                row["tmux_created_epoch"],
                row["working_dir"],
                row["claude_session_uuid"],
                row["title"],
            ),
        )
    return row["session_uuid"]


def test_the_preview_answers_for_a_row_with_no_tmux_identity(app_env):
    """THE VERIFICATION D ASKED FOR, over HTTP against a real row."""
    client, state_dir = app_env
    _insert(state_dir)
    r = client.get(
        "/api/v1/sessions/imported/restart/preview",
        params={"session_uuid": ROW_UUID},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pane_state"] == "unknown", (
        "there is no pane, so its state is not a fact anybody measured"
    )
    assert body["unchanged"]["actionable"] is False, (
        "an unpicked restart of an imported row is not offered at all"
    )
    assert body["wrappers_status"] in ("ok", "unavailable")


def test_the_preview_refuses_a_row_that_has_a_tmux_identity(app_env):
    """NEGATIVE CONTROL: the two restart paths cannot silently swap.

    A 409 rather than a 404, because the row EXISTS and the caller used
    the wrong endpoint for it. 'not found' would send them looking for a
    row that is right there.
    """
    client, state_dir = app_env
    _insert(state_dir, tmux_name="cloude_live", tmux_created_epoch=1000)
    r = client.get(
        "/api/v1/sessions/imported/restart/preview",
        params={"session_uuid": ROW_UUID},
    )
    assert r.status_code == 409
    assert "cloude_live" in r.json()["detail"]


def test_an_unknown_session_uuid_is_a_404(app_env):
    client, _ = app_env
    r = client.get(
        "/api/v1/sessions/imported/restart/preview",
        params={"session_uuid": "nope"},
    )
    assert r.status_code == 404


def test_the_action_refuses_rather_than_starting_when_a_gate_declines(app_env):
    """A refusal is a 200 with status='refused', never a silent success.

    Nothing is spawned: the transcript for this conversation is not on
    disk in this test's environment, so the guard declines before the
    manager is ever reached - which is also why this test needs no
    session manager on app.state.
    """
    client, state_dir = app_env
    _insert(state_dir)
    r = client.post(
        "/api/v1/sessions/imported/restart",
        json={"session_uuid": ROW_UUID, "agent_type": "not-a-configured-wrapper"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "refused"
    assert body["session_uuid"] == ROW_UUID
    assert body["detail"]
