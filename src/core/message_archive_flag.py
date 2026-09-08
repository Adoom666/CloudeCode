"""The one switch that decides whether the message archive exists at all.

WHY THIS IS A SWITCH AND NOT A PREFERENCE. The message archive is not a
panel that can be hidden. Turning it on creates a schema, starts a
background thread that walks ``~/.claude/projects`` every fifteen
minutes, and indexes the contents of the user's own conversations into a
database on their disk. Nobody gets that by upgrading. So the whole
subsystem hangs off ONE resolved answer, read here, and every one of the
four surfaces that could leak it - the schema migration, the ingest
scheduler, the API routes and the UI entry points - asks this module
rather than deciding for itself. Four gates that each derive their own
answer are four gates that can disagree, and a disagreement in this
direction is a background indexer running for someone who never asked.

THREE OUTCOMES, NOT TWO. ``enabled`` and ``disabled`` are the answers you
get when something definite was read. ``cannot_determine`` is what you
get when nothing could be: a config file that will not parse, a block
that is not an object, an env override set to a word this module does not
recognise. It is NOT folded into either of the other two, because a user
who turned the feature ON and hit a broken config must not see the same
thing as a user who left it off - that is the exact false-green this
codebase's three-outcome rule exists to stop.

CANNOT DETERMINE GATES CLOSED, AND SAYS SO. Every consumer treats
``cannot_determine`` as "do not run", because the cost of guessing wrong
in the permissive direction is indexing someone's private transcripts
without consent, and the cost of guessing wrong in the restrictive
direction is a feature that does not appear until a config is fixed. The
state travels with the refusal - :func:`resolve` returns the reason, the
``/api/v1/features`` route publishes it, and the UI reports it - so it is
a NAMED refusal rather than a silent one.

ABSENT MEANS OFF, INCLUDING ON AN INSTALL THAT ALREADY HAS THE TABLES.
An existing install whose database already carries ``message_*`` tables
and whose ``config.json`` has no ``message_archive`` block resolves to
DISABLED. That is a decision, not an accident: "this install has the
tables" is evidence of a past upgrade, never evidence of consent. Off is
DORMANT, never destructive - nothing drops, alters or truncates those
tables, so adding ``{"message_archive": {"enabled": true}}`` and
restarting brings the install back exactly as it was.

PRECEDENCE: env, then config file, then the default. The env override
exists for the same reason ``CLOUDE_CORPUS_INGEST`` does - the test suite
and a one-off ops run need to say so without editing a user's config -
and it wins because an operator typing it at the command line is a more
recent statement of intent than a file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: Env override. ``1/true/on/yes`` forces the feature on, ``0/false/off/no``
#: forces it off. Any OTHER value resolves to ``cannot_determine`` rather
#: than falling through to a default - a kill switch nobody can read is not
#: a kill switch, and on a consent-bearing feature it must not fail open.
ENABLE_ENV = "CLOUDE_MESSAGE_ARCHIVE"

#: The ``config.json`` block and the boolean inside it.
CONFIG_KEY = "message_archive"
CONFIG_ENABLED_FIELD = "enabled"

#: What an install that has never heard of this feature gets. This value
#: is asserted directly by tests/test_message_archive_flag.py so that a
#: future edit cannot flip the whole subsystem on for every existing user
#: by changing one literal.
DEFAULT_ENABLED = False

STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_CANNOT_DETERMINE = "cannot_determine"

SOURCE_ENV = "env"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "default"

_TRUTHY = frozenset({"1", "true", "on", "yes"})
_FALSEY = frozenset({"0", "false", "off", "no"})


@dataclass(frozen=True)
class MessageArchiveFlag:
    """The resolved answer, with the evidence that produced it.

    Description: an immutable record of what was read and where from, so
      a caller can report WHY the archive is off rather than only that it
      is. ``enabled`` is deliberately a derived property and not a stored
      bool: storing both a tri-state and a bool invites the two to
      disagree, and the bool is the one that would win silently.
    Inputs: state (str) - one of ``STATE_ENABLED``, ``STATE_DISABLED``,
      ``STATE_CANNOT_DETERMINE``. source (str) - ``SOURCE_ENV``,
      ``SOURCE_CONFIG`` or ``SOURCE_DEFAULT``. reason (str) - one
      sentence naming what was read.
    Output: n/a.
    Example: MessageArchiveFlag(STATE_DISABLED, SOURCE_DEFAULT, "...")
    """

    state: str
    source: str
    reason: str

    @property
    def enabled(self) -> bool:
        """Report whether the subsystem may run.

        Description: ONLY ``enabled`` is permission. ``cannot_determine``
          returns False on purpose - see the module docstring.
        Inputs: none.
        Output: bool.
        Example: resolve().enabled -> False
        """
        return self.state == STATE_ENABLED


def _resolve_env(raw: str) -> MessageArchiveFlag:
    """Turn a raw env value into a resolution.

    Inputs: raw (str) - the unstripped environment value.
    Output: MessageArchiveFlag.
    Example: _resolve_env("1").state -> "enabled"
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return MessageArchiveFlag(
            STATE_ENABLED, SOURCE_ENV,
            f"{ENABLE_ENV}={raw!r} switches the message archive on.",
        )
    if value in _FALSEY:
        return MessageArchiveFlag(
            STATE_DISABLED, SOURCE_ENV,
            f"{ENABLE_ENV}={raw!r} switches the message archive off.",
        )
    return MessageArchiveFlag(
        STATE_CANNOT_DETERMINE, SOURCE_ENV,
        f"{ENABLE_ENV}={raw!r} is not a value this build recognises "
        f"(expected one of {sorted(_TRUTHY | _FALSEY)}); the message "
        "archive stays off until it is corrected.",
    )


