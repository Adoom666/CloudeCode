"""User-editable list of common shell commands for the console tab.

feat/settings-tabs-and-commands - a small, ordered, named list stored in
``config.json`` under the TOP-LEVEL ``terminal_commands`` key. Each entry is
one shell command line the user can run in one click from the settings
panel's "terminal" tab.

SECURITY MODEL, load-bearing, read before changing anything here
-----------------------------------------------------------------
These strings are shell commands and this app is not single-user. They are
therefore NEVER executed server-side. There is deliberately:

  * no ``subprocess.run`` / ``os.system`` / ``eval`` anywhere in this module
    or in any caller that consumes a ``TerminalCommand.command``;
  * no endpoint that accepts a command STRING and runs it. The only
    launch-side input the API accepts is a command ``id`` (see
    ``CreateSessionRequest.terminal_command_id``), which is looked up in
    the user's own config here. A string the client invents cannot reach a
    shell through this path at all.

The single sanctioned consumption path is:

  client sends ``agent_type="shell"`` + ``terminal_command_id="<id>"``
    -> ``Settings.get_terminal_command(id)`` resolves it FROM CONFIG
    -> ``SessionManager.create_session`` starts an ordinary console session
       (``agents.shell_command``, i.e. ``$SHELL -i``)
    -> the resolved text is written into that pane with the EXISTING
       ``SessionBackend.write()`` (tmux ``send-keys``), exactly as if the
       user had typed it.

So the command is typed into a tmux pane the user is looking at and can
Ctrl-C, not exec'd by the server process. If you ever find yourself adding
a "run this command and return its output" endpoint, stop: that is a remote
shell, which is a different product with a different threat model.

Why the console session shells out through ``$SHELL -i``: a non-interactive
shell does NOT source ``~/.zshrc``, so ``claude`` (and the user's ``cld`` /
``cldor`` functions, and Homebrew's PATH entries) are simply not on PATH
there. ``agents.shell_command`` is ``$SHELL -i`` precisely so rc files load
and these commands resolve. This is the same constraint
``Settings.get_agent_command`` documents for its ``zsh -c 'source
~/.zshrc; ...'`` wrapper.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

import structlog
from pydantic import BaseModel, Field, field_validator

logger = structlog.get_logger()

#: Config.json key holding the list. Top-level (a sibling of ``agents`` /
#: ``providers``) because these commands are not agent-launch config: they
#: run in a plain console session and have nothing to do with any agent CLI.
TERMINAL_COMMANDS_KEY = "terminal_commands"

#: Stable identifier charset. Same shape/rationale as
#: ``agent_wrappers.WRAPPER_ID_PATTERN`` - filesystem- and URL-safe, and
#: unambiguous as a path segment in ``PUT /terminal/commands``.
TERMINAL_COMMAND_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_TERMINAL_COMMAND_ID_RE = re.compile(TERMINAL_COMMAND_ID_PATTERN)

#: Upper bound on how many entries are accepted in one replace call. Not a
#: security control (nothing here executes) - a guard against a runaway
#: client writing an unbounded list into config.json.
MAX_TERMINAL_COMMANDS = 100


def is_valid_terminal_command_id(v: str) -> bool:
    """Single source of truth for terminal-command id validation.

    Inputs: v (str) - candidate id.
    Output: bool - True if v matches TERMINAL_COMMAND_ID_PATTERN exactly.
    """
    return bool(_TERMINAL_COMMAND_ID_RE.fullmatch(v))


class TerminalCommand(BaseModel):
    """One named shell command line runnable from the terminal tab.

    - ``id``: stable identifier; the ONLY value a launch request sends
      (see this module's docstring on why a raw command string is never
      accepted from a client).
    - ``label``: human-readable name shown on the button.
    - ``command``: the shell line, typed verbatim into a console pane.

    Order in the stored list IS the display order - reordering is a
    whole-list replace, not a per-entry index field, so the persisted
    order can never disagree with itself.
    """

    id: str = Field(..., description="Stable id; the only value a launch request sends")
    label: str = Field(..., description="Human-readable name shown on the button")
    command: str = Field(..., description="Shell line typed into a console pane")

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not is_valid_terminal_command_id(v):
            raise ValueError(f"terminal command id must match {TERMINAL_COMMAND_ID_PATTERN}")
        return v

    @field_validator("label", "command")
    @classmethod
    def _validate_nonblank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("label and command must not be blank")
        return v


def _preferred_top_command() -> str:
    """Choose the seeded process-viewer command for THIS machine.

    Description: ``htop`` when it is installed, else the always-present
      ``top``. Probed once at seed time (never at launch time) so the
      stored value is a plain editable string the user can change, not a
      hidden runtime branch that would surprise them later.
    Inputs: none.
    Output: str - "htop" or "top".
    """
    return "htop" if shutil.which("htop") else "top"


def default_terminal_commands() -> List[Dict[str, str]]:
    """Build the seed list of terminal commands.

    Description: the three entries requested for the feature. Returned as
      plain dicts (not models) because both consumers want JSON-shaped
      data: the pydantic default on ``AuthConfig`` and the v1->v2 config
      migration, which writes them straight to disk.
      Notes on the specific choices:
        * ``brew upgrade --cask claude-code`` - Claude Code is installed
          as a Homebrew CASK on the deploy host, NOT via npm. An npm
          command here would silently do nothing.
        * ``tmux -L cloude ls`` - the app runs every session on its own
          ``-L cloude`` socket (``SessionConfig.tmux_socket_name``). A
          bare ``tmux ls`` would report the user's unrelated default
          server instead.
    Inputs: none.
    Output: list[dict] - TerminalCommand-shaped dicts in display order.
    Example: default_terminal_commands()[0]["id"] -> "update-claude"
    """
    return [
        {
            "id": "update-claude",
            "label": "update claude",
            "command": "brew upgrade --cask claude-code",
        },
        {
            "id": "show-tmux",
            "label": "show running tmux",
            "command": "tmux -L cloude ls",
        },
        {
            "id": "top",
            "label": "top",
            "command": _preferred_top_command(),
        },
    ]


def find_terminal_command(
    commands: List[TerminalCommand], command_id: str
) -> Optional[TerminalCommand]:
    """Look up a terminal command by id.

    Inputs: commands (list[TerminalCommand]); command_id (str).
    Output: TerminalCommand | None - None when the id is unknown, which
      every caller MUST treat as "launch a plain console, run nothing"
      rather than as an error worth failing the launch over.
    """
    for c in commands:
        if c.id == command_id:
            return c
    return None


def validate_command_list(raw: List[dict]) -> List[dict]:
    """Validate a whole replacement list before it can reach disk.

    Description: enforces per-entry schema (via ``TerminalCommand``),
      uniqueness of ids, and the list-size cap. Whole-list replace is the
      only write shape, so this is the only validation gate needed for
      add / edit / delete / reorder alike.
    Inputs: raw (list[dict]) - client-supplied entries, in display order.
    Output: list[dict] - normalized, model-validated dicts in the same order.
    Raises: ValueError - empty-able but malformed input: bad field, bad id
      charset, duplicate id, or more than MAX_TERMINAL_COMMANDS entries.
    Example: validate_command_list([{"id": "top", "label": "top",
      "command": "htop"}]) -> [{"id": "top", ...}]
    """
    if len(raw) > MAX_TERMINAL_COMMANDS:
        raise ValueError(f"at most {MAX_TERMINAL_COMMANDS} terminal commands are allowed")
    validated: List[dict] = []
    seen: set = set()
    for entry in raw:
        model = TerminalCommand(**entry)
        if model.id in seen:
            raise ValueError(f"duplicate terminal command id '{model.id}'")
        seen.add(model.id)
        validated.append(model.model_dump())
    return validated


def replace_terminal_commands(config_path: Path, raw: List[dict]) -> List[dict]:
    """Persist a whole new terminal-command list to config.json.

    Description: validates first (nothing malformed ever reaches disk),
      backs the pre-write bytes up to ``config.json.bak`` and writes via
      tmp-file + fsync + os.replace - the same one-generation backup and
      atomic-write convention ``Settings.update_settings_config`` and
      ``Settings._write_wrappers`` already use. Whole-list replace covers
      add, edit, delete AND reorder with one code path.
    Inputs:
      config_path (Path) - path to config.json.
      raw (list[dict]) - the complete new list, in display order.
    Output: list[dict] - the validated, persisted list.
    Raises:
      FileNotFoundError: config_path does not exist.
      ValueError: invalid JSON on disk, or a malformed entry (see
        ``validate_command_list``).
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Auth config file not found: {config_path}")

    validated = validate_command_list(raw)

    with open(config_path) as f:
        existing_raw = f.read()
    try:
        data = json.loads(existing_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {config_path}: {e}")

    data[TERMINAL_COMMANDS_KEY] = validated

    try:
        backup_path = config_path.with_suffix(config_path.suffix + ".bak")
        backup_path.write_text(existing_raw)
    except OSError as e:
        logger.warning("terminal_commands_backup_failed", error=str(e))

    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, config_path)

    logger.info("terminal_commands_replaced", count=len(validated))
    return validated
