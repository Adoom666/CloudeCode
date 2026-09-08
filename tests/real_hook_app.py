"""The app under test, the pane it launches, and the two ways to touch them.

Split out of :mod:`tests.real_hook_harness` so neither file grows past the
500-line guideline. The primitives (the opt-in gate, the named skips, the
hook ledger, the node bridge to ``ledStateFor``) live there; the thing that
stands a server up and drives a real agent lives here.

Read that module's header first - it carries the five measurements that
shaped this design, including why the trust dialog is answered rather than
pre-seeded and why ``CLAUDE_CONFIG_DIR`` cannot be used.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from tests.real_hook_harness import HookLedger, free_port

#: Path the hook one-liner POSTs to, relative to the app root.
HOOK_PATH: str = "/api/v1/hooks/claude-event"

#: Keystrokes, as the browser would send them over the terminal socket.
KEY_DOWN: bytes = b"\x1b[B"
KEY_ENTER: bytes = b"\r"

#: Birth geometry the harness negotiates, so the TUI has room to render
#: the dialogs this file reads back out of the pane.
TERMINAL_COLS: int = 200
TERMINAL_ROWS: int = 50

#: How long to let the server finish its resize handshake and repaint
#: before the first keystroke. Its own mid-handshake sleep is 150ms.
HANDSHAKE_SETTLE_SECONDS: float = 1.5


class RealHookApp:
    """The app under test, listening on a real port, with one real session.

    Description: a REAL ``SessionManager`` behind the REAL
      ``src.api.routes`` router and the REAL ``/ws/terminal`` socket,
      served by uvicorn on loopback. Nothing in the path under test is
      stubbed, and ``require_auth`` is deliberately NOT overridden: the
      hook route has no JWT and ``/sessions/list`` does, and both facts
      are part of what is being measured.

      It does NOT import ``src.main.app``. That app's lifespan runs a
      config migration, a database migration, a first-run import and a
      boot re-adopt over every visible tmux session, against paths that
      would have to be redirected one by one - a much larger blast radius
      than the question under test, and "do not touch the live app or
      database" is not a thing to get 90 percent right.
    Inputs: none at construction; use as a context manager.
    Output: an object exposing ``session_id``, ``ledger``, ``row()``,
      ``tmux()``, ``pane_tail()``, ``send_keys()``, ``view()`` and
      ``kill_agent()``.
    """

    def __init__(self) -> None:
        self.port: int = free_port()
        self.ledger: HookLedger = HookLedger()
        self.session_id: str = ""
        self.tmux_name: str = ""
        self._tmpdirs: list[Path] = []
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None
        self._token: str = ""
        self._httpx: Any = None
        self.work_dir: Path = Path()
        self.settings_file: Path = Path()

    # -- lifecycle ---------------------------------------------------- #

    def _mkdtemp(self, prefix: str) -> Path:
        path = Path(tempfile.mkdtemp(prefix=prefix))
        self._tmpdirs.append(path)
        return path

    def _write_claude_settings(self) -> Path:
        """Write this run's ``--settings`` file: our hooks, plus an ask rule.

        Description: the hook block comes from the PRODUCTION builder, so
          the one-liner under test cannot drift from the shipped one. The
          ``permissions.ask`` rule is what makes ``PermissionRequest``
          deterministic - measured, ``date +%s`` is on claude's own
          allowlist and raises no prompt at all.
        Inputs: none. Output: Path to the settings JSON.
        """
        from src.core.claude_hooks import _build_hook_block

        settings_dir = self._mkdtemp("cc_rht_settings_")
        path = settings_dir / "real-hook-test-settings.json"
        path.write_text(
            json.dumps(
                {
                    "hooks": _build_hook_block(),
                    "permissions": {"ask": ["Bash"], "allow": [], "deny": []},
                },
                indent=2,
            )
        )
        return path

    def _write_app_config(self, claude_command: str) -> Path:
        """Write the throwaway ``config.json`` this run's Settings reads.

        Description: no wrappers, one explicit ``agents.claude_command``.
          That is the documented rung 3 of ``Settings.get_agent_command``,
          so the launch still goes through the real resolver and the real
          ``~/.zshrc``-sourcing render rather than around them.
        Inputs: claude_command (str) - the command the pane runs.
        Output: Path to the config file.
        """
        config_dir = self._mkdtemp("cc_rht_config_")
        path = config_dir / "config.json"
        path.write_text(
            json.dumps({"agents": {"claude_command": claude_command, "wrappers": []}})
        )
        return path

    def __enter__(self) -> "RealHookApp":
        import httpx
        import uvicorn
        from fastapi import FastAPI

        from src.api.auth import create_access_token
        from src.api.routes import router as api_router
        from src.api.websocket import router as ws_router
        from src.config import settings
        from src.core.local_servers import LocalServersTracker
        from src.core.log_monitor import LogMonitor
        from src.core.session_manager import SessionManager

        self.work_dir = self._mkdtemp("cc_rht_work_")
        self.settings_file = self._write_claude_settings()
        claude_bin = shutil.which("claude") or "claude"
        command = (
            f"{claude_bin} --permission-mode default "
            f"--settings {self.settings_file}"
        )

        settings._auth_config_cache = None
        self._prev_config_file = settings.auth_config_file
        self._prev_port = settings.port
        settings.auth_config_file = str(self._write_app_config(command))
        # get_env_for_spawn builds CLOUDECODE_HOOK_URL from settings.port at
        # spawn time, so this must be the real listening port BEFORE the
        # session is created or the pane's curl aims at nothing.
        settings.port = self.port

        # Create the throwaway cloude.db so the run exercises the NORMAL
        # persistence path. Without it every create logs
        # ``create_persist_no_datastore`` and the session's ownership
        # rests on the degraded in-memory tier - a different code path
        # from the one a user is on, and therefore the wrong one to
        # measure a status light against.
        from src.core.db_migration import ensure_db_migrated

        ensure_db_migrated(settings.get_state_dir(), None, None)

        app = FastAPI()

        @app.middleware("http")
        async def _observe_hooks(request, call_next):  # noqa: ANN001, ANN202
            """Record hook POSTs without touching the request body."""
            is_hook = request.url.path.endswith(HOOK_PATH)
            event = request.headers.get("X-Cloudecode-Event", "") if is_hook else ""
            sid = request.headers.get("X-Cloudecode-Session", "") if is_hook else ""
            response = await call_next(request)
            if is_hook:
                self.ledger.record(event, sid, response.status_code)
            return response

        app.include_router(api_router, prefix="/api/v1")
        app.include_router(ws_router)
        manager = SessionManager()
        app.state.session_manager = manager
        # The terminal socket reads all three off app.state. Leaving
        # log_monitor out closed every WS with an AttributeError the client
        # only saw as "no close frame received", which is exactly the kind
        # of failure that looks like the feature and is the harness.
        app.state.log_monitor = LogMonitor(manager)

        started = threading.Event()

        @app.on_event("startup")
        async def _startup() -> None:
            """Build the loop-bound tracker the routes read off app.state."""
            import asyncio

            app.state.local_servers = LocalServersTracker(
                loop=asyncio.get_running_loop()
            )
            started.set()

        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        deadline = time.monotonic() + 20
        while not (self._server.started and started.is_set()):
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "the test server did not start, so NOTHING was measured"
                )
            time.sleep(0.05)

        self._token, _ = create_access_token()
        self._httpx = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=20.0,
        )
        self.app = app
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.tmux_name:
                self.tmux("kill-session", "-t", self.tmux_name)
        finally:
            if self._httpx is not None:
                self._httpx.close()
            if self._server is not None:
                self._server.should_exit = True
            if self._thread is not None:
                self._thread.join(timeout=15)
            from src.config import settings

            settings.auth_config_file = self._prev_config_file
            settings.port = self._prev_port
            settings._auth_config_cache = None
            for path in self._tmpdirs:
                shutil.rmtree(path, ignore_errors=True)

    # -- creating the session through the app's own path --------------- #

    def create_session(self, label: str = "real hook led test") -> str:
        """Create the session through ``POST /api/v1/sessions``.

        Description: the app's own create path, so the pane is born with
          the real ``CLOUDECODE_SESSION_ID`` / ``CLOUDECODE_HOOK_TOKEN`` /
          ``CLOUDECODE_HOOK_URL`` trio that ``get_env_for_spawn`` injects.
          Minting those by hand here would test a pane this app never
          built.
        Inputs: label (str) - the session's birth name.
        Output: str - the new session id, also stored on ``self``.
        Raises: RuntimeError when the create is refused, carrying the body.
        """
        resp = self._httpx.post(
            "/api/v1/sessions",
            json={
                "working_dir": str(self.work_dir),
                "auto_start_claude": True,
                "label": label,
            },
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"create refused with {resp.status_code}: {resp.text[:400]}"
            )
        body = resp.json()
        self.session_id = body["id"]
        # The create response's ``tmux_session`` can be empty - the name is
        # settled by the backend, not by the request - so the pane is
        # addressed through whatever the LIST row reports rather than
        # through a value that happened to be filled in at 201 time.
        self.tmux_name = body.get("tmux_session") or self.resolve_tmux_name()
        return self.session_id

    def resolve_tmux_name(self) -> str:
        """Read this session's tmux name off its ``/sessions/list`` row.

        Description: WRAPPER level - ``tmux_session`` sits beside
          ``activity_status``, not inside ``.session``. Caches on first
          success so a dead-pane assertion can still address the pane
          after the row stops reporting it.
        Inputs: none.
        Output: str - empty when the row cannot be read.
        """
        if self.tmux_name:
            return self.tmux_name
        row = self.row() or {}
        self.tmux_name = row.get("tmux_session") or ""
        return self.tmux_name

    # -- reading what the user would see ------------------------------- #

    def row(self) -> Optional[dict[str, Any]]:
        """Return this session's ``/sessions/list`` entry, wrapper level.

        Description: THE WRAPPER, not ``.session``. ``activity_status``,
          ``unread`` and ``startup_gate`` all sit on the outer object and
          reading them off the nested model returns ``None`` silently -
          gotcha 1, the single most repeated bug in this project.
        Inputs: none.
        Output: Optional[dict] - None when the session is not listed.
        """
        resp = self._httpx.get("/api/v1/sessions/list")
        if resp.status_code != 200:
            return None
        for info in resp.json():
            if (info.get("session") or {}).get("id") == self.session_id:
                return info
        return None

    def signals(self) -> dict[str, Any]:
        """The three LED inputs, exactly as the row carries them.

        Inputs: none.
        Output: dict with ``activity_status``, ``unread``, ``startup_gate``.
        """
        row = self.row() or {}
        return {
            "activity_status": row.get("activity_status"),
            "unread": row.get("unread"),
            "startup_gate": row.get("startup_gate"),
        }

    # -- tmux, on this run's socket only ------------------------------- #

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        """Run one tmux command against this run's guarded test socket.

        Inputs: args (str) - the argv after ``-L <test socket>``.
        Output: CompletedProcess with text output captured.
        """
        from tests.socket_guard import TEST_SOCKET_NAME

        return subprocess.run(
            ["tmux", "-L", TEST_SOCKET_NAME, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def pane_tail(self, lines: int = 20) -> str:
        """Last non-blank lines of the pane, for a failure message.

        Inputs: lines (int).
        Output: str - empty when the pane cannot be captured.
        """
        name = self.resolve_tmux_name()
        if not name:
            return ""
        out = self.tmux("capture-pane", "-p", "-t", name).stdout
        return "\n".join([ln for ln in out.splitlines() if ln.strip()][-lines:])

    def agent_pid(self) -> Optional[int]:
        """The pane's process id, or None when tmux does not answer.

        Inputs: none. Output: Optional[int].
        """
        name = self.resolve_tmux_name()
        if not name:
            return None
        out = self.tmux(
            "list-panes", "-t", name, "-F", "#{pane_pid}"
        ).stdout.strip()
        return int(out.splitlines()[0]) if out.strip().isdigit() else None

    def kill_agent(self) -> None:
        """SIGKILL the pane's process, leaving a DEAD pane behind.

        Description: the backend sets ``remain-on-exit on``, so killing
          the process is what makes ``#{pane_dead}`` true - the only
          signal that can report ``dead``. Killing the SESSION instead
          would remove the row rather than change its status, which
          measures nothing.
        Inputs: none. Output: None.
        """
        pid = self.agent_pid()
        if pid is not None:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, check=False)

    # -- input, over the app's own terminal socket --------------------- #

    def _ws_url(self) -> str:
        return (
            f"ws://127.0.0.1:{self.port}/ws/terminal"
            f"?session_id={self.session_id}"
        )

    def view(self) -> None:
        """Bind a WS terminal and drop it - the app's own mark-viewed path.

        Description: ``websocket_terminal`` calls
          ``mark_session_viewed`` on bind, and that is the ONLY thing that
          clears the auto-unread flag a ``Stop`` hook set. Calling the
          manager method directly would skip the seam under test.
        Inputs: none. Output: None.
        """
        self.send_keys()

    def send_keys(self, *payloads: bytes, settle: float = 0.4) -> None:
        """Send keystrokes through the app's real terminal WebSocket.

        Description: binary frames on ``/ws/terminal`` land in
          ``SessionManager.send_input`` and then in ``TmuxBackend.write``,
          which is what the browser does.

          THE RESIZE HANDSHAKE COMES FIRST, and skipping it is not a
          shortcut - it silently loses every keystroke. ``websocket_terminal``
          opens with a bounded window in which it waits for the client's
          ``pty_resize``, and it DROPS any binary frame that arrives before
          that ("the user can't have typed anything yet"). A client that
          connects, types and hangs up inside that window is answered with
          ``ws_handshake_error`` and the pane never moves - which reads
          exactly like a broken hook pipeline. Measured: three runs of this
          file sat on the trust dialog for the full timeout for this reason
          alone.

          The socket is closed again on the way out so that a later
          ``Stop`` lands with NO client viewing; a held-open socket would
          keep clearing unread and ``finished_unread`` could never be
          reached.
        Inputs: payloads (bytes) - sent in order; settle (float) - seconds
          to pause between them, since a TUI menu needs a beat.
        Output: None.
        """
        import json as _json

        from websockets.sync.client import connect

        from src.api.deps import SUBPROTOCOL_MARKER

        with connect(
            self._ws_url(),
            subprotocols=[SUBPROTOCOL_MARKER, self._token],
            open_timeout=15,
            max_size=None,
        ) as ws:
            ws.send(
                _json.dumps(
                    {"type": "pty_resize", "cols": TERMINAL_COLS, "rows": TERMINAL_ROWS}
                )
            )
            # The server repaints the pane after the resize and sleeps
            # 150ms mid-handshake; draining until it goes quiet is what
            # tells us the input loop is actually running.
            self._drain(ws, HANDSHAKE_SETTLE_SECONDS)
            for payload in payloads:
                ws.send(payload)
                time.sleep(settle)
                self._drain(ws, 0.1)
            self._drain(ws, 0.3)

    @staticmethod
    def _drain(ws: Any, seconds: float) -> None:
        """Read and discard whatever the server sends for a while.

        Description: the terminal socket pushes scrollback and paint
          frames unprompted. Never reading them would let the connection
          stall behind a full buffer, and this harness cares about none
          of the content.
        Inputs: ws - an open sync websocket; seconds (float) - how long.
        Output: None.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                ws.recv(timeout=0.05)
            except Exception:  # noqa: BLE001 - a quiet socket is the normal case
                time.sleep(0.02)

    def type_prompt(self, text: str) -> None:
        """Type a prompt into the agent and submit it.

        Inputs: text (str) - the prompt, sent without a trailing newline
          before a separate Enter, because claude's TUI needs the beat.
        Output: None.
        """
        self.send_keys(text.encode("utf-8"), KEY_ENTER, settle=0.6)
