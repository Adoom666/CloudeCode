"""Picking a wrapper at restart time, and being told the truth first.

TWO THINGS ARE UNDER TEST HERE and they are not the same thing.

1. THE CHOICE ACTUALLY RUNS. The respawn ladder gates on tmux's
   ``#{pane_start_command}``: empty means ``RESPAWN_SHELL``, which hands
   back a LOGIN SHELL. That gate exists because the STORED
   ``sessions.agent_type`` is written on every create and so is not
   evidence of intent. A wrapper the user picked in this request is
   different evidence, so it outranks the gate - and the real-tmux test
   below proves the picked command is what ends up in the pane, not a
   shell.

2. THE PREVIEW CANNOT LIE. The rung is predicted by the SAME function
   the action runs, before anything is spawned, so a picker cannot
   promise ``claude-chrome`` and deliver zsh. Every refusal keeps its own
   name: ``not_dead`` and ``cannot_determine`` are answers, not
   instructions, and no choice of wrapper turns either into a yes.

A LIVE SESSION'S RUNG IS NOW PREDICTED, AND THAT IS NOT THE SAME AS
RESTARTING ONE. ``project_restart_rung`` answers "what would this come
back as" for a pane that is still running, because the sessions worth
warning about are exactly the live ones. It is a PREDICTION, NEVER A
PERMISSION: a live pane still answers ``not_dead`` on every acting path,
there is a test below that pins it staying that way even when a wrapper
is picked, and nothing here passes ``-k``. So this feature cannot quietly
grow into one that kills running agents.

WHAT IS STILL NOT BUILT: close-and-recreate, the second half of TODO item
22. Its design questions are unanswered (same tmux name or a new one;
``--resume`` or a fresh transcript) and it walks into the
``session_group_members`` primary-key defect.
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
from src.core.session_agent_choice import (  # noqa: E402
    CHOICE_ACCEPTED,
    CHOICE_CANNOT_DETERMINE,
    CHOICE_UNKNOWN,
    validate_agent_choice,
)
from src.core.session_identity import record_instance  # noqa: E402
from src.core.session_respawn import (  # noqa: E402
    RESPAWN_AGENT,
    RESPAWN_TRANSCRIPT_MISSING,
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_NOT_DEAD,
    RESPAWN_SHELL,
    resolve_respawn_plan,
)
from src.core.session_restart_preview import (  # noqa: E402
    WRAPPERS_OK,
    WRAPPERS_UNAVAILABLE,
    WrapperOffer,
    build_restart_preview,
)
from tests.s7_helpers import migrated_connection  # noqa: E402
from tests.socket_guard import derive_test_socket  # noqa: E402


# ---------------------------------------------------------------------------
# The ladder - pure, no tmux needed
# ---------------------------------------------------------------------------


def test_a_picked_wrapper_beats_an_empty_start_command():
    """THE landmine, disarmed. This is the whole feature in one assert.

    A pane born as a bare shell has an EMPTY ``pane_start_command`` and
    the ladder lands on ``shell``. Before this change there was no way to
    say "no, run claude-chrome in it" - the restart button silently
    handed back a login shell. An explicit choice now wins, and the plan
    marks itself ``chosen`` so the sentence shown to the user can say the
    pane was a shell and is about to stop being one.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command="",
        agent_command=None,
        chosen_agent_command="run-claude-chrome",
        chosen_agent_type="claude-chrome",
    )
    assert plan.kind == RESPAWN_AGENT
    assert plan.command == "run-claude-chrome"
    assert plan.chosen is True
    assert "plain shell" in plan.detail
    assert "claude-chrome" in plan.detail


def test_a_picked_wrapper_beats_an_unreadable_start_command():
    """``None`` start command plus a choice is determined, not unknown.

    The missing information is exactly the information the user just
    supplied, and the sentence says which half was missing.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command=None,
        agent_command=None,
        chosen_agent_command="run-claude-chrome",
        chosen_agent_type="claude-chrome",
    )
    assert plan.kind == RESPAWN_AGENT
    assert plan.chosen is True
    assert "did not report a start command" in plan.detail


def test_a_picked_wrapper_replaces_the_stored_one():
    """A session that HAS a recorded agent still honours the new pick."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        agent_command="cld",
        chosen_agent_command="run-claude-chrome",
        chosen_agent_type="claude-chrome",
    )
    assert plan.command == "run-claude-chrome"
    assert plan.chosen is True


