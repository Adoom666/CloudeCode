"""Policy tests for pushing a CloudeCode rename into Claude Code.

Every function under test is pure, so the whole policy is exercised with
no tmux, no terminal and no Claude process. What that buys: the decision
that types text into a user's live pane is decided by code that can be
tested exhaustively, rather than by a condition discovered at runtime.
"""

import pytest

from src.core.claude_rename import (
    MAX_NAME_CHARS,
    MIN_RENAME_VERSION,
    PUSH_DEFERRED,
    PUSH_SENT,
    PUSH_UNSUPPORTED,
    decide_push,
    oob_rename_argv,
    launch_name_args,
    name_is_pushable,
    parse_claude_version,
    rename_command,
    supports_rename,
)

VER = (2, 1, 248)


# ---- version parsing -------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("2.1.248 (Claude Code)", (2, 1, 248)),
        ("2.1.248", (2, 1, 248)),
        ("  2.1.205 (Claude Code)\n", (2, 1, 205)),
        ("not a version", None),
        ("2.1", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_claude_version(text, expected):
    assert parse_claude_version(text) == expected


def test_unknown_version_is_treated_as_unsupported():
    """The safe direction, and it must be asserted rather than assumed.

    Not sending costs a cosmetic mismatch. Sending `/rename Foo` to a
    build with no such command types a literal line into the user's
    conversation, which they then have to notice and delete.
    """
    assert supports_rename(None) is False
    assert supports_rename((2, 1, MIN_RENAME_VERSION[2] - 1)) is False
    assert supports_rename(MIN_RENAME_VERSION) is True


# ---- what may be sent at all -----------------------------------------

def test_a_newline_is_never_pushable():
    """send-keys would SUBMIT at the newline, splitting the label in two.

    Half would arrive as a /rename and the remainder as a prompt to
    Claude - a defect that writes into the user's conversation.
    """
    assert name_is_pushable("first\nsecond") is False
    assert name_is_pushable("carriage\rreturn") is False


def test_names_claude_would_rewrite_are_refused():
    assert name_is_pushable("x" * MAX_NAME_CHARS) is True
    assert name_is_pushable("x" * (MAX_NAME_CHARS + 1)) is False
    assert name_is_pushable("") is False
    assert name_is_pushable("   ") is False
    assert name_is_pushable("bell\x07here") is False


def test_ordinary_names_including_spaces_and_emoji_are_pushable():
    assert name_is_pushable("Refactor spike (round 2)") is True
    assert name_is_pushable("Refactor spike 🚀") is True


# ---- the out-of-band decision ---------------------------------------

def _d(**kw):
    """decide_push with the working defaults filled in."""
    base = dict(
        label="X", claude_uuid="u1", claude_version=VER, is_claude_session=True
    )
    base.update(kw)
    return decide_push(**base)


def test_a_bound_conversation_gets_the_rename():
    outcome, reason = _d()
    assert outcome == PUSH_SENT
    assert "out of band" in reason


def test_no_activity_gate_exists_any_more():
    """THE POINT OF THE REWRITE.

    The previous design typed into the pane and had to refuse whenever
    Claude was busy or waiting on an answer - text typed at a question
    BECOMES the answer. Out-of-band touches no pane, so there is no
    session state in which the rename must be skipped. If someone
    reintroduces an activity argument here, this test says why not to.
    """
    import inspect

    params = set(inspect.signature(decide_push).parameters)
    assert "activity_status" not in params
    assert "hooks_seen" not in params
    assert params == {"label", "claude_uuid", "claude_version", "is_claude_session"}


def test_an_unbound_conversation_defers_because_resume_needs_a_uuid():
    """The one genuine deferral left.

    --resume addresses a Claude session BY UUID. A session that has not
    bound one yet cannot be addressed at all, so this is 'not yet',
    never 'failed' - the label is already stored on our side.
    """
    outcome, reason = _d(claude_uuid=None)
    assert outcome == PUSH_DEFERRED
    assert "uuid" in reason


def test_non_claude_and_old_claude_are_unsupported_not_deferred():
    """Deferred means try later; unsupported means never."""
    outcome, _ = _d(is_claude_session=False)
    assert outcome == PUSH_UNSUPPORTED
    outcome, reason = _d(claude_version=(2, 1, 100))
    assert outcome == PUSH_UNSUPPORTED
    assert "2.1.205" in reason


def test_an_unpushable_name_defers_rather_than_sending_a_different_one():
    outcome, reason = _d(label="bad\nname")
    assert outcome == PUSH_DEFERRED
    assert "disagree" in reason


def test_the_argv_is_a_list_so_no_shell_ever_parses_the_label():
    """A label is user text and may contain quotes, backticks or $(...).

    Passing argv as a list removes the injection class outright rather
    than trying to escape it. This repo has already been bitten once by a
    backtick inside a quoted string being executed as a command.
    """
    argv = oob_rename_argv("/usr/bin/claude", "uuid-1", 'evil"; $(rm -rf /) #')
    assert isinstance(argv, list)
    assert argv[:4] == ["/usr/bin/claude", "-p", "--resume", "uuid-1"]
    # The label travels as ONE argv element, unsplit and unparsed.
    assert argv[4].startswith("/rename ")
    assert argv[4].endswith('evil"; $(rm -rf /) #')
    assert len(argv) == 5


def test_rename_command_is_the_literal_line():
    assert rename_command("My Session") == "/rename My Session"


# ---- the launch flag -------------------------------------------------

def test_launch_name_args_only_for_the_claude_family():
    """A per-FAMILY capability read off the family, not the wrapper.

    This is the cldl defect's exact shape: passing --name to codex or
    shell hands an unknown flag to a program that never had it, which
    breaks the launch rather than degrading it.
    """
    assert launch_name_args(label="S", family="claude", claude_version=VER) == ["--name", "S"]
    for family in ("codex", "hermes", "openclaw", "shell", "local", None):
        assert launch_name_args(label="S", family=family, claude_version=VER) == []


def test_launch_name_args_degrade_to_empty_never_raise():
    """Empty leaves the command line exactly as it was.

    The only safe failure mode for something that edits a command line is
    to change nothing.
    """
    assert launch_name_args(label=None, family="claude", claude_version=VER) == []
    assert launch_name_args(label="S", family="claude", claude_version=None) == []
    assert launch_name_args(label="S", family="claude", claude_version=(2, 1, 1)) == []
    assert launch_name_args(label="bad\nname", family="claude", claude_version=VER) == []


# ---- the launch-time wiring -----------------------------------------

def test_launch_name_args_for_resolves_the_family_that_will_run():
    """The wiring, not just the policy.

    `_launch_name_args_for` is where the family question is actually
    asked at launch. Pinning only `launch_name_args` would leave the
    call site free to pass the wrong family and still pass every test.
    """
    from src.core.claude_rename import launch_name_args_for_agent_type as _launch_name_args_for

    assert _launch_name_args_for(label=None, agent_type="claude") == []

    got = _launch_name_args_for(label="Spike", agent_type="claude")
    assert got in ([], ["--name", "Spike"]), (
        "either the local claude supports --name and we pass it, or it "
        "does not and we pass nothing - never anything else"
    )

    # The discriminating half: a non-claude family must NEVER get the flag,
    # regardless of what the local claude version happens to be.
    for family in ("codex", "hermes", "openclaw", "shell"):
        assert _launch_name_args_for(label="Spike", agent_type=family) == []


def test_an_unknown_agent_type_still_gets_the_flag_and_that_is_correct():
    """Follows `get_family`'s launch-time contract, not intuition.

    `get_family` never answers "I don't know" - a launch has to run
    something, so an unresolvable name falls back to the claude family.
    The command that then gets built IS a claude command, so `--name` is
    valid for it. Asserting `[]` here (the intuitive guess, and what this
    test originally claimed) would pin behaviour that contradicts the
    resolver the launch actually uses.

    What must hold is the pairing: whatever family the launch resolves
    to is the family whose flag support decides this.
    """
    from src.core.agent_families import get_family
    from src.core.claude_rename import launch_name_args_for_agent_type as _l

    for junk in ("no-such-agent-type", None):
        assert get_family(junk).name == "claude"
        assert _l(label="S", agent_type=junk) in ([], ["--name", "S"])

    # A REAL non-claude family is the case that must always be empty.
    assert get_family("shell").name == "shell"
    assert _l(label="S", agent_type="shell") == []
