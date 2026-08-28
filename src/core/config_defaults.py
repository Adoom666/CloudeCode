"""The shipped defaults for ``config.json``, derived from the Python model.

WHY THIS MODULE EXISTS

``config.example.json`` is not the shipped defaults. It is a hand-maintained
sample of them, and it goes stale the moment somebody adds a field to the
model without remembering to add it to the example. Two keys had already
drifted out of it by 2026-08-20:

    terminal_commands   - read by Settings.load_auth_config at src/config.py,
                          with a real default factory in
                          src/core/terminal_commands.py
    config_version      - a declared AuthConfig field, read by the loader

The upgrade merge derived "what upstream ships" from the example file, so it
saw both keys in the user's config, failed to find them in the example, and
reported ``REMOVED UPSTREAM`` for two settings that are fully live and
supported. That is a confident verdict about something the tool never
measured, which is the exact false-green class this codebase exists to kill.

The fix is to move the source of truth to where the defaults actually live -
the model - and treat the example file as what it is: a curated presentation
layer that may legitimately lag.

WHAT "AUTHORITATIVE" MEANS HERE

A key is supported if ``Settings.load_auth_config`` reads it. That is the only
definition that matters, because that function IS the config contract. This
module lists those keys together with the default each one resolves to when
absent. Every scalar value is the same constant ``AuthConfig`` uses for its own
field default (``src.core.auth_defaults``) and every structured value comes
from the default factory the loader itself calls, so no number is written down
twice and none of them can drift apart.

``tests/test_config_defaults_cover_loader.py`` reads ``src/config.py`` and
fails if the loader gained a ``data.get("...")`` key that this table does not
declare, so the drift that produced the original defect cannot recur silently.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

It does not replace ``config.example.json``, and it does not outrank it. The
example carries prose ``_comment_*`` keys and a curated slash-command list that
have no model representation at all, so wherever the example speaks, it wins.
These defaults fill in only where the example has gone stale, and only for a
key the user's own file already has - see ``effective_shipped_defaults`` for
why that restriction matters.

"""

from __future__ import annotations

import copy
from typing import Any

from src.core import auth_defaults
from src.core.terminal_commands import (
    TERMINAL_COMMANDS_KEY,
    default_terminal_commands,
)

#: Top-level keys the loader reads which carry no default of their own: their
#: absence is a legitimate, fully-determined state rather than a missing
#: setting. ``template_path`` is Optional[str] = None in the model; writing an
#: explicit null into the defaults would make "unset" indistinguishable from
#: "set to nothing", so it is declared here and omitted from the defaults
#: mapping instead.
OPTIONAL_KEYS: frozenset[str] = frozenset({"template_path"})


def shipped_defaults() -> dict[str, Any]:
    """Build the shipped defaults for ``config.json`` from the model.

    Every value is read from the model's own declaration - the constants in
    ``src.core.auth_defaults`` that ``AuthConfig`` itself uses for its field
    defaults, and the default factories the loader calls - so changing a
    default in one place cannot leave this table describing the old one.

    Returns:
        A fresh mapping of top-level config key to its shipped default value.
        Mutating the result cannot affect a later call.

    Example:
        >>> "terminal_commands" in shipped_defaults()
        True
    """
    return {
        "config_version": auth_defaults.CONFIG_VERSION,
        "jwt_expiry_minutes": auth_defaults.JWT_EXPIRY_MINUTES,
        "access_token_ttl_seconds": auth_defaults.ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token_ttl_seconds": auth_defaults.REFRESH_TOKEN_TTL_SECONDS,
        "refresh_grace_seconds": auth_defaults.REFRESH_GRACE_SECONDS,
        TERMINAL_COMMANDS_KEY: default_terminal_commands(),
        "common_slash_commands": [],
        "projects": [],
        "session": {},
        "auth_rate_limits": {},
        "notifications": {},
        "agents": {},
        "uploads": {},
        "providers": {},
    }


def supported_keys() -> frozenset[str]:
    """Every top-level key the config loader honours.

    A key in this set is supported by the running code whether or not
    ``config.example.json`` happens to mention it. The merge uses this to tell
    "upstream removed this setting" apart from "the example file is stale",
    which are opposite conclusions the old code collapsed into one.

    Returns:
        The union of keys carrying a default and keys that are legitimately
        optional.
    """
    return frozenset(shipped_defaults()) | OPTIONAL_KEYS


def effective_shipped_defaults(
    example: dict | None,
    mine: dict | None = None,
) -> dict[str, Any]:
    """The defaults the merge should compare against.

    Starts from the curated example file, because its values ARE the shipped
    presentation of the defaults - the prose ``_comment_*`` keys and the
    shipped slash-command list have no model representation at all, so the
    example must win wherever it speaks.

    Then, and only then, fills in a model default for a key the user actually
    HAS that the example has gone stale on. That restriction is deliberate:
    materialising every model default into everyone's ``config.json`` would
    rewrite files to add settings nobody asked about. The merge needs the
    model here to answer one question - "is this key still supported" - not to
    expand the file.

    Args:
        example: Parsed ``config.example.json``, or None when absent.
        mine: The user's live configuration, or None. Only its key set is
            consulted.

    Returns:
        A new mapping. Never shares structure with either argument.

    Example:
        >>> eff = effective_shipped_defaults({"a": 1}, {"a": 9, "config_version": 3})
        >>> eff["a"], eff["config_version"]
        (1, 0)
    """
    combined: dict[str, Any] = copy.deepcopy(example) if example else {}
    if mine:
        model = shipped_defaults()
        for key in mine:
            if key not in combined and key in model:
                combined[key] = copy.deepcopy(model[key])
    return combined
