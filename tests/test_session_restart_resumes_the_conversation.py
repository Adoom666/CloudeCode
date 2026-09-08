"""A RESTART RESUMES THE SAME CONVERSATION, ON EVERY RUNG THAT CAN.

THE OWNER'S DEFINITION, 2026-09-07, verbatim: "restart on recent is
really just resume. restart on open is close and resume session so it
loads a new wrapper or new claude binary." One semantic, two mechanics. A
dead row has no process to kill, so its restart IS a resume. A live row
has its pane killed first, and the kill exists only so the pane picks up
a new wrapper or a new claude binary. BOTH come back on the same
conversation.

THE DEFECT THESE TESTS PIN. ``f95a9ed`` made that true on the REPLAY rung
only, where tmux happens to replay a recorded ``--resume`` it wrote down
itself (3 of the owner's 22 live sessions). The AGENT rung re-derives its
command through ``Settings.get_agent_command``, which carries no
``--resume``, so a restart there started a FRESH conversation wearing the
old session's name and silently dropped the user's working context.

FOUR PROPERTIES, AND NONE OF THEM IS THE OTHERS:

1. The stored ``sessions.claude_session_uuid`` reaches the command, as
   ``extra_args`` through the wrapper's own quoting, on the dead path and
   the live path alike.
2. A MISSING transcript refuses BOTH paths. This is the check whose
   absence produced a pane dead on the first tick while the row still
   read ``lifecycle=running``. ``unchecked`` never refuses: not having
   been able to look is not evidence a file is gone.
3. A NULL uuid is a NAMED outcome, ``none_recorded``, surfaced to the
   user - never a quiet fresh start dressed up as a restart. A row that
   could not be read is ``unknown``, which is not either of the other
   two, and an unknown is never a yes.
4. The preview predicts the action's command BYTE FOR BYTE, ``--resume``
   included. One ladder, one command builder; a badge and a button that
   describe different strings are the drift the shared tail exists to
   prevent.

Plus the four gates from ``f95a9ed``, re-pinned here because this change
touches every function they run through.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")

from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_models import SESSION_ORIGIN_OBSERVED  # noqa: E402
from src.core.session_identity import record_instance  # noqa: E402
from src.core.session_respawn import (  # noqa: E402
    RESPAWN_AGENT,
    RESPAWN_SHELL,
    RESPAWN_TRANSCRIPT_MISSING,
    project_restart_rung,
    resolve_respawn_plan,
)
from src.core.session_resume_target import (  # noqa: E402
    CONVERSATION_NONE_RECORDED,
    CONVERSATION_RESUMED,
    CONVERSATION_UNKNOWN,
    ResumeTarget,
    continuity_from_command,
    resume_extra_args,
    resume_target_from_row,
)
from src.core.session_transcript_presence import (  # noqa: E402
    CONVERSATION_ABSENT,
    CONVERSATION_UNCHECKED,
    ConversationPresence,
)
from tests.s7_helpers import migrated_connection  # noqa: E402
from tests.socket_guard import derive_test_socket  # noqa: E402

#: A syntactically valid conversation id. It names nothing on disk, which
#: is the point wherever the transcript guard is under test.
UUID = "82aabe7b-c0be-4430-b127-bbf8aad17a57"


# ---------------------------------------------------------------------------
# 1. The row's uuid is classified into three outcomes, never two
# ---------------------------------------------------------------------------


def test_a_row_holding_a_uuid_resumes_it():
    """The ordinary case: there is a conversation, so it comes back."""
    target = resume_target_from_row(
        {"claude_session_uuid": UUID}, row_read_ok=True
    )
    assert target.outcome == CONVERSATION_RESUMED
    assert target.claude_session_uuid == UUID
    assert resume_extra_args(target) == ["--resume", UUID]


def test_a_null_uuid_is_none_recorded_and_injects_nothing():
    """NOT a silent fresh start. A named state, and no ``--resume``.

    The row was READ and holds no conversation, so there is genuinely
    nothing to resume. That is a legitimate restart; performing it
    quietly and calling it a restart is the defect.
    """
    for row in ({"claude_session_uuid": None}, {"claude_session_uuid": ""}, {}):
        target = resume_target_from_row(row, row_read_ok=True)
        assert target.outcome == CONVERSATION_NONE_RECORDED
        assert target.claude_session_uuid is None
        assert resume_extra_args(target) is None
        assert "without its history" in target.detail


def test_a_row_that_could_not_be_read_is_unknown_and_never_a_yes():
    """The third outcome, and it is not a flavour of the other two.

    A datastore that did not answer is an absence of information. It must
    not read as "no conversation was ever recorded" and it must not claim
    a resume it cannot perform.
    """
    target = resume_target_from_row({"claude_session_uuid": UUID},
                                    row_read_ok=False)
    assert target.outcome == CONVERSATION_UNKNOWN
    assert target.claude_session_uuid is None
    assert resume_extra_args(target) is None
    assert "cannot be determined" in target.detail


def test_a_replay_command_that_only_continues_is_unknown_not_resumed():
    """``--continue`` names no uuid, so nothing can check or claim it."""
    assert continuity_from_command(f"claude --resume {UUID}") == (
        CONVERSATION_RESUMED
    )
    assert continuity_from_command("claude --continue") == CONVERSATION_UNKNOWN
    assert continuity_from_command("cld") == CONVERSATION_NONE_RECORDED
    assert continuity_from_command(None) == CONVERSATION_UNKNOWN
    # THE SHORT ``-c`` IS NOT MATCHED, and this is the reason: every
    # wrapper this app renders is ``zsh -c '...'``, so matching it would
    # bury every recorded start command under a permanent unknown.
    assert continuity_from_command(
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'"
    ) == CONVERSATION_NONE_RECORDED


# ---------------------------------------------------------------------------
# 2. The ladder reports continuity truthfully, per rung
# ---------------------------------------------------------------------------


def test_the_agent_rung_reports_resumed_when_the_command_carries_a_resume():
    """DERIVED FROM THE ARGV, so the claim cannot outrun the command."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        agent_command=f"zsh -c 'cld --resume {UUID}'",
        resume_outcome=CONVERSATION_RESUMED,
    )
    assert plan.kind == RESPAWN_AGENT
    assert plan.conversation == CONVERSATION_RESUMED
    assert plan.resume_uuid == UUID
    assert "resuming the same conversation" in plan.detail


