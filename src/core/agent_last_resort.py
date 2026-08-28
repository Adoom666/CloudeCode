"""Per-family LAST-RESORT launch commands, and the example wrappers built
from the same text.

THE BUG THIS MODULE EXISTS TO CLOSE
-----------------------------------
``AgentFamily.last_resort`` was ``None`` for every family except
``claude``. Reaching it is easy: a family with no wrapper falls back to
``render_static_command``, which runs the configured
``agents.<family>_command``; when that string is empty the renderer asks
the family for its last resort, and ``None`` means it returns the EMPTY
STRING. An empty string is then handed to
``tmux new-session ... <command>``, which starts a pane that immediately
exits. Nothing launches, nothing is logged, and the failure is
indistinguishable from a session that died on its own.

That is the exact false-green shape this codebase keeps finding: a code
path that cannot evaluate ("I have nothing to run") reported as a normal
outcome ("here is your command"). There are three outcomes here, not two:
the tool runs, the tool is missing, or nothing is configured at all. The
third must SAY so.

WHY A LAST RESORT PER FAMILY RATHER THAN JUST DEFAULT WRAPPERS
--------------------------------------------------------------
A wrapper is opt-in user data in ``config.json``. Shipping example
wrappers for codex/hermes/openclaw/shell is worth doing (see
``example_wrapper`` below, and ``agent_wrappers.EXAMPLE_WRAPPERS``) but it
CANNOT fix this bug: an example nobody imported is not in the config, so
the resolver still walks the same empty-string path. The guarantee has to
live where the empty string was produced. So: both, with different jobs.
The ``last_resort`` column is the guarantee; the examples are a starting
point a user can edit.

WHY THE FAILURE DROPS INTO A SHELL
-----------------------------------
``exec codex`` when codex is absent prints one line of "command not
found" and the pane exits instantly. In a tmux pane the user is watching,
an instantly-exiting pane paints nothing they can read. Keeping the pane
alive with an interactive shell after printing the diagnosis is what makes
the failure LEGIBLE rather than merely loud.

WHY THE rc IS SOURCED
---------------------
Same reason ``claude``'s last resort does it (see
``src/core/shell_init.py``): tmux's pane shell is neither interactive nor
a login shell, so a tool installed under ``~/.local/bin``, a Homebrew
prefix, an nvm/asdf shim, or defined as a shell function is simply not on
PATH there. Probing PATH without sourcing the rc would report "not
installed" for a tool that is installed, which is a WRONG diagnosis, not a
conservative one.

``shell`` is the exception and takes no rc prefix: its command is
``$SHELL -i``, which must reach tmux unwrapped (see
``agent_families.render_static_command``), and there is no third-party
binary to probe.
"""

from __future__ import annotations

import shlex
from typing import Dict, Optional

from src.core.shell_init import rc_prefixed

#: The interactive shell a failed launch falls back INTO so its diagnosis
#: stays on screen. ``${SHELL:-/bin/sh}`` because ``$SHELL`` is not
#: guaranteed to be set in a tmux pane's environment, and an unquoted
#: empty ``$SHELL -i`` would run ``-i`` as a command.
FALLBACK_SHELL = '"${SHELL:-/bin/sh}" -i'

#: What a plain console session runs when ``agents.shell_command`` is
#: blank. Deliberately identical to the shipped default for that field.
SHELL_LAST_RESORT = FALLBACK_SHELL


def missing_tool_message(binary: str, family: str, command_field: str) -> str:
    """The lines printed into the pane when a family's tool is absent.

    Description: names the tool, states that NOTHING is configured for
      this family (which is the only way this text is ever reached), and
      lists the three concrete fixes. Lowercase, no punctuation
      decoration; this is terminal output, not UI chrome.
    Inputs:
      binary (str) - the executable that was probed, e.g. "codex".
      family (str) - the agent family name, e.g. "codex".
      command_field (str) - the config key holding the static fallback,
        e.g. "codex_command".
    Output: str - newline-joined message body, no trailing newline.
    Example: missing_tool_message("codex", "codex", "codex_command")
    """
    # DELIBERATELY APOSTROPHE-FREE. Every line is shlex-quoted into a
    # single-quoted shell word and that word is then shlex-quoted AGAIN
    # by rc_prefixed. One apostrophe becomes '"'"'"'"'"'"'"'"' after two
    # rounds; four of them made the rendered command four times longer
    # than the text it prints, for no gain.
    return "\n".join([
        f"cloude: {binary} is not installed, or is not on the PATH of this pane.",
        f"nothing is configured for the {family} family: no {family} wrapper,"
        f" and agents.{command_field} is empty.",
        f"fix any one of: install {binary}; add a {family} wrapper in"
        f" settings > wrappers; or set {command_field}.",
        "dropping you into a shell so this message stays readable.",
    ])


