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

Why keyed on the INSTANCE, not on the session_id and not on the tmux name
alone: a session_id dies on detach/destroy/restart, so it is too short-
lived to carry a durable flag; but a tmux NAME is too long-lived, because
names are reused and a flag set on a killed session reappeared on the next
one to take its name. The key is therefore ``<tmux_name>@<created_epoch>``,
the same ``(socket, name, #{session_created})`` identity rule
``sessions.tmux_created_epoch`` uses throughout ``src/core`` - reused, not
re-invented. See ``compose_key`` for what an unmeasurable epoch does, and
why it degrades to the legacy bare-name key rather than to a new one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

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

    @staticmethod
    def compose_key(tmux_name: str, epoch: Optional[int]) -> str:
        """The storage key for one session instance.

        Description: THE KEY IS THE INSTANCE, NOT THE NAME. tmux names are
            reused - kill ``cloude_Infrastructure`` and make another one
            and the new session answers to the same string. Keyed by name
            alone, an unread flag set on the old conversation reappeared on
            the new one, pointing the user at a Stop that happened in a
            session that no longer exists. ``#{session_created}`` is what
            separates the two, and it is the same identity rule
            ``sessions.tmux_created_epoch`` uses everywhere else in this
            codebase, reused rather than re-invented.

            A restart-in-place does NOT move the epoch (measured on tmux
            3.7c: ``respawn-pane -k`` replaces the pane's PROCESS, while
            ``#{session_created}`` belongs to the SESSION), so the flag
            survives exactly the operation the user thinks of as "restart"
            and is dropped exactly when the session is genuinely a new one.

            A ``None`` epoch means the probe could not answer, and that is
            NOT a new instance - it is an unknown one. It composes to the
            bare name, which is also the legacy key shape, so an
            unmeasurable epoch degrades to the old behaviour instead of
            silently minting a second entry for a session that already has
            one.
        Inputs:
            tmux_name: literal tmux session name.
            epoch: ``#{session_created}`` for that instance, or None when
                it could not be measured.
        Output: str - the dict key.
        Example:
            >>> UnreadStore.compose_key("cloude_a", 1757000000)
            'cloude_a@1757000000'
            >>> UnreadStore.compose_key("cloude_a", None)
            'cloude_a'
        """
        return f"{tmux_name}@{int(epoch)}" if epoch is not None else tmux_name

    @staticmethod
    def name_of(key: str) -> str:
        """Recover the tmux name from a stored key, composite or legacy.

        Description: ``rsplit`` on the LAST ``@`` and only when the tail is
            all digits, because a tmux name may legitimately contain an
            ``@`` and must not be truncated at it.
        Inputs: key (str) - a stored dict key.
        Output: str - the tmux name.
        Example:
            >>> UnreadStore.name_of("cloude_a@17")
            'cloude_a'
            >>> UnreadStore.name_of("user@host")
            'user@host'
        """
        head, sep, tail = key.rpartition("@")
        if sep and tail.isdigit():
            return head
        return key

    def _lookup(self, tmux_name: str, epoch: Optional[int]) -> Optional[dict]:
        """The entry for this instance, preferring the composite key.

        Falls back to the legacy bare-name entry so a store written before
        the re-key still answers. Returns None when neither is present.
        """
        entry = self._data.get(self.compose_key(tmux_name, epoch))
        if entry is not None:
            return entry
        return self._data.get(tmux_name)

    def is_unread(self, tmux_name: str | None, epoch: Optional[int] = None) -> bool:
        """True iff this session instance carries an auto or manual flag.

        Inputs:
            tmux_name: literal tmux session name. Falsy is False.
            epoch: ``#{session_created}``, or None when unmeasured.
        Output: bool.
        Example:
            >>> store.is_unread("cloude_myproj", 1757000000)
            False
        """
        if not tmux_name:
            return False
        entry = self._lookup(tmux_name, epoch)
        if not entry:
            return False
        return bool(entry.get("auto")) or bool(entry.get("manual"))

    def set_flag(
        self,
        tmux_name: str | None,
        field_name: str,
        value: bool,
        epoch: Optional[int] = None,
    ) -> None:
        """Set one sub-flag ("auto" or "manual") for one instance, and persist.

        Description: Idempotent - writing the same value twice reaches the
            same file. Drops the entry once both sub-flags are False so the
            file never accumulates dead
            ``{"auto": false, "manual": false}`` rows.

            MIGRATES ON WRITE. When a legacy bare-name entry exists and an
            epoch is now known, the entry moves to the composite key rather
            than a second one being created alongside it. Doing it on write
            rather than in a startup pass means a flag is never migrated
            onto an epoch that was only guessed: the write is the moment we
            hold a measured epoch for that exact session.
        Inputs:
            tmux_name: literal tmux session name. Falsy is a no-op
                (nothing to key the flag on).
            field_name: "auto" or "manual".
            value: new value for that sub-flag.
            epoch: ``#{session_created}``, or None when unmeasured.
        Output: None (persisted immediately).
        """
        if not tmux_name:
            return
        key = self.compose_key(tmux_name, epoch)
        entry = self._lookup(tmux_name, epoch) or {"auto": False, "manual": False}
        entry[field_name] = bool(value)
        if key != tmux_name:
            # Composite key in play: retire any legacy bare-name entry so
            # the same session cannot be represented twice.
            self._data.pop(tmux_name, None)
        if entry["auto"] or entry["manual"]:
            self._data[key] = entry
        else:
            self._data.pop(key, None)
        self._save()

    def prune(self, alive_names: set[str]) -> None:
        """Drop every entry whose tmux name is not in ``alive_names``.

        Called at startup reconciliation (mirrors the pinned-themes
        pruner) so a tmux session killed outside the UI
        (``tmux -L cloude kill-session``) doesn't leave a permanent
        unread badge nothing can ever clear.
        """
        # Compare on the NAME half of the key: a composite key
        # ``cloude_a@17`` is alive iff ``cloude_a`` is in the live listing.
        # Pruning on the raw key would delete every composite entry on the
        # first pass, silently wiping the whole store.
        dead = {
            key for key in self._data if self.name_of(key) not in alive_names
        }
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