def test_the_agent_rung_says_so_when_there_is_no_conversation():
    """The sentence the user reads must name the loss, not omit it."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        agent_command="zsh -c 'cld'",
        resume_outcome=CONVERSATION_NONE_RECORDED,
    )
    assert plan.kind == RESPAWN_AGENT
    assert plan.conversation == CONVERSATION_NONE_RECORDED
    assert plan.resume_uuid is None
    assert "without its history" in plan.detail


def test_a_caller_claiming_a_resume_with_no_uuid_is_downgraded():
    """A claim with nothing behind it is not repeated back to the user."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        agent_command="zsh -c 'cld'",
        resume_outcome=CONVERSATION_RESUMED,
    )
    assert plan.conversation == CONVERSATION_NONE_RECORDED


def test_an_unstated_lookup_is_unknown_not_none_recorded():
    """"Nobody told me" is not "nothing is there"."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        agent_command="zsh -c 'cld'",
    )
    assert plan.conversation == CONVERSATION_UNKNOWN
    assert "cannot be determined" in plan.detail


def test_the_shell_rung_is_none_recorded_whatever_the_row_says():
    """A login shell carries no conversation. That is measured, not unread."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command="",
        agent_command=None,
        resume_outcome=CONVERSATION_RESUMED,
    )
    assert plan.kind == RESPAWN_SHELL
    assert plan.conversation == CONVERSATION_NONE_RECORDED


# ---------------------------------------------------------------------------
# 3. THE FOUR GATES from f95a9ed, re-pinned across this change
# ---------------------------------------------------------------------------


def test_a_projection_can_never_arm_a_kill_whatever_the_conversation():
    """GATE: ``project_restart_rung`` has no liveness input, and gains none.

    ``resume_outcome`` is a new argument on both entry points. It selects
    no rung and is not liveness, so a projection must remain structurally
    incapable of setting ``kills_live_pane`` for every value of it.
    """
    for outcome in (
        None,
        CONVERSATION_RESUMED,
        CONVERSATION_NONE_RECORDED,
        CONVERSATION_UNKNOWN,
        "some-future-word",
    ):
        for start in (None, "", '"cld"'):
            for agent in (None, f"cld --resume {UUID}"):
                plan = project_restart_rung(
                    probe_ok=True,
                    pane_start_command=start,
                    agent_command=agent,
                    resume_outcome=outcome,
                )
                assert plan.kills_live_pane is False, (
                    f"a prediction armed a kill: {outcome!r} {start!r} {agent!r}"
                )


