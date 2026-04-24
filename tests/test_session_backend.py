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
    backend = PTYBackend("test", Path.home(), None)
    assert backend.discover_existing() == []


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
    with mock.patch("src.core.session_backend.shutil.which", return_value=None):
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
                    tmux_socket_name = "cloude"
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


# ---- TmuxBackend integration (requires tmux) ----------------------------


# Use a unique socket per test run so CI parallelism + local dev don't clash.
_TEST_SOCKET = f"cloude_test_{uuid.uuid4().hex[:8]}"


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
        names = backend.discover_existing()
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
    assert argv[3] == f"{backend.tmux_session}:0.0"
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
    assert argv[3] == f"{backend.tmux_session}:0.0"
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
        assert second.tmux_session in second.discover_existing()

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
        results = owned_backend.list_attachable_sessions(
            owned_names={owned_backend.tmux_session}
        )
        by_name = {r["name"]: r for r in results}

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


# ---- Test 2: ensure_pipe_pane does NOT clobber existing pipe-pane -------


def test_ensure_pipe_pane_does_not_clobber_existing_pipe():
    """When ``#{pane_pipe}`` returns "1" (pipe already active), ensure_pipe_pane
    must NOT issue a ``pipe-pane`` command — doing so would stop the user's
    existing pipe (``pipe-pane -o`` is a toggle, and the non-toggle form
    would still STOP a mismatched command or overwrite the user's target).

    Mocks ``_run_tmux`` to return "1" for the display-message probe, then
    asserts no subsequent call has "pipe-pane" as its first arg.
    """
    backend = TmuxBackend(
        session_id=f"pipe-clobber-{uuid.uuid4().hex[:6]}",
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

    # Exactly ONE call expected — the display-message probe. No pipe-pane.
    assert len(calls) == 1, (
        f"expected only the display-message probe, got {len(calls)} calls: {calls}"
    )
    assert calls[0][0] == "display-message", (
        f"first call must be display-message probe, got {calls[0][0]}"
    )

    # Defensive: no pipe-pane command anywhere.
    pipe_pane_calls = [c for c in calls if c and c[0] == "pipe-pane"]
    assert pipe_pane_calls == [], (
        f"ensure_pipe_pane must NOT issue pipe-pane when pane_pipe=1, "
        f"got {pipe_pane_calls}"
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


# ---- Test 4: adopt_external_session 409 / detach-on-confirm -------------


@pytest.mark.asyncio
async def test_adopt_external_session_raises_409_without_confirm():
    """If a session is already active, adopt must raise HTTPException(409)
    unless ``confirm_detach=True`` — silent detach of a user's live
    session is unacceptable (the user should see the swap modal first).
    """
    from fastapi import HTTPException

    from src.core.session_manager import SessionManager

    # Instantiate without hitting _load_session_metadata's side effects.
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    # Simulate an active session.
    sm.has_active_session = MagicMock(return_value=True)  # type: ignore[assignment]

    with pytest.raises(HTTPException) as excinfo:
        await sm.adopt_external_session("some_external", confirm_detach=False)

    assert excinfo.value.status_code == 409, (
        f"expected 409 when active session exists and confirm_detach=False, "
        f"got {excinfo.value.status_code}"
    )
    assert "confirm_detach" in excinfo.value.detail.lower(), (
        f"409 detail should mention confirm_detach; got {excinfo.value.detail!r}"
    )


@pytest.mark.asyncio
async def test_adopt_external_session_detaches_prior_when_confirmed():
    """With ``confirm_detach=True``, adopt must invoke ``detach_current_session``
    (NOT ``destroy_session``) on the prior backend BEFORE building the new
    one. Switching never kills — the prior tmux session stays alive so the
    user can re-adopt it later. Destruction is only via the explicit
    destroy button. Mocks the TmuxBackend factory so we don't touch tmux
    at all — this is purely a control-flow test of the adopt sequence.
    """
    from src.core.session_manager import SessionManager
    from src.models import Session, SessionStatus

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    # Pretend there's an active session. has_active_session() must report
    # True initially so the confirm-gate sees it and triggers detach.
    sm.session = Session(
        id="prior_session",
        pty_pid=None,
        working_dir="/tmp",
        status=SessionStatus.RUNNING,
        created_at=datetime_for_tests(),
        last_activity=datetime_for_tests(),
    )
    sm.backend = MagicMock()
    sm.backend.is_alive = MagicMock(return_value=True)
    sm.backend.tmux_session = "cloude_prior"

    # Track detach call. The old destroy path must NOT be invoked —
    # destruction is reserved for the explicit destroy button.
    detach_mock = AsyncMock(return_value=True)
    sm.detach_current_session = detach_mock  # type: ignore[assignment]
    destroy_mock = AsyncMock(return_value=True)
    sm.destroy_session = destroy_mock  # type: ignore[assignment]

    # Stub _resolve_external_cwd so we don't actually shell out.
    sm._resolve_external_cwd = AsyncMock(return_value=Path("/tmp"))  # type: ignore[assignment]

    # Mock the TmuxBackend.for_external classmethod so no real tmux work runs.
    fake_backend = MagicMock()
    fake_backend.attach_existing = AsyncMock(return_value=None)
    fake_backend.capture_scrollback = MagicMock(return_value=b"")
    fake_backend._pipe_path = Path("/tmp/does_not_exist_for_test.pipe")

    with patch(
        "src.core.tmux_backend.TmuxBackend.for_external",
        return_value=fake_backend,
    ), patch(
        "src.core.session_manager.settings"
    ) as mock_settings:
        # Stub auth config so load_auth_config().session.* resolves.
        auth_cfg = MagicMock()
        auth_cfg.session.tmux_socket_name = "cloude"
        auth_cfg.session.scrollback_lines = 3000
        auth_cfg.notifications.idle_threshold_seconds = 30.0
        mock_settings.load_auth_config.return_value = auth_cfg

        result = await sm.adopt_external_session(
            "external_target",
            confirm_detach=True,
        )

    detach_mock.assert_awaited_once(), (
        "detach_current_session must be awaited on confirmed detach"
    )
    destroy_mock.assert_not_awaited(), (
        "destroy_session must NEVER be called on adopt-swap — "
        "switching is detach-only; destruction is only via the destroy button"
    )
    assert "session" in result, "adopt response must contain 'session'"
    assert "initial_scrollback_b64" in result, "adopt response must contain 'initial_scrollback_b64'"
    assert "fifo_start_offset" in result, "adopt response must contain 'fifo_start_offset'"
    fake_backend.attach_existing.assert_awaited_once()


def datetime_for_tests():
    """Small helper — imports datetime only at call site to avoid top clutter."""
    from datetime import datetime
    return datetime.utcnow()


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
        auth_cfg.session.tmux_socket_name = "cloude"
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
    sm.session = Session(
        id="foo",
        pty_pid=None,
        working_dir=str(tmp_path),
        status=SessionStatus.RUNNING,
        created_at=datetime_for_tests(),
        last_activity=datetime_for_tests(),
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
        # Stub _clear_stale_metadata so we don't touch the filesystem.
        sm._clear_stale_metadata = MagicMock(  # type: ignore[assignment]
            side_effect=lambda: setattr(sm, "session", None),
        )
        await sm.lifespan_startup()

    # The hydrate backend's attach_existing must NOT have been called —
    # ownership gate must block it.
    attach_mock.assert_not_awaited()
    # And the backend attribute must NOT have been swapped to the hydrate
    # candidate — sm.backend stays unchanged (None here).
    assert sm.backend is None, (
        "non-owned session must not be registered as the active backend"
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

    sm.session = Session(
        id="legacy_sess",
        pty_pid=None,
        working_dir=str(tmp_path),
        status=SessionStatus.RUNNING,
        created_at=datetime_for_tests(),
        last_activity=datetime_for_tests(),
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
        ["tmux", "-L", "cloude", "new-session", "-d", "-s", name],
        check=True,
        capture_output=True,
    )
    # Build SM — _load_session_metadata is best-effort; patch to no-op
    # so a stray real session_metadata.json doesn't taint this test.
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    try:
        subprocess.run(
            ["tmux", "-L", "cloude", "send-keys", "-t",
             f"{name}:0.0", f"echo {marker}", "Enter"],
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
            ["tmux", "-L", "cloude", "kill-session", "-t", name],
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
            ["tmux", "-L", "cloude", "has-session", "-t", tmux_name],
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
            ["tmux", "-L", "cloude", "has-session", "-t", tmux_name],
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
            ["tmux", "-L", "cloude", "kill-session", "-t",
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
            ["tmux", "-L", "cloude", "has-session", "-t", tmux_name_a],
            capture_output=True,
        )
        assert alive_a_before.returncode == 0, (
            "baseline: prior session A must be alive before adopt-swap"
        )

        # --- Step 2: spawn external session B (the target of the adopt).
        subprocess.run(
            ["tmux", "-L", "cloude", "new-session", "-d", "-s", name_b],
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
            ["tmux", "-L", "cloude", "has-session", "-t", tmux_name_a],
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
                ["tmux", "-L", "cloude", "kill-session", "-t", tname],
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
                    ["tmux", "-L", "cloude", "kill-session",
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


# ---- Task 5: adopt-on-collision -----------------------------------------


@requires_tmux
@pytest.mark.asyncio
async def test_create_session_adopts_when_target_name_exists(tmp_path, monkeypatch):
    """If a tmux session with the derived name already exists on our
    socket, create_session should adopt it rather than fail with 'already
    running' or create a duplicate."""
    from src.core.session_manager import SessionManager

    # Sandbox so we don't touch the real metadata file.
    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path / "logs"))

    project_name = f"adopt_collide_{secrets.token_hex(4)}"
    target_tmux = f"cloude_{project_name}"

    # Pre-create the tmux session on our socket so the collision fires.
    subprocess.run(
        ["tmux", "-L", "cloude", "new-session", "-d", "-s", target_tmux],
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
            assert sm.backend.tmux_session == target_tmux, (
                f"expected adopted tmux_session {target_tmux!r}, "
                f"got {sm.backend.tmux_session!r}"
            )
            # Session existed BEFORE create_session — verify not wiped.
            assert subprocess.call(
                ["tmux", "-L", "cloude", "has-session", "-t", target_tmux],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) == 0, "adopted tmux session was unexpectedly killed"
        finally:
            if sm.backend is not None:
                try:
                    await sm.detach_current_session()
                except Exception:
                    pass
    finally:
        subprocess.run(
            ["tmux", "-L", "cloude", "kill-session", "-t", target_tmux],
            check=False,
            capture_output=True,
        )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