def test_a_picked_wrapper_never_revives_a_live_pane():
    """THE SCOPE BOUNDARY, pinned. Restarting a LIVE session is item 22.

    No wrapper choice may turn ``not_dead`` into an action. tmux itself
    also refuses ``respawn-pane`` on a live pane without ``-k``, which
    this code never passes, but the ladder must not even try - a picker
    that kills a running agent is the single worst outcome available
    here.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="0",
        pane_start_command="",
        agent_command=None,
        chosen_agent_command="run-claude-chrome",
        chosen_agent_type="claude-chrome",
    )
    assert plan.kind == RESPAWN_NOT_DEAD
    assert plan.actionable is False
    assert plan.command is None
    assert plan.chosen is False


def test_a_picked_wrapper_never_overrides_a_probe_that_did_not_answer():
    """Cannot-determine must never render as a yes, choice or no choice."""
    plan = resolve_respawn_plan(
        probe_ok=False,
        pane_dead=None,
        pane_start_command=None,
        agent_command=None,
        chosen_agent_command="run-claude-chrome",
        chosen_agent_type="claude-chrome",
    )
    assert plan.kind == RESPAWN_CANNOT_DETERMINE
    assert plan.actionable is False
    assert plan.chosen is False


def test_no_choice_leaves_the_ladder_byte_for_byte_as_it_was():
    """The default path is untouched. Regression guard for every caller."""
    assert (
        resolve_respawn_plan(
            probe_ok=True,
            pane_dead="1",
            pane_start_command="",
            agent_command="cld",
        ).kind
        == RESPAWN_SHELL
    )
    assert (
        resolve_respawn_plan(
            probe_ok=True,
            pane_dead="1",
            pane_start_command='"cld"',
            agent_command=None,
        ).kind
        == "replay"
    )
    # An empty string is not a choice. A caller that passes through a
    # blank form field must not accidentally arm the override.
    assert (
        resolve_respawn_plan(
            probe_ok=True,
            pane_dead="1",
            pane_start_command="",
            agent_command="cld",
            chosen_agent_command="   ",
        ).kind
        == RESPAWN_SHELL
    )


# ---------------------------------------------------------------------------
# The preview - what the picker is allowed to say
# ---------------------------------------------------------------------------


def test_preview_says_shell_for_the_baseline_and_agent_for_the_pick():
    """Both answers, from one reading of the pane, side by side.

    This is what makes an honest warning possible: the user sees that
    restarting as-is gives a shell AND that picking claude-chrome does
    not, before committing to either.
    """
    preview = build_restart_preview(
        name="cloude_api",
        probe_ok=True,
        pane_dead="1",
        pane_start_command="",
        stored_agent_type=None,
        stored_agent_command=None,
        offers=[
            WrapperOffer(agent_type="claude-chrome", label="chrome", command="cc"),
            WrapperOffer(agent_type="cldl", label="lm studio", command="cl"),
        ],
    )
    assert preview.unchanged.kind == RESPAWN_SHELL
    assert preview.wrappers_status == WRAPPERS_OK
    assert [o.kind for o in preview.options] == [RESPAWN_AGENT, RESPAWN_AGENT]
    assert all(o.resolvable and o.actionable_now for o in preview.options)
    assert [o.agent_type for o in preview.options] == ["claude-chrome", "cldl"]


def test_preview_marks_the_current_wrapper_without_reordering():
    preview = build_restart_preview(
        name="cloude_api",
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        stored_agent_type="cldl",
        stored_agent_command="cl",
        offers=[
            WrapperOffer(agent_type="claude-chrome", command="cc"),
            WrapperOffer(agent_type="cldl", command="cl"),
        ],
    )
    assert [o.is_current for o in preview.options] == [False, True]
    assert preview.current_agent_type == "cldl"
    # A wrapper with no label falls back to its id rather than rendering
    # an empty row.
    assert preview.options[0].label == "claude-chrome"


def test_preview_keeps_an_unlaunchable_wrapper_in_the_list_with_a_reason():
    """A choice that vanishes reads as a choice that was never made."""
    preview = build_restart_preview(
        name="cloude_api",
        probe_ok=True,
        pane_dead="1",
        pane_start_command="",
        stored_agent_type=None,
        stored_agent_command=None,
        offers=[
            WrapperOffer(
                agent_type="cldl",
                command=None,
                unavailable_reason="the 'cldl' agent needs a model",
            )
        ],
    )
    assert len(preview.options) == 1
    assert preview.options[0].resolvable is False
    assert preview.options[0].actionable_now is False
    assert preview.options[0].kind == RESPAWN_CANNOT_DETERMINE
    assert "needs a model" in preview.options[0].detail


def test_preview_distinguishes_no_wrappers_from_could_not_read_them():
    """The third outcome for the LIST itself, not just for each option."""
    empty = build_restart_preview(
        name="a",
        probe_ok=True,
        pane_dead="1",
        pane_start_command="",
        stored_agent_type=None,
        stored_agent_command=None,
        offers=[],
    )
    unknown = build_restart_preview(
        name="a",
        probe_ok=True,
        pane_dead="1",
        pane_start_command="",
        stored_agent_type=None,
        stored_agent_command=None,
        offers=None,
    )
    assert empty.wrappers_status == WRAPPERS_OK
    assert unknown.wrappers_status == WRAPPERS_UNAVAILABLE
    assert empty.options == unknown.options == []
    assert empty.wrappers_status != unknown.wrappers_status


def test_preview_of_a_live_session_offers_nothing_actionable():
    """Every option on a running session is not_dead, and none is a yes."""
    preview = build_restart_preview(
        name="a",
        probe_ok=True,
        pane_dead="0",
        pane_start_command='"cld"',
        stored_agent_type="cld",
        stored_agent_command="cld",
        offers=[WrapperOffer(agent_type="claude-chrome", command="cc")],
    )
    assert preview.unchanged.kind == RESPAWN_NOT_DEAD
    assert preview.unchanged.actionable is False
    assert [o.kind for o in preview.options] == [RESPAWN_NOT_DEAD]
    assert not any(o.actionable_now for o in preview.options)
    # ...but the wrapper itself resolved perfectly well. The old single
    # 'available' flag reported these two different facts as one, so a
    # live session's options were indistinguishable from unconfigured ones.
    assert all(o.resolvable for o in preview.options)


def test_the_preview_applies_the_same_transcript_guard_as_the_action():
    """A preview that skipped the guard would promise a refused restart.

    ``TmuxBackend.respawn`` refuses a REPLAY whose recorded start command
    resumes a conversation that is not on disk - that is the incident
    where a pane died with status 127 while the row still read running.
    The preview has to apply the same guard or the picker says "would
    replay the recorded command" about something the server will decline.

    Only a DEFINITE absence refuses, so ``unchecked`` is asserted
    alongside: refusing on "I could not look" would break restart on
    every machine whose corpus lives somewhere the checker was not told
    about.
    """
    resuming = "zsh -c 'claude --resume 82aabe7b-c0be-4430-b127-bbf8aad17a57'"
    kw = dict(
        name="cloude_api",
        probe_ok=True,
        pane_dead="1",
        pane_start_command=resuming,
        stored_agent_type=None,
        stored_agent_command=None,
        offers=[WrapperOffer(agent_type="claude-chrome", command="cc")],
    )

    gone = build_restart_preview(presence_outcome="absent", **kw)
    assert gone.unchanged.kind == RESPAWN_TRANSCRIPT_MISSING
    assert gone.unchanged.actionable is False
    assert gone.projected.kind == RESPAWN_TRANSCRIPT_MISSING
    assert gone.unchanged.command is None

    # The wrapper choice is the WAY OUT of that refusal, and it must stay
    # offered: its command carries no --resume, so there is nothing
    # missing about it.
    assert gone.options[0].actionable_now is True
    assert gone.options[0].kind == RESPAWN_AGENT

    for outcome in ("present", "unchecked", None):
        fine = build_restart_preview(presence_outcome=outcome, **kw)
        assert fine.unchanged.kind == "replay", (
            f"presence {outcome!r} refused a preview; only 'absent' may"
        )


# ---------------------------------------------------------------------------
# Validation - refuse, never substitute
# ---------------------------------------------------------------------------


class _FakeWrapper:
    def __init__(self, wid):
        self.id = wid
        self.label = wid


class _FakeAgents:
    def __init__(self, wrappers):
        self.wrappers = wrappers


class _FakeAuth:
    def __init__(self, wrappers):
        self.agents = _FakeAgents(wrappers)


class _FakeSettings:
    """Only the two methods the validator is allowed to touch."""

    def __init__(self, ids, *, raise_load=False, command="resolved"):
        self._ids = ids
        self._raise_load = raise_load
        self._command = command

    def load_auth_config(self):
        if self._raise_load:
            raise OSError("config.json is unreadable")
        return _FakeAuth([_FakeWrapper(i) for i in self._ids])

    def get_agent_command(self, agent_type, model=None, extra_args=None):
        # MIRRORS THE REAL SIGNATURE, extra_args included. The restart
        # path passes the session's ['--resume', '<uuid>'] through here,
        # so a fake that refused the kwarg would pass while the real call
        # raised.
        return self._command


def test_an_unknown_wrapper_id_is_refused_and_never_substituted():
    """``get_agent_command`` would silently return the DEFAULT wrapper.

    That forgiveness is right for a launch and catastrophic for a picker:
    the user asks for claude-chrome and gets claude-skip-permissions with
    nothing on screen saying so. The validator refuses first, and names
    what does exist.
    """
    choice = validate_agent_choice(
        _FakeSettings(["claude-skip-permissions", "claude-chrome"]), "nope"
    )
    assert choice.verdict == CHOICE_UNKNOWN
    assert choice.accepted is False
    assert choice.command is None
    assert "claude-chrome" in choice.detail


def test_an_unreadable_config_cannot_determine_rather_than_unknown():
    choice = validate_agent_choice(
        _FakeSettings([], raise_load=True), "claude-chrome"
    )
    assert choice.verdict == CHOICE_CANNOT_DETERMINE
    assert choice.accepted is False
    assert "cannot be determined" in choice.detail


def test_a_configured_wrapper_is_accepted_with_its_command():
    choice = validate_agent_choice(
        _FakeSettings(["claude-chrome"], command="run-cc"), " claude-chrome "
    )
    assert choice.verdict == CHOICE_ACCEPTED
    assert choice.agent_type == "claude-chrome"
    assert choice.command == "run-cc"


def test_a_wrapper_that_resolves_to_nothing_is_not_accepted():
    choice = validate_agent_choice(
        _FakeSettings(["claude-chrome"], command="   "), "claude-chrome"
    )
    assert choice.verdict == CHOICE_CANNOT_DETERMINE
    assert choice.accepted is False


# ---------------------------------------------------------------------------
# Real tmux: the pick lands in the pane, and the row remembers it
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
    name = derive_test_socket(f"pick_{request.node.name[:20]}")
    _tmux(name, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(name, "set-option", "-wg", "remain-on-exit", "on")
    yield name
    subprocess.run(
        ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
    )


def _patch_settings(monkeypatch, sm, *, wrappers):
    """Point the manager's settings at a fake wrapper list.

    Description: ``Settings`` is a pydantic model, so an instance
      attribute cannot be set on it - the patch has to go on the CLASS,
      and the replacement therefore takes ``self``. Getting this wrong
      raises "object has no field", which reads like a missing method
      rather than a test-harness problem.
    Inputs: monkeypatch (pytest fixture). sm (the session_manager module).
      wrappers (list[str]) - ids to offer.
    Output: None.
    """
    monkeypatch.setattr(
        type(sm.settings),
        "load_auth_config",
        lambda self: _FakeAuth([_FakeWrapper(w) for w in wrappers]),
    )


def _patch_command(monkeypatch, sm, command):
    """Make every agent_type resolve to one known command string.

    Inputs: monkeypatch (pytest fixture). sm (the session_manager
      module). command (str) - what get_agent_command returns.
    Output: None.
    """
    monkeypatch.setattr(
        type(sm.settings),
        "get_agent_command",
        lambda self, agent_type, model=None, extra_args=None: command,
    )


def _manager():
    from unittest.mock import patch

    from src.core.session_manager import SessionManager

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        return SessionManager()


@pytestmark_tmux
@pytest.mark.asyncio
async def test_the_picked_wrapper_is_what_ends_up_in_the_pane(
    socket, tmp_path, monkeypatch
):
    """A pane born as a BARE SHELL, restarted onto a picked wrapper.

    This is the owner's case end to end and the reason the gate had to
    give way: without the override this session's restart returns a login
    shell, so the marker below would never be written. The assertion is
    on the pane's own ``#{pane_start_command}`` after the fact, not on
    the return value, because the return value is what a shell respawn
    would also happily report.
    """
    from src.core import session_manager as sm

    name = "picked"
    marker = tmp_path / "ran.txt"
    # A pane with NO start command - the shell rung, exactly the state
    # HANDOFF section 5 describes as silently handing back zsh.
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24",
    )
    _tmux(socket, "send-keys", "-t", name, "exit", "Enter")
    assert _wait_for(socket, name, "1"), "setup: the pane did not exit"

    baseline = _tmux(
        socket, "list-panes", "-t", name, "-F", "#{pane_start_command}"
    ).stdout.strip()
    assert baseline == "", (
        f"setup: this pane was supposed to have no start command, got {baseline!r}"
    )

    chosen_command = f"sh -c \"echo picked > {marker}; sleep 30\""
    _patch_settings(monkeypatch, sm, wrappers=["claude-chrome"])
    _patch_command(monkeypatch, sm, chosen_command)

    mgr = _manager()
    result = await mgr.respawn_session(
        name, socket_name=socket, agent_type="claude-chrome"
    )

    assert result["ok"] is True, result["detail"]
    assert result["kind"] == RESPAWN_AGENT
    assert result["chosen"] is True
    assert result["agent_type"] == "claude-chrome"
    assert _wait_for(socket, name, "0"), "the pane did not come back to life"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.exists(), (
        "the picked wrapper never ran; the pane came back as a plain shell, "
        "which is the exact defect this feature exists to remove"
    )


@pytestmark_tmux
@pytest.mark.asyncio
async def test_an_unknown_wrapper_id_refuses_and_spawns_nothing(socket, tmp_path):
    """A 400-shaped refusal, and the corpse is left exactly as found."""
    from src.core import session_manager as sm

    name = "refused"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 0.2; exit 0"',
    )
    assert _wait_for(socket, name, "1")

    mgr = _manager()
    with pytest.raises(ValueError) as excinfo:
        await mgr.respawn_session(
            name, socket_name=socket, agent_type="not-a-real-wrapper"
        )
    assert "not-a-real-wrapper" in str(excinfo.value)
    assert _pane_dead(socket, name) == "1", "a refused restart spawned something"
    assert sm is not None


@pytestmark_tmux
@pytest.mark.asyncio
async def test_the_choice_is_remembered_and_identity_is_unchanged(
    socket, tmp_path, monkeypatch
):
    """Persist the pick, and prove it is still the SAME session row.

    Two assertions that have to hold together. ``sessions.agent_type``
    must change - that is the hand-edit this feature replaces - and
    ``session_uuid`` must NOT, because a respawn preserves the instance
    triple and is therefore not allowed to mint a row. One without the
    other is a bug: a new row would carry the choice and lose the
    session's whole history.
    """
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    with closing(migrated_connection(db_dir)):
        pass

    name = "remember"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 0.2; exit 0"',
    )
    epoch = int(
        _tmux(
            socket, "display-message", "-p", "-t", name, "#{session_created}"
        ).stdout.strip()
    )
    with closing(connect(db_path_for(db_dir))) as conn:
        record_instance(
            conn,
            socket=socket,
            name=name,
            epoch=epoch,
            origin=SESSION_ORIGIN_OBSERVED,
        )
        conn.commit()
        before = conn.execute(
            "SELECT session_uuid, agent_type FROM sessions"
        ).fetchall()
    assert len(before) == 1, "setup: expected exactly one row"
    uuid_before = before[0]["session_uuid"]

    assert _wait_for(socket, name, "1")

    monkeypatch.setattr(
        type(sm.settings), "get_state_dir", lambda self: db_dir
    )
    _patch_settings(monkeypatch, sm, wrappers=["claude-chrome"])
    _patch_command(monkeypatch, sm, 'sh -c "sleep 30"')

    mgr = _manager()
    result = await mgr.respawn_session(
        name, socket_name=socket, agent_type="claude-chrome"
    )
    assert result["ok"] is True, result["detail"]
    assert result["agent_type_persisted"] is True
    assert result["session_uuid"] == uuid_before, (
        "the restart reported a different session; identity moved"
    )

    with closing(connect(db_path_for(db_dir))) as conn:
        after = conn.execute(
            "SELECT session_uuid, agent_type, parent_session_id, fork_kind "
            "FROM sessions"
        ).fetchall()
    assert len(after) == 1, "the restart minted a second row; that is a fork"
    assert after[0]["session_uuid"] == uuid_before
    assert after[0]["agent_type"] == "claude-chrome"
    assert after[0]["parent_session_id"] is None
    assert after[0]["fork_kind"] is None


@pytestmark_tmux
@pytest.mark.asyncio
async def test_a_failed_restart_does_not_record_the_choice(
    socket, tmp_path, monkeypatch
):
    """A wrapper that dies on arrival is not what the session is running.

    Recording it would make the NEXT restart re-derive a command already
    observed to fail, with nothing on screen saying so.
    """
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    with closing(migrated_connection(db_dir)):
        pass

    name = "diedonarrival"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 0.2; exit 0"',
    )
    epoch = int(
        _tmux(
            socket, "display-message", "-p", "-t", name, "#{session_created}"
        ).stdout.strip()
    )
    with closing(connect(db_path_for(db_dir))) as conn:
        record_instance(
            conn,
            socket=socket,
            name=name,
            epoch=epoch,
            origin=SESSION_ORIGIN_OBSERVED,
        )
        conn.commit()
    assert _wait_for(socket, name, "1")

    monkeypatch.setattr(
        type(sm.settings), "get_state_dir", lambda self: db_dir
    )
    _patch_settings(monkeypatch, sm, wrappers=["claude-chrome"])
    _patch_command(monkeypatch, sm, 'sh -c "echo nope 1>&2; exit 3"')

    mgr = _manager()
    result = await mgr.respawn_session(
        name, socket_name=socket, agent_type="claude-chrome"
    )
    assert result["ok"] is False
    assert result["agent_type_persisted"] is False
    with closing(connect(db_path_for(db_dir))) as conn:
        row = conn.execute("SELECT agent_type FROM sessions").fetchone()
    assert row["agent_type"] is None, "a restart that failed recorded the choice"


@pytestmark_tmux
@pytest.mark.asyncio
async def test_restart_preview_reads_the_pane_and_spawns_nothing(
    socket, tmp_path, monkeypatch
):
    """The read-only route's engine, against a real dead pane.

    The decisive assertion is the one AFTER the call: the pane is still
    dead. A preview that spawned anything would be indistinguishable from
    the action it exists to be safer than.
    """
    from src.core import session_manager as sm

    name = "previewed"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24",
    )
    _tmux(socket, "send-keys", "-t", name, "exit", "Enter")
    assert _wait_for(socket, name, "1")

    _patch_settings(monkeypatch, sm, wrappers=["claude-chrome"])
    _patch_command(monkeypatch, sm, "run-cc")

    mgr = _manager()
    preview = await mgr.restart_preview(name, socket_name=socket)

    assert preview.name == name
    assert preview.unchanged.kind == RESPAWN_SHELL
    assert preview.wrappers_status == WRAPPERS_OK
    assert [o.kind for o in preview.options] == [RESPAWN_AGENT]
    assert _pane_dead(socket, name) == "1", "the preview spawned something"
