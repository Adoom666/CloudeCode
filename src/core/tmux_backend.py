"""Tmux-backed session backend.

Uses a DEDICATED tmux server socket (``tmux -L cloude``) so we never touch
the user's default tmux server. Sessions are named ``cloude_<slug>``.

Key design points:

- **Binary-safe writes** (three-path routing):

  ``send-keys -l <text>`` for short, control-free UTF-8 — the fast path
  for regular typing.

  ``send-keys -H <hex pairs>`` for short byte sequences that contain
  control chars (arrow keys, Ctrl-X, Esc, Backspace — real keystrokes).
  tmux treats each hex pair as a literal byte delivered via key event,
  which interactive TUIs interpret correctly.

  ``load-buffer`` + ``paste-buffer -d -p`` reserved for LARGE payloads
  (actual clipboard pastes). Bracketed-paste markers let Claude
  distinguish paste from typed input — correct behavior for paste,
  wrong behavior for keystrokes.

- **Output streaming**: ``tmux pipe-pane -o 'cat >> <fifo>'`` streams every
  pane byte to a file. We tail that file asynchronously and call
  `on_output(bytes)` for every chunk. The file is rotated when it exceeds
  ``MAX_LOG_BYTES`` or is older than ``ROTATE_AGE_HOURS``.

- **Single-active invariant**: the backend itself does NOT enforce
  one-at-a-time; `SessionManager` does. This backend DOES refuse to start
  if a session with the same name already exists — callers must call
  ``discover_existing()`` first to re-attach.

- **Restart survival**: `discover_existing()` lists ``cloude_*`` sessions on
  the dedicated server. `SessionManager.lifespan_startup` uses it to
  re-register the slug stored in ``session_metadata.json``.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from src.core.session_backend import SessionBackend

logger = structlog.get_logger()

# ---- Tunables ---------------------------------------------------------------
# Module-scope constants (not in config.json) so they're easy to find in code.
# If we ever want to expose these, wire through `AuthConfig.session` — for now
# the values below are battle-tested defaults.

#: Rotate the pipe-pane log once it passes 10 MiB.
MAX_LOG_BYTES: int = 10 * 1024 * 1024

#: Rotate regardless of size after this many hours. 24h matches a normal
#: coding-session cadence.
ROTATE_AGE_HOURS: int = 24

#: Default starting window geometry for new tmux sessions. We never attach a
#: client (output is streamed via pipe-pane), so tmux has no client dims to
#: key off of. Without `-x/-y` + `window-size manual`, tmux clamps the
#: window to its 80x24 birth size forever — making TUI apps like Claude CLI
#: render at 80x24 while xterm.js draws at the browser's actual size.
#: These are reasonable defaults; the WS client's first `resize` request
#: replaces them within milliseconds of connect.
INITIAL_COLS: int = 132
INITIAL_ROWS: int = 40

#: Bytes threshold above which we switch from ``send-keys -l`` to
#: ``load-buffer``/``paste-buffer``. Below this AND no control chars → fast
#: path. Above OR control chars → paste-buffer path.
PASTE_THRESHOLD_BYTES: int = 256

#: Default socket name, overridable via ``AuthConfig.session.tmux_socket_name``.
DEFAULT_SOCKET_NAME: str = "cloude"

#: Session name prefix — ``cloude_<slug>``.
SESSION_PREFIX: str = "cloude_"

#: FIFO + rotated log live under the log directory. File name is
#: ``tmux_<slug>.pipe``.
PIPE_SUFFIX: str = ".pipe"


# ---- Helpers ---------------------------------------------------------------


def _slugify(raw: str) -> str:
    """Sanitize an arbitrary session id into a tmux-legal slug.

    tmux forbids ``.`` and ``:`` in session names and interprets ``.`` as a
    window/pane separator. We replace ``.``, ``:``, whitespace, and ``/`` with
    ``_``, then strip to a conservative charset.
    """
    out = []
    for ch in raw:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("_")
    slug = "".join(out).strip("_")
    # tmux also dislikes empty names.
    return slug or "default"


def _has_control_chars(data: bytes) -> bool:
    """True if `data` contains any control byte that paste-buffer must handle.

    We flag ALL bytes < 0x20 except ``\\t`` (0x09), ``\\n`` (0x0a), and
    ``\\r`` (0x0d), plus ``0x7f`` (DEL). In practice this catches ``0x03``
    (^C), ``0x04`` (^D), ``0x1b`` (ESC), etc.
    """
    safe = {0x09, 0x0a, 0x0d}
    return any((b < 0x20 and b not in safe) or b == 0x7f for b in data)


def _safe_target(session_name: str, pane: str = "0.0") -> str:
    """Compose a tmux target string (``<session>:<window>.<pane>``) safely.

    tmux parses ``:`` as the window/pane separator and ``.`` as the pane
    separator within a target. If either appears inside ``session_name``
    the command tmux actually executes is NOT the one we meant to send —
    it selects a different (possibly wrong) target.

    We use list-form argv everywhere (``asyncio.create_subprocess_exec``,
    ``subprocess.run`` with a list), so shell-metacharacter injection is
    already impossible. This helper is about the OTHER vector: tmux's own
    target-parsing semantics. We refuse to format a target that would be
    interpreted differently than intended.

    Args:
        session_name: tmux session name. MUST NOT contain ``:`` or ``.``.
        pane: pane specifier within the session. Defaults to ``"0.0"``
            (window 0, pane 0). Callers SHOULD keep this as a literal —
            we don't validate it since it's never user-controlled.

    Returns:
        Formatted target string suitable for ``-t``.

    Raises:
        ValueError: if ``session_name`` contains ``:`` or ``.``.
    """
    if ":" in session_name or "." in session_name:
        raise ValueError(
            f"unsafe tmux session name {session_name!r}: "
            f"contains ':' or '.' which tmux parses as target separators"
        )
    return f"{session_name}:{pane}"


# ---- Backend --------------------------------------------------------------


class TmuxBackend(SessionBackend):
    """Session backend that runs the child under tmux on a dedicated socket."""

    def __init__(
        self,
        session_id: str,
        working_dir: Path,
        on_output: Optional[Callable[[bytes], Any]] = None,
        socket_name: str = DEFAULT_SOCKET_NAME,
        scrollback_lines: int = 3000,
        session_name: Optional[str] = None,
    ) -> None:
        super().__init__(session_id, working_dir, on_output)

        self.socket_name = socket_name
        self.scrollback_lines = scrollback_lines
        self.slug = _slugify(session_id)
        # If an explicit session_name is provided (used by create_session with
        # project_name for verbatim naming), it OVERRIDES the legacy
        # cloude_<slug> derivation. Otherwise default to the legacy hex-based
        # name so existing call sites are unchanged.
        if session_name is not None:
            self.tmux_session = session_name
        else:
            self.tmux_session = f"{SESSION_PREFIX}{self.slug}"

        # Per-session pipe-pane output file lives under the log directory.
        # We resolve lazily to avoid importing settings at module import time.
        self._pipe_path: Optional[Path] = None

        self._reader_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_rotate_check = time.monotonic()
        self._rotation_started_at = time.monotonic()

        # Set to True by ``TmuxBackend.for_external()`` to mark this backend
        # as an adoption of a user-started tmux session. Changes adopt-time
        # behavior in ``attach_existing()`` (refuses dead panes, starts
        # pipe-pane defensively, enforces remain-on-exit, warns on
        # non-manual window-size) without affecting the normal create flow.
        self._is_external: bool = False

        # Byte offset recorded right after pipe-pane is confirmed active on
        # adoption. The tail loop seeks here on open so bytes that were
        # already captured in the initial scrollback (and painted
        # client-side before the WS opened) aren't streamed again. None
        # means "seek to EOF" — the normal create/rehydrate behavior.
        self._adopt_tail_start_offset: Optional[int] = None

    # ---- internal helpers ------------------------------------------------

    def _tmux_base(self) -> List[str]:
        """Common tmux argv prefix — always uses our dedicated socket."""
        return ["tmux", "-L", self.socket_name]

    def _resolve_pipe_path(self) -> Path:
        if self._pipe_path is not None:
            return self._pipe_path
        # Lazy import: src.config pulls env vars and may not be available in
        # some test contexts.
        try:
            from src.config import settings
            log_dir = settings.get_log_dir()
        except Exception:
            log_dir = Path("/tmp")

        # External sessions may keep a literal tmux name in self.slug that
        # isn't safe as a filename (spaces, unicode, etc.). Normalize with
        # ``_slugify`` here; cloude-owned slugs are already safe so this is
        # a no-op in the normal path. External names add an ``ext_``
        # prefix to guarantee filename distinctness from any accidentally
        # colliding cloude-owned slug.
        if self._is_external:
            fname_slug = f"ext_{_slugify(self.slug)}"
        else:
            fname_slug = self.slug
        self._pipe_path = log_dir / f"tmux_{fname_slug}{PIPE_SUFFIX}"
        return self._pipe_path

    async def _run_tmux(
        self,
        *args: str,
        stdin_bytes: Optional[bytes] = None,
        check: bool = True,
    ) -> tuple[int, bytes, bytes]:
        """Run a one-shot tmux command, optionally piping stdin bytes."""
        argv = self._tmux_base() + list(args)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=stdin_bytes)
        if check and proc.returncode != 0:
            logger.error(
                "tmux_command_failed",
                argv=argv,
                returncode=proc.returncode,
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        return proc.returncode or 0, stdout, stderr

    def _run_tmux_sync(
        self,
        *args: str,
        stdin_bytes: Optional[bytes] = None,
        check: bool = True,
    ) -> tuple[int, bytes, bytes]:
        """Sync variant for use in `is_alive`, `discover_existing`, etc."""
        import subprocess

        argv = self._tmux_base() + list(args)
        proc = subprocess.run(
            argv,
            input=stdin_bytes,
            capture_output=True,
            check=False,
        )
        if check and proc.returncode != 0:
            logger.debug(
                "tmux_command_nonzero",
                argv=argv,
                returncode=proc.returncode,
                stderr=proc.stderr.decode("utf-8", errors="replace")[:200],
            )
        return proc.returncode, proc.stdout, proc.stderr

    # ---- SessionBackend API ---------------------------------------------

    async def start(
        self,
        command: Optional[str] = None,
        env: Optional[dict] = None,
        initial_cols: Optional[int] = None,
        initial_rows: Optional[int] = None,
    ) -> None:
        """Create the tmux session + start pipe-pane streaming.

        ``initial_cols`` / ``initial_rows`` override the module-level
        INITIAL_COLS / INITIAL_ROWS when BOTH are supplied. One without the
        other is treated as "not supplied" — we don't mix a client dim with
        a default, because that would create an asymmetric starting pane
        (e.g. client gives cols=100, we'd pair with default rows=40 which
        is almost certainly wrong for that viewport).
        """
        if self._running:
            raise RuntimeError("TmuxBackend already running")

        if not shutil.which("tmux"):
            raise RuntimeError("tmux not found on PATH")

        self.working_dir.mkdir(parents=True, exist_ok=True)

        # Prepare pipe file BEFORE starting tmux so we don't miss bytes.
        pipe_path = self._resolve_pipe_path()
        pipe_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate any stale file from a previous session with the same slug.
        pipe_path.write_bytes(b"")

        # Resolve birth geometry: client-supplied dims win when BOTH are
        # provided, otherwise fall back to module defaults. The WS resize
        # handshake reshapes the pane after connect regardless, so this
        # only matters for the brief window before the first resize frame.
        use_cols = initial_cols if (initial_cols and initial_rows) else INITIAL_COLS
        use_rows = initial_rows if (initial_cols and initial_rows) else INITIAL_ROWS

        # Build the session. ``new-session -d -s <name> -c <cwd> [command]``.
        # If a command is supplied, tmux runs that as pane 0's process; the
        # shell exits when the command ends unless ``remain-on-exit`` is set.
        # For our case (Claude CLI) we want the pane to stick around even if
        # Claude exits, so we enable remain-on-exit after creation.
        #
        # ``-x`` / ``-y`` fix the window's birth geometry. Without them tmux
        # uses 80x24 and — combined with default ``window-size latest`` and
        # zero attached clients — stays there forever. We pair these with
        # ``window-size manual`` below so `resize-window` is the ONLY thing
        # that can change the size (no client-sizing surprises).
        args = [
            "new-session",
            "-d",
            "-s",
            self.tmux_session,
            "-c",
            str(self.working_dir),
            "-x",
            str(use_cols),
            "-y",
            str(use_rows),
        ]
        if command:
            args.append(command)

        # Merge env overlay into this tmux invocation's environment so the
        # new session inherits it. tmux captures the environment of the
        # `new-session` call.
        tmux_env = os.environ.copy()
        tmux_env.setdefault("TERM", "xterm-256color")
        tmux_env.setdefault("COLORTERM", "truecolor")
        if env:
            tmux_env.update(env)

        argv = self._tmux_base() + args
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=tmux_env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"tmux new-session failed: {msg.strip()}")

        # Keep the pane alive even after child exits so scrollback persists.
        await self._run_tmux(
            "set-option", "-t", self.tmux_session, "remain-on-exit", "on", check=False
        )

        # Enable extended keys (tmux 3.2+) so modifier+key sequences like
        # Shift+Enter arrive as CSI u (`\x1b[13;2u`) at the pane intact,
        # instead of being collapsed to bare CR. Required for Claude Code
        # CLI's multi-line input prompt to recognize Shift+Enter as
        # "newline-insert" vs. CR=submit. Paired with the terminal-features
        # `extkeys` flag below which advertises extended-key support to the
        # pane's $TERM — Claude reads the terminfo to decide whether to
        # emit CSI u or legacy keys.
        #
        # ``-s`` targets the tmux server (global, persists for the life of
        # the tmux socket). Safe to re-run per session; tmux ignores repeat
        # sets of the same value.
        await self._run_tmux(
            "set-option", "-s", "extended-keys", "on", check=False
        )
        # ``-as`` = append-and-set to the terminal-features option list.
        # We target xterm-256color (our default TERM set above) and add the
        # ``extkeys`` feature flag which tells tmux this terminal supports
        # extended keys. Without this, tmux still processes extended-keys
        # internally but may not advertise the capability to the pane.
        await self._run_tmux(
            "set-option", "-as", "terminal-features",
            "xterm-256color:extkeys", check=False
        )
        # Drop the ESC key-timeout to 0ms on this tmux server so ESC-prefixed
        # sequences aren't split into "ESC then CR" by the default 500ms
        # wait window. Defense-in-depth for any fallback client that sends
        # `\x1b\r` (Alt+Enter) instead of CSI u.
        await self._run_tmux(
            "set-option", "-s", "escape-time", "0", check=False
        )

        # Critical for headless (no-client) operation: lock the window size to
        # manual so only `resize-window` changes it. Default is ``latest``
        # which sizes to the most recent attached client; with zero clients
        # tmux never leaves the 80x24 birth size.
        await self._run_tmux(
            "set-option", "-t", self.tmux_session, "window-size", "manual", check=False
        )
        # Prevent size clamping based on other windows in the session.
        # (We only ever have window 0, but be defensive — future code that
        # adds a second window shouldn't silently shrink pane 0.)
        await self._run_tmux(
            "set-option", "-t", self.tmux_session, "aggressive-resize", "off", check=False
        )

        # Start pipe-pane — this streams pane output to our file.
        # Using shell redirection so tmux appends (not truncates) on rotation.
        pipe_cmd = f"cat >> {shlex.quote(str(pipe_path))}"
        rc, _, err = await self._run_tmux(
            "pipe-pane",
            "-t",
            _safe_target(self.tmux_session),
            "-o",
            pipe_cmd,
        )
        if rc != 0:
            logger.error(
                "tmux_pipe_pane_failed",
                stderr=err.decode("utf-8", errors="replace"),
            )

        self._running = True
        self._rotation_started_at = time.monotonic()

        # Kick off the output reader loop.
        await self.read_async()

        logger.info(
            "tmux_backend_started",
            session=self.tmux_session,
            socket=self.socket_name,
            cwd=str(self.working_dir),
            pipe=str(pipe_path),
        )

    async def attach_existing(self, needs_pipe_setup: bool = False) -> None:
        """Rehydrate state for an existing tmux session on our socket.

        Precondition: `discover_existing()` has already confirmed the session
        is alive on the configured socket. For OUR OWN sessions (``cloude_*``
        born via ``start()``), pipe-pane is still active on the tmux server
        side (tmux, not us, holds that pipe), so the pipe file is still being
        appended to. We open it and tail from the END so we don't re-emit
        historical output as if it were new.

        For EXTERNAL sessions (Track 1 "Adopt an external session" flow —
        user started it via ``tmux -L cloude new -s <name>``), there is
        likely NO pipe-pane active yet, so the caller passes
        ``needs_pipe_setup=True`` to trigger:

        1. Refuse if ``#{pane_dead}`` is ``"1"`` — a dead pane can't be
           usefully adopted.
        2. ``ensure_pipe_pane()`` — query first, start only if not already
           active (non-toggle).
        3. ``set-option remain-on-exit on`` defensively so an external
           child exiting doesn't silently collapse the pane while our
           adoption is live.
        4. WARN if ``window-size`` isn't ``manual`` — ``resize-window``
           may oscillate against tmux's auto-sizing.

        External mode is also auto-triggered when ``self._is_external`` is
        True (set by ``TmuxBackend.for_external``), so the caller can pass
        ``needs_pipe_setup=False`` and still get the right behavior.

        This MUST be idempotent: calling it twice is fine. Calling it after
        ``stop()`` is not supported.
        """
        if self._running:
            logger.debug("tmux_backend_attach_noop", session=self.tmux_session)
            return

        # Verify the session is actually alive on the socket. If it's not,
        # caller made a mistake — raise loudly so the upstream rehydrate
        # path can clean up stale metadata instead of entering a bogus state.
        if not self.is_alive():
            raise RuntimeError(
                f"attach_existing: tmux session {self.tmux_session} is not alive"
            )

        do_external_setup = needs_pipe_setup or self._is_external

        if do_external_setup:
            target = _safe_target(self.tmux_session)

            # 1. Refuse dead panes — nothing to stream from, and our attempts
            # to set options on them produce confusing errors further down.
            rc, out, err = await self._run_tmux(
                "display-message", "-t", target, "-p", "#{pane_dead}",
                check=False,
            )
            if rc != 0:
                raise RuntimeError(
                    f"cannot adopt {self.tmux_session}: pane-dead probe failed: "
                    f"{err.decode('utf-8', errors='replace').strip()}"
                )
            if out.decode("utf-8", errors="replace").strip() == "1":
                raise RuntimeError(
                    f"cannot adopt {self.tmux_session}: pane already dead"
                )

            # 2. Ensure pipe-pane is active WITHOUT clobbering a user's
            # existing pipe-pane (e.g. personal logging).
            await self.ensure_pipe_pane()

            # 2b. Record the FIFO offset NOW — immediately after
            # pipe-pane is confirmed active. The tail loop will seek
            # here on open instead of EOF so nothing between "pipe-pane
            # started" and "tail loop actually opens fd" is lost. The
            # scrollback capture (step 5 in ``adopt_external_session``)
            # pulls from tmux's visible-pane buffer, so there's no
            # overlap contest here.
            try:
                pipe_path = self._resolve_pipe_path()
                if pipe_path.exists():
                    self._adopt_tail_start_offset = pipe_path.stat().st_size
                else:
                    self._adopt_tail_start_offset = 0
            except OSError as exc:
                logger.warning(
                    "adopt_fifo_offset_stat_failed",
                    session=self.tmux_session,
                    error=str(exc),
                )
                self._adopt_tail_start_offset = 0

            # 3. Defensive remain-on-exit so external death doesn't silently
            # collapse the pane mid-adoption. Users who need tear-down
            # semantics can flip it back themselves.
            await self._run_tmux(
                "set-option", "-t", target, "remain-on-exit", "on",
                check=False,
            )

            # 4. Surface window-size divergence. ``show-option -sv``
            # queries the server-level option; ``resize-window -x -y``
            # may oscillate when this isn't ``manual``.
            rc_ws, out_ws, _ = await self._run_tmux(
                "show-option", "-sv", "window-size", check=False,
            )
            ws_val = out_ws.decode("utf-8", errors="replace").strip() if rc_ws == 0 else ""
            if ws_val and ws_val != "manual":
                logger.warning(
                    "external_session_window_size_not_manual",
                    session=self.tmux_session,
                    window_size=ws_val,
                    note="resize-window -x -y may oscillate with tmux auto-resize",
                )

        # Recompute / re-resolve pipe file path. It was written to by the
        # old Python process; the tmux server kept pipe-pane running, so
        # the file is still being appended to. We tail from current EOF.
        pipe_path = self._resolve_pipe_path()
        if not pipe_path.exists():
            # Shouldn't happen if tmux's pipe-pane is alive, but handle gracefully
            # by re-running pipe-pane to re-establish the pipe. This is a
            # defensive reconnect — the old pipe-pane process inside tmux
            # continues, we just make sure our file target exists.
            logger.warning(
                "tmux_backend_pipe_missing_recreating",
                session=self.tmux_session,
                pipe=str(pipe_path),
            )
            pipe_path.parent.mkdir(parents=True, exist_ok=True)
            pipe_path.touch()

        self._pipe_path = pipe_path
        self._running = True
        self._rotation_started_at = time.monotonic()

        await self.read_async()

        logger.info(
            "tmux_backend_attached_existing",
            session=self.tmux_session,
            socket=self.socket_name,
            pipe=str(pipe_path),
            external=self._is_external,
        )

    async def ensure_pipe_pane(self) -> None:
        """Start ``pipe-pane`` on pane 0 iff no pipe is currently active.

        Why query-then-act instead of just calling ``pipe-pane``:
        ``pipe-pane -o`` is explicitly a TOGGLE in tmux (since 1.8) — running
        it on a pane that already has an active pipe STOPS piping. That's
        catastrophic when the user has their own ``pipe-pane`` running
        (e.g. personal session logging). We query ``#{pane_pipe}`` first
        (``"0"`` = no pipe, ``"1"`` = pipe active) and only start our pipe
        when none is active. If a pipe is already running we log and return
        — the disclosure tooltip tells the user to stop theirs first.

        We also use ``pipe-pane`` WITHOUT ``-o`` when we DO start it — ``-o``
        is the toggle form and we've already proven no pipe is active, so
        we want the explicit non-toggle start semantics.
        """
        if not self._running and not self._is_external:
            raise RuntimeError("backend not running")

        target = _safe_target(self.tmux_session)

        rc, out, err = await self._run_tmux(
            "display-message", "-t", target, "-p", "#{pane_pipe}",
            check=False,
        )
        if rc != 0:
            raise RuntimeError(
                f"display-message #{{pane_pipe}} failed: "
                f"{err.decode('utf-8', errors='replace').strip()}"
            )
        state = out.decode("utf-8", errors="replace").strip()

        if state == "1":
            logger.info(
                "pipe_pane_already_active",
                session=self.tmux_session,
                note="user had their own pipe-pane; adoption will not clobber",
            )
            return

        pipe_path = self._resolve_pipe_path()
        pipe_path.parent.mkdir(parents=True, exist_ok=True)
        if not pipe_path.exists():
            pipe_path.touch()

        pipe_cmd = f"cat >> {shlex.quote(str(pipe_path))}"
        rc2, _, err2 = await self._run_tmux(
            "pipe-pane", "-t", target, pipe_cmd,
            check=False,
        )
        if rc2 != 0:
            raise RuntimeError(
                f"pipe-pane failed: "
                f"{err2.decode('utf-8', errors='replace').strip()}"
            )
        logger.info(
            "pipe_pane_started",
            session=self.tmux_session,
            pipe=str(pipe_path),
        )

    @classmethod
    def for_external(
        cls,
        session_name: str,
        working_dir: Path,
        on_output: Optional[Callable[[bytes], Any]] = None,
        socket_name: str = DEFAULT_SOCKET_NAME,
        scrollback_lines: int = 3000,
    ) -> "TmuxBackend":
        """Build a TmuxBackend bound to an EXTERNALLY-created tmux session.

        Alternative constructor for the Track 1 "Adopt an external session"
        flow. Unlike the normal ``TmuxBackend(...)`` path, which slugifies
        ``session_id`` into ``cloude_<slug>``, this preserves the literal
        tmux name the user gave their session — we're adopting, not
        creating.

        Also flips ``self._is_external = True`` so ``attach_existing()``
        takes the adopt-time branch (pipe-pane setup, remain-on-exit,
        window-size WARN) without the caller having to pass the flag.

        Args:
            session_name: literal tmux session name as shown in
                ``tmux -L cloude list-sessions``. MUST NOT contain ``:``
                or ``.`` (tmux target separators).
            working_dir: for metadata only; we never chdir the pane.
            on_output: fan-out callback for streamed bytes.
            socket_name: tmux socket. Defaults to the Cloude Code
                dedicated socket so we only adopt from where we look.
            scrollback_lines: lines captured on adopt for initial paint.

        Raises:
            ValueError: if ``session_name`` is unsafe for a tmux target.
        """
        # Fail fast on unsafe names before any state mutation.
        _safe_target(session_name)

        inst = cls(
            session_id=session_name,
            working_dir=working_dir,
            on_output=on_output,
            socket_name=socket_name,
            scrollback_lines=scrollback_lines,
        )
        # Bypass the slugified ``cloude_<slug>`` naming — we're adopting.
        inst.tmux_session = session_name
        inst.slug = session_name  # used in the pipe-file filename
        inst._is_external = True
        return inst

    def list_attachable_sessions(
        self, owned_names: Optional[set] = None
    ) -> List[Dict[str, Any]]:
        """Enumerate tmux sessions on our socket for the adopt UI.

        Runs ``tmux -L <socket> list-sessions -F
        '#{session_name}|#{session_created}|#{session_windows}'`` and
        splits each line on ``|``. No server running / no sessions → [].

        ``created_by_cloude`` is set by cross-referencing each name
        against ``owned_names`` (the SessionManager-persisted set of
        session names Cloude Code created). When ``owned_names`` is
        None, we fall back to the ``cloude_`` prefix heuristic AND log
        a debug note — callers from the live app path should always
        pass the owned set so a user's ``cloude_whatever`` external
        session doesn't masquerade as ours.

        If ``owned_names`` contains a name that's NOT in the live tmux
        listing, log a WARN (stale metadata — the reconciler should
        prune, but we surface it here too for observability).
        """
        if not shutil.which("tmux"):
            return []

        rc, out, _ = self._run_tmux_sync(
            "list-sessions",
            "-F",
            "#{session_name}|#{session_created}|#{session_windows}",
            check=False,
        )
        if rc != 0:
            # Exit 1 w/ "no server running" — expected when no sessions yet.
            return []

        raw_lines = out.decode("utf-8", errors="replace").splitlines()
        live_names: set = set()
        results: List[Dict[str, Any]] = []

        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 3:
                logger.debug(
                    "list_attachable_sessions_unparseable_row", raw=line
                )
                continue
            name, created_raw, windows_raw = parts[0], parts[1], parts[2]
            live_names.add(name)

            try:
                created_at_epoch = int(created_raw)
            except ValueError:
                created_at_epoch = 0
            try:
                window_count = int(windows_raw)
            except ValueError:
                window_count = 0

            if owned_names is not None:
                created_by_cloude = name in owned_names
            else:
                # Fallback heuristic; caller from live path should pass
                # the owned set so we're not trusting a spoofable prefix.
                created_by_cloude = name.startswith(SESSION_PREFIX)

            results.append({
                "name": name,
                "created_by_cloude": created_by_cloude,
                "created_at_epoch": created_at_epoch,
                "window_count": window_count,
            })

        if owned_names:
            stale = owned_names - live_names
            if stale:
                logger.warning(
                    "owned_tmux_sessions_not_in_live_listing",
                    stale=sorted(stale),
                    note="reconciler should prune these on next startup",
                )

        return results

    async def stop(self) -> None:
        """Kill the tmux session and tear down the read loop."""
        if not self._running and self._reader_task is None:
            return

        logger.info("tmux_backend_stopping", session=self.tmux_session)
        self._running = False

        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("tmux_reader_teardown_error", error=str(exc))
            self._reader_task = None

        # Stop pipe-pane (no-op if session is already gone).
        await self._run_tmux(
            "pipe-pane",
            "-t",
            _safe_target(self.tmux_session),
            check=False,
        )

        # Kill the session on our socket only.
        await self._run_tmux(
            "kill-session",
            "-t",
            self.tmux_session,
            check=False,
        )

        logger.info("tmux_backend_stopped", session=self.tmux_session)

    async def write(self, data: bytes) -> None:
        """Binary-safe write to pane 0.

        Three paths:
        - Short + control-free           → send-keys -l <text>
        - Short + has control bytes      → send-keys -H <hex pairs>
        - Large (paste payload)          → load-buffer + paste-buffer -d -p

        ``send-keys -l`` treats the payload literally as UTF-8 text with no
        key-name lookup — fastest path for regular typing. ``send-keys -H``
        delivers each 2-hex-digit argv token as a literal byte *as a key
        event* — the correct vehicle for keystrokes like Backspace (0x7f),
        Escape (0x1b), arrow keys (\\x1b[A), Ctrl chords (0x01-0x1f), and
        F-keys. ``paste-buffer -d -p`` wraps the payload in bracketed-paste
        markers (\\x1b[200~ ... \\x1b[201~); Claude's TUI uses those to tell
        paste-from-clipboard apart from typed input, so we reserve this path
        for genuinely large payloads that can only be pastes.
        """
        if not self._running:
            raise RuntimeError("TmuxBackend is not running")

        if not data:
            return

        if len(data) > PASTE_THRESHOLD_BYTES:
            # True paste — use bracketed paste so Claude distinguishes from typed input
            await self._write_via_paste_buffer(data)
        elif _has_control_chars(data):
            # Short keystroke with control bytes (arrow, Ctrl-X, Esc, Backspace, F-keys)
            await self._write_via_hex_keys(data)
        else:
            # Short plain text — fastest path
            await self._write_via_send_keys_literal(data)

    async def _write_via_send_keys_literal(self, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace")
        rc, _, err = await self._run_tmux(
            "send-keys",
            "-l",
            "-t",
            _safe_target(self.tmux_session),
            text,
        )
        if rc != 0:
            raise RuntimeError(
                f"tmux send-keys -l failed: {err.decode('utf-8', errors='replace').strip()}"
            )

    async def _write_via_hex_keys(self, data: bytes) -> None:
        # tmux send-keys -H takes each hex pair as ONE argv element.
        # e.g., Backspace (\x7f) -> ["7f"]; Arrow Up (\x1b[A) -> ["1b","5b","41"]
        hex_args = [f"{b:02x}" for b in data]
        rc, _, err = await self._run_tmux(
            "send-keys",
            "-H",
            "-t",
            _safe_target(self.tmux_session),
            *hex_args,
        )
        if rc != 0:
            raise RuntimeError(
                f"tmux send-keys -H failed: {err.decode('utf-8', errors='replace').strip()}"
            )

    async def _write_via_paste_buffer(self, data: bytes) -> None:
        # Load bytes into a named buffer then paste. Buffer name is
        # derived from the slug so concurrent backends (shouldn't happen
        # under single-session, but be safe) don't collide.
        buf_name = f"cloude_{self.slug}"
        rc, _, err = await self._run_tmux(
            "load-buffer",
            "-b",
            buf_name,
            "-",
            stdin_bytes=data,
        )
        if rc != 0:
            raise RuntimeError(
                f"tmux load-buffer failed: {err.decode('utf-8', errors='replace').strip()}"
            )
        rc, _, err = await self._run_tmux(
            "paste-buffer",
            "-d",
            "-p",
            "-b",
            buf_name,
            "-t",
            _safe_target(self.tmux_session),
        )
        if rc != 0:
            raise RuntimeError(
                f"tmux paste-buffer failed: {err.decode('utf-8', errors='replace').strip()}"
            )

    def resize(self, cols: int, rows: int) -> None:
        """Resize the tmux window to match the xterm.js client geometry.

        We use ``resize-window -x -y`` because:

        - ``refresh-client -C`` only works when a client IS attached. We
          never attach one (output is streamed via `pipe-pane`), so
          `refresh-client` is a silent no-op.
        - ``resize-window`` operates server-side. With ``window-size manual``
          (set in `start()`), tmux honors the request regardless of client
          state and emits SIGWINCH to the pane's foreground process so TUI
          apps (Claude CLI, vim, less, etc.) re-render at the new geometry.

        Fire-and-forget so the WS receive loop doesn't block on tmux IPC.
        """
        try:
            self._run_tmux_sync(
                "resize-window",
                "-t",
                self.tmux_session,
                "-x",
                str(cols),
                "-y",
                str(rows),
                check=False,
            )
            # Defensive: older tmux versions may not auto-propagate SIGWINCH
            # after a server-side resize. `refresh-client -S` is a no-op when
            # no client is attached (our case) but documents intent and
            # costs nothing.
            self._run_tmux_sync(
                "refresh-client",
                "-S",
                check=False,
            )
        except Exception as exc:
            logger.debug("tmux_resize_error", error=str(exc))

    def is_alive(self) -> bool:
        """True iff the tmux session exists on our socket."""
        rc, _, _ = self._run_tmux_sync(
            "has-session",
            "-t",
            self.tmux_session,
            check=False,
        )
        return rc == 0

    def discover_existing(self) -> List[str]:
        """List all ``cloude_*`` sessions on our dedicated socket."""
        if not shutil.which("tmux"):
            return []
        rc, out, _ = self._run_tmux_sync(
            "list-sessions",
            "-F",
            "#{session_name}",
            check=False,
        )
        if rc != 0:
            # Exit code 1 with "no server running" is expected when no
            # sessions exist yet.
            return []
        names = out.decode("utf-8", errors="replace").splitlines()
        return [n.strip() for n in names if n.strip().startswith(SESSION_PREFIX)]

    def capture_scrollback(self, lines: int = 3000) -> bytes:
        """Capture the pane's recent scrollback as raw bytes (UTF-8).

        ``capture-pane -p`` writes to stdout. ``-S -<N>`` sets start line N
        lines above the cursor. ``-J`` joins wrapped lines (matches what
        users see in the terminal). ``-e`` preserves ANSI escape sequences
        so xterm.js can replay colors/positioning faithfully.
        """
        if lines <= 0:
            lines = self.scrollback_lines

        self.replay_in_progress = True
        try:
            rc, out, _ = self._run_tmux_sync(
                "capture-pane",
                "-p",
                "-e",
                "-J",
                "-S",
                f"-{lines}",
                "-t",
                _safe_target(self.tmux_session),
                check=False,
            )
            if rc != 0:
                return b""
            return out
        finally:
            # Note: Item 7 will move this flag flip closer to the WS send
            # site (after bytes are written to the socket). For now we
            # clear it immediately — the callback-suppression is still a
            # future-Item-7 concern.
            self.replay_in_progress = False

    async def read_async(self) -> None:
        """Start the background output-tail loop (idempotent)."""
        if self._reader_task and not self._reader_task.done():
            return
        self._reader_task = asyncio.create_task(self._tail_loop())

    # ---- internal read loop ---------------------------------------------

    async def _tail_loop(self) -> None:
        """Tail the pipe-pane file and fan out bytes via `on_output`."""
        pipe_path = self._resolve_pipe_path()

        # Wait briefly for the file to exist (pipe-pane creates on first write).
        deadline = time.monotonic() + 5.0
        while not pipe_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        # Open once, then seek to end-of-file so a restart doesn't re-emit
        # everything. For replay, callers use `capture_scrollback()` instead.
        try:
            # Use a raw fd so we can set O_NONBLOCK.
            fd = os.open(str(pipe_path), os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            logger.warning("tmux_pipe_file_missing", path=str(pipe_path))
            return

        try:
            # Seek position:
            #   - Adoption path: to the recorded post-pipe-pane byte
            #     offset so we resume exactly where the initial
            #     scrollback painted. Bounded to actual file size —
            #     an offset larger than the file (shouldn't happen
            #     but defensive) degrades to SEEK_END.
            #   - Normal path (create / rehydrate): SEEK_END — we
            #     only want bytes produced after we started reading.
            if self._adopt_tail_start_offset is not None:
                try:
                    current_size = os.fstat(fd).st_size
                except OSError:
                    current_size = 0
                seek_to = min(self._adopt_tail_start_offset, current_size)
                try:
                    os.lseek(fd, seek_to, os.SEEK_SET)
                except OSError:
                    # Fall back to EOF — no worse than normal rehydrate.
                    try:
                        os.lseek(fd, 0, os.SEEK_END)
                    except OSError:
                        pass
                logger.info(
                    "tmux_tail_seek_adopt_offset",
                    session=self.tmux_session,
                    offset=seek_to,
                    recorded=self._adopt_tail_start_offset,
                    file_size=current_size,
                )
                # Single-use — clear so subsequent fd reopens (rotation)
                # use SEEK_END like the normal path.
                self._adopt_tail_start_offset = None
            else:
                try:
                    os.lseek(fd, 0, os.SEEK_END)
                except OSError:
                    pass

            while self._running:
                # Rotation check — once a second is plenty.
                now = time.monotonic()
                if now - self._last_rotate_check > 1.0:
                    self._last_rotate_check = now
                    await self._maybe_rotate(pipe_path, fd)

                try:
                    chunk = os.read(fd, 8192)
                except BlockingIOError:
                    chunk = b""
                except OSError as exc:
                    logger.warning("tmux_pipe_read_error", error=str(exc))
                    await asyncio.sleep(0.1)
                    continue

                if not chunk:
                    await asyncio.sleep(0.02)
                    continue

                if self.on_output is not None:
                    try:
                        result = self.on_output(chunk)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        logger.error("tmux_on_output_error", error=str(exc))

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("tmux_tail_loop_crashed", error=str(exc))
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    async def _maybe_rotate(self, pipe_path: Path, current_fd: int) -> None:
        """Rotate the pipe file if it's too big or too old.

        We rename the current file to ``<name>.1``, then truncate the pipe
        back to zero. tmux's ``cat >> file`` keeps appending after our
        rename because the shell re-opens the path each time the pipe-pane
        hook fires — no tmux restart needed. We re-point our read fd at the
        freshly-truncated file.
        """
        try:
            st = os.stat(str(pipe_path))
        except FileNotFoundError:
            return

        age_hours = (time.monotonic() - self._rotation_started_at) / 3600.0
        too_big = st.st_size > MAX_LOG_BYTES
        too_old = age_hours > ROTATE_AGE_HOURS

        if not (too_big or too_old):
            return

        logger.info(
            "tmux_pipe_rotating",
            size=st.st_size,
            age_hours=round(age_hours, 2),
            reason="size" if too_big else "age",
        )

        rotated = pipe_path.with_suffix(pipe_path.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
            os.rename(str(pipe_path), str(rotated))
            # Truncate by creating a new empty file at the original path.
            pipe_path.touch()
            # Permissive perms so tmux (same uid) can keep writing.
            os.chmod(str(pipe_path), stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            logger.warning("tmux_pipe_rotate_failed", error=str(exc))
            return

        self._rotation_started_at = time.monotonic()
