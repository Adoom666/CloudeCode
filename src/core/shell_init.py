"""The one place that decides how ``~/.zshrc`` is sourced for a new pane.

WHY THIS MODULE EXISTS
----------------------

Every agent launch is wrapped in ``zsh -c 'source ~/.zshrc ...; <cmd>'``.
The rc has to be sourced in the launching shell itself, because that is
where the user's ``PATH``, exports and shell FUNCTIONS come from - ``cld``
is a function defined in ``~/.zshrc``, and tmux's spawned pane shell is
neither interactive nor a login shell, so without the source a launch is
"command not found". That requirement is not negotiable and nothing here
changes it.

The rc-sourcing prefix used to be a string literal repeated in three
places (``agent_families._render_claude_last_resort``,
``agent_families.render_static_command``, and
``agent_wrappers.render_wrapper_invocation``). It is now a function, so
the decision below is made once.

THE BUG THIS FIXES: A SILENT HANG AT SESSION START
--------------------------------------------------

The prefix was ``source ~/.zshrc >/dev/null 2>&1``. Discarding rc output
is a real goal - a measured real-world ``~/.zshrc`` prints a 23-line,
797-byte MOTD banner, and painting that into every pane before the agent
starts is noise nobody asked for.

But rc ran with a TTY still on **stdin**, and a script that sees a TTY on
stdin reasonably concludes it may ask the user a question. The reported
case was a dotfiles update checker whose logic is, correctly::

    if [[ -t 0 ]]; then
        printf "Do you want to update the profile? [y/N]: "   # STDOUT
        read -r REPLY
    else
        echo "[dotfiles] update available - run ... to update"
    fi

In a tmux pane ``[[ -t 0 ]]`` is true, so it prompts and blocks on
``read``. The prompt goes to **stdout** (``printf``, not ``>&2``), which
``>/dev/null`` throws away. Result: the pane emits zero bytes,
``capture-pane`` returns completely empty, the client faithfully renders
nothing, and the session looks dead. The user presses Enter blind and
that keystroke answers a question they never saw.

So the app was handing rc a TTY on stdin while discarding its stdout: it
invited the question and then hid it.

THE FIX, AND WHAT WAS REJECTED
------------------------------

The prefix is now ``source ~/.zshrc >/dev/null 2>&1 </dev/null``. The
redirections apply to the ``source`` builtin ONLY - fd 0 is restored for
every command after it, so the AGENT still gets the pane's real TTY on
stdin, which it needs. Verified in a real detached tmux session: after
the source, ``[[ -t 0 ]]`` is true, ``$FROM_RC`` is exported, and a
function defined by the rc still runs.

Making stdin consistent with stdout is what fixes the class:

* A script that gates on ``[[ -t 0 ]]`` - the near-universal convention,
  and exactly what the reported script does - now takes its
  non-interactive branch and never blocks. That branch already exists
  because the author wrote it for SSH and cron; a pane whose output is
  discarded is the same situation.
* A script that reads WITHOUT a guard gets EOF immediately: ``read``
  returns non-zero with an empty REPLY and the script carries on. An
  unbounded hang becomes an instant default. Verified in tmux.

Rejected alternatives:

* **Stop discarding stderr, keep discarding stdout.** Does not fix the
  reported bug at all - the prompt is written to stdout by ``printf``,
  verified against the actual script rather than assumed. It would also
  paint three lines of genuine junk ("TERM environment variable not
  set.") into every pane, buying noise for no safety.
* **Do not redirect at all.** Honest, but measured at 23 lines of banner
  per session. Trading a silent hang for a permanent banner is a bad
  deal when ``</dev/null`` removes the hang and keeps the pane clean.
* **Run rc non-interactively via ``zsh -ic`` or by unsetting the ``i``
  flag.** Changes rc semantics wholesale, which is precisely the risk
  called out above: the rc is sourced BECAUSE the wrapper needs the
  functions it defines, and rc files routinely gate function definitions
  on interactivity. Redirecting fd 0 changes exactly one observable
  thing and leaves every definition intact.

Suppressing output remains a bet that nothing important is said during
rc. ``src/api/ws_startup_paint.py`` covers the residual case: a pane that
is still blank with a live process gets a message saying so, rather than
looking dead.
"""

from __future__ import annotations

import shlex

#: How ``~/.zshrc`` is sourced in every launched pane.
#:
#: ``>/dev/null 2>&1`` keeps rc chatter out of the pane. ``</dev/null``
#: is what makes that suppression safe: it stops rc from asking a
#: question whose prompt the suppression would swallow. All three
#: redirections are scoped to the ``source`` builtin, so the agent that
#: runs afterwards still has the pane's real stdin, stdout and stderr.
#:
#: Do not drop ``</dev/null`` without also dropping ``>/dev/null`` - the
#: hang is caused by the two disagreeing, not by either one alone.
RC_SOURCE = "source ~/.zshrc >/dev/null 2>&1 </dev/null"


def rc_prefixed(command: str) -> str:
    """Prefix ``command`` with the rc source, inside its own ``zsh -c``.

    Description: builds the complete, tmux-ready shell string for a launch
      that needs the user's rc environment (PATH, exports, and functions
      such as ``cld``). The result is a single flat string, which is what
      the tmux backend hands to ``new-session ... <command>``.
    Inputs:
      command (str) - the shell text to run once rc has been sourced.
        Passed through verbatim; callers that interpolate untrusted
        values must quote them before calling.
    Output: str - e.g. ``zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'``
    Example:
        >>> rc_prefixed("cld")
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'"
    """
    return f"zsh -c {shlex.quote(f'{RC_SOURCE}; {command}')}"
