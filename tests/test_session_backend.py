"""Tests for src.core.session_backend + TmuxBackend + PTYBackend.

Run with:
    python3 -m pytest tests/test_session_backend.py -v
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from unittest import mock

import pytest


# ---- minimal env bootstrap so `src.config` import succeeds --------------
# The pydantic Settings loader raises + sys.exit(1) if DEFAULT_WORKING_DIR
# or LOG_DIRECTORY are missing. Inject safe defaults before any imports.

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_tests_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_tests_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.session_backend import SessionBackend, build_backend
from src.core.tmux_backend import (
    INITIAL_COLS,
    INITIAL_ROWS,
    TmuxBackend,
    _has_control_chars,
    _slugify,
)
from src.utils.pty_session import PTYBackend
from src.core import tmux_discovery
from tests.socket_guard import TEST_SOCKET_NAME


TMUX_AVAILABLE = shutil.which("tmux") is not None
requires_tmux = pytest.mark.skipif(not TMUX_AVAILABLE, reason="tmux not on PATH")


# ---- slug sanitizer ------------------------------------------------------


def test_slugify_replaces_dots():
    assert "." not in _slugify("my.project.name")
    assert _slugify("my.project") == "my_project"


def test_slugify_handles_mixed_invalid_chars():
    assert _slugify("hello world:foo/bar.baz") == "hello_world_foo_bar_baz"


def test_slugify_preserves_alphanumeric_and_dash_underscore():
    assert _slugify("My-Session_42") == "My-Session_42"


def test_slugify_empty_input_returns_default():
    assert _slugify("") == "default"
    assert _slugify("...") == "default"


# ---- control-char classifier --------------------------------------------


def test_has_control_chars_flags_etx():
    assert _has_control_chars(b"\x03hello")


def test_has_control_chars_flags_escape():
    assert _has_control_chars(b"\x1b[A")


def test_has_control_chars_ignores_whitespace():
    assert not _has_control_chars(b"hello\tworld\n")
    assert not _has_control_chars(b"line1\r\nline2")


def test_has_control_chars_flags_del():
    assert _has_control_chars(b"foo\x7fbar")


# ---- PTYBackend conforms to the ABC --------------------------------------


def test_pty_backend_is_session_backend():
    backend = PTYBackend(
        session_id="test",
        working_dir=Path.home(),
        on_output=None,
    )
    assert isinstance(backend, SessionBackend)


def test_pty_backend_discover_existing_is_empty():
    """A PTY backend has a KNOWN zero, not an unknown - ok must be True."""
    backend = PTYBackend("test", Path.home(), None)
    listing = backend.discover_existing()
    assert listing.ok is True
    assert listing.sessions == []
    assert listing.reason == "not_applicable"


def test_pty_backend_capture_scrollback_is_empty():
    backend = PTYBackend("test", Path.home(), None)
    assert backend.capture_scrollback() == b""


def test_pty_backend_required_methods_are_present():
    """Every abstract method in SessionBackend must be concretely implemented."""
    backend = PTYBackend("test", Path.home(), None)
    for name in (
        "start",
        "stop",
        "write",
        "resize",
        "is_alive",
        "read_async",
        "discover_existing",
        "capture_scrollback",
    ):
        assert callable(getattr(backend, name)), f"missing {name}"


# ---- build_backend factory ----------------------------------------------


def test_build_backend_falls_back_to_pty_when_tmux_missing():
    # PATCH POINT MOVED. tmux discovery is no longer a bare
    # ``shutil.which`` in this module: it is
    # ``src.core.tmux_discovery``, which searches PATH *and* the
    # well-known absolute install locations (a GUI-launched app has no
    # shell PATH) and then EXECUTES ``tmux -V`` to prove the binary
    # runs. Patching shutil.which here would no longer simulate a
    # missing tmux - it would simulate nothing at all and the test
    # would pass or fail for unrelated reasons. Patch the resolver, and
    # clear the memoized probe so the fake is not read from cache.
    tmux_discovery.reset_probe_cache()
    with mock.patch(
        "src.core.tmux_discovery.resolve_tmux_path", return_value=None
    ):
        backend = build_backend(
            settings_obj=None,
            session_id="fallback-test",
            working_dir=Path.home(),
            on_output=None,
        )
        assert isinstance(backend, PTYBackend), (
            f"expected PTYBackend fallback, got {type(backend).__name__}"
        )


def test_build_backend_force_pty_even_when_tmux_present():
    """A Settings-like object forcing 'pty' must skip tmux."""
    class StubSettings:
        def load_auth_config(self):
            class AC:
                class session:
                    backend = "pty"
                    tmux_socket_name = _TEST_SOCKET
                    scrollback_lines = 3000
            return AC()

    backend = build_backend(
        settings_obj=StubSettings(),
        session_id="forced-pty",
        working_dir=Path.home(),
        on_output=None,
    )
    assert isinstance(backend, PTYBackend)


@requires_tmux
def test_build_backend_auto_picks_tmux_when_available():
    backend = build_backend(
        settings_obj=None,
        session_id="auto-pick",
        working_dir=Path.home(),
        on_output=None,
    )
    assert isinstance(backend, TmuxBackend)


@requires_tmux
def test_build_backend_honors_configured_socket_name():
    """REGRESSION (fix/backend-socket-name): ``build_backend`` used to
    construct ``TmuxBackend`` without ``socket_name`` at all, so every
    session silently landed on the hardcoded ``DEFAULT_SOCKET_NAME``
    ("cloude") no matter what ``AuthConfig.session.tmux_socket_name``
    said. This locks in that a configured non-default socket name is
    actually threaded through to the backend instance.

    Uses a unique, never-``cloude`` socket name so this test can never
    touch the app's real shared socket even if the fix regresses.
    """
    configured = f"cloude_regress_{uuid.uuid4().hex[:8]}"
    assert configured != "cloude"

    class StubSettings:
        def load_auth_config(self):
            class AC:
                class session:
                    backend = "tmux"
                    tmux_socket_name = configured
                    scrollback_lines = 100

            return AC()

    backend = build_backend(
        settings_obj=StubSettings(),
        session_id=f"socket-honor-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
    )
    assert isinstance(backend, TmuxBackend)
    assert backend.socket_name == configured, (
        f"build_backend must honor the configured tmux_socket_name; "
        f"expected {configured!r}, got {backend.socket_name!r}"
    )


@requires_tmux
def test_build_backend_defaults_to_cloude_socket_with_no_settings():
    """Counterpart to the regression test above: with no settings object
    (or a settings object whose config lookup fails), the default path
    MUST be unchanged — sessions still land on ``DEFAULT_SOCKET_NAME``
    ("cloude"). This is the socket the user's live sessions already run
    on; a regression here would strand them.
    """
    from src.core.tmux_backend import DEFAULT_SOCKET_NAME
    from tests.socket_guard import shipped_default_socket_name

    backend = build_backend(
        settings_obj=None,
        session_id=f"socket-default-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
    )
    assert isinstance(backend, TmuxBackend)
    # Behaviour: with no settings, the backend takes the module default.
    assert backend.socket_name == DEFAULT_SOCKET_NAME

    # Shipped value: read from source, because the in-process constant is
    # deliberately redirected to a throwaway socket while tests run. This
    # keeps the "the app ships 'cloude'" assertion honest without letting
    # the suite execute anything against that socket.
    assert shipped_default_socket_name() == "cloude"


@requires_tmux
def test_build_backend_create_and_adopt_agree_on_socket(tmux_socket_cleanup):
    """A session created via ``build_backend`` on a configured socket must
    be adoptable AND destroyable on that SAME socket — the split-brain
    the bug caused: create silently used ``cloude`` while the adopt path
    (``SessionManager._adopt_external_session``, session_manager.py:2762)
    already read the configured value, so an adopted-by-name lookup could
    never find a session that create had actually placed elsewhere.

    Runs entirely on ``tmux_socket_cleanup``'s unique per-test socket —
    never the real ``cloude`` socket.
    """

    class StubSettings:
        def load_auth_config(self):
            class AC:
                class session:
                    backend = "tmux"
                    tmux_socket_name = tmux_socket_cleanup
                    scrollback_lines = 100

            return AC()

    settings_obj = StubSettings()
    created = build_backend(
        settings_obj=settings_obj,
        session_id=f"agree-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
        session_name=f"cloude_agree_{uuid.uuid4().hex[:6]}",
    )
    try:
        asyncio.run(created.start())

        adopt_socket = settings_obj.load_auth_config().session.tmux_socket_name
        assert created.socket_name == adopt_socket, (
            "create and adopt resolved DIFFERENT sockets — split-brain"
        )

        adopted = TmuxBackend.for_external(
            session_name=created.tmux_session,
            working_dir=Path.home(),
            on_output=None,
            socket_name=adopt_socket,
        )
        asyncio.run(adopted.attach_existing(needs_pipe_setup=True))
        assert adopted.is_alive(), (
            "adopt could not find the session create just started on "
            "the same configured socket"
        )
        asyncio.run(adopted.stop())
    finally:
        asyncio.run(created.stop())


# ---- TmuxBackend integration (requires tmux) ----------------------------


# The one socket this suite may touch. Owned by tests/socket_guard.py, which
# also blocks, at the subprocess layer, any tmux invocation aimed elsewhere.
_TEST_SOCKET = TEST_SOCKET_NAME


@pytest.fixture
def tmux_socket_cleanup():
    """Kill the test tmux server after each test, regardless of outcome."""
    yield _TEST_SOCKET
    try:
        import subprocess
        subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "kill-server"],
            capture_output=True,
            check=False,
        )
    except Exception:
        pass


@requires_tmux
def test_tmux_backend_discover_existing_finds_created_session(tmux_socket_cleanup):
    """After start(), discover_existing() must include this backend's session name."""
    backend = TmuxBackend(
        session_id=f"discovery-test-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    try:
        asyncio.run(backend.start())
        listing = backend.discover_existing()
        assert listing.ok is True
        names = listing.sessions
        assert backend.tmux_session in names, (
            f"expected {backend.tmux_session} in {names}"
        )
        assert backend.is_alive()
    finally:
        asyncio.run(backend.stop())


@requires_tmux
def test_tmux_backend_write_large_paste_delivers_content_to_pane(tmux_socket_cleanup):
    """Large payloads (>PASTE_THRESHOLD_BYTES) route through the paste path
    (load-buffer + paste-buffer -d -p) and the content reaches the pane.

    tmux's own emulator consumes the bracketed-paste markers (\\x1b[200~ /
    \\x1b[201~) before they appear in `capture-pane` output, so we can't
    assert on the markers here — that contract is locked by the mocked argv
    test ``test_tmux_backend_write_large_payload_uses_load_and_paste_buffer``.
    What we CAN verify end-to-end is that a large payload actually reaches
    the pane (the paste path is wired up correctly and doesn't swallow the
    bytes).
    """
    backend = TmuxBackend(
        session_id=f"bigpaste-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )

    async def _inner():
        # Use /bin/cat as the pane's first process — no prompt, echoes stdin.
        await backend.start(command="/bin/cat")
        # Give cat time to start up.
        await asyncio.sleep(0.3)

        payload = b"abc" * 700  # 2100 bytes, > PASTE_THRESHOLD_BYTES → paste path
        await backend.write(payload)

        # Let the paste buffer flush through the pane.
        await asyncio.sleep(0.8)

        cap = backend.capture_scrollback(lines=500)
        text = cap.decode("utf-8", errors="replace")

        # `cat` echoes whatever lands on stdin, so we should see 'abc' in
        # the pane. If paste-buffer had failed, the pane would be empty.
        assert "abc" in text, (
            f"expected 'abc' in capture, got {len(cap)} bytes: {text[:200]!r}"
        )
        assert backend.is_alive()

    try:
        asyncio.run(_inner())
    finally:
        asyncio.run(backend.stop())


# ---- write() path-routing argv assertions (mocked) -----------------------
#
# These tests pin the argv shape that TmuxBackend.write() feeds to tmux for
# each of the three routing paths. Mocking `_run_tmux` avoids needing a real
# tmux server and makes the contract with tmux explicit — if someone ever
# "simplifies" the routing back into a two-path world, these tests scream.


def _make_backend_for_write_tests() -> TmuxBackend:
    """Build a backend and mark it running without touching tmux."""
    backend = TmuxBackend(
        session_id=f"write-routing-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
    )
    backend._running = True  # bypass start()
    return backend


def _patch_run_tmux(backend: TmuxBackend):
    """Return an AsyncMock bound to backend._run_tmux that records argv.

    Each call is appended to `calls` as the positional `args` tuple (which
    is the argv AFTER the ``tmux -L <socket>`` prefix — exactly the contract
    we care about for routing correctness).
    """
    calls: list[tuple] = []

    async def fake(*args, stdin_bytes=None, check=True):
        calls.append(args)
        return 0, b"", b""

    backend._run_tmux = fake  # type: ignore[assignment]
    return calls


def test_tmux_backend_write_plain_text_uses_send_keys_l():
    """Plain ASCII text → send-keys -l <text> (fast path)."""
    backend = _make_backend_for_write_tests()
    calls = _patch_run_tmux(backend)

    asyncio.run(backend.write(b"hello"))

    assert len(calls) == 1, f"expected exactly 1 tmux call, got {len(calls)}"
    argv = calls[0]
    assert argv[0] == "send-keys"
    assert argv[1] == "-l"
    assert argv[2] == "-t"
    # Bare session name, not "<session>:0.0" — see _safe_target: a
    # hardcoded window 0 breaks on any tmux.conf with base-index 1.
    assert argv[3] == backend.tmux_session
    assert argv[4] == "hello"


def test_tmux_backend_write_backspace_uses_hex_keys():
    """Single-byte Backspace (0x7f) → send-keys -H 7f."""
    backend = _make_backend_for_write_tests()
    calls = _patch_run_tmux(backend)

    asyncio.run(backend.write(b"\x7f"))

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "send-keys"
    assert argv[1] == "-H"
    assert argv[2] == "-t"
    # Bare session name, not "<session>:0.0" — see _safe_target: a
    # hardcoded window 0 breaks on any tmux.conf with base-index 1.
    assert argv[3] == backend.tmux_session
    # Hex pair is the ONLY trailing argv — one byte, one pair.
    assert argv[4:] == ("7f",), f"expected hex pair '7f', got trailing {argv[4:]}"


def test_tmux_backend_write_escape_single_byte():
    """Single-byte Escape (0x1b) → send-keys -H 1b (no bracketed-paste wrap)."""
    backend = _make_backend_for_write_tests()
    calls = _patch_run_tmux(backend)

    asyncio.run(backend.write(b"\x1b"))

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "send-keys"
    assert argv[1] == "-H"
    assert argv[4:] == ("1b",)


def test_tmux_backend_write_arrow_sequence_no_paste_wrap():
    """Up arrow (\\x1b[A) → send-keys -H 1b 5b 41 (three hex pairs, no paste path)."""
    backend = _make_backend_for_write_tests()
    calls = _patch_run_tmux(backend)

    asyncio.run(backend.write(b"\x1b[A"))

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "send-keys"
    assert argv[1] == "-H"
    # Three hex pairs, one per byte: \x1b -> 1b, '[' -> 5b, 'A' -> 41
    assert argv[4:] == ("1b", "5b", "41"), f"expected 1b 5b 41, got {argv[4:]}"


def test_tmux_backend_write_ctrl_c_uses_hex_keys():
    """Ctrl+C (0x03) → send-keys -H 03 (keystroke path, NOT paste)."""
    backend = _make_backend_for_write_tests()
    calls = _patch_run_tmux(backend)

    asyncio.run(backend.write(b"\x03"))

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "send-keys"
    assert argv[1] == "-H"
    assert argv[4:] == ("03",)


def test_tmux_backend_write_shift_tab_uses_hex_keys():
    """Shift+Tab (\\x1b[Z) → send-keys -H 1b 5b 5a."""
    backend = _make_backend_for_write_tests()
    calls = _patch_run_tmux(backend)

    asyncio.run(backend.write(b"\x1b[Z"))

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "send-keys"
    assert argv[1] == "-H"
    assert argv[4:] == ("1b", "5b", "5a")


def test_tmux_backend_write_large_payload_uses_load_and_paste_buffer():
    """Large payloads (>PASTE_THRESHOLD_BYTES) → load-buffer then paste-buffer -d -p."""
    backend = _make_backend_for_write_tests()
    calls = _patch_run_tmux(backend)

    # 500 bytes of plain ASCII — no control chars, but over the threshold.
    payload = b"A" * 500
    asyncio.run(backend.write(payload))

    assert len(calls) == 2, f"paste path should issue two tmux calls, got {len(calls)}"
    load_argv, paste_argv = calls
    assert load_argv[0] == "load-buffer"
    assert "-b" in load_argv
    assert load_argv[-1] == "-"  # read stdin
    assert paste_argv[0] == "paste-buffer"
    assert "-d" in paste_argv  # delete buffer after paste
    assert "-p" in paste_argv  # bracketed paste
    assert "-b" in paste_argv
    assert "-t" in paste_argv


def test_tmux_backend_write_empty_is_noop():
    """Empty payload must NOT invoke tmux at all."""
    backend = _make_backend_for_write_tests()
    calls = _patch_run_tmux(backend)

    asyncio.run(backend.write(b""))

    assert calls == [], f"empty write must be a no-op, got {calls}"


def test_tmux_backend_write_raises_when_not_running():
    """write() before start() must raise (guard against ordering bugs)."""
    backend = TmuxBackend(
        session_id=f"not-running-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
    )
    # _running defaults to False.
    with pytest.raises(RuntimeError, match="not running"):
        asyncio.run(backend.write(b"hello"))


@requires_tmux
def test_tmux_backend_is_alive_lifecycle(tmux_socket_cleanup):
    """is_alive() flips False → True → False across start/stop."""
    backend = TmuxBackend(
        session_id=f"alive-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    assert not backend.is_alive()
    try:
        asyncio.run(backend.start())
        assert backend.is_alive()
    finally:
        asyncio.run(backend.stop())
    # Give tmux a beat to reap the session.
    time.sleep(0.2)
    assert not backend.is_alive()


@requires_tmux
@pytest.mark.asyncio
async def test_tmux_backend_resize_actually_resizes_window(tmux_socket_cleanup):
    """After resize(), `tmux display-message -p '#{window_width}'` should match.

    Regression test for the headless-sizing bug: TmuxBackend used to create
    sessions without `-x/-y` and call `refresh-client -C` (which requires an
    attached client) to resize. Without an attached client the window stayed
    at its 80x24 birth size forever, so Claude CLI rendered its TUI at 80x24
    while the xterm.js client drew at the real browser geometry.

    Fix: birth the window at INITIAL_COLS x INITIAL_ROWS, set
    `window-size manual`, and use `resize-window -x -y` (server-side, works
    with zero clients) for subsequent resizes. This test locks that in.
    """
    slug = f"resize_test_{secrets.token_hex(4)}"
    wd = Path(tempfile.mkdtemp(prefix="cc_resize_"))
    backend = TmuxBackend(
        session_id=slug,
        working_dir=wd,
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    try:
        await backend.start()

        # Initial dims must match the module-level constants.
        out = subprocess.check_output(
            [
                "tmux",
                "-L",
                backend.socket_name,
                "display-message",
                "-t",
                backend.tmux_session,
                "-p",
                "#{window_width}x#{window_height}",
            ]
        ).decode().strip()
        assert out == f"{INITIAL_COLS}x{INITIAL_ROWS}", f"initial dims wrong: {out}"

        # Trigger a resize and wait for the fire-and-forget subprocess.
        backend.resize(cols=100, rows=30)
        await asyncio.sleep(0.3)

        out = subprocess.check_output(
            [
                "tmux",
                "-L",
                backend.socket_name,
                "display-message",
                "-t",
                backend.tmux_session,
                "-p",
                "#{window_width}x#{window_height}",
            ]
        ).decode().strip()
        assert out == "100x30", f"after resize dims wrong: {out}"
    finally:
        await backend.stop()


@requires_tmux
@pytest.mark.asyncio
async def test_tmux_backend_start_honors_initial_dims(tmux_socket_cleanup):
    """start(initial_cols=100, initial_rows=30) must birth the window at 100x30.

    Regression test for the birth-size flexibility added alongside the WS
    resize handshake: callers (SessionManager via CreateSessionRequest) can
    now pass client-measured dims so the pane doesn't flash at the module
    default of 132x40 before the first resize frame arrives.
    """
    slug = f"initdims_{secrets.token_hex(4)}"
    wd = Path(tempfile.mkdtemp(prefix="cc_initdims_"))
    backend = TmuxBackend(
        session_id=slug,
        working_dir=wd,
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    try:
        await backend.start(initial_cols=100, initial_rows=30)

        out = subprocess.check_output(
            [
                "tmux",
                "-L",
                backend.socket_name,
                "display-message",
                "-t",
                backend.tmux_session,
                "-p",
                "#{window_width}x#{window_height}",
            ]
        ).decode().strip()
        # Key assertion: NOT the default INITIAL_COLS x INITIAL_ROWS.
        assert out == "100x30", (
            f"expected 100x30 from initial_cols/initial_rows override, got {out} "
            f"(default would be {INITIAL_COLS}x{INITIAL_ROWS})"
        )
    finally:
        await backend.stop()


@requires_tmux
@pytest.mark.asyncio
async def test_tmux_backend_start_ignores_one_sided_initial_dims(tmux_socket_cleanup):
    """initial_cols alone (without initial_rows) falls back to defaults.

    Asymmetric input is treated as "not supplied" so we never pair a
    client-measured col count with a default row count (or vice versa),
    which would produce a nonsense pane shape.
    """
    slug = f"onesided_{secrets.token_hex(4)}"
    wd = Path(tempfile.mkdtemp(prefix="cc_onesided_"))
    backend = TmuxBackend(
        session_id=slug,
        working_dir=wd,
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    try:
        # Only cols supplied — should fall back to defaults for BOTH dims.
        await backend.start(initial_cols=100, initial_rows=None)

        out = subprocess.check_output(
            [
                "tmux",
                "-L",
                backend.socket_name,
                "display-message",
                "-t",
                backend.tmux_session,
                "-p",
                "#{window_width}x#{window_height}",
            ]
        ).decode().strip()
        assert out == f"{INITIAL_COLS}x{INITIAL_ROWS}", (
            f"asymmetric dims should fall back to defaults, got {out}"
        )
    finally:
        await backend.stop()


@requires_tmux
@pytest.mark.asyncio
async def test_tmux_backend_write_ctrl_l_single_byte(tmux_socket_cleanup):
    """write(b'\\x0c') must deliver the single control byte without error.

    The WS resize handshake sends Ctrl+L (0x0c) after reshaping to force
    the foreground app to redraw at the new size. 0x0c is a short
    control byte that triggers the ``send-keys -H`` keystroke path
    (short + has control bytes). This test locks in that the single-byte
    control write completes without raising and the session stays alive.
    """
    backend = TmuxBackend(
        session_id=f"ctrll_{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )

    async def _inner():
        # `cat` echoes whatever we write. 0x0c on most terminals renders as
        # a form-feed which may or may not scroll — the important thing is
        # the write doesn't raise AND the pane receives a byte.
        await backend.start(command="/bin/cat")
        await asyncio.sleep(0.3)

        # Single byte, control char → paste-buffer path.
        await backend.write(b"\x0c")
        await asyncio.sleep(0.5)

        # Session must still be alive — cat would die if we'd somehow
        # corrupted the pipe. Also confirms paste-buffer didn't raise.
        assert backend.is_alive(), "pane died after Ctrl+L write"

    try:
        await _inner()
    finally:
        await backend.stop()


# ---- attach_existing() rehydration path ---------------------------------


@requires_tmux
@pytest.mark.asyncio
async def test_tmux_backend_attach_existing_flips_running(tmux_socket_cleanup):
    """Simulate a server restart: create a session with one TmuxBackend, then
    build a SECOND instance pointing at the same slug/socket and call
    attach_existing(). It must flip _running=True and stay alive.
    """
    slug = f"attach_flip_{secrets.token_hex(4)}"
    wd = Path(tempfile.mkdtemp(prefix="cc_attach_"))
    first = TmuxBackend(
        session_id=slug,
        working_dir=wd,
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    try:
        # First instance births the tmux session.
        await first.start()
        assert first.is_alive()

        # Second instance — fresh Python-side state, same tmux session on the socket.
        # This is what SessionManager builds on server restart via build_backend().
        second = TmuxBackend(
            session_id=slug,
            working_dir=wd,
            on_output=None,
            socket_name=tmux_socket_cleanup,
        )
        assert second._running is False, "fresh instance must start with _running=False"
        assert second.tmux_session in second.discover_existing().sessions

        await second.attach_existing()

        assert second._running is True, "attach_existing must flip _running=True"
        assert second.is_alive(), "attached instance must see the live tmux session"

        # Calling attach_existing a second time must be a no-op, not an error.
        await second.attach_existing()
        assert second._running is True
    finally:
        # Clean up: kill via whichever instance still has references. Since
        # the tmux session is shared, either stop() will do the job — but
        # the second instance's reader task is the one we spun up, so tear
        # it down explicitly to avoid leaking the asyncio task.
        try:
            await second.stop()
        except Exception:
            pass
        try:
            await first.stop()
        except Exception:
            pass


@requires_tmux
@pytest.mark.asyncio
async def test_tmux_backend_attach_existing_raises_if_session_gone(tmux_socket_cleanup):
    """Attaching to a slug with no live tmux session must raise RuntimeError."""
    slug = f"attach_gone_{secrets.token_hex(4)}"
    wd = Path(tempfile.mkdtemp(prefix="cc_attach_gone_"))
    backend = TmuxBackend(
        session_id=slug,
        working_dir=wd,
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    # No start() — the tmux session does not exist on the socket.
    assert not backend.is_alive()
    with pytest.raises(RuntimeError, match="is not alive"):
        await backend.attach_existing()
    # And _running must NOT have been flipped despite the attempt.
    assert backend._running is False


@requires_tmux
@pytest.mark.asyncio
async def test_tmux_backend_attach_existing_write_works_after(tmux_socket_cleanup):
    """After attach_existing(), write() must succeed (the original bug)."""
    slug = f"attach_write_{secrets.token_hex(4)}"
    wd = Path(tempfile.mkdtemp(prefix="cc_attach_write_"))
    first = TmuxBackend(
        session_id=slug,
        working_dir=wd,
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    second = TmuxBackend(
        session_id=slug,
        working_dir=wd,
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    try:
        await first.start(command="/bin/cat")
        await asyncio.sleep(0.2)

        await second.attach_existing()

        # The original bug: write() raised "TmuxBackend is not running" after
        # rehydrate. This must NOT raise now.
        await second.write(b"\x7f")  # Backspace — hex-keys path
        await second.write(b"hello")  # Plain text — send-keys -l path

        assert second.is_alive()
    finally:
        try:
            await second.stop()
        except Exception:
            pass
        try:
            await first.stop()
        except Exception:
            pass


def test_pty_backend_attach_existing_raises_not_implemented():
    """PTYBackend cannot rehydrate — attach_existing must raise NotImplementedError."""
    backend = PTYBackend("attach-not-impl", Path.home(), None)
    with pytest.raises(NotImplementedError, match="does not persist"):
        asyncio.run(backend.attach_existing())


# ========================================================================
# Track 1 — "Adopt an external session" (plan v3 tests 1-7)
# ========================================================================
#
# Six essential unit tests covering the adopt flow behavior, plus one
# end-to-end integration test that exercises the full SessionManager
# adoption against a live tmux -L cloude socket.
#
# Test 1  — list_attachable_sessions flags ownership correctly (live tmux)
# Test 2  — ensure_pipe_pane() does NOT clobber an existing pipe-pane
# Test 3  — TmuxBackend.for_external() preserves literal name (no slugify)
# Test 4  — adopt_external_session() 409 without confirm, teardown with confirm
# Test 5  — adopt_external_session() surfaces pane-dead error
# Test 6  — lifespan_startup honors owned_tmux_sessions + legacy backfill
# Test 7  — end-to-end adopt against a live tmux socket (integration)
# ------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402


# ---- Test 1: list_attachable_sessions flags ownership correctly ---------


@requires_tmux
def test_list_attachable_sessions_flags_ownership_correctly(tmux_socket_cleanup):
    """Both cloude-owned and externally-created sessions must appear with
    correct ``created_by_cloude`` flags when ``owned_names`` is passed.

    Creates ONE session via TmuxBackend.start() (cloude-owned) and ONE via
    direct ``tmux -L <socket> new-session``. Asserts both surface in the
    listing with the right ownership flag cross-referenced against the
    explicit owned set — not the spoofable ``cloude_`` prefix heuristic.
    """
    # 1. cloude-owned session via the normal start() path.
    owned_backend = TmuxBackend(
        session_id=f"owned-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
        socket_name=tmux_socket_cleanup,
    )
    external_name = f"external_test_{secrets.token_hex(4)}"

    try:
        asyncio.run(owned_backend.start())

        # 2. External session created via direct tmux CLI on the same socket.
        subprocess.run(
            ["tmux", "-L", tmux_socket_cleanup, "new-session",
             "-d", "-s", external_name],
            check=True,
            capture_output=True,
        )

        # 3. List with explicit owned set (just the cloude-owned name).
        listing = owned_backend.list_attachable_sessions(
            owned_names={owned_backend.tmux_session}
        )
        assert listing.ok is True, (
            f"live tmux listing must be evaluable; got {listing!r}"
        )
        by_name = {r["name"]: r for r in listing.sessions}

        # Both must appear.
        assert owned_backend.tmux_session in by_name, (
            f"owned session missing from listing; got {sorted(by_name)}"
        )
        assert external_name in by_name, (
            f"external session missing from listing; got {sorted(by_name)}"
        )

        # Ownership flags.
        assert by_name[owned_backend.tmux_session]["created_by_cloude"] is True, (
            "owned session must be flagged created_by_cloude=True"
        )
        assert by_name[external_name]["created_by_cloude"] is False, (
            "external session must be flagged created_by_cloude=False"
        )

        # Window counts should be >= 1 for each live session.
        assert by_name[owned_backend.tmux_session]["window_count"] >= 1
        assert by_name[external_name]["window_count"] >= 1
    finally:
        # Kill external first (owned backend's stop also cleans its own).
        subprocess.run(
            ["tmux", "-L", tmux_socket_cleanup, "kill-session",
             "-t", external_name],
            check=False,
            capture_output=True,
        )
        try:
            asyncio.run(owned_backend.stop())
        except Exception:
            pass


# ---- Test 2: ensure_pipe_pane REPLACES an existing pipe-pane ------------


def test_ensure_pipe_pane_replaces_existing_pipe():
    """When ``#{pane_pipe}`` returns "1", ensure_pipe_pane must close the
    existing pipe and then start its own.

    This asserts the adoption contract established by the terminal-freeze fix
    in commit 6dfe52d. The original contract was the opposite: if any pipe was
    already active, ensure_pipe_pane logged ``pipe_pane_already_active`` and
    returned without piping. That deferred to the user's own logging
    ``pipe-pane``, but it meant an adopted session never got a pipe CloudeCode
    could read, so the websocket streaming loop tailed an empty file forever
    and the browser showed a permanently frozen terminal banner. Delivering
    output is what adoption is for, so the replacement is deliberate.

    Mocks ``_run_tmux`` to return "1" for the display-message probe, then
    asserts the exact three-call sequence: probe, bare ``pipe-pane`` (which in
    tmux closes any currently-piped command on the pane), then ``pipe-pane``
    with our own ``cat >>`` target.

    Inputs: none.
    Outputs: none. Raises AssertionError if the call sequence differs.
    """
    backend = TmuxBackend(
        session_id=f"pipe-replace-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
    )
    # Mark as external so the "not running" guard in ensure_pipe_pane
    # doesn't trip (external adopt is the real-world caller).
    backend._is_external = True

    calls: list[tuple] = []

    async def fake_run_tmux(*args, stdin_bytes=None, check=True):
        calls.append(args)
        # First call is the display-message probe for #{pane_pipe}.
        # Return "1" meaning pipe is already active.
        if args[0] == "display-message":
            return 0, b"1\n", b""
        return 0, b"", b""

    backend._run_tmux = fake_run_tmux  # type: ignore[assignment]

    asyncio.run(backend.ensure_pipe_pane())

    # Probe, close-existing, start-ours.
    assert len(calls) == 3, (
        f"expected probe + close + start, got {len(calls)} calls: {calls}"
    )
    assert calls[0][0] == "display-message", (
        f"first call must be display-message probe, got {calls[0][0]}"
    )

    # The close call is a bare `pipe-pane -t <target>` with NO command, which
    # is how tmux stops an active pipe. A trailing command here would mean we
    # never actually closed the user's pipe.
    assert calls[1][0] == "pipe-pane", (
        f"second call must close the existing pipe, got {calls[1]}"
    )
    assert len(calls[1]) == 3, (
        f"close call must carry no pipe command, got {calls[1]}"
    )

    # The start call must target our own capture file.
    assert calls[2][0] == "pipe-pane", (
        f"third call must start our pipe, got {calls[2]}"
    )
    assert calls[2][-1].startswith("cat >> "), (
        f"third call must pipe into our capture file, got {calls[2]}"
    )


def test_ensure_pipe_pane_starts_pipe_when_not_active():
    """Counterpart to the clobber test: when ``#{pane_pipe}`` returns "0",
    ensure_pipe_pane MUST issue pipe-pane to start capture. Locks in the
    happy path so a regression can't turn ensure_pipe_pane into a no-op.
    """
    backend = TmuxBackend(
        session_id=f"pipe-start-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
    )
    backend._is_external = True

    calls: list[tuple] = []

    async def fake_run_tmux(*args, stdin_bytes=None, check=True):
        calls.append(args)
        if args[0] == "display-message":
            return 0, b"0\n", b""
        return 0, b"", b""

    backend._run_tmux = fake_run_tmux  # type: ignore[assignment]

    asyncio.run(backend.ensure_pipe_pane())

    pipe_pane_calls = [c for c in calls if c and c[0] == "pipe-pane"]
    assert len(pipe_pane_calls) == 1, (
        f"expected exactly one pipe-pane call when pane_pipe=0, "
        f"got {len(pipe_pane_calls)}: {pipe_pane_calls}"
    )
    # No -o flag — we've confirmed no pipe is active, so the non-toggle
    # form is correct (toggle would be wrong in the general case).
    argv = pipe_pane_calls[0]
    assert "-o" not in argv, (
        f"pipe-pane must NOT use -o (toggle) when we've proven no pipe "
        f"is active; got argv {argv}"
    )


# ---- Test 3: TmuxBackend.for_external preserves literal name ------------


def test_for_external_preserves_literal_name_without_slugify():
    """``TmuxBackend.for_external("my-literal-name", ...)`` must bind the
    tmux session name LITERALLY — no slugification. The user gave their
    session a specific name when running ``tmux -L cloude new -s <name>``
    and we must adopt under THAT name, not ``cloude_<slug>``.
    """
    literal = "my-literal-name"
    inst = TmuxBackend.for_external(
        session_name=literal,
        working_dir=Path("/tmp"),
        on_output=None,
    )

    # The key invariants that differentiate for_external from the normal ctor.
    assert inst.tmux_session == literal, (
        f"for_external must preserve literal name, got {inst.tmux_session!r}"
    )
    assert "cloude_" not in inst.tmux_session, (
        f"for_external must NOT prepend cloude_ prefix, got {inst.tmux_session!r}"
    )
    assert inst._is_external is True, (
        "for_external must flip _is_external=True so attach_existing takes "
        "the adopt branch (pipe-pane setup, remain-on-exit, window-size warn)"
    )
    # slug is used for the pipe file name — also preserved literal.
    assert inst.slug == literal


def test_for_external_rejects_unsafe_session_name():
    """Names containing ``:`` or ``.`` are rejected up-front — tmux parses
    them as target separators and the adoption would target the wrong pane.
    """
    with pytest.raises(ValueError, match="unsafe tmux session name"):
        TmuxBackend.for_external(
            session_name="bad:name",
            working_dir=Path("/tmp"),
            on_output=None,
        )
    with pytest.raises(ValueError, match="unsafe tmux session name"):
        TmuxBackend.for_external(
            session_name="bad.name",
            working_dir=Path("/tmp"),
            on_output=None,
        )


# ---- Test 4: adopt_external_session coexists w/ another session ---------
# (multi-session refactor: adopt no longer raises 409 and no longer detaches)


@pytest.mark.asyncio
async def test_adopt_external_session_no_409_when_other_session_active():
    """Multi-session: adopting an external tmux session when another session
    is already live must NOT raise 409 — both sessions coexist. The legacy
    single-active 409 gate (and the ``confirm_detach`` modal it implied) is
    gone; ``confirm_detach`` is accepted for API back-compat and ignored.
    """
    from src.core.session_manager import SessionManager
    from src.models import Session, SessionStatus

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    # Pre-register an existing live session A directly into the dicts.
    sess_a = Session(
        id="ses_existing",
        pty_pid=None,
        working_dir="/tmp",
        status=SessionStatus.RUNNING,
        created_at=datetime_for_tests(),
        last_activity=datetime_for_tests(),
    )
    backend_a = MagicMock()
    backend_a.is_alive = MagicMock(return_value=True)
    backend_a.tmux_session = "cloude_existing"
    sm._register_session(sess_a, backend_a)

    sm._resolve_external_cwd = AsyncMock(return_value=Path("/tmp"))  # type: ignore[assignment]

    fake_backend = MagicMock()
    fake_backend.attach_existing = AsyncMock(return_value=None)
    fake_backend.capture_scrollback = MagicMock(return_value=b"")
    fake_backend.tmux_session = "external_target"
    fake_backend._pipe_path = Path("/tmp/does_not_exist_for_test.pipe")

    with patch(
        "src.core.tmux_backend.TmuxBackend.for_external",
        return_value=fake_backend,
    ), patch(
        "src.core.session_manager.settings"
    ) as mock_settings:
        auth_cfg = MagicMock()
        auth_cfg.session.tmux_socket_name = _TEST_SOCKET
        auth_cfg.session.scrollback_lines = 3000
        auth_cfg.notifications.idle_threshold_seconds = 30.0
        mock_settings.load_auth_config.return_value = auth_cfg

        # Both with and without confirm_detach must succeed (no 409 ever).
        result = await sm.adopt_external_session(
            "external_target", confirm_detach=False
        )

    assert "session" in result and "initial_scrollback_b64" in result
    assert "fifo_start_offset" in result
    # Session A must still be registered — adopt never detaches/destroys it.
    assert "ses_existing" in sm.sessions, (
        "adopting a second session must NOT remove the first one"
    )
    assert "adopted:external_target" in sm.sessions, (
        "the adopted session must be registered as a concurrent session"
    )
    fake_backend.attach_existing.assert_awaited_once()


@pytest.mark.asyncio
async def test_adopt_external_session_does_not_detach_or_destroy_prior():
    """Multi-session: adopt must NEVER invoke ``detach_current_session`` or
    ``destroy_session`` — switching to a different session is now additive,
    not a swap. Pure control-flow test; the TmuxBackend factory is mocked.
    """
    from src.core.session_manager import SessionManager
    from src.models import Session, SessionStatus

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    sess_a = Session(
        id="prior_session",
        pty_pid=None,
        working_dir="/tmp",
        status=SessionStatus.RUNNING,
        created_at=datetime_for_tests(),
        last_activity=datetime_for_tests(),
    )
    backend_a = MagicMock()
    backend_a.is_alive = MagicMock(return_value=True)
    backend_a.tmux_session = "cloude_prior"
    sm._register_session(sess_a, backend_a)

    detach_mock = AsyncMock(return_value=True)
    sm.detach_current_session = detach_mock  # type: ignore[assignment]
    destroy_mock = AsyncMock(return_value=True)
    sm.destroy_session = destroy_mock  # type: ignore[assignment]
    sm._resolve_external_cwd = AsyncMock(return_value=Path("/tmp"))  # type: ignore[assignment]

    fake_backend = MagicMock()
    fake_backend.attach_existing = AsyncMock(return_value=None)
    fake_backend.capture_scrollback = MagicMock(return_value=b"")
    fake_backend.tmux_session = "external_target"
    fake_backend._pipe_path = Path("/tmp/does_not_exist_for_test.pipe")

    with patch(
        "src.core.tmux_backend.TmuxBackend.for_external",
        return_value=fake_backend,
    ), patch(
        "src.core.session_manager.settings"
    ) as mock_settings:
        auth_cfg = MagicMock()
        auth_cfg.session.tmux_socket_name = _TEST_SOCKET
        auth_cfg.session.scrollback_lines = 3000
        auth_cfg.notifications.idle_threshold_seconds = 30.0
        mock_settings.load_auth_config.return_value = auth_cfg

        result = await sm.adopt_external_session(
            "external_target", confirm_detach=True
        )

    detach_mock.assert_not_awaited(), (
        "adopt must NOT detach the prior session — sessions are additive now"
    )
    destroy_mock.assert_not_awaited(), (
        "adopt must NEVER call destroy_session"
    )
    assert "session" in result
    fake_backend.attach_existing.assert_awaited_once()


def datetime_for_tests():
    """Small helper — imports datetime only at call site to avoid top clutter."""
    from datetime import datetime
    return datetime.utcnow()


# ---- Test 4b: per-session output fan-out isolation ---------------------


@pytest.mark.asyncio
async def test_concurrent_sessions_output_isolation():
    """Two concurrent sessions, each with its own subscriber queue.

    - Output produced by session A's backend lands ONLY in A's queue.
    - B's queue stays empty.
    - Destroying A does NOT touch B's subscribers (B's queue still works).
    This is the core invariant the multi-session refactor buys.
    """
    import base64

    from src.core.session_manager import SessionManager
    from src.models import Session, SessionStatus

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    # Register two sessions A and B directly (no real backends needed —
    # we exercise the fan-out plumbing, not tmux).
    def _mk_backend(name):
        b = MagicMock()
        b.is_alive = MagicMock(return_value=True)
        b.tmux_session = name

        async def _stop():
            return None

        b.stop = _stop
        return b

    sess_a = Session(id="ses_a", working_dir="/tmp", status=SessionStatus.RUNNING)
    sess_b = Session(id="ses_b", working_dir="/tmp", status=SessionStatus.RUNNING)
    sm._register_session(sess_a, _mk_backend("cloude_a"))
    sm._register_session(sess_b, _mk_backend("cloude_b"))

    qa = sm.subscribe_output("ses_a")
    qb = sm.subscribe_output("ses_b")

    # Push output through session A's bound handler.
    handler_a = sm._make_output_handler("ses_a")
    await handler_a(b"hello-A")

    assert not qa.empty(), "session A's subscriber must receive A's bytes"
    got = base64.b64decode(await qa.get())
    assert got == b"hello-A"
    assert qb.empty(), "session B's subscriber must NOT receive A's bytes"

    # Symmetric check for B.
    handler_b = sm._make_output_handler("ses_b")
    await handler_b(b"hello-B")
    assert not qb.empty()
    assert base64.b64decode(await qb.get()) == b"hello-B"
    assert qa.empty()

    # Destroy A. B's subscriber list must be untouched.
    metadata_path = sm.__class__.__module__  # noqa: F841 (sanity, unused)
    with patch.object(type(sm), "_save_session_metadata", lambda *a, **k: None), \
         patch("src.core.session_manager.settings") as mock_settings:
        mock_settings.get_session_metadata_path.return_value = Path(
            "/tmp/never_existent_metadata_for_test.json"
        )
        await sm.destroy_session("ses_a")

    assert "ses_a" not in sm.sessions
    assert "ses_b" in sm.sessions, "destroying A must NOT remove B"
    assert qb in sm._subscribers.get("ses_b", []), (
        "destroying A must not clear B's subscribers"
    )
    # B's queue still functions after A's teardown.
    handler_b2 = sm._make_output_handler("ses_b")
    await handler_b2(b"still-alive-B")
    assert base64.b64decode(await qb.get()) == b"still-alive-B"


# ---- Test 5: adopt_external_session propagates pane-dead error ----------


@pytest.mark.asyncio
async def test_adopt_external_session_refuses_dead_pane():
    """When the target pane is already dead (``#{pane_dead}`` == "1"),
    ``attach_existing`` raises RuntimeError("pane already dead") — the
    adopt method must propagate (wrap or bare) so the route returns 500
    instead of silently registering a dead session.
    """
    from src.core.session_manager import SessionManager

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    # No active session — skip the confirm gate entirely.
    sm.has_active_session = MagicMock(return_value=False)  # type: ignore[assignment]
    sm._resolve_external_cwd = AsyncMock(return_value=Path("/tmp"))  # type: ignore[assignment]

    # Backend that immediately raises on attach_existing simulating a dead pane.
    fake_backend = MagicMock()
    fake_backend.attach_existing = AsyncMock(
        side_effect=RuntimeError("cannot adopt foo: pane already dead")
    )
    fake_backend.capture_scrollback = MagicMock(return_value=b"")
    fake_backend._pipe_path = Path("/tmp/never_existent.pipe")

    with patch(
        "src.core.tmux_backend.TmuxBackend.for_external",
        return_value=fake_backend,
    ), patch(
        "src.core.session_manager.settings"
    ) as mock_settings:
        auth_cfg = MagicMock()
        auth_cfg.session.tmux_socket_name = _TEST_SOCKET
        auth_cfg.session.scrollback_lines = 3000
        mock_settings.load_auth_config.return_value = auth_cfg

        with pytest.raises(RuntimeError, match="pane already dead"):
            await sm.adopt_external_session("foo", confirm_detach=True)


# ---- Test 6: lifespan_startup honors owned_tmux_sessions + legacy fallback


@pytest.mark.asyncio
async def test_lifespan_startup_does_not_rehydrate_non_owned_session(tmp_path):
    """When metadata has ``owned_tmux_sessions: []`` and a session id whose
    tmux slug IS live on the socket, lifespan_startup must NOT rehydrate
    that session — it's not ours. The ownership gate prevents a user's
    ``cloude_foo`` external session from being silently adopted as our own.
    """
    from src.core.session_manager import SessionManager
    from src.models import Session, SessionStatus

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    # Simulate loaded metadata: session exists, but owned set is empty.
    sm._register_session(
        Session(
            id="foo",
            pty_pid=None,
            working_dir=str(tmp_path),
            status=SessionStatus.RUNNING,
            created_at=datetime_for_tests(),
            last_activity=datetime_for_tests(),
        ),
        backend=None,
    )
    sm.owned_tmux_sessions = set()  # NOT owned
    sm._legacy_metadata_needs_backfill = False  # new-schema file

    # Build a fake probe backend that reports the slug is live (simulating
    # an external user-created ``cloude_foo`` session on the socket).
    fake_probe = MagicMock()
    fake_probe.discover_existing = MagicMock(return_value=["cloude_foo"])

    attach_mock = AsyncMock()
    fake_hydrate = MagicMock()
    fake_hydrate.tmux_session = "cloude_foo"
    fake_hydrate.attach_existing = attach_mock

    # First build_backend call is for the probe; second is for the rehydrate
    # candidate. Return the probe first, then the hydration backend.
    with patch(
        "src.core.session_manager.build_backend",
        side_effect=[fake_probe, fake_hydrate],
    ):
        # Stub _clear_stale_metadata so we don't touch the filesystem;
        # mirror the real method's effect of wiping the session's state.
        sm._clear_stale_metadata = MagicMock(  # type: ignore[assignment]
            side_effect=lambda sid=None: sm._wipe_session_state(sid or "foo"),
        )
        await sm.lifespan_startup()

    # The hydrate backend's attach_existing must NOT have been called —
    # ownership gate must block it.
    attach_mock.assert_not_awaited()
    # And no backend must have been registered for the non-owned session.
    assert sm.current_backend is None, (
        "non-owned session must not be registered as an active backend"
    )


@pytest.mark.asyncio
async def test_lifespan_startup_legacy_backfill_populates_owned_set(tmp_path):
    """When metadata lacks ``owned_tmux_sessions`` (pre-v3 schema) AND a
    matching tmux session exists, rehydrate succeeds and the owned set
    is populated post-hoc. Guards against stranding in-flight sessions
    across the v3 upgrade.
    """
    from src.core.session_manager import SessionManager
    from src.models import Session, SessionStatus

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    sm._register_session(
        Session(
            id="legacy_sess",
            pty_pid=None,
            working_dir=str(tmp_path),
            status=SessionStatus.RUNNING,
            created_at=datetime_for_tests(),
            last_activity=datetime_for_tests(),
        ),
        backend=None,
    )
    sm.owned_tmux_sessions = set()  # empty set
    sm._legacy_metadata_needs_backfill = True  # <-- key flag for the legacy path

    fake_probe = MagicMock()
    fake_probe.discover_existing = MagicMock(return_value=["cloude_legacy_sess"])

    attach_mock = AsyncMock()
    fake_hydrate = MagicMock()
    fake_hydrate.tmux_session = "cloude_legacy_sess"
    fake_hydrate.attach_existing = attach_mock

    save_mock = MagicMock()
    sm._save_session_metadata = save_mock  # type: ignore[assignment]

    with patch(
        "src.core.session_manager.build_backend",
        side_effect=[fake_probe, fake_hydrate],
    ):
        await sm.lifespan_startup()

    attach_mock.assert_awaited_once(), (
        "legacy-backfill path must still rehydrate the active session"
    )
    assert "cloude_legacy_sess" in sm.owned_tmux_sessions, (
        f"owned_tmux_sessions must be backfilled; got {sm.owned_tmux_sessions}"
    )
    save_mock.assert_called(), (
        "metadata must be re-persisted after legacy backfill to migrate schema"
    )


# ---- Test 7: end-to-end adopt (integration, requires live tmux) ---------


@requires_tmux
@pytest.mark.asyncio
async def test_adopt_external_session_end_to_end(tmp_path, monkeypatch):
    """Full adopt cycle against a real ``tmux -L cloude`` socket.

    Spawns a real external tmux session, echoes a known marker into it,
    invokes ``SessionManager.adopt_external_session``, and verifies:
      - initial_scrollback_b64 decodes to contain the marker echo
      - fifo_start_offset >= 0
      - backend.tmux_session matches the literal external name
      - a subsequent write() reaches the pane via capture_scrollback()
      - cleanup kills the session without error
    """
    import base64

    from src.core.session_manager import SessionManager

    name = f"adopt_itest_{secrets.token_hex(4)}"
    marker = f"external-hello-{secrets.token_hex(3)}"
    # Point DEFAULT_WORKING_DIR at a tmp dir so SessionManager constructors
    # don't depend on env state.
    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path / "logs"))

    # Create the external session and inject a deterministic marker.
    subprocess.run(
        ["tmux", "-L", _TEST_SOCKET, "new-session", "-d", "-s", name],
        check=True,
        capture_output=True,
    )
    # Build SM — _load_session_metadata is best-effort; patch to no-op
    # so a stray real session_metadata.json doesn't taint this test.
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    try:
        subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "send-keys", "-t",
             name, f"echo {marker}", "Enter"],
            check=True,
            capture_output=True,
        )
        # Let the echo land in the pane.
        await asyncio.sleep(0.6)

        result = await sm.adopt_external_session(name, confirm_detach=True)

        # Contract assertions on the response shape.
        assert "session" in result
        assert "initial_scrollback_b64" in result
        assert "fifo_start_offset" in result
        assert result["fifo_start_offset"] >= 0, (
            f"fifo_start_offset must be non-negative, got {result['fifo_start_offset']}"
        )

        sb_b64 = result["initial_scrollback_b64"]
        assert sb_b64, "initial_scrollback_b64 should be non-empty after the echo"
        decoded = base64.b64decode(sb_b64).decode("utf-8", "replace")
        assert marker in decoded, (
            f"expected marker {marker!r} in scrollback; got {decoded[:300]!r}..."
        )

        # Backend is wired up correctly — literal external name, not slugified.
        assert sm.backend is not None
        assert sm.backend.tmux_session == name, (
            f"expected backend.tmux_session == {name!r}, "
            f"got {sm.backend.tmux_session!r}"
        )

        # Write new bytes via the backend; verify via capture_scrollback.
        second_marker = f"second-line-{secrets.token_hex(3)}"
        await sm.backend.write(f"echo {second_marker}\n".encode("utf-8"))
        await asyncio.sleep(0.6)

        pane_bytes = sm.backend.capture_scrollback()
        pane_text = pane_bytes.decode("utf-8", errors="replace")
        assert second_marker in pane_text, (
            f"second write must reach the pane; expected {second_marker!r}, "
            f"got trailing pane: {pane_text[-300:]!r}"
        )
    finally:
        # Clean up tmux session.
        subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "kill-session", "-t", name],
            check=False,
            capture_output=True,
        )
        # Tear down backend to avoid leaking the tail task.
        if sm.backend is not None:
            try:
                await sm.backend.stop()
            except Exception:
                pass


# ---- Test N: detach_current_session keeps tmux alive --------------------


@requires_tmux
@pytest.mark.asyncio
async def test_detach_current_session_keeps_tmux_alive(tmp_path, monkeypatch):
    """``SessionManager.detach_current_session`` must tear down Python-side
    handles (backend refs, session, reader task) WITHOUT killing the tmux
    session on the server. The tmux pane should still pass ``has-session``
    after detach so the user can re-adopt it from the launchpad.

    Also asserts ``owned_tmux_sessions`` is preserved — the detached
    session should still be labeled as cloude-owned in the Adopt UI for
    the remainder of this server lifetime.
    """
    from src.core.session_manager import SessionManager

    # Sandbox env so we don't tamper with a real metadata file.
    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path / "logs"))

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    session_id = f"detach_itest_{secrets.token_hex(4)}"

    try:
        # Create a real cloude-owned session on the real socket.
        await sm.create_session(
            session_id=session_id,
            working_dir=str(tmp_path),
            auto_start_claude=False,
        )
        assert sm.backend is not None
        tmux_name = sm.backend.tmux_session
        # The newly-created session is tracked as owned.
        assert tmux_name in sm.owned_tmux_sessions

        # Sanity: tmux says the session is alive BEFORE detach.
        alive_before = subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "has-session", "-t", tmux_name],
            capture_output=True,
        )
        assert alive_before.returncode == 0, (
            "baseline: tmux session should be alive after create_session"
        )

        # Act: detach.
        detached = await sm.detach_current_session()
        assert detached is True

        # Python-side invariants.
        assert sm.backend is None, "detach must clear backend ref"
        assert sm.session is None, "detach must clear session ref"
        assert sm.idle_watcher is None, "detach must stop idle watcher"

        # owned_tmux_sessions must persist so Adopt UI still flags the
        # detached session as cloude-owned.
        assert tmux_name in sm.owned_tmux_sessions, (
            "owned_tmux_sessions entry must survive detach so the Adopt "
            "UI labels the detached session as created_by_cloude=True"
        )

        # The tmux session must still be alive on the server — the whole
        # point of detach-vs-destroy.
        alive_after = subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "has-session", "-t", tmux_name],
            capture_output=True,
        )
        assert alive_after.returncode == 0, (
            f"tmux session {tmux_name} must still be alive after detach "
            "(stderr: {!r})".format(alive_after.stderr.decode(errors='replace'))
        )

        # A second detach call is a no-op (returns False, doesn't raise).
        assert await sm.detach_current_session() is False

    finally:
        # Always clean up the tmux session we created, whether the test
        # passed or failed. Use the real cloude socket since that's where
        # we created it.
        subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "kill-session", "-t",
             f"cloude_{session_id}"],
            check=False,
            capture_output=True,
        )


