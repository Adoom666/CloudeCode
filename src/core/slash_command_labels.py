"""Short descriptions for the common-slash-command row.

WHY SHORT: the row renders as a grid of tappable chips. On a phone a chip
is roughly half the viewport wide, so a description only helps if it fits
on one line next to a command name that is already 6-10 characters. A
sentence wraps to three lines, the grid grows past the fold, and the row
stops being the quick-access affordance it exists to be. Every entry here
is capped at ``MAX_DESCRIPTION_LENGTH`` and that cap is enforced by test,
not by good intentions.

BACKWARD COMPATIBILITY: ``common_slash_commands`` in config.json has
always been a list of bare strings::

    "common_slash_commands": ["/clear", "/compact"]

That form still works and always will - ``normalize()`` fills the
description in from the table below. A user who wants their own wording
(or who added a command this table has never heard of) can write the
object form instead, mixed freely with plain strings::

    "common_slash_commands": [
      "/clear",
      {"command": "/deploy", "description": "ship it"}
    ]

The two forms are interchangeable per entry, so no existing config needs
touching and no migration runs.
"""

from typing import Any, Dict, List, Union

# Hard cap on a description's rendered length. Measured, not guessed: at
# 375 CSS px (the narrowest phone this is expected to run on) a chip is
# 47% wide, and at the 0.6rem mobile description size exactly 18
# characters render before the ellipsis starts eating words - checked in a
# browser at that width, not estimated. Past this the text truncates and
# carries less than the hover tooltip it replaced.
MAX_DESCRIPTION_LENGTH = 18

# Built-in table. Lowercase, no trailing period, verb-first where a verb
# reads naturally. Keys are the bare command including the leading slash.
SHORT_DESCRIPTIONS: Dict[str, str] = {
    "/add-dir": "add a working dir",
    "/agents": "manage subagents",
    "/bug": "report a bug",
    "/clear": "wipe conversation",
    "/compact": "summarize history",
    "/config": "open settings",
    "/context": "show context usage",
    "/cost": "session cost",
    "/diff": "review changes",
    "/doctor": "check the install",
    "/effort": "set effort level",
    "/export": "export transcript",
    "/help": "list commands",
    "/hooks": "configure hooks",
    "/init": "write CLAUDE.md",
    "/login": "sign in",
    "/logout": "sign out",
    "/mcp": "manage mcp servers",
    "/memory": "edit memory files",
    "/model": "switch model",
    "/output-style": "set output style",
    "/permissions": "manage permissions",
    "/plan": "plan before edits",
    "/pr-comments": "read pr comments",
    "/privacy-settings": "privacy settings",
    "/release-notes": "what changed",
    "/resume": "reopen a session",
    "/review": "review a pr",
    "/rewind": "undo a checkpoint",
    "/security-review": "review for vulns",
    "/status": "account status",
    "/statusline": "set status line",
    "/tasks": "background tasks",
    "/terminal-setup": "install keybinding",
    "/todos": "list current todos",
    "/upgrade": "change plan",
    "/usage": "token and cost",
    "/vim": "vim editing mode",
}

# The set the API falls back to when config.json declares none. Kept here
# rather than inline in the route so the defaults and their descriptions
# live in one file.
DEFAULT_COMMON_COMMANDS: List[str] = [
    "/agents",
    "/clear",
    "/compact",
    "/context",
    "/hooks",
    "/mcp",
    "/resume",
    "/rewind",
    "/usage",
    "/login",
]

# Commands appended to an EXISTING user's list by a config migration, in
# order. A user whose config.json already declares
# ``common_slash_commands`` never sees a change to DEFAULT_COMMON_COMMANDS
# above, because the config wins; the migration is the only way a new
# default reaches them. Keep this list in sync with the tail of
# DEFAULT_COMMON_COMMANDS.
MIGRATION_APPENDED_COMMANDS: List[str] = [
    "/login",
]


def describe(command: str) -> str:
    """Look up the built-in short description for a command.

    Inputs:
        command: bare command string, with or without a leading slash.
    Returns:
        The short description, or "" when the command is unknown. An
        unknown command is not an error: users add their own commands and
        the row must render them regardless.
    Example:
        >>> describe("/clear")
        'wipe conversation'
    """
    if not isinstance(command, str):
        return ""
    key = command.strip()
    if not key:
        return ""
    if not key.startswith("/"):
        key = "/" + key
    return SHORT_DESCRIPTIONS.get(key, "")


