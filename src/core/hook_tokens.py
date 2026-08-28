"""Durable per-session hook tokens, so a restart does not silence hooks.

THE DEFECT THIS EXISTS TO FIX, measured on the mini 2026-08-28. The
per-session HMAC token was minted at session-create and held ONLY in
``SessionManager._hook_tokens``, an in-memory dict. The same token is
baked into the tmux pane's environment at spawn, and the hook command
reads it from there at fire time - so it is fixed for the life of the
agent process and cannot be re-issued to a running session.

Restart the server and those two facts collide. The agent keeps posting
perfectly well-formed hooks, forever, to a server that has forgotten the
token, and every one is rejected:

    POST /api/v1/hooks/claude-event  ->  {"detail":"invalid token"}  403

Nothing about that is visible from the pane. And because everything
downstream of hooks dies at once - activity status, toasts, session
lineage - and the fallback tier reports a CONSTANT ``idle`` under this
app's launch path, a session whose hooks are dead is indistinguishable
from a healthy one sitting at a prompt.

WHY PERSISTING IS THE RIGHT FIX AND NOT A WEAKENING. The original design
kept the token in memory on purpose, so a compromised process could not
learn one. That property is mostly notional here: the token is ALREADY
readable by the same user, out of the pane's own environment
(``tmux show-environment``), which is where the agent reads it from. A
0600 file in the state dir adds no reader who could not already run that
command. What the memory-only design actually bought was "the token stops
being valid when the session ends", and that is preserved exactly - the
entry is dropped on destroy, and any entry whose session is gone is
garbage-collected on load.

The loopback check is untouched and remains the layer that matters
against anything off-box.

THREE OUTCOMES, because a token store that fails quietly recreates the
very bug it fixes. Load and save both report whether they worked; a
caller that cannot persist is told so and can degrade to memory-only
WITH the reason, rather than believing it is durable.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

#: Filename inside the state dir. Sits beside the other durable
#: per-install state rather than in the datastore, because it is
#: credential material with a different lifetime to any DB row.
TOKENS_FILENAME: str = "hook_tokens.json"

#: Owner read/write only. The tokens authenticate a loopback endpoint, so
#: the file must never be group- or world-readable even on a single-user
#: box - the cost of getting this wrong is silent and permanent.
_FILE_MODE = 0o600

#: Schema marker. A file this code cannot read is IGNORED, never guessed
#: at: a mangled token store must degrade to "no stored tokens", which
#: costs one restart's worth of hooks, rather than to a partially-parsed
#: one that authenticates some sessions and not others.
#:
#: v2 adds ``tmux_name`` beside each token, because surviving the restart
#: needs BOTH halves. The token gets the hook past authentication; the
#: tmux name is what lets the server work out WHICH session it belongs
#: to. Without the second, a restored token produces a hook that
#: authenticates, returns 200, and resolves to nothing - measured, and
#: indistinguishable from success from the agent's side.
#: v1 files (token-only) are still read: their entries authenticate and
#: carry no name, which is strictly better than discarding them.
_SCHEMA = 2
_SCHEMA_READABLE = (1, 2)

LOAD_OK = "ok"
LOAD_ABSENT = "absent"
LOAD_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class LoadResult:
    """What a load attempt actually achieved.

    ``status`` distinguishes the three cases that must never be
    collapsed: the file was read; there was no file yet (the normal state
    on a fresh install, and NOT an error); or the file exists and could
    not be used. The third is the one that silently becomes the original
    bug if it is reported as an empty store.
    """

    tokens: Dict[str, str]
    status: str
    detail: Optional[str] = None
    #: session_id -> tmux session name, for the sessions that recorded one
    #: (v2 entries). Empty for a v1 file, which is honest: those entries
    #: can authenticate but cannot be resolved to a session.
    tmux_names: Dict[str, str] = field(default_factory=dict)

    @property
    def durable(self) -> bool:
        """Whether the store can be trusted to have survived a restart."""
        return self.status in (LOAD_OK, LOAD_ABSENT)


def tokens_path(state_dir: Path) -> Path:
    """Where the token file lives for a given state dir."""
    return Path(state_dir) / TOKENS_FILENAME


def load_tokens(
    state_dir: Path, live_session_ids: Optional[Iterable[str]] = None
) -> LoadResult:
    """Read the stored tokens, dropping any whose session is gone.

    Description: ``live_session_ids``, when supplied, garbage-collects the
      file. A token whose session no longer exists can never be presented
      by anything, so keeping it is pure credential accumulation - the
      file would grow for the life of the install. Passing None keeps
      everything, which is correct before the session list is known.

      A file that cannot be parsed yields an EMPTY store with status
      ``unreadable`` rather than raising. That is a deliberate choice and
      the reason ``status`` exists: the caller must be able to tell "no
      tokens were stored" from "I could not read what was stored", and
      only the second one warrants a warning to the user.
    Inputs: state_dir (Path). live_session_ids (iterable[str] | None) -
      session ids currently known; entries outside it are dropped.
    Output: LoadResult.
    Example: load_tokens(Path('/tmp')).status -> 'absent'
    """
    path = tokens_path(state_dir)
    if not path.exists():
        return LoadResult(tokens={}, status=LOAD_ABSENT)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("hook_tokens_unreadable", error=str(exc))
        return LoadResult(tokens={}, status=LOAD_UNREADABLE, detail=str(exc))

    if not isinstance(raw, dict) or raw.get("schema") not in _SCHEMA_READABLE:
        return LoadResult(
            tokens={},
            status=LOAD_UNREADABLE,
            detail=f"unrecognised schema (want {_SCHEMA})",
        )
    stored = raw.get("tokens")
    if not isinstance(stored, dict):
        return LoadResult(
            tokens={}, status=LOAD_UNREADABLE, detail="tokens is not an object"
        )

    tokens: Dict[str, str] = {}
    names: Dict[str, str] = {}
    for k, v in stored.items():
        if not isinstance(k, str) or not k:
            continue
        if isinstance(v, str) and v:
            # v1 shape: a bare token, no name recorded.
            tokens[k] = v
        elif isinstance(v, dict):
            tok = v.get("token")
            nm = v.get("tmux_name")
            if isinstance(tok, str) and tok:
                tokens[k] = tok
            if isinstance(nm, str) and nm:
                names[k] = nm
    if live_session_ids is not None:
        live = set(live_session_ids)
        tokens = {k: v for k, v in tokens.items() if k in live}
        names = {k: v for k, v in names.items() if k in live}
    return LoadResult(tokens=tokens, status=LOAD_OK, tmux_names=names)


def save_tokens(
    state_dir: Path,
    tokens: Dict[str, str],
    tmux_names: Optional[Dict[str, str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Write the token map atomically, owner-only.

    Description: temp file in the same directory, fsync, ``os.replace``
      - the same shape every other durable write in this codebase uses,
      so a crash mid-write leaves the previous file intact rather than a
      truncated one. The mode is set on the TEMP file before the rename,
      because a chmod after the replace leaves a window in which the real
      file is world-readable.

      NEVER LOGS A TOKEN, including on failure.
    Inputs: state_dir (Path). tokens (dict[str, str]).
    Output: tuple[bool, str | None] - (wrote, reason-if-not).
    Example: save_tokens(Path('/tmp'), {'ses_a': 'tok'}) -> (True, None)
    """
    path = tokens_path(state_dir)
    names = tmux_names or {}
    entries = {
        sid: {"token": tok, "tmux_name": names.get(sid)}
        for sid, tok in tokens.items()
    }
    payload = json.dumps({"schema": _SCHEMA, "tokens": entries}, indent=2)
    tmp_path = None
    try:
        # DELIBERATELY DOES NOT CREATE THE DIRECTORY. `settings.get_state_dir()`
        # owns that and raises its own named error when it cannot; creating
        # it here would mean this function decides where state lives.
        #
        # It also caused real damage: under a test where `settings` is a
        # MagicMock, `get_state_dir()` returns a mock whose str() is
        # "MagicMock/settings.get_state_dir()", and mkdir(parents=True)
        # duly created that as a directory tree in the repo root. A write
        # helper that fabricates its own destination will eventually
        # fabricate the wrong one.
        if not path.parent.is_dir():
            return (False, f"state dir does not exist: {path.parent}")
        fd, tmp = tempfile.mkstemp(
            prefix=".hook_tokens.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp)
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _FILE_MODE)
        os.replace(tmp_path, path)
        return (True, None)
    except OSError as exc:
        logger.warning("hook_tokens_save_failed", error=str(exc))
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return (False, str(exc))
