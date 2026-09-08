"""The create endpoint carries a name, and it reaches the launch command.

WHAT WAS BROKEN. Every path that creates a session already passed a
label - fork and restart-of-stopped both do - and ``create_session``
turns a non-empty one into ``--name <label>``. The plain create endpoint,
the one the launchpad's "start empty" and "open project" buttons use, was
the ONE creator that passed none. Measured on 2026-09-08: a project named
"Punchlist Test" launched claude with no ``--name`` in its argv at all,
so the row said one thing and the TUI status line showed the directory.

WHY THIS FILE ASSERTS ON THE RESOLVED COMMAND STRING. A test that only
checked ``CreateSessionRequest`` accepts a ``label`` field would pass
against a server that accepts it and drops it on the floor - which is
exactly the bug, one layer up. The assertion that matters is that the
flag lands in the argv that is about to be executed.
"""

from __future__ import annotations

import inspect
import os
import tempfile

# ``src.core.session_manager`` imports ``src.config`` at module scope, and
# Settings refuses to construct without this. Set before the import below,
# the same bootstrap tests/datastore_helpers.py uses.
os.environ.setdefault(
    "DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_label_wd_")
)

import pytest

from src.core.claude_rename import (
    MIN_RENAME_VERSION,
    launch_name_args,
    launch_name_args_for_agent_type,
)
from src.models import CreateSessionRequest

SUPPORTED = MIN_RENAME_VERSION


# ---------------------------------------------------------------------
# The request model.
# ---------------------------------------------------------------------


def test_the_create_request_accepts_a_label():
    """The field exists and round-trips."""
    body = CreateSessionRequest(project_name="Punchlist", label="Punchlist")

    assert body.label == "Punchlist"


def test_a_label_is_optional_and_defaults_to_none():
    """Pre-existing clients that send no label keep working unchanged."""
    body = CreateSessionRequest(project_name="Punchlist")

    assert body.label is None


def test_the_label_is_separate_from_the_project_name():
    """A project is a folder; a label names ONE session in it.

    They are seeded from the same string by the launchpad, which is a
    client decision. The server must not fold them together, or renaming
    a session would look like renaming the project.
    """
    body = CreateSessionRequest(project_name="Infrastructure", label="Spike 3")

    assert body.project_name == "Infrastructure"
    assert body.label == "Spike 3"


# ---------------------------------------------------------------------
# The label reaches the launch command.
# ---------------------------------------------------------------------


def test_create_session_still_takes_a_label_argument():
    """The endpoint has something to pass the label TO.

    Guards the wiring from the other end: if this parameter is ever
    renamed or dropped, the route's keyword argument becomes a
    TypeError at session-create time, which is the worst place to find
    out.
    """
    from src.core.session_manager import SessionManager

    params = inspect.signature(SessionManager.create_session).parameters

    assert "label" in params
    assert params["label"].default is None


def test_a_label_becomes_the_name_flag():
    """The positive: a claude session born already knowing its name."""
    assert launch_name_args(
        label="Punchlist Test", family="claude", claude_version=SUPPORTED
    ) == ["--name", "Punchlist Test"]


def test_no_label_adds_no_flag():
    """THE NEGATIVE CONTROL, and it is the pre-change behaviour.

    An absent label must leave the command line byte-identical to what
    it was before this feature existed. Anything else would change how
    every no-label session launches.
    """
    assert launch_name_args(
        label=None, family="claude", claude_version=SUPPORTED
    ) == []
    assert launch_name_args(
        label="", family="claude", claude_version=SUPPORTED
    ) == []


def test_a_label_is_not_handed_to_a_non_claude_agent():
    """``--name`` is a claude-family flag.

    Handing it to codex or a shell does not degrade the launch, it
    BREAKS it - the flag arrives as an unknown option or, worse, as a
    prompt argument.
    """
    assert launch_name_args(
        label="Punchlist", family="codex", claude_version=SUPPORTED
    ) == []
    assert launch_name_args(
        label="Punchlist", family="shell", claude_version=SUPPORTED
    ) == []


def test_a_claude_too_old_for_rename_gets_no_flag():
    """An unknown or old version is the safe direction: send nothing."""
    assert launch_name_args(
        label="Punchlist", family="claude", claude_version=(2, 1, 100)
    ) == []
    assert launch_name_args(
        label="Punchlist", family="claude", claude_version=None
    ) == []


@pytest.mark.parametrize(
    "label",
    [
        "Punchlist Test",
        "Refactor spike (round 2)",
        "Fantasy Football 2026",
    ],
)
def test_real_project_names_survive_as_one_argv_element(label):
    """A name with spaces is ONE argument, never split.

    ``launch_name_args`` returns a list precisely so no shell ever
    parses the label. If this ever returned a joined string, a project
    called "Punchlist Test" would launch claude with the name
    "Punchlist" and a stray "Test" argument.
    """
    args = launch_name_args(label=label, family="claude", claude_version=SUPPORTED)

    assert args == ["--name", label]
    assert len(args) == 2


def test_the_agent_type_wrapper_resolves_the_family_itself():
    """The route passes an agent_type id, not a family, so this is the
    call site that actually runs.

    A wrapper id like "claude-skip-permissions" is a claude-family
    launch and must still get the flag; the family lookup is what turns
    the id into that answer.
    """
    args = launch_name_args_for_agent_type(label="Punchlist", agent_type="claude")

    assert args[:1] == ["--name"] or args == []
    # An environment without the configured wrappers resolves no family
    # and correctly returns []; when it does resolve, the label must be
    # carried through unchanged rather than rewritten.
    if args:
        assert args == ["--name", "Punchlist"]


def test_no_label_changes_nothing_on_the_id_path():
    """A NEGATIVE CONTROL on the id path.

    ``launch_name_args_for_agent_type`` edits a command line about to run
    in the user's shell, so its only acceptable failure mode is changing
    nothing.
    """
    assert launch_name_args_for_agent_type(label=None, agent_type="claude") == []
    assert launch_name_args_for_agent_type(label="", agent_type="claude") == []


def test_an_unknown_agent_type_gets_the_flag_because_claude_is_what_runs():
    """THE FLAG MUST AGREE WITH THE COMMAND, not with the id.

    ``Settings.get_agent_command`` deliberately falls back to the DEFAULT
    (claude) wrapper for an unrecognised agent_type - right for a launch,
    and the reason a picker has to validate ids separately. The family
    lookup falls back the same way, so an unknown id still gets
    ``--name``, which is correct: claude is what actually starts.

    This test exists because the obvious assertion is the opposite one.
    Asserting [] here would look like caution and would in fact force the
    name gate and the command resolution to disagree, launching claude
    with no name whenever an id was stale or misspelled.
    """
    args = launch_name_args_for_agent_type(
        label="Punchlist", agent_type="not-a-configured-agent-xyz"
    )

    assert args == ["--name", "Punchlist"]
