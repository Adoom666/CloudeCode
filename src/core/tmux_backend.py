"""Tmux-backed session backend.

Uses a DEDICATED tmux server socket (``tmux -L cloude``) so we never touch
the user's default tmux server. Sessions are named ``cloude_<slug>``.

Key design points:

- **Binary-safe writes** (three-path routing):

  ``send-keys -l <text>`` for short, control-free UTF-8 - the fast path
  for regular typing.

  ``send-keys -H <hex pairs>`` for short byte sequences that contain
  control chars (arrow keys, Ctrl-X, Esc, Backspace - real keystrokes).
  tmux treats each hex pair as a literal byte delivered via key event,
  which interactive TUIs interpret correctly.

  ``load-buffer`` + ``paste-buffer -d -p`` reserved for LARGE payloads
  (actual clipboard pastes). Bracketed-paste markers let Claude
  distinguish paste from typed input - correct behavior for paste,
  wrong behavior for keystrokes.

- **Output streaming**: ``tmux pipe-pane -o 'cat >> <fifo>'`` streams every
  pane byte to a file. We tail that file asynchronously and call
  `on_output(bytes)` for every chunk. The file is rotated when it exceeds
  ``MAX_LOG_BYTES`` or is older than ``ROTATE_AGE_HOURS``.

- **Single-active invariant**: the backend itself does NOT enforce
  one-at-a-time; `SessionManager` does. This backend DOES refuse to start
  if a session with the same name already exists - callers must call
  ``discover_existing()`` first to re-attach.

- **Restart survival**: `discover_existing()` lists ``cloude_*`` sessions on
  the dedicated server. `SessionManager.lifespan_startup` uses it to
  re-register the slug stored in ``session_metadata.json``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from src.core import debug_trace

from src.core.pane_locale import apply_pane_locale
from src.core.tmux_discovery import resolve_tmux_path, tmux_argv_prefix
from src.core.tmux_listing_parse import (
    LISTING_FORMAT,
    parse_listing_row,
    resolve_ownership,
    split_listing_rows,
)
from src.core.tmux_listing import (
    REASON_PROBE_ERROR,
    REASON_TIMEOUT,
    REASON_TMUX_MISSING,
    TmuxListing,
    classify_listing_failure,
    listing_env,
)
from src.core.scrollback_replay import normalize_replay_newlines
from src.core.session_backend import SessionBackend
from src.core.session_respawn import (
    RESPAWN_PANE_FORMAT,
    RespawnResult,
    parse_respawn_probe,
    resolve_respawn_plan,
)

logger = structlog.get_logger()

# ---- Tunables ---------------------------------------------------------------
# Module-scope constants (not in config.json) so they're easy to find in code.
# If we ever want to expose these, wire through `AuthConfig.session` - for now
# the values below are battle-tested defaults.

#: Wall-clock budget for a one-shot ENUMERATION call (list-sessions /
#: list-panes). These three run on the request path - the launchpad polls
#: them every few seconds - so a tmux server wedged on a stuck socket must
#: not hold an HTTP worker open indefinitely. On expiry the probe reports
#: ``ok=False, reason='timeout'`` rather than an empty list, per the
#: THREE-OUTCOME RULE. Only the listing calls take it; write/attach paths
#: are deliberately left unbounded because they are not poll-driven.
LIST_TIMEOUT_SECONDS: float = 5.0

#: Rotate the pipe-pane log once it passes 10 MiB.
MAX_LOG_BYTES: int = 10 * 1024 * 1024

#: Rotate regardless of size after this many hours. 24h matches a normal
#: coding-session cadence.
ROTATE_AGE_HOURS: int = 24

#: Default starting window geometry for new tmux sessions. We never attach a
#: client (output is streamed via pipe-pane), so tmux has no client dims to
#: key off of. Without `-x/-y` + `window-size manual`, tmux clamps the
#: window to its 80x24 birth size forever - making TUI apps like Claude CLI
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

#: Scrollback rows tmux keeps per pane on OUR socket.
#:
#: CloudeCode carries its own explicit tmux settings and deliberately does
#: NOT source the user's ``~/.tmux.conf``: a personal config references
#: plugins (tpm, resurrect, continuum) that will not exist on another
#: machine, so inheriting it makes the app's behaviour depend on the box
#: it runs on. The cost of owning the settings is that a default we never
#: state is the default we get - the socket was silently running tmux's
#: stock 2000, a quarter of what the same user configures for himself.
#:
#: Applied with ``set-option -g`` BEFORE ``new-session``. tmux resolves
#: history-limit through the normal option lookup when it trims a pane's
#: history, so a later change reaches existing panes too, but setting it
#: first means a pane is never briefly born under the stock limit.
HISTORY_LIMIT: int = 10000

#: Session name prefix - ``cloude_<slug>``.
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


def _safe_target(session_name: str, pane: Optional[str] = None) -> str:
    """Compose a tmux target string safely.

    tmux parses ``:`` as the window/pane separator and ``.`` as the pane
    separator within a target. If either appears inside ``session_name``
    the command tmux actually executes is NOT the one we meant to send -
    it selects a different (possibly wrong) target.

    We use list-form argv everywhere (``asyncio.create_subprocess_exec``,
    ``subprocess.run`` with a list), so shell-metacharacter injection is
    already impossible. This helper is about the OTHER vector: tmux's own
    target-parsing semantics. We refuse to format a target that would be
    interpreted differently than intended.

    WINDOW INDEX: this used to hardcode ``<session>:0.0``. That is wrong on
    any machine whose tmux.conf sets ``base-index 1`` / ``pane-base-index 1``
    (a very common setting): the first window is index 1, so every
    ``send-keys -t <session>:0.0`` fails with ``can't find window: 0`` and
    NOTHING typed in the browser ever reaches the pane. Targeting the bare
    session name instead resolves to that session's CURRENT window and
    pane, which is both base-index-agnostic and more correct - a session
    with a second window should receive input where the user is looking,
    not always in window 0.

    Args:
        session_name: tmux session name. MUST NOT contain ``:`` or ``.``.
        pane: optional explicit ``<window>.<pane>`` specifier. Omit it (the
            default) to target the session's current window/pane. Callers
            SHOULD keep this a literal - it is never user-controlled and is
            not validated.

    Returns:
        Formatted target string suitable for ``-t``.

    Raises:
        ValueError: if ``session_name`` contains ``:`` or ``.``.

    Example: _safe_target("cloude_demo") -> "cloude_demo"
    """
    if ":" in session_name or "." in session_name:
        raise ValueError(
            f"unsafe tmux session name {session_name!r}: "
            f"contains ':' or '.' which tmux parses as target separators"
        )
    if pane is None:
        return session_name
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
        # means "seek to EOF" - the normal create/rehydrate behavior.
        self._adopt_tail_start_offset: Optional[int] = None

    # ---- internal helpers ------------------------------------------------

    def _tmux_base(self) -> List[str]:
        """Common tmux argv prefix - always uses our dedicated socket.

        Uses the ABSOLUTE path resolved by src.core.tmux_discovery, not the
        bare name "tmux". A subprocess inherits this process's PATH, and a
        GUI-launched app has no shell PATH, so a bare name resolves only
        when whoever launched us happened to patch PATH first. Falls back to
        the bare name when nothing resolved, so start()'s own missing-tmux
        error is what the user sees.
        """
        return tmux_argv_prefix(self.socket_name)

    async def _apply_history_limit(self) -> None:
        """Set this socket's scrollback depth to :data:`HISTORY_LIMIT`.

        Idempotent and cheap; tmux ignores a repeat set of the same value.
        ``check=False`` because a socket that cannot take the option is not
        a reason to refuse the session - the pane just keeps the stock
        depth, which is what it had before this existed.

        Returns:
            None.
        """
        await self._run_tmux(
            "set-option", "-g", "history-limit", str(HISTORY_LIMIT), check=False
        )

    async def _apply_remain_on_exit(self) -> None:
        """Turn on ``remain-on-exit`` globally, BEFORE any window exists.

        Inputs:
            None.

        Returns:
            None.

        ``remain-on-exit`` is a WINDOW option, so it has to be set with
        ``-w``; ``-g`` alone writes the session table and the window is
        born without it. Setting it globally before ``new-session`` is the
        whole point: an agent that fails fast - binary not on PATH, auth
        banner then a non-zero exit - can be gone before a post-creation
        ``set-option`` lands, and then there is no window left to set the
        option ON. Measured on tmux 3.7c: with this set first, a session
        whose command is ``true`` leaves ``pane_dead=1``; without it, the
        very next ``list-panes`` answers "can't find window".

        Today the dead-on-arrival probe tears that corpse down anyway, so
        the race is benign - but it is benign by accident, and it cost a
        previous session five test failures to work out why. The pane is
        now guaranteed to exist for the probe to read.

        ``check=False`` for the same reason as the history limit: a socket
        that will not take the option is not a reason to refuse a session.
        """
        await self._run_tmux(
            "set-option", "-wg", "remain-on-exit", "on", check=False
        )

    async def read_history_limit(self) -> Optional[int]:
        """Read the socket's current global ``history-limit``.

        Verification seam: the point of the setting is the number tmux
        actually holds, and ``show-options`` is the only thing that
        reports it.

        Returns:
            The configured row count, or None when tmux could not be
            asked or answered with something non-numeric. None is a real
            third answer - "could not determine" - and callers must not
            read it as the default.
        """
        rc, out, _ = await self._run_tmux(
            "show-options", "-gv", "history-limit", check=False
        )
        if rc != 0:
            return None
        raw = out.decode("utf-8", errors="replace").strip()
        try:
            return int(raw)
        except ValueError:
            return None

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
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> tuple[int, bytes, bytes]:
        """Sync variant for use in `is_alive`, `discover_existing`, etc.

        Inputs:
            *args: tmux arguments appended to ``self._tmux_base()``.
            stdin_bytes: optional bytes to pipe to the process.
            check: log a debug line on a non-zero exit.
            timeout: wall-clock budget in seconds, or None for unbounded.
                The three enumeration methods pass
                ``LIST_TIMEOUT_SECONDS`` because they run on the polled
                request path.
            env: complete environment for the child, or None to inherit
                this process's. Only the LISTING path passes one (see
                :data:`LISTING_ENV_OVERRIDES`); commands that CREATE a
                session must inherit, because the environment handed to
                ``new-session`` becomes the user's shell environment and
                forcing a locale there would break their rendering.

        Output:
            tuple[int, bytes, bytes]: returncode, stdout, stderr.

        Raises:
            subprocess.TimeoutExpired: when ``timeout`` elapses. Callers
                on the listing path catch this and report
                ``ok=False, reason='timeout'`` - never an empty list.
        """
        import subprocess

        argv = self._tmux_base() + list(args)
        proc = subprocess.run(
            argv,
            input=stdin_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
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
        other is treated as "not supplied" - we don't mix a client dim with
        a default, because that would create an asymmetric starting pane
        (e.g. client gives cols=100, we'd pair with default rows=40 which
        is almost certainly wrong for that viewport).
        """
        if self._running:
            raise RuntimeError("TmuxBackend already running")

        if resolve_tmux_path() is None:
            raise RuntimeError(
                "tmux was not found on PATH or at any well-known install "
                "location (see src/core/tmux_discovery.WELL_KNOWN_PATHS)"
            )

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

        # Scrollback depth BEFORE the pane exists - see HISTORY_LIMIT.
        # ``set-option`` also starts the tmux server when none is running,
        # which is exactly the ordering we want on a cold socket.
        await self._apply_history_limit()

        # remain-on-exit BEFORE the window exists - see the method's
        # docstring. Set after creation it is a race the fast-failing agent
        # wins.
        await self._apply_remain_on_exit()

        # Build the session. ``new-session -d -s <name> -c <cwd> [command]``.
        # If a command is supplied, tmux runs that as pane 0's process; the
        # shell exits when the command ends unless ``remain-on-exit`` is set,
        # which is why it is set globally just above rather than here.
        #
        # ``-x`` / ``-y`` fix the window's birth geometry. Without them tmux
        # uses 80x24 and - combined with default ``window-size latest`` and
        # zero attached clients - stays there forever. We pair these with
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
        # The env overlay. It goes into this invocation's environment AND,
        # below, onto the command as ``-e`` pairs.
        #
        # THE COMMENT THAT USED TO BE HERE SAID "tmux captures the
        # environment of the new-session call". That is true only when this
        # call is what STARTS the server. When a server is already running
        # on our socket - the normal case for every session after the first
        # - the new session's environment comes from the SERVER's global
        # table and the client's environment is discarded.
        #
        # The consequence was not a missing variable, which would have been
        # obvious. It was a STALE one: every session after the first
        # inherited the CLOUDECODE_SESSION_ID captured when the server
        # started, so Claude's SessionStart hook POSTed a session id
        # belonging to a DIFFERENT, long-dead session. The hook fired, the
        # request succeeded, and the binding resolved UNRESOLVED - so
        # claude_session_uuid was never written, and anything that needs it
        # (resume, fork) could never work. Measured on a real install: a
        # pane created today carried an id from six days earlier.
        #
        # The locale block below already knew this and used ``-e``. The
        # lesson had been learned for LANG and not applied to these.
        tmux_env = os.environ.copy()
        tmux_env.setdefault("TERM", "xterm-256color")
        tmux_env.setdefault("COLORTERM", "truecolor")
        if env:
            tmux_env.update(env)

        # ---- Pane locale ----------------------------------------------------
        # A LaunchAgent-spawned server inherits no LANG at all, so the
        # pane's shell lands in the 7-bit "C" locale and zsh prints
        # "character not in range" once per line of any function that
        # touches a multibyte character - on every session start, before
        # the user has typed anything.
        #
        # It has to travel as ``new-session -e``, NOT merely in tmux_env.
        # When a tmux SERVER is already running on our socket (the normal
        # case after the first session), the new session's environment is
        # taken from the SERVER's global environment, and the client's
        # environment is discarded. So exporting LANG here would silently
        # do nothing for every session but the first. ``-e`` sets the
        # session environment explicitly, and it applies to pane 0 because
        # it is part of the same command that creates it. ``set-environment``
        # after the fact would be too late for pane 0, and
        # ``update-environment`` only feeds attaching clients, which we
        # never have - we stream via pipe-pane.
        apply_pane_locale(tmux_env)
        pane_lang = tmux_env.get("LANG")
        if pane_lang:
            args.extend(["-e", f"LANG={pane_lang}"])

        # The caller's overlay, explicitly, for the reason spelled out
        # above: without this the pane gets the SERVER's stale copy.
        #
        # Only the keys the caller actually passed - never os.environ - so
        # this cannot leak the app's whole environment into a user's pane.
        # A value carrying a newline is skipped rather than truncated: tmux
        # takes one KEY=VALUE per -e, so a newline would make the remainder
        # unparseable, and half an environment variable is worse than none.
        for key, value in sorted((env or {}).items()):
            if value is None:
                continue
            text = str(value)
            if "\n" in text or "\r" in text or not key or "=" in key:
                logger.warning(
                    "tmux_env_pair_skipped",
                    key=key,
                    reason="key or value cannot be expressed as one -e pair",
                )
                continue
            args.extend(["-e", f"{key}={text}"])

        if command:
            args.append(command)

        argv = self._tmux_base() + args
        # DEBUG TRACE. The stale-environment bug lived exactly here and was
        # invisible: the spawn succeeded, the variable was set, and its
        # VALUE belonged to a different session. Recording the argv and the
        # keys we passed makes that answerable in one grep instead of an
        # hour. Values are scrubbed; the session id is not a secret and is
        # the whole point of looking.
        debug_trace.trace(
            "tmux.new_session",
            session=self.tmux_session,
            cwd=str(self.working_dir),
            env_keys_passed=sorted((env or {}).keys()),
            cloudecode_session_id=(env or {}).get("CLOUDECODE_SESSION_ID"),
            dash_e_pairs=[a for i, a in enumerate(args) if i and args[i - 1] == "-e"],
            has_command=bool(command),
            argv_head=argv[:6],
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=tmux_env,
        )
        _, stderr = await proc.communicate()
        debug_trace.trace(
            "tmux.new_session.result",
            session=self.tmux_session,
            returncode=proc.returncode,
            stderr=stderr.decode("utf-8", errors="replace").strip()[:300],
        )
        if proc.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"tmux new-session failed: {msg.strip()}")

        # Belt and braces: re-assert it on THIS session explicitly, so the
        # invariant does not depend on the socket's global table still
        # saying what it said a moment ago. The global set above is what
        # makes the window exist at all when the command exits instantly;
        # this line is what keeps the setting readable per session.
        await self._run_tmux(
            "set-option", "-t", self.tmux_session, "remain-on-exit", "on", check=False
        )

        # ---- Dead-on-arrival probe ------------------------------------------
        # ``remain-on-exit on`` is a double-edged blade: long, healthy sessions
        # need it for replay/scrollback, but it ALSO preserves an empty dead
        # pane forever when the spawned agent exits immediately (binary not
        # on PATH, auth banner + non-zero exit, missing config, etc.). The
        # tmux session creation succeeded, ``pipe-pane`` attaches to a pane
        # that will never write a byte, and the WebSocket lands on a frozen
        # "PTY terminal ready" welcome screen with no diagnostic.
        #
        # Mitigation: pause briefly to let the child either start or fail,
        # then ask tmux ``#{pane_dead}`` + ``#{pane_dead_status}``. If dead,
        # capture whatever the pane managed to print (banner / stderr),
        # tear the corpse down so the user can retry without a name
        # collision, and raise a RuntimeError that propagates up to the
        # API layer as a 502.
        #
        # 250ms floor is the spec - empirically catches exec-not-found,
        # missing-binary, and immediate-banner-and-exit cases on modern
        # hardware while staying well below user-perceived launch latency.
        await asyncio.sleep(0.25)

        target_for_probe = _safe_target(self.tmux_session)
        rc_probe, out_probe, _ = await self._run_tmux(
            "list-panes",
            "-t",
            self.tmux_session,
            "-F",
            "#{pane_dead} #{pane_dead_status}",
            check=False,
        )
        if rc_probe == 0:
            probe_line = out_probe.decode("utf-8", errors="replace").splitlines()[0] \
                if out_probe.strip() else ""
            parts = probe_line.split(" ", 1)
            pane_dead = parts[0].strip() if parts else ""
            pane_dead_status = parts[1].strip() if len(parts) > 1 else ""

            if pane_dead == "1" or pane_dead_status:
                # Capture pane output (banner / stderr) for diagnostics.
                rc_cap, out_cap, _ = await self._run_tmux(
                    "capture-pane",
                    "-t",
                    target_for_probe,
                    "-p",
                    "-S",
                    "-200",
                    check=False,
                )
                first_meaningful = ""
                if rc_cap == 0:
                    for raw_line in out_cap.decode("utf-8", errors="replace").splitlines():
                        # Strip ANSI escape sequences so the surfaced message
                        # is human-readable (the launch banner often opens
                        # with cursor/color escapes). Coarse CSI/OSC strip -
                        # good enough for a one-line error surface.
                        cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", raw_line)
                        cleaned = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", cleaned)
                        cleaned = cleaned.strip()
                        if cleaned:
                            first_meaningful = cleaned
                            break

                # Tear down the dead session so the user can retry without
                # tmux complaining about a duplicate session name.
                await self._run_tmux(
                    "kill-session",
                    "-t",
                    self.tmux_session,
                    check=False,
                )

                logger.error(
                    "tmux_pane_dead_on_arrival",
                    session=self.tmux_session,
                    command=command,
                    pane_dead=pane_dead,
                    pane_dead_status=pane_dead_status,
                    capture=first_meaningful[:500],
                )

                if first_meaningful:
                    raise RuntimeError(
                        f"agent failed to launch: {first_meaningful}"
                    )
                raise RuntimeError(
                    f"agent failed to launch: process exited immediately "
                    f"(pane_dead_status={pane_dead_status or 'unknown'})"
                )

        # Enable extended keys (tmux 3.2+) so modifier+key sequences like
        # Shift+Enter arrive as CSI u (`\x1b[13;2u`) at the pane intact,
        # instead of being collapsed to bare CR. Required for Claude Code
        # CLI's multi-line input prompt to recognize Shift+Enter as
        # "newline-insert" vs. CR=submit. Paired with the terminal-features
        # `extkeys` flag below which advertises extended-key support to the
        # pane's $TERM - Claude reads the terminfo to decide whether to
        # emit CSI u or legacy keys.
        #
        # ``-s`` targets the tmux server (global, persists for the life of
        # the tmux socket). Safe to re-run per session; tmux ignores repeat
        # sets of the same value.
        await self._run_tmux(
            "set-option", "-s", "extended-keys", "on", check=False
        )

        # Enable mouse mode on the cloude socket so wheel events get intercepted
        # by tmux instead of going through as arrow keys to TUI apps in alt-screen
        # (e.g. Claude Code, which would otherwise cycle prompt history).
        # Global session option so all sessions on the cloude socket pick it up.
        await self._run_tmux("set-option", "-g", "mouse", "on", check=False)

        # Override the default WheelUp/DownPane bindings: in alt-screen (any TUI),
        # enter copy-mode and let tmux's scrollback drive the wheel. In normal
        # screen mode (a shell prompt), forward as usual so rare shell-mouse use
        # still works. -T root binds at the root key-table.
        await self._run_tmux(
            "bind-key", "-T", "root", "WheelUpPane",
            "if-shell", "-Ft=", "#{alternate_on}",
            "copy-mode -e ; send-keys -X -N 3 scroll-up",
            "send-keys -M",
            check=False,
        )
        await self._run_tmux(
            "bind-key", "-T", "root", "WheelDownPane",
            "if-shell", "-Ft=", "#{pane_in_mode}",
            "send-keys -X -N 3 scroll-down",
            "send-keys -M",
            check=False,
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
        # (We only ever have window 0, but be defensive - future code that
        # adds a second window shouldn't silently shrink pane 0.)
        await self._run_tmux(
            "set-option", "-t", self.tmux_session, "aggressive-resize", "off", check=False
        )

        # Start pipe-pane - this streams pane output to our file.
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

        For EXTERNAL sessions (Track 1 "Adopt an external session" flow -
        user started it via ``tmux -L cloude new -s <name>``), there is
        likely NO pipe-pane active yet, so the caller passes
        ``needs_pipe_setup=True`` to trigger:

        1. Refuse if ``#{pane_dead}`` is ``"1"`` - a dead pane can't be
           usefully adopted.
        2. ``ensure_pipe_pane()`` - query first, start only if not already
           active (non-toggle).
        3. ``set-option remain-on-exit on`` defensively so an external
           child exiting doesn't silently collapse the pane while our
           adoption is live.
        4. WARN if ``window-size`` isn't ``manual`` - ``resize-window``
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
        # caller made a mistake - raise loudly so the upstream rehydrate
        # path can clean up stale metadata instead of entering a bogus state.
        if not self.is_alive():
            raise RuntimeError(
                f"attach_existing: tmux session {self.tmux_session} is not alive"
            )

        do_external_setup = needs_pipe_setup or self._is_external

        if do_external_setup:
            target = _safe_target(self.tmux_session)

            # 1. Refuse dead panes - nothing to stream from, and our attempts
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

            # 2b. Record the FIFO offset NOW - immediately after
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

            # 4. Make the adopted session RESIZABLE, rather than logging a
            # warning that it is not.
            #
            # MEASURED 2026-08-17: an adopted session sat at 80x24 (tmux's
            # birth default) while an app-created one on the same socket
            # was 163x46. The difference was entirely here. A session
            # created outside the app keeps ``window-size latest``, which
            # sizes the window to the most recently attached CLIENT - and
            # this app never attaches one, it streams with ``pipe-pane``.
            # With zero clients tmux has nothing to size to, so the window
            # stays at its birth geometry and every ``resize-window`` we
            # issue is undone. Adoption is a supported feature, so an
            # adopted session gets the same three settings a created one
            # does and the WS resize handshake then sticks.
            await self._run_tmux(
                "set-option", "-t", target, "window-size", "manual", check=False,
            )
            await self._run_tmux(
                "set-option", "-t", target, "aggressive-resize", "off", check=False,
            )
            await self._apply_history_limit()

        # Recompute / re-resolve pipe file path. It was written to by the
        # old Python process; the tmux server kept pipe-pane running, so
        # the file is still being appended to. We tail from current EOF.
        pipe_path = self._resolve_pipe_path()
        if not pipe_path.exists():
            # Shouldn't happen if tmux's pipe-pane is alive, but handle gracefully
            # by re-running pipe-pane to re-establish the pipe. This is a
            # defensive reconnect - the old pipe-pane process inside tmux
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
        """Start ``pipe-pane`` on pane 0, replacing any pipe already active.

        Why query-then-act instead of just calling ``pipe-pane``:
        ``pipe-pane -o`` is explicitly a TOGGLE in tmux (since 1.8): running
        it on a pane that already has an active pipe STOPS piping. So we query
        ``#{pane_pipe}`` first (``"0"`` = no pipe, ``"1"`` = pipe active) and
        branch on the answer rather than toggling blind.

        When a pipe IS already active (typically the user's own session
        logging) we close it with a bare ``pipe-pane`` and start ours. This
        overrides the user's pipe on purpose. The original behaviour was to
        log and return, leaving theirs alone, but an adopted session then had
        no pipe CloudeCode could read: the websocket streaming loop tailed an
        empty file forever and the browser showed a frozen terminal. Streaming
        the session is the whole point of adoption, so ours has to win.

        We use ``pipe-pane`` WITHOUT ``-o`` when we start ours, because ``-o``
        is the toggle form and by this point no pipe is active either way, so
        we want the explicit non-toggle start semantics.

        Inputs: none. Reads ``self.tmux_session`` and ``self._is_external``.
        Outputs: None. Raises RuntimeError if the backend is not running and
        not external, if the ``#{pane_pipe}`` probe fails, or if starting the
        pipe fails.
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
            # Adoption contract: when the user hands the session over to
            # CloudeCode, our pipe MUST be the one delivering bytes - otherwise
            # the WS streaming loop tails an empty file forever and the
            # browser sees a frozen banner. Close whatever pipe is already
            # active (typically the user's own logging pipe-pane) before
            # starting ours. `pipe-pane` with no command closes any
            # currently-piped command on the target pane.
            logger.info(
                "pipe_pane_replacing_existing",
                session=self.tmux_session,
                note="closing user's pipe-pane so adoption can stream output",
            )
            await self._run_tmux(
                "pipe-pane", "-t", target,
                check=False,
            )

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
        tmux name the user gave their session - we're adopting, not
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
        # Bypass the slugified ``cloude_<slug>`` naming - we're adopting.
        inst.tmux_session = session_name
        inst.slug = session_name  # used in the pipe-file filename
        inst._is_external = True
        return inst

    def _run_listing(self, *args: str) -> tuple[Optional[TmuxListing], str]:
        """Run one ENUMERATION tmux command and split the two outcomes apart.

        Description: The single gate every listing method goes through, so
            the "tmux is absent / timed out / errored" branches cannot
            drift apart between the three of them. Returns a ready-made
            failure ``TmuxListing`` OR decoded stdout, never both. A
            ``no_server`` exit is NOT a failure and comes back as a
            successful empty listing (see
            :func:`src.core.tmux_listing.classify_listing_failure`).

        Inputs:
            *args (str): tmux arguments appended to ``self._tmux_base()``.

        Output:
            tuple[Optional[TmuxListing], str]: when the first element is
                not None it is the FINAL result and the caller must return
                it verbatim (it may be a legitimate empty answer with
                ``ok=True, reason='no_server'``). When it is None, the
                second element is tmux's decoded stdout, ready to parse.

        Example:
            >>> failure, text = backend._run_listing("list-sessions", "-F", "#S")
            >>> failure is None
            True
        """
        import subprocess

        if resolve_tmux_path() is None:
            # Not "zero sessions" - we have no way to ask the question.
            return (
                TmuxListing.unavailable(
                    REASON_TMUX_MISSING,
                    detail="tmux not found on PATH or at any well-known "
                           "install location",
                ),
                "",
            )

        try:
            rc, out, err = self._run_tmux_sync(
                *args,
                check=False,
                timeout=LIST_TIMEOUT_SECONDS,
                env=listing_env(),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "tmux_listing_timeout",
                argv=args,
                socket=self.socket_name,
                timeout_seconds=LIST_TIMEOUT_SECONDS,
            )
            return (
                TmuxListing.unavailable(
                    REASON_TIMEOUT,
                    detail=f"tmux did not answer within {LIST_TIMEOUT_SECONDS}s",
                ),
                "",
            )
        except OSError as exc:
            logger.warning(
                "tmux_listing_probe_error",
                argv=args,
                socket=self.socket_name,
                error=str(exc),
            )
            return (
                TmuxListing.unavailable(REASON_PROBE_ERROR, detail=str(exc)),
                "",
            )

        if rc != 0:
            stderr_text = err.decode("utf-8", errors="replace")
            listing = classify_listing_failure(rc, stderr_text)
            if not listing.ok:
                # A non-zero exit we could NOT read as "no server" is a
                # real error. Warn, because the alternative history of
                # this code silently called it zero sessions.
                logger.warning(
                    "tmux_listing_unavailable",
                    argv=args,
                    socket=self.socket_name,
                    returncode=rc,
                    reason=listing.reason,
                    stderr=stderr_text.strip()[:200],
                )
            return listing, ""

        return None, out.decode("utf-8", errors="replace")

    def list_attachable_sessions(
        self,
        owned_names: Optional[set] = None,
        owned_instances: Optional[set] = None,
    ) -> TmuxListing:
        """Enumerate tmux sessions on our socket for the adopt UI.

        Runs ``tmux -L <socket> list-sessions -F LISTING_FORMAT`` and
        parses each line with
        :func:`src.core.tmux_listing_parse.parse_listing_row`. The
        caller-controlled session NAME is the LAST field and the split is
        bounded, so a name containing the ``|`` delimiter can no longer
        forge the fields in front of it. A row that does not validate is
        refused and logged, never half-parsed.

        Inputs:
            owned_names (Optional[set]): names this app persisted as its
                own, used to resolve ``created_by_cloude``.
            owned_instances (Optional[set]): ``(tmux_name, epoch)`` pairs
                sourced from ``sessions.origin`` (feat/sessions-table,
                S4). PREFERRED over ``owned_names`` when supplied,
                because it identifies the tmux INSTANCE rather than the
                name, and the name is not an identity - it is reusable.
                Every entry must carry an INTEGER epoch. A ``None``
                epoch is NOT a wildcard and is ignored - the wildcard
                form used to defeat the epoch tier for exactly the
                sessions it protects. ``None`` for the whole argument
                means "no instance opinion", and resolution falls back to
                ``owned_names``.

        Output:
            TmuxListing: ``ok=True`` with one dict row per session when
                the probe ran (``sessions=[]`` with
                ``reason='no_server'`` is a real answer of zero);
                ``ok=False`` with ``sessions=[]`` when tmux is missing,
                timed out, or failed - the caller must not read that as
                zero sessions.

        RESOLUTION ORDER for ``created_by_cloude`` lives in
        :func:`src.core.tmux_listing_parse.resolve_ownership`, which
        documents all four tiers. The one worth repeating here is tier 2:
        if the datastore holds ANY instance for this NAME under a
        different epoch, the answer is False and the legacy name set is
        never consulted. Without that tier, a session named ``foo`` that
        this app owned could die, the user could create a new unrelated
        ``foo``, and the new process would badge as ours off the name
        alone.

        If ``owned_names`` contains a name that's NOT in the live tmux
        listing, log a WARN (stale metadata - the reconciler should
        prune, but we surface it here too for observability). That WARN
        is only meaningful on a listing that ran, so it is skipped
        entirely on the unavailable path.

        Example:
            >>> backend.list_attachable_sessions(owned_names=set()).ok
            True
        """
        failure, stdout_text = self._run_listing(
            "list-sessions", "-F", LISTING_FORMAT
        )
        if failure is not None:
            return failure

        # split_listing_rows, NEVER str.splitlines(). A session name may
        # legally contain NEL, LS or PS, all three of which splitlines()
        # treats as row terminators - so one tmux row became two parser
        # rows, the second one entirely caller-chosen, forging the
        # identity triple this method badges ownership from. See
        # tmux_listing_parse's module docstring.
        raw_lines = split_listing_rows(stdout_text)
        live_names: set = set()
        results: List[Dict[str, Any]] = []
        # Counted, not just logged. A refused row makes this listing a
        # VALID answer that is not a COMPLETE one, and any caller
        # reasoning from ABSENCE (the lifecycle reconciler) must be able
        # to tell those apart before it writes a verdict to disk. See
        # TmuxListing.complete.
        refused_rows = 0

        for line in raw_lines:
            row = parse_listing_row(line)
            if row is None:
                # A row we cannot fully validate is REFUSED, never
                # half-trusted. Logged so a format change shows up as
                # rows going missing WITH a reason, not as a short list.
                refused_rows += 1
                if line.strip():
                    logger.warning(
                        "list_attachable_sessions_unparseable_row",
                        raw=line.strip()[:200],
                        note=(
                            "row did not match LISTING_FORMAT; refused "
                            "rather than parsed on a best-effort basis"
                        ),
                    )
                continue

            name = row["name"]
            created_at_epoch = row["created_at_epoch"]
            live_names.add(name)

            created_by_cloude = resolve_ownership(
                name,
                created_at_epoch,
                owned_instances,
                owned_names,
                prefix=SESSION_PREFIX,
            )

            results.append({
                "name": name,
                "created_by_cloude": created_by_cloude,
                "created_at_epoch": created_at_epoch,
                "window_count": row["window_count"],
                "tmux_session_id": row["session_id"],
            })

        if owned_names:
            stale = owned_names - live_names
            if stale:
                logger.warning(
                    "owned_tmux_sessions_not_in_live_listing",
                    stale=sorted(stale),
                    note="reconciler should prune these on next startup",
                )

        return TmuxListing.answered(results, refused_rows=refused_rows)

    async def respawn(
        self, agent_command: Optional[str] = None
    ) -> RespawnResult:
        """Put a process back into this session's dead pane, in place.

        Description: the whole restart path. Probes the pane, runs the
            ladder in ``src.core.session_respawn`` to decide what to run,
            then ``tmux respawn-pane`` and a dead-on-arrival re-probe.

            NOTHING IS CREATED AND NOTHING IS DESTROYED. The tmux session,
            its ``#{session_created}`` epoch, its ``#{pane_id}``, its
            scrollback and its ``pipe-pane`` all survive (measured on tmux
            3.7c - see tests/test_tmux_respawn_real.py). That is what keeps
            the app's ``sessions`` row, project attribution, pinned theme,
            unread state and name attached to the same session: the
            instance triple this row is keyed on does not change, so no new
            row can be minted and no lineage/fork column is ever touched.

            ``-k`` IS DELIBERATELY NEVER PASSED. tmux refuses
            ``respawn-pane`` on a live pane without it, so a click on a row
            painted 'dead' that has since come back to life cannot kill a
            running agent. The ``not_dead`` branch below is the friendly
            message; tmux is the actual guarantee.

            IT ALSO NEVER KILLS THE SESSION ON FAILURE, which is where it
            departs from ``start()``. On create, tearing down a
            dead-on-arrival corpse frees the name for a retry. On restart
            the session IS the thing the user is trying to keep, so a
            failed respawn leaves the corpse, the scrollback and the row
            exactly as it found them and reports why.

            NO RETRY LOOP. An agent that crashes on startup comes back as
            ``ok=False`` naming its exit status and the first meaningful
            line it printed. The user clicks again or does not; this method
            never decides to.

        Inputs:
            agent_command: What the app would launch for this session's
                recorded ``agent_type``, or None when it has no record.
                Only CONSULTED when tmux confirms the pane had a start
                command at all - see the ladder's docstring for why
                ``agent_type`` alone is not admissible evidence.

        Output:
            RespawnResult: ``kind`` is the ladder verdict, ``ok`` says
                whether a process is running in the pane now, ``detail`` is
                one sentence fit to show the user.

        Example:
            >>> res = await backend.respawn(agent_command="cld")
            >>> res.kind, res.ok
            ('agent', True)
        """
        target = _safe_target(self.tmux_session)

        rc, out, _ = await self._run_tmux(
            "list-panes",
            "-t",
            self.tmux_session,
            "-F",
            RESPAWN_PANE_FORMAT,
            check=False,
        )
        decoded = out.decode("utf-8", errors="replace") if out else ""
        first_line = decoded.splitlines()[0] if decoded.strip() else ""
        probe_ok = rc == 0 and bool(first_line)

        pane_dead: Optional[str] = None
        start_command: Optional[str] = None
        if probe_ok:
            pane_dead, _dead_status, start_command = parse_respawn_probe(first_line)

        plan = resolve_respawn_plan(
            probe_ok=probe_ok,
            pane_dead=pane_dead,
            pane_start_command=start_command,
            agent_command=agent_command,
        )

        if not plan.actionable:
            logger.info(
                "tmux_respawn_refused",
                session=self.tmux_session,
                kind=plan.kind,
                detail=plan.detail,
            )
            return RespawnResult(kind=plan.kind, ok=False, detail=plan.detail)

        args: List[str] = ["respawn-pane", "-t", target]
        if plan.command:
            args.append(plan.command)

        rc_spawn, _, err_spawn = await self._run_tmux(*args, check=False)
        if rc_spawn != 0:
            stderr = err_spawn.decode("utf-8", errors="replace").strip()
            logger.error(
                "tmux_respawn_command_failed",
                session=self.tmux_session,
                kind=plan.kind,
                returncode=rc_spawn,
                stderr=stderr[:300],
            )
            return RespawnResult(
                kind=plan.kind,
                ok=False,
                detail=(
                    f"tmux refused to restart this pane: "
                    f"{stderr or 'no error text'}"
                ),
                command=plan.command,
            )

        # Same 250ms dead-on-arrival window ``start()`` uses. A binary that
        # is missing, unauthenticated, or misconfigured exits inside it, and
        # reporting THAT is what stops a one-click restart from looking like
        # a success that did nothing.
        await asyncio.sleep(0.25)

        rc_after, out_after, _ = await self._run_tmux(
            "list-panes",
            "-t",
            self.tmux_session,
            "-F",
            RESPAWN_PANE_FORMAT,
            check=False,
        )
        decoded_after = (
            out_after.decode("utf-8", errors="replace") if out_after else ""
        )
        if rc_after != 0 or not decoded_after.strip():
            # THIRD OUTCOME, on the verification rather than the plan. The
            # respawn command succeeded; we simply cannot see the result.
            # Reported as not-ok so nothing downstream renders an unmeasured
            # success, and SAID so rather than blamed on the agent.
            return RespawnResult(
                kind=plan.kind,
                ok=False,
                detail=(
                    "the restart command succeeded but tmux did not answer "
                    "when asked whether the pane came back, so whether it is "
                    "running cannot be determined"
                ),
                command=plan.command,
            )

        dead_after, status_after, _ = parse_respawn_probe(
            decoded_after.splitlines()[0]
        )
        if dead_after == "1":
            banner = await self._first_meaningful_pane_line(target)
            logger.error(
                "tmux_respawn_died_on_arrival",
                session=self.tmux_session,
                kind=plan.kind,
                pane_dead_status=status_after,
                capture=banner[:300],
            )
            reason = banner or f"exit status {status_after or 'unknown'}"
            return RespawnResult(
                kind=plan.kind,
                ok=False,
                detail=f"it started and exited again: {reason}",
                command=plan.command,
            )

        logger.info(
            "tmux_respawn_ok",
            session=self.tmux_session,
            kind=plan.kind,
        )
        return RespawnResult(
            kind=plan.kind, ok=True, detail=plan.detail, command=plan.command
        )

    async def _first_meaningful_pane_line(self, target: str) -> str:
        """Newest non-blank line of pane output, with escapes stripped.

        Description: a launch banner opens with cursor/colour escapes, so
            the raw capture is unreadable in an error surface. Coarse
            CSI/OSC strip - good enough for one line of diagnostics, and
            deliberately not a full terminal emulator.

        Inputs:
            target: an already ``_safe_target``-validated tmux target.

        Output:
            str: the first meaningful line found, or '' when the capture
                failed or held nothing but blanks. An empty string means
                "nothing to quote", and callers must fall back to the exit
                status rather than presenting it as a message.

        Example:
            >>> await backend._first_meaningful_pane_line("cloude_x")
            'command not found: claude'
        """
        rc, out, _ = await self._run_tmux(
            "capture-pane", "-t", target, "-p", "-S", "-200", check=False
        )
        if rc != 0:
            return ""
        for raw_line in reversed(out.decode("utf-8", errors="replace").splitlines()):
            cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", raw_line)
            cleaned = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", cleaned)
            cleaned = cleaned.strip()
            # tmux writes its own "Pane is dead (...)" footer into the
            # capture. Quoting that back at the user says nothing about
            # WHY, so it is skipped in favour of the agent's own output.
            if cleaned and not cleaned.startswith("Pane is dead"):
                return cleaned
        return ""

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
        key-name lookup - fastest path for regular typing. ``send-keys -H``
        delivers each 2-hex-digit argv token as a literal byte *as a key
        event* - the correct vehicle for keystrokes like Backspace (0x7f),
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
            # True paste - use bracketed paste so Claude distinguishes from typed input
            await self._write_via_paste_buffer(data)
        elif _has_control_chars(data):
            # Short keystroke with control bytes (arrow, Ctrl-X, Esc, Backspace, F-keys)
            await self._write_via_hex_keys(data)
        else:
            # Short plain text - fastest path
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

    @property
    def pid(self) -> Optional[int]:
        """The OS pid of pane 0's foreground process, or None if unknown.

        Description: Queries ``#{pane_pid}`` via ``list-panes`` for this
            backend's session/pane. Mirrors ``PTYBackend.pid`` so
            ``SessionManager`` can call ``getattr(backend, "pid", None)``
            uniformly across both backends. Unlike PTYBackend (which tracks
            a single forked pid for the life of the process), a tmux pane's
            foreground pid can change as commands run inside it - this
            always reflects the CURRENT foreground process, not the shell
            that was originally spawned.

        Inputs: none (reads ``self.tmux_session`` / ``self.socket_name``).

        Output:
            Optional[int]: The pane's current foreground pid, or None if
                the session is gone or the query fails.

        Example:
            >>> backend.pid
            48213
        """
        rc, out, _ = self._run_tmux_sync(
            "display-message",
            "-t",
            _safe_target(self.tmux_session),
            "-p",
            "#{pane_pid}",
            check=False,
        )
        if rc != 0:
            return None
        raw = out.decode("utf-8", errors="replace").strip()
        try:
            return int(raw)
        except ValueError:
            return None

    def list_pane_status_all(self) -> TmuxListing:
        """Bulk-query activity status for every pane on this tmux server.

        Description: One ``list-panes -a`` call across the WHOLE dedicated
            tmux server, so callers building a status view for many sessions
            (owned + external) pay a single subprocess cost instead of one
            query per session. Only pane 0 of each session is meaningful for
            this app (we never create a second window/pane), so when a
            session reports multiple panes we keep the first line tmux
            returns for that session name.

        Inputs: none (reads ``self.socket_name``).

        Output:
            TmuxListing: ``ok=True`` with one row per LIVE tmux session on
                the socket, each ``{"name": str, "pane_dead": str,
                "pane_current_command": str, "pid": Optional[int],
                "status": str}``. ``status`` is pre-resolved via
                ``resolve_pane_status()`` so callers never touch the raw
                fields unless they want to. ``ok=True, sessions=[],
                reason='no_server'`` when no server is running (a real
                answer of zero). ``ok=False`` when the probe could not
                run at all, in which case callers must fall back to
                ``STATUS_UNKNOWN`` rather than to "no panes".

        Example:
            >>> backend.list_pane_status_all().sessions
            [{'name': 'cloude_myproj', 'pane_dead': '0',
              'pane_current_command': 'claude', 'pid': 4821,
              'status': 'running'}]
        """
        # Local import avoids a module-level cycle: session_status has no
        # dependency back on tmux_backend, but keeping the import at the
        # call site matches this file's existing lazy-import convention
        # for settings (see _resolve_pipe_path).
        from src.core.session_status import resolve_pane_status

        failure, stdout_text = self._run_listing(
            "list-panes",
            "-a",
            "-F",
            "#{session_name}|#{pane_dead}|#{pane_current_command}|#{pane_pid}",
        )
        if failure is not None:
            return failure

        seen: set = set()
        results: List[Dict[str, Any]] = []
        # Same row-delimiter rule as list_attachable_sessions: the name
        # is caller-controlled and splitlines() would let it manufacture
        # extra rows. This format puts the name FIRST, so a forged row
        # here cannot be told from a real one by field position at all -
        # which makes using the correct row split the only defence.
        for line in split_listing_rows(stdout_text):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 4:
                logger.debug("list_pane_status_all_unparseable_row", raw=line)
                continue
            name, pane_dead, current_command, pid_raw = parts[0], parts[1], parts[2], parts[3]
            if name in seen:
                # Keep only the first pane per session (our sessions are
                # always single-window/single-pane; defensive for any
                # externally-created session with extra panes).
                continue
            seen.add(name)
            try:
                pid_val: Optional[int] = int(pid_raw)
            except ValueError:
                pid_val = None
            results.append({
                "name": name,
                "pane_dead": pane_dead,
                "pane_current_command": current_command,
                "pid": pid_val,
                "status": resolve_pane_status(pane_dead, current_command),
            })
        return TmuxListing.answered(results)

    async def rename_session(self, new_name: str) -> None:
        """Rename this tmux session in-place via ``rename-session``.

        Atomic from the tmux server's perspective - the session keeps its
        windows, panes, history, pipe-pane hooks, and remain-on-exit setting.
        Caller is responsible for upstream state re-keying (the SessionManager
        layer handles ``owned_tmux_sessions`` + ``pinned_themes`` + the
        per-session ``Session.tmux_session`` field).

        Args:
            new_name: New tmux session name. MUST be pre-validated by the
                caller (route layer enforces ``^[A-Za-z0-9_-]{1,64}$``). We
                still fail loudly via ``_safe_target`` if the value contains
                a target separator - defense in depth, never trust upstream.

        Raises:
            ValueError: ``new_name`` contains tmux target separators (``:``
                or ``.``).
            RuntimeError: tmux ``rename-session`` returned non-zero.
        """
        # Defense-in-depth: validate the new name as a tmux target. The
        # route layer enforces a stricter regex; this catches any code
        # path that bypasses it (test fixtures, future internal callers).
        _safe_target(new_name)

        if not self.tmux_session:
            raise RuntimeError("rename_session: backend has no tmux session name")

        if new_name == self.tmux_session:
            # No-op rename. Treat as success - the user's intent ("the
            # session should be named X") is already satisfied.
            return

        rc, _, err = await self._run_tmux(
            "rename-session",
            "-t",
            self.tmux_session,
            new_name,
            check=False,
        )
        if rc != 0:
            raise RuntimeError(
                f"tmux rename-session failed: "
                f"{err.decode('utf-8', errors='replace').strip()}"
            )

        logger.info(
            "tmux_backend_renamed",
            old=self.tmux_session,
            new=new_name,
            socket=self.socket_name,
        )
        self.tmux_session = new_name

    def discover_existing(self) -> TmuxListing:
        """List all ``cloude_*`` sessions on our dedicated socket.

        Description: The startup reconciler's only view of live tmux
            state. It PRUNES persisted ownership against this answer, so
            a wrong empty here is not a display bug - it silently
            destroys the user's ownership records. That is why the
            unavailable case must stay distinguishable from zero.

        Inputs: none (reads ``self.socket_name``).

        Output:
            TmuxListing: ``ok=True`` with ``sessions`` a list of
                ``cloude_``-prefixed session names (empty list plus
                ``reason='no_server'`` means genuinely none);
                ``ok=False`` when the probe could not run, in which case
                the caller MUST NOT prune or clear anything.

        Example:
            >>> backend.discover_existing().sessions
            ['cloude_myproj']
        """
        failure, stdout_text = self._run_listing(
            "list-sessions",
            "-F",
            "#{session_name}",
        )
        if failure is not None:
            return failure
        # The row-delimiter rule again, and it bites hardest here: this
        # listing feeds the owned-name reconciliation, so a name split
        # into two by splitlines() manufactures a second cloude_-prefixed
        # "session" that the reconciler would then act on. The whole row
        # IS the name in this format, so a name carrying a boundary
        # character now survives as one row and simply fails to match
        # anything, which is the honest outcome.
        names = split_listing_rows(stdout_text)
        return TmuxListing.answered(
            [n.strip() for n in names if n.strip().startswith(SESSION_PREFIX)]
        )

    def capture_scrollback(self, lines: int = 3000) -> bytes:
        """Capture the pane's recent scrollback as raw bytes (UTF-8).

        ``capture-pane -p`` writes to stdout. ``-S -<N>`` sets start line N
        lines above the cursor. ``-e`` preserves ANSI escape sequences
        so xterm.js can replay colors/positioning faithfully.
        ``-J`` joins hardware-wrapped lines back into logical lines, so xterm.js
        can re-wrap them cleanly at the browser viewport width. Without ``-J``,
        tmux emits each pane-width-wrapped visual line as a separate output line,
        and xterm's re-wrap conflicts with tmux's pane-width wraps, producing
        visually jumbled scrollback when the user scrolls above the live viewport.

        The bytes are passed through
        ``scrollback_replay.normalize_replay_newlines`` before they are
        returned. ``-p`` separates lines with a BARE LF, and the client's
        xterm runs ``convertEol: false`` (correctly - a live PTY sends
        ``\\r\\n`` and TUIs need a bare LF to mean "down one row, keep the
        column"). Replaying bare LFs therefore staircases every captured
        line to the column where the previous one ended, which is the
        "scrolling back janks the alignment" report. See that module.

        Returns:
            Captured pane bytes with CRLF line endings, or ``b""`` when
            the tmux call fails.
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
            return normalize_replay_newlines(out)
        finally:
            # Note: Item 7 will move this flag flip closer to the WS send
            # site (after bytes are written to the socket). For now we
            # clear it immediately - the callback-suppression is still a
            # future-Item-7 concern.
            self.replay_in_progress = False

    def pane_in_alternate_screen(self) -> bool:
        """Report whether pane 0 is on the alternate screen buffer.

        Returns:
            True when tmux reports ``#{alternate_on}`` as 1. False on any
            failure - the caller's fallback is the safer branch.
        """
        rc, out, _ = self._run_tmux_sync(
            "display-message",
            "-p",
            "-t",
            _safe_target(self.tmux_session),
            "#{alternate_on}",
            check=False,
        )
        if rc != 0:
            return False
        return out.decode("utf-8", errors="replace").strip() == "1"

    def session_age_seconds(self) -> Optional[float]:
        """How long this tmux session has existed, in seconds.

        Used by ``src/api/ws_startup_paint.py`` to tell a session that is
        merely young (nothing painted yet, perfectly normal) apart from
        one that has been alive for a while and has still produced no
        output at all - the signature of a shell startup script blocked
        on a prompt nobody can see.

        Returns:
            Age in seconds, or ``None`` when it cannot be determined -
            tmux failed, is not running, or returned an unparseable
            ``#{session_created}``. ``None`` is a distinct third outcome
            and must NOT be read as "young" or as "old"; the caller
            declines to make a claim.
        """
        rc, out, _ = self._run_tmux_sync(
            "display-message",
            "-p",
            "-t",
            _safe_target(self.tmux_session),
            "#{session_created}",
            check=False,
        )
        if rc != 0:
            return None
        try:
            created = int(out.decode("utf-8", errors="replace").strip())
        except ValueError:
            return None
        return max(0.0, time.time() - created)

    def capture_visible_screen(self) -> bytes:
        """Capture the pane's visible screen as a replayable byte stream.

        ``-S 0`` starts at the first line of the visible pane (history is
        addressed with negative numbers), so this is the viewport and
        nothing above it. ``-e`` keeps the ANSI attributes. We do NOT pass
        ``-J``: the scrollback path joins wrapped lines so the browser can
        re-wrap them, but here the pane was just resized to the client's
        exact geometry, so tmux's own line breaks are the correct ones and
        joining them would re-wrap content that is already right.

        Trailing blank lines are dropped so the cursor ends up immediately
        after the last real character. That is what makes an unterminated
        prompt ("password: ") look like a prompt rather than like text
        with the cursor parked below it.

        Returns:
            Bytes with CRLF line endings, or ``b""`` on failure.
        """
        rc, out, _ = self._run_tmux_sync(
            "capture-pane",
            "-p",
            "-e",
            "-S",
            "0",
            "-t",
            _safe_target(self.tmux_session),
            check=False,
        )
        if rc != 0:
            return b""
        body = out.rstrip(b"\r\n")
        if not body.strip():
            return b""
        return normalize_replay_newlines(body)

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
            #     scrollback painted. Bounded to actual file size -
            #     an offset larger than the file (shouldn't happen
            #     but defensive) degrades to SEEK_END.
            #   - Normal path (create / rehydrate): SEEK_END - we
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
                    # Fall back to EOF - no worse than normal rehydrate.
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
                # Single-use - clear so subsequent fd reopens (rotation)
                # use SEEK_END like the normal path.
                self._adopt_tail_start_offset = None
            else:
                try:
                    os.lseek(fd, 0, os.SEEK_END)
                except OSError:
                    pass

            while self._running:
                # Rotation check - once a second is plenty.
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
        hook fires - no tmux restart needed. We re-point our read fd at the
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