# ---- Test N+1: adopt-swap detaches prior (keeps it alive) ---------------


@requires_tmux
@pytest.mark.asyncio
async def test_adopt_external_session_detach_keeps_prior_tmux_alive(
    tmp_path, monkeypatch
):
    """Adopt-swap must DETACH the prior active session, not destroy it.

    Switching never kills — only the explicit destroy button should kill
    tmux. This test encodes that invariant end-to-end against a real
    ``tmux -L cloude`` socket:

      1. Create a cloude-owned session A via ``create_session``.
      2. Create an external session B via ``tmux new -d -s B``.
      3. Call ``adopt_external_session(B, confirm_detach=True)``.
      4. Assert ``has-session A`` still returns 0 (alive).
      5. Assert ``sm.backend.tmux_session == B``.
    """
    from src.core.session_manager import SessionManager

    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path / "logs"))

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    session_id_a = f"swap_prior_{secrets.token_hex(4)}"
    name_b = f"swap_ext_{secrets.token_hex(4)}"
    tmux_name_a: str | None = None

    try:
        # --- Step 1: bring up cloude-owned session A on the real socket.
        await sm.create_session(
            session_id=session_id_a,
            working_dir=str(tmp_path),
            auto_start_claude=False,
        )
        assert sm.backend is not None, "A must be the active backend"
        tmux_name_a = sm.backend.tmux_session
        assert tmux_name_a in sm.owned_tmux_sessions

        alive_a_before = subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "has-session", "-t", tmux_name_a],
            capture_output=True,
        )
        assert alive_a_before.returncode == 0, (
            "baseline: prior session A must be alive before adopt-swap"
        )

        # --- Step 2: spawn external session B (the target of the adopt).
        subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "new-session", "-d", "-s", name_b],
            check=True,
            capture_output=True,
        )

        # --- Step 3: adopt-swap with explicit consent to detach.
        result = await sm.adopt_external_session(name_b, confirm_detach=True)
        assert "session" in result
        assert sm.backend is not None
        assert sm.backend.tmux_session == name_b, (
            f"post-swap backend must point at B={name_b!r}, "
            f"got {sm.backend.tmux_session!r}"
        )

        # --- Step 4: the INVARIANT — A's tmux session is STILL ALIVE.
        alive_a_after = subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "has-session", "-t", tmux_name_a],
            capture_output=True,
        )
        assert alive_a_after.returncode == 0, (
            f"prior session A={tmux_name_a!r} must still be alive after "
            "adopt-swap (detach-not-destroy); "
            f"stderr={alive_a_after.stderr.decode(errors='replace')!r}"
        )

        # --- Step 5: A stays in owned_tmux_sessions so the Adopt UI
        # re-offers it tagged created_by_cloude=True.
        assert tmux_name_a in sm.owned_tmux_sessions, (
            "owned_tmux_sessions entry for A must survive adopt-swap "
            "so the launchpad re-lists A as cloude-owned"
        )

    finally:
        # Clean up both sessions; ignore failures (e.g. already gone).
        for tname in filter(None, [tmux_name_a, name_b]):
            subprocess.run(
                ["tmux", "-L", _TEST_SOCKET, "kill-session", "-t", tname],
                check=False,
                capture_output=True,
            )
        # Tear down the adopted backend to avoid a leaked tail task.
        if sm.backend is not None:
            try:
                await sm.backend.stop()
            except Exception:
                pass