def test_only_a_measured_live_pane_and_a_confirmation_arm_the_kill():
    """GATE: the three conditions, still all three, with a resume present."""
    resuming = f"cld --resume {UUID}"
    dead = resolve_respawn_plan(
        probe_ok=True, pane_dead="1", pane_start_command='"cld"',
        agent_command=resuming, resume_outcome=CONVERSATION_RESUMED,
    )
    assert dead.kills_live_pane is False

    unconfirmed = resolve_respawn_plan(
        probe_ok=True, pane_dead="0", pane_start_command='"cld"',
        agent_command=resuming, resume_outcome=CONVERSATION_RESUMED,
    )
    assert unconfirmed.kind == "not_dead"
    assert unconfirmed.kills_live_pane is False

    confirmed = resolve_respawn_plan(
        probe_ok=True, pane_dead="0", pane_start_command='"cld"',
        agent_command=resuming, resume_outcome=CONVERSATION_RESUMED,
        live_restart_confirmed=True,
    )
    assert confirmed.kind == RESPAWN_AGENT
    assert confirmed.kills_live_pane is True
    assert confirmed.conversation == CONVERSATION_RESUMED


# ---------------------------------------------------------------------------
# 4. The transcript guard, on BOTH paths
# ---------------------------------------------------------------------------

pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="real tmux binary not available"
)


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _pane_dead(socket: str, name: str) -> str:
    proc = _tmux(socket, "list-panes", "-t", name, "-F", "#{pane_dead}")
    return proc.stdout.strip() if proc.returncode == 0 else "?"


