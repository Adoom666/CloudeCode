"""Which families need the user's rc sourced, and why the table was wrong.

THE DEFECT. ``AgentFamily.sources_zshrc`` was True for ``claude`` alone.
Every other family returned its configured static command RAW, straight
into ``tmux new-session ... <cmd>``. The tmux pane shell is
non-interactive and non-login, so it never reads ``~/.zshrc`` - which is
exactly the reasoning ``Settings.get_agent_command`` already spells out
at length for the claude family, and exactly why ``rc_prefixed`` exists.
A ``codex`` installed by nvm, asdf, mise, or any other version manager
that puts its shims on PATH from an rc file is therefore not on PATH, and
the pane dies with "command not found".

WHAT MAKES IT WORSE THAN A PLAIN OVERSIGHT, and the thing that argues the
table is wrong rather than the callers: the LAST-RESORT path already gets
this right. ``make_tool_last_resort`` wraps its script in ``rc_prefixed``
unconditionally. So the behaviour was inverted -

    codex_command = ""       -> last resort  -> rc-sourced   -> WORKS
    codex_command = "codex"  -> static path  -> raw          -> BROKEN

which means configuring NOTHING worked and leaving the SHIPPED DEFAULT in
place did not. ``AgentsConfig.codex_command`` defaults to ``"codex"``, so
the broken path is the one every install lands on.

``shell`` is deliberately NOT flipped. ``shell_command`` is ``$SHELL -i``,
an INTERACTIVE shell, which sources the user's rc itself by definition.
Wrapping it would source the rc twice and hard-code zsh as the launcher
for a family whose whole point is honouring ``$SHELL``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
import pytest

from src.config import AgentsConfig
from src.core.agent_families import (
    AGENT_FAMILIES,
    get_family,
    render_static_command,
)
from src.core.shell_init import RC_SOURCE

#: Families that launch a THIRD-PARTY CLI the user installed themselves.
#: Every one of these can come from a version manager, so every one needs
#: the rc sourced before the binary is looked up.
TOOL_FAMILIES = ("claude", "codex", "hermes", "openclaw", "local")


@pytest.mark.parametrize("name", TOOL_FAMILIES)
def test_a_tool_family_sources_the_users_rc(name):
    """The table column, asserted directly.

    Stated against the table rather than against a rendered string so the
    reason is visible at the point a future family is added: if it runs a
    binary the user installed, it owes an rc source.
    """
    assert get_family(name).sources_zshrc is True, (
        f"{name} launches a user-installed CLI but does not source ~/.zshrc; "
        "a version-manager install will not be on PATH in a tmux pane"
    )


def test_the_shell_family_does_not_source_the_rc():
    """``$SHELL -i`` sources the rc itself. Wrapping it is wrong twice.

    It would source the rc a second time, and it would hard-code zsh as
    the launcher for the one family whose entire purpose is running
    whatever ``$SHELL`` the user actually has.
    """
    assert get_family("shell").sources_zshrc is False


@pytest.mark.parametrize("name", TOOL_FAMILIES)
def test_the_shipped_default_command_is_rendered_rc_sourced(name):
    """THE REGRESSION, MEASURED THROUGH THE REAL DEFAULT.

    Renders each family's ACTUAL shipped default from AgentsConfig rather
    than a literal, so this fails if someone changes a default to a bare
    binary name again.
    """
    family = get_family(name)
    default = getattr(AgentsConfig(), family.command_field)
    rendered = render_static_command(family, default)
    assert RC_SOURCE in rendered, (
        f"{name} renders its default {default!r} as {rendered!r}, "
        "with no rc source; a tmux pane will not find a version-managed binary"
    )
    assert rendered.startswith("zsh -c ")


def test_the_static_path_and_the_last_resort_path_agree(name="codex"):
    """The inversion this file exists to close, asserted as an invariant.

    An empty command falls to ``last_resort``, which has always been
    rc-sourced. A configured command takes the static path. Those two must
    not disagree about whether the rc is needed - that disagreement WAS
    the bug, and it pointed the wrong way: the fallback worked and the
    shipped default did not.
    """
    family = get_family(name)
    from_last_resort = render_static_command(family, "")
    from_static = render_static_command(family, "codex")
    assert (RC_SOURCE in from_last_resort) == (RC_SOURCE in from_static)


def test_every_family_declares_the_column_deliberately():
    """No family may be added without an explicit rc decision.

    A bool cannot be omitted from a frozen dataclass without a default,
    and this one has none - but assert the whole table is covered anyway,
    so a new family shows up here rather than silently inheriting whatever
    the author typed first.
    """
    covered = set(TOOL_FAMILIES) | {"shell"}
    assert {f.name for f in AGENT_FAMILIES} == covered, (
        "a family was added or removed; decide its sources_zshrc value "
        "and record the reason in this test"
    )