# ---- sanitize_tmux_name -------------------------------------------------

def test_sanitize_tmux_name_preserves_verbatim_input():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("Cloude Code Dev") == "Cloude Code Dev"


def test_sanitize_tmux_name_replaces_dot_and_colon():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("Dotted.Name:Thing") == "Dotted_Name_Thing"


def test_sanitize_tmux_name_preserves_emoji_and_unicode():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("🔥 cool 🔥") == "🔥 cool 🔥"


def test_sanitize_tmux_name_collapses_whitespace_runs():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("   many   spaces   ") == "many spaces"


def test_sanitize_tmux_name_returns_empty_for_unusable_input():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("") == ""
    assert _sanitize_tmux_name("   ") == ""
    assert _sanitize_tmux_name(":::...") == "______"


def test_sanitize_tmux_name_only_separators_yields_underscore_run():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("...") == "___"


def test_sanitize_tmux_name_strips_newlines_and_tabs():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("foo\n\tbar") == "foo bar"


def test_tmux_backend_accepts_verbatim_session_name_override():
    """When session_name= is passed to __init__, it's used verbatim
    instead of applying the slug+prefix transformation to session_id."""
    backend = TmuxBackend(
        session_id="ses_abc123",
        working_dir=Path(tempfile.mkdtemp(prefix="cc_t2_")),
        session_name="cloude_Cloude Code Dev",
    )
    assert backend.tmux_session == "cloude_Cloude Code Dev"
    # session_id still recorded for metadata
    assert backend.session_id == "ses_abc123"


