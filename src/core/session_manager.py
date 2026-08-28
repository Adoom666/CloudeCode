"""Session manager for Claude Code instances.

Multi-concurrent-session design: holds any number of live `SessionBackend`
instances, keyed by ``session_id`` - two browser tabs can each attach to a
different session and neither disconnects the other. Per-session state
(backend, output subscribers, log buffer, command count, idle watcher, adopt
FIFO offset) lives in dicts keyed by ``session_id``; global state
(``owned_tmux_sessions``, ``pinned_themes``, the notification router) stays
scalar. Backend type (tmux vs PTY) is selected per-session via
``build_backend(settings)`` which reads ``AuthConfig.session.backend``.

Back-compat shim: ``self.session`` / ``self.backend`` are read-only
properties resolving to ``current_session()`` / ``current_backend`` (the
most-recently-created session) so the handful of legacy single-session
callers in ``src/api`` keep working unchanged.
"""

import asyncio
import hmac
import json
import os
import re
import base64
import secrets
import shutil
import sqlite3
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime
from fastapi import HTTPException
import structlog

from src.config import settings
from src.models import (
    Session,
    SessionStatus,
    SessionInfo,
    SessionStats,
    LogEntry,
    Toast,
)
from src.core.workspace_settings import build_spawn_env
from src.core.session_backend import SessionBackend, build_backend
from src.core.tmux_backend import SESSION_PREFIX
from src.core.tmux_listing import TmuxListing, coerce_listing
from src.core.agent_family_display import resolve_family_for_display
from src.core.session_status import STATUS_UNKNOWN
from src.core.session_activity import (
    EVENT_STOP,
    SessionActivityTracker,
    map_tmux_fallback,
)
from src.core.unread_store import UnreadStore
from src.core.notifications.idle_watcher import IdleWatcher
from src.core.upload_sweeper import (
    SweepOutcome,
    UploadSweeper,
    datastore_project_paths,
    sweep_verdict,
)
from src.utils.pty_session import PTYSessionError
from src.utils.template_manager import copy_templates as copy_template_files

logger = structlog.get_logger()

# Bounded wait for a just-started pane to become addressable before the
# initial terminal command is typed into it (see
# ``SessionManager._type_initial_terminal_command``). tmux creates the
# session before window 0 exists, so the first write can legitimately fail
# for a few hundred ms. 20 x 0.1s = 2s ceiling: long enough for every
# observed start, short enough that a genuinely broken pane does not stall
# session creation.
_TERMINAL_COMMAND_WRITE_ATTEMPTS = 20
_TERMINAL_COMMAND_WRITE_DELAY_SECONDS = 0.1


# Characters that must never survive into a tmux session name.
#
# '.' and ':' are tmux's own pane and window separators. '|' is legal in a
# tmux name but is the DELIMITER of the listing format this app parses
# (src/core/tmux_listing_parse.LISTING_FORMAT), so a project called
# "api|prod" used to mint a name the app's own parser could not read back
# - no attacker required. The bounded split in that module makes such a
# name parseable now, so this is defence in depth rather than the primary
# guard, and it is kept because a name the app itself minted should never
# depend on a parser subtlety to be read correctly.
#
# Non-whitespace control characters go too. The whitespace ones (tab,
# newline, carriage return, vertical tab, form feed) are deliberately NOT
# in this class: rule 2's whitespace collapse already turns them into a
# single space, which is the pre-existing behaviour and is the friendlier
# result for a name someone typed with a stray newline. \x09 to \x0d are
# therefore excluded here so the two rules do not fight over them.
_TMUX_FORBIDDEN_CHARS = re.compile(r"[.:|\x00-\x08\x0e-\x1f\x7f]")
_WHITESPACE_RUN = re.compile(r"\s+")


def backfill_agent_type(
    session: Optional[Session],
    owned_tmux_sessions: Optional[set] = None,
) -> int:
    """Phase 6 one-shot ``agent_type`` backfill (pure, testable).

    Pre-Phase-6 ``session_metadata.json`` files have no ``agent_type``
    field, so ``Session(**raw)`` deserializes it as ``None``. Owned
    sessions could only have been claude (the only agent we supported
    pre-Phase-6), so backfill those to ``"claude"``. Adopted sessions
    (id prefixed ``adopted:`` or absent from ``owned_tmux_sessions``)
    stay ``None`` - the Phase 7 fingerprint detector populates them
    on adopt.

    Args:
        session: The active Session to backfill (mutated in place).
            ``None`` is a no-op.
        owned_tmux_sessions: Set of tmux session names this server
            created. When supplied AND non-empty, used as the source
            of truth for "ours vs adopted". When ``None`` or empty,
            falls back to the ``adopted:`` id-prefix heuristic for
            backward compatibility with legacy callers.

    Returns:
        Number of sessions backfilled (0 or 1 in single-active mode).
        Idempotent: a session whose ``agent_type`` is already set
        returns 0.
    """
    if session is None or session.agent_type is not None:
        return 0

    is_adopted = session.id.startswith("adopted:")
    if is_adopted:
        return 0

    session.agent_type = "claude"
    return 1


def _sanitize_tmux_name(name: str) -> str:
    """Transform a project name into a tmux-safe session name (verbatim where possible).

    tmux itself forbids only '.' (pane separator) and ':' (window separator).
    This helper additionally rejects '|' and control characters, because those
    are structural in the listing format the app parses back
    (src/core/tmux_listing_parse.LISTING_FORMAT): '|' is the field delimiter
    and a newline would split one session across two rows. Everything else
    (spaces, case, unicode, emoji, punctuation) is legal and preserved, so the
    helper still keeps the original name as close to verbatim as it can.

    Rules:
      1. Replace any '.', ':', '|' or control character with '_'.
      2. Collapse runs of whitespace (including newlines/tabs) into a single space.
      3. Strip leading and trailing whitespace.

    Returns empty string for truly empty/whitespace-only input (caller's fallback signal).
    """
    if not name:
        return ""
    replaced = _TMUX_FORBIDDEN_CHARS.sub("_", name)
    collapsed = _WHITESPACE_RUN.sub(" ", replaced)
    return collapsed.strip()


@dataclass(frozen=True)
class ProbeHealth:
    """Outcome of the most recent tmux listing probe (S9).

    THREE OUTCOMES, not two. ``ok=None`` ("never probed") must never be
    read the same as ``ok=True`` ("probed and healthy") - a caller that
    treats "no answer yet" as "healthy" is exactly the false-green class
    this repo's CLAUDE.md names as the recurring defect. See
    ``SessionManager.last_probe_health``.

    Attributes:
        ok: None - no probe has run yet this process's lifetime.
            True - the most recent probe succeeded (regardless of how
            many rows it returned). False - the most recent probe
            failed.
        reason: short machine token for the failure (mirrors
            ``TmuxListing.reason``), or None when ``ok`` is not False.
        detail: human-readable detail for the same failure, or None.
    """

    ok: Optional[bool]
    reason: Optional[str] = None
    detail: Optional[str] = None


def _configured_wrappers():
    """The user's configured launch wrappers, or an empty list.

    Description: THE ONE PLACE this is read for display resolution, and it
      exists because the previous spelling was wrong everywhere and could
      not fail. Three call sites used::

          getattr(getattr(settings, "agents", None), "wrappers", None) or []

      ``Settings`` has no ``agents`` attribute - the agents block lives on
      ``settings.load_auth_config().agents`` - so that chain returned None,
      then ``or []``, on every machine, forever. The defensive getattr
      turned a WRONG ATTRIBUTE PATH into a plausible empty answer instead
      of an AttributeError, and an empty wrapper list is not obviously
      wrong: ``resolve_family_for_display`` simply could not match any
      wrapper id, so every session launched through a wrapper rendered
      "unknown family". The session we knew the most about displayed worse
      than one we had only fingerprinted.

      The defensiveness that matters is kept - a config that will not load
      must not break a listing - but it is now around the IO, not around a
      misspelled path.
    Output: list - AgentWrapper objects; empty only when the config truly
      has none or could not be read.
    Example: _configured_wrappers()
    """
    try:
        return settings.load_auth_config().agents.wrappers or []
    except Exception as exc:  # noqa: BLE001 - a bad config must not break a listing
        logger.warning("configured_wrappers_unavailable", error=str(exc))
        return []