def normalize(
    raw: Union[List[Any], None],
) -> List[Dict[str, str]]:
    """Normalize a ``common_slash_commands`` config value.

    Accepts the historical list-of-strings form, the newer
    list-of-objects form, and any mix of the two. Entries that carry no
    usable command are dropped rather than rendered as an empty chip.

    Inputs:
        raw: the config value, or None.
    Returns:
        A list of ``{"command": str, "description": str}`` dicts, in the
        order given. A config-supplied description always wins over the
        built-in table so the file stays authoritative; the table only
        fills gaps.
    Example:
        >>> normalize(["/clear", {"command": "/x", "description": "mine"}])
        [{'command': '/clear', 'description': 'wipe conversation'},
         {'command': '/x', 'description': 'mine'}]
    """
    out: List[Dict[str, str]] = []
    for entry in raw or []:
        if isinstance(entry, str):
            command = entry.strip()
            description = describe(command)
        elif isinstance(entry, dict):
            command = str(entry.get("command", "")).strip()
            description = entry.get("description")
            if description is None:
                description = describe(command)
            description = str(description).strip()
        else:
            continue

        if not command:
            continue
        out.append({"command": command, "description": description})
    return out


def entry_command(entry: Any) -> str:
    """Read the command out of one raw ``common_slash_commands`` entry.

    Works on both supported entry forms without normalizing the whole
    list, so a caller that must PRESERVE the user's original entries
    (a config migration, say) can still tell what each one names.

    Inputs:
        entry: a bare string, or a ``{"command", "description"}`` dict.
    Returns:
        The command with a leading slash, or "" when the entry carries
        no usable command (including for types this format never had).
    Example:
        >>> entry_command({"command": "login"})
        '/login'
    """
    if isinstance(entry, str):
        command = entry.strip()
    elif isinstance(entry, dict):
        command = str(entry.get("command", "")).strip()
    else:
        return ""
    if not command:
        return ""
    return command if command.startswith("/") else "/" + command


def append_missing_commands(raw: Any, commands: List[str]) -> List[Any]:
    """Append commands a list does not already contain, preserving it.

    Purely additive: every existing entry is passed through byte-for-byte
    in its original form (string or object), so a user's own wording and
    ordering survive. Appends bare strings, which is the historical form
    and lets the built-in description table supply the label.

    Inputs:
        raw: the existing ``common_slash_commands`` value; a non-list is
            treated as an empty list.
        commands: commands to ensure are present, appended in order.
    Returns:
        A NEW list. Equal in content to the input when every command was
        already present, which makes repeated calls idempotent.
    Example:
        >>> append_missing_commands(["/clear"], ["/login"])
        ['/clear', '/login']
    """
    existing = list(raw) if isinstance(raw, list) else []
    present = {entry_command(e) for e in existing}
    out = list(existing)
    for command in commands:
        key = command if command.startswith("/") else "/" + command
        if key not in present:
            out.append(key)
            present.add(key)
    return out


def storage_form(raw: Any) -> List[Any]:
    """Render entries into the NARROWEST form that still carries their meaning.

    An entry whose description is empty, whitespace-only or absent
    becomes a BARE STRING. An entry with a real description keeps the
    ``{"command", "description"}`` object.

    WHY THIS EXISTS - a downgrade break, measured. ``AuthConfig
    .common_slash_commands`` is ``List[str]`` at v0.8.1 and
    ``List[Union[str, Dict[str, Any]]]`` on the new tip. ONE object-form
    entry makes the old version's ``load_auth_config`` raise a pydantic
    ValidationError and the server EXIT at startup - not a degraded mode,
    it does not start. An object entry carrying no description holds
    nothing the object form exists to hold, so writing it that way breaks
    the old version in exchange for nothing.

    WHAT THIS DELIBERATELY DOES NOT DO. It does not strip a real
    description to make a config downgrade-safe. That wording is the
    user's data; a config that uses descriptions stays downgrade-unsafe
    as a consequence of HIS choice. This narrows the blast radius to
    those configs, it does not eliminate it.

    Collapsing an empty-description object to a bare string is invisible
    to every reader, because ``normalize()`` fills a bare string's
    description in from the built-in table - which is where an empty one
    would have got its label anyway.

    Inputs:
        raw: the existing ``common_slash_commands`` value; a non-list is
            treated as an empty list. Entries carrying no usable command
            are dropped, matching ``normalize()``.
    Returns:
        A NEW list of bare strings and/or ``{"command", "description"}``
        dicts. Idempotent: passing its own output back returns an equal
        list.
    Example:
        >>> storage_form([{"command": "/x", "description": ""}, "/clear"])
        ['/x', '/clear']
    """
    out: List[Any] = []
    for entry in raw if isinstance(raw, list) else []:
        command = entry_command(entry)
        if not command:
            continue
        if isinstance(entry, dict):
            description = entry.get("description")
            description = "" if description is None else str(description).strip()
        else:
            description = ""
        if description:
            out.append({"command": command, "description": description})
        else:
            out.append(command)
    return out


def commands_only(details: List[Dict[str, str]]) -> List[str]:
    """Project normalized entries back down to bare command strings.

    Exists so the ``/config/common-commands`` response can keep its
    original ``commands`` key exactly as it was - a flat list of strings -
    while adding descriptions alongside it. Any client written against the
    old response shape keeps working untouched.

    Inputs:
        details: output of :func:`normalize`.
    Returns:
        List of command strings in the same order.
    """
    return [d["command"] for d in details]