def test_tmux_backend_without_session_name_uses_legacy_slug():
    """Backward compat: no session_name kwarg → legacy cloude_<slug> naming."""
    backend = TmuxBackend(
        session_id="ses_abc123",
        working_dir=Path(tempfile.mkdtemp(prefix="cc_t2b_")),
    )
    # Existing code derives slug from session_id; we assert prefix and
    # that the session_id content appears somewhere.
    assert backend.tmux_session.startswith("cloude_")
    assert "abc123" in backend.tmux_session


def test_create_session_request_accepts_project_name():
    from src.models import CreateSessionRequest
    req = CreateSessionRequest(
        working_dir="/tmp",
        auto_start_claude=False,
        copy_templates=False,
        project_name="Cloude Code Dev",
    )
    assert req.project_name == "Cloude Code Dev"


def test_create_session_request_project_name_defaults_to_none():
    from src.models import CreateSessionRequest
    req = CreateSessionRequest(
        working_dir="/tmp",
        auto_start_claude=False,
        copy_templates=False,
    )
    assert req.project_name is None


# ---- Task 4: project_name end-to-end plumbing ---------------------------


@requires_tmux
@pytest.mark.asyncio
async def test_session_manager_create_session_verbatim_name(tmp_path, monkeypatch):
    """create_session(project_name="T4 Verbatim Test") must yield
    tmux_session == "cloude_T4 Verbatim Test" — project_name flows through
    SessionManager → build_backend → TmuxBackend verbatim (with prefix).
    """
    from src.core.session_manager import SessionManager

    # Sandbox so we don't touch a real metadata file.
    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path / "logs"))

    session_id = f"t4verbatim_{secrets.token_hex(4)}"
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()
    try:
        await sm.create_session(
            session_id=session_id,
            working_dir=str(tmp_path),
            auto_start_claude=False,
            copy_templates=False,
            project_name="T4 Verbatim Test",
        )
        assert sm.backend is not None
        assert sm.backend.tmux_session == "cloude_T4 Verbatim Test", (
            f"expected verbatim tmux_session cloude_T4 Verbatim Test, "
            f"got {sm.backend.tmux_session!r}"
        )
    finally:
        if sm.backend is not None:
            try:
                await sm.destroy_session()
            except Exception:
                # Belt-and-suspenders: kill by literal name in case
                # destroy_session fails mid-teardown.
                subprocess.run(
                    ["tmux", "-L", _TEST_SOCKET, "kill-session",
                     "-t", "cloude_T4 Verbatim Test"],
                    check=False,
                    capture_output=True,
                )


