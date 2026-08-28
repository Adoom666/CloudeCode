"""Push a CloudeCode rename through to Claude Code's own session name.

WHY THIS EXISTS. CloudeCode's rename and Claude Code's rename are two
different names for the same session, and until now only the first one
moved. Claude Code has carried ``/rename <name>`` since v2.1.205 and
``-n/--name`` at launch, and it keeps the result in its own transcript as
a ``custom-title`` row - so the session the user renamed in the browser
went on calling itself something else in the prompt bar, the ``/resume``
picker and the terminal title.

WHAT THIS CANNOT DO, and it shapes everything below: there is no
``SessionRename`` hook event (verified against the shipped 2.1.248 binary:
zero occurrences), and ``/rename`` does NOT fire ``UserPromptSubmit``,
because Claude intercepts slash commands before they become prompts. So
the sync is ONE-WAY BY CONSTRUCTION. CloudeCode can push a name TO Claude;
it can never be notified that a user typed ``/rename`` in the terminal. It
learns that only by pulling - ``session_title`` arrives on the next
SessionStart payload, one lifecycle event late, and lands in
``sessions.claude_title``.

WHY IT IS GATED RATHER THAN JUST SENT. The push is `tmux send-keys` into a
pane the user is sitting in. That is a real mutation of live state, and
the failure mode is not an error - it is a line of text landing in the
middle of whatever they were typing, or a slash command queued behind a
running turn and executed minutes later against a conversation that has
moved on. So the decision to send is made from the session's ACTIVITY
STATUS, and the three outcomes are named rather than collapsed:

  PUSH_SENT       - the pane was idle at a prompt; the command went in.
  PUSH_DEFERRED   - the pane was busy, dead, or its state could not be
                    read. Nothing was sent. This is NOT a failure: the
                    name is already stored on our side, and Claude's copy
                    can be pushed later or set by the user.
  PUSH_UNSUPPORTED- this session is not a Claude session, or the running
                    Claude is older than ``MIN_RENAME_VERSION``.

Never invent a fourth meaning by returning PUSH_SENT for a send whose
effect was not observed. A queued keystroke is not a rename.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

#: Claude Code releases before this have no ``/rename`` command at all.
#: From the official command list: "Also available in non-interactive mode
#: (-p); requires Claude Code v2.1.205 or later."
MIN_RENAME_VERSION: Tuple[int, int, int] = (2, 1, 205)

#: The three outcomes. See the module docstring - they are deliberately
#: distinguishable, because "we did not send" and "we sent" must never
#: render the same way in a log or a response.
PUSH_SENT: str = "sent"
PUSH_DEFERRED: str = "deferred"
PUSH_UNSUPPORTED: str = "unsupported"

#: Claude's own cap, applied at every rename surface including claude.ai
#: and the desktop app. Mirrored here so a name we refuse to send is
#: refused for the same reason Claude would refuse it, rather than being
#: truncated silently into a different name than the user chose.
MAX_NAME_CHARS: int = 200


def parse_claude_version(text: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """Parse a ``claude --version`` string into a comparable triple.

    Description: accepts the shipped format ``"2.1.248 (Claude Code)"`` and
      a bare ``"2.1.248"``. Returns None for anything it cannot parse
      rather than guessing a version, because a wrong version guess here
      decides whether we send a command to a user's terminal.
    Inputs: text (str | None) - raw stdout from ``claude --version``.
    Output: tuple[int, int, int] | None.
    Example: parse_claude_version("2.1.248 (Claude Code)") -> (2, 1, 248)
    """
    if not text:
        return None
    head = text.strip().split()[0] if text.strip() else ""
    parts = head.split(".")
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def supports_rename(version: Optional[Tuple[int, int, int]]) -> bool:
    """Whether a parsed Claude version has ``/rename``.

    Description: an UNKNOWN version returns False. That is the safe
      direction: not sending costs a cosmetic mismatch the user can fix,
      while sending ``/rename Foo`` to a build that has no such command
      types a literal line into their conversation.
    Inputs: version (tuple | None) - from ``parse_claude_version``.
    Output: bool.
    Example: supports_rename((2, 1, 248)) -> True
    """
    if version is None:
        return False
    return version >= MIN_RENAME_VERSION


def name_is_pushable(label: str) -> bool:
    """Whether a label can be sent to Claude unchanged.

    Description: refuses anything Claude itself would rewrite, so the two
      sides cannot silently end up with different names. Claude replaces
      control and invisible characters with spaces and caps at
      ``MAX_NAME_CHARS``; rather than pre-apply those rules and push a
      name the user did not choose, a label that would be altered is not
      pushed at all and the mismatch stays visible on our side.

      A newline is refused for a second, harder reason: ``send-keys``
      would submit at the newline, so half the label would arrive as a
      command and the remainder as a prompt.
    Inputs: label (str) - the user's chosen label.
    Output: bool.
    Example: name_is_pushable("Refactor spike") -> True
    """
    if not label or not label.strip():
        return False
    if len(label) > MAX_NAME_CHARS:
        return False
    return all(ch == " " or ch.isprintable() for ch in label)


#: OUT-OF-BAND IS THE PRIMARY PATH, and it replaced an activity gate.
#:
#: The first design typed `/rename <name>` into the live pane and had to
#: be gated on the session being at a prompt - because text typed while
#: Claude is waiting on an answer BECOMES that answer. That gate was
#: necessary, fiddly, and it meant a rename simply did not happen while a
#: session was busy.
#:
#: `claude -p --resume <uuid> "/rename <name>"` needs none of it. Measured
#: on the mini against a live, running session: 0.7 seconds, no pane
#: contact, no fork, the same uuid, and the session stayed fully
#: responsive afterwards. It also works UNAUTHENTICATED, because /rename
#: is a local operation that makes no model call.
#:
#: TWO COSTS, both measured rather than assumed.
#: 1. It is NOT invisible. The call appends a `<local-command-caveat>` and
#:    a `<command-name>/rename</command-name>` record to the shared
#:    transcript, so two processes write one conversation. They
#:    interleaved cleanly here and the live session carried on, but that
#:    is one sample and not a guarantee - which is exactly why this shape
#:    is used for a WRITE THAT CANNOT BE MADE ANY OTHER WAY and never for
#:    reading. Anything we merely want to KNOW is already in the
#:    transcript, free and read-only, with no second writer.
#: 2. It does NOT refresh the running UI. The live process holds its title
#:    in memory; the new name appears on resume and in the /resume picker.
#:
#: What it cannot be used for: anything that mutates the conversation the
#: live process is holding - `/clear`, `/compact`. Two writers, and the
#: live process wins on its next write, silently discarding the OOB work.
OOB_TIMEOUT_SECONDS: int = 15


def oob_rename_argv(
    claude_path: str, claude_uuid: str, label: str
) -> List[str]:
    """The exact argv for an out-of-band rename.

    Description: a list, never a shell string - the label is user text
      and may contain quotes, backticks or `$(...)`. Passing it as an
      argv element means no shell ever parses it, which removes the whole
      injection class rather than trying to escape it.
    Inputs: claude_path (str) - resolved claude binary. claude_uuid (str)
      - the conversation to resume. label (str) - the new name.
    Output: list[str] - argv.
    Example: oob_rename_argv('/bin/claude', 'u1', 'Spike')
      -> ['/bin/claude', '-p', '--resume', 'u1', '/rename Spike']
    """
    return [
        claude_path,
        "-p",
        "--resume",
        claude_uuid,
        f"/rename {label}",
    ]


def decide_push(
    *,
    label: str,
    claude_uuid: Optional[str],
    claude_version: Optional[Tuple[int, int, int]],
    is_claude_session: bool,
) -> Tuple[str, str]:
    """Decide whether an out-of-band rename can be performed.

    Description: PURE FUNCTION, and much smaller than the gate it
      replaced. Out-of-band needs no activity check at all - it never
      touches the pane, so there is no state of the session in which it
      would be misread as input.

      What it does need is a conversation to resume. `--resume` addresses
      a Claude session by uuid, and a session that has not yet bound one
      (no SessionStart hook has arrived) cannot be addressed at all. That
      is a DEFERRAL, not a failure: the name is already stored on our
      side and can be pushed once the uuid is known.
    Inputs: label (str). claude_uuid (str | None) - the bound Claude
      conversation uuid. claude_version (tuple | None).
      is_claude_session (bool).
    Output: tuple[str, str] - (outcome, human-readable reason).
    Example: decide_push(label='x', claude_uuid='u1',
      claude_version=(2,1,248), is_claude_session=True)[0] -> 'sent'
    """
    if not is_claude_session:
        return (PUSH_UNSUPPORTED, "not a Claude session")
    if not supports_rename(claude_version):
        shown = ".".join(str(p) for p in claude_version) if claude_version else "unknown"
        return (
            PUSH_UNSUPPORTED,
            f"claude {shown} has no /rename (needs "
            f"{'.'.join(str(p) for p in MIN_RENAME_VERSION)}+)",
        )
    if not name_is_pushable(label):
        return (
            PUSH_DEFERRED,
            "claude would rewrite this name, so the two sides would "
            "disagree - not sending",
        )
    if not claude_uuid:
        return (
            PUSH_DEFERRED,
            "no Claude conversation uuid is bound yet, so there is "
            "nothing for --resume to address",
        )
    return (PUSH_SENT, "out of band via --resume, no pane contact")


def rename_command(label: str) -> str:
    """The exact line to type into the pane.

    Description: no shell quoting - ``send-keys -l`` sends literal text to
      the application, not to a shell, so the label travels as typed.
      ``name_is_pushable`` has already refused anything containing a
      newline or a control character, which is what makes that safe.
    Inputs: label (str) - already passed ``name_is_pushable``.
    Output: str - e.g. ``"/rename My Session"``.
    Example: rename_command("My Session") -> '/rename My Session'
    """
    return f"/rename {label}"


#: The only family whose CLI accepts ``-n/--name``. This is a per-FAMILY
#: fact, not a per-wrapper one: a wrapper forwards "$@" to whatever binary
#: its family names, so a claude wrapper carries the flag through and a
#: codex or shell wrapper hands it to a program that has never heard of
#: it. Getting this wrong does not degrade the launch, it BREAKS it - the
#: flag arrives as an unknown option or, worse, as a prompt argument.
#: (Same shape as the cldl picker defect: a per-family capability read off
#: the wrong object.)
RENAME_CAPABLE_FAMILY: str = "claude"


def launch_name_args(
    *,
    label: Optional[str],
    family: Optional[str],
    claude_version: Optional[Tuple[int, int, int]],
) -> list:
    """Arguments that make Claude launch already knowing its name.

    Description: the RISK-FREE half of name syncing. ``/rename`` has to be
      typed into a live pane and is therefore gated on that pane being
      idle; ``--name`` is set at birth, before anything is running, so
      there is nothing to interrupt and no queueing to reason about. When
      a session is created with a label already chosen, this is strictly
      better than pushing a rename afterwards.

      Returns EMPTY rather than raising for every case it cannot serve -
      no label, a label Claude would rewrite, a non-claude family, or a
      version without the flag. An empty arg list leaves the launch
      exactly as it was before this function existed, which is the only
      safe failure mode for something that edits a command line.
    Inputs: label (str | None) - the user's chosen label. family (str |
      None) - the agent family. claude_version (tuple | None).
    Output: list[str] - ``["--name", label]`` or ``[]``.
    Example: launch_name_args(label='Spike', family='claude',
      claude_version=(2, 1, 248)) -> ['--name', 'Spike']
    """
    if not label or family != RENAME_CAPABLE_FAMILY:
        return []
    if not supports_rename(claude_version):
        return []
    if not name_is_pushable(label):
        return []
    return ["--name", label]


def detect_claude_version() -> Optional[Tuple[int, int, int]]:
    """The version of the claude CLI this box would actually launch.

    Description: reuses ``server_status.collect_claude_cli`` rather than
      running its own probe. That module already solves the hard part -
      ``claude`` is not on a non-interactive shell's PATH here, so it
      walks absolute candidates - and a second resolver would be free to
      disagree with the one the status page shows, which is how two
      sources of truth start.

      Returns None on ANY failure, and None means unsupported everywhere
      it is consumed. Import errors and subprocess failures are caught
      together on purpose: from this function's point of view they are
      the same fact, which is "I could not establish a version", and the
      caller must not send a command on the strength of a guess.
    Inputs: none.
    Output: tuple[int, int, int] | None.
    Example: detect_claude_version() -> (2, 1, 248)
    """
    try:
        from src.core.server_status import collect_claude_cli

        info = collect_claude_cli()
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("claude_version_probe_failed", error=str(exc))
        return None
    if not info or not info.get("available"):
        return None
    return parse_claude_version(info.get("version"))


def launch_name_args_for_agent_type(
    *, label: Optional[str], agent_type: Optional[str]
) -> list:
    """``--name`` arguments for a launch of ``agent_type``, or nothing.

    Description: the call-site wrapper around ``launch_name_args``.
      Resolves ``agent_type`` to the family that will ACTUALLY run and
      asks whether that family takes the flag. Uses the LAUNCH-time
      resolver (``agent_families.get_family``) deliberately - the
      question here is not "what should a status pill display" but "what
      binary is about to receive this argument", and only the launch
      resolver answers that.

      Lives in this module rather than in ``session_manager`` so it can
      be tested without importing the server's settings machinery, which
      exits the process when no .env is present. A policy function that
      can only run inside a configured server is a policy function
      nobody tests.

      Returns ``[]`` on ANY failure and never raises: it edits a command
      line that is about to execute in the user's shell, so the only
      acceptable failure mode is changing nothing.
    Inputs: label (str | None). agent_type (str | None).
    Output: list[str] - ``["--name", label]`` or ``[]``.
    Example: launch_name_args_for_agent_type(label='Spike',
      agent_type='claude') -> ['--name', 'Spike']
    """
    if not label:
        return []
    try:
        from src.core.agent_families import get_family

        family = getattr(get_family(agent_type), "name", None)
        return launch_name_args(
            label=label, family=family, claude_version=detect_claude_version()
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("launch_name_args_failed", error=str(exc))
        return []