def _wait_for(socket: str, name: str, want: str, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pane_dead(socket, name) == want:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture()
def socket(request):
    """A SCRATCH tmux socket, never ``cloude``. Killed on teardown."""
    name = derive_test_socket(f"res_{request.node.name[:18]}")
    _tmux(name, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(name, "set-option", "-wg", "remain-on-exit", "on")
    yield name
    subprocess.run(
        ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
    )


def _manager():
    from unittest.mock import patch

    from src.core.session_manager import SessionManager

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        return SessionManager()


def _seed_row(db_dir: Path, socket: str, name: str, epoch: int, uuid):
    """Create the sessions row this restart will read.

    Inputs: db_dir (Path) - state dir. socket (str). name (str). epoch
      (int) - ``#{session_created}``. uuid (str | None) - the
      conversation to record, or None for the none_recorded case.
    Output: None.
    """
    with closing(migrated_connection(db_dir)):
        pass
    with closing(connect(db_path_for(db_dir))) as conn:
        record_instance(
            conn,
            socket=socket,
            name=name,
            epoch=epoch,
            origin=SESSION_ORIGIN_OBSERVED,
        )
        conn.execute(
            "UPDATE sessions SET claude_session_uuid = ?, agent_type = 'claude'",
            (uuid,),
        )
        conn.commit()


def _echo_argv_command(marker: Path) -> str:
    """A launch command that records the arguments it was given.

    Description: the wrapper's own rendering is ``get_agent_command``'s
      job and is tested with it. What THIS file has to prove is that the
      ``--resume`` reaches the pane at all, so the fake command writes
      its own ``"$@"`` to a file and the assertion is made on what tmux
      actually ran.
    Inputs: marker (Path) - file to write the arguments into.
    Output: str - a shell command string.
    """
    return f"sh -c 'printf \"%s\\n\" \"$@\" > {marker}; sleep 30' _"


def _patch_command(monkeypatch, sm, base: str):
    """Render ``extra_args`` onto a known command, as the real one does.

    Inputs: monkeypatch (pytest fixture). sm (the session_manager
      module). base (str) - the command before any extra arguments.
    Output: None.
    """
    import shlex

    def render(self, agent_type, model=None, extra_args=None):
        tail = "".join(f" {shlex.quote(a)}" for a in (extra_args or []))
        return base + tail

    monkeypatch.setattr(type(sm.settings), "get_agent_command", render)


def _new_dead_pane(socket: str, name: str, cwd: Path) -> int:
    """A pane with a recorded start command whose process has exited.

    Inputs: socket (str). name (str). cwd (Path).
    Output: int - the session's ``#{session_created}`` epoch.
    """
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(cwd),
        "-x", "80", "-y", "24", 'sh -c "sleep 0.2; exit 0"',
    )
    epoch = int(
        _tmux(
            socket, "display-message", "-p", "-t", name, "#{session_created}"
        ).stdout.strip()
    )
    assert _wait_for(socket, name, "1"), "setup: the pane did not die"
    return epoch


@pytestmark_tmux
@pytest.mark.asyncio
async def test_the_dead_agent_rung_resumes_the_row_s_conversation(
    socket, tmp_path, monkeypatch
):
    """THE HEADLINE. A restart on a dead row comes back on the same chat.

    Fails without the fix: ``get_agent_command`` is called with no
    ``extra_args``, the pane comes back with a bare command, and the
    marker file holds nothing.
    """
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    name = "resumedead"
    marker = tmp_path / "argv.txt"
    epoch = _new_dead_pane(socket, name, tmp_path)
    _seed_row(db_dir, socket, name, epoch, UUID)

    monkeypatch.setattr(type(sm.settings), "get_state_dir", lambda self: db_dir)
    _patch_command(monkeypatch, sm, _echo_argv_command(marker))
    # The transcript is not on this machine, so the guard must be told it
    # could not look. UNCHECKED never refuses - that is the whole rule.
    monkeypatch.setattr(
        "src.core.session_transcript_presence.conversation_presence",
        lambda *a, **k: ConversationPresence(
            outcome=CONVERSATION_UNCHECKED, detail="no corpus in this test"
        ),
    )

    mgr = _manager()
    result = await mgr.respawn_session(name, socket_name=socket)

    assert result["kind"] == RESPAWN_AGENT, result["detail"]
    assert result["ok"] is True, result["detail"]
    assert result["conversation"] == CONVERSATION_RESUMED
    assert result["killed_live_pane"] is False, "a dead pane has nothing to kill"
    assert f"--resume {UUID}" in (result["command"] or "")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.exists(), "the restarted command never ran"
    argv = marker.read_text().split()
    assert argv == ["--resume", UUID], (
        f"the pane did not resume the row's conversation; got {argv!r}. "
        "A restart that starts a fresh conversation is the defect."
    )


@pytestmark_tmux
@pytest.mark.asyncio
async def test_a_missing_transcript_refuses_the_dead_agent_rung(
    socket, tmp_path, monkeypatch
):
    """A DEFINITE absence refuses, and spawns nothing, on the DEAD path.

    Fails without the fix: with no ``--resume`` on the agent rung the
    plan carries no ``resume_uuid``, the guard never runs, and the
    restart happily starts a pane on a conversation that is gone - which
    exits at once while the row still reads running.
    """
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    name = "gonechat"
    marker = tmp_path / "argv.txt"
    epoch = _new_dead_pane(socket, name, tmp_path)
    _seed_row(db_dir, socket, name, epoch, UUID)

    monkeypatch.setattr(type(sm.settings), "get_state_dir", lambda self: db_dir)
    _patch_command(monkeypatch, sm, _echo_argv_command(marker))
    monkeypatch.setattr(
        "src.core.session_transcript_presence.conversation_presence",
        lambda *a, **k: ConversationPresence(
            outcome=CONVERSATION_ABSENT,
            detail="that conversation has no transcript on this machine",
        ),
    )

    mgr = _manager()
    result = await mgr.respawn_session(name, socket_name=socket)

    assert result["kind"] == RESPAWN_TRANSCRIPT_MISSING, result
    assert result["ok"] is False
    assert result["killed_live_pane"] is False
    assert _pane_dead(socket, name) == "1", "a refused restart spawned something"
    assert not marker.exists(), "the refused command ran anyway"


@pytestmark_tmux
@pytest.mark.asyncio
async def test_a_missing_transcript_refuses_the_live_path_and_kills_nothing(
    socket, tmp_path, monkeypatch
):
    """The same refusal where it costs most: the pane is WORKING.

    On the live path the refusal is what stops ``-k`` reaching tmux, so a
    conversation that is gone cannot cost the user the session that is
    running.
    """
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    name = "liveguard"
    marker = tmp_path / "argv.txt"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 30"',
    )
    epoch = int(
        _tmux(
            socket, "display-message", "-p", "-t", name, "#{session_created}"
        ).stdout.strip()
    )
    assert _pane_dead(socket, name) == "0", "setup: the pane should be alive"
    _seed_row(db_dir, socket, name, epoch, UUID)

    monkeypatch.setattr(type(sm.settings), "get_state_dir", lambda self: db_dir)
    _patch_command(monkeypatch, sm, _echo_argv_command(marker))
    monkeypatch.setattr(
        "src.core.session_transcript_presence.conversation_presence",
        lambda *a, **k: ConversationPresence(
            outcome=CONVERSATION_ABSENT, detail="gone"
        ),
    )

    mgr = _manager()
    result = await mgr.respawn_session(
        name, socket_name=socket, live_restart_confirmed=True
    )

    assert result["kind"] == RESPAWN_TRANSCRIPT_MISSING, result
    assert result["ok"] is False
    assert result["killed_live_pane"] is False
    assert _pane_dead(socket, name) == "0", (
        "the live pane was killed for a conversation that is not there"
    )
    assert not marker.exists()