def guarded_launch_script(binary: str, invocation: str, family: str, command_field: str) -> str:
    """Build the shell text that runs a tool or explains why it cannot.

    Description: the single source of the "probe, then run or explain"
      shape, shared by every family's last resort AND by the example
      wrapper offered for that family, so the two can never disagree
      about what a missing tool looks like. The message goes to stderr so
      it is not mistaken for the tool's own output.
    Inputs:
      binary (str) - executable to probe with ``command -v``.
      invocation (str) - the full command line to exec when present,
        e.g. ``"openclaw tui"``. Passed through verbatim; it is
        library-authored text, never user input.
      family (str) - agent family name, for the message.
      command_field (str) - the family's static-command config key.
    Output: str - a single-line ``if ... else ... fi`` shell string.
    Example: guarded_launch_script("codex", "codex", "codex", "codex_command")
    """
    lines = missing_tool_message(binary, family, command_field).split("\n")
    quoted = " ".join(shlex.quote(line) for line in lines)
    return (
        f"if command -v {shlex.quote(binary)} >/dev/null 2>&1; then "
        f"exec {invocation}; "
        f"else printf '%s\\n' {quoted} >&2; exec {FALLBACK_SHELL}; fi"
    )


def make_tool_last_resort(binary: str, invocation: str, family: str, command_field: str):
    """Build a family's ``last_resort`` renderer for a third-party CLI.

    Description: wraps ``guarded_launch_script`` in the rc-sourcing
      prefix. The returned callable matches the
      ``Callable[[Optional[str]], str]`` shape ``AgentFamily.last_resort``
      declares; the ``model`` argument is accepted and IGNORED, because
      none of these CLIs takes an OpenRouter model id the way ``cldor``
      does, and silently appending one would arrive as a prompt argument
      (the same defect ``AgentWrapper.accepts_model`` exists to prevent).
    Inputs:
      binary (str); invocation (str); family (str); command_field (str) -
        see ``guarded_launch_script``.
    Output: Callable[[Optional[str]], str] - the renderer.
    Example: make_tool_last_resort("codex", "codex", "codex", "codex_command")(None)
    """
    script = guarded_launch_script(binary, invocation, family, command_field)

    def _render(model: Optional[str] = None) -> str:
        """Render this family's last-resort command. ``model`` is ignored."""
        return rc_prefixed(script)

    _render.__name__ = f"_render_{family}_last_resort"
    return _render


def render_shell_last_resort(model: Optional[str] = None) -> str:
    """Last resort for the ``shell`` family: a plain interactive shell.

    Description: returned RAW, with no ``zsh -c`` prefix, because that is
      what ``shell_command`` has always been and what tmux needs for a
      console pane (see ``agent_families``' module docstring). There is no
      tool to probe: if ``$SHELL`` is unset the ``${SHELL:-/bin/sh}``
      default covers it, so this branch cannot produce an empty command.
    Inputs: model (str | None) - ignored; a console session has no model.
    Output: str - ``"${SHELL:-/bin/sh}" -i``.
    Example: render_shell_last_resort() -> '"${SHELL:-/bin/sh}" -i'
    """
    return SHELL_LAST_RESORT


def example_wrapper(family: str, binary: str, invocation: str, command_field: str, label: str) -> Dict[str, object]:
    """Build the offered example wrapper for a third-party-CLI family.

    Description: the script is the SAME guarded text the family's last
      resort uses, so importing the example and editing it starts from a
      launch that already fails legibly. Deliberately NOT modelled on the
      author's ``cld`` / ``cldor`` examples: those read a credential from
      the macOS Keychain at run time because Claude Code needs one, and
      copying that pattern for a tool that does not would teach a
      keychain dependency nobody asked for.
    Inputs:
      family (str) - the wrapper's family; also its id.
      binary (str) - executable probed by the script.
      invocation (str) - command line run when present.
      command_field (str) - the family's static-command config key.
      label (str) - lowercase UI label.
    Output: dict - an ``AgentWrapper``-shaped dict.
    Example: example_wrapper("codex", "codex", "codex", "codex_command", "codex")
    """
    return {
        "id": f"{family}-default",
        "family": family,
        "label": label,
        "script": guarded_launch_script(binary, invocation, family, command_field),
        "entry": None,
        "description": (
            f"example - runs {binary} when it is on PATH, and when it is not, "
            "prints what is wrong and leaves you in a shell instead of an "
            "empty pane that exits on its own."
        ),
        "default": False,
        "accepts_model": False,
    }
