"""The respawn command-resolution ladder (src/core/session_respawn.py).

These are the pure-classification half. The empirical half - whether
``tmux respawn-pane`` actually revives a ``remain-on-exit`` corpse and
whether the session's identity survives it - is in
``tests/test_tmux_respawn_real.py``, which drives a REAL tmux binary on
its own throwaway socket. A mock cannot answer that question, so it is
deliberately not asked here.

The case this file exists to pin is the trap: ``sessions.agent_type`` is
written on every create regardless of ``auto_start_claude``, so a bare
console carries ``agent_type='claude'``. The ladder must NOT launch an
agent into it.
"""

from __future__ import annotations

import pytest

from src.core.session_respawn import (
    ACTIONABLE_RESPAWN_KINDS,
    ALL_RESPAWN_KINDS,
    RESPAWN_AGENT,
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_NOT_DEAD,
    RESPAWN_REPLAY,
    RESPAWN_SHELL,
    RespawnPlan,
    resolve_respawn_plan,
)


def test_dead_pane_with_agent_record_re_derives_the_agent_command():
    """The user's own case: exited to update Claude, restart the agent."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        agent_command="zsh -c 'source ~/.zshrc; cld'",
    )
    assert plan.kind == RESPAWN_AGENT
    assert plan.command == "zsh -c 'source ~/.zshrc; cld'"
    assert plan.actionable is True


def test_re_derived_command_wins_over_the_stale_recorded_one():
    """An updated wrapper must be picked up, not byte-replayed.

    This is the whole reason tier 1 re-derives instead of replaying: the
    pane recorded the OLD invocation, and restarting after an update has
    to run the NEW one.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"/old/path/claude --flag"',
        agent_command="/new/path/claude --flag",
    )
    assert plan.command == "/new/path/claude --flag"
    assert "/old/path" not in (plan.command or "")


def test_dead_pane_without_agent_record_replays_tmux_own_command():
    """An adopted session: tmux knows, we do not. Let tmux reuse its record."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"codex"',
        agent_command=None,
    )
    assert plan.kind == RESPAWN_REPLAY
    # None means "pass no command argument"; it is NOT a refusal.
    assert plan.command is None
    assert plan.actionable is True


@pytest.mark.parametrize("agent_command", ["cld", "claude --dangerously-skip-permissions"])
def test_bare_shell_pane_never_launches_an_agent(agent_command):
    """THE TRAP. agent_type is set on every create, autostart or not.

    An empty ``pane_start_command`` from a probe that WORKED is positive
    evidence the pane was born a bare console. Trusting agent_type here
    would start an agent in a shell the user opened deliberately.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command="",
        agent_command=agent_command,
    )
    assert plan.kind == RESPAWN_SHELL
    assert plan.command is None
    assert plan.actionable is True


def test_failed_probe_is_cannot_determine_not_a_guess():
    """Third outcome. No evidence is not the same as no command."""
    plan = resolve_respawn_plan(
        probe_ok=False,
        pane_dead=None,
        pane_start_command=None,
        agent_command="cld",
    )
    assert plan.kind == RESPAWN_CANNOT_DETERMINE
    assert plan.command is None
    assert plan.actionable is False
    assert "cannot be determined" in plan.detail


def test_missing_start_command_field_is_cannot_determine():
    """A probe that answered but omitted the field is still no evidence.

    Distinct from the empty-string case above: empty means tmux said
    "none", None means tmux said nothing.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command=None,
        agent_command="cld",
    )
    assert plan.kind == RESPAWN_CANNOT_DETERMINE
    assert plan.actionable is False


def test_live_pane_is_not_dead_and_is_not_actionable():
    """Restarting a running agent is not something this path may do."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="0",
        pane_start_command='"cld"',
        agent_command="cld",
    )
    assert plan.kind == RESPAWN_NOT_DEAD
    assert plan.actionable is False
    assert plan.command is None


def test_every_kind_carries_a_sentence_the_ui_can_show():
    """A blank cell is not a report. Every verdict explains itself."""
    cases = [
        dict(probe_ok=True, pane_dead="1", pane_start_command='"cld"', agent_command="cld"),
        dict(probe_ok=True, pane_dead="1", pane_start_command='"x"', agent_command=None),
        dict(probe_ok=True, pane_dead="1", pane_start_command="", agent_command=None),
        dict(probe_ok=True, pane_dead="0", pane_start_command="", agent_command=None),
        dict(probe_ok=False, pane_dead=None, pane_start_command=None, agent_command=None),
    ]
    seen = set()
    for kw in cases:
        plan = resolve_respawn_plan(**kw)
        seen.add(plan.kind)
        assert plan.detail.strip(), f"{plan.kind} has no detail sentence"
    assert seen == ALL_RESPAWN_KINDS


def test_non_actionable_kinds_never_carry_a_command():
    """A refusal that still hands back a command invites a caller to run it."""
    for kind in ALL_RESPAWN_KINDS - ACTIONABLE_RESPAWN_KINDS:
        assert kind not in ACTIONABLE_RESPAWN_KINDS
    assert RespawnPlan(kind=RESPAWN_CANNOT_DETERMINE).command is None
    assert RespawnPlan(kind=RESPAWN_NOT_DEAD).command is None


def test_whitespace_only_agent_command_falls_through_to_replay():
    """A config that resolves to blank is no record at all."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        agent_command="   ",
    )
    assert plan.kind == RESPAWN_REPLAY
