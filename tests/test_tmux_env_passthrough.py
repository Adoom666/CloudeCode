"""The pane's environment, and why merging it into the client is not enough.

THE DEFECT. ``TmuxBackend.start`` merged the caller's env overlay into the
tmux INVOCATION's environment, under a comment asserting "tmux captures the
environment of the new-session call". That is true only when the call is
what STARTS the server. When a server is already running on the socket -
every session after the first - the new session's environment comes from
the SERVER's global table and the client's environment is discarded.

WHY IT WAS INVISIBLE. The failure was not a MISSING variable, which would
have shown up immediately. It was a STALE one. Every session after the
first inherited the ``CLOUDECODE_SESSION_ID`` captured when the server
started, so Claude's SessionStart hook POSTed a session id belonging to a
different, long-dead session. The hook fired, the HTTP call succeeded, and
the binding resolved UNRESOLVED - so ``claude_session_uuid`` was never
written and everything that needs it (resume, fork) could never work.
Measured on a real install: a pane created today carried an id from six
days earlier.

The locale block in the same function already knew this and used ``-e``.
The lesson had been learned for LANG and not applied to anything else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
import ast


def _start_source() -> str:
    """The source of TmuxBackend.start."""
    tree = ast.parse((ROOT / "src/core/tmux_backend.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "start":
            return ast.get_source_segment(
                (ROOT / "src/core/tmux_backend.py").read_text(), node
            ) or ""
    raise AssertionError("TmuxBackend.start not found")


def test_the_env_overlay_is_emitted_as_dash_e_pairs():
    """The whole fix, asserted at the source.

    A behavioural test would need a real tmux server already running on a
    socket to reproduce the discard, which is exactly the setup the bug
    hid behind. The structural check is cheap and names the mechanism.
    """
    src = _start_source()
    assert 'args.extend(["-e", f"{key}={text}"])' in src, (
        "the caller's env overlay is no longer emitted as -e pairs; without "
        "that, every session after the first inherits the SERVER's stale copy"
    )


def test_only_the_callers_keys_travel_not_the_whole_environment():
    """The overlay is the caller's dict, never os.environ.

    tmux_env is a copy of the process environment and is still passed as
    the subprocess env - that part is fine. What must NOT happen is
    iterating it into -e pairs, which would push the app's entire
    environment into a user's pane.
    """
    src = _start_source()
    assert "for key, value in sorted((env or {}).items()):" in src
    assert "for key, value in sorted(tmux_env.items())" not in src


def test_the_stale_premise_comment_is_gone():
    """The comment asserted the wrong mechanism and is why this survived.

    It is allowed to be QUOTED while being corrected - that is how the next
    reader learns it - so the check is that it no longer stands alone as an
    unqualified claim.
    """
    src = _start_source()
    if "tmux captures the environment of the" in src:
        assert "true only when this" in src or "THE COMMENT THAT USED TO BE HERE" in src, (
            "the wrong premise is stated without its correction"
        )


def test_a_value_with_a_newline_is_skipped_not_truncated():
    """tmux takes one KEY=VALUE per -e.

    A newline makes the remainder unparseable, and half an environment
    variable reaching a pane is worse than none - it would look set.
    """
    src = _start_source()
    assert 'if "\\n" in text or "\\r" in text' in src
    assert "tmux_env_pair_skipped" in src


def test_the_locale_still_travels_as_dash_e():
    """The case that was already right must stay right."""
    src = _start_source()
    assert 'args.extend(["-e", f"LANG={pane_lang}"])' in src
