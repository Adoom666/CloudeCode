"""Durable per-tmux-name read/unread store (feat/hook-driven-status).

Extracted into its own module rather than growing ``session_manager.py``
further (already over the project's 500-line guideline) - this is pure
disk I/O + a plain dict, with ZERO knowledge of sessions, backends, or
tmux. ``SessionManager`` owns one instance, resolves a session_id to a
tmux name itself (this module has no way to do that), and delegates the
actual flag storage here. Mirrors the on-disk shape and atomic-write
protocol ``SessionManager._save_pinned_themes`` already uses, so anyone
who has read that code recognizes this one immediately.

Why server-side, not localStorage: the user drives this from both a phone
browser and a desktop browser, and the unread flag must follow him.
Why name-keyed, not session-id-keyed: a session_id dies on
detach/destroy/restart; the tmux name is the one thing that survives
detach -> swap -> re-adopt, matching ``pinned_themes.json``'s convention.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

logger = structlog.get_logger()


class UnreadStore:
    """In-memory dict of ``{tmux_name: {"auto": bool, "manual": bool}}``,
    atomically persisted to a single JSON file.

    - "auto" is set by a ``Stop`` hook, cleared when a WS terminal binds
      to the session (the user looked at it) - see
      ``SessionManager.mark_session_viewed``.
    - "manual" is set/cleared ONLY by the user's explicit mark-unread
      control; viewing the session does NOT clear it.
    - A session is unread iff either sub-flag is True. An entry with both
      False is dropped rather than kept as a hygiene no-op row.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """Load from disk. Missing/malformed file = empty store; never raises."""
        if not self._path.exists():
            return
        try:
            with open(self._path, "r") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                logger.warning(
                    "unread_state_unexpected_shape", type=type(raw).__name__
                )
                return
            loaded: dict[str, dict] = {}
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                entry = {
                    "auto": bool(v.get("auto", False)),
                    "manual": bool(v.get("manual", False)),
                }
                if entry["auto"] or entry["manual"]:
                    loaded[str(k)] = entry
            self._data = loaded
            logger.info("unread_state_loaded", count=len(self._data))
        except Exception as exc:
            logger.warning("failed_to_load_unread_state", error=str(exc))

    def _save(self) -> None:
        """Persist atomically (write-to-tmp + rename). Never raises."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w") as f:
                json.dump(self._data, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(str(tmp), str(self._path))
        except Exception as exc:
            logger.error("failed_to_save_unread_state", error=str(exc))

    def is_unread(self, tmux_name: str | None) -> bool:
        """True iff ``tmux_name`` carries an auto or manual unread flag.

        Example:
            >>> store.is_unread("cloude_myproj")
            False
        """
        if not tmux_name:
            return False
        entry = self._data.get(tmux_name)
        if not entry:
            return False
        return bool(entry.get("auto")) or bool(entry.get("manual"))

    def set_flag(self, tmux_name: str | None, field_name: str, value: bool) -> None:
        """Set one sub-flag ("auto" or "manual") for ``tmux_name`` and persist.

        Drops the entry once both sub-flags are False so the file never
        accumulates dead ``{"auto": false, "manual": false}`` rows.

        Inputs:
            tmux_name: literal tmux session name. A falsy value is a no-op
                (nothing to key the flag on).
            field_name: "auto" or "manual".
            value: new value for that sub-flag.
        Output: None (persisted immediately).
        """
        if not tmux_name:
            return
        entry = self._data.get(tmux_name) or {"auto": False, "manual": False}
        entry[field_name] = bool(value)
        if entry["auto"] or entry["manual"]:
            self._data[tmux_name] = entry
        else:
            self._data.pop(tmux_name, None)
        self._save()

    def prune(self, alive_names: set[str]) -> None:
        """Drop every entry whose tmux name is not in ``alive_names``.

        Called at startup reconciliation (mirrors the pinned-themes
        pruner) so a tmux session killed outside the UI
        (``tmux -L cloude kill-session``) doesn't leave a permanent
        unread badge nothing can ever clear.
        """
        dead = {name for name in self._data if name not in alive_names}
        if not dead:
            return
        logger.info("unread_state_pruning_dead", names=sorted(dead))
        for name in dead:
            self._data.pop(name, None)
        self._save()

    @property
    def raw(self) -> dict[str, dict]:
        """Read-only-by-convention access to the underlying dict (tests)."""
        return self._data