def _config_path() -> Optional[Path]:
    """Locate ``config.json`` without importing :mod:`src.config` at module load.

    Description: :mod:`src.config` imports from :mod:`src.core`, so a
      module-level import of it here would close an import cycle and
      break every consumer in ``src/core``. The import is therefore made
      inside the call, which is also the only place its cost is paid.
      SystemExit IS CAUGHT ON PURPOSE. :mod:`src.config` prints a
      configuration banner and calls ``sys.exit`` when ``.env`` is
      missing or incomplete. Reading a feature switch must never be able
      to terminate the process that asked, so the one call that can do
      that is wrapped. A caller that genuinely needs a working
      :mod:`src.config` will hit its own failure at its own time.
    Inputs: none.
    Output: Path when the settings object could be built, None when it
      could not - which is reported as ``cannot_determine``, never as a
      default.
    Example: _config_path() -> PosixPath('/Users/x/.../config.json')
    """
    try:
        from src.config import settings
    except (ImportError, SystemExit):
        return None
    raw = getattr(settings, "auth_config_file", None)
    if not raw:
        return None
    return Path(raw).expanduser()


def _resolve_block(block: Any, path: Path) -> MessageArchiveFlag:
    """Turn the ``message_archive`` block into a resolution.

    Inputs: block (Any) - whatever the JSON held under ``CONFIG_KEY``,
      including None when the key was absent. path (Path) - the file it
      came from, named in every reason string.
    Output: MessageArchiveFlag.
    Example: _resolve_block({"enabled": True}, p).state -> "enabled"
    """
    if block is None:
        return MessageArchiveFlag(
            STATE_DISABLED, SOURCE_DEFAULT,
            f"no {CONFIG_KEY!r} block in {path}; the message archive is "
            "off by default. An install that already has message tables "
            "keeps them untouched - off is dormant, not destructive.",
        )
    if not isinstance(block, dict):
        return MessageArchiveFlag(
            STATE_CANNOT_DETERMINE, SOURCE_CONFIG,
            f"{CONFIG_KEY!r} in {path} is a {type(block).__name__}, not an "
            "object; the message archive stays off until it is corrected.",
        )
    if CONFIG_ENABLED_FIELD not in block:
        return MessageArchiveFlag(
            STATE_DISABLED, SOURCE_DEFAULT,
            f"{CONFIG_KEY}.{CONFIG_ENABLED_FIELD} is absent from {path}; "
            "the message archive is off by default.",
        )
    value = block[CONFIG_ENABLED_FIELD]
    if not isinstance(value, bool):
        return MessageArchiveFlag(
            STATE_CANNOT_DETERMINE, SOURCE_CONFIG,
            f"{CONFIG_KEY}.{CONFIG_ENABLED_FIELD} in {path} is "
            f"{value!r}, not true or false; the message archive stays off "
            "until it is corrected.",
        )
    if value:
        return MessageArchiveFlag(
            STATE_ENABLED, SOURCE_CONFIG,
            f"{CONFIG_KEY}.{CONFIG_ENABLED_FIELD} is true in {path}.",
        )
    return MessageArchiveFlag(
        STATE_DISABLED, SOURCE_CONFIG,
        f"{CONFIG_KEY}.{CONFIG_ENABLED_FIELD} is false in {path}.",
    )


def resolve(config_path: Optional[Path] = None) -> MessageArchiveFlag:
    """Resolve the message-archive switch, with its evidence.

    Description: env override first, then the ``message_archive`` block
      in ``config.json``, then the off-by-default. Reading is not cached:
      the three callers are a process-import, a startup migration and a
      lifespan, all of which run once, and a cache here would make an
      env change invisible to a test in a way that is far more expensive
      than re-reading a small JSON file three times.
    Inputs: config_path (Path | None) - override for tests; None resolves
      it from :class:`src.config.Settings`.
    Output: MessageArchiveFlag.
    Raises: nothing. Every failure resolves to ``cannot_determine``.
    Example: resolve().state -> "disabled"
    """
    raw_env = os.environ.get(ENABLE_ENV)
    if raw_env is not None:
        return _resolve_env(raw_env)

    path = config_path if config_path is not None else _config_path()
    if path is None:
        return MessageArchiveFlag(
            STATE_CANNOT_DETERMINE, SOURCE_CONFIG,
            "the location of config.json could not be resolved, so the "
            "message archive switch could not be read; it stays off.",
        )
    if not path.exists():
        return MessageArchiveFlag(
            STATE_DISABLED, SOURCE_DEFAULT,
            f"{path} does not exist, so this install has never opted in; "
            "the message archive is off by default.",
        )
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return MessageArchiveFlag(
            STATE_CANNOT_DETERMINE, SOURCE_CONFIG,
            f"{path} could not be read ({type(exc).__name__}: {exc}), so "
            "the message archive switch could not be measured; it stays off.",
        )
    if not isinstance(data, dict):
        return MessageArchiveFlag(
            STATE_CANNOT_DETERMINE, SOURCE_CONFIG,
            f"{path} does not contain a JSON object, so the message "
            "archive switch could not be measured; it stays off.",
        )
    return _resolve_block(data.get(CONFIG_KEY), path)


def message_archive_enabled(config_path: Optional[Path] = None) -> bool:
    """Report whether the message archive may run at all.

    Description: the one-line form of :func:`resolve` for gates that only
      need permission. Anything that REPORTS the state to a human should
      call :func:`resolve` instead and publish the reason, because "off"
      and "could not tell" are different messages.
    Inputs: config_path (Path | None) - see :func:`resolve`.
    Output: bool - True only for ``STATE_ENABLED``.
    Example: message_archive_enabled() -> False
    """
    return resolve(config_path).enabled
