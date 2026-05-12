"""Session manager for Claude Code instances.

Multi-concurrent-session design: holds any number of live `SessionBackend`
instances, keyed by ``session_id`` — two browser tabs can each attach to a
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
import json
import os
import re
import base64
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime
from fastapi import HTTPException
import structlog

from src.config import settings
from src.models import Session, SessionStatus, SessionInfo, SessionStats, LogEntry
from src.core.session_backend import SessionBackend, build_backend
from src.core.tmux_backend import SESSION_PREFIX
from src.core.notifications.idle_watcher import IdleWatcher
from src.core.upload_sweeper import UPLOAD_DIR_NAME, UploadSweeper
from src.utils.pty_session import PTYSessionError
from src.utils.template_manager import copy_templates as copy_template_files

logger = structlog.get_logger()


_TMUX_FORBIDDEN_CHARS = re.compile(r"[.:]")
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
    stay ``None`` — the Phase 7 fingerprint detector populates them
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

    tmux forbids only '.' (pane separator) and ':' (window separator) — everything else
    (spaces, case, unicode, emoji, punctuation) is legal. This helper preserves the
    original name as closely as possible.

    Rules:
      1. Replace any '.' or ':' with '_'.
      2. Collapse runs of whitespace (including newlines/tabs) into a single space.
      3. Strip leading and trailing whitespace.

    Returns empty string for truly empty/whitespace-only input (caller's fallback signal).
    """
    if not name:
        return ""
    replaced = _TMUX_FORBIDDEN_CHARS.sub("_", name)
    collapsed = _WHITESPACE_RUN.sub(" ", replaced)
    return collapsed.strip()