@pytestmark_tmux
@pytest.mark.asyncio
async def test_a_live_restart_resumes_and_kills_exactly_one_process(
    socket, tmp_path, monkeypatch
):
    """The owner's second mechanic: kill, then come back on the same chat."""
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    name = "liveresume"
    marker = tmp_path / "argv.txt"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 30"',
    )
    epoch = int(
        _tmux(
            socket, "display-message", "-p", "-t", name, "#{session_created}"
        ).stdout.strip()
    )
    _seed_row(db_dir, socket, name, epoch, UUID)

    monkeypatch.setattr(type(sm.settings), "get_state_dir", lambda self: db_dir)
    _patch_command(monkeypatch, sm, _echo_argv_command(marker))
    monkeypatch.setattr(
        "src.core.session_transcript_presence.conversation_presence",
        lambda *a, **k: ConversationPresence(
            outcome=CONVERSATION_UNCHECKED, detail="not looked"
        ),
    )

    mgr = _manager()
    result = await mgr.respawn_session(
        name, socket_name=socket, live_restart_confirmed=True
    )

    assert result["ok"] is True, result["detail"]
    assert result["kind"] == RESPAWN_AGENT
    assert result["killed_live_pane"] is True
    assert result["conversation"] == CONVERSATION_RESUMED

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.exists(), "the live restart never ran the new command"
    assert marker.read_text().split() == ["--resume", UUID]


@pytestmark_tmux
@pytest.mark.asyncio
async def test_a_null_uuid_restarts_and_says_it_lost_the_history(
    socket, tmp_path, monkeypatch
):
    """No conversation to resume is REPORTED, never silently performed."""
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    name = "nochat"
    marker = tmp_path / "argv.txt"
    epoch = _new_dead_pane(socket, name, tmp_path)
    _seed_row(db_dir, socket, name, epoch, None)

    monkeypatch.setattr(type(sm.settings), "get_state_dir", lambda self: db_dir)
    _patch_command(monkeypatch, sm, _echo_argv_command(marker))

    mgr = _manager()
    result = await mgr.respawn_session(name, socket_name=socket)

    assert result["ok"] is True, result["detail"]
    assert result["kind"] == RESPAWN_AGENT
    assert result["conversation"] == CONVERSATION_NONE_RECORDED
    assert "--resume" not in (result["command"] or "")
    assert "without its history" in result["detail"], (
        "the user was not told this session comes back blank"
    )


# ---------------------------------------------------------------------------
# 5. The preview predicts the action, --resume included
# ---------------------------------------------------------------------------


