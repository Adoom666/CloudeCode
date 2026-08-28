"""Regression tests for replayed-scrollback line endings.

THE BUG THESE EXIST FOR (fix/scroll-render-jank, 2026-08-16)

``tmux capture-pane -p`` separates lines with a BARE LF. The client
replays those bytes into an xterm built with ``convertEol: false``, where
a bare LF means "move down one row, KEEP the column". Reproduced on an
iPhone 16e simulator (iOS 26.1, DPR 3, 43x34 grid): writing
``"AAAAAAAAAA\\nBBBBBBBBBB\\nCCCCCCCCCC"`` into the live terminal produced

    AAAAAAAAAA
              BBBBBBBBBB
                        CCCCCCCCCC

and the xterm buffer dump matched the screen exactly, which is what makes
it a BUFFER bug rather than a paint bug. Every adopted or rejoined
session's replayed history was staircased this way; freshly streamed PTY
output was always clean because a real PTY sends ``\\r\\n``.

The tests below pin the two halves of the fix: the normalizer's
semantics, and the fact that ``TmuxBackend.capture_scrollback`` actually
applies it (the half that would silently rot if someone rewrote the
capture path).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_sbn_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_sbn_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.scrollback_replay import normalize_replay_newlines  # noqa: E402
from src.core import tmux_backend as tmux_backend_mod  # noqa: E402


# ---- the normalizer ----------------------------------------------------

def test_bare_lf_becomes_crlf():
    """The staircase case: capture-pane's bare LFs must become CRLF."""
    assert normalize_replay_newlines(b"AAA\nBBB\nCCC") == b"AAA\r\nBBB\r\nCCC"


def test_existing_crlf_is_untouched():
    """Already-correct input must not gain a second carriage return."""
    assert normalize_replay_newlines(b"AAA\r\nBBB\r\n") == b"AAA\r\nBBB\r\n"


def test_normalization_is_idempotent():
    """Applying it twice equals applying it once."""
    once = normalize_replay_newlines(b"AAA\nBBB\r\nCCC\n")
    assert normalize_replay_newlines(once) == once


def test_mixed_endings_all_land_on_crlf():
    """A capture that mixes both styles comes out uniform."""
    assert (
        normalize_replay_newlines(b"one\ntwo\r\nthree\nfour")
        == b"one\r\ntwo\r\nthree\r\nfour"
    )


def test_lone_carriage_return_is_preserved():
    """A bare CR is a real cursor command (progress bars, spinners) and
    must survive untouched - it is not a line ending to rewrite."""
    assert normalize_replay_newlines(b"50%\r100%") == b"50%\r100%"


def test_ansi_escapes_survive():
    """``capture-pane -e`` emits SGR sequences. None of them embed a raw
    LF, so a byte-level substitution must leave them byte-identical."""
    src = b"\x1b[1;32mgreen\x1b[0m\nplain\n"
    assert normalize_replay_newlines(src) == b"\x1b[1;32mgreen\x1b[0m\r\nplain\r\n"


def test_empty_input_returns_empty():
    """The capture-failed path returns b"" and must stay b""."""
    assert normalize_replay_newlines(b"") == b""


def test_no_newlines_is_unchanged():
    assert normalize_replay_newlines(b"single line") == b"single line"


# ---- the wiring --------------------------------------------------------

class _StubBackend:
    """Minimal stand-in exposing only what capture_scrollback touches."""

    scrollback_lines = 3000
    tmux_session = "cloude_probe"
    replay_in_progress = False

    def __init__(self, payload: bytes, rc: int = 0):
        self._payload = payload
        self._rc = rc
        self.args: tuple = ()

    def _run_tmux_sync(self, *args, **kwargs):
        self.args = args
        return self._rc, self._payload, b""


def test_capture_scrollback_normalizes_its_output():
    """The real defect: capture_scrollback must not hand bare LFs to the
    client, however the tmux call itself is spelled."""
    stub = _StubBackend(b"drwxr-xr-x  jsugamele\ntotal 12\n")
    out = tmux_backend_mod.TmuxBackend.capture_scrollback(stub, lines=10)
    assert out == b"drwxr-xr-x  jsugamele\r\ntotal 12\r\n"
    assert b"\n" not in out.replace(b"\r\n", b"")


def test_capture_scrollback_failure_still_returns_empty():
    """A non-zero tmux exit keeps returning b"" - the normalizer must not
    turn a failure into a success."""
    stub = _StubBackend(b"whatever\n", rc=1)
    assert tmux_backend_mod.TmuxBackend.capture_scrollback(stub, lines=10) == b""


@pytest.mark.parametrize("flag", ["-p", "-e", "-J"])
def test_capture_scrollback_keeps_its_tmux_flags(flag):
    """-J joins tmux's hardware wraps into logical lines, which is only
    correct once every line actually starts at column 0. Guard the pair."""
    stub = _StubBackend(b"x\n")
    tmux_backend_mod.TmuxBackend.capture_scrollback(stub, lines=10)
    assert flag in stub.args