class SessionManager:
    """Manages Claude Code sessions via a pluggable SessionBackend."""

    def __init__(self):
        """Initialize the session manager."""
        # ---- per-session state, keyed by session_id ---------------------
        # Multiple sessions coexist; two browser tabs can each be attached
        # to a different session. Touching one session's entry NEVER
        # touches another's — that isolation is the whole point.
        self.sessions: dict[str, Session] = {}
        self.backends: dict[str, SessionBackend] = {}
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
        # time — consumed once by the WS tailer. See ``consume_adopt_fifo_offset``.
        self.adopt_fifo_offsets: dict[str, int] = {}
        # Most-recently-created/adopted session id — backs the back-compat
        # ``current_session()`` / ``self.session`` / ``self.backend`` views.
        self._last_session_id: Optional[str] = None
        # Notification router reference — set by ``attach_notification_router``
        # during FastAPI lifespan startup (after both the SessionManager and
        # the router are constructed). When None, IdleWatcher instantiation
        # is skipped and no notification events fire.
        self._notification_router = None

        # Track 1 — adopt-external-session support.
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

        # SESSION-IDENTITY-V2 — durable per-tmux-name pinned-theme map.
        # Lives in its own file (``pinned_themes.json``) so it survives
        # detach + swap + re-adopt cycles; ``session_metadata.json`` is
        # unlinked on detach and overwritten on swap and so cannot
        # function as the source of truth for a per-name pin. Mutated
        # by ``set_pinned_theme`` / ``clear_pinned_theme``; consulted by
        # ``adopt_external_session`` (seeds Session.pinned_theme on
        # re-entry) and ``list_attachable_sessions`` (decorates rows so
        # the launchpad can paint the pin without entering the session).
        self.pinned_themes: dict[str, str] = {}

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
    # these; do NOT assign to them from new code — touch the dicts instead.
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
        ``self._subscribers[session_id]`` — destroying session A never
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
        - Reconcile ``owned_tmux_sessions`` against the live listing —
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
          cleanup is out of scope — a v2 ``cloude-cleanup`` script).

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
        # Phase 6 — one-shot agent_type backfill. Logic extracted to the
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
        tmux_alive = set(probe.discover_existing())

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

        # SESSION-IDENTITY-V2 — prune pinned-theme entries whose tmux
        # session is gone. We only prune when we have a confirmed live
        # tmux probe (non-empty ``tmux_alive`` OR a successful empty
        # listing — both mean the probe ran). Prevents indefinite
        # growth from sessions the user destroyed outside our UI
        # (e.g. ``tmux -L cloude kill-session``).
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

        if persisted is None:
            # No metadata on disk → nothing to re-adopt.
            if tmux_alive:
                logger.info(
                    "session_backend_discovered_orphans",
                    count=len(tmux_alive),
                    names=sorted(tmux_alive),
                    hint="no metadata on disk — leaving orphans alone",
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
        ownership_ok = (
            target_name is not None
            and (
                target_name in self.owned_tmux_sessions
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
        try:
            if metadata_path.exists():
                metadata_path.unlink()
                logger.info("stale_session_metadata_deleted")
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
        auth_cfg = settings.load_auth_config()
        cfg = auth_cfg.uploads
        if not cfg.enabled:
            return
        sweeper = UploadSweeper(
            ttl_seconds=cfg.ttl_seconds,
            interval_seconds=0,
            project_paths=[p.path for p in auth_cfg.projects],
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

        Unlike the pre-refactor code, we do NOT probe the process here — at
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
        — this is metadata, not a source of truth for money. Losing
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
                # continue — the rename is still atomic per POSIX.
                logger.debug("metadata_fsync_unsupported", error=str(exc))

        os.replace(str(tmp), str(path))

    def _save_session_metadata(self, session: Optional[Session] = None):
        """Save session metadata atomically, including the owned-set.

        Persists ``session`` if given, else the current session. Only one
        session is ever persisted across restarts — the most-recently-
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
            # the new schema — one successful save is the migration.
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

    def get_pinned_theme(self, tmux_name: str) -> Optional[str]:
        """Return the persisted pin for a tmux session name, or None."""
        if not tmux_name:
            return None
        return self.pinned_themes.get(tmux_name)

    def set_pinned_theme(
        self, tmux_name: str, theme_id: Optional[str]
    ) -> None:
        """Persist (or clear) the pinned theme for a tmux session name.

        ``theme_id`` None or empty clears the pin. Always persists — a
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
                    # ``pinned_themes.json`` — adopt seeds from there).
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

    # ---- output fan-out (per session) -----------------------------------

    def subscribe_output(self, session_id: Optional[str] = None) -> asyncio.Queue:
        """Subscribe to a session's backend output stream.

        ``session_id`` None → the current session (back-compat). The
        returned queue receives ONLY that session's bytes (base64-encoded
        strings); a session's output never leaks into another's queue.
        """
        sid = self._resolve_session_id(session_id)
        # Tolerate "no session yet" — return an orphan queue so callers
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
    ) -> Session:
        """Create a new Claude Code session.

        Multiple sessions coexist — this does NOT raise if other sessions
        are live (the old single-active invariant is gone). A zombie
        session matching this exact ``session_id`` (stale metadata, dead
        backend) is cleaned up first.

        ``initial_cols`` / ``initial_rows`` are forwarded to the backend's
        ``start()`` so the pane is birthed at the client's measured size.
        Both must be supplied together or both omitted; backends fall back
        to their own defaults otherwise. The WS resize handshake reshapes
        later regardless — these are strictly a birth-time optimization.

        ``project_name`` (optional) is the human-readable project label from
        the launchpad. When supplied and non-empty after sanitization, the
        resulting tmux session is named ``cloude_<sanitized name>`` verbatim
        instead of falling back to the legacy ``cloude_ses_<hex>`` derivation
        keyed off ``session_id``. An empty/whitespace-only value (or one that
        sanitizes to empty) silently falls back to legacy naming — this is
        by design so the launchpad can always send the field without special-
        casing blanks. PTYBackend ignores the override entirely.
        """
        # Clean up a zombie entry for this exact id (stale metadata / dead
        # backend) — but leave any OTHER live sessions alone.
        if session_id in self.sessions and (
            session_id not in self.backends
            or not self.backends[session_id].is_alive()
        ):
            logger.info("cleaning_up_zombie_session", session_id=session_id)
            self._wipe_session_state(session_id)

        # Phase 6 — resolve effective agent_type. Precedence:
        #   1. explicit ``agent_type`` kwarg (request-level override)
        #   2. project-level default (ProjectConfig.agent_type) when
        #      ``project_name`` matches a configured project
        #   3. ``"claude"`` as the final safe default
        # Resolved value is persisted on the Session and drives
        # ``settings.get_agent_command(...)`` for the launch string.
        resolved_agent_type: Optional[str] = agent_type
        if not resolved_agent_type and project_name:
            try:
                proj = settings.get_project(project_name)
                if proj is not None:
                    resolved_agent_type = proj.agent_type
            except Exception:
                # Don't fail session create if config lookup misbehaves.
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

        # Adopt-on-collision: if project_name resolves to a tmux session name
        # that is already alive on our socket, reuse it rather than erroring.
        # Matches "open project X" == "resume my X session whether alive or not."
        # The probe is a throwaway — never assigned to self.backend, never started.
        if tmux_session_name:
            probe = build_backend(
                settings,
                session_id="__collision_probe__",
                working_dir=Path.home(),
                on_output=None,
            )
            try:
                existing = probe.discover_existing() or []
            except Exception as exc:
                logger.debug("collision_probe_failed", error=str(exc))
                existing = []
            if tmux_session_name in existing:
                logger.info(
                    "session_create_redirected_to_adopt",
                    project=project_name,
                    existing_tmux=tmux_session_name,
                )
                result = await self.adopt_external_session(
                    name=tmux_session_name,
                    confirm_detach=True,
                )
                # adopt_external_session returns dict {session, initial_scrollback_b64,
                # fifo_start_offset}; create_session must return Session — unwrap.
                return result["session"] if isinstance(result, dict) else result

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

            if auto_start_claude:
                # Phase 6 — resolve via the agents map. For claude with
                # default config this yields the same string the old
                # ``f"{claude_cli} --dangerously-skip-permissions"`` did
                # (CLAUDE_CLI_PATH env-fallback preserved inside the helper).
                command = settings.get_agent_command(resolved_agent_type)
                await backend.start(
                    command=command,
                    initial_cols=initial_cols,
                    initial_rows=initial_rows,
                )
            else:
                await backend.start(
                    initial_cols=initial_cols,
                    initial_rows=initial_rows,
                )

            # Best-effort PID for metadata: TmuxBackend doesn't track a single
            # pid, PTYBackend exposes one via `.pid`.
            pid = getattr(backend, "pid", None)

            new_session = Session(
                id=session_id,
                pty_pid=pid,
                working_dir=str(work_path),
                status=SessionStatus.RUNNING,
                created_at=datetime.utcnow(),
                last_activity=datetime.utcnow(),
                agent_type=resolved_agent_type,
                # PIN-FIX-EXECUTE — carry the bare tmux name on the inner
                # Session so frontend can use it as the pin-key handle
                # without falling back to session.id.
                tmux_session=tmux_session_name,
            )

            self._register_session(new_session, backend)

            # Track 1: record tmux-backend ownership so a post-create crash
            # still leaves the name recoverable from ``session_metadata.json``
            # — and the adopt UI correctly flags it as ``created_by_cloude``.
            owned_name = getattr(backend, "tmux_session", None)
            if owned_name:
                self.owned_tmux_sessions.add(owned_name)

            self._save_session_metadata(new_session)

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
            )

            return new_session

        except PTYSessionError as e:
            logger.error("session_creation_failed", error=str(e))
            await self._cleanup_failed_create(session_id, backend, idle_watcher)
            raise ValueError(f"Failed to create session: {e}") from e
        except RuntimeError as e:
            # Backend.start() raises RuntimeError for hard infrastructure
            # failures: tmux missing on PATH, ``new-session`` non-zero exit,
            # OR — added in the dead-on-arrival probe — when the spawned
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
        later — but it leaves the tmux session itself alive. Other live
        sessions are untouched.

        Why stop pipe-pane here (vs leaving it attached): our pipe-pane
        writes into ``tmux_<slug>.pipe``; the subsequent re-adopt via
        ``TmuxBackend.for_external`` derives its pipe path as
        ``tmux_ext_<slug>.pipe`` — a DIFFERENT file. If we leave the old
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
            # Tear down the idle watcher first — mirrors destroy ordering so
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
            # Wipe THIS session's state only — leave tmux alive, leave
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
        → the current session. Only touches THAT session's state — other
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
                # SESSION-IDENTITY-V2 — explicit destroy means this name is
                # dead; drop its pin too.
                self.discard_pinned_theme(owned_name)

            if backend is not None:
                await backend.stop()

            sess.status = SessionStatus.STOPPED

            working_dir = sess.working_dir
            if working_dir:
                uploads_dir = Path(working_dir).expanduser() / UPLOAD_DIR_NAME
                if uploads_dir.exists():
                    shutil.rmtree(uploads_dir, ignore_errors=True)
                    logger.info(
                        "upload_dir_cleaned_on_destroy", path=str(uploads_dir)
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

    def _session_info_for(self, session_id: str) -> Optional[SessionInfo]:
        """Build SessionInfo for a specific live session, or None."""
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
        return SessionInfo(
            session=sess,
            recent_logs=self.get_recent_logs(session_id=session_id),
            local_servers=[],
            stats=stats,
            session_backend=backend_name,
            tmux_session=tmux_session_name,
            agent_type=sess.agent_type,
            pinned_theme=sess.pinned_theme,
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
        out: list[SessionInfo] = []
        for sid in list(self.sessions.keys()):
            info = self._session_info_for(sid)
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

    def list_attachable_sessions(self) -> list[dict]:
        """Enumerate tmux sessions on our socket, flagged by ownership.

        Thin pass-through to ``backend.list_attachable_sessions``, but we
        always instantiate a fresh PROBE backend rather than using
        ``self.backend`` — the user should be able to list external
        sessions whether or not they currently have an active session
        (the adopt-UI fetch happens at launchpad render time).
        """
        probe = build_backend(
            settings,
            session_id="__probe__",
            working_dir=Path.home(),
            on_output=None,
        )
        rows = probe.list_attachable_sessions(
            owned_names=set(self.owned_tmux_sessions)
        )
        # SESSION-IDENTITY-V2 — decorate each row with its persisted
        # pinned theme (if any). The launchpad's active-session banner
        # uses this so re-entering a session paints the right theme on
        # first frame; without it, the client would wait until the
        # adopt response to learn the pin and the user would see a
        # one-frame Lovecraft flash before the pin paints.
        for row in rows:
            name = row.get("name")
            if name:
                row["pinned_theme"] = self.pinned_themes.get(name)
        return rows

    async def adopt_external_session(
        self, name: str, confirm_detach: bool = False
    ) -> dict:
        """Adopt an externally-created tmux session on our socket.

        Multi-session: this NEVER detaches another session and NEVER
        raises 409. ``confirm_detach`` is accepted for API back-compat
        and IGNORED — multiple adopted/owned sessions coexist. If a
        session with this exact id (``adopted:<name>``) is already
        registered (re-adopt by another tab), its old backend is wiped
        first before the fresh attach.

        Ordered sequence (fixes the scrollback/WS race):
          1. Build a ``TmuxBackend.for_external(name, ...)`` instance.
          2. ``attach_existing(needs_pipe_setup=True)`` — starts pipe-pane
             BEFORE any scrollback capture so the FIFO is warm.
          3. Record ``fifo_start_offset = os.path.getsize(pipe_path)``
             right after pipe-pane is active — the WS tailer seeks here
             so the client doesn't see bytes already painted via scrollback.
          4. Capture scrollback via ``backend.capture_scrollback()``.
          5. Register the session/backend (keyed ``adopted:<name>``) and
             stash the FIFO offset for the WS handler to consume.

        The adopted session is NOT added to ``owned_tmux_sessions`` — it
        isn't ours, we're borrowing it.

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

        # Resolve the adopted pane's cwd via a one-shot tmux probe. We
        # use this for metadata display only — we never chdir.
        working_dir = await self._resolve_external_cwd(name)

        # Late import: src.core.tmux_backend imports SessionBackend from
        # session_backend, which we already import — no cycle — but
        # keeping the import local matches the pattern in build_backend.
        from src.core.tmux_backend import TmuxBackend

        backend = TmuxBackend.for_external(
            session_name=name,
            working_dir=working_dir,
            on_output=self._make_output_handler(adopted_id),
            socket_name=settings.load_auth_config().session.tmux_socket_name,
            scrollback_lines=settings.load_auth_config().session.scrollback_lines,
        )

        # Step 3 — ensure pipe-pane BEFORE capturing scrollback so the
        # FIFO is guaranteed warm at the moment we read its size.
        await backend.attach_existing(needs_pipe_setup=True)

        # Step 4 — record FIFO offset immediately. Any bytes that hit
        # the FIFO between this line and the scrollback capture below
        # will be BOTH in the scrollback AND after the offset — that's
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

        # Step 5 — capture scrollback AFTER the offset read so anything
        # that arrives mid-capture is safely past the offset (the tailer
        # will stream it without duplication).
        scrollback = backend.capture_scrollback()

        sb_b64 = (
            base64.b64encode(scrollback).decode("ascii")
            if scrollback else ""
        )

        # Phase 7 — fingerprint the captured bytes to identify which AI
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

        # Step 5 — register.
        # SESSION-IDENTITY-V2 — restore any previously-pinned theme for
        # this tmux name (durable in pinned_themes.json across detach/swap).
        prior_pin = self.get_pinned_theme(name)
        adopted_session = Session(
            id=adopted_id,
            pty_pid=None,
            working_dir=str(working_dir),
            status=SessionStatus.RUNNING,
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
            agent_type=detected_agent_type,
            pinned_theme=prior_pin,
            # PIN-FIX-EXECUTE — carry the bare tmux name so frontend uses
            # it (not the "adopted:" prefixed id) as the pin-key handle.
            tmux_session=name,
        )
        self._register_session(adopted_session, backend)
        # External sessions are intentionally NOT added to
        # ``owned_tmux_sessions`` — we don't own them; we adopted them.
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
        Ctrl-D'd ``claude``) — leaving the session permanently un-killable
        from the UI. This path skips adoption entirely and just runs
        ``tmux -L <socket> kill-session -t <name>`` directly.

        Refuses to destroy the currently-active backend's session — the
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

        # Validate the name as a tmux target — same rule as adoption,
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

        # Drop ownership tracking regardless — if it WAS owned and we
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

        # SESSION-IDENTITY-V2 — drop any pinned theme for this name so
        # killing a session also evicts its preference. No-op if no pin
        # was set.
        self.discard_pinned_theme(name)

        if rc == 0:
            logger.info("external_session_destroyed", name=name)
            return {"name": name, "killed": True, "already_gone": False}

        # tmux returns non-zero for "session not found" too — treat that
        # as success so the UI converges. We match against the canonical
        # phrasing tmux emits: "can't find session: <name>".
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if "can't find session" in stderr_text.lower() or "session not found" in stderr_text.lower():
            logger.info(
                "external_session_already_gone", name=name, stderr=stderr_text
            )
            return {"name": name, "killed": False, "already_gone": True}

        # Genuine failure — surface as RuntimeError so the route layer
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

    async def _resolve_external_cwd(self, name: str) -> Path:
        """Best-effort cwd probe for an adopted tmux pane.

        Reads ``#{pane_current_path}`` via ``tmux display-message``.
        Falls back to ``~`` on any failure — metadata only, never chdir.
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
