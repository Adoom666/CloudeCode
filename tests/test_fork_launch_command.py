"""The launch string a FORK runs, through both resolution paths.

A GUI fork spawns a real tmux session that resumes an existing Claude
conversation and branches it: ``--resume <uuid> --fork-session``. Those
arguments have to reach the agent CLI THROUGH the user's own wrapper, not
around it, because the wrapper is where their auth is set up (the author's
real ``cld`` exports an OAuth token out of the keychain before exec'ing
claude). A fork that bypassed it would launch unauthenticated.

Two paths have to carry them and they carry them differently:

  wrapper   as positional arguments after a throwaway ``_`` $0, forwarded
            through the wrapper's own ``"$@"`` chain
  static    appended to the command string BEFORE the rc wrapper is
            applied, because doing string surgery on an already-quoted
            ``zsh -c '...'`` is how quoting bugs get made
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

from src.core.agent_families import get_family, render_static_command
from src.core.agent_wrappers import AgentWrapper, render_wrapper_invocation

UUID = "0199aa11-bbbb-cccc-dddd-eeeeeeeeeeee"
FORK_ARGS = ["--resume", UUID, "--fork-session"]


def _wrapper(**kw):
    """An AgentWrapper with test defaults."""
    base = dict(
        id="cld", label="cld", script="cld() { command claude \"$@\"; }",
        entry="cld", default=True, accepts_model=False, family="claude",
    )
    base.update(kw)
    return AgentWrapper(**base)


# --- the wrapper path --------------------------------------------------------


def test_fork_args_reach_a_wrapper_as_positionals(tmp_path):
    """They land after a throwaway $0 so "$@" can see all of them."""
    out = render_wrapper_invocation(_wrapper(), tmp_path, extra_args=FORK_ARGS)
    assert out.endswith(f" _ --resume {UUID} --fork-session")


def test_the_throwaway_zero_is_emitted_even_with_no_model(tmp_path):
    """THE BUG THIS GUARDS.

    The ``_`` used to be emitted only when a model was present. With fork
    args and no model, the first real argument would land in $0 - which
    ``"$@"`` does not include - and vanish with no error at all. The fork
    would launch a FRESH conversation while reporting success.
    """
    out = render_wrapper_invocation(_wrapper(), tmp_path, extra_args=FORK_ARGS)
    assert " _ " in out, "no $0 slot: the first fork argument would be swallowed"
    assert out.index(" _ ") < out.index("--resume")


def test_a_model_and_fork_args_coexist_in_order(tmp_path):
    """Model stays $1; fork args follow it."""
    out = render_wrapper_invocation(
        _wrapper(accepts_model=True), tmp_path, model="vendor/m-1",
        extra_args=FORK_ARGS,
    )
    assert out.endswith(f" _ vendor/m-1 --resume {UUID} --fork-session")


def test_a_modelless_wrapper_still_receives_the_fork_args(tmp_path):
    """accepts_model gates the MODEL, never the fork arguments.

    accepts_model answers "does this wrapper consume an OpenRouter model
    id". --resume/--fork-session are arguments to the agent CLI itself and
    every wrapper forwards "$@" to it. Gating them on that flag would make
    a fork through a modelless wrapper launch a brand new conversation
    instead of the forked one, silently.

    Asserted through ``Settings.get_agent_command``, because that is where
    the gate lives - ``render_wrapper_invocation`` is handed an
    already-decided model and does not second-guess it.
    """
    import json

    from src.config import Settings

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "config_version": 3,
        "agents": {
            "claude_command": "",
            "wrappers": [_wrapper(accepts_model=False).model_dump()],
        },
        "projects": [],
    }))
    s = Settings(
        default_working_dir=str(tmp_path),
        log_directory=str(tmp_path / "logs"),
        totp_secret="x",
        jwt_secret="y",
        auth_config_file=str(config_path),
    )

    out = s.get_agent_command("cld", model="vendor/m-1", extra_args=FORK_ARGS)
    assert "vendor/m-1" not in out, "a modelless wrapper was handed a model"
    assert f"--resume {UUID} --fork-session" in out


def test_no_extra_args_is_byte_identical_to_before(tmp_path):
    """The common launch must not change shape at all."""
    plain = render_wrapper_invocation(_wrapper(), tmp_path)
    assert " _ " not in plain
    assert plain == render_wrapper_invocation(_wrapper(), tmp_path, extra_args=[])


# --- the static path ---------------------------------------------------------


def test_fork_args_land_inside_the_rc_quoting(tmp_path):
    """Appended before rc_prefixed, so they are inside the zsh -c string."""
    out = render_static_command(get_family("claude"), "claude", extra_args=FORK_ARGS)
    assert out.startswith("zsh -c ")
    assert f"claude --resume {UUID} --fork-session" in out
    # and NOT dangling outside the closing quote
    assert not out.rstrip().endswith("--fork-session'") is False


def test_a_raw_family_still_gets_them(tmp_path):
    """shell renders raw; arguments still append."""
    out = render_static_command(get_family("shell"), "$SHELL -i", extra_args=["--x"])
    assert out == "$SHELL -i --x"


@pytest.mark.parametrize("hostile", [
    "; rm -rf /", "`whoami`", "$(id)", "~/secret", "a b", "'quoted'",
    "$IFS", "\n newline", "--not-a-flag=$(id)",
])
def test_a_hostile_uuid_cannot_break_out(tmp_path, hostile):
    """Every argument survives as exactly ONE token, unmodified.

    Asserted by re-parsing the rendered string with the same lexer a shell
    uses, rather than by substring-matching for scary characters. A
    substring check cannot tell "quoted safely" from "interpolated
    dangerously" - it only knows the bytes are present, which is true in
    both cases.
    """
    import shlex

    st = render_static_command(
        get_family("claude"), "claude", extra_args=["--resume", hostile]
    )
    # zsh -c '<inner>'  ->  take the inner script back apart
    outer = shlex.split(st)
    assert outer[0] == "zsh" and outer[1] == "-c"
    inner_tokens = shlex.split(outer[2].split("; ", 1)[1])
    assert inner_tokens == ["claude", "--resume", hostile], inner_tokens

    w = render_wrapper_invocation(
        _wrapper(), tmp_path, extra_args=["--resume", hostile]
    )
    w_tokens = shlex.split(w)
    assert w_tokens[-3:] == ["_", "--resume", hostile], w_tokens[-3:]
