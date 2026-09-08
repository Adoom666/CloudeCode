"""Predicting what a LIVE session would come back as, without touching it.

WHY THIS FILE EXISTS. The respawn ladder answers "what should we run
NOW", and for a live pane it correctly stops at ``RESPAWN_NOT_DEAD``
before it ever reads ``#{pane_start_command}``. That is the safe answer
and the wrong one for a picker: it can only say a running session is
running, never what that session would come back AS.

On this machine that is the entire interesting population. 18 sessions
are live, most idle for days, and every session created via
``auto_start_claude:false`` carries a NULL ``sessions.agent_type``. Those
are precisely the ones an empty ``#{pane_start_command}`` would return as
a bare login shell. A restart control that cannot warn them is the
control that loses somebody's work.

TWO PROPERTIES ARE UNDER TEST, and keeping them apart is the point.

1. THE PREDICTION IS USEFUL. ``project_restart_rung`` reports the rung a
   restart would land on with the liveness gate lifted, so a live session
   gets 'shell' / 'replay' / 'agent' instead of a uniform 'not_dead'.
2. THE PREDICTION IS NEVER A PERMISSION. Every ACTING path still answers
   'not_dead' for a live pane, ``pane_state`` carries liveness as its own
   three-valued fact, and no projection can turn either into a yes. If
   these two ever merge, a picker starts authorising the killing of
   running agents, which is the worst outcome this feature can have.

THE TRANSCRIPT GUARD at the bottom is from a live incident: a restart
resumed a ``claude_session_uuid`` whose transcript did not exist on disk,
the pane died instantly with status 127, and the row still read
``lifecycle=running``.

This path was ASSUMED safe from that by construction, on the grounds that
it only ever renders a wrapper id to a command. That assumption was
WRONG, and measuring it is what found the hole: ``RESPAWN_REPLAY`` hands
tmux back its own ``#{pane_start_command}``, and of the 19 live sessions
on the owner's box on 2026-09-07, THREE carry an explicit
``--resume <uuid>`` in that recorded string. A replay of one of those is
a resume, and nothing had ever looked inside it.

So the guard is a real check, not a ban: the uuid is extracted onto
``RespawnPlan.resume_uuid``, the caller proves the file exists, and a
DEFINITE absence becomes ``RESPAWN_TRANSCRIPT_MISSING`` - a refusal that
carries no command. ``unchecked`` never refuses, because not having
looked is not evidence of absence.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")

from src.core.session_respawn import (  # noqa: E402
    ALL_PANE_STATES,
    PANE_ALIVE,
    PANE_DEAD,
    PANE_UNKNOWN,
    RESPAWN_AGENT,
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_NOT_DEAD,
    RESPAWN_REPLAY,
    RESPAWN_SHELL,
    RESPAWN_TRANSCRIPT_MISSING,
    pane_state_from_probe,
    project_restart_rung,
    refuse_if_transcript_missing,
    resolve_respawn_plan,
)
from src.core.session_restart_preview import (  # noqa: E402
    WrapperOffer,
    build_restart_preview,
)

SRC = ROOT / "src"


def _live(**kw):
    """A preview of a session whose pane is ALIVE, with overridable parts.

    Description: every test here is about a running session, so the live
        pane is the default and each test states only what it varies.

    Inputs:
        **kw: any ``build_restart_preview`` keyword to override.

    Output:
        RestartPreview.
    """
    args = dict(
        name="cloude_live",
        probe_ok=True,
        pane_dead="0",
        pane_start_command="",
        stored_agent_type=None,
        stored_agent_command=None,
        offers=[],
    )
    args.update(kw)
    return build_restart_preview(**args)


# ---------------------------------------------------------------------------
# 1. The prediction is useful - a live session gets a real rung
# ---------------------------------------------------------------------------


def test_a_live_session_with_an_empty_start_command_projects_a_shell():
    """THE landmine, seen BEFORE anything is destroyed.

    An empty ``#{pane_start_command}`` is the case that hands back a
    login shell. Without a projection the preview says only 'not_dead',
    so this session is indistinguishable from one that would restart
    perfectly - which is the whole reason the owner lost a session.
    """
    preview = _live(pane_start_command="")

    # The acting answer is unchanged and still refuses.
    assert preview.unchanged.kind == RESPAWN_NOT_DEAD
    assert preview.unchanged.actionable is False

    # The useful answer: it would come back a SHELL, and says so.
    assert preview.projected.kind == RESPAWN_SHELL
    assert "plain shell" in preview.projected.detail


def test_a_live_session_with_a_command_but_no_agent_type_projects_replay():
    """The landmine's second half, and the NULL agent_type population.

    A non-empty start command with no stored ``agent_type`` lands on
    REPLAY, not AGENT. Every session created via ``auto_start_claude``
    false is in this bucket, so it must be nameable in advance.
    """
    preview = _live(
        pane_start_command='"cld"',
        stored_agent_type=None,
        stored_agent_command=None,
    )
    assert preview.unchanged.kind == RESPAWN_NOT_DEAD
    assert preview.projected.kind == RESPAWN_REPLAY


def test_a_live_session_projects_agent_only_when_both_halves_are_present():
    """AGENT needs a recorded start command AND a resolvable agent_type."""
    preview = _live(
        pane_start_command='"cld"',
        stored_agent_type="claude-skip-permissions",
        stored_agent_command="cld",
    )
    assert preview.projected.kind == RESPAWN_AGENT
    assert preview.projected.command == "cld"


def test_each_wrapper_is_predicted_for_a_live_session():
    """The picker's per-option prediction, which is what it renders.

    A uniform 'not_dead' across every option tells the user nothing about
    which wrapper does what.
    """
    preview = _live(
        pane_start_command="",
        offers=[
            WrapperOffer(agent_type="claude-chrome", command="cldc"),
            WrapperOffer(agent_type="claude-skip-permissions", command="cld"),
        ],
    )
    # Acting: still refused, for every one of them.
    assert [o.kind for o in preview.options] == [RESPAWN_NOT_DEAD] * 2
    assert not any(o.actionable_now for o in preview.options)

    # Predicting: each names the agent it would actually start.
    assert [o.projected_kind for o in preview.options] == [RESPAWN_AGENT] * 2
    assert [o.command for o in preview.options] == ["cldc", "cld"]
    assert all("plain shell" in o.projected_detail for o in preview.options)


# ---------------------------------------------------------------------------
# 2. The prediction is never a permission
# ---------------------------------------------------------------------------


def test_a_projection_is_never_not_dead():
    """The projection answers a different question, so it never refuses.

    ``not_dead`` leaking into a projected rung would mean the liveness
    gate is being consulted twice and the prediction is worthless again.
    """
    for dead in ("0", "1"):
        for start in ("", '"cld"', None):
            projected = project_restart_rung(
                probe_ok=True,
                pane_start_command=start,
                agent_command="cld",
            )
            assert projected.kind != RESPAWN_NOT_DEAD
            # ...while the acting ladder still refuses a live pane.
            acting = resolve_respawn_plan(
                probe_ok=True,
                pane_dead=dead,
                pane_start_command=start,
                agent_command="cld",
            )
            if dead == "0":
                assert acting.kind == RESPAWN_NOT_DEAD


def test_picking_a_wrapper_cannot_revive_a_live_pane_through_the_preview():
    """An explicit choice supplies a missing command, never a missing corpse.

    This is the invariant that stops the picker growing into something
    that kills running agents.
    """
    preview = _live(
        pane_start_command="",
        offers=[WrapperOffer(agent_type="claude-chrome", command="cldc")],
    )
    assert preview.unchanged.kind == RESPAWN_NOT_DEAD
    assert preview.options[0].kind == RESPAWN_NOT_DEAD
    assert preview.options[0].actionable_now is False


def test_pane_state_is_its_own_three_valued_fact():
    """Liveness is reported separately, and 'unknown' is not the others."""
    assert pane_state_from_probe(True, "1") == PANE_DEAD
    assert pane_state_from_probe(True, "0") == PANE_ALIVE
    assert pane_state_from_probe(False, None) == PANE_UNKNOWN
    # A probe that answered but returned no liveness field is NOT alive.
    assert pane_state_from_probe(True, None) == PANE_UNKNOWN
    assert {PANE_DEAD, PANE_ALIVE, PANE_UNKNOWN} == set(ALL_PANE_STATES)

    assert _live(pane_dead="0").pane_state == PANE_ALIVE
    assert _live(pane_dead="1").pane_state == PANE_DEAD
    assert _live(probe_ok=False, pane_dead=None).pane_state == PANE_UNKNOWN


def test_an_unreadable_probe_projects_cannot_determine_never_a_shell():
    """The third outcome, on the projection too.

    'I could not look' must not become 'there is nothing to run', which
    would silently recommend a login shell for a pane nobody read.
    """
    preview = _live(probe_ok=False, pane_dead=None, pane_start_command=None)
    assert preview.pane_state == PANE_UNKNOWN
    assert preview.projected.kind == RESPAWN_CANNOT_DETERMINE
    assert preview.projected.actionable is False
    assert preview.unchanged.kind == RESPAWN_CANNOT_DETERMINE


def test_resolvable_and_actionable_now_are_different_facts():
    """Two reasons a row is greyed out, and they must stay distinguishable.

    A single flag made a perfectly configured wrapper on a LIVE session
    look identical to one that could not be turned into a command at all.
    """
    preview = _live(
        pane_start_command="",
        offers=[
            WrapperOffer(agent_type="good", command="cldc"),
            WrapperOffer(
                agent_type="needs-model",
                command=None,
                unavailable_reason="the 'cldl' agent needs a model",
            ),
        ],
    )
    good, broken = preview.options

    # The pane is alive, so NEITHER may be acted on...
    assert good.actionable_now is False
    assert broken.actionable_now is False
    # ...but only one of them is actually unusable, and the reason survives.
    assert good.resolvable is True
    assert broken.resolvable is False
    assert broken.projected_kind == RESPAWN_CANNOT_DETERMINE
    assert "needs a model" in broken.detail


# ---------------------------------------------------------------------------
# 3. One ladder - the projection cannot drift from the action
# ---------------------------------------------------------------------------


def test_on_a_dead_pane_the_projection_equals_the_action():
    """The two entry points differ ONLY by the liveness gate.

    Once the pane is dead that gate is satisfied, so every input must
    produce an identical verdict. A divergence here means a second copy
    of the ladder has appeared and the preview may now promise an agent
    and deliver a shell.
    """
    for start in ("", '"cld"', None, "   "):
        for agent in ("cld", None, ""):
            for chosen in (None, "cldc"):
                acting = resolve_respawn_plan(
                    probe_ok=True,
                    pane_dead="1",
                    pane_start_command=start,
                    agent_command=agent,
                    chosen_agent_command=chosen,
                    chosen_agent_type="claude-chrome" if chosen else None,
                )
                projected = project_restart_rung(
                    probe_ok=True,
                    pane_start_command=start,
                    agent_command=agent,
                    chosen_agent_command=chosen,
                    chosen_agent_type="claude-chrome" if chosen else None,
                )
                assert acting == projected, (start, agent, chosen)


def test_a_dead_pane_preview_reports_the_same_rung_twice():
    """The whole-preview form of the property above."""
    preview = _live(pane_dead="1", pane_start_command="")
    assert preview.pane_state == PANE_DEAD
    assert preview.unchanged == preview.projected
    assert preview.unchanged.kind == RESPAWN_SHELL


# ---------------------------------------------------------------------------
# 4. The transcript guard - a missing transcript cannot reach this path
# ---------------------------------------------------------------------------


#: The ONE function allowed to build a restart's ``--resume`` argv, and
#: the one allowed to EXTRACT a uuid from a command that already carries
#: one. Everything else on the restart path must go through them.
RESUME_BUILDER = "core/session_restart.py"
RESUME_EXTRACTOR = "core/session_respawn.py"

#: Modules on the restart path, scanned for a hand-rolled resume.
RESTART_PATH_MODULES = (
    "core/session_restart_preview.py",
    "core/session_agent_choice.py",
    "core/session_resume_target.py",
    "core/session_manager.py",
    "core/tmux_backend.py",
)


def test_no_module_on_the_restart_path_spells_its_own_resume():
    """One builder for ``--resume``, so one place can be audited.

    THE HISTORY. This test used to assert the opposite - that the wrapper
    chooser NEVER resumed anything - because at the time nothing on that
    path did, and a resume bolted on later would have skipped the
    transcript check. The owner then defined restart, 2026-09-07: "restart
    on recent is really just resume. restart on open is close and resume
    session so it loads a new wrapper or new claude binary." So the
    chooser DOES resume now, deliberately, and the old ban would forbid
    the correct behaviour.

    THE UNDERLYING GUARANTEE IS UNCHANGED and this is what carries it:
    every restart resume is built by
    ``session_restart.resume_arguments`` out of a uuid classified by
    ``session_resume_target``, and is checked against the filesystem by
    ``session_transcript_presence`` before anything spawns. A module that
    spells its own ``--resume`` string has stepped outside both, which is
    exactly how the incident happened - a resume against a transcript
    that did not exist, a pane dead on the first tick, and a row still
    reading ``lifecycle=running``.

    If this fails, do not delete it: route the new resume through
    ``session_resume_target.resume_extra_args``.
    """
    # PARSED, NOT GREPPED. A docstring that MENTIONS --resume (this
    # repo documents heavily, and doctests print argv lists verbatim) is
    # prose; only a string constant in executable code can become argv.
    # A line-based scan cannot tell those apart and would either miss a
    # real one or fail on the documentation.
    offenders = []
    for rel in RESTART_PATH_MODULES:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                body = getattr(node, "body", None)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "--resume" in node.value
            ):
                offenders.append(f"src/{rel}:{node.lineno}")

    assert not offenders, (
        "a module on the restart path builds its own --resume:\n  "
        + "\n  ".join(offenders)
        + f"\n\nBuild it in src/{RESUME_BUILDER} via resume_arguments and "
        f"read it back in src/{RESUME_EXTRACTOR} via resume_uuid_in, so "
        "src/core/session_transcript_presence.py can prove the file is "
        "there first. Resuming a transcript that is not there kills the "
        "pane while the row still reads lifecycle=running."
    )


def test_a_replay_that_resumes_is_refused_when_the_transcript_is_gone():
    """The ladder DOES see resumes now, so a ban was replaced by a guard.

    ``RESPAWN_REPLAY`` hands tmux back its own ``#{pane_start_command}``,
    and measured on the owner's box 2026-09-07, 3 of 19 live sessions
    carry an explicit ``--resume <uuid>`` in theirs. So a replay CAN
    re-run a resume, and a blanket "this module must not mention
    --resume" would have left that unguarded while reading green.

    What is pinned instead is the behaviour: the uuid is extracted, and a
    DEFINITE absence becomes a named refusal that carries no command.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command=(
            "zsh -c 'claude --resume 82aabe7b-c0be-4430-b127-bbf8aad17a57'"
        ),
        agent_command=None,
    )
    assert plan.kind == RESPAWN_REPLAY
    assert plan.resume_uuid == "82aabe7b-c0be-4430-b127-bbf8aad17a57", (
        "the replay rung no longer notices the conversation it would resume"
    )

    refused = refuse_if_transcript_missing(plan, "absent")
    assert refused.kind == RESPAWN_TRANSCRIPT_MISSING
    assert refused.actionable is False
    assert refused.command is None, "a refusal handed back a command to run"
    assert refused.resume_uuid == plan.resume_uuid
    assert refused.detail.strip()


def test_only_a_definite_absence_refuses_a_replay():
    """UNCHECKED is not ABSENT, and neither is PRESENT.

    Refusing on "I could not look" would break restart on every machine
    whose corpus lives somewhere the checker was not told about, which is
    a far larger blast radius than the incident it guards.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command=(
            "zsh -c 'claude --resume 82aabe7b-c0be-4430-b127-bbf8aad17a57'"
        ),
        agent_command=None,
    )
    for outcome in ("present", "unchecked", None):
        passed = refuse_if_transcript_missing(plan, outcome)
        assert passed is plan, (
            f"presence {outcome!r} refused a restart; only 'absent' may"
        )
        assert passed.actionable is True


def test_a_command_with_no_resume_is_never_refused_for_a_transcript():
    """No uuid, nothing to be missing. A guard that fires here is a bug."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="1",
        pane_start_command='"cld"',
        agent_command="cld",
    )
    assert plan.resume_uuid is None
    assert refuse_if_transcript_missing(plan, "absent") is plan