@requires_tmux
@pytest.mark.asyncio
async def test_session_manager_create_session_without_project_name_uses_legacy(
    tmp_path, monkeypatch
):
    """create_session without project_name → legacy ``cloude_ses_<hex>``
    naming is preserved (backward compat — nothing else should change).
    """
    from src.core.session_manager import SessionManager

    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path / "logs"))

    session_id = f"ses_{secrets.token_hex(4)}"
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()
    try:
        await sm.create_session(
            session_id=session_id,
            working_dir=str(tmp_path),
            auto_start_claude=False,
            copy_templates=False,
        )
        assert sm.backend is not None
        assert sm.backend.tmux_session.startswith("cloude_ses_"), (
            f"expected legacy cloude_ses_<hex> naming, "
            f"got {sm.backend.tmux_session!r}"
        )
    finally:
        if sm.backend is not None:
            try:
                await sm.destroy_session()
            except Exception:
                pass


# ---- Task 5: uniquify-on-collision (multi-session, never adopt) ---------


@requires_tmux
@pytest.mark.asyncio
async def test_create_session_uniquifies_when_target_name_exists(tmp_path, monkeypatch):
    """If a tmux session with the derived name already exists on our
    socket, create_session must mint a NEW, distinct tmux session with a
    numeric-suffixed name (``cloude_<x>-2``) rather than adopting the
    existing one. The pre-existing session must be left completely alone —
    still alive, untouched, unrenamed."""
    from src.core.session_manager import SessionManager

    # Sandbox so we don't touch the real metadata file.
    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path / "logs"))

    project_name = f"collide_{secrets.token_hex(4)}"
    target_tmux = f"cloude_{project_name}"
    expected_new_tmux = f"{target_tmux}-2"

    # Pre-create the tmux session on our socket so the collision fires.
    subprocess.run(
        ["tmux", "-L", _TEST_SOCKET, "new-session", "-d", "-s", target_tmux],
        check=True,
    )
    try:
        wd = Path(tempfile.mkdtemp(prefix="cc_t5_"))
        with patch.object(SessionManager, "_load_session_metadata", return_value=None):
            sm = SessionManager()
        try:
            await sm.create_session(
                session_id=f"ses_{secrets.token_hex(4)}",
                working_dir=str(wd),
                auto_start_claude=False,
                copy_templates=False,
                project_name=project_name,
            )
            assert sm.backend is not None
            assert sm.backend.tmux_session == expected_new_tmux, (
                f"expected uniquified tmux_session {expected_new_tmux!r}, "
                f"got {sm.backend.tmux_session!r}"
            )
            # Both the pre-existing session AND the new one must be alive.
            assert subprocess.call(
                ["tmux", "-L", _TEST_SOCKET, "has-session", "-t", target_tmux],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) == 0, "pre-existing tmux session was unexpectedly touched"
            assert subprocess.call(
                ["tmux", "-L", _TEST_SOCKET, "has-session", "-t", expected_new_tmux],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) == 0, "new uniquified tmux session was not created"
        finally:
            if sm.backend is not None:
                try:
                    await sm.destroy_session()
                except Exception:
                    pass
    finally:
        subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "kill-session", "-t", target_tmux],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "kill-session", "-t", expected_new_tmux],
            check=False,
            capture_output=True,
        )


