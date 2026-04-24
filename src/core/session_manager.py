"""Session manager for Claude Code instances.

Single-active-session design: holds at most ONE `SessionBackend` at a time.
Backend type (tmux vs PTY) is selected at construction via
`build_backend(settings)` which reads `AuthConfig.session.backend`.
"""

import asyncio
import json
import os
import re
import base64
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
from src.utils.pty_session import PTYSessionError
from src.utils.template_manager import copy_templates as copy_template_files

logger = structlog.get_logger()


_TMUX_FORBIDDEN_CHARS = re.compile(r"[.:]")
_WHITESPACE_RUN = re.compile(r"\s+")


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
        self.session: Optional[Session] = None
        self.backend: Optional[SessionBackend] = None
        self.log_buffer: list[LogEntry] = []
        self.command_count: int = 0
        self._output_subscribers: list[asyncio.Queue] = []
        # Item 7: per-session idle watcher. Constructed lazily at
        # ``create_session`` so we can inject the live router from
        # ``app.state``; cleared by ``destroy_session``. Exposed directly
        # (not via a getter) so the WS chunk handler can bypass the
        # property-lookup cost in its hot path.
        self.idle_watcher: Optional[IdleWatcher] = None
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

        # Byte offset into the external session's pipe-pane FIFO at the
        # moment the adoption captured its initial scrollback. The WS
        # tailer reads this ONCE (clearing to None) before entering the
        # live-stream loop, so the first bytes a client sees after the
        # painted scrollback are the ones that arrived AFTER capture —
        # no duplicated replay, no missed bytes. Only set by
        # ``adopt_external_session``; normal ``create_session`` leaves
        # it None (tailer seeks to EOF as before).
        self.adopt_fifo_start_offset: Optional[int] = None

        # Load persisted session if it exists
        self._load_session_metadata()

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
        """
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
                if self.session is not None:
                    self._save_session_metadata()

        if self.session is None:
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
        work_path = Path(self.session.working_dir)
        backend = build_backend(
            settings,
            session_id=self.session.id,
            working_dir=work_path,
            on_output=self._handle_backend_output,
        )

        if not tmux_alive:
            # No tmux sessions at all — treat metadata as stale.
            logger.info(
                "session_metadata_has_no_backend_match",
                session_id=self.session.id,
            )
            self._clear_stale_metadata()
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
                    session_id=self.session.id,
                    backend=type(backend).__name__,
                )
                self._clear_stale_metadata()
                return
            except RuntimeError as e:
                logger.warning(
                    "session_backend_attach_failed",
                    session_id=self.session.id,
                    error=str(e),
                )
                self._clear_stale_metadata()
                return

            self.backend = backend
            self.session.status = SessionStatus.RUNNING

            # Legacy backfill: first successful rehydrate populates the
            # owned-set and re-persists under the new schema.
            if self._legacy_metadata_needs_backfill:
                self.owned_tmux_sessions.add(target_name)
                self._save_session_metadata()
                logger.info(
                    "session_metadata_legacy_backfilled",
                    session_id=self.session.id,
                    owned=sorted(self.owned_tmux_sessions),
                )

            logger.info(
                "session_re_registered_from_backend",
                session_id=self.session.id,
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
                    session_id=self.session.id,
                    target=target_name,
                    owned=sorted(self.owned_tmux_sessions),
                    note="not rehydrating a non-owned session",
                )
            else:
                logger.warning(
                    "session_metadata_slug_not_in_backend",
                    session_id=self.session.id,
                    target=target_name,
                    discovered=sorted(tmux_alive),
                )
            self._clear_stale_metadata()

    def _clear_stale_metadata(self) -> None:
        """Delete on-disk metadata for a session that can't be re-adopted."""
        metadata_path = settings.get_session_metadata_path()
        try:
            if metadata_path.exists():
                metadata_path.unlink()
                logger.info("stale_session_metadata_deleted")
        except Exception as exc:
            logger.error("failed_to_delete_stale_metadata", error=str(exc))
        self.session = None

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

            self.session = Session(**raw)

            if owned is None and raw.get("id"):
                # Pre-v3 metadata: no owned-set was persisted. Mark for
                # backfill on next save; the reconciler in
                # ``lifespan_startup`` will populate the set once the
                # slug is confirmed live on the tmux socket.
                self.owned_tmux_sessions = set()
                self._legacy_metadata_needs_backfill = True
                logger.info(
                    "session_metadata_legacy_detected",
                    session_id=self.session.id,
                    note="owned_tmux_sessions will be backfilled on rehydrate",
                )
            else:
                self.owned_tmux_sessions = set(owned or [])
                self._legacy_metadata_needs_backfill = False

            logger.info(
                "session_metadata_loaded",
                session_id=self.session.id,
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

    def _save_session_metadata(self):
        """Save session metadata atomically, including the owned-set."""
        if not self.session:
            return

        try:
            payload = self.session.model_dump()
            payload["owned_tmux_sessions"] = sorted(self.owned_tmux_sessions)
            self._write_metadata_atomic(payload)

            # Clear the backfill sentinel once we've successfully persisted
            # the new schema — one successful save is the migration.
            self._legacy_metadata_needs_backfill = False

            logger.debug(
                "session_metadata_saved",
                session_id=self.session.id,
                owned_count=len(self.owned_tmux_sessions),
            )

        except Exception as e:
            logger.error("failed_to_save_session_metadata", error=str(e))

    # ---- output fan-out -------------------------------------------------

    async def _handle_backend_output(self, data: bytes):
        """Handle output from the backend. Broadcasts to WS subscribers."""
        encoded_data = base64.b64encode(data).decode('utf-8')

        for queue in self._output_subscribers.copy():
            try:
                await queue.put(encoded_data)
            except Exception as e:
                logger.error("failed_to_send_to_subscriber", error=str(e))
                self._output_subscribers.remove(queue)

    def subscribe_output(self) -> asyncio.Queue:
        """Subscribe to backend output stream."""
        queue = asyncio.Queue()
        self._output_subscribers.append(queue)
        return queue

    def unsubscribe_output(self, queue: asyncio.Queue):
        """Unsubscribe from backend output stream."""
        if queue in self._output_subscribers:
            self._output_subscribers.remove(queue)

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
    ) -> Session:
        """Create a new Claude Code session.

        Preserves the single-active invariant: if a session is already live,
        this raises. If there's stale metadata without a live backend, clean
        it up first.

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
        if self.has_active_session():
            raise ValueError("A session is already running. Stop it before creating a new one.")

        # Clean up zombie session metadata if exists
        if self.session and not self.has_active_session():
            logger.info("cleaning_up_zombie_session", session_id=self.session.id)
            self._clear_stale_metadata()

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
            sanitized = _sanitize_tmux_name(project_name)
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

        try:
            # Build a fresh backend for the new session.
            self.backend = build_backend(
                settings,
                session_id=session_id,
                working_dir=work_path,
                on_output=self._handle_backend_output,
                session_name=tmux_session_name,
            )

            if auto_start_claude:
                claude_cli = settings.get_claude_cli_path()
                command = f"{claude_cli} --dangerously-skip-permissions"
                await self.backend.start(
                    command=command,
                    initial_cols=initial_cols,
                    initial_rows=initial_rows,
                )
            else:
                await self.backend.start(
                    initial_cols=initial_cols,
                    initial_rows=initial_rows,
                )

            # Best-effort PID for metadata: TmuxBackend doesn't track a single
            # pid, PTYBackend exposes one via `.pid`.
            pid = getattr(self.backend, "pid", None)

            self.session = Session(
                id=session_id,
                pty_pid=pid,
                working_dir=str(work_path),
                status=SessionStatus.RUNNING,
                created_at=datetime.utcnow(),
                last_activity=datetime.utcnow()
            )

            # Track 1: record tmux-backend ownership BEFORE returning, so
            # a post-create crash still leaves the name recoverable from
            # ``session_metadata.json`` — and the adopt UI correctly
            # flags it as ``created_by_cloude=True``.
            owned_name = getattr(self.backend, "tmux_session", None)
            if owned_name:
                self.owned_tmux_sessions.add(owned_name)

            self._save_session_metadata()

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
                self.idle_watcher = IdleWatcher(
                    session_slug=session_id,
                    router=self._notification_router,
                    threshold_s=threshold,
                )
                await self.idle_watcher.start()

            logger.info(
                "session_created",
                session_id=session_id,
                pid=pid,
                backend=self.backend_name,
            )

            return self.session

        except PTYSessionError as e:
            logger.error("session_creation_failed", error=str(e))
            if self.session:
                self.session.status = SessionStatus.ERROR
            raise ValueError(f"Failed to create session: {e}") from e
        except Exception as e:
            logger.error("session_creation_failed", error=str(e))
            if self.session:
                self.session.status = SessionStatus.ERROR
            # Also clean up a half-built backend + watcher.
            if self.backend is not None:
                try:
                    await self.backend.stop()
                except Exception:
                    pass
                self.backend = None
            if self.idle_watcher is not None:
                try:
                    await self.idle_watcher.stop()
                except Exception:
                    pass
                self.idle_watcher = None
            raise ValueError(f"Failed to create session: {e}") from e

    async def detach_current_session(self) -> bool:
        """Detach from the current backend WITHOUT killing tmux.

        This is the "soft" counterpart to ``destroy_session``: it tears down
        the Python-side handles (reader task, idle watcher, backend refs,
        output subscribers, stashed offsets) and stops our pipe-pane so the
        server-side tmux session can be cleanly re-adopted later — but it
        leaves the tmux session itself alive. The user's shell state and
        any running foreground process (Claude CLI, vim, long build, ...)
        continue as if the web UI were never connected.

        Why stop pipe-pane here (vs leaving it attached): our pipe-pane
        writes into ``tmux_<slug>.pipe``; the subsequent re-adopt via
        ``TmuxBackend.for_external`` derives its pipe path as
        ``tmux_ext_<slug>.pipe`` — a DIFFERENT file. If we leave the old
        pipe-pane active, the re-adopt's ``ensure_pipe_pane`` sees
        ``#{pane_pipe} == 1`` and refuses to clobber it, then the tailer
        opens the new (empty) path and silently streams nothing.
        Turning our pipe off on detach means re-adopt gets a fresh pipe
        at the new path — correct and unambiguous. The tmux session
        itself is untouched.

        We keep ``owned_tmux_sessions`` intact so the Adopt UI correctly
        labels the detached session as ``created_by_cloude=True`` — the
        user can re-adopt it from there, or start a fresh session without
        losing the old one.

        On-disk metadata is unlinked so a server restart doesn't silently
        auto-rehydrate the detached session; it'll appear as an adoptable
        external session (pragmatic trade-off — the ``created_by_cloude``
        flag degrades to False after a restart, but the session remains
        recoverable).

        Returns False (no-op) when no session is active. True otherwise.
        """
        if not self.session or self.backend is None:
            logger.info("detach_current_session_noop")
            return False

        logger.info("detaching_session", session_id=self.session.id)

        try:
            # Tear down the idle watcher first — mirrors destroy ordering so
            # a trailing poll iteration can't fire after the backend is gone.
            if self.idle_watcher is not None:
                try:
                    await self.idle_watcher.stop()
                except Exception as exc:
                    logger.warning(
                        "idle_watcher_stop_error_on_detach",
                        error=str(exc),
                    )
                self.idle_watcher = None

            # Cancel the backend's reader task so no more pipe bytes land
            # in the output fan-out after detach. TmuxBackend.stop() does
            # this as part of its shutdown; we mirror the part we want
            # (reader teardown) without the part we don't (kill-session).
            reader_task = getattr(self.backend, "_reader_task", None)
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
                        "reader_task_cancel_error_on_detach",
                        error=str(exc),
                    )
                try:
                    self.backend._reader_task = None
                except Exception:
                    pass

            # Stop OUR pipe-pane so a subsequent re-adopt can cleanly set up
            # its own pipe at the (different) external-path. Best-effort —
            # if this fails the re-adopt still works because
            # ``ensure_pipe_pane`` logs and returns without clobbering.
            try:
                if hasattr(self.backend, "_run_tmux"):
                    # tmux_backend internals — reach for them only if
                    # present so PTYBackend stays unaffected.
                    from src.core.tmux_backend import _safe_target
                    target_name = getattr(self.backend, "tmux_session", None)
                    if target_name:
                        await self.backend._run_tmux(
                            "pipe-pane",
                            "-t",
                            _safe_target(target_name),
                            check=False,
                        )
                # Flag the backend as no-longer-running so any lingering
                # write attempt raises loudly instead of touching tmux.
                try:
                    self.backend._running = False
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(
                    "pipe_pane_stop_failed_on_detach",
                    error=str(exc),
                )

            # Clear references — leave tmux alive.
            self.backend = None
            self.session = None
            self.log_buffer.clear()
            self.command_count = 0
            self._output_subscribers.clear()
            # FIFO offset is single-use and session-scoped; reset on detach.
            self.adopt_fifo_start_offset = None

            # Unlink the on-disk metadata so a server restart doesn't try
            # to auto-rehydrate — the detached session is meant to surface
            # in the Adopt list, not silently reclaim the active slot.
            # ``owned_tmux_sessions`` stays in memory for the remainder of
            # this process so the Adopt UI correctly labels the detached
            # session as cloude-owned during the current server lifetime.
            metadata_path = settings.get_session_metadata_path()
            try:
                if metadata_path.exists():
                    metadata_path.unlink()
            except OSError as exc:
                logger.warning(
                    "session_metadata_unlink_failed_on_detach",
                    error=str(exc),
                )

            logger.info("session_detached")
            return True

        except Exception as e:
            logger.error("session_detach_failed", error=str(e))
            raise

    async def destroy_session(self) -> bool:
        """Destroy the current session."""
        if not self.session:
            raise ValueError("No session to destroy")

        logger.info("destroying_session", session_id=self.session.id)

        try:
            # Item 7: tear down the watcher FIRST. Stopping it before the
            # backend guarantees no poll iteration races with the pending
            # backend shutdown (the backend's final bytes could otherwise
            # fire a last-gasp TASK_COMPLETE after the session is gone).
            if self.idle_watcher is not None:
                try:
                    await self.idle_watcher.stop()
                except Exception as exc:
                    logger.warning(
                        "idle_watcher_stop_error",
                        error=str(exc),
                    )
                self.idle_watcher = None

            # Track 1: drop ownership record BEFORE we lose the handle to
            # ``self.backend.tmux_session``. Persistence happens via the
            # unlink-then-reset below — no separate save needed since the
            # whole metadata file is about to be removed.
            owned_name = getattr(self.backend, "tmux_session", None) if self.backend else None
            if owned_name:
                self.owned_tmux_sessions.discard(owned_name)

            if self.backend is not None:
                await self.backend.stop()
                self.backend = None

            self.session.status = SessionStatus.STOPPED

            metadata_path = settings.get_session_metadata_path()
            if metadata_path.exists():
                metadata_path.unlink()

            self.session = None
            self.log_buffer.clear()
            self.command_count = 0
            self._output_subscribers.clear()
            # FIFO offset is single-use and session-scoped; reset on teardown.
            self.adopt_fifo_start_offset = None

            logger.info("session_destroyed")
            return True

        except Exception as e:
            logger.error("session_destruction_failed", error=str(e))
            raise

    # ---- I/O -------------------------------------------------------------

    async def send_command(self, command: str) -> bool:
        """Send a command (with trailing newline) to the backend."""
        if not self.session:
            raise ValueError("No active session")

        if self.session.status != SessionStatus.RUNNING:
            raise ValueError(f"Session is not running (status: {self.session.status})")

        if not self.backend:
            raise ValueError("Backend not initialized")

        logger.info(
            "sending_command",
            session_id=self.session.id,
            command=command[:50] + "..." if len(command) > 50 else command
        )

        try:
            await self.backend.write(command.encode("utf-8") + b"\n")
            self.session.last_activity = datetime.utcnow()
            self.command_count += 1
            self._save_session_metadata()
            return True

        except Exception as e:
            logger.error("send_command_failed", error=str(e))
            raise ValueError(f"Failed to send command: {e}") from e

    async def send_input(self, data: str) -> bool:
        """Send raw input to the backend."""
        if not self.session or not self.backend:
            raise ValueError("No active session")

        if self.session.status != SessionStatus.RUNNING:
            raise ValueError(f"Session is not running (status: {self.session.status})")

        try:
            await self.backend.write(data.encode("utf-8"))
            self.session.last_activity = datetime.utcnow()
            return True

        except Exception as e:
            logger.error("send_input_failed", error=str(e))
            raise ValueError(f"Failed to send input: {e}") from e

    def resize_terminal(self, cols: int, rows: int):
        """Resize the backend's terminal."""
        if not self.backend:
            return

        try:
            self.backend.resize(cols, rows)
            logger.debug("terminal_resized", cols=cols, rows=rows)
        except Exception as e:
            logger.error("terminal_resize_failed", error=str(e))

    def capture_scrollback(self, lines: int = 3000) -> bytes:
        """Capture backend scrollback for WS replay on reconnect.

        Returns b"" when no backend is active, for PTYBackend, or when the
        backend can't produce scrollback. The WS handler treats b"" as
        "nothing to replay" and enters the live stream directly.
        """
        if not self.backend:
            return b""
        try:
            return self.backend.capture_scrollback(lines=lines)
        except Exception as exc:
            logger.error("capture_scrollback_failed", error=str(exc))
            return b""

    # ---- log buffer (unchanged) -----------------------------------------

    def get_recent_logs(self, limit: int = 100) -> list[LogEntry]:
        """Get recent log entries."""
        return self.log_buffer[-limit:]

    def add_log_entry(self, content: str, log_type: str = "stdout"):
        """Add a log entry to the buffer."""
        if not self.session:
            return

        entry = LogEntry(
            timestamp=datetime.utcnow(),
            session_id=self.session.id,
            content=content,
            log_type=log_type
        )

        self.log_buffer.append(entry)

        if len(self.log_buffer) > settings.log_buffer_size:
            self.log_buffer = self.log_buffer[-settings.log_buffer_size:]

    async def get_session_info(self) -> Optional[SessionInfo]:
        """Get complete session information."""
        if not self.has_active_session():
            return None

        uptime = int((datetime.utcnow() - self.session.created_at).total_seconds())

        stats = SessionStats(
            total_commands=self.command_count,
            uptime_seconds=uptime,
            log_lines=len(self.log_buffer),
            active_tunnels=len(self.session.tunnels)
        )

        # Pull tmux_session from the backend when available. The tmux backend
        # exposes the attribute directly; pty backend does not. Using getattr
        # with a None default keeps this backend-agnostic.
        tmux_session_name = (
            getattr(self.backend, "tmux_session", None) if self.backend else None
        )

        return SessionInfo(
            session=self.session,
            recent_logs=self.get_recent_logs(),
            active_tunnels=self.session.tunnels,
            stats=stats,
            session_backend=self.backend_name,
            tmux_session=tmux_session_name,
        )

    def has_active_session(self) -> bool:
        """True iff a session is running AND its backend is alive."""
        return (
            self.session is not None
            and self.session.status == SessionStatus.RUNNING
            and self.backend is not None
            and self.backend.is_alive()
        )

    # ---- Track 1: adopt an externally-started tmux session ----------------

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
        return probe.list_attachable_sessions(
            owned_names=set(self.owned_tmux_sessions)
        )

    async def adopt_external_session(
        self, name: str, confirm_detach: bool = False
    ) -> dict:
        """Adopt an externally-created tmux session on our socket.

        Ordered sequence (plan v3 — fixes the scrollback/WS race):

          1. Gate on single-active invariant: if a session is live and
             ``confirm_detach`` is False, raise 409. If confirmed,
             DETACH the prior backend (Python-side handles torn down,
             tmux session kept alive — user can re-adopt it later).
          2. Build a ``TmuxBackend.for_external(name, ...)`` instance.
          3. ``attach_existing(needs_pipe_setup=True)`` — starts pipe-pane
             BEFORE any scrollback capture so the FIFO is warm.
          4. Record ``fifo_start_offset = os.path.getsize(pipe_path)``
             immediately after pipe-pane is confirmed active. This is the
             handoff contract for the WS tailer: seek to this offset on
             first read so the client doesn't see bytes that were already
             painted via the scrollback.
          5. Capture scrollback via ``backend.capture_scrollback()`` —
             reads from tmux's visible-pane buffer, NOT the FIFO.
          6. Register the backend and stash the offset on ``self`` for
             the WS handler to consume.

        Switching never kills. Destruction only happens via the explicit
        destroy button. The prior session's tmux pane remains alive on
        the ``-L cloude`` socket and its entry in ``owned_tmux_sessions``
        stays intact so it re-appears in the Adopt list tagged
        ``created_by_cloude=True``.

        The adopted session is NOT added to ``owned_tmux_sessions`` —
        it isn't ours, we're borrowing it.

        Args:
            name: literal tmux session name as shown in the launchpad.
            confirm_detach: explicit consent to detach from the current
                active session if any. False + active session = 409.

        Returns:
            dict with ``session``, ``initial_scrollback_b64``, and
            ``fifo_start_offset`` keys. The route layer wraps this in
            the ``AdoptSessionResponse`` pydantic model.

        Raises:
            HTTPException(409): active session exists and
                ``confirm_detach`` wasn't explicitly True.
            RuntimeError: pane already dead, or pipe-pane setup failed.
            ValueError: if ``name`` contains tmux target separators.
        """
        if self.has_active_session() and not confirm_detach:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Active session will be detached (kept alive in tmux); "
                    "retry with confirm_detach=True"
                ),
            )
        if self.has_active_session():
            prior = self.session.id if self.session else "?"
            logger.info(
                "session_swapped_for_adopt",
                prior=prior,
                new=name,
                action="detach",
            )
            await self.detach_current_session()

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
            on_output=self._handle_backend_output,
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

        # Step 6 — register.
        self.backend = backend
        self.session = Session(
            id=f"adopted:{name}",
            pty_pid=None,
            working_dir=str(working_dir),
            status=SessionStatus.RUNNING,
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
        )
        # External sessions are intentionally NOT added to
        # ``owned_tmux_sessions`` — we don't own them; we adopted them.
        # They'll appear in the adopt UI with ``created_by_cloude=False``
        # during the adoption window, which is correct.
        self._save_session_metadata()

        # Stash the offset for the WS tailer to consume on its first read.
        self.adopt_fifo_start_offset = fifo_start_offset

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
            self.idle_watcher = IdleWatcher(
                session_slug=self.session.id,
                router=self._notification_router,
                threshold_s=threshold,
            )
            await self.idle_watcher.start()

        logger.info(
            "session_adopted_external",
            session=name,
            working_dir=str(working_dir),
            fifo_start_offset=fifo_start_offset,
            scrollback_bytes=len(scrollback),
        )

        return {
            "session": self.session,
            "initial_scrollback_b64": sb_b64,
            "fifo_start_offset": fifo_start_offset,
        }

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

    def consume_adopt_fifo_offset(self) -> Optional[int]:
        """One-shot read of the adopt FIFO offset (None if not set / already consumed).

        The WS tailer calls this exactly once on connect. We clear the
        stashed value so a reconnect later doesn't try to re-seek to
        a stale offset against a (by then) much larger FIFO.
        """
        offset = self.adopt_fifo_start_offset
        self.adopt_fifo_start_offset = None
        return offset