@pytestmark_tmux
@pytest.mark.asyncio
async def test_the_preview_and_the_action_agree_on_the_whole_command(
    socket, tmp_path, monkeypatch
):
    """BYTE FOR BYTE, or the badge and the button describe different things.

    Fails without the fix on the action side: the preview would render a
    ``--resume`` the respawn then dropped, or the reverse. Both are read
    from the SAME lookup here, which is what makes them agree by
    construction.
    """
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    name = "agree"
    marker = tmp_path / "argv.txt"
    epoch = _new_dead_pane(socket, name, tmp_path)
    _seed_row(db_dir, socket, name, epoch, UUID)

    monkeypatch.setattr(type(sm.settings), "get_state_dir", lambda self: db_dir)
    _patch_command(monkeypatch, sm, _echo_argv_command(marker))
    monkeypatch.setattr(
        "src.core.session_transcript_presence.conversation_presence",
        lambda *a, **k: ConversationPresence(
            outcome=CONVERSATION_UNCHECKED, detail="not looked"
        ),
    )

    mgr = _manager()
    preview = await mgr.restart_preview(name, socket_name=socket)
    predicted = preview.unchanged.command
    assert predicted and f"--resume {UUID}" in predicted, (
        f"the preview did not predict the resume; got {predicted!r}"
    )
    assert preview.unchanged.conversation == CONVERSATION_RESUMED
    assert preview.projected.conversation == CONVERSATION_RESUMED

    result = await mgr.respawn_session(name, socket_name=socket)
    assert result["command"] == predicted, (
        "the preview promised a different command from the one that ran"
    )


@pytestmark_tmux
@pytest.mark.asyncio
async def test_every_wrapper_option_predicts_its_own_resume(
    socket, tmp_path, monkeypatch
):
    """A picked wrapper resumes too, and the preview says the same string."""
    from src.core import session_manager as sm
    from src.core.session_agent_choice import validate_agent_choice

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    name = "wrapresume"
    marker = tmp_path / "argv.txt"
    epoch = _new_dead_pane(socket, name, tmp_path)
    _seed_row(db_dir, socket, name, epoch, UUID)

    class _W:
        def __init__(self, wid):
            self.id = wid
            self.label = wid

    class _Auth:
        def __init__(self, wrappers):
            self.agents = type("A", (), {"wrappers": wrappers})()

    monkeypatch.setattr(type(sm.settings), "get_state_dir", lambda self: db_dir)
    monkeypatch.setattr(
        type(sm.settings),
        "load_auth_config",
        lambda self: _Auth([_W("claude-chrome")]),
    )
    _patch_command(monkeypatch, sm, _echo_argv_command(marker))
    monkeypatch.setattr(
        "src.core.session_transcript_presence.conversation_presence",
        lambda *a, **k: ConversationPresence(
            outcome=CONVERSATION_UNCHECKED, detail="not looked"
        ),
    )

    mgr = _manager()
    preview = await mgr.restart_preview(name, socket_name=socket)
    assert len(preview.options) == 1
    option = preview.options[0]
    assert f"--resume {UUID}" in (option.command or "")
    assert option.conversation == CONVERSATION_RESUMED

    # The action builds the picked wrapper's command through the SAME
    # validator with the SAME extra args, so the two strings must match.
    choice = validate_agent_choice(
        sm.settings,
        "claude-chrome",
        extra_args=resume_extra_args(
            ResumeTarget(CONVERSATION_RESUMED, UUID, "")
        ),
    )
    assert choice.command == option.command


# ---------------------------------------------------------------------------
# 6. The preview keeps two conversations apart
# ---------------------------------------------------------------------------


def test_a_verdict_about_one_conversation_never_refuses_another():
    """Two rungs can resume two different uuids now.

    The replay rung re-runs tmux's recorded command; the agent rung
    resumes the row's uuid. A transcript verdict measured for one says
    nothing about the other, and applying it across would refuse a
    restart nobody measured.
    """
    from src.core.session_restart_preview import build_restart_preview

    other = "11111111-2222-3333-4444-555555555555"
    preview = build_restart_preview(
        name="two",
        probe_ok=True,
        pane_dead="1",
        pane_start_command=f'"cld --resume {other}"',
        stored_agent_type="claude",
        stored_agent_command=f"cld --resume {UUID}",
        offers=[],
        # Only the RECORDED command's conversation is gone.
        presence_by_uuid={other: (CONVERSATION_ABSENT, "that one is gone")},
        resume_outcome=CONVERSATION_RESUMED,
    )
    # The agent rung wins over replay and resumes the ROW's uuid, which
    # nothing said was missing, so it must not be refused.
    assert preview.unchanged.kind == RESPAWN_AGENT
    assert preview.unchanged.resume_uuid == UUID
    assert preview.unchanged.conversation == CONVERSATION_RESUMED
