"""User-chosen favorites for the slash-command chip row.

``common_slash_commands`` used to be a HAND-PICKED list: whoever edited
config.json decided which commands got a chip, and a user who wanted a
different set had to hand-edit JSON. This module turns the same key into
the storage behind a STAR the user toggles on any command in the palette.
Same key, same two entry forms, same migration chain. What changes is who
writes it.

THE STATE THAT HAS TO BE DISTINGUISHED, and why it needs the raw file
---------------------------------------------------------------------
There are THREE states, and collapsing the last two is the whole trap:

1. the key is ABSENT from config.json  -> the user has never starred
   anything, so show the built-in defaults;
2. the key holds entries               -> those are the favorites;
3. the key holds an EMPTY LIST         -> the user starred and then
   unstarred everything. That is a CHOICE, and re-seeding it with the
   defaults would silently undo it. An empty chip row is the correct
   answer, with an empty state that says so.

``AuthConfig.common_slash_commands`` defaults to ``[]``, so by the time
config.json has been parsed into that model, states 1 and 3 are the same
value and are no longer tellable apart. So every read here goes to the
FILE, where key presence still exists. That also removes this endpoint
from the ``_auth_config_cache`` staleness question entirely, which is why
nothing in src/config.py had to change.

BACKWARD COMPATIBILITY
----------------------
Every existing entry is passed through BYTE FOR BYTE on a write, in its
original form. A user's hand-authored
``{"command": "/deploy", "description": "ship it"}`` survives starring
something else, and the historical bare-string form is what a newly
starred command is appended as, exactly as
``slash_command_labels.append_missing_commands`` has always done. Nothing
here rewrites an entry it did not have to touch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import structlog

from src.core import slash_command_labels

logger = structlog.get_logger()

#: Config.json key holding the favorites. Unchanged from when the same
#: key held a hand-picked list; renaming it would orphan every config.
FAVORITES_KEY = "common_slash_commands"

#: Upper bound on the starred set. Not a security control - a guard
#: against a runaway client writing an unbounded list into config.json,
#: and a soft statement that a chip row of 60 items is not a quick-access
#: affordance any more.
MAX_FAVORITES = 40


class FavoritesError(ValueError):
    """A favorites operation could not be applied.

    Raised for a blank command and for exceeding ``MAX_FAVORITES``.
    Distinct from a JSON/IO failure so the route can answer 400 rather
    than 500.
    """


def read_raw(config_path: Path) -> Tuple[Any, bool]:
    """Read the favorites value out of config.json, preserving key presence.

    Description: the file, not ``AuthConfig``, because the parsed model
      cannot tell an absent key from an empty list. See the module
      docstring.
    Inputs: config_path (Path) - path to config.json.
    Output: (Any, bool) - the raw stored value (``[]`` when absent), and
      whether the key was DECLARED in the file at all.
    Raises:
      FileNotFoundError: config_path does not exist.
      ValueError: invalid JSON on disk.
    Example: read_raw(p) -> (["/clear"], True)
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Auth config file not found: {config_path}")
    try:
        with open(config_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {config_path}: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"Invalid config shape in {config_path}: expected an object")
    return data.get(FAVORITES_KEY, []), FAVORITES_KEY in data


def resolve(raw: Any, declared: bool) -> List[Dict[str, str]]:
    """Turn the stored value into the chip list the client renders.

    Description: an UNDECLARED key means "never starred anything", which
      gets the built-in defaults so a fresh install has a useful row
      instead of an empty bar. A DECLARED key is authoritative even when
      it is empty, because an empty declared list is a choice the user
      made by unstarring.
    Inputs:
      raw (Any) - the stored value from ``read_raw``.
      declared (bool) - whether the key exists in config.json.
    Output: list[dict] - ``{"command", "description"}`` in display order.
    Example: resolve([], False) -> the ten built-in defaults
    """
    if not declared:
        return slash_command_labels.normalize(
            slash_command_labels.DEFAULT_COMMON_COMMANDS
        )
    return slash_command_labels.normalize(raw if isinstance(raw, list) else [])


def is_favorite(raw: Any, declared: bool, command: str) -> bool:
    """Whether a command is currently starred.

    Inputs: raw (Any); declared (bool); command (str) - with or without a
      leading slash.
    Output: bool.
    Example: is_favorite(["/clear"], True, "clear") -> True
    """
    key = slash_command_labels.entry_command(command)
    if not key:
        return False
    return any(d["command"] == key for d in resolve(raw, declared))