@requires_tmux
@pytest.mark.asyncio
async def test_create_session_uniquifies_third_collision(tmp_path, monkeypatch):
    """A THIRD create_session call against an already-double-collided name
    must mint ``cloude_<x>-3``, proving the uniquifier walks past -2 when
    that's also taken."""
    from src.core.session_manager import SessionManager

    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path / "logs"))

    project_name = f"collide3_{secrets.token_hex(4)}"
    target_tmux = f"cloude_{project_name}"
    second_tmux = f"{target_tmux}-2"
    expected_third_tmux = f"{target_tmux}-3"

    # Pre-create BOTH the base name and the "-2" name on our socket.
    subprocess.run(
        ["tmux", "-L", _TEST_SOCKET, "new-session", "-d", "-s", target_tmux],
        check=True,
    )
    subprocess.run(
        ["tmux", "-L", _TEST_SOCKET, "new-session", "-d", "-s", second_tmux],
        check=True,
    )
    try:
        wd = Path(tempfile.mkdtemp(prefix="cc_t5c_"))
        with patch.object(SessionManager, "_load_session_metadata", return_value=None):
            sm = SessionManager()
        try:
            await sm.create_session(
                session_id=f"ses_{secrets.token_hex(4)}",
                working_dir=str(wd),
                auto_start_claude=False,
                copy_templates=False,
                project_name=project_name,
            )
            assert sm.backend is not None
            assert sm.backend.tmux_session == expected_third_tmux, (
                f"expected uniquified tmux_session {expected_third_tmux!r}, "
                f"got {sm.backend.tmux_session!r}"
            )
        finally:
            if sm.backend is not None:
                try:
                    await sm.destroy_session()
                except Exception:
                    pass
    finally:
        for name in (target_tmux, second_tmux, expected_third_tmux):
            subprocess.run(
                ["tmux", "-L", _TEST_SOCKET, "kill-session", "-t", name],
                check=False,
                capture_output=True,
            )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