class SessionManager:
    """Manages Claude Code sessions via a pluggable SessionBackend."""

    def __init__(self):
        """Initialize the session manager."""
        # ---- per-session state, keyed by session_id ---------------------
        # Multiple sessions coexist; two browser tabs can each be attached
        # to a different session. Touching one session's entry NEVER
        # touches another's - that isolation is the whole point.
        self.sessions: dict[str, Session] = {}
        self.backends: dict[str, SessionBackend] = {}
        # session_id -> configured terminal-command id awaiting its first
        # client attach (feat/settings-tabs-and-commands). Holds an ID, never
        # a command string; the text is read from config.json at flush time.
        # In-memory only and popped on flush, so a restart or a reconnect
        # can never replay a command. See flush_pending_terminal_command.
        self.pending_terminal_commands: dict[str, str] = {}
        # Output fan-out: each backend's ``on_output`` callback is bound to
        # its own session_id (see ``_make_output_handler``), so bytes route
        # to ``self._subscribers[session_id]`` and nowhere else.
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # Per-session log buffers / command counters (capped per session at
        # ``settings.log_buffer_size``).
        self.log_buffers: dict[str, list[LogEntry]] = {}
        self.command_counts: dict[str, int] = {}
        # Item 7: per-session idle watcher. Constructed lazily at
        # ``create_session`` / ``adopt_external_session`` so we can inject
        # the live router from ``app.state``; cleared on destroy/detach.
        self.idle_watchers: dict[str, IdleWatcher] = {}
        # Byte offset into an adopted session's pipe-pane FIFO at capture
        # time - consumed once by the WS tailer. See ``consume_adopt_fifo_offset``.
        self.adopt_fifo_offsets: dict[str, int] = {}
        # Most-recently-created/adopted session id - backs the back-compat
        # ``current_session()`` / ``self.session`` / ``self.backend`` views.
        self._last_session_id: Optional[str] = None
        # Notification router reference - set by ``attach_notification_router``
        # during FastAPI lifespan startup (after both the SessionManager and
        # the router are constructed). When None, IdleWatcher instantiation
        # is skipped and no notification events fire.
        self._notification_router = None

        # The tmux socket the most recent attachable probe was bound to.
        # None until the first probe runs. Read by
        # ``list_attachable_sessions_with_socket`` so the adopt path keys
        # its rows on the socket the listing came from rather than on the
        # one settings claims.
        self._last_probe_socket: Optional[str] = None

        # Track 1 - adopt-external-session support.
        #
        # ``owned_tmux_sessions`` holds the full tmux session names that
        # Cloude Code itself created (e.g. ``cloude_myproject``). Persisted
        # in ``session_metadata.json`` so the UI can reliably tell
        # OUR-sessions apart from USER-started tmux sessions on the same
        # ``-L cloude`` socket (rather than spoof-able prefix matching).
        # Populated by ``create_session`` BEFORE return, pruned by
        # ``destroy_session``, reconciled on ``lifespan_startup``.
        self.owned_tmux_sessions: set[str] = set()

        # Set True by ``_load_session_metadata`` when reading a pre-v3
        # metadata file that lacks ``owned_tmux_sessions``. In that case
        # we treat the single active slug as owned for ONE rehydrate, then
        # re-persist the new schema on first successful round-trip. Guards
        # against stranding in-flight sessions on upgrade.
        self._legacy_metadata_needs_backfill: bool = False

        # v0.7.0 Part 2 - per-session toast notifications. Newest-first list
        # per session id; pruning keeps ALL unacked + last 50 acked (see
        # ``_prune_toasts``). Cleared on ``_wipe_session_state``. The
        # ``_theme_accent_cache`` memoizes theme manifest accent-color reads
        # keyed by theme id (manifest files are effectively static across
        # the server's lifetime - no invalidation needed).
        self._pending_toasts: dict[str, list[Toast]] = {}
        self._theme_accent_cache: dict[str, Optional[str]] = {}

        # v0.7.0 Part 3 - per-session HMAC tokens. Minted on
        # ``create_session`` / ``adopt_external_session``, injected as
        # ``CLOUDECODE_HOOK_TOKEN`` into the spawned agent's tmux env, and
        # forwarded back by Claude Code's lifecycle hooks via the
        # ``X-Cloudecode-Token`` header so the loopback hook endpoint can
        # authenticate the originating session. Dropped on
        # ``_wipe_session_state``. NEVER logged.
        # DURABLE, NOT EPHEMERAL, since 2026-08-28. Held in memory for
        # speed but backed by ``hook_tokens.json`` in the state dir,
        # because the same token is baked into each tmux pane's env at
        # spawn and read from there at hook-fire time - so it cannot be
        # re-issued to a running agent. A server restart used to forget
        # the table while every agent kept presenting its baked token,
        # which 403'd forever and silently killed activity status,
        # toasts and lineage for every pre-restart session. See
        # src/core/hook_tokens.py for the full reasoning.
        self._hook_tokens: dict[str, str] = {}
        # session_id -> tmux name, persisted beside the token. Surviving a
        # restart needs BOTH: the token gets a hook past authentication,
        # this is what lets the server work out WHICH session it belongs
        # to. Restoring only the token produces a hook that authenticates,
        # returns 200, and resolves to nothing - measured, and from the
        # agent's side indistinguishable from success.
        self._hook_tmux_names: dict[str, str] = {}
        # tmux name -> last activity state written, so the listing path
        # writes only on CHANGE. See _persist_settled_activity_state.
        self._last_persisted_activity: dict[str, str] = {}
        # cloudecode session id -> the claude uuid whose SessionEnd we
        # most recently saw. This is the ONLY thing that distinguishes
        # Claude's /branch from its /fork: both arrive as source="fork",
        # but /branch ends the previous conversation first and /fork does
        # not. Consumed and cleared by the next SessionStart.
        self._last_session_end_uuid: dict[str, str] = {}
        self._hook_tokens_durable: bool = True
        self._load_hook_tokens()

        # SESSION-IDENTITY-V2 - durable per-tmux-name pinned-theme map.
        # Lives in its own file (``pinned_themes.json``) so it survives
        # detach + swap + re-adopt cycles; ``session_metadata.json`` is
        # unlinked on detach and overwritten on swap and so cannot
        # function as the source of truth for a per-name pin. Mutated
        # by ``set_pinned_theme`` / ``clear_pinned_theme``; consulted by
        # ``adopt_external_session`` (seeds Session.pinned_theme on
        # re-entry) and ``list_attachable_sessions`` (decorates rows so
        # the launchpad can paint the pin without entering the session).
        self.pinned_themes: dict[str, str] = {}

        # feat/hook-driven-status - ephemeral, in-memory hook-signal state
        # machine (question/working/subagent-depth/heartbeat). One instance
        # for the whole manager, keyed internally by session_id. NOT
        # persisted - see src/core/session_activity.py's module docstring
        # for why a restart legitimately forgets this.
        self._activity_tracker = SessionActivityTracker()

        # feat/hook-driven-status - durable per-tmux-name read/unread
        # store. Own module (src/core/unread_store.py) rather than more
        # inline dict+I/O here - mirrors pinned_themes' persistence shape
        # (own file, name-keyed, survives detach/swap/re-adopt) without
        # growing this already-large file further.
        self._unread_store = UnreadStore(settings.get_unread_state_path())

        # S9 - listing-time fingerprint cache, keyed on the INSTANCE
        # TRIPLE (tmux_socket, tmux_name, tmux_created_epoch), never on
        # the name alone (a name is reusable; the triple is not - same
        # identity rule as ``sessions.tmux_created_epoch`` throughout
        # this module). ``GET /sessions/attachable`` runs on every home
        # screen load/poll, so probing scrollback per row on every call
        # would make the launcher slow in proportion to session count.
        # Caching in memory, keyed on the triple, means each distinct
        # tmux instance is fingerprinted AT MOST ONCE per process
        # lifetime: a cache hit costs one dict lookup, a miss costs one
        # ``tmux capture-pane`` call (no pipe-pane, no FIFO - see
        # ``_fingerprint_agent_type_for_listing``). Not persisted to the
        # ``sessions`` table on purpose: writes to that table are
        # deliberately restricted to ``src/core/session_identity.py``
        # (see session_store.py's module docstring, "WHERE THE WRITES
        # LIVE") and an unadopted external session may have no row there
        # at all. The tradeoff this accepts: a bare shell that later
        # execs an agent CLI keeps reading as "unknown family" until the
        # process restarts. That is the same staleness window the
        # instance-triple identity model accepts everywhere else in this
        # file, not a new one.
        self._listing_fingerprint_cache: dict[
            tuple[str, str, int], Optional[str]
        ] = {}

        # S9 - health of the MOST RECENT tmux listing probe, whichever
        # caller ran it (``list_attachable_sessions`` is the common
        # path, called on every home-screen poll). ``None`` until the
        # first probe of this process's lifetime - genuinely different
        # from both True and False, because "never checked" is its own
        # answer and must not be read as "checked and healthy". The
        # RECENT group (``GET /sessions/recent``) reads this rather than
        # triggering its own extra probe: RESTART safety depends on the
        # stored ``stopped`` rows being trustworthy RIGHT NOW, and a
        # currently-failing probe means we cannot currently confirm
        # that - a row that looks stopped in a stale read could in fact
        # be running, and offering RESTART against it is how you get two
        # of the same session.
        self._last_probe_ok: Optional[bool] = None
        self._last_probe_reason: Optional[str] = None
        self._last_probe_detail: Optional[str] = None

        # Load persisted session if it exists
        self._load_session_metadata()
        self._load_pinned_themes()

    # ---- multi-session accessors / back-compat shims --------------------

    def current_session(self) -> Optional[Session]:
        """The most-recently-created/adopted session, or None.

        Legacy single-session callers that genuinely just want "a" session
        use this. New code that knows the session id should use
        ``self.sessions[session_id]`` / ``get_backend(session_id)`` directly.
        """
        if self._last_session_id and self._last_session_id in self.sessions:
            return self.sessions[self._last_session_id]
        # _last_session_id may be stale (last session destroyed); fall back
        # to whatever's still around (dicts preserve insertion order).
        if self.sessions:
            sid = next(reversed(self.sessions))
            self._last_session_id = sid
            return self.sessions[sid]
        self._last_session_id = None
        return None

    @property
    def current_backend(self) -> Optional[SessionBackend]:
        """Backend of ``current_session()``, or None."""
        sess = self.current_session()
        if sess is None:
            return None
        return self.backends.get(sess.id)

    # Read-only back-compat aliases. Legacy callers in src/api only READ
    # these; do NOT assign to them from new code - touch the dicts instead.
    @property
    def session(self) -> Optional[Session]:
        return self.current_session()

    @property
    def backend(self) -> Optional[SessionBackend]:
        return self.current_backend

    @property
    def idle_watcher(self) -> Optional[IdleWatcher]:
        """Idle watcher of the current session (back-compat for the WS hot path)."""
        sess = self.current_session()
        if sess is None:
            return None
        return self.idle_watchers.get(sess.id)

    @property
    def adopt_fifo_start_offset(self) -> Optional[int]:
        """FIFO offset of the current session (back-compat)."""
        sess = self.current_session()
        if sess is None:
            return None
        return self.adopt_fifo_offsets.get(sess.id)

    def get_session(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def get_backend(self, session_id: str) -> Optional[SessionBackend]:
        return self.backends.get(session_id)

    def list_sessions(self) -> list[Session]:
        """All live sessions, insertion order (oldest first)."""
        return list(self.sessions.values())

    def _resolve_session_id(self, session_id: Optional[str]) -> Optional[str]:
        """Map an explicit id (validated) or None (-> current) to a live id."""
        if session_id is not None:
            return session_id if session_id in self.sessions else None
        sess = self.current_session()
        return sess.id if sess is not None else None

    def _make_output_handler(self, session_id: str):
        """Build an ``on_output`` callback bound to ``session_id``.

        Each backend gets its OWN handler so its bytes only ever land in
        ``self._subscribers[session_id]`` - destroying session A never
        touches session B's subscribers.
        """
        async def _on_output(data: bytes) -> None:
            encoded = base64.b64encode(data).decode("utf-8")
            subs = self._subscribers.get(session_id)
            if not subs:
                return
            for queue in list(subs):
                try:
                    await queue.put(encoded)
                except Exception as e:  # pragma: no cover - defensive
                    logger.error("failed_to_send_to_subscriber", error=str(e))
                    try:
                        subs.remove(queue)
                    except ValueError:
                        pass
        return _on_output

    def _wipe_session_state(self, session_id: str) -> None:
        """Drop every per-session dict entry for ``session_id``. Idempotent.

        Never touches another session's state. Subscribers for THIS session
        are cleared (their WS readers will see the queue go quiet and exit
        on disconnect / explicit teardown by the caller).
        """
        self.sessions.pop(session_id, None)
        self.backends.pop(session_id, None)
        self._subscribers.pop(session_id, None)
        self.log_buffers.pop(session_id, None)
        self.command_counts.pop(session_id, None)
        self.idle_watchers.pop(session_id, None)
        self.adopt_fifo_offsets.pop(session_id, None)
        # v0.7.0 Part 2 - drop pending toasts for this session. Other
        # sessions' toast lists are untouched.
        self._pending_toasts.pop(session_id, None)
        # v0.7.0 Part 3 - drop the HMAC hook token. After this point any
        # incoming hook POST for this session_id rejects with 403 (unknown
        # session → ``validate_hook_token`` returns False).
        # THE TOKEN IS DELIBERATELY NOT DROPPED HERE, and getting this
        # wrong once already cost a full debugging cycle.
        #
        # This function means "forget the in-memory state for this id" and
        # its callers are startup stale-cleanup, zombie cleanup and a
        # failed create - none of which is "the user ended this session".
        # Deleting the durable token here revoked the credential of a
        # perfectly live agent, so the very restart the token store exists
        # to survive wiped the store on the way down. Measured: a seeded
        # entry survived a restart untouched, while a real session's token
        # vanished before the server exited.
        #
        # Forgetting state and revoking a credential are two different
        # operations. The token's real lifetime is "as long as a tmux
        # session by that name is still owned", which is what
        # ``_gc_hook_tokens`` enforces at load, so nothing accumulates.
        # feat/hook-driven-status - drop the ephemeral hook-signal state.
        # The persisted unread flag (keyed by tmux NAME, not session_id) is
        # deliberately untouched here - it must survive detach/re-adopt.
        self._activity_tracker.forget(session_id)
        if self._last_session_id == session_id:
            self._last_session_id = (
                next(reversed(self.sessions)) if self.sessions else None
            )

    # ---- notification wiring --------------------------------------------

    def attach_notification_router(self, router) -> None:
        """Inject the NotificationRouter after lifespan has built it.

        Called from ``src/main.py`` once during FastAPI startup. Kept as an
        explicit setter rather than a constructor arg so SessionManager can
        still be built before the router exists (matches the current
        lifespan ordering where the SessionManager is constructed first and
        must be usable for pre-router operations like ``lifespan_startup``).
        """
        self._notification_router = router

    # ---- v0.7.0 Part 3 - Claude Code hook authentication -----------------
    #
    # Each live session has a unique per-process HMAC-bearer token that is
    # injected into the spawned agent's tmux env as ``CLOUDECODE_HOOK_TOKEN``.
    # Claude Code's lifecycle hooks (Stop / Notification / PermissionRequest)
    # POST to ``/api/v1/hooks/claude-event`` with the token in an
    # ``X-Cloudecode-Token`` header; the route validates via
    # ``validate_hook_token`` before recording a toast. The endpoint is also
    # loopback-only - this is a defense-in-depth pair, not a single layer.

    def _load_hook_tokens(self) -> None:
        """Rehydrate the hook-token table from disk at startup.

        Description: without this, every agent that survived the restart
          presents a token the server has never heard of and is rejected
          403 forever - silently, because nothing downstream of a hook
          reports its own absence. Never raises: a store that cannot be
          read leaves an empty table and sets ``_hook_tokens_durable``
          False, so the condition is KNOWN rather than merely suffered.
        Inputs: none (reads ``settings.get_state_dir()``).
        Output: None.
        """
        try:
            from src.core.hook_tokens import load_tokens

            result = load_tokens(settings.get_state_dir())
            self._hook_tokens = dict(result.tokens)
            self._hook_tmux_names = dict(result.tmux_names)
            self._gc_hook_tokens()
            self._hook_tokens_durable = result.durable
            if not result.durable:
                logger.warning(
                    "hook_tokens_not_durable",
                    detail=result.detail,
                    note=(
                        "sessions that survive a restart will 403 on every "
                        "hook until they are recreated"
                    ),
                )
            elif result.tokens:
                logger.info(
                    "hook_tokens_restored", count=len(result.tokens)
                )
        except Exception as exc:  # noqa: BLE001 - startup must not die here
            self._hook_tokens = {}
            self._hook_tmux_names = {}
            self._hook_tokens_durable = False
            logger.warning("hook_tokens_load_threw", error=str(exc))

    def _gc_hook_tokens(self) -> None:
        """Drop stored tokens whose tmux session is no longer owned.

        Description: the token's honest lifetime is "as long as a tmux
          session by that name is still ours". Bounding it that way -
          rather than by whether an id is in the in-memory table - is
          what lets a token survive a restart while still not
          accumulating for the life of the install.

          READS ``session_metadata.json`` DIRECTLY rather than
          ``self.owned_tmux_sessions``, because this runs from
          ``__init__`` before that set is rehydrated.

          KNOWN: IT IS ONE RESTART BEHIND. The file is reconciled against
          live tmux LATER in startup, so a session killed since the last
          run is still listed as owned when this reads it, and its token
          survives until the restart after. Measured: after killing four
          sessions the store still held 7 entries; the next restart took
          it to 1.

          Left as is because the lag errs in the safe direction. Over-
          retention keeps a token nothing can present - a dead session
          has no process to send a hook. Over-deletion would revoke a
          LIVE agent that cannot be re-issued one, which is the bug this
          whole store exists to fix. Reading a not-yet-reconciled list
          and pruning hard against it would be exactly that mistake. An unreadable or
          absent metadata file KEEPS EVERYTHING: "I could not find out
          which sessions are owned" must never be actioned as "none are",
          which would delete every token on every start and reinstate the
          bug this store exists to fix.
        Inputs: none.
        Output: None.
        """
        if not self._hook_tokens:
            return
        try:
            import json as _json

            meta = settings.get_state_dir() / "session_metadata.json"
            if not meta.exists():
                return
            owned = set(
                _json.loads(meta.read_text()).get("owned_tmux_sessions") or []
            )
        except (OSError, ValueError) as exc:
            logger.debug("hook_token_gc_skipped", error=str(exc))
            return
        if not owned:
            return

        # A token with NO recorded name is kept: it predates schema 2 and
        # cannot be judged, and discarding what cannot be evaluated is the
        # false-green move.
        dead = [
            sid
            for sid, name in self._hook_tmux_names.items()
            if name not in owned
        ]
        if not dead:
            return
        for sid in dead:
            self._hook_tokens.pop(sid, None)
            self._hook_tmux_names.pop(sid, None)
        logger.info("hook_tokens_gc", dropped=len(dead))
        self._persist_hook_tokens()

    def _persist_hook_tokens(self) -> None:
        """Write the token table out. Never raises, never logs a token."""
        try:
            from src.core.hook_tokens import save_tokens

            ok, reason = save_tokens(
                settings.get_state_dir(),
                self._hook_tokens,
                tmux_names=self._hook_tmux_names,
            )
            self._hook_tokens_durable = ok
            if not ok:
                logger.warning("hook_tokens_not_persisted", reason=reason)
        except Exception as exc:  # noqa: BLE001 - see docstring
            self._hook_tokens_durable = False
            logger.warning("hook_tokens_persist_threw", error=str(exc))

    def _mint_hook_token(
        self, session_id: str, tmux_name: Optional[str] = None
    ) -> str:
        """Mint and store a fresh URL-safe token for ``session_id``.

        Replaces any existing token for the same id (e.g. a re-adopt of a
        session whose backend was wiped). Returns the new token. The value
        is NEVER logged.
        """
        token = secrets.token_urlsafe(32)
        self._hook_tokens[session_id] = token
        # THE NAME MUST BE PASSED IN, not looked up. This is called BEFORE
        # the tmux spawn (the token has to exist to be injected into the
        # pane's environment), so ``self.sessions`` does not carry this id
        # yet and a lookup here returns None every time - measured: the
        # first store written this way recorded `tmux_name: null` for a
        # session whose name was known to its own caller.
        # NO FALLBACK LOOKUP. An earlier version ended `or getattr(
        # self.sessions.get(session_id), "tmux_session", None)`, which the
        # comment above already explains can only ever return None here -
        # so it was dead code whose sole effect was to require a fully
        # built manager, and it crashed five terminal tests on a test
        # double that has no `.sessions`. A fallback that cannot succeed
        # is not a safety net, it is an extra failure mode.
        if tmux_name:
            self._hook_tmux_names[session_id] = tmux_name
        self._persist_hook_tokens()
        return token

    def get_hook_token(self, session_id: str) -> Optional[str]:
        """Return the active hook token for ``session_id``, or None."""
        return self._hook_tokens.get(session_id)

    def validate_hook_token(self, session_id: str, token: str) -> bool:
        """Constant-time compare a presented token against the stored one.

        Returns False when the session is unknown, no token has been
        minted, or the value mismatches. Implemented via
        ``hmac.compare_digest`` so timing-leak attacks can't enumerate
        tokens via response-time deltas.
        """
        if not session_id or not token:
            return False
        expected = self._hook_tokens.get(session_id)
        if expected is None:
            return False
        # ``compare_digest`` requires equal-length byte/str inputs. The
        # length check itself is short-circuit, but since token_urlsafe(32)
        # always yields a 43-char string, length-mismatch from a forged
        # input is an unconditional False anyway.
        try:
            return hmac.compare_digest(expected, token)
        except (TypeError, ValueError):
            return False

    def get_env_for_spawn(self, session_id: str) -> dict[str, str]:
        """Return the env-var trio injected into the spawned agent's tmux env.

        Minted lazily on first call so backends that ``start()`` BEFORE the
        session is fully registered (we don't - see ``create_session``) can
        still ask. Empty dict when the configured port can't be read.

        Layering (feat/settings-gui): the user's global workspace
        environment goes in FIRST, then ``development_root`` as
        ``CLOUDE_DEV_ROOT`` and ``default_shell`` as ``SHELL``, then the
        three app variables below LAST so they win unconditionally. See
        ``src/core/workspace_settings.build_spawn_env`` - precedence is
        expressed as write order rather than trusted to the name policy,
        so the control channel stays intact even if that policy is later
        loosened.

        This reaches only NEWLY spawned terminals. tmux copies the
        environment at ``new-session`` time; a session already running
        keeps the environment it was born with, and the settings screen
        says so rather than letting the user believe a save applied
        everywhere.

        Variables:
            CLOUDECODE_SESSION_ID - used as the ``X-Cloudecode-Session``
                header so the hook endpoint can route the POST to the right
                session's toast bucket.
            CLOUDECODE_HOOK_TOKEN - bearer credential for the same hook
                endpoint. Validated via ``validate_hook_token``.
            CLOUDECODE_HOOK_URL - full loopback URL the hook curl POSTs to.
                Built from ``settings.port`` so port overrides (e.g.
                cloudecode running on 5001 vs the default 8000) flow
                through automatically.
        """
        token = self._hook_tokens.get(session_id) or self._mint_hook_token(session_id)
        try:
            port = settings.port
        except Exception:  # pragma: no cover - defensive
            return {}
        app_env = {
            "CLOUDECODE_SESSION_ID": session_id,
            "CLOUDECODE_HOOK_TOKEN": token,
            "CLOUDECODE_HOOK_URL": (
                f"http://127.0.0.1:{port}/api/v1/hooks/claude-event"
            ),
        }

        # LM Studio's address for the `cldl` wrapper, passed as an ENV VAR
        # rather than interpolated into the command string. That is not a
        # style choice: a host that never enters shell text cannot be
        # quoted wrongly, so the whole class of question disappears instead
        # of being answered carefully in one place and forgotten in the
        # next. Set only when configured - an empty CLDL_HOST would look to
        # the wrapper like a deliberate blank, which is a different claim
        # from "the user has not set this up".
        try:
            local_host = settings.load_auth_config().providers.local_host
        except Exception:  # noqa: BLE001 - a bad config must not block a spawn
            local_host = ""
        if local_host:
            app_env["CLDL_HOST"] = local_host

        # feat/settings-gui - the user's global workspace environment.
        # THIS function is the single funnel every spawn goes through
        # (create_session and the adopt path both call it), which is why
        # the global env is merged here and nowhere else: one place to
        # add it means one place it can be missing from.
        #
        # A failure to read the config must never stop a session from
        # starting, so this degrades to the app trio alone rather than
        # raising. It is logged, so a silently-unapplied environment is
        # visible instead of looking like the user typed nothing.
        try:
            workspace = settings.load_auth_config().workspace.model_dump()
        except Exception as exc:  # noqa: BLE001 - see comment above
            logger.warning("workspace_env_unavailable", error=str(exc))
            return app_env
        return build_spawn_env(workspace, app_env)

    # ---- backend type introspection --------------------------------------

    @property
    def backend_name(self) -> str:
        """Human-readable backend name for API responses ('tmux' / 'pty' / 'none')."""
        if self.backend is None:
            return "none"
        cls = self.backend.__class__.__name__
        # "TmuxBackend" → "tmux", "PTYBackend" → "pty"
        return cls.replace("Backend", "").lower()

    # ---- lifespan startup: discover + re-register -----------------------

    async def lifespan_startup(self) -> None:
        """Called once on server startup to re-adopt a surviving tmux session.

        This is separate from `__init__` because it needs to be awaitable and
        is driven by the FastAPI lifespan context manager. `main.py` calls
        this after `SessionManager()` is constructed.

        Behavior:
        - Build a probe backend using the metadata slug (if any).
        - Ask it to `discover_existing()`.
        - Reconcile ``owned_tmux_sessions`` against the live listing -
          prune entries whose tmux session no longer exists. Prevents
          indefinite growth from orphaned records after crashes.
        - If the metadata's slug is present in the discovered list AND
          (new-schema case) is in ``owned_tmux_sessions`` OR (legacy
          case) the backfill flag is set, re-register the session as
          active and start the backend's read loop.
        - On first successful rehydrate of legacy metadata, add the
          slug to the owned set and re-persist so subsequent boots use
          the new schema directly.
        - Log other discovered sessions and leave them alone (orphan
          cleanup is out of scope - a v2 ``cloude-cleanup`` script).

        After tmux reconciliation, runs a one-shot orphan sweep of
        ``.cloude_uploads/`` buckets to catch files left by force-killed
        previous runs where ``destroy_session()`` never ran. Wrapped in
        ``try/finally`` so the sweep fires regardless of which
        reconciliation branch exited.
        """
        try:
            await self._lifespan_tmux_reconcile()
        finally:
            await self._sweep_orphan_uploads()

    def _register_session(
        self, session: Session, backend: Optional[SessionBackend]
    ) -> None:
        """Wire a Session (and optional backend) into the per-session dicts.

        Marks it as the current session. Used by both the create/adopt
        paths and the lifespan rehydrate path. Initializes empty
        subscriber/log/command containers if absent.
        """
        self.sessions[session.id] = session
        if backend is not None:
            self.backends[session.id] = backend
        self._subscribers.setdefault(session.id, [])
        self.log_buffers.setdefault(session.id, [])
        self.command_counts.setdefault(session.id, 0)
        self._last_session_id = session.id

    async def _lifespan_tmux_reconcile(self) -> None:
        """Tmux-side of lifespan startup. See ``lifespan_startup`` for
        full contract. Extracted so the public method can guarantee
        the orphan-upload sweep runs after every reconciliation path.

        Multi-session note: we still persist/rehydrate at most ONE session
        across restarts (concurrent live sessions are a runtime feature,
        not a durability one). The "persisted" session is the one returned
        by ``current_session()`` after ``_load_session_metadata``.
        """
        persisted = self.current_session()
        # Phase 6 - one-shot agent_type backfill. Logic extracted to the
        # top-level ``backfill_agent_type`` helper for direct unit testing
        # without spinning up the full lifespan path. Idempotent + safe to
        # re-run; only persists + logs when something actually changed.
        backfilled = backfill_agent_type(persisted, self.owned_tmux_sessions)
        if backfilled:
            self._save_session_metadata()
            logger.info(
                "session_metadata_agent_type_backfilled",
                count=backfilled,
            )

        # Probe tmux state once, upfront. We use this for both the
        # reconciler and the rehydrate path.
        probe = build_backend(
            settings,
            session_id="__probe__",
            working_dir=Path.home(),
            on_output=None,
        )
        # THREE-OUTCOME GATE. Everything below this line PRUNES persisted
        # state against ``tmux_alive``: the ownership set, the pinned
        # themes, the unread store, and (via ``_clear_stale_metadata``)
        # the session record itself. Under the old bare-list API a probe
        # that could not run returned ``[]``, which is indistinguishable
        # from "no sessions exist", so a single transient tmux failure at
        # startup silently deleted EVERY ownership record the user had.
        # The comment forty lines below even claimed this was already
        # guarded ("we only prune when we have a confirmed live tmux
        # probe") - it was not; nothing measured that.
        #
        # ``TmuxListing.ok`` is that measurement. False means the probe
        # produced no answer, so we change nothing and come up with the
        # metadata intact. We also do NOT rehydrate: attaching requires
        # knowing the session is alive, and we do not.
        listing = coerce_listing(probe.discover_existing())
        if not listing.ok:
            logger.warning(
                "lifespan_tmux_probe_unavailable",
                reason=listing.reason,
                detail=listing.detail,
                owned_count=len(self.owned_tmux_sessions),
                note=(
                    "tmux could not be enumerated - skipping ownership, "
                    "pinned-theme and unread pruning, and skipping "
                    "rehydrate. No persisted state was changed."
                ),
            )
            return
        tmux_alive = set(listing.names)

        # Reconciler: prune owned-set entries no longer alive on tmux.
        # Persist the pruned set only if we also have an active session
        # on record (otherwise there's nothing else to write and we'd
        # just emit a shell metadata file).
        if self.owned_tmux_sessions:
            stale = self.owned_tmux_sessions - tmux_alive
            if stale:
                logger.info(
                    "owned_tmux_sessions_pruning_stale",
                    stale=sorted(stale),
                )
                self.owned_tmux_sessions -= stale
                if persisted is not None:
                    self._save_session_metadata()

        # SESSION-IDENTITY-V2 - prune pinned-theme entries whose tmux
        # session is gone. Reaching this line already means the probe
        # RAN (the ``listing.ok`` gate above returns early otherwise), so
        # an empty ``tmux_alive`` here is a measured zero rather than an
        # unanswered question. Prevents indefinite growth from sessions
        # the user destroyed outside our UI (``tmux -L cloude
        # kill-session``).
        if self.pinned_themes:
            dead_pins = {
                name for name in self.pinned_themes if name not in tmux_alive
            }
            if dead_pins:
                logger.info(
                    "pinned_themes_pruning_dead",
                    names=sorted(dead_pins),
                )
                for name in dead_pins:
                    self.pinned_themes.pop(name, None)
                self._save_pinned_themes()

        # feat/hook-driven-status - same reconciliation for the persisted
        # unread store: a tmux session the user killed outside our UI
        # (``tmux -L cloude kill-session``) shouldn't leave a permanent
        # unread badge nothing can ever clear (mark_session_viewed needs a
        # live backend to resolve tmux_name -> flag, which no longer
        # exists once tmux itself forgot the name).
        self._unread_store.prune(tmux_alive)

        if persisted is None:
            # No metadata on disk → nothing to re-adopt.
            if tmux_alive:
                logger.info(
                    "session_backend_discovered_orphans",
                    count=len(tmux_alive),
                    names=sorted(tmux_alive),
                    hint="no metadata on disk - leaving orphans alone",
                )
            return

        # Build a backend matching the metadata's session id.
        work_path = Path(persisted.working_dir)
        backend = build_backend(
            settings,
            session_id=persisted.id,
            working_dir=work_path,
            on_output=self._make_output_handler(persisted.id),
        )

        if not tmux_alive:
            # No tmux sessions at all → treat metadata as stale.
            logger.info(
                "session_metadata_has_no_backend_match",
                session_id=persisted.id,
            )
            self._clear_stale_metadata(persisted.id)
            return

        # For TmuxBackend, the registered name is `cloude_<slug>`. Match against it.
        # For PTYBackend, `discover_existing()` is always empty so we never reach here.
        target_name = getattr(backend, "tmux_session", None)

        # Ownership gate: we only rehydrate OUR sessions. A user-created
        # tmux session on our socket (``cloude_foo`` they made themselves)
        # must NOT be rehydrated as if it were ours; it'll surface in the
        # adopt UI instead.
        # feat/sessions-table (S4): this is an OWNERSHIP decision, so it
        # goes through the shared resolver rather than reading the legacy
        # set directly - a session whose stored origin is 'created' or
        # 'adopted' is ours and must rehydrate even after the in-memory
        # set has been rebuilt from scratch by a restart. That is the
        # whole reason origin is a stored column.
        ownership_ok = (
            target_name is not None
            and (
                self.is_owned_tmux_name(target_name)
                or self._legacy_metadata_needs_backfill
            )
        )

        if target_name and target_name in tmux_alive and ownership_ok:
            try:
                await backend.attach_existing()
            except NotImplementedError:
                logger.warning(
                    "session_backend_cannot_rehydrate",
                    session_id=persisted.id,
                    backend=type(backend).__name__,
                )
                self._clear_stale_metadata(persisted.id)
                return
            except RuntimeError as e:
                logger.warning(
                    "session_backend_attach_failed",
                    session_id=persisted.id,
                    error=str(e),
                )
                self._clear_stale_metadata(persisted.id)
                return

            persisted.status = SessionStatus.RUNNING
            self._register_session(persisted, backend)

            # Legacy backfill: first successful rehydrate populates the
            # owned-set and re-persists under the new schema.
            if self._legacy_metadata_needs_backfill:
                self.owned_tmux_sessions.add(target_name)
                self._save_session_metadata()
                logger.info(
                    "session_metadata_legacy_backfilled",
                    session_id=persisted.id,
                    owned=sorted(self.owned_tmux_sessions),
                )

            logger.info(
                "session_re_registered_from_backend",
                session_id=persisted.id,
                backend_session=target_name,
            )
            # Log strangers so the operator knows they're there.
            orphans = [n for n in tmux_alive if n != target_name]
            if orphans:
                logger.info(
                    "session_backend_orphans_ignored", names=sorted(orphans)
                )
        else:
            # Either the tmux session died, or the slug isn't ours to
            # rehydrate. Log the reason and clear stale metadata.
            if target_name and target_name in tmux_alive and not ownership_ok:
                logger.warning(
                    "session_metadata_slug_not_owned",
                    session_id=persisted.id,
                    target=target_name,
                    owned=sorted(self.owned_tmux_sessions),
                    note="not rehydrating a non-owned session",
                )
            else:
                logger.warning(
                    "session_metadata_slug_not_in_backend",
                    session_id=persisted.id,
                    target=target_name,
                    discovered=sorted(tmux_alive),
                )
            self._clear_stale_metadata(persisted.id)

    def _clear_stale_metadata(self, session_id: Optional[str] = None) -> None:
        """Delete on-disk metadata for a session that can't be re-adopted.

        If ``session_id`` is given, that session's in-memory state is
        wiped too; if None, the current session (if any) is wiped.
        """
        metadata_path = settings.get_session_metadata_path()
        # DROP THE SESSION POINTER, KEEP THE OWNED SET. This file is the
        # only durable home of ``owned_tmux_sessions``, and that set is
        # about EVERY session the app created - not about the one session
        # whose slug we are discarding here. Unlinking the file outright
        # (what this used to do) threw away N sessions' ownership record
        # to clean up 1, and the trigger is the ORDINARY case, not an
        # error path: ``session_metadata_slug_not_in_backend`` fires
        # whenever the last-active tmux session is simply gone by the next
        # start. Measured on the live install - four load/delete pairs
        # ~40ms apart, then the file never returned, and from that point
        # every launcher-created session resolved EXTERNAL because
        # ``resolve_ownership`` fell past its empty tier-3 legacy set.
        #
        # An owned-set-only payload (no ``id``) is written instead, which
        # ``_load_session_metadata`` reads back without rehydrating a
        # session - so the dead pointer stays dead and surfaces in the
        # Adopt list exactly as before.
        # ORDERING IS DELIBERATE: unlink FIRST, then re-write. The unlink
        # of the RESOLVED path is left exactly as it was so this change
        # does not disturb the state-dir migration semantics - the file
        # still relocates out of the legacy ``log_directory`` on the next
        # write, which ``tests/test_session_meta_continuity.py`` measures.
        # ``_write_metadata_atomic`` re-resolves after the unlink, which is
        # the same thing ``_save_session_metadata`` would have done.
        try:
            if metadata_path.exists():
                metadata_path.unlink()
                logger.info("stale_session_metadata_deleted")
            if self.owned_tmux_sessions:
                self._write_metadata_atomic(
                    {"owned_tmux_sessions": sorted(self.owned_tmux_sessions)}
                )
                logger.info(
                    "stale_session_metadata_owned_set_kept",
                    owned_count=len(self.owned_tmux_sessions),
                )
        except Exception as exc:
            logger.error("failed_to_delete_stale_metadata", error=str(exc))
        sid = session_id
        if sid is None:
            cur = self.current_session()
            sid = cur.id if cur is not None else None
        if sid is not None:
            self._wipe_session_state(sid)

    async def _sweep_orphan_uploads(self) -> None:
        """One-shot sweep on startup using current AuthConfig.uploads settings.

        Catches files left behind by force-killed previous runs where
        destroy_session() never ran. Identical prune logic to the periodic
        UploadSweeper so they share intent.
        """
        from src.core.project_authority import resolve_projects

        auth_cfg = settings.load_auth_config()
        cfg = auth_cfg.uploads
        if not cfg.enabled:
            return
        sweeper = UploadSweeper(
            ttl_seconds=cfg.ttl_seconds,
            interval_seconds=0,
            # PROJECT PATHS COME FROM THE DATASTORE, which is the only
            # place projects live as of v1.0.4. The helper keeps the
            # three-outcome contract: a list (possibly empty) when the
            # datastore was read, None when it could not be, and None
            # means the sweeper deletes nothing at all rather than
            # sweeping paths it could not verify.
            project_paths=datastore_project_paths(
                resolve_projects(settings.get_state_dir())
            ),
            default_dir=settings.get_working_dir(),
        )
        try:
            result = await sweeper.sweep_now()
            logger.info("upload_orphan_sweep_complete", **result)
        except Exception as exc:
            logger.warning("upload_orphan_sweep_failed", error=str(exc))

    # ---- metadata persistence -------------------------------------------

    def _load_session_metadata(self):
        """Load session metadata from disk if it exists.

        Unlike the pre-refactor code, we do NOT probe the process here - at
        `__init__` time we don't yet know which backend to build. The probe
        happens in `lifespan_startup()`.

        Schema v3 adds ``owned_tmux_sessions`` (a list). Missing field
        triggers the legacy-backfill path: populate the set with the
        active session's slug for ONE rehydrate, flip a sentinel flag,
        and re-persist with the new schema on the first successful save.
        This avoids stranding in-flight sessions on upgrade.
        """
        metadata_path = settings.get_session_metadata_path()

        if not metadata_path.exists():
            logger.info("no_existing_session_metadata")
            return

        try:
            with open(metadata_path, "r") as f:
                raw = json.load(f)

            # Extract the new schema field BEFORE handing the rest to
            # ``Session(**)``, which would reject unknown keys with
            # ``extra='forbid'`` if we ever tightened it.
            owned = raw.pop("owned_tmux_sessions", None)

            # OWNED-SET-ONLY PAYLOAD. Written by ``_clear_stale_metadata``
            # when it drops an un-rehydratable session pointer but has an
            # ownership record worth keeping. There is no session to
            # rehydrate, and that is the whole point - handing this to
            # ``Session(**raw)`` would raise and the except below would
            # swallow the owned set along with it, which is the exact loss
            # the owned-set-only payload exists to prevent.
            if not raw.get("id"):
                self.owned_tmux_sessions = set(owned or [])
                self._legacy_metadata_needs_backfill = False
                logger.info(
                    "session_metadata_owned_set_only_loaded",
                    owned_count=len(self.owned_tmux_sessions),
                    note="no persisted session to rehydrate",
                )
                return

            loaded = Session(**raw)
            # Register the persisted session into the per-session dicts
            # (backend wired later by ``_lifespan_tmux_reconcile``). This
            # is the only session restored across restarts; concurrent live
            # sessions are a runtime-only feature.
            self._register_session(loaded, backend=None)

            if owned is None and raw.get("id"):
                # Pre-v3 metadata: no owned-set was persisted. Mark for
                # backfill on next save; the reconciler in
                # ``lifespan_startup`` will populate the set once the
                # slug is confirmed live on the tmux socket.
                self.owned_tmux_sessions = set()
                self._legacy_metadata_needs_backfill = True
                logger.info(
                    "session_metadata_legacy_detected",
                    session_id=loaded.id,
                    note="owned_tmux_sessions will be backfilled on rehydrate",
                )
            else:
                self.owned_tmux_sessions = set(owned or [])
                self._legacy_metadata_needs_backfill = False

            logger.info(
                "session_metadata_loaded",
                session_id=loaded.id,
                owned_count=len(self.owned_tmux_sessions),
                note="probe deferred to lifespan_startup",
            )
        except Exception as e:
            logger.error("failed_to_load_session_metadata", error=str(e))

    def _write_metadata_atomic(self, data: dict) -> None:
        """Durable, crash-consistent metadata write.

        Protocol: write to a sibling ``.tmp`` file → ``f.flush()`` →
        ``os.fsync(fd)`` → ``os.replace(tmp, final)``. ``os.replace`` is
        the only rename primitive guaranteed atomic across POSIX and
        Windows. ``fsync`` before the rename prevents a kernel panic
        from stranding a zero-byte file at the final path (which, on
        ext4 ``data=ordered``, is a real scenario).

        The directory's own ``fsync`` (for rename durability) is skipped
        - this is metadata, not a source of truth for money. Losing
        the very last write to a sudden power failure is acceptable;
        losing SESSION OWNERSHIP isn't, which is what the atomic rename
        prevents.
        """
        path = settings.get_session_metadata_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")

        with tmp.open("w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError as exc:
                # tmpfs and some network FS don't support fsync; log and
                # continue - the rename is still atomic per POSIX.
                logger.debug("metadata_fsync_unsupported", error=str(exc))

        os.replace(str(tmp), str(path))

    def _save_session_metadata(self, session: Optional[Session] = None):
        """Save session metadata atomically, including the owned-set.

        Persists ``session`` if given, else the current session. Only one
        session is ever persisted across restarts - the most-recently-
        active one is the pragmatic choice (concurrent live sessions are a
        runtime feature, not a durability one).
        """
        sess = session or self.current_session()
        if not sess:
            return

        try:
            payload = sess.model_dump()
            payload["owned_tmux_sessions"] = sorted(self.owned_tmux_sessions)
            self._write_metadata_atomic(payload)

            # Clear the backfill sentinel once we've successfully persisted
            # the new schema - one successful save is the migration.
            self._legacy_metadata_needs_backfill = False

            logger.debug(
                "session_metadata_saved",
                session_id=sess.id,
                owned_count=len(self.owned_tmux_sessions),
            )

        except Exception as e:
            logger.error("failed_to_save_session_metadata", error=str(e))

    # ---- pinned-themes persistence (SESSION-IDENTITY-V2) ---------------

    def _load_pinned_themes(self) -> None:
        """Load the per-tmux-name pinned-theme map from disk.

        Missing file = empty map (first run / never pinned). Malformed
        file = empty map + warning log; we never crash startup over a
        corrupt non-critical preferences file. Values must be strings;
        any other type is dropped on load.
        """
        path = settings.get_pinned_themes_path()
        if not path.exists():
            return
        try:
            with open(path, "r") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                logger.warning(
                    "pinned_themes_unexpected_shape",
                    type=type(raw).__name__,
                )
                return
            self.pinned_themes = {
                str(k): v for k, v in raw.items()
                if isinstance(v, str) and v
            }
            logger.info(
                "pinned_themes_loaded", count=len(self.pinned_themes)
            )
        except Exception as exc:
            logger.warning("failed_to_load_pinned_themes", error=str(exc))

    def _save_pinned_themes(self) -> None:
        """Persist the pinned-theme map atomically.

        Re-uses the same atomic-rename protocol as ``_save_session_metadata``
        so a crash mid-write can never leave a half-written file at the
        canonical path.
        """
        path = settings.get_pinned_themes_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w") as f:
                json.dump(self.pinned_themes, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(str(tmp), str(path))
        except Exception as exc:
            logger.error("failed_to_save_pinned_themes", error=str(exc))

    # ---- read/unread persistence (feat/hook-driven-status) ---------------
    #
    # Storage itself lives in ``src/core/unread_store.py`` (own file, own
    # atomic-write protocol, zero knowledge of sessions/tmux). Everything
    # below is the thin session_id/tmux-name resolution glue that only
    # SessionManager has the context to do.

    def _is_unread(self, tmux_name: Optional[str]) -> bool:
        """True iff ``tmux_name`` carries an auto or manual unread flag."""
        return self._unread_store.is_unread(tmux_name)

    def mark_session_viewed(self, session_id: str) -> None:
        """Clear the AUTO unread flag for the session bound to ``session_id``.

        Called when a WS terminal actually binds to a session (see
        ``src/api/websocket.py``'s ``connection_manager.bind_session``
        call site) - the strongest "the user is looking at this" signal
        the server has, deliberately stronger than merely appearing in a
        list/poll response. Does NOT touch the manual flag: a session the
        user explicitly pinned unread for followup stays flagged even
        after they open it, until they explicitly clear it (see
        ``set_manual_unread``) - this is the "survives being viewed"
        requirement.
        """
        backend = self.backends.get(session_id)
        tmux_name = getattr(backend, "tmux_session", None) if backend else None
        if not tmux_name:
            return
        self._unread_store.set_flag(tmux_name, "auto", False)

    def set_manual_unread(self, tmux_name: str, unread: bool) -> None:
        """Set or clear the MANUAL unread flag for a tmux session name.

        Description: The user-facing "mark unread for followup" control.
            Keyed by tmux name (not session_id) so it works for BOTH a
            live session and an attachable-but-not-live one - you can pin
            a conversation unread whether or not anything is currently
            attached to it. Unlike the auto flag, nothing but this method
            (a repeat call, presumably from the user clicking again)
            clears it.
        Inputs:
            tmux_name: literal tmux session name (never a session_id).
            unread: True to set, False to clear.
        Output: None (persisted immediately).
        Example:
            >>> mgr.set_manual_unread("cloude_myproj", True)
            >>> mgr._is_unread("cloude_myproj")
            True
        """
        if not tmux_name:
            raise ValueError("tmux_name is required")
        self._unread_store.set_flag(tmux_name, "manual", unread)

    def get_pinned_theme(self, tmux_name: str) -> Optional[str]:
        """Return the persisted pin for a tmux session name, or None."""
        if not tmux_name:
            return None
        return self.pinned_themes.get(tmux_name)

    def set_pinned_theme(
        self, tmux_name: str, theme_id: Optional[str]
    ) -> None:
        """Persist (or clear) the pinned theme for a tmux session name.

        ``theme_id`` None or empty clears the pin. Always persists - a
        cleared pin must round-trip across server restart same as a set
        one. Mirrors onto the live in-memory ``Session.pinned_theme``
        when the named session is the currently-active backend, so a
        subsequent ``get_session_info()`` reflects the change without
        requiring a re-load from disk.
        """
        if not tmux_name:
            return
        if theme_id:
            self.pinned_themes[tmux_name] = theme_id
        else:
            self.pinned_themes.pop(tmux_name, None)
        self._save_pinned_themes()

        # Mirror onto any live Session whose backend IS this tmux name, so
        # SessionInfo serialization picks it up immediately.
        for sid, backend in list(self.backends.items()):
            if getattr(backend, "tmux_session", None) == tmux_name:
                sess = self.sessions.get(sid)
                if sess is not None:
                    sess.pinned_theme = theme_id
                    # Persist into session_metadata so a server-restart
                    # rehydrate path keeps the in-memory mirror coherent
                    # (belt-and-suspenders; durable source of truth is
                    # ``pinned_themes.json`` - adopt seeds from there).
                    self._save_session_metadata(sess)
                break

    def discard_pinned_theme(self, tmux_name: str) -> None:
        """Drop a name's pin entry entirely. No-op if not present.

        Called on explicit destroy paths (``destroy_session`` /
        ``destroy_external_session``) so a tmux name that's truly gone
        doesn't accumulate dead pins forever.
        """
        if tmux_name and tmux_name in self.pinned_themes:
            self.pinned_themes.pop(tmux_name, None)
            self._save_pinned_themes()

    # ---- project-scoped theme (v0.7.0 - .cc.theme dotfile) -------------
    #
    # The source of truth for a project's theme is ``<working_dir>/.cc.theme``
    # (a single-line file containing the theme id + trailing newline). This
    # supersedes the per-tmux-name ``pinned_themes.json`` map for the
    # multi-machine use case: two browsers / two machines pointed at the
    # same checkout converge on the same theme without round-tripping a
    # per-machine cache.
    #
    # ``pinned_themes.json`` is RETAINED as a back-compat fallback for one
    # release so sessions pinned under v0.6.x still paint correctly.
    # ``migrate_pinned_theme_to_dotfile`` is best-effort and runs on
    # attach/adopt to ferry old entries into the new format. The old map
    # is not deleted by migration - it decays naturally as users re-pin.

    @staticmethod
    def _project_theme_path(working_dir) -> Optional[Path]:
        """Resolve the dotfile path under ``working_dir``.

        Returns None when ``working_dir`` is empty / unresolvable so callers
        can short-circuit without try/except gymnastics. Tilde-expansion
        and absolute resolution happen here so caller paths stay simple.
        """
        if not working_dir:
            return None
        try:
            return Path(str(working_dir)).expanduser().resolve() / ".cc.theme"
        except (OSError, RuntimeError):
            return None

    def get_project_theme(self, working_dir) -> Optional[str]:
        """Read ``<working_dir>/.cc.theme``; fall back to pinned_themes.json.

        Resolution order:
          1. ``<working_dir>/.cc.theme`` (v0.7.0+ project-scoped source of truth)
          2. ``pinned_themes.json`` keyed by the bare tmux name - but ONLY
             when a caller already supplied ``working_dir`` *and* no
             dotfile exists. This branch is the read-time back-compat
             fallback for sessions pinned under v0.6.x.

        Step 2 cannot be performed here without a tmux name; this method
        only does the dotfile read. Callers that need the JSON fallback
        should call ``get_pinned_theme(tmux_name)`` themselves and prefer
        whichever they receive. Returns None when nothing is pinned.
        """
        path = self._project_theme_path(working_dir)
        if path is None or not path.exists():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                "project_theme_read_failed",
                path=str(path),
                error=str(exc),
            )
            return None
        return content or None

    def set_project_theme(self, working_dir, theme_id: Optional[str]) -> None:
        """Atomically write/clear ``<working_dir>/.cc.theme``.

        Empty/None ``theme_id`` deletes the dotfile (clears the pin).
        Otherwise writes ``<theme_id>\\n`` with mode 0o644 via
        ``tmp + os.replace`` so a crash mid-write can never leave a
        half-written file at the canonical path.

        Raises:
            FileNotFoundError: ``working_dir`` does not exist.
            OSError: ``working_dir`` is not writable.
            ValueError: ``working_dir`` resolves to None (caller bug).
        """
        path = self._project_theme_path(working_dir)
        if path is None:
            raise ValueError(f"Invalid working_dir: {working_dir!r}")

        parent = path.parent
        if not parent.exists():
            raise FileNotFoundError(
                f"working_dir does not exist: {parent}"
            )
        if not parent.is_dir():
            raise NotADirectoryError(
                f"working_dir is not a directory: {parent}"
            )

        # Clear branch - delete the dotfile if present.
        if not theme_id:
            if path.exists():
                try:
                    path.unlink()
                    logger.info("project_theme_cleared", path=str(path))
                except OSError as exc:
                    logger.error(
                        "project_theme_clear_failed",
                        path=str(path),
                        error=str(exc),
                    )
                    raise
            return

        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(f"{theme_id}\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            try:
                os.chmod(str(tmp), 0o644)
            except OSError:
                # chmod failure on the tmp shouldn't abort the write -
                # the final replace will still publish the file. Log only.
                logger.debug("project_theme_chmod_failed", path=str(tmp))
            os.replace(str(tmp), str(path))
            logger.info(
                "project_theme_set",
                path=str(path),
                theme_id=theme_id,
            )
        except OSError:
            # Best-effort cleanup of the tmp on failure so we don't leave
            # turds in user projects.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def migrate_pinned_theme_to_dotfile(self, session) -> bool:
        """Ferry a v0.6.x pinned_themes.json entry into ``.cc.theme``.

        Runs on attach/adopt when:
          - ``<session.working_dir>/.cc.theme`` does NOT exist, AND
          - ``pinned_themes.json`` has an entry for ``session.tmux_session``
            (or the trailing component of ``session.id`` for owned sessions).

        Best-effort: any exception is logged and swallowed so the
        attach/adopt path is never broken by a failed migration. The
        legacy map entry is intentionally NOT deleted - let it decay
        naturally; this release keeps it as a read-time fallback.

        Returns True on successful migration, False otherwise.
        """
        if session is None:
            return False
        working_dir = getattr(session, "working_dir", None)
        if not working_dir:
            return False
        try:
            path = self._project_theme_path(working_dir)
            if path is None:
                return False
            # If the dotfile already exists, the new format wins - nothing
            # to migrate.
            if path.exists():
                return False
            # Working dir must actually exist on disk before we'd write to it.
            if not path.parent.is_dir():
                logger.debug(
                    "migrate_pinned_theme_skipped_no_dir",
                    working_dir=str(working_dir),
                )
                return False
            # Resolve the legacy key. Prefer the explicit ``tmux_session``
            # field (canonical pin handle); fall back to the trailing
            # component of ``session.id`` for adopted rows pre-PIN-FIX.
            tmux_name = getattr(session, "tmux_session", None)
            if not tmux_name:
                sid = getattr(session, "id", "") or ""
                if sid.startswith("adopted:"):
                    tmux_name = sid[len("adopted:"):]
                else:
                    tmux_name = sid
            if not tmux_name:
                return False
            legacy_pin = self.pinned_themes.get(tmux_name)
            if not legacy_pin:
                return False
            self.set_project_theme(working_dir, legacy_pin)
            logger.info(
                "theme_migrated_to_dotfile",
                session_id=getattr(session, "id", None),
                working_dir=str(working_dir),
                tmux_name=tmux_name,
                theme_id=legacy_pin,
            )
            return True
        except Exception as exc:
            logger.warning(
                "theme_migration_failed",
                session_id=getattr(session, "id", None),
                working_dir=str(working_dir),
                error=str(exc),
            )
            return False

    def resolve_project_theme(
        self, working_dir, tmux_name: Optional[str] = None
    ) -> Optional[str]:
        """Combined lookup: dotfile first, then JSON fallback by tmux name.

        Convenience wrapper for callers (create_session / adopt) that want
        a single call returning the effective pin without writing migration
        glue at every call site.
        """
        dotfile = self.get_project_theme(working_dir)
        if dotfile is not None:
            return dotfile
        if tmux_name:
            return self.get_pinned_theme(tmux_name)
        return None

    # ---- toast notifications (v0.7.0 Part 2) ----------------------------
    #
    # Per-session list of ``Toast`` records, newest-first. Pruning rule:
    # keep ALL unacked + at most the LAST 50 acked (acked beyond that cap
    # fall off the tail). Every unacked toast stays surfaceable to a
    # re-attaching browser via ``get_toasts(..., unacked_only=True)``.
    #
    # THAT CAP BOUNDS ONLY THE ACKED TAIL. The unacked half has no cap and
    # cannot have one, because a cap there would drop a notification the
    # user has not seen. The unacked list is instead kept small at the
    # SOURCE: a kind whose older unacked record is strictly superseded by
    # a newer one is REPLACED IN PLACE rather than appended. Today that is
    # ``Stop`` and only ``Stop`` - see ``_TOAST_SUPERSEDING_KINDS``. Before
    # that rule existed, a session that ran 200 assistant turns without the
    # user dismissing anything held 200 identical "Your turn" records and
    # replayed all 200 on every attach backfill.
    #
    # Color resolution: when a toast is recorded, we read the session's
    # project theme (via ``resolve_project_theme``), then read the theme
    # manifest at ``client/css/themes/<id>/theme.json`` to extract the
    # ``--color-accent`` CSS var. The lookup is memoized per theme id -
    # theme manifests are static files that don't change during a server's
    # uptime, so no invalidation is needed. ``--color-accent`` was chosen
    # because (a) every theme.json sampled defines it, and (b) it's the
    # value already used elsewhere in the client as the session-identity
    # accent. Fall back to None when the theme isn't found or the var is
    # missing - the client CSS has its own ``var(... fallback)`` chain.

    _TOAST_ACKED_CAP = 50

    @staticmethod
    def _themes_dir() -> Path:
        """Return the bundled themes root (``client/css/themes/``).

        Computed from this file's location: session_manager.py lives at
        ``src/core/session_manager.py``, so two ``parent`` hops reach
        the repo root. Mirrors the resolver used in
        ``routes._bundled_themes_root`` - kept duplicated rather than
        cross-imported to avoid a routes <-> session_manager cycle.
        """
        return (
            Path(__file__).resolve().parent.parent.parent
            / "client"
            / "css"
            / "themes"
        )

    def _get_theme_accent_color(self, theme_id: Optional[str]) -> Optional[str]:
        """Resolve the ``--color-accent`` hex/rgba string for a theme id.

        Memoized in ``self._theme_accent_cache`` so repeated toast records
        for the same theme don't pay the JSON parse cost every time.
        Returns None when ``theme_id`` is falsy, the manifest is missing,
        the manifest is malformed, or ``cssVars`` lacks ``--color-accent``.
        """
        if not theme_id:
            return None
        # ``None`` is a valid cached value (theme exists but has no
        # accent var) - distinguish via ``in`` check rather than truthiness.
        if theme_id in self._theme_accent_cache:
            return self._theme_accent_cache[theme_id]

        manifest_path = self._themes_dir() / theme_id / "theme.json"
        accent: Optional[str] = None
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            css_vars = raw.get("cssVars") if isinstance(raw, dict) else None
            if isinstance(css_vars, dict):
                val = css_vars.get("--color-accent")
                if isinstance(val, str) and val.strip():
                    accent = val.strip()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug(
                "toast_theme_accent_read_failed",
                theme_id=theme_id,
                path=str(manifest_path),
                error=str(exc),
            )
            accent = None

        self._theme_accent_cache[theme_id] = accent
        return accent

    def _get_session_accent_color(
        self, session: Optional[Session]
    ) -> Optional[str]:
        """Resolve the per-session accent color via the project theme.

        Returns None when the session is unknown, has no working dir, or
        has no resolvable theme. The hot path is two dict reads + a
        cached lookup once the theme is known - cheap enough to call on
        every ``record_toast`` without batching.
        """
        if session is None:
            return None
        working_dir = getattr(session, "working_dir", None)
        tmux_name = getattr(session, "tmux_session", None)
        theme_id = self.resolve_project_theme(working_dir, tmux_name)
        return self._get_theme_accent_color(theme_id)

    def _prune_toasts(self, session_id: str) -> None:
        """Trim the acked-toasts tail past ``_TOAST_ACKED_CAP``.

        Unacked toasts are preserved unconditionally (every unacked toast
        is potentially surfaceable to a re-attaching browser). Acked
        toasts beyond the cap are dropped from the END of the list
        (newest-first ordering means the tail is the OLDEST acked).
        """
        toasts = self._pending_toasts.get(session_id)
        if not toasts:
            return
        acked_count = 0
        keep: list[Toast] = []
        for t in toasts:
            if t.acknowledged:
                if acked_count < self._TOAST_ACKED_CAP:
                    keep.append(t)
                    acked_count += 1
                # else: drop - past the cap
            else:
                keep.append(t)
        self._pending_toasts[session_id] = keep

    # v0.7.0 Part 4 - Map the WS toast ``kind`` string (the wire-level
    # vocabulary used by the Claude hook endpoint) to a typed EventType
    # so the notification router can fan out to ntfy + Slack. Unmapped
    # kinds (e.g. a future toast kind that doesn't need a push) skip
    # the router emit silently.
    _TOAST_KIND_TO_EVENT_TYPE = {
        "Stop": "CLAUDE_STOP",
        "PermissionRequest": "CLAUDE_PERMISSION_REQUEST",
        "Notification": "CLAUDE_NOTIFICATION",
    }

    # Kinds whose older UNACKED record is strictly superseded by a newer
    # one. Deliberately a one-element set, and the exclusions are the
    # point:
    #
    #   Stop      - matcher "*", no throttle, one per assistant turn,
    #               title always the literal "Your turn". Every older
    #               unacked Stop says the same thing the newest one says,
    #               because "your turn" has been continuously true since
    #               the first of them fired. Nothing is lost.
    #   Notification - the BODY is the message. Two Notifications are two
    #               things to read; collapsing them destroys one.
    #   PermissionRequest - each is a distinct decision about a distinct
    #               command. Superseding one would silently discard a
    #               command the user was never shown. Never collapse a
    #               decision.
    #
    # This mirrors client/js/toast.js COALESCE_KEY exactly, including the
    # keying on title, so server storage and client rendering partition
    # the same set the same way. See record_toast for why they must.
    _TOAST_SUPERSEDING_KINDS = frozenset({"Stop"})

    @staticmethod
    def _find_supersedable_toast(
        bucket: list[Toast], kind: str, title: str
    ) -> Optional[Toast]:
        """Return the record a new ``(kind, title)`` toast should replace.

        Description: Scans a session's toast bucket for an UNACKED record
            of a superseding kind carrying the same title. Returns None
            when the kind does not supersede, when nothing matches, or
            when the only matches are acknowledged.
        Inputs:
            bucket: the session's newest-first list of Toast records.
            kind: wire-level toast kind of the incoming event.
            title: title of the incoming event. Part of the match key so
                this partitions identically to the client's coalesce key.
        Output: the Toast to replace in place, or None to append a new one.

        ACKNOWLEDGED RECORDS ARE NEVER RETURNED. An acked toast is one the
        user dismissed; reusing its id and clearing nothing would still
        leave a record the backfill has already stopped serving, and
        mutating its body would rewrite history the user acted on. A new
        turn after a dismissal is a genuinely new notification and gets a
        new id.

        Example:
            >>> SessionManager._find_supersedable_toast([], "Stop", "Your turn")
        """
        if kind not in SessionManager._TOAST_SUPERSEDING_KINDS:
            return None
        for existing in bucket:
            if (
                existing.kind == kind
                and not existing.acknowledged
                and existing.title == title
            ):
                return existing
        return None

    def _live_session_id_for_stale_id(self, session_id: str) -> Optional[str]:
        """Map a pre-restart session id onto the live one for that pane.

        Description: uses the persisted ``session_id -> tmux name`` map,
          then looks for a CURRENTLY REGISTERED session carrying that
          tmux name. Both halves are required: the first survives the
          restart, the second proves the thing it names is live right
          now.

          Returns None when the name is unknown or nothing live carries
          it. That is a genuine "no such session", distinct from "an id I
          have not seen since the restart", and the caller keeps its
          existing behaviour for it.
        Inputs: session_id (str) - the id the hook presented.
        Output: str | None - a live session id, or None.
        """
        tmux_name = self._hook_tmux_names.get(session_id)
        if not tmux_name:
            return None
        for live_id, sess in self.sessions.items():
            if getattr(sess, "tmux_session", None) == tmux_name:
                logger.info(
                    "toast_session_id_remapped",
                    stale_id=session_id,
                    live_id=live_id,
                    tmux_name=tmux_name,
                )
                return live_id
        return None

    def record_toast(
        self,
        session_id: str,
        kind: str,
        title: str,
        body: Optional[str] = None,
    ) -> Toast:
        """Record a new toast for ``session_id`` and return the Toast.

        Prepends to the per-session list (newest-first). Resolves the
        session's project-theme accent color and bakes it onto the Toast
        so the client can paint a session-colored left border without an
        extra theme lookup on the wire. Caller is responsible for the
        WS broadcast (the route layer does this after calling this method
        - keeps storage and fanout decoupled).

        SUPERSESSION. When the session already holds an UNACKED toast of a
        superseding kind with the same title (``Stop``, and only ``Stop``),
        this REPLACES that record in place and returns it instead of
        appending a second one. The id is deliberately preserved:

          - The client dedupes by id and a coalesced card acks EVERY member
            id on dismiss. A fresh id per turn would leave the browser
            holding ids the server no longer has (their acks land on
            nothing) while the server holds an id the browser never saw,
            which comes straight back on the next attach backfill.
          - The caller broadcasts the returned Toast either way, so the
            client sees one id for one card and the count it renders
            matches the number of records the server actually holds. A
            card reading "x12" over a single stored record is the same
            class of lie as twelve cards over twelve records, just
            pointing the other way.

        The returned Toast is therefore not always newly created. Callers
        must not assume ``toast.id`` is unseen.

        v0.7.0 Part 4 - also emits a ``NotificationEvent`` into the
        attached router (if any) so ntfy + Slack channels fan out from
        the same call site. Emit is best-effort: router missing, kind
        unmapped, or import error all skip the emit without raising.

        Raises:
            ValueError: when ``session_id`` is unknown (we won't record
                toasts for sessions that don't exist - the client would
                have no live WS to receive them on).
        """
        import uuid as _uuid

        session = self.sessions.get(session_id)
        if session is None:
            # THE ID MAY SIMPLY PREDATE A RESTART. The pane's
            # CLOUDECODE_SESSION_ID is baked in at spawn and cannot be
            # re-issued to a running agent, so after a restart the same
            # live session comes back under an adopted id and every hook
            # still presents the old one. The lineage and activity paths
            # already re-map through the persisted tmux name; without the
            # same step here a surviving session keeps its status and
            # lineage but silently loses every toast.
            #
            # This resolves to a session that IS live, so the docstring's
            # reason for raising still holds for the case it was written
            # about: if nothing live carries that name, there is no WS to
            # deliver on and the caller's 410 is correct.
            remapped = self._live_session_id_for_stale_id(session_id)
            if remapped is not None:
                session_id = remapped
                session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Unknown session_id: {session_id!r}")

        color = self._get_session_accent_color(session)
        # WHICH SESSION, IN WORDS. Stamped here because this is the only
        # moment the answer is certain: the session is live and in hand.
        # By render time the browser may be holding a toast for a session
        # it has no row for at all (a different view, a session since
        # destroyed, or an attach backfill replaying history).
        #
        # The name-only label read is the right one HERE and only here.
        # This path has no creation epoch - it is driven by hook events,
        # and probing tmux for a creation time on every hook event is not
        # a cost a notification path can pay. It is bounded: the newest
        # row for the name decides, and the live session is always the
        # newest instance of its own name, so a dead predecessor cannot
        # lend its label. See label_for_name's docstring.
        #
        # NEVER FAILS THE TOAST. A notification must not be lost to a
        # bookkeeping read, so a throwing lookup degrades to "no label",
        # which renders as the tmux name.
        session_name = getattr(session, "tmux_session", None)
        try:
            session_label = self._label_for_tmux_name(session_name)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("toast_label_read_threw", error=str(exc))
            session_label = None
        bucket = self._pending_toasts.setdefault(session_id, [])
        superseded = self._find_supersedable_toast(bucket, kind, title)
        if superseded is not None:
            # REPLACE IN PLACE, KEEPING THE ID. See _find_supersedable_toast
            # for which records qualify and why the id must not change.
            superseded.body = body
            superseded.color = color
            superseded.session_label = session_label
            superseded.session_name = session_name
            superseded.created_at = datetime.utcnow()
            bucket.remove(superseded)
            bucket.insert(0, superseded)  # newest-first
            toast = superseded
            logger.info(
                "toast_superseded",
                session_id=session_id,
                toast_id=toast.id,
                kind=kind,
            )
        else:
            toast = Toast(
                id=_uuid.uuid4().hex,
                session_id=session_id,
                kind=kind,
                title=title,
                body=body,
                color=color,
                session_label=session_label,
                session_name=session_name,
                created_at=datetime.utcnow(),
                acknowledged=False,
            )
            bucket.insert(0, toast)  # newest-first
            logger.info(
                "toast_recorded",
                session_id=session_id,
                toast_id=toast.id,
                kind=kind,
                color=color,
            )
        self._prune_toasts(session_id)

        # v0.7.0 Part 4 - fan out to the notification router (ntfy + Slack).
        # Lazy import to keep the (already-circular-prone) notifications
        # package off the session_manager import chain. Any failure here
        # is best-effort - the toast is already recorded and the WS
        # broadcast happens regardless.
        try:
            if self._notification_router is not None:
                event_type_name = self._TOAST_KIND_TO_EVENT_TYPE.get(kind)
                if event_type_name is not None:
                    import time as _time
                    from src.core.notifications.events import (
                        EventType,
                        NotificationEvent,
                    )
                    event = NotificationEvent(
                        kind=EventType[event_type_name],
                        session_slug=session_id,
                        timestamp=_time.monotonic(),
                        snippet=body or title or "",
                    )
                    self._notification_router.emit(event)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "toast_router_emit_failed",
                session_id=session_id,
                kind=kind,
                error=str(exc),
            )

        return toast

    def record_hook_event(
        self, session_id: str, kind: str, payload: Optional[dict] = None
    ) -> None:
        """Feed one Claude Code lifecycle hook event into the activity model.

        Description: Called by the hook endpoint (``POST
            /hooks/claude-event``) for EVERY event kind - including the
            ones that never produce a toast (PreToolUse/PostToolUse/
            SubagentStart/SubagentStop/UserPromptSubmit). Best-effort by
            design: an unknown ``session_id`` is a documented no-op here
            (the toast path, ``record_toast``, is the one that legitimately
            raises/404s on an unknown session - activity tracking is a
            secondary signal and must never block hook delivery or make
            the endpoint fail for a session that's mid-teardown).
        Inputs:
            session_id: cloudecode session id from the validated hook POST.
            kind: hook event kind (one of
                ``claude_hooks.TOAST_EVENTS + claude_hooks.ACTIVITY_ONLY_EVENTS``).
            payload: the hook's raw JSON body. Unused today (the state
                machine only needs the event kind + arrival time) but
                threaded through for forward-compat and so a future signal
                (e.g. a specific tool name) doesn't require an endpoint
                signature change.
        Output: None.
        Example:
            >>> mgr.record_hook_event("ses_1", "PreToolUse")
        """
        self._activity_tracker.record_event(session_id, kind)
        backend = self.backends.get(session_id)
        tmux_name = getattr(backend, "tmux_session", None) if backend else None
        if not tmux_name:
            # After a restart the id is not in `backends` yet, but the
            # persisted map still knows the name - the same fallback the
            # lineage path uses, and the reason a surviving agent's status
            # keeps being recorded instead of silently stopping.
            tmux_name = self._hook_tmux_names.get(session_id)
        if kind == EVENT_STOP and tmux_name:
            self._unread_store.set_flag(tmux_name, "auto", True)
        self._persist_activity_state(session_id, tmux_name)

    def _persist_settled_activity_state(
        self, tmux_name: Optional[str], state: Optional[str]
    ) -> None:
        """Stamp a display-computed activity state, only when it changed.

        Description: the counterpart to the hook-time stamp. This runs
          from the listing path, which holds a REAL pane status, so it is
          the only place that can honestly record `idle`.

          WRITES ONLY ON CHANGE. The listing runs often; re-stamping an
          unchanged value would be a database write per poll per session,
          and it would also keep refreshing `activity_state_at` on a row
          nothing had happened to - which would defeat the staleness
          check, since a stale value would look perpetually fresh. The
          guard is therefore correctness as much as cost.
        Inputs: tmux_name (str | None). state (str | None).
        Output: None.
        """
        from src.core.session_status import STATUS_UNKNOWN

        if not tmux_name or not state or state == STATUS_UNKNOWN:
            return
        if self._last_persisted_activity.get(tmux_name) == state:
            return
        conn = None
        try:
            from src.core.activity_persist import write_state
            from src.core.db import transaction

            conn = self._writable_datastore_connection()
            if conn is None:
                return
            with transaction(conn):
                if write_state(conn, tmux_name, state):
                    self._last_persisted_activity[tmux_name] = state
        except Exception as exc:  # noqa: BLE001 - never break a listing
            logger.debug("settled_activity_persist_failed", error=str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _restored_activity_state(self, tmux_name: Optional[str]) -> Optional[str]:
        """The durable activity state for a session, if still trustworthy.

        Description: reads ``activity_state`` / ``activity_state_at`` off
          the real-instance row and judges it by age through
          ``activity_persist.restore_state``. Returns None for absent,
          unparseable, stale or ``dead`` - all of which the caller must
          leave as not-measured rather than rounding to ``idle``.
        Inputs: tmux_name (str | None).
        Output: str | None.
        """
        if not tmux_name:
            return None
        conn = None
        try:
            from src.core.activity_persist import restore_state

            conn = self._writable_datastore_connection()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT activity_state, activity_state_at FROM sessions "
                "WHERE tmux_name = ? AND tmux_created_epoch IS NOT NULL "
                "ORDER BY tmux_created_epoch DESC, id DESC LIMIT 1",
                (tmux_name,),
            ).fetchone()
            if not row:
                return None
            state, _reason = restore_state(row[0], row[1])
            return state
        except Exception as exc:  # noqa: BLE001 - a read must not break listing
            logger.debug("activity_state_restore_failed", error=str(exc))
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _persist_activity_state(
        self, session_id: str, tmux_name: Optional[str]
    ) -> None:
        """Stamp the freshly-computed activity state onto the session row.

        Description: makes the hook-derived status DURABLE. Without this,
          the state lives only in ``SessionActivityTracker``, an in-memory
          dict, and a server restart forgets what every session was doing
          - which does not degrade to "unknown" but to a confident
          ``idle``, because the tmux fallback reports a constant under
          this app's launch path.

          Best-effort and silent on failure by design: a status write must
          never be able to fail hook delivery. It writes the state the
          tracker just computed, so the durable value and the live value
          cannot disagree.
        Inputs: session_id (str). tmux_name (str | None).
        Output: None.
        """
        if not tmux_name:
            return
        conn = None
        try:
            from src.core.activity_persist import write_state
            from src.core.session_status import STATUS_UNKNOWN

            # STATUS_UNKNOWN is passed deliberately: at hook time we have
            # not run a tmux probe, and doing one per hook would mean a
            # subprocess on every single tool call.
            #
            # `resolve` only uses the tmux argument for the dead-check and
            # for the final fallback, so a hook-derived state (question,
            # working, working_subagent, finished_unread) comes back
            # unaffected - those are exactly the values worth stamping
            # here, because they are what the hooks actually told us.
            #
            # When the heartbeat has expired it returns STATUS_UNKNOWN,
            # and skipping that write is right: we genuinely cannot tell
            # idle from anything else without tmux. The SETTLED value is
            # stamped by the display path instead, which has a real pane
            # status - see `_persist_settled_activity_state`. Without that
            # counterpart this skip freezes a mid-turn `working` in the
            # row forever, which is what it did when first written.
            state = self._activity_tracker.resolve(
                session_id, STATUS_UNKNOWN, unread=False
            )
            if not state or state == STATUS_UNKNOWN:
                return
            conn = self._writable_datastore_connection()
            if conn is None:
                return
            from src.core.db import transaction

            with transaction(conn):
                write_state(conn, tmux_name, state)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.debug("activity_state_persist_failed", error=str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def ack_toast(self, session_id: str, toast_id: str) -> bool:
        """Mark a toast acknowledged. Idempotent.

        Returns True when the toast was found AND state actually changed
        (i.e. wasn't already acked). Returns False when not found OR
        already acked - useful for the route layer to skip the WS
        broadcast on a no-op double-click.
        """
        bucket = self._pending_toasts.get(session_id)
        if not bucket:
            return False
        for t in bucket:
            if t.id == toast_id:
                if t.acknowledged:
                    return False
                t.acknowledged = True
                self._prune_toasts(session_id)
                logger.info(
                    "toast_acked",
                    session_id=session_id,
                    toast_id=toast_id,
                )
                return True
        return False

    def get_toasts(
        self, session_id: str, unacked_only: bool = False
    ) -> list[Toast]:
        """Return toasts for a session, optionally filtered to unacked.

        Newest-first. Returns an empty list (NOT None) when the session
        has no recorded toasts - callers can iterate without a None check.
        """
        bucket = self._pending_toasts.get(session_id, [])
        if unacked_only:
            return [t for t in bucket if not t.acknowledged]
        return list(bucket)

    # ---- output fan-out (per session) -----------------------------------

    def subscribe_output(self, session_id: Optional[str] = None) -> asyncio.Queue:
        """Subscribe to a session's backend output stream.

        ``session_id`` None → the current session (back-compat). The
        returned queue receives ONLY that session's bytes (base64-encoded
        strings); a session's output never leaks into another's queue.
        """
        sid = self._resolve_session_id(session_id)
        # Tolerate "no session yet" - return an orphan queue so callers
        # (e.g. the auth-only WS test) don't have to special-case it.
        key = sid if sid is not None else "__orphan__"
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(key, []).append(queue)
        return queue

    def unsubscribe_output(
        self, queue: asyncio.Queue, session_id: Optional[str] = None
    ):
        """Unsubscribe a queue from a session's output stream.

        ``session_id`` None → search all buckets (covers callers that
        don't track which session the queue belonged to). Idempotent.
        """
        if session_id is not None:
            subs = self._subscribers.get(session_id)
            if subs and queue in subs:
                subs.remove(queue)
            return
        for subs in self._subscribers.values():
            if queue in subs:
                subs.remove(queue)
                return

    # ---- session lifecycle ----------------------------------------------

    async def flush_pending_terminal_command(self, session_id: str) -> None:
        """Type this session's pending terminal command, once, on attach.

        Description: resolves the pending id against config.json (never
          against anything the client sent) and writes the stored text
          plus a newline into the pane through the SAME
          ``SessionBackend.write()`` used for real keystrokes from the
          browser. Nothing is exec'd here: no subprocess, no shell -c, no
          eval. The user's own interactive shell, in a pane they are
          looking at, interprets the line and they can Ctrl-C it.

          WHY ON ATTACH, NOT AT CREATE: a console is born at 80x24 when
          the launcher has no terminal mounted to measure, and the WS
          handshake then resizes it and sends Ctrl+L to force a repaint
          (see src/api/websocket.py). Anything typed before that point
          runs correctly but is CLEARED off the visible screen by that
          repaint, so the user clicks "run" and lands on a blank prompt.
          Verified live: typed at create, the command ran and its output
          existed only in tmux scrollback. Flushing after the handshake
          puts the output where the user is actually looking.

          Pops the id first, so a reconnect to the same session never
          re-runs the command. Best-effort: a write failure must never
          break an otherwise-good session, so it is logged and swallowed.
        Inputs: session_id (str) - the session being attached to.
        Output: None.
        """
        command_id = self.pending_terminal_commands.pop(session_id, None)
        if not command_id:
            return
        command = settings.get_terminal_command(command_id)
        if command is None:
            return
        backend = self.backends.get(session_id)
        if backend is None:
            return

        payload = (command.command + "\n").encode("utf-8")
        last_error: Optional[str] = None
        for attempt in range(_TERMINAL_COMMAND_WRITE_ATTEMPTS):
            try:
                await backend.write(payload)
                logger.info(
                    "terminal_command_typed",
                    session_id=session_id,
                    command_id=command.id,
                    attempt=attempt,
                )
                return
            except (OSError, RuntimeError, ValueError) as e:
                # A just-started pane can briefly not be addressable yet.
                last_error = str(e)
                await asyncio.sleep(_TERMINAL_COMMAND_WRITE_DELAY_SECONDS)

        logger.warning(
            "terminal_command_type_failed",
            session_id=session_id,
            command_id=command.id,
            error=last_error,
            attempts=_TERMINAL_COMMAND_WRITE_ATTEMPTS,
        )

    async def create_session(
        self,
        session_id: str,
        working_dir: Optional[str] = None,
        auto_start_claude: bool = True,
        copy_templates: bool = False,
        initial_cols: Optional[int] = None,
        initial_rows: Optional[int] = None,
        project_name: Optional[str] = None,
        agent_type: Optional[str] = None,
        model: Optional[str] = None,
        terminal_command_id: Optional[str] = None,
        agent_extra_args: Optional[List[str]] = None,
        label: Optional[str] = None,
    ) -> Session:
        """Create a new Claude Code session.

        ``agent_extra_args`` appends arguments to the resolved agent command
        (see ``Settings.get_agent_command``). It exists for the FORK path,
        which passes ``--resume <uuid> --fork-session`` so the new tmux
        session resumes an existing conversation and branches it. The args
        travel THROUGH the user's own wrapper, not around it, because the
        wrapper is where their auth is set up.

        Multiple sessions coexist - this does NOT raise if other sessions
        are live (the old single-active invariant is gone). A zombie
        session matching this exact ``session_id`` (stale metadata, dead
        backend) is cleaned up first.

        ``initial_cols`` / ``initial_rows`` are forwarded to the backend's
        ``start()`` so the pane is birthed at the client's measured size.
        Both must be supplied together or both omitted; backends fall back
        to their own defaults otherwise. The WS resize handshake reshapes
        later regardless - these are strictly a birth-time optimization.

        ``project_name`` (optional) is the human-readable project label from
        the launchpad. When supplied and non-empty after sanitization, the
        resulting tmux session is named ``cloude_<sanitized name>`` verbatim
        instead of falling back to the legacy ``cloude_ses_<hex>`` derivation
        keyed off ``session_id``. An empty/whitespace-only value (or one that
        sanitizes to empty) silently falls back to legacy naming - this is
        by design so the launchpad can always send the field without special-
        casing blanks. PTYBackend ignores the override entirely.

        ``model`` (provider-selector modal, v3.1) - only meaningful when
        the resolved agent_type is ``"claude"``. None launches Claude
        directly via the ``cld`` zsh function; set launches it OpenRouter-
        routed via ``cldor <model>`` (see ``Settings.get_agent_command``).
        Ignored (harmlessly) for every other agent_type. Persisted on the
        resulting ``Session`` alongside ``agent_type`` regardless of
        ``auto_start_claude``, so it survives for the life of the session
        even on a manual/no-autostart create.

        ``terminal_command_id`` (feat/settings-tabs-and-commands) - id of
        a configured entry in config.json's ``terminal_commands``. When it
        resolves, the stored command text is TYPED into the new pane after
        the shell starts, via the existing ``SessionBackend.write()``
        (tmux ``send-keys``) - the same path a keystroke from the user's
        browser takes. It is never exec'd by this process and never passed
        to a shell by us, which is why an id (not a command string) is
        what crosses the API boundary; see
        ``src/core/terminal_commands.py``. An unknown id is ignored and
        the session is just a plain console.
        """
        # Clean up a zombie entry for this exact id (stale metadata / dead
        # backend) - but leave any OTHER live sessions alone.
        if session_id in self.sessions and (
            session_id not in self.backends
            or not self.backends[session_id].is_alive()
        ):
            logger.info("cleaning_up_zombie_session", session_id=session_id)
            self._wipe_session_state(session_id)

        # Phase 6 - resolve effective agent_type. Precedence:
        #   1. explicit ``agent_type`` kwarg (request-level override)
        #   2. project-level default (ProjectConfig.agent_type) when
        #      ``project_name`` matches a configured project
        #   3. ``"claude"`` as the final safe default
        # Resolved value is persisted on the Session and drives
        # ``settings.get_agent_command(...)`` for the launch string.
        resolved_agent_type: Optional[str] = agent_type
        if not resolved_agent_type and project_name:
            try:
                # feat/db-is-authoritative: the projects table, with a
                # config.json fallback when the datastore is unreachable,
                # rather than reading config.json directly.
                from src.api import projects_service

                resolved_agent_type = projects_service.agent_type_for(
                    settings, project_name
                )
            except Exception:
                # Don't fail session create if the lookup misbehaves.
                resolved_agent_type = None
        if not resolved_agent_type:
            resolved_agent_type = "claude"

        # Determine working directory
        if working_dir:
            work_path = Path(working_dir).expanduser()
        else:
            work_path = settings.get_working_dir() / session_id

        work_path.mkdir(parents=True, exist_ok=True)

        logger.info(
            "creating_session",
            session_id=session_id,
            working_dir=str(work_path),
            copy_templates=copy_templates
        )

        # Copy template files if requested
        if copy_templates:
            try:
                auth_config = settings.load_auth_config()
                if auth_config.template_path:
                    success, error = copy_template_files(
                        auth_config.template_path,
                        str(work_path)
                    )
                    if success:
                        logger.info("templates_copied_to_session", path=str(work_path))
                    else:
                        logger.warning("template_copy_failed", error=error)
                else:
                    logger.warning("no_template_path_configured")
            except Exception as e:
                logger.error("template_copy_error", error=str(e))

        # Derive a verbatim tmux session-name override from project_name when
        # supplied. Empty sanitized result → None (fall back to legacy hex
        # naming via the backend's own slug derivation from session_id).
        tmux_session_name: Optional[str] = None
        if project_name:
            # Defensive idempotency: if an older client (or stale Recent
            # Project entry) hands us a name that already begins with the
            # tmux namespace prefix, strip ALL leading copies before we
            # prepend our own. Prevents `cloude_cloude_*` regressions.
            stripped = project_name
            while stripped.startswith(SESSION_PREFIX):
                stripped = stripped[len(SESSION_PREFIX):]
            sanitized = _sanitize_tmux_name(stripped)
            if sanitized:
                tmux_session_name = f"{SESSION_PREFIX}{sanitized}"

        # Uniquify-on-collision: a project click must ALWAYS spawn a NEW
        # session against the working_dir, never adopt/reuse an existing
        # one - the user runs multiple concurrent sessions per directory.
        # If the derived name is already taken, append a numeric suffix
        # (``cloude_foo`` -> ``cloude_foo-2`` -> ``cloude_foo-3`` ...)
        # until we find one that's free against EVERY source of truth:
        #   - the live tmux socket (probe.discover_existing()) - tmux
        #     itself hard-fails "duplicate session" on collision
        #   - self.active_tmux_names() - in-memory live backends
        #   - self.owned_tmux_sessions - persisted names we've taken,
        #     including detached-but-not-destroyed sessions
        # This mirrors rename_session's collision check (active OR
        # owned_tmux_sessions) so a name minted here can never be
        # rejected by the rename guard later. Explicit attach-to-a-
        # specific-session still goes through adopt_external_session
        # via the running-sessions list - untouched by this path.
        if tmux_session_name:
            probe = build_backend(
                settings,
                session_id="__collision_probe__",
                working_dir=Path.home(),
                on_output=None,
            )
            # A failed probe here is SAFE to treat as an empty
            # contribution, unlike the reconciler above, because this set
            # is only ever UNIONed with ``owned_tmux_sessions`` and the
            # live-backend names to decide whether a name is taken. The
            # worst case is that we mint a name tmux already holds, and
            # tmux itself hard-fails "duplicate session" on create - a
            # loud, immediate, recoverable error, not silent data loss.
            # It is still logged at WARN so the degraded input is visible.
            try:
                collision_listing = coerce_listing(probe.discover_existing())
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("collision_probe_failed", error=str(exc))
                collision_listing = TmuxListing.unavailable(
                    "probe_error", detail=str(exc)
                )
            if not collision_listing.ok:
                logger.warning(
                    "collision_probe_unavailable",
                    reason=collision_listing.reason,
                    note=(
                        "name uniquification is falling back to owned + "
                        "live-backend names only; tmux will still reject "
                        "a genuine duplicate at create time"
                    ),
                )
            tmux_existing = set(collision_listing.names)
            taken = tmux_existing | self.active_tmux_names() | self.owned_tmux_sessions

            base_name = tmux_session_name
            if base_name in taken:
                MAX_SUFFIX_ATTEMPTS = 999
                candidate = base_name
                for suffix in range(2, MAX_SUFFIX_ATTEMPTS + 2):
                    candidate = f"{base_name}-{suffix}"
                    if candidate not in taken:
                        break
                else:
                    raise RuntimeError(
                        f"Could not find a free tmux session name for "
                        f"{base_name!r} after {MAX_SUFFIX_ATTEMPTS} attempts"
                    )
                logger.info(
                    "session_create_name_uniquified",
                    project=project_name,
                    base_tmux_name=base_name,
                    uniquified_tmux_name=candidate,
                )
                tmux_session_name = candidate

        backend: Optional[SessionBackend] = None
        new_session: Optional[Session] = None
        idle_watcher: Optional[IdleWatcher] = None
        try:
            # Build a fresh backend for the new session. Its on_output is
            # bound to THIS session_id so its bytes only fan out to this
            # session's subscribers.
            backend = build_backend(
                settings,
                session_id=session_id,
                working_dir=work_path,
                on_output=self._make_output_handler(session_id),
                session_name=tmux_session_name,
            )

            # v0.7.0 Part 3 - mint the per-session hook token BEFORE the
            # tmux spawn so we can inject CLOUDECODE_* env vars into the
            # new-session call. TmuxBackend merges ``env`` into the tmux
            # process's environment, which the spawned agent inherits.
            # PTYBackend's start() signature also accepts ``env`` (or
            # ignores extra kwargs - see backend) so this is safe across
            # backend types.
            self._mint_hook_token(session_id, tmux_name=tmux_session_name)
            spawn_env = self.get_env_for_spawn(session_id)

            if auto_start_claude:
                # Phase 6 - resolve via the agents map. For claude with
                # default config this yields the same string the old
                # ``f"{claude_cli} --dangerously-skip-permissions"`` did
                # (CLAUDE_CLI_PATH env-fallback preserved inside the helper).
                # Give Claude its name at BIRTH when we already know it.
                # This is the risk-free half of name syncing: a launch
                # flag interrupts nothing, unlike `/rename`, which has to
                # be typed into a live pane and is therefore gated on
                # that pane being idle.
                #
                # Gated on the family that will ACTUALLY run, not on the
                # agent_type string: `--name` is a claude-family flag, and
                # handing it to codex or shell does not degrade the
                # launch, it breaks it. (The cldl picker defect was this
                # same mistake - a per-family capability read off the
                # wrong object.)
                from src.core.claude_rename import (
                    launch_name_args_for_agent_type,
                )

                name_args = launch_name_args_for_agent_type(
                    label=label, agent_type=resolved_agent_type
                )
                command = settings.get_agent_command(
                    resolved_agent_type,
                    model=model,
                    extra_args=(agent_extra_args or []) + name_args,
                )
                await backend.start(
                    command=command,
                    env=spawn_env,
                    initial_cols=initial_cols,
                    initial_rows=initial_rows,
                )
            else:
                await backend.start(
                    env=spawn_env,
                    initial_cols=initial_cols,
                    initial_rows=initial_rows,
                )

            # Recorded, not typed yet - it is flushed when the client
            # attaches, so the handshake's repaint cannot clear the output.
            # See flush_pending_terminal_command.
            if terminal_command_id:
                self.pending_terminal_commands[session_id] = terminal_command_id

            # PID for metadata: both backends expose `.pid` now.
            # PTYBackend tracks a single forked pid for the process
            # lifetime; TmuxBackend.pid queries the pane's CURRENT
            # foreground pid via `tmux display-message -p '#{pane_pid}'`
            # (see src/core/tmux_backend.py). getattr's default stays as
            # defense-in-depth for any future backend that omits `.pid`.
            pid = getattr(backend, "pid", None)

            # v0.7.0 - seed pinned_theme from ``<work_path>/.cc.theme`` (or
            # legacy ``pinned_themes.json`` for the tmux name when no
            # dotfile exists). New projects without a pin yield None,
            # which is the original behavior.
            prior_pin = self.resolve_project_theme(work_path, tmux_session_name)
            new_session = Session(
                id=session_id,
                pty_pid=pid,
                working_dir=str(work_path),
                status=SessionStatus.RUNNING,
                created_at=datetime.utcnow(),
                last_activity=datetime.utcnow(),
                agent_type=resolved_agent_type,
                model=model,
                pinned_theme=prior_pin,
                # PIN-FIX-EXECUTE - carry the bare tmux name on the inner
                # Session so frontend can use it as the pin-key handle
                # without falling back to session.id.
                tmux_session=tmux_session_name,
            )

            self._register_session(new_session, backend)
            # Best-effort: ferry any legacy pinned_themes.json entry into
            # the dotfile so subsequent restarts read from the new source
            # of truth. Read-then-migrate ordering keeps the read above
            # deterministic when both exist.
            try:
                self.migrate_pinned_theme_to_dotfile(new_session)
            except Exception as exc:  # pragma: no cover - helper swallows
                logger.debug(
                    "post_create_migrate_unexpected_throw", error=str(exc)
                )

            # Track 1: record tmux-backend ownership so a post-create crash
            # still leaves the name recoverable from ``session_metadata.json``
            # - and the adopt UI correctly flags it as ``created_by_cloude``.
            owned_name = getattr(backend, "tmux_session", None)
            if owned_name:
                self.owned_tmux_sessions.add(owned_name)

            self._save_session_metadata(new_session)

            # THE AUTHORITY. The two lines above are tiers 3 and below:
            # a set that dies with this process, and a file the DB
            # outranks. ``sessions`` is what the badge is computed from
            # (src/models.py:346), and until this call it was never told
            # that the launcher had made anything - which is why every
            # launcher-created session read EXTERNAL after a restart.
            #
            # Runs LAST on purpose. It needs tmux's #{session_created}
            # for the instance key, which does not exist until the spawn
            # above succeeded, and it must not be able to fail the
            # creation of a session that is already live and working.
            # persist_creation never raises; it returns a named outcome,
            # and an unrecorded session is a repairable degraded state
            # while a fabricated row is not repairable by anyone.
            create_persist_outcome = None
            if owned_name:
                # THE PROVENANCE HANDOFF. ``resolved_agent_type`` was
                # decided in phase 6 above and ``auto_start_claude``
                # decided whether its command was actually executed.
                # Both were already on the in-memory Session and in
                # session_metadata.json; the AUTHORITY - the sessions
                # table - was never told, which is why every
                # launcher-created row carried agent_type NULL and the
                # UI fell through to a scrollback guess for a session
                # this app started itself.
                persisted = self.persist_creation(
                    owned_name,
                    working_dir=str(work_path),
                    agent_type=resolved_agent_type,
                    agent_launched=bool(auto_start_claude),
                )
                create_persist_outcome = persisted.outcome
                if not persisted.recorded:
                    logger.warning(
                        "session_created_not_attributed",
                        session_id=session_id,
                        tmux_name=owned_name,
                        outcome=persisted.outcome,
                        detail=persisted.detail,
                        note=(
                            "the session is LIVE; only its ownership row "
                            "is missing, so it will badge external until "
                            "it is recorded or adopted"
                        ),
                    )
            else:
                # Not a tmux-backed session, so there is no instance
                # triple for the sessions table to key on. Named rather
                # than silent: "the question does not arise" is a
                # different answer from "the write failed".
                from src.core.session_create_persist import (
                    CREATE_NOT_TMUX_BACKED,
                )

                create_persist_outcome = CREATE_NOT_TMUX_BACKED

            # Item 7: spin up the per-session IdleWatcher. Skipped silently
            # when the router hasn't been attached (e.g. in tests that
            # exercise SessionManager without a full app lifespan) so the
            # session lifecycle doesn't break.
            if self._notification_router is not None:
                try:
                    auth_config = settings.load_auth_config()
                    threshold = getattr(
                        auth_config.notifications,
                        "idle_threshold_seconds",
                        30.0,
                    )
                except Exception:
                    threshold = 30.0
                idle_watcher = IdleWatcher(
                    session_slug=session_id,
                    router=self._notification_router,
                    threshold_s=threshold,
                )
                await idle_watcher.start()
                self.idle_watchers[session_id] = idle_watcher

            logger.info(
                "session_created",
                session_id=session_id,
                pid=pid,
                backend=type(backend).__name__.replace("Backend", "").lower(),
                # Say on the same line whether the authority learned about
                # this session. A `session_created` with no attribution
                # outcome beside it is exactly the false green this fix
                # exists to remove.
                origin_persist=create_persist_outcome,
            )

            return new_session

        except PTYSessionError as e:
            logger.error("session_creation_failed", error=str(e))
            await self._cleanup_failed_create(session_id, backend, idle_watcher)
            raise ValueError(f"Failed to create session: {e}") from e
        except RuntimeError as e:
            # Backend.start() raises RuntimeError for hard infrastructure
            # failures: tmux missing on PATH, ``new-session`` non-zero exit,
            # OR - added in the dead-on-arrival probe - when the spawned
            # agent process exits immediately and tmux's remain-on-exit
            # would otherwise leave the user staring at a frozen welcome
            # screen. Preserve the type (do NOT rewrap as ValueError) so
            # the route layer can return 502 Bad Gateway with the original
            # message visible to the client.
            logger.error("session_creation_failed_runtime", error=str(e))
            await self._cleanup_failed_create(session_id, backend, idle_watcher)
            raise
        except Exception as e:
            logger.error("session_creation_failed", error=str(e))
            await self._cleanup_failed_create(session_id, backend, idle_watcher)
            raise ValueError(f"Failed to create session: {e}") from e

    async def _cleanup_failed_create(
        self,
        session_id: str,
        backend: Optional[SessionBackend],
        idle_watcher: Optional[IdleWatcher],
    ) -> None:
        """Tear down a half-built session after ``create_session`` failed.

        Stops the backend + idle watcher (best-effort) and wipes any
        per-session state that ``_register_session`` may have written.
        Never touches another session's state.
        """
        # If the session was registered before the failure, mark it errored
        # for any in-flight observer, then wipe it.
        sess = self.sessions.get(session_id)
        if sess is not None:
            sess.status = SessionStatus.ERROR
        if backend is not None:
            try:
                await backend.stop()
            except Exception:
                pass
        iw = idle_watcher or self.idle_watchers.get(session_id)
        if iw is not None:
            try:
                await iw.stop()
            except Exception:
                pass
        self._wipe_session_state(session_id)

    async def detach_current_session(
        self, session_id: Optional[str] = None
    ) -> bool:
        """Detach from a session's backend WITHOUT killing tmux.

        ``session_id`` None → the current (most-recent) session. This is
        the "soft" counterpart to ``destroy_session``: it tears down the
        Python-side handles (reader task, idle watcher, backend ref, output
        subscribers, stashed offset) for THAT session ONLY and stops its
        pipe-pane so the server-side tmux session can be cleanly re-adopted
        later - but it leaves the tmux session itself alive. Other live
        sessions are untouched.

        Why stop pipe-pane here (vs leaving it attached): our pipe-pane
        writes into ``tmux_<slug>.pipe``; the subsequent re-adopt via
        ``TmuxBackend.for_external`` derives its pipe path as
        ``tmux_ext_<slug>.pipe`` - a DIFFERENT file. If we leave the old
        pipe-pane active, the re-adopt's ``ensure_pipe_pane`` sees
        ``#{pane_pipe} == 1`` and refuses to clobber it, then the tailer
        opens the new (empty) path and silently streams nothing.

        On-disk metadata is unlinked when the detached session was the
        persisted one, so a restart doesn't silently auto-rehydrate it
        (it'll surface in the Adopt list instead). ``owned_tmux_sessions``
        is left intact.

        Returns False (no-op) when the session isn't live. True otherwise.
        """
        sid = self._resolve_session_id(session_id)
        backend = self.backends.get(sid) if sid else None
        if not sid or backend is None:
            logger.info("detach_current_session_noop")
            return False

        logger.info("detaching_session", session_id=sid)

        try:
            # Tear down the idle watcher first - mirrors destroy ordering so
            # a trailing poll iteration can't fire after the backend is gone.
            iw = self.idle_watchers.get(sid)
            if iw is not None:
                try:
                    await iw.stop()
                except Exception as exc:
                    logger.warning(
                        "idle_watcher_stop_error_on_detach", error=str(exc)
                    )

            # Cancel the backend's reader task so no more pipe bytes land
            # in the fan-out after detach. TmuxBackend.stop() does this as
            # part of its shutdown; we mirror only the reader teardown.
            reader_task = getattr(backend, "_reader_task", None)
            if reader_task is not None:
                try:
                    reader_task.cancel()
                    try:
                        await reader_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.debug(
                            "reader_task_teardown_error_on_detach",
                            error=str(exc),
                        )
                except Exception as exc:
                    logger.debug(
                        "reader_task_cancel_error_on_detach", error=str(exc)
                    )
                try:
                    backend._reader_task = None
                except Exception:
                    pass

            # Stop OUR pipe-pane so a subsequent re-adopt can cleanly set up
            # its own pipe at the (different) external-path. Best-effort.
            try:
                if hasattr(backend, "_run_tmux"):
                    from src.core.tmux_backend import _safe_target
                    target_name = getattr(backend, "tmux_session", None)
                    if target_name:
                        await backend._run_tmux(
                            "pipe-pane",
                            "-t",
                            _safe_target(target_name),
                            check=False,
                        )
                try:
                    backend._running = False
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(
                    "pipe_pane_stop_failed_on_detach", error=str(exc)
                )

            was_persisted = (self.current_session() is not None and
                             self.current_session().id == sid)
            # Wipe THIS session's state only - leave tmux alive, leave
            # other sessions alone.
            self._wipe_session_state(sid)

            # Unlink on-disk metadata only when the detached session was the
            # one persisted (so a restart doesn't auto-rehydrate it).
            if was_persisted:
                metadata_path = settings.get_session_metadata_path()
                try:
                    if metadata_path.exists():
                        metadata_path.unlink()
                except OSError as exc:
                    logger.warning(
                        "session_metadata_unlink_failed_on_detach",
                        error=str(exc),
                    )
                # If another session is still around, persist that one so a
                # restart rehydrates *something* live rather than nothing.
                if self.current_session() is not None:
                    self._save_session_metadata()

            logger.info("session_detached", session_id=sid)
            return True

        except Exception as e:
            logger.error("session_detach_failed", error=str(e))
            raise

    async def destroy_session(self, session_id: Optional[str] = None) -> bool:
        """Destroy a session (kill its backend / tmux). ``session_id`` None
        → the current session. Only touches THAT session's state - other
        live sessions are untouched.
        """
        sid = self._resolve_session_id(session_id)
        if not sid:
            raise ValueError("No session to destroy")
        sess = self.sessions.get(sid)
        backend = self.backends.get(sid)
        if sess is None:
            raise ValueError("No session to destroy")

        logger.info("destroying_session", session_id=sid)

        try:
            # Item 7: tear down the watcher FIRST so no poll iteration races
            # with the pending backend shutdown.
            iw = self.idle_watchers.get(sid)
            if iw is not None:
                try:
                    await iw.stop()
                except Exception as exc:
                    logger.warning("idle_watcher_stop_error", error=str(exc))

            # Track 1: drop ownership record BEFORE we lose the backend handle.
            owned_name = getattr(backend, "tmux_session", None) if backend else None
            if owned_name:
                self.owned_tmux_sessions.discard(owned_name)
                # SESSION-IDENTITY-V2 - explicit destroy means this name is
                # dead; drop its pin too.
                self.discard_pinned_theme(owned_name)

            if backend is not None:
                await backend.stop()

            sess.status = SessionStatus.STOPPED

            # Layer 1 of upload cleanup (see upload_sweeper's module
            # docstring). This is a RECURSIVE delete of an operator-supplied
            # path, so it goes through the same three-question verdict the
            # background sweeper uses rather than trusting exists(): a
            # session's working_dir is a real project directory, and under
            # test it is whatever the fixture happened to leave behind.
            # ignore_errors=True used to make any refusal invisible.
            working_dir = sess.working_dir
            if working_dir:
                verdict = sweep_verdict(working_dir)
                if verdict.outcome is SweepOutcome.SWEEP:
                    shutil.rmtree(verdict.bucket, ignore_errors=True)
                    logger.info(
                        "upload_dir_cleaned_on_destroy",
                        path=str(verdict.bucket),
                    )
                elif verdict.outcome is SweepOutcome.REFUSED:
                    logger.warning(
                        "upload_dir_cleanup_refused",
                        working_dir=str(working_dir),
                        reason=verdict.reason,
                    )

            was_persisted = (self.current_session() is not None and
                             self.current_session().id == sid)

            self._wipe_session_state(sid)

            # Metadata holds the most-recently-active session. If we just
            # destroyed it, either re-point metadata at another live session
            # or unlink the file entirely.
            if was_persisted:
                if self.current_session() is not None:
                    self._save_session_metadata()
                else:
                    metadata_path = settings.get_session_metadata_path()
                    if metadata_path.exists():
                        metadata_path.unlink()

            logger.info("session_destroyed", session_id=sid)
            return True

        except Exception as e:
            logger.error("session_destruction_failed", error=str(e))
            raise

    def _is_live_tmux_name(self, candidate: str) -> bool:
        """Whether ``candidate`` names a tmux session that exists now.

        Description: lets a label write accept a tmux NAME where it used
          to demand an app session id. Returns False on any listing
          failure - a name that cannot be confirmed is not treated as
          confirmed, so the caller reports a definite failure rather than
          writing against a guess.
        Inputs: candidate (str).
        Output: bool.
        """
        if not candidate:
            return False
        try:
            _socket, listing = self.list_attachable_sessions_with_socket()
            if not getattr(listing, "ok", False):
                return False
            return any(
                (row or {}).get("name") == candidate
                for row in (getattr(listing, "sessions", None) or [])
            )
        except Exception as exc:  # noqa: BLE001 - a probe must not raise here
            logger.debug("live_tmux_name_check_failed", error=str(exc))
            return False

    def set_session_label(self, session_id: str, label: str) -> bool:
        """Store the user-facing LABEL for one session. No tmux involved.

        Description: what the rename surface calls now. A label is a
          display string on the row; it is not the tmux session name and
          writing it touches no identity column, so a rename can no
          longer move ``tmux_name`` out from under the identity key and
          split one session into two rows.

          The tmux name and the creation epoch are read to KEY the write
          and are never modified. The epoch comes from a fresh listing
          because that is the only place it exists - the row is keyed on
          it, so a label write needs it just as much as a create does.

          NEVER RAISES for a data problem. A missing session, an
          unavailable datastore or a listing that could not answer all
          return False, which the route renders as a definite failure.
          An invalid label is the one exception, and it is raised before
          anything is read.
        Inputs: session_id (str) - the app's session id, not a tmux name.
          label (str) - the user's label; validated before any read.
        Output: bool - True only when a row now carries that label.
        Raises: InvalidLabel - the label cannot be stored.
        Example: mgr.set_session_label('abc123', 'Media Compression')
        """
        from src.core.session_label import set_label_for_instance, validate_label

        validate_label(label)

        sess = self.sessions.get(session_id)
        tmux_name = getattr(sess, "tmux_session", None) if sess else None
        if not tmux_name:
            # NOT REQUIRING A LIVE SESSION ANY MORE. This used to be the
            # end of the road, and it was the reason the rename pencil was
            # greyed out on every row the app had not adopted - including
            # a user's own running sessions, all seven of them at once
            # after a restart. The precondition was real for the ORIGINAL
            # design, where renaming meant typing into the pane: you
            # cannot type into a backend you do not hold.
            #
            # Rename is out-of-band now. It needs a tmux name to key the
            # row and a claude uuid to address the conversation, and both
            # are readable from the datastore and the live listing without
            # the session being open. So an id we do not hold is no longer
            # a dead end - it may simply BE a tmux name.
            tmux_name = self._hook_tmux_names.get(session_id)
        if not tmux_name and self._is_live_tmux_name(session_id):
            tmux_name = session_id
        if not tmux_name:
            logger.warning(
                "session_label_not_tmux_backed",
                session_id=session_id,
                note=(
                    "no tmux name for this session, so there is no "
                    "instance triple to key a label on"
                ),
            )
            return False

        probe_socket, listing = self.list_attachable_sessions_with_socket()
        socket = probe_socket or self._tmux_socket_name()
        if not listing.ok:
            logger.warning(
                "session_label_listing_unavailable",
                session_id=session_id,
                listing_reason=listing.reason,
                note=(
                    "the creation epoch could not be read, so the row "
                    "cannot be keyed. Nothing written - a label aimed at "
                    "a guessed instance is worse than no label"
                ),
            )
            return False

        from src.core.session_adopt_persist import find_live_instance

        live = find_live_instance(listing, tmux_name)
        if live is None:
            return False
        epoch = live.get("created_at_epoch")

        conn = self._writable_datastore_connection()
        if conn is None:
            return False
        try:
            from src.core.db import transaction

            with transaction(conn):
                stored = set_label_for_instance(
                    conn,
                    socket=socket,
                    name=tmux_name,
                    epoch=epoch,
                    label=label,
                )
            # The label is OURS and is already durable at this point. The
            # push below is a courtesy that keeps Claude's own name for
            # the session in step, and it is deliberately outside the
            # transaction and deliberately unable to fail the rename: a
            # pane that is busy, dead or unreadable must not turn a
            # successful rename into an error the user has to think about.
            if stored:
                self._push_rename_to_claude(session_id, tmux_name, label)
            return stored
        except sqlite3.Error as exc:
            logger.warning(
                "session_label_write_failed",
                session_id=session_id,
                error=str(exc),
            )
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _push_rename_to_claude(
        self, session_id: str, tmux_name: str, label: str
    ) -> str:
        """Rename the Claude conversation OUT OF BAND, never via the pane.

        Description: runs `claude -p --resume <uuid> "/rename <label>"` as
          a short-lived subprocess. It never touches the tmux pane, so
          there is no state of the session in which it could be misread
          as input - which is what the previous design needed an activity
          gate for, and why a rename used to be skipped whenever Claude
          was busy. Measured against a live running session: 0.7s, no
          fork, same uuid, session responsive immediately after.

          NEVER RAISES and never changes the caller's result. The label is
          already stored before this runs; this only keeps Claude's own
          name in step.

          Note what it does NOT do: refresh the running UI. The live
          process holds its title in memory, so the new name shows on
          resume and in the /resume picker. That is the accepted cost of
          not typing into a pane somebody is using.
        Inputs: session_id (str). tmux_name (str) - kept for logging.
          label (str) - the user's new label.
        Output: str - one of the ``claude_rename.PUSH_*`` outcomes.
        Example: mgr._push_rename_to_claude('abc', 'cloude_x', 'Spike')
        """
        from src.core.claude_rename import (
            OOB_TIMEOUT_SECONDS,
            PUSH_SENT,
            decide_push,
            detect_claude_version,
            oob_rename_argv,
        )

        try:
            sess = self.sessions.get(session_id)
            family = getattr(sess, "agent_family", None) if sess else None
            claude_uuid = self._claude_uuid_for_tmux_name(tmux_name)
            outcome, reason = decide_push(
                label=label,
                claude_uuid=claude_uuid,
                claude_version=detect_claude_version(),
                is_claude_session=(family in (None, "claude")),
            )
            if outcome != PUSH_SENT:
                logger.info(
                    "claude_rename_not_pushed",
                    session_id=session_id,
                    outcome=outcome,
                    reason=reason,
                    note="the CloudeCode label is stored either way",
                )
                return outcome

            from src.core.server_status import collect_claude_cli

            info = collect_claude_cli() or {}
            claude_path = info.get("path")
            if not claude_path:
                return "deferred"

            import subprocess

            argv = oob_rename_argv(claude_path, claude_uuid, label)
            # ARGV, NOT A SHELL STRING. The label is user text and may
            # contain quotes or $(...); passing it as an argv element
            # means no shell parses it at all.
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info(
                "claude_rename_pushed",
                session_id=session_id,
                reason=reason,
                timeout_s=OOB_TIMEOUT_SECONDS,
            )
            return outcome
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning(
                "claude_rename_push_threw",
                session_id=session_id,
                error=str(exc),
            )
            return "deferred"

    def _claude_uuid_for_tmux_name(self, tmux_name: str) -> Optional[str]:
        """The bound Claude conversation uuid for a tmux session name.

        Description: reads the newest REAL INSTANCE row for the name - one
          with an epoch - so a `/clear` conversation row (epoch NULL)
          cannot lend its uuid to a rename aimed at the session. Returns
          None when nothing is bound yet, which the caller reports as a
          deferral rather than guessing.
        Inputs: tmux_name (str).
        Output: str | None.
        """
        conn = None
        try:
            conn = self._writable_datastore_connection()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT claude_session_uuid FROM sessions "
                "WHERE tmux_name = ? AND tmux_created_epoch IS NOT NULL "
                "AND claude_session_uuid IS NOT NULL "
                "ORDER BY tmux_created_epoch DESC, id DESC LIMIT 1",
                (tmux_name,),
            ).fetchone()
            return row[0] if row else None
        except sqlite3.Error as exc:
            logger.debug("claude_uuid_lookup_failed", error=str(exc))
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    async def rename_session(
        self, session_id: str, new_name: str
    ) -> "SessionInfo":
        """Rename a live session's tmux backend AND re-key in-memory state.

        Validates uniqueness against every live backend's tmux name
        (``active_tmux_names()``) AND the persisted ``owned_tmux_sessions``
        set - a name collision against either is a conflict.

        On success the following state is updated atomically (from the
        caller's perspective; we hold no async lock since SessionManager
        is single-threaded under the asyncio event loop):

          * ``TmuxBackend.tmux_session`` -> new_name (via ``backend.rename_session``)
          * ``Session.tmux_session`` -> new_name
          * ``owned_tmux_sessions`` re-keyed (drop old, add new) for owned
            sessions. Adopted sessions keep their ``adopted:<old_name>`` id
            but the in-memory ``Session.tmux_session`` carries the new name.
          * ``pinned_themes`` map re-keyed (drop old, add new) so a v0.6.x
            downgrade still finds the pin under the new name. v0.7.0 themes
            live in ``<working_dir>/.cc.theme`` keyed by cwd, so the
            authoritative project theme is unaffected by the rename.
          * ``session_metadata.json`` re-persisted so a restart rehydrate
            sees the new name.

        Args:
            session_id: Session id (NOT tmux name). Returns ValueError if
                unknown / not running.
            new_name: Already-validated tmux name (route layer enforces the
                charset). We re-validate against the live tmux landscape
                here for uniqueness.

        Returns:
            Updated ``SessionInfo`` reflecting the new name.

        Raises:
            ValueError: Unknown session id, or session not running.
            FileExistsError: ``new_name`` collides with another live tmux
                name or an owned-but-detached session name.
            RuntimeError: ``tmux rename-session`` failed at the backend.
        """
        sess = self.sessions.get(session_id)
        backend = self.backends.get(session_id)
        if sess is None or backend is None:
            raise ValueError(f"Unknown session id: {session_id!r}")

        old_name = getattr(backend, "tmux_session", None)
        if not old_name:
            raise ValueError(
                f"Session {session_id!r} has no tmux name to rename"
            )

        # No-op rename (idempotent contract): treat as success without
        # touching tmux or persisting anything.
        if new_name == old_name:
            info = self._session_info_for(session_id)
            if info is None:
                raise ValueError(f"Session {session_id!r} not running")
            return info

        # Uniqueness: collision against ANY live backend's tmux name OR an
        # owned name persisted from a prior session (e.g. detached but
        # not destroyed). The persisted set is the durable source of
        # truth for "names we've taken"; live ``active_tmux_names`` is
        # the runtime source for "names we're holding right now".
        active = self.active_tmux_names()
        if new_name in active or new_name in self.owned_tmux_sessions:
            raise FileExistsError(
                f"Tmux session name {new_name!r} is already in use"
            )

        # Backend handles the actual ``tmux rename-session`` + updates its
        # own ``self.tmux_session``. We re-key in-memory state after.
        await backend.rename_session(new_name)

        # Re-key owned set (idempotent ``discard`` + ``add``). Adopted
        # sessions aren't in this set - only owned ones are persisted -
        # so this is a no-op for adopt rows. We still ALWAYS add ``new_name``
        # only when the OLD name was in the set, so an adopt-then-rename
        # doesn't accidentally promote an external session into the owned
        # registry.
        if old_name in self.owned_tmux_sessions:
            self.owned_tmux_sessions.discard(old_name)
            self.owned_tmux_sessions.add(new_name)

        # Re-key the deprecated pinned-themes map. v0.7.0's project theme
        # ``.cc.theme`` is keyed by working_dir (unaffected by rename), but
        # the legacy per-tmux-name JSON map needs to follow the name so a
        # downgrade-to-v0.6.x doesn't lose the pin. ``self.pinned_themes``
        # is the in-memory mirror of that file.
        if old_name in self.pinned_themes:
            theme_id = self.pinned_themes.pop(old_name)
            self.pinned_themes[new_name] = theme_id
            self._save_pinned_themes()

        # Mirror the new tmux name onto the Session record so SessionInfo
        # serialization picks it up immediately (and so a restart-rehydrate
        # path sees the right name in session_metadata.json).
        sess.tmux_session = new_name

        # Persist. Best-effort - a write failure logs but doesn't roll the
        # tmux rename back, since the tmux side already succeeded and the
        # in-memory state is authoritative until the next restart anyway.
        try:
            self._save_session_metadata(sess)
        except Exception as exc:
            logger.warning(
                "rename_session_metadata_persist_failed",
                session_id=session_id,
                old=old_name,
                new=new_name,
                error=str(exc),
            )

        logger.info(
            "session_renamed",
            session_id=session_id,
            old=old_name,
            new=new_name,
        )

        info = self._session_info_for(session_id)
        if info is None:
            raise ValueError(f"Session {session_id!r} not running")
        return info

    # ---- I/O (per session) ----------------------------------------------

    def _require_running(self, session_id: Optional[str]):
        """Return (sid, session, backend) for a RUNNING session, else raise."""
        sid = self._resolve_session_id(session_id)
        if not sid:
            raise ValueError("No active session")
        sess = self.sessions.get(sid)
        backend = self.backends.get(sid)
        if sess is None or backend is None:
            raise ValueError("No active session")
        if sess.status != SessionStatus.RUNNING:
            raise ValueError(f"Session is not running (status: {sess.status})")
        return sid, sess, backend

    async def send_command(
        self, command: str, session_id: Optional[str] = None
    ) -> bool:
        """Send a command (with trailing newline) to a session's backend."""
        sid, sess, backend = self._require_running(session_id)

        logger.info(
            "sending_command",
            session_id=sid,
            command=command[:50] + "..." if len(command) > 50 else command,
        )

        try:
            await backend.write(command.encode("utf-8") + b"\n")
            sess.last_activity = datetime.utcnow()
            self.command_counts[sid] = self.command_counts.get(sid, 0) + 1
            self._save_session_metadata(sess)
            return True
        except Exception as e:
            logger.error("send_command_failed", error=str(e))
            raise ValueError(f"Failed to send command: {e}") from e

    async def send_input(
        self, data: str, session_id: Optional[str] = None
    ) -> bool:
        """Send raw input to a session's backend."""
        sid, sess, backend = self._require_running(session_id)
        try:
            await backend.write(data.encode("utf-8"))
            sess.last_activity = datetime.utcnow()
            return True
        except Exception as e:
            logger.error("send_input_failed", error=str(e))
            raise ValueError(f"Failed to send input: {e}") from e

    def resize_terminal(
        self, cols: int, rows: int, session_id: Optional[str] = None
    ):
        """Resize a session's backend terminal. No-op if the session/backend
        isn't live."""
        sid = self._resolve_session_id(session_id)
        backend = self.backends.get(sid) if sid else None
        if backend is None:
            return
        try:
            backend.resize(cols, rows)
            logger.debug("terminal_resized", cols=cols, rows=rows, session_id=sid)
        except Exception as e:
            logger.error("terminal_resize_failed", error=str(e))

    def capture_scrollback(
        self, lines: int = 3000, session_id: Optional[str] = None
    ) -> bytes:
        """Capture a session's backend scrollback for WS replay on reconnect.

        Returns b"" when no backend is live, for PTYBackend, or on capture
        failure. The WS handler treats b"" as "nothing to replay".
        """
        sid = self._resolve_session_id(session_id)
        backend = self.backends.get(sid) if sid else None
        if backend is None:
            return b""
        try:
            return backend.capture_scrollback(lines=lines)
        except Exception as exc:
            logger.error("capture_scrollback_failed", error=str(exc))
            return b""

    # ---- log buffer (per session) ---------------------------------------

    def get_recent_logs(
        self, limit: int = 100, session_id: Optional[str] = None
    ) -> list[LogEntry]:
        """Get recent log entries for a session (default: current)."""
        sid = self._resolve_session_id(session_id)
        if not sid:
            return []
        return self.log_buffers.get(sid, [])[-limit:]

    def add_log_entry(
        self, content: str, log_type: str = "stdout",
        session_id: Optional[str] = None,
    ):
        """Append a log entry to a session's buffer (default: current)."""
        sid = self._resolve_session_id(session_id)
        if not sid:
            return
        buf = self.log_buffers.setdefault(sid, [])
        buf.append(LogEntry(
            timestamp=datetime.utcnow(),
            session_id=sid,
            content=content,
            log_type=log_type,
        ))
        if len(buf) > settings.log_buffer_size:
            del buf[: len(buf) - settings.log_buffer_size]

    def _build_tmux_status_map(self) -> dict:
        """One bulk tmux query, resolved into ``{tmux_session_name: row}``.

        Description: Single source of truth for activity-status lookups.
            Instantiates a throwaway probe TmuxBackend (same pattern as
            ``list_attachable_sessions``) and calls
            ``list_pane_status_all()``, which is ONE ``tmux list-panes -a``
            subprocess call covering every session on the socket. Callers
            key into the result by tmux session name (NOT our internal
            ``session_id``) since that's what both owned backends
            (``backend.tmux_session``) and attachable rows (``row["name"]``)
            carry in common.

            Non-tmux deployments (PTYBackend / no tmux on PATH) get an empty
            map - every lookup then falls back to ``STATUS_UNKNOWN``, which
            is honest: we have no pane-level introspection for a PTY child.

        Inputs: none (reads live tmux state via the probe backend).

        Output:
            dict[str, dict]: tmux session name -> row from
                ``TmuxBackend.list_pane_status_all()`` (has "status", "pid",
                "pane_dead", "pane_current_command"). Empty dict on any
                failure or when tmux isn't the active backend type.

        Example:
            >>> mgr._build_tmux_status_map()
            {'cloude_myproj': {'status': 'running', 'pid': 4821, ...}}
        """
        try:
            probe = build_backend(
                settings,
                session_id="__status_probe__",
                working_dir=Path.home(),
                on_output=None,
            )
            if not hasattr(probe, "list_pane_status_all"):
                return {}
            listing = coerce_listing(probe.list_pane_status_all())
            if not listing.ok:
                # An empty map is the RIGHT degradation here, and it is
                # not a false green: every consumer resolves a missing
                # name to ``STATUS_UNKNOWN`` (see ``_session_info_for``
                # and ``list_attachable_sessions``), which is exactly the
                # third outcome. Logged at WARN so the reason is not
                # invisible.
                logger.warning(
                    "tmux_status_map_unavailable",
                    reason=listing.reason,
                    detail=listing.detail,
                    note="every session status falls back to unknown",
                )
                return {}
            return {
                row["name"]: row for row in listing.sessions if row.get("name")
            }
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            logger.warning("tmux_status_map_build_failed", error=str(exc))
            return {}

    def _session_info_for(
        self, session_id: str, status_map: Optional[dict] = None
    ) -> Optional[SessionInfo]:
        """Build SessionInfo for a specific live session, or None.

        ``status_map`` (optional) lets a caller iterating many sessions
        (``list_session_infos``) build the bulk tmux status query ONCE and
        pass it in, instead of every session triggering its own subprocess
        call. When omitted, a fresh single-use map is built here so
        single-session callers (``get_session_info``) still get a real
        status without the caller having to know about the map.

        The returned ``SessionInfo.session.pty_pid`` is resolved LIVE off
        that same status map (see the ``live_pid`` block below) rather than
        trusting whatever was captured on ``Session`` at creation/adopt
        time - see fix/adopted-session-pid.
        """
        sess = self.sessions.get(session_id)
        backend = self.backends.get(session_id)
        if sess is None or backend is None or not backend.is_alive():
            return None
        if sess.status != SessionStatus.RUNNING:
            return None
        uptime = int((datetime.utcnow() - sess.created_at).total_seconds())
        stats = SessionStats(
            total_commands=self.command_counts.get(session_id, 0),
            uptime_seconds=uptime,
            log_lines=len(self.log_buffers.get(session_id, [])),
            local_servers=0,
        )
        tmux_session_name = getattr(backend, "tmux_session", None)
        backend_name = backend.__class__.__name__.replace("Backend", "").lower()

        if status_map is None:
            status_map = self._build_tmux_status_map()
        row = status_map.get(tmux_session_name) if tmux_session_name else None
        raw_tmux_status = row["status"] if row else STATUS_UNKNOWN
        # feat/hook-driven-status - the raw tmux classification (dead check
        # + graceful-fallback source) is combined with this session's live
        # hook signal (if any) and its persisted unread flag into ONE
        # unified status. See src/core/session_activity.py.
        unread = self._is_unread(tmux_session_name)
        activity_status = self._activity_tracker.resolve(
            session_id, raw_tmux_status, unread=unread
        )
        # AFTER A RESTART THE TRACKER IS EMPTY, and what it falls back to
        # is not "unknown" - it is the tmux tier, which under this app's
        # launch path reports a CONSTANT (every pane shows its wrapper
        # shell). So a session whose state was forgotten renders a
        # confident `idle`, indistinguishable from one genuinely at a
        # prompt. The durable state is consulted only when no hook has
        # been seen this run, so a live signal always wins, and only when
        # it is fresh enough to still describe now - a stale `working`
        # is a lie about right now, and returns not-measured instead.
        if not self._activity_tracker.hooks_seen(session_id):
            restored = self._restored_activity_state(tmux_session_name)
            if restored:
                activity_status = restored
        else:
            # THE SETTLED VALUE, stamped where the inputs are real. The
            # hook path cannot write this: with no tmux probe it resolves
            # to UNKNOWN once the heartbeat expires and correctly declines
            # to guess, so without this line a session's row keeps the
            # `working` written mid-turn and never settles to idle.
            self._persist_settled_activity_state(
                tmux_session_name, activity_status
            )

        # fix/adopted-session-pid - pid is resolved LIVE off the same bulk
        # ``list_pane_status_all()`` row already fetched for status above,
        # instead of trusting ``sess.pty_pid`` (which for an ADOPTED
        # session is hardcoded None at registration time, and for ANY
        # session goes stale the moment the pane's foreground process
        # changes - e.g. claude exits and a bare shell remains). The bulk
        # call already runs once per status fetch, so this is free. Falls
        # back to ``backend.pid`` (a single extra query) only when the row
        # is missing or its pid could not be parsed; falls back to the
        # captured ``sess.pty_pid`` last, for non-tmux backends that
        # never appear in the tmux status map at all (PTYBackend - whose
        # pid is stable for the process lifetime, so the captured value
        # is still correct).
        live_pid = row.get("pid") if row else None
        if live_pid is None:
            live_pid = getattr(backend, "pid", None)
        if live_pid is None:
            live_pid = sess.pty_pid
        sess_out = sess if live_pid == sess.pty_pid else sess.model_copy(
            update={"pty_pid": live_pid}
        )

        # feat/agent-family-pills - resolved fresh on every read (not
        # persisted) so a config edit (wrapper deleted/renamed) is
        # reflected immediately instead of showing a stale answer.
        # ``sess.agent_type_via_fingerprint`` is the only piece of
        # provenance that DOES need to survive - see its docstring on
        # ``Session`` for why fingerprint-derived values are otherwise
        # textually indistinguishable from launched ones.
        # THE DATABASE ROW IS AUTHORITATIVE FOR WHAT LAUNCHED THIS SESSION.
        # An ADOPTED session's in-memory Session comes back with
        # agent_type None, while the row it was adopted from still records
        # the wrapper id exactly - so resolving the family off the
        # in-memory copy alone reported "unknown family" about a session
        # whose launch we had written down. The in-memory value still wins
        # when it has one; the row is a fallback, not an override.
        row_identity = self._identity_for_live_name(tmux_session_name)
        effective_agent_type = sess.agent_type
        from_fingerprint = sess.agent_type_via_fingerprint
        if not effective_agent_type and row_identity:
            row_agent_type = row_identity.get("agent_type")
            if row_agent_type:
                effective_agent_type = row_agent_type
                # PROVENANCE TRAVELS WITH THE VALUE. The row's agent_type
                # was written by a LAUNCH - it is the wrapper id we chose -
                # so a family resolved from it is a fact, not a guess, and
                # must not render with the tilde that means "fingerprinted".
                # Carrying the in-memory fingerprint flag onto a value that
                # did not come from a fingerprint would label a certainty
                # as a guess, which is the same defect as the reverse and
                # just as misleading.
                from_fingerprint = False
        display_family, display_family_source = resolve_family_for_display(
            effective_agent_type,
            _configured_wrappers(),
            from_fingerprint=from_fingerprint,
        )

        return SessionInfo(
            session=sess_out,
            recent_logs=self.get_recent_logs(session_id=session_id),
            local_servers=[],
            stats=stats,
            session_backend=backend_name,
            tmux_session=tmux_session_name,
            # The user-facing label, read off the row. None when the row
            # carries none, which the client renders by falling back to
            # the tmux name - so a session with no label looks exactly
            # like it did before labels existed.
            label=self._label_for_tmux_name(tmux_session_name),
            session_row_id=row_identity["id"] if row_identity else None,
            parent_session_id=(
                row_identity["parent_session_id"] if row_identity else None
            ),
            agent_type=effective_agent_type,
            agent_family=display_family.name if display_family else None,
            agent_family_source=display_family_source,
            pinned_theme=sess.pinned_theme,
            activity_status=activity_status,
            unread=unread,
            # fix/session-ownership-source - ownership is membership in the
            # persisted owned set, NOT the shape of ``session_id``. After a
            # restart the app re-attaches to still-running tmux sessions
            # through the adopt path, so a session this app created carries
            # an ``adopted:`` id while its NAME is still in
            # ``owned_tmux_sessions``. The name is the durable identity; the
            # id is not. Same source AttachableSession uses, so the two
            # payloads can never disagree about the same session.
            created_by_cloude=bool(
                tmux_session_name
                and self.is_owned_tmux_name(tmux_session_name)
            ),
        )

    async def get_session_info(
        self, session_id: Optional[str] = None
    ) -> Optional[SessionInfo]:
        """Complete session information for one session (default: current)."""
        sid = self._resolve_session_id(session_id)
        if not sid:
            return None
        return self._session_info_for(sid)

    async def list_session_infos(self) -> list[SessionInfo]:
        """SessionInfo for every live session, oldest first."""
        status_map = self._build_tmux_status_map()
        out: list[SessionInfo] = []
        for sid in list(self.sessions.keys()):
            info = self._session_info_for(sid, status_map=status_map)
            if info is not None:
                out.append(info)
        return out

    def has_active_session(self) -> bool:
        """True iff at least one session is running AND its backend is alive."""
        for sid, backend in self.backends.items():
            sess = self.sessions.get(sid)
            if (
                sess is not None
                and sess.status == SessionStatus.RUNNING
                and backend.is_alive()
            ):
                return True
        return False

    def is_session_live(self, session_id: str) -> bool:
        """True iff this specific session is running AND its backend alive."""
        sess = self.sessions.get(session_id)
        backend = self.backends.get(session_id)
        return (
            sess is not None
            and sess.status == SessionStatus.RUNNING
            and backend is not None
            and backend.is_alive()
        )

    # ---- Track 1: adopt an externally-started tmux session ----------------

    def active_tmux_names(self) -> set[str]:
        """tmux session names currently bound to a live backend.

        Used by the attachable-sessions route to drop self-adopt rows for
        ALL live sessions (not just the most-recent one).
        """
        names: set[str] = set()
        for backend in self.backends.values():
            n = getattr(backend, "tmux_session", None)
            if n:
                names.add(n)
        return names

    # ---- Ownership: sessions.origin is the source of truth -------------
    #
    # feat/sessions-table (S4). Ownership used to be membership of the
    # in-memory ``owned_tmux_sessions`` set, which was rebuilt from a live
    # listing on every start and therefore could not remember an adoption
    # across a restart. It is now ``sessions.origin``, a stored column
    # anchored on the tmux INSTANCE triple.
    #
    # ``owned_tmux_sessions`` is deliberately still maintained and still
    # consulted. It is removed in a separate follow-up, only once
    # scripts/verify_session_ownership_badge.py has passed against the DB
    # as the source of truth. Until then the two are UNIONed, which can
    # only ever widen the owned set by rows the import derived FROM that
    # same set - so the badge cannot regress while the cutover settles.
    #
    # THE DB IS CONSULTED THROUGH ONE PLACE. Three call sites reading
    # ownership three different ways is how the original bug survived, so
    # every read below funnels through ``_owned_instances_from_db``.

    def _datastore_connection(self):
        """Open cloude.db for a best-effort ownership read, or return None.

        Description: ownership is read on the render path, so a database
          that is missing, locked or pre-v2 must degrade to the legacy
          in-memory set rather than raise into a badge. None means "the
          datastore has no opinion", which is NOT the same as "this app
          owns nothing" - the caller falls back instead of concluding.
        Inputs: none (reads ``settings.get_state_dir()``).
        Output: sqlite3.Connection | None.
        """
        try:
            from src.core.db import connect, db_path_for

            path = db_path_for(settings.get_state_dir())
            if not Path(path).exists():
                return None
            return connect(path, create=False)
        except Exception as exc:  # noqa: BLE001 - never break the render path
            logger.debug("ownership_datastore_unavailable", error=str(exc))
            return None

    def _owned_instances_from_db(self) -> Optional[set]:
        """Read the owned ``(tmux_name, epoch)`` pairs from sessions.origin.

        Description: the single DB read behind every ownership decision in
          this class. Keyed on the instance, never on the name alone, so a
          reused tmux name cannot inherit a dead session's badge.
        Inputs: none.
        Output: set[tuple[str, int]] | None - None when the datastore
          could not answer at all (absent, unreadable, pre-v2). An EMPTY
          SET is a real answer of "the DB knows of no owned instance";
          None is the absence of an answer, and the two are handled
          differently by every caller.
        """
        conn = self._datastore_connection()
        if conn is None:
            return None
        try:
            from src.core.session_store import owned_instances, sessions_table_ready

            if not sessions_table_ready(conn):
                return None
            return owned_instances(conn, socket=self._tmux_socket_name())
        except Exception as exc:  # noqa: BLE001 - never break the render path
            logger.debug("ownership_db_read_failed", error=str(exc))
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - close failure is not a verdict
                pass

    def _writable_datastore_connection(self):
        """Open the datastore for WRITING, or return None.

        Description: the sibling of :meth:`_datastore_connection`, which
          is deliberately ``create=False`` because it serves the RENDER
          path and must never bring a database into existence as a side
          effect of drawing a badge. This one is used by the adopt path,
          which is an explicit user action on an app that has already
          booted, so the file is expected to be there; it still declines
          to create one, because an adoption that silently births a fresh
          empty datastore would lose the install's history rather than
          record a claim into it.
        Inputs: none (reads ``settings.get_state_dir()``).
        Output: sqlite3.Connection | None - None when the datastore
          cannot be opened at all, which the caller must report as
          COULD NOT EVALUATE and never as a failed adoption.
        """
        try:
            from src.core.db import connect, db_path_for

            path = db_path_for(settings.get_state_dir())
            if not Path(path).exists():
                return None
            return connect(path, create=False)
        except Exception as exc:  # noqa: BLE001 - adoption must not crash
            logger.warning("adopt_datastore_unavailable", error=str(exc))
            return None

    def record_claude_lifecycle_event(
        self, session_id: str, event_kind: str, payload: dict
    ):
        """Write Claude-session identity / fork lineage for one hook event.

        Description: the correlation step between the TWO identities this
          app holds. A Claude Code hook knows its own conversation uuid
          and nothing about tmux; cloudecode knows the tmux instance and
          nothing about the conversation. The bridge is
          ``CLOUDECODE_SESSION_ID`` - injected into the spawned agent's
          environment at ``new-session`` time by
          :meth:`get_env_for_spawn`, echoed back by the hook as the
          ``X-Cloudecode-Session`` header, and resolved HERE to a tmux
          name and then, via a live listing, to the instance triple that
          keys the row. No new channel and no new credential: the
          existing hook trio already proves both identity and locality.

          THIS IS THE ONLY PLACE THE TWO CAN BE JOINED. The hook cannot
          learn the tmux creation epoch (it is not in the pane's
          environment), and the server cannot learn the conversation uuid
          any other way, so the join has to happen server-side against a
          fresh listing - the same shape ``persist_adoption`` uses, for
          the same reason.

          IT NEVER RAISES. Every failure is a named outcome. The caller is
          the hook endpoint, which runs inside a live working session's
          critical path; an exception escaping here would become a 500,
          and while the hook one-liner discards that, a telemetry write
          has no business producing one.

          ``SessionEnd`` is accepted and deliberately writes nothing to
          lineage. It is a real event worth logging, but the row already
          says which conversation was there, and marking it ended would
          duplicate a lifecycle the tmux probe owns and measures directly.
        Inputs: session_id (str) - the cloudecode session id from the
          header. event_kind (str) - 'SessionStart' or 'SessionEnd'.
          payload (dict) - the hook's JSON body, read defensively; no
          field is required to be present.
        Output: LineageResult - ``outcome`` is one of bound / continued /
          forked / unresolved. Never None, never an exception.
        Example: mgr.record_claude_lifecycle_event(sid, 'SessionStart', p)
        """
        from src.core.session_lineage import (
            LINEAGE_CONTINUED,
            LINEAGE_UNRESOLVED,
            LineageResult,
            record_claude_session,
        )

        if event_kind != "SessionStart":
            # SessionEnd IS NOW LOAD-BEARING, though it still writes no
            # lineage of its own. It is the one signal that says the pane
            # LEFT a conversation, which is what separates /branch (pane
            # moved) from /fork (pane stayed) - both of which arrive as
            # source="fork" and are otherwise identical.
            if event_kind == "SessionEnd":
                ended = payload.get("session_id") if isinstance(payload, dict) else None
                if isinstance(ended, str) and ended:
                    self._last_session_end_uuid[session_id] = ended
            return LineageResult(
                outcome=LINEAGE_CONTINUED,
                detail=f"{event_kind} carries no lineage transition",
            )

        claude_uuid = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(claude_uuid, str) or not claude_uuid:
            return LineageResult(
                outcome=LINEAGE_UNRESOLVED,
                detail="the SessionStart payload carried no session_id",
            )

        session = self.get_session(session_id)
        tmux_name = getattr(session, "tmux_session", None) if session else None
        if not tmux_name:
            # RESTART FALLBACK. The pane's CLOUDECODE_SESSION_ID is baked
            # in at spawn and cannot be re-issued to a running agent, but
            # after a restart nothing in memory carries that id any more -
            # sessions come back under adopted ids, and session_metadata
            # only records tmux NAMES. So a surviving agent's hooks used
            # to authenticate (once tokens became durable) and then
            # resolve to nothing, which is a 200 that records exactly as
            # much as a 403 did.
            #
            # The persisted map is the missing half. It is consulted ONLY
            # when the live lookup misses, so a live session always wins
            # and this can never override current state with a stale name.
            tmux_name = self._hook_tmux_names.get(session_id)
            if tmux_name:
                logger.info(
                    "lineage_resolved_from_persisted_name",
                    session_id=session_id,
                    note="session id predates a restart; resolved by tmux name",
                )
        if not tmux_name:
            return LineageResult(
                outcome=LINEAGE_UNRESOLVED,
                detail=(
                    "no live session carries this cloudecode session id, and "
                    "no persisted tmux name is recorded for it"
                ),
            )

        conn = self._writable_datastore_connection()
        if conn is None:
            return LineageResult(
                outcome=LINEAGE_UNRESOLVED,
                detail="the datastore could not be opened to record lineage",
            )
        try:
            # CONSUMED ONCE. A SessionEnd immediately before this
            # SessionStart means the pane moved off the old conversation;
            # anything else means it stayed and this is a background
            # agent. Popped rather than read so a single SessionEnd can
            # never mark two later starts as pane-moves.
            pane_left_previous = (
                self._last_session_end_uuid.pop(session_id, None) is not None
            )

            from src.core.db import transaction

            # THE SOCKET THE LISTING ACTUALLY RAN AGAINST, not the one
            # settings says it should have - same reasoning as
            # persist_adoption, and the same reason a verification harness
            # pinning its own socket cannot leak a probe onto another one.
            from src.core.session_adopt_persist import find_live_instance

            probe_socket, listing = self.list_attachable_sessions_with_socket()
            socket = probe_socket or self._tmux_socket_name()

            # THREE OUTCOMES AT THE PROBE, NOT TWO. A listing that could
            # not RUN and a listing that ran and does not contain the name
            # are different facts, and only the first is "we could not
            # tell". Collapsing them would let a broken tmux probe silently
            # discard lineage as though the session were simply gone.
            if not getattr(listing, "ok", False):
                return LineageResult(
                    outcome=LINEAGE_UNRESOLVED,
                    detail=(
                        "the tmux listing could not run, so the instance "
                        f"could not be identified: {getattr(listing, 'reason', None)}"
                    ),
                )
            live = find_live_instance(listing, tmux_name)
            if live is None:
                return LineageResult(
                    outcome=LINEAGE_UNRESOLVED,
                    detail=(
                        f"tmux session {tmux_name!r} is not in a listing that ran"
                    ),
                )
            try:
                epoch = int(live.get("created_at_epoch"))
            except (TypeError, ValueError):
                # No readable epoch means no identity triple. Reported as
                # unresolved rather than guessed at - record_claude_session
                # would refuse it anyway, but saying so here names WHY.
                epoch = None

            with transaction(conn):
                return record_claude_session(
                    conn,
                    socket=socket,
                    name=tmux_name,
                    epoch=epoch,
                    claude_uuid=claude_uuid,
                    source=payload.get("source"),
                    title=payload.get("session_title"),
                    pane_left_previous=pane_left_previous,
                )
        except Exception as exc:  # noqa: BLE001 - lineage must never raise
            logger.warning(
                "claude_lineage_record_failed",
                session=session_id,
                event_kind=event_kind,
                error=str(exc),
            )
            return LineageResult(
                outcome=LINEAGE_UNRESOLVED,
                detail=f"could not record lineage: {exc}",
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - close failure is not a verdict
                pass

    def persist_adoption(self, name: str):
        """Write ``origin='adopted'`` for one tmux session, durably.

        Description: build step S7. Adoption used to be recorded NOWHERE:
          the name was deliberately kept out of ``owned_tmux_sessions``
          (an in-memory set rebuilt from a live listing anyway), so an
          adopted session was permanently external and the claim did not
          survive so much as a page reload. It is now a stored column on
          a row keyed by the tmux INSTANCE triple, so it survives an app
          restart, a server restart and a reboot.

          THE DECISION: an adopted session is OURS, for good. ``created``
          and ``adopted`` both badge as ours because
          ``session_store.owned_instances`` selects
          ``SESSION_OWNED_ORIGINS``, which holds both. ``observed`` is the
          only external value. Which of the two it was stays in the
          column and is shown on the session detail surface.

          Takes a FRESH listing rather than trusting the client's, so an
          instance that died between the client's list and its click is
          caught here and reported as gone instead of being claimed.
        Inputs: name (str) - the tmux session name being adopted.
        Output: AdoptPersistResult - ``persisted`` is True only when a
          row now carries ``origin='adopted'``. Every failure is a named
          outcome, never an exception and never a silent success.
        Example: mgr.persist_adoption('cloude_a').persisted
        """
        from src.core.session_adopt_persist import (
            PERSIST_LISTING_UNAVAILABLE,
            AdoptPersistResult,
            persist_adoption,
        )

        conn = self._writable_datastore_connection()
        if conn is None:
            return AdoptPersistResult(
                outcome=PERSIST_LISTING_UNAVAILABLE,
                detail="the datastore could not be opened to record the claim",
            )
        try:
            from src.core.db import transaction
            from src.core.tmux_session_cwd import make_working_dir_probe

            # THE SOCKET THE LISTING ACTUALLY RAN AGAINST, not the one
            # settings says it should have. Same lesson main.py already
            # carries for the first-run import: a row keyed on one socket
            # and a probe run against another produce a consistent-looking
            # result that is wrong, because the writer and the reader
            # agree with each other and neither agrees with tmux. Reading
            # it off the probe backend also keeps the cwd probe on that
            # same socket, so a test or verification harness that pins a
            # dedicated socket cannot leak a probe onto the real one.
            probe_socket, listing = self.list_attachable_sessions_with_socket()
            socket = probe_socket or self._tmux_socket_name()
            with transaction(conn):
                return persist_adoption(
                    conn,
                    socket=socket,
                    name=name,
                    listing=listing,
                    working_dir_probe=make_working_dir_probe(socket),
                )
        except Exception as exc:  # noqa: BLE001 - adoption must not crash
            logger.warning(
                "adopt_persist_failed", session=name, error=str(exc)
            )
            return AdoptPersistResult(
                outcome=PERSIST_LISTING_UNAVAILABLE,
                detail=f"could not record the claim: {exc}",
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - close failure is not a verdict
                pass

    def persist_creation(
        self,
        name: str,
        working_dir: Optional[str] = None,
        agent_type: Optional[str] = None,
        agent_launched: Optional[bool] = None,
    ):
        """Write ``origin='created'`` for one tmux session, durably.

        Description: the sibling of :meth:`persist_adoption`, and the
          write site ``SESSION_ORIGIN_CREATED`` did not have. Ownership of
          a launcher-made session used to live only in
          ``owned_tmux_sessions`` (in memory, dies with the process) and
          ``session_metadata.json`` (tier 3, and outranked by any DB
          opinion). The authority - the ``sessions`` table - was never
          told, so ``session_store.owned_instances`` never held the
          instance and the launcher badged the user's own session
          EXTERNAL after every restart.

          Takes a FRESH listing, for the same reason adoption does: the
          row is keyed on ``(socket, name, epoch)`` and the epoch is
          tmux's ``#{session_created}``, which only exists once tmux has
          made the session. Nothing is written unless a probe that RAN
          contains the name, so a row can never claim a session that is
          not there.

          NEVER RAISES, and a failure here must never fail the creation:
          by the time this runs the session is live and usable, and
          tearing it down over a bookkeeping write would turn a wrong
          badge into lost work. Every failure is a named outcome the
          caller logs, and the condition is repairable - a later adopt,
          or a re-record onto the same triple, MERGEs onto the same row.
        Inputs: name (str) - the tmux session name just created.
          working_dir (str | None) - the directory it was created in,
          which this path already knows and need not probe for.
          agent_type (str | None) - the agent this path RESOLVED and
          built its launch command from. agent_launched (bool | None) -
          whether that command was actually run, or a bare shell was
          started instead. Both are forwarded verbatim; the module-level
          ``persist_creation`` is the single place that turns them into
          stored provenance. Passing them is what stops a session the
          app itself started from rendering a GUESSED agent type.
        Output: CreatePersistResult - ``recorded`` is True only when a
          row now carries ``origin='created'``.
        Example: mgr.persist_creation('cloude_a').recorded
        """
        from src.core.session_create_persist import (
            CREATE_NO_DATASTORE,
            CreatePersistResult,
            persist_creation,
        )

        conn = self._writable_datastore_connection()
        if conn is None:
            logger.warning(
                "create_persist_no_datastore",
                tmux_name=name,
                note=(
                    "the session is live but there is no datastore to "
                    "record it in, so its ownership rests on the "
                    "degraded in-memory tier until one exists"
                ),
            )
            return CreatePersistResult(
                outcome=CREATE_NO_DATASTORE,
                detail="the datastore could not be opened to record the session",
            )
        try:
            from src.core.db import transaction
            from src.core.tmux_session_cwd import make_working_dir_probe

            # The socket the probe ACTUALLY ran against, never the one
            # settings says it should have. Same reasoning as
            # persist_adoption: a row keyed on one socket and a listing
            # taken from another agree with each other and with nothing
            # else.
            probe_socket, listing = self.list_attachable_sessions_with_socket()
            socket = probe_socket or self._tmux_socket_name()
            with transaction(conn):
                return persist_creation(
                    conn,
                    socket=socket,
                    name=name,
                    listing=listing,
                    agent_type=agent_type,
                    agent_launched=agent_launched,
                    working_dir=working_dir,
                    working_dir_probe=make_working_dir_probe(socket),
                )
        except Exception as exc:  # noqa: BLE001 - creation must not crash
            logger.warning(
                "create_persist_failed",
                session=name,
                error=str(exc),
                note=(
                    "the session itself is unaffected; only its ownership "
                    "record failed to write"
                ),
            )
            return CreatePersistResult(
                outcome=CREATE_NO_DATASTORE,
                detail=f"could not record the new session: {exc}",
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - close failure is not a verdict
                pass

    def _tmux_socket_name(self) -> str:
        """The tmux socket the stored instance triples are keyed on.

        Description: read from settings with the schema default as the
          fallback, so a row written by the import and a row read by the
          badge cannot disagree about which socket they describe.
        THE PROBE WINS OVER SETTINGS WHEN THE TWO DISAGREE, and that is
          the whole point of this method rather than a raw settings read.
          A row is WRITTEN keyed on the socket the adopt path saw and is
          READ back by ``owned_instances`` keyed on whatever this
          returns; if those two ever differ, the badge is answered from a
          socket nothing was written to and every adopted session reads
          as external. Following the socket the probe was ACTUALLY bound
          to keeps writer and reader on the same key AND keeps that key
          equal to reality - the failure main.py's ``socket=`` argument
          already documents, where the two sides agreed with each other
          and neither agreed with tmux. In production the values are
          identical; they diverge only where a harness pins a backend to
          a dedicated socket, and there following the pin is exactly
          right.
        Inputs: none.
        Output: str.
        """
        from src.core.db_models import DEFAULT_TMUX_SOCKET

        probed = getattr(self, "_last_probe_socket", None)
        if probed:
            return str(probed)

        session_cfg = getattr(settings, "session", None)
        return (
            getattr(session_cfg, "tmux_socket_name", None)
            or DEFAULT_TMUX_SOCKET
        )

    def last_probe_health(self) -> "ProbeHealth":
        """Report whether the most recent tmux listing probe succeeded.

        Description: read by ``GET /sessions/recent`` (S9) to decide
          whether the stored RECENT rows may be shown as fact. Reuses
          whichever call to :meth:`list_attachable_sessions` most
          recently ran - normally the home screen's own poll - rather
          than triggering a fresh probe of its own, so viewing RECENT
          never adds tmux load on top of what the launcher already pays.
        Inputs: none.
        Output: ProbeHealth - ``ok`` is None when no probe has run yet
          this process's lifetime (a real third state, distinct from
          both True and False - see the field's docstring in
          ``__init__``), True/False otherwise, with ``reason``/``detail``
          populated only on a known failure.
        Example: mgr.last_probe_health().ok
        """
        return ProbeHealth(
            ok=self._last_probe_ok,
            reason=self._last_probe_reason,
            detail=self._last_probe_detail,
        )

    def tmux_socket_name(self) -> str:
        """The tmux socket this manager probes and keys its rows on.

        Description: the public face of :meth:`_tmux_socket_name`, added
          because src/main.py must hand the first-run import the SAME
          socket the probe ran against. It previously passed nothing, so
          imported rows took the module default while this manager read
          the CONFIGURED value back - and a user with a custom
          ``session.tmux_socket_name`` got an ownership badge that fell
          back to the name-only tier for the entire install.
        Inputs: none.
        Output: str - the configured socket name, or the schema default.
        Example: mgr.tmux_socket_name()  # 'cloude'
        """
        return self._tmux_socket_name()

    def owned_tmux_instances(self) -> Optional[set]:
        """Owned ``(tmux_name, epoch)`` pairs from the datastore, and only those.

        Description: the value handed to the attachable listing, which is
          the one path that HAS the epoch for every row and can therefore
          make the identity-correct decision.

          THE LEGACY NAME SET IS DELIBERATELY NOT FOLDED IN HERE. It used
          to be, as ``(name, None)``, and the backend read a None epoch as
          a NAME-ONLY WILDCARD. That disabled the epoch tier for every
          session this app had created since the last restart - which is
          precisely the population the epoch exists to protect - so a dead
          ``cloude_work`` replaced by the user's own unrelated
          ``cloude_work`` badged as ours, exactly as it did before the
          epoch was introduced. The legacy names still reach the backend,
          but as the SEPARATE ``owned_names`` argument, so they can be
          resolved at their own, lower, explicitly name-only tier and can
          never override a stored epoch. See
          :func:`src.core.tmux_listing_parse.resolve_ownership`.
        Inputs: none.
        Output: set[tuple[str, int]] | None - None when the datastore
          could not answer at all. An EMPTY SET is a real answer ("the DB
          knows of no owned instance") and is not the same as None.
        """
        from_db = self._owned_instances_from_db()
        if from_db is None:
            return None
        return set(from_db)

    def is_owned_tmux_name(self, name: Optional[str]) -> bool:
        """Report whether a tmux NAME belongs to a session we own.

        Description: the name-only fallback, for call sites that carry no
          creation epoch - ``SessionInfo`` is one. Lossy in exactly one
          way, stated so nobody has to rediscover it: a name owned as one
          instance and now reused by a different, unowned instance reads
          as owned here until the epoch reaches this call site. The
          attachable listing, which does have the epoch, is not lossy.
        Inputs: name (str | None) - a tmux session name.
        Output: bool - False for None or an empty name.
        Example: mgr.is_owned_tmux_name('cloude_a')
        """
        if not name:
            return False
        if name in self.owned_tmux_sessions:
            return True
        from_db = self._owned_instances_from_db()
        if from_db is None:
            return False
        return any(owned_name == name for owned_name, _epoch in from_db)

    def _fingerprint_agent_type_for_listing(
        self, *, socket: str, name: str, epoch: Optional[int]
    ) -> Optional[str]:
        """Detect which agent CLI a listed tmux instance is running.

        Description: the LISTING-TIME counterpart to the fingerprint scan
          ``adopt_external_session`` already runs. That path captures
          scrollback through a fully attached backend (pipe-pane, FIFO,
          resize) because it needs the bytes for first-paint replay too;
          this one only needs to know which agent is running, so it uses
          a bare ``TmuxBackend.for_external`` (no attach, no pipe-pane -
          one ``tmux capture-pane`` subprocess call and nothing else) and
          throws the bytes away after scanning them.

          Cached per instance triple in ``self._listing_fingerprint_cache``
          so a listing poll after the first never re-probes tmux for a
          session it has already seen - see that field's docstring for
          why this is safe and what it trades away. A ``None`` epoch
          cannot be cached (no stable key), so it is fingerprinted fresh
          every call - this only happens for a row tmux itself could not
          date, which S5's listing parser already treats as a refused
          row in the common path.
        Inputs: socket (str) - tmux socket the row was listed on.
          name (str) - tmux session name. epoch (int | None) -
          ``#{session_created}``, the instance discriminator.
        Output: str | None - a value from
          ``agent_fingerprint.AGENT_FINGERPRINTS`` (e.g. ``"codex"``), or
          None when nothing matched or the probe failed. None is cached
          exactly like a hit: "fingerprinted, found nothing" is itself an
          answer worth not re-asking for.
        Example:
          mgr._fingerprint_agent_type_for_listing(
              socket='cloude', name='cloude_a', epoch=1723999999)
        """
        if epoch is None:
            return self._detect_agent_type_from_pane(socket=socket, name=name)

        key = (socket, name, int(epoch))
        if key in self._listing_fingerprint_cache:
            return self._listing_fingerprint_cache[key]

        detected = self._detect_agent_type_from_pane(socket=socket, name=name)
        self._listing_fingerprint_cache[key] = detected
        return detected

    def _detect_agent_type_from_pane(
        self, *, socket: str, name: str
    ) -> Optional[str]:
        """Run one uncached ``capture-pane`` + fingerprint scan.

        Description: split out of
          :meth:`_fingerprint_agent_type_for_listing` so the cache
          decision and the actual probe are two separately testable
          units. Never raises - a probe failure (dead pane, tmux gone,
          unsafe name) is not a reason to fail the whole listing, it is
          just one more row that stays "unknown family".
        Inputs: socket (str), name (str).
        Output: str | None.
        """
        from src.core.agent_fingerprint import detect_agent_type
        from src.core.tmux_backend import TmuxBackend

        try:
            probe_backend = TmuxBackend.for_external(
                session_name=name,
                working_dir=Path.home(),
                socket_name=socket,
            )
            scrollback = probe_backend.capture_scrollback(lines=2000)
            scrollback_text = scrollback.decode("utf-8", errors="replace")
            return detect_agent_type(scrollback_text)
        except Exception as exc:  # noqa: BLE001 - a probe must never crash listing
            logger.debug(
                "listing_fingerprint_probe_failed",
                session=name,
                socket=socket,
                error=str(exc),
            )
            return None

    def _stored_launch_for_listing(self, *, socket, name, epoch):
        """Read this instance's RECORDED launch decision, or NOT KNOWN.

        Description: a thin, never-raising bridge from the listing loop to
          ``session_agent_provenance.stored_launch_for``. Every failure -
          no datastore, an unreadable row, a locked file - answers NOT
          KNOWN, which is honest: it means the caller falls through to a
          fingerprint scan and the result renders as the inference it is.
          It must never raise, because a bookkeeping read has no business
          failing the home screen's session list.
        Inputs: socket (str), name (str), epoch (int | None) - the
          instance triple the listing row carries.
        Output: StoredLaunch - read ``known`` before ``agent_type``.
        Example: self._stored_launch_for_listing(
                     socket='cloude', name='a', epoch=7).known
        """
        from src.core.session_agent_provenance import (
            NOT_KNOWN,
            stored_launch_for,
        )

        conn = None
        try:
            conn = self._writable_datastore_connection()
        except Exception:
            conn = None
        if conn is None:
            return NOT_KNOWN
        try:
            return stored_launch_for(
                conn, socket=socket, name=name, epoch=epoch
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("stored_launch_lookup_threw", error=str(exc))
            return NOT_KNOWN
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _label_for_tmux_name(self, tmux_name):
        """Read the stored LABEL for a tmux session name, or None.

        Description: a never-raising decoration read for the callers
          that have no creation epoch in hand. Delegates to
          :func:`src.core.session_label.label_for_name`, which documents
          exactly how weak the name-only key is and what it forbids: the
          NEWEST row for the name decides, and if that row carries no
          title the answer is None, so a dead predecessor can never lend
          its label to a live session that was never named.

          PREFER :meth:`_label_for_instance` WHENEVER AN EPOCH EXISTS.
          A tmux listing row carries ``created_at_epoch`` and the
          attribution prompt carries ``epoch``, so for those callers the
          exact triple is free and this weaker read has no reason to be
          used at all.

          Every failure answers None, which the client renders as "no
          label" and falls back to the tmux name for. A bookkeeping read
          must never be able to fail a session payload.
        Inputs: tmux_name (str | None).
        Output: str | None - the label, or None when there is none.
        Example: self._label_for_tmux_name('cloude_a')
        """
        from src.core.session_label import label_for_name

        if not tmux_name:
            return None
        conn = None
        try:
            conn = self._writable_datastore_connection()
            if conn is None:
                return None
            return label_for_name(
                conn, socket=self._tmux_socket_name(), name=tmux_name
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("session_label_read_threw", error=str(exc))
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _identity_for_live_name(self, tmux_name):
        """Row identity for a LIVE session, which knows only its tmux name.

        Description: a SessionInfo carries no creation epoch, so the exact
          triple is not available here. This takes the NEWEST instance of
          that name, which for a LIVE session is the right row by
          construction - the pane you are attached to is the most recent
          instance, and an older row with the same name is a dead session
          whose name was reused. Weaker than
          :meth:`_identity_for_instance`, and said out loud rather than
          hidden.

          Never raises: a decoration read must not fail the payload it
          decorates.
        Inputs: tmux_name (str | None).
        Output: dict with ``id``, ``parent_session_id``, ``agent_type``, or
          None.
        Example: self._identity_for_live_name('cloude_work')
        """
        from src.core.session_store import identity_for_live_name

        if not tmux_name:
            return None
        conn = None
        try:
            conn = self._writable_datastore_connection()
            if conn is None:
                return None
            return identity_for_live_name(
                conn, socket=self._tmux_socket_name(), name=tmux_name
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("session_identity_live_read_threw", error=str(exc))
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _identity_for_instance(self, tmux_name, epoch):
        """Read the stored ROW IDENTITY for one tmux INSTANCE, or None.

        Description: same exact-key contract as
          :meth:`_label_for_instance` and for the same reason - an id is
          something a human reads AS identity, so a name-only read that
          could hand back a dead session's id is not acceptable here.
          Keyed on the full ``(socket, name, epoch)`` triple.

          Never raises: a decoration read must not be able to fail the
          payload it decorates.
        Inputs: tmux_name (str | None). epoch (int | None).
        Output: dict with ``id`` and ``parent_session_id``, or None. None
          is a real answer - an external tmux session the app never
          created has no row, and the UI renders nothing rather than
          inventing an id for it.
        Example: self._identity_for_instance('cloude_a', 1700000000)
        """
        from src.core.session_store import identity_for_instance

        if not tmux_name or epoch is None:
            return None
        conn = None
        try:
            conn = self._writable_datastore_connection()
            if conn is None:
                return None
            return identity_for_instance(
                conn,
                socket=self._tmux_socket_name(),
                name=tmux_name,
                epoch=epoch,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("session_identity_instance_read_threw", error=str(exc))
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _label_for_instance(self, tmux_name, epoch):
        """Read the stored LABEL for one tmux INSTANCE, or None.

        Description: the exact-key decoration read. Keyed on the full
          ``(socket, name, epoch)`` triple, so the answer is either this
          instance's label or None - never another session's. Use this
          for anything a human reads as identity.

          Never raises, for the same reason as
          :meth:`_label_for_tmux_name`: a decoration read must not be
          able to fail the payload it decorates.
        Inputs: tmux_name (str | None). epoch (int | None) - the tmux
          creation epoch; None has no instance to key on and answers
          None rather than falling back to the name.
        Output: str | None - the label, or None.
        Example: self._label_for_instance('cloude_a', 1700000000)
        """
        from src.core.session_label import label_for_instance

        if not tmux_name or epoch is None:
            return None
        conn = None
        try:
            conn = self._writable_datastore_connection()
            if conn is None:
                return None
            return label_for_instance(
                conn,
                socket=self._tmux_socket_name(),
                name=tmux_name,
                epoch=epoch,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("session_label_instance_read_threw", error=str(exc))
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def reconcile_lifecycle(self, listing: TmuxListing) -> "ReconcileOutcome":
        """Fold one tmux listing into the stored ``lifecycle`` column.

        Description: the bridge between the live probe and the datastore,
          and the ONLY place this class writes ``lifecycle``. It is called
          from :meth:`list_attachable_sessions` on the branch where the
          probe ANSWERED, because that listing is already a complete
          enumeration of the socket, the home screen pays for it on every
          load anyway, and a second scheduled probe would only add a
          second way to be wrong.

          It hands the socket the probe was ACTUALLY bound to, never the
          configured one - the two can disagree under a pinned test
          backend, and reconciling socket A's rows against socket B's
          listing would reap every one of them.

          NEVER RAISES. A datastore that is absent, locked or unreadable
          is a reason to leave every stored row exactly as it is, not a
          reason to fail the launcher's session list. The failure is
          logged and the outcome says it was not evaluated.

          The transaction is owned here, not in
          :func:`~src.core.session_lifecycle.reconcile_from_listing`,
          which matches ``session_identity.record_instance``. The commit
          is skipped entirely when nothing was reaped, so the common case
          - a poll where every session is still alive - opens the file,
          runs one SELECT and writes nothing at all.
        Inputs: listing (TmuxListing) - the probe result; its ``ok`` and
          ``complete`` are re-read inside the reconciler, which is where
          the gate lives.
        Output: ReconcileOutcome - ``evaluated=False`` whenever the probe
          could not answer, the listing was partial, or the datastore
          could not be opened. Those are three different reasons and none
          of them is "nothing died".
        Example: mgr.reconcile_lifecycle(listing).changed
        """
        from src.core.session_lifecycle import (
            RECONCILE_NO_TABLE,
            ReconcileOutcome,
            reconcile_from_listing,
        )

        conn = self._writable_datastore_connection()
        if conn is None:
            return ReconcileOutcome(
                outcome=RECONCILE_NO_TABLE,
                evaluated=False,
                detail="datastore not available for writing",
            )
        try:
            outcome = reconcile_from_listing(
                conn,
                listing=listing,
                socket=self._last_probe_socket or self._tmux_socket_name(),
            )
            if outcome.changed:
                # PROVABLY REDUNDANT TODAY, KEPT ANYWAY. src.core.db.connect
                # opens with isolation_level=None, so the UPDATE is already
                # durable the moment it executes and this commit is a no-op
                # (pinned by test_the_datastore_connection_is_autocommit).
                # It stays because it costs nothing and is the only line
                # that would keep this correct if that ever changes, and a
                # reap that silently vanished on close would look exactly
                # like a machine where nothing had died.
                conn.commit()
                logger.info(
                    "session_lifecycle_reconciled",
                    stopped=len(outcome.stopped_uuids),
                    examined=outcome.examined,
                    session_uuids=list(outcome.stopped_uuids),
                )
            return outcome
        except sqlite3.Error as exc:
            # A datastore problem is not a verdict about any session, so
            # the rows keep whatever they already said. Never re-raised:
            # the launcher's session list does not depend on this.
            logger.warning(
                "session_lifecycle_reconcile_failed",
                error=str(exc),
                note="stored lifecycles left untouched",
            )
            return ReconcileOutcome(
                outcome=RECONCILE_NO_TABLE,
                evaluated=False,
                detail=f"datastore error: {exc}",
            )
        finally:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - close failure is not a verdict
                pass

    def list_attachable_sessions_with_socket(self):
        """Enumerate attachable sessions AND name the socket they came from.

        Description: :meth:`list_attachable_sessions` throws away one
          fact its caller sometimes needs - WHICH socket the probe ran
          against. That is normally the configured value, but it is read
          off the probe backend rather than off settings, because the two
          can disagree (a harness may pin a backend to a dedicated
          socket) and a row keyed on one socket while the listing came
          from another is the exact defect main.py's ``socket=`` argument
          was added to fix: writer and reader agree with each other and
          neither agrees with tmux.
        Inputs: none.
        Output: tuple[str | None, TmuxListing] - the socket the probe
          used (None when the backend does not expose one), and the
          listing itself, ``ok=False`` propagated verbatim.
        Example: mgr.list_attachable_sessions_with_socket()[0]  # 'cloude'
        """
        listing = self.list_attachable_sessions()
        return self._last_probe_socket, listing

    def list_attachable_sessions(self) -> TmuxListing:
        """Enumerate tmux sessions on our socket, flagged by ownership.

        Description: Thin pass-through to
            ``backend.list_attachable_sessions``, but we always
            instantiate a fresh PROBE backend rather than using
            ``self.backend`` - the user should be able to list external
            sessions whether or not they currently have an active session
            (the adopt-UI fetch happens at launchpad render time).

        Inputs: none (reads ``self.owned_tmux_sessions``).

        Output:
            TmuxListing: ``ok=True`` with decorated dict rows (each
                carrying ``pinned_theme``, ``status`` and ``unread`` on
                top of the backend's fields). ``ok=False`` propagates
                the probe failure verbatim and carries NO rows, so the
                route can answer "cannot determine" instead of "zero".

        Example:
            >>> mgr.list_attachable_sessions().ok
            True
        """
        probe = build_backend(
            settings,
            session_id="__probe__",
            working_dir=Path.home(),
            on_output=None,
        )
        # Record the socket the probe is ACTUALLY bound to, for
        # list_attachable_sessions_with_socket. See that method for why
        # settings is not a trustworthy answer to this question.
        self._last_probe_socket = getattr(probe, "socket_name", None)
        listing = coerce_listing(
            probe.list_attachable_sessions(
                owned_names=set(self.owned_tmux_sessions),
                owned_instances=self.owned_tmux_instances(),
            )
        )
        if not listing.ok:
            # Propagate the unknown untouched. Decorating rows we do not
            # have would be inventing them; returning [] with ok=True
            # would be the original bug.
            logger.warning(
                "attachable_listing_unavailable",
                reason=listing.reason,
                detail=listing.detail,
            )
            # S9 - record the failure for GET /sessions/recent. Nothing
            # about the stored ``sessions`` table is touched here (no
            # write on a failed probe, same rule ``reconcile_existing``
            # already enforces for lifecycle) - only the in-memory health
            # flag other readers consult moves.
            self._last_probe_ok = False
            self._last_probe_reason = listing.reason
            self._last_probe_detail = listing.detail
            return listing
        # S9 - a successful listing is this process's evidence that tmux
        # answered just now, independent of what rows it returned (an
        # empty tmux server is still a successful probe).
        self._last_probe_ok = True
        self._last_probe_reason = None
        self._last_probe_detail = None
        # THE REAPER. This listing is a complete enumeration of the
        # socket, so it is the one moment the app can tell that a stored
        # 'running' row's tmux instance is gone. Runs here rather than on
        # a timer because the home screen already pays for this probe;
        # writes only when something actually died. The ok / complete
        # gate lives inside reconcile_from_listing, not here, so no
        # caller can bypass it. Never raises.
        self.reconcile_lifecycle(listing)
        rows = listing.sessions
        # Status lights: one extra bulk tmux call (list-panes -a), reused
        # via the same probe backend / socket. This is the ONLY place a
        # dead-but-still-in-tmux session (remain-on-exit) gets its state
        # surfaced - these rows are exactly the ones NOT bound to a live
        # backend, so `_session_info_for`'s status never sees them.
        # A FAILED status probe here does not invalidate the row set we
        # already have; it only means each row's activity light falls
        # back to ``STATUS_UNKNOWN`` below, which is the honest third
        # outcome for that one field rather than for the whole listing.
        status_listing = (
            coerce_listing(probe.list_pane_status_all())
            if hasattr(probe, "list_pane_status_all")
            else TmuxListing.answered([])
        )
        if not status_listing.ok:
            logger.warning(
                "attachable_status_map_unavailable",
                reason=status_listing.reason,
                note="attachable rows fall back to unknown activity status",
            )
        status_map = {
            row2["name"]: row2
            for row2 in status_listing.sessions
            if row2.get("name")
        }
        # SESSION-IDENTITY-V2 - decorate each row with its persisted
        # pinned theme (if any). The launchpad's active-session banner
        # uses this so re-entering a session paints the right theme on
        # first frame; without it, the client would wait until the
        # adopt response to learn the pin and the user would see a
        # one-frame Lovecraft flash before the pin paints.
        for row in rows:
            name = row.get("name")
            if name:
                row["pinned_theme"] = self.pinned_themes.get(name)
                # The user-facing label for this instance, so the home
                # screen shows what the user called it rather than the
                # derived tmux handle. None falls back to the name.
                # KEYED ON THE FULL TRIPLE, because a listing row already
                # carries its creation epoch - the exact key is free here,
                # so there is no reason to accept the name-only read's
                # weaker guarantee on a row a human reads as identity.
                row["label"] = self._label_for_instance(
                    name, row.get("created_at_epoch")
                )
                # The durable row id the user can point at, plus its
                # parent when it is a fork. Same exact-key read as the
                # label above: an id is read AS identity, so a name-only
                # lookup that could return a dead session's id is not
                # acceptable. An external session has no row and gets
                # None, which the UI renders as nothing rather than an
                # invented number.
                identity = self._identity_for_instance(
                    name, row.get("created_at_epoch")
                )
                row["session_row_id"] = identity["id"] if identity else None
                row["parent_session_id"] = (
                    identity["parent_session_id"] if identity else None
                )
                status_row = status_map.get(name)
                raw_tmux_status = status_row["status"] if status_row else STATUS_UNKNOWN
                # feat/hook-driven-status - attachable rows have no live
                # session_id (nothing is currently attached to them), so
                # there is no hook signal to consult by construction: no
                # hook can ever fire for a session with no running process
                # bound to it. Map straight from tmux + the persisted
                # unread flag, never claiming a hook-driven state we have
                # no evidence for.
                unread = self._is_unread(name)
                row["status"] = map_tmux_fallback(raw_tmux_status, unread=unread)
                row["unread"] = unread
                # S9 - listing NOW fingerprints (cached per instance
                # triple, see ``_fingerprint_agent_type_for_listing``),
                # so a row's family pill says what the session is
                # actually running instead of a uniform "unknown family"
                # for every external session. ``from_fingerprint=True``
                # unconditionally: every value this branch can produce
                # came from the scrollback scan, never from a stored
                # launch choice, so the pill must always render as a
                # GUESS (dashed) rather than a fact - even when the scan
                # found nothing and the result is still "unknown".
                # THE RECORDED ANSWER COMES FIRST. Fingerprinting every
                # row unconditionally threw away a fact in order to
                # render a guess: for a session THIS APP LAUNCHED the
                # agent is on record, because the launcher chose the
                # command and ran it. Only when nothing was recorded -
                # an adopted or externally-created session - does the
                # scrollback scan run, and only then is the result an
                # inference. ``from_fingerprint`` is now derived from
                # WHICH branch answered rather than hardcoded True, so
                # the pill's dashed treatment tracks the actual
                # provenance instead of the code path.
                probe_socket = self._last_probe_socket or self._tmux_socket_name()
                launch = self._stored_launch_for_listing(
                    socket=probe_socket,
                    name=name,
                    epoch=row.get("created_at_epoch"),
                )
                if launch.known:
                    effective_agent_type = launch.agent_type
                    from_fingerprint = False
                else:
                    effective_agent_type = self._fingerprint_agent_type_for_listing(
                        socket=probe_socket,
                        name=name,
                        epoch=row.get("created_at_epoch"),
                    )
                    from_fingerprint = True
                row["agent_type"] = effective_agent_type
                display_family, display_family_source = resolve_family_for_display(
                    effective_agent_type,
                    _configured_wrappers(),
                    from_fingerprint=from_fingerprint,
                )
                row["agent_family"] = display_family.name if display_family else None
                row["agent_family_source"] = display_family_source
        # refused_rows is carried through: this method REWRAPS the
        # backend's listing, and dropping the count here would hand every
        # downstream absence-based caller a partial list that claims to
        # be complete.
        return TmuxListing.answered(rows, reason=listing.reason,
                                    detail=listing.detail,
                                    refused_rows=listing.refused_rows)

    async def adopt_external_session(
        self,
        name: str,
        confirm_detach: bool = False,
        initial_cols: Optional[int] = None,
        initial_rows: Optional[int] = None,
    ) -> dict:
        """Adopt an externally-created tmux session on our socket.

        Multi-session: this NEVER detaches another session and NEVER
        raises 409. ``confirm_detach`` is accepted for API back-compat
        and IGNORED - multiple adopted/owned sessions coexist. If a
        session with this exact id (``adopted:<name>``) is already
        registered (re-adopt by another tab), its old backend is wiped
        first before the fresh attach.

        Ordered sequence (fixes the scrollback/WS race):
          1. Build a ``TmuxBackend.for_external(name, ...)`` instance.
          2. ``attach_existing(needs_pipe_setup=True)`` - starts pipe-pane
             BEFORE any scrollback capture so the FIFO is warm.
          3. Record ``fifo_start_offset = os.path.getsize(pipe_path)``
             right after pipe-pane is active - the WS tailer seeks here
             so the client doesn't see bytes already painted via scrollback.
          3b. Resize the pane to the attaching client's dimensions, when
             it supplied them. An external session is born 80x24 and this
             app never attaches a tmux client, so nothing else will ever
             reshape it - measured 2026-08-17, an adopted session sat at
             80x24 next to an app-created 163x46 one on the same socket.
             This runs BEFORE the capture so the captured bytes are
             emitted at the width the client will render them at, which
             is the same ordering the rejoin path already uses.
          4. Capture scrollback via ``backend.capture_scrollback()``.
          5. Register the session/backend (keyed ``adopted:<name>``) and
             stash the FIFO offset for the WS handler to consume.

        The adopted session is NOT added to ``owned_tmux_sessions`` - it
        isn't ours, we're borrowing it.

        Args:
            name: literal tmux session name on our socket.
            confirm_detach: accepted for API back-compat, ignored.
            initial_cols: client-measured grid width, or None.
            initial_rows: client-measured grid height, or None. Both must
                be supplied for the pre-capture resize to run; one alone
                is not enough to describe a grid and is ignored.

        Returns:
            dict with ``session``, ``initial_scrollback_b64``, and
            ``fifo_start_offset`` keys (route wraps in AdoptSessionResponse).

        Raises:
            RuntimeError: pane already dead, or pipe-pane setup failed.
            ValueError: if ``name`` contains tmux target separators.
        """
        _ = confirm_detach  # accepted for API back-compat; intentionally ignored
        adopted_id = f"adopted:{name}"
        # Re-adopt of an already-attached session: tear down the stale
        # backend for this exact id first (best-effort) so we don't leak
        # two pipe-pane tailers on the same FIFO.
        if adopted_id in self.backends:
            old_backend = self.backends.get(adopted_id)
            old_iw = self.idle_watchers.get(adopted_id)
            if old_iw is not None:
                try:
                    await old_iw.stop()
                except Exception:
                    pass
            if old_backend is not None:
                rt = getattr(old_backend, "_reader_task", None)
                if rt is not None:
                    try:
                        rt.cancel()
                        try:
                            await rt
                        except (asyncio.CancelledError, Exception):
                            pass
                    except Exception:
                        pass
            self._wipe_session_state(adopted_id)

        # S7 - PERSIST THE CLAIM BEFORE ATTACHING, so the liveness gate
        # runs before anything is torn down or built. ``origin`` is
        # written once and never recomputed, which is what makes an
        # adopted session stay ours across a restart. A session that
        # died between the client's listing and this click is caught
        # here and raised as a NAMED gone error - the route answers
        # "that session is no longer there" with a refresh, and NO ROW
        # IS MARKED ADOPTED. Every other persistence failure is logged
        # and the adoption continues: the user gets his session, and the
        # badge falls back to the legacy name tier rather than the whole
        # request failing over a bookkeeping write.
        from src.core.session_adopt_persist import (
            PERSIST_SESSION_GONE,
            AdoptTargetGoneError,
        )

        adopt_persist = self.persist_adoption(name)
        if adopt_persist.outcome == PERSIST_SESSION_GONE:
            raise AdoptTargetGoneError(
                adopt_persist.detail or "that session is no longer there"
            )
        if not adopt_persist.persisted:
            logger.warning(
                "adopt_not_persisted",
                session=name,
                outcome=adopt_persist.outcome,
                detail=adopt_persist.detail,
                note=(
                    "the session is being adopted but the claim was not "
                    "recorded, so its badge is not durable yet"
                ),
            )

        # Resolve the adopted pane's cwd via a one-shot tmux probe. We
        # use this for metadata display only - we never chdir.
        working_dir = await self._resolve_external_cwd(name)

        # Late import: src.core.tmux_backend imports SessionBackend from
        # session_backend, which we already import - no cycle - but
        # keeping the import local matches the pattern in build_backend.
        from src.core.tmux_backend import TmuxBackend

        backend = TmuxBackend.for_external(
            session_name=name,
            working_dir=working_dir,
            on_output=self._make_output_handler(adopted_id),
            socket_name=settings.load_auth_config().session.tmux_socket_name,
            scrollback_lines=settings.load_auth_config().session.scrollback_lines,
        )

        # Step 3 - ensure pipe-pane BEFORE capturing scrollback so the
        # FIFO is guaranteed warm at the moment we read its size.
        await backend.attach_existing(needs_pipe_setup=True)

        # Step 3b - reshape the adopted pane to the client's grid before
        # anything reads it. See the docstring: without this an adopted
        # session keeps tmux's 80x24 birth geometry forever.
        if initial_cols and initial_rows:
            try:
                backend.resize(initial_cols, initial_rows)
                logger.info(
                    "adopted_session_resized",
                    session=name,
                    cols=initial_cols,
                    rows=initial_rows,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                # A failed pre-resize is not a reason to refuse the
                # adoption: the WS resize handshake reshapes the pane
                # again once the socket opens. Say so rather than
                # swallowing it, because the scrollback about to be
                # captured is then at the wrong width.
                logger.warning(
                    "adopted_session_resize_failed",
                    session=name,
                    cols=initial_cols,
                    rows=initial_rows,
                    error=str(exc),
                )

        # Step 4 - record FIFO offset immediately. Any bytes that hit
        # the FIFO between this line and the scrollback capture below
        # will be BOTH in the scrollback AND after the offset - that's
        # fine; the client paints the scrollback first and the tailer
        # seeks past the offset, so the overlap is bounded and
        # well-defined.
        #
        # We use ``os.path.getsize`` over ``Path.stat().st_size`` to
        # avoid constructing a Path just for this read; the backend
        # already resolved the path.
        try:
            fifo_start_offset = os.path.getsize(str(backend._pipe_path))
        except OSError as exc:
            logger.warning(
                "adopt_fifo_offset_read_failed",
                session=name,
                error=str(exc),
            )
            fifo_start_offset = 0

        # Step 5 - capture scrollback AFTER the offset read so anything
        # that arrives mid-capture is safely past the offset (the tailer
        # will stream it without duplication).
        scrollback = backend.capture_scrollback()

        sb_b64 = (
            base64.b64encode(scrollback).decode("ascii")
            if scrollback else ""
        )

        # Phase 7 - fingerprint the captured bytes to identify which AI
        # CLI is running inside the adopted tmux session. ``None`` is a
        # valid outcome and renders as "Unknown" in the UI (Phase 8).
        from src.core.agent_fingerprint import detect_agent_type
        try:
            scrollback_text = scrollback.decode("utf-8", errors="replace")
        except Exception:
            scrollback_text = ""
        detected_agent_type = detect_agent_type(scrollback_text)
        logger.info(
            "agent_fingerprint_detected",
            session=name,
            agent_type=detected_agent_type,
        )

        # Step 5 - register.
        # v0.7.0 - project-scoped theme lookup: ``<working_dir>/.cc.theme``
        # is the source of truth. ``pinned_themes.json`` is read as a
        # back-compat fallback only when no dotfile exists; the
        # migration helper below ferries old entries into the new format.
        prior_pin = self.resolve_project_theme(working_dir, name)
        # fix/adopt-response-pid - ``_session_info_for`` still resolves
        # ``pty_pid`` LIVE on every subsequent read (a tmux pane's
        # foreground pid changes over the session's life, so any value
        # captured here goes stale eventually regardless). But the
        # ADOPT RESPONSE ITSELF (``AdoptSessionResponse.session``) is
        # built from THIS ``adopted_session`` object directly in
        # ``routes.adopt_session`` - it never goes through
        # ``_session_info_for``. Leaving this None meant the client's
        # very first paint (and everything cached from it - see
        # client/js/terminal.js ``connectToSession``) showed "PID: ?"
        # forever, even though a later GET /sessions would have shown
        # the real pid. Reusing ``TmuxBackend.pid`` (same property
        # ``create_session`` already uses below) instead of inventing a
        # third pid-resolution path - one extra ``display-message``
        # call, paid once per adopt, not on a hot path.
        adopted_session = Session(
            id=adopted_id,
            pty_pid=getattr(backend, "pid", None),
            working_dir=str(working_dir),
            status=SessionStatus.RUNNING,
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
            agent_type=detected_agent_type,
            # feat/agent-family-pills - this value came from scrollback
            # fingerprinting above, not a launch/config choice. True
            # regardless of whether detection actually found a match:
            # ``resolve_family_for_display`` already renders a None/blank
            # agent_type as "unknown" independent of this flag; the flag
            # only changes rendering for a value it DID find.
            agent_type_via_fingerprint=True,
            pinned_theme=prior_pin,
            # PIN-FIX-EXECUTE - carry the bare tmux name so frontend uses
            # it (not the "adopted:" prefixed id) as the pin-key handle.
            tmux_session=name,
        )
        self._register_session(adopted_session, backend)
        # Best-effort migration AFTER the read so the read remains
        # deterministic (dotfile beats JSON when both exist post-migration).
        # Failures here are logged + swallowed; never block adopt.
        try:
            self.migrate_pinned_theme_to_dotfile(adopted_session)
        except Exception as exc:  # pragma: no cover - helper already swallows
            logger.debug("post_adopt_migrate_unexpected_throw", error=str(exc))

        # v0.7.0 Part 3 - mint a hook token for the adopted session and
        # best-effort push the env into the live tmux session via
        # ``set-environment``. CAVEAT: tmux's session env propagates to NEW
        # processes spawned in panes; the already-running ``claude`` (the
        # whole reason we're adopting) won't see it. So Stop / Notification
        # / PermissionRequest hooks will only fire for adopted sessions
        # IFF the user re-launches ``claude`` inside the pane after we
        # adopt. The token is registered regardless so any such re-launch
        # works without extra plumbing.
        # Same reasoning as the create path: record the tmux name WITH
        # the token, so a hook arriving after a restart can still be
        # resolved to a session. An adopted session is exactly the case
        # that needs it - its id is minted here and exists nowhere else.
        self._mint_hook_token(adopted_id, tmux_name=name)
        spawn_env = self.get_env_for_spawn(adopted_id)
        try:
            for var, val in spawn_env.items():
                await backend._run_tmux(
                    "set-environment", "-t", name, var, val, check=False
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "adopt_set_environment_failed",
                session=name,
                error=str(exc),
            )
        # External sessions are intentionally NOT added to
        # ``owned_tmux_sessions`` - we don't own them; we adopted them.
        self._save_session_metadata(adopted_session)

        # Stash the FIFO offset for THIS session's WS tailer to consume.
        self.adopt_fifo_offsets[adopted_id] = fifo_start_offset

        # Spin up IdleWatcher per the normal create path so notifications
        # fire for adopted sessions too. Router may be None in tests.
        if self._notification_router is not None:
            try:
                auth_config = settings.load_auth_config()
                threshold = getattr(
                    auth_config.notifications,
                    "idle_threshold_seconds",
                    30.0,
                )
            except Exception:
                threshold = 30.0
            iw = IdleWatcher(
                session_slug=adopted_id,
                router=self._notification_router,
                threshold_s=threshold,
            )
            await iw.start()
            self.idle_watchers[adopted_id] = iw

        logger.info(
            "session_adopted_external",
            session=name,
            working_dir=str(working_dir),
            fifo_start_offset=fifo_start_offset,
            scrollback_bytes=len(scrollback),
        )

        return {
            "session": adopted_session,
            "initial_scrollback_b64": sb_b64,
            "fifo_start_offset": fifo_start_offset,
        }

    async def destroy_external_session(self, name: str) -> dict:
        """Destroy an external (or otherwise non-active) tmux session by name.

        Counterpart to ``destroy_session`` for the launchpad's "X" button on
        a row that is NOT the currently-active backend. The previous flow was
        adopt-then-destroy, which fails with ``RuntimeError("pane already
        dead")`` for sessions where the foreground process exited (e.g. user
        Ctrl-D'd ``claude``) - leaving the session permanently un-killable
        from the UI. This path skips adoption entirely and just runs
        ``tmux -L <socket> kill-session -t <name>`` directly.

        Refuses to destroy the currently-active backend's session - the
        caller should use ``DELETE /sessions`` for that path so the in-memory
        backend, idle watcher, local-server tracker entries, and metadata
        get torn down cleanly.

        ``kill-session`` is treated as idempotent: a missing session is
        success (returns ``already_gone=True``) so the UI converges even
        when tmux state drifts under us.

        Args:
            name: literal tmux session name as shown in the running list.

        Returns:
            ``{"name": <name>, "killed": bool, "already_gone": bool}``

        Raises:
            ValueError: name contains tmux target separators, or name
                matches the currently-active backend's session.
        """
        from src.core.tmux_backend import _safe_target, DEFAULT_SOCKET_NAME

        # Guard: a session currently bound to a live backend must be torn
        # down via the full destroy path (DELETE /sessions[?session_id=])
        # so reader task + idle watcher + metadata get cleaned up. Calling
        # kill-session out from under a live backend would orphan all that.
        for sid, backend in self.backends.items():
            if getattr(backend, "tmux_session", None) == name:
                raise ValueError(
                    f"{name!r} is a currently-active session (id={sid!r}); "
                    "use DELETE /sessions to destroy it"
                )

        # Validate the name as a tmux target - same rule as adoption,
        # so we don't accidentally interpret ':' or '.' as separators.
        try:
            target = _safe_target(name)
        except ValueError:
            raise

        try:
            socket_name = settings.load_auth_config().session.tmux_socket_name
        except Exception:
            socket_name = DEFAULT_SOCKET_NAME

        logger.info("destroying_external_session", name=name, socket=socket_name)

        proc = await asyncio.create_subprocess_exec(
            "tmux", "-L", socket_name, "kill-session", "-t", target,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        rc = proc.returncode or 0

        # Drop ownership tracking regardless - if it WAS owned and we
        # just killed it, the entry is now stale; if it wasn't owned,
        # the discard is a no-op. Persist so a server restart doesn't
        # resurrect the pruned entry.
        if name in self.owned_tmux_sessions:
            self.owned_tmux_sessions.discard(name)
            try:
                self._save_session_metadata()
            except Exception as exc:
                logger.warning(
                    "owned_tmux_sessions_save_after_external_destroy_failed",
                    name=name,
                    error=str(exc),
                )

        # SESSION-IDENTITY-V2 - drop any pinned theme for this name so
        # killing a session also evicts its preference. No-op if no pin
        # was set.
        self.discard_pinned_theme(name)

        if rc == 0:
            logger.info("external_session_destroyed", name=name)
            return {"name": name, "killed": True, "already_gone": False}

        # tmux returns non-zero for "session not found" too - treat that
        # as success so the UI converges. We match against the canonical
        # phrasing tmux emits: "can't find session: <name>".
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if "can't find session" in stderr_text.lower() or "session not found" in stderr_text.lower():
            logger.info(
                "external_session_already_gone", name=name, stderr=stderr_text
            )
            return {"name": name, "killed": False, "already_gone": True}

        # Genuine failure - surface as RuntimeError so the route layer
        # turns it into a 500 with the tmux stderr in the detail. This
        # is the only path that should ever 500.
        logger.error(
            "external_session_destroy_failed",
            name=name,
            returncode=rc,
            stderr=stderr_text,
        )
        raise RuntimeError(
            f"tmux kill-session for {name!r} failed (rc={rc}): {stderr_text}"
        )

    def _agent_command_for_tmux_name(self, name: str) -> Optional[str]:
        """Command this app would launch for the session with this tmux name.

        Description: looks up the in-memory ``Session`` carrying this tmux
            name and re-derives its launch command through
            ``Settings.get_agent_command`` using the ``agent_type`` and
            ``model`` recorded at create time.

            RE-DERIVED, NOT REPLAYED, and that is the point. The user's
            reported case is "I exited to update Claude, now start it
            again", so a restart has to run the CURRENT command for that
            agent type - a new wrapper path, a new CLI location - not the
            string that was executed weeks ago.

            Returning None is a real answer, not a failure: it means this
            app has no record of what this session runs, which is the
            normal state for a session the user started outside CloudeCode.
            The caller's ladder then falls back to tmux's own record.

        Inputs:
            name: literal tmux session name.

        Output:
            Optional[str]: shell command string, or None when this app has
                no record for that name or the config could not be read.

        Example:
            >>> mgr._agent_command_for_tmux_name("cloude_api")
            "zsh -c 'source ~/.zshrc ...; cld'"
        """
        match = None
        for session in self.sessions.values():
            if getattr(session, "tmux_session", None) == name:
                match = session
                break
        if match is None:
            return None

        agent_type = getattr(match, "agent_type", None)
        if not agent_type:
            return None

        try:
            return settings.get_agent_command(
                agent_type, model=getattr(match, "model", None)
            )
        except Exception as exc:
            # A config we cannot read is "no record", never a guess. The
            # ladder degrades to tmux's own start command, which is a
            # worse-but-honest answer rather than an invented one.
            logger.warning(
                "respawn_agent_command_unresolved",
                tmux_name=name,
                agent_type=agent_type,
                error=str(exc),
            )
            return None

    async def respawn_session(
        self, name: str, *, socket_name: Optional[str] = None
    ) -> dict:
        """Restart the agent inside a dead session, keeping the session.

        Description: the server half of the launcher's restart control.
            ``remain-on-exit`` means a session whose agent exited still
            exists - window, pane, pane id, scrollback and the app's
            ``pipe-pane`` all intact - so this puts a process back into
            that pane rather than building anything new.

            IDENTITY IS PRESERVED BY CONSTRUCTION, NOT BY COPYING FIELDS.
            The durable identity of a session row is the instance triple
            ``(tmux_socket, tmux_name, tmux_created_epoch)``, and
            ``#{session_created}`` belongs to the SESSION, not to the
            pane's process - a respawn does not change it. So every
            existing lookup keeps matching the SAME row and its
            ``session_uuid``, ``origin``, ``project_id``, ``pinned_theme``,
            unread state and title are untouched because nothing writes
            them. This method issues NO database write at all.

            THAT IS ALSO WHAT SEPARATES IT FROM A FORK. A fork deliberately
            mints a new row and sets ``parent_session_id`` / ``fork_kind``.
            A respawn cannot mint one, because there is no new instance for
            a row to key on, and it never touches either column.

            AN ALREADY-BOUND BACKEND IS REUSED, not rebuilt. When the user
            has the session open in the app there is a live ``TmuxBackend``
            with a running tail loop; respawning through it means the
            existing ``pipe-pane`` (which survives the respawn - measured)
            keeps streaming and the open terminal simply comes back to
            life. Building a second backend for the same pane would leave
            two readers on one file.

        Inputs:
            name: literal tmux session name as shown in the session list.
            socket_name: tmux socket override. Internal/test use only -
                the HTTP route never passes it, so a client cannot aim
                this at another socket. Defaults to the configured one.

        Output:
            dict: ``{"name", "kind", "ok", "detail", "command"}``.
                ``ok`` False with a ``kind`` of ``cannot_determine`` is a
                normal, successful API call reporting that the pane could
                not be read - it is not an error.

        Raises:
            ValueError: ``name`` contains a tmux target separator.

        Example:
            >>> await mgr.respawn_session("cloude_api")
            {'name': 'cloude_api', 'kind': 'agent', 'ok': True, ...}
        """
        from src.core.tmux_backend import (
            DEFAULT_SOCKET_NAME,
            TmuxBackend,
            _safe_target,
        )

        # Same validation as adoption and external destroy: a name holding
        # ':' or '.' would be read by tmux as a window/pane target.
        _safe_target(name)

        if socket_name is None:
            try:
                socket_name = settings.load_auth_config().session.tmux_socket_name
            except Exception:
                socket_name = DEFAULT_SOCKET_NAME

        agent_command = self._agent_command_for_tmux_name(name)

        backend = None
        for candidate in self.backends.values():
            if getattr(candidate, "tmux_session", None) == name:
                backend = candidate
                break

        if backend is None or not hasattr(backend, "respawn"):
            # No live backend (or a non-tmux one): build a bare handle
            # purely to issue the tmux calls. Nothing is adopted, no
            # pipe-pane is started, and it is discarded on return.
            backend = TmuxBackend.for_external(
                session_name=name,
                working_dir=Path(settings.default_working_dir).expanduser(),
                on_output=None,
                socket_name=socket_name,
            )

        logger.info(
            "respawn_session_request",
            name=name,
            socket=socket_name,
            has_agent_record=agent_command is not None,
        )

        result = await backend.respawn(agent_command=agent_command)

        return {
            "name": name,
            "kind": result.kind,
            "ok": result.ok,
            "detail": result.detail,
            "command": result.command,
        }

    async def _resolve_external_cwd(self, name: str) -> Path:
        """Best-effort cwd probe for an adopted tmux pane.

        Reads ``#{pane_current_path}`` via ``tmux display-message``.
        Falls back to ``~`` on any failure - metadata only, never chdir.
        """
        from src.core.tmux_backend import _safe_target, DEFAULT_SOCKET_NAME

        try:
            socket_name = settings.load_auth_config().session.tmux_socket_name
        except Exception:
            socket_name = DEFAULT_SOCKET_NAME

        try:
            target = _safe_target(name)
        except ValueError as exc:
            logger.warning(
                "adopt_cwd_unsafe_target", name=name, error=str(exc)
            )
            return Path.home()

        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "-L", socket_name, "display-message",
                "-t", target, "-p", "#{pane_current_path}",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return Path.home()
            raw = out.decode("utf-8", errors="replace").strip()
            if not raw:
                return Path.home()
            path = Path(raw)
            return path if path.exists() else Path.home()
        except Exception as exc:
            logger.debug("adopt_cwd_probe_failed", name=name, error=str(exc))
            return Path.home()

    def consume_adopt_fifo_offset(
        self, session_id: Optional[str] = None
    ) -> Optional[int]:
        """One-shot read of a session's adopt FIFO offset (None if unset/consumed).

        The WS tailer calls this exactly once on connect. We clear the
        stashed value so a reconnect later doesn't re-seek to a stale
        offset against a (by then) much larger FIFO. ``session_id`` None
        → the current session.
        """
        sid = self._resolve_session_id(session_id)
        if not sid:
            return None
        return self.adopt_fifo_offsets.pop(sid, None)