def toggle(raw: Any, declared: bool, command: str, favorite: bool) -> List[Any]:
    """Star or unstar one command, returning the new RAW list to persist.

    Description: when the key was never declared, the built-in defaults
      are MATERIALIZED first. Without that, unstarring a default would
      write a list that still contained it (there was nothing to remove
      from), and the chip would come straight back - a toggle that
      silently does nothing is worse than one that refuses. Existing
      entries are copied through unchanged, in their original form; a
      newly starred command is appended as a bare string, the historical
      form, so the built-in description table supplies its label.
    Inputs:
      raw (Any) - stored value from ``read_raw``.
      declared (bool) - whether the key exists in config.json.
      command (str) - the command to toggle, with or without a slash.
      favorite (bool) - True to star, False to unstar.
    Output: list - the complete new value for ``FAVORITES_KEY``.
    Raises: FavoritesError - blank command, or starring past MAX_FAVORITES.
    Example: toggle(["/clear"], True, "/diff", True) -> ["/clear", "/diff"]
    """
    key = slash_command_labels.entry_command(command)
    if not key:
        raise FavoritesError("command must not be blank")

    if declared:
        existing = list(raw) if isinstance(raw, list) else []
    else:
        existing = list(slash_command_labels.DEFAULT_COMMON_COMMANDS)

    if favorite:
        out = slash_command_labels.append_missing_commands(existing, [key])
        if len(out) > MAX_FAVORITES:
            raise FavoritesError(
                f"too many favorites: {len(out)} exceeds the limit of {MAX_FAVORITES}"
            )
        return out

    return [
        entry for entry in existing
        if slash_command_labels.entry_command(entry) != key
    ]


def write(config_path: Path, entries: List[Any]) -> None:
    """Persist a new favorites list to config.json.

    Description: backs the pre-write bytes up to ``config.json.bak`` and
      writes via tmp-file + fsync + os.replace - the same one-generation
      backup and atomic-write convention
      ``terminal_commands.replace_terminal_commands`` and
      ``Settings._write_wrappers`` already use, so a crash mid-write can
      never leave a truncated config.
      NORMALIZED ON EVERY SAVE, NOT ONCE BY A MIGRATION. Entries go
      through ``slash_command_labels.storage_form``, which emits a BARE
      STRING for any entry with no real description and keeps the object
      form only where a description actually exists. One object-form
      entry makes v0.8.1's ``load_auth_config`` raise and the server exit
      at startup, so an object carrying an empty description breaks a
      downgrade in exchange for nothing. A one-time migration would not
      hold: this is the path that WRITES the shape, so it is the path
      that has to keep it narrow. A real description is preserved
      verbatim - see ``storage_form`` for why that is deliberate.
    Inputs: config_path (Path); entries (list) - the complete new value.
    Output: None.
    Raises:
      FileNotFoundError: config_path does not exist.
      ValueError: invalid JSON on disk.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Auth config file not found: {config_path}")

    entries = slash_command_labels.storage_form(entries)

    with open(config_path) as f:
        existing_raw = f.read()
    try:
        data = json.loads(existing_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {config_path}: {e}")

    data[FAVORITES_KEY] = entries

    try:
        backup_path = config_path.with_suffix(config_path.suffix + ".bak")
        backup_path.write_text(existing_raw)
    except OSError as e:
        logger.warning("slash_favorites_backup_failed", error=str(e))

    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, config_path)

    logger.info("slash_favorites_written", count=len(entries))


def payload(config_path: Path) -> Dict[str, Any]:
    """Build the ``/config/common-commands`` response body.

    Description: the ONE place the response shape is assembled, so the
      GET and the toggle route can never drift. ``commands`` and
      ``command_details`` are unchanged from the original response, so a
      client written against the old shape keeps working; ``defaulted``
      is new and lets the client say "these are the defaults, star
      something to make them yours" rather than implying the user chose
      them.
    Inputs: config_path (Path) - path to config.json.
    Output: dict - ``commands``, ``command_details``, ``defaulted``.
    """
    raw, declared = read_raw(config_path)
    details = resolve(raw, declared)
    return {
        "commands": slash_command_labels.commands_only(details),
        "command_details": details,
        "defaulted": not declared,
    }
