"""Unit tests for TerminalSizeNegotiator (src/core/terminal_size.py).

WHY THIS EXISTS. Two browsers attached to the same tmux session both send
pty_resize; without arbitration the last write wins and the pane flaps
between whichever client resized most recently. This pins the negotiation
rule in isolation, with no tmux/websocket/asyncio involved -- see
src/core/terminal_size.py's module docstring for the full design rationale.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.terminal_size import TerminalSizeNegotiator  # noqa: E402


def test_single_client_effective_size_equals_its_own_request():
    """One client's effective size is exactly what it asked for.

    This is the "behavior unchanged from today" case: with only one
    browser attached, negotiation must be a no-op pass-through.
    """
    neg = TerminalSizeNegotiator()
    result = neg.set_client_size("sess-1", "client-a", 100, 40)

    assert result == (100, 40)
    assert neg.effective_size("sess-1") == (100, 40)
    assert neg.client_count("sess-1") == 1


def test_two_clients_effective_is_elementwise_minimum():
    """Two differently-sized clients negotiate down to the smaller size."""
    neg = TerminalSizeNegotiator()
    neg.set_client_size("sess-1", "client-a", 200, 50)
    result = neg.set_client_size("sess-1", "client-b", 90, 30)

    assert result == (90, 30)
    assert neg.effective_size("sess-1") == (90, 30)
    assert neg.client_count("sess-1") == 2


def test_small_client_disconnect_grows_effective_size_back():
    """When the constraining (smaller) client leaves, the pane grows back."""
    neg = TerminalSizeNegotiator()
    neg.set_client_size("sess-1", "client-a", 200, 50)
    neg.set_client_size("sess-1", "client-b", 90, 30)
    assert neg.effective_size("sess-1") == (90, 30)

    result = neg.remove_client("sess-1", "client-b")

    assert result == (200, 50)
    assert neg.effective_size("sess-1") == (200, 50)
    assert neg.client_count("sess-1") == 1


def test_large_client_disconnect_effective_size_stays_small():
    """The larger client leaving must not change the negotiated minimum."""
    neg = TerminalSizeNegotiator()
    neg.set_client_size("sess-1", "client-a", 200, 50)
    neg.set_client_size("sess-1", "client-b", 90, 30)

    result = neg.remove_client("sess-1", "client-a")

    # Effective size is unchanged (still bound by client-b) -> None.
    assert result is None
    assert neg.effective_size("sess-1") == (90, 30)
    assert neg.client_count("sess-1") == 1


def test_last_client_disconnect_leaves_no_clients_and_does_not_crash():
    """Removing the only remaining client cleans up without raising."""
    neg = TerminalSizeNegotiator()
    neg.set_client_size("sess-1", "client-a", 100, 40)

    result = neg.remove_client("sess-1", "client-a")

    assert result is None
    assert neg.effective_size("sess-1") is None
    assert neg.client_count("sess-1") == 0

    # Session entry must be cleaned up, not left as an empty dict -- removing
    # an already-gone client again is a safe no-op, not a crash either.
    assert neg.remove_client("sess-1", "client-a") is None


def test_mixed_axes_take_minimum_per_axis_not_per_client():
    """One client narrower-but-taller than another: min is per-axis.

    client-a is narrower (80 cols) but taller (60 rows) than client-b (120
    cols, 20 rows). The effective size must combine the narrower cols with
    the shorter rows, even though no single client actually has (80, 20).
    """
    neg = TerminalSizeNegotiator()
    neg.set_client_size("sess-1", "client-a", 80, 60)
    result = neg.set_client_size("sess-1", "client-b", 120, 20)

    assert result == (80, 20)
    assert neg.effective_size("sess-1") == (80, 20)


def test_non_positive_dimensions_are_ignored():
    """Zero or negative cols/rows must not corrupt the tracked minimum."""
    neg = TerminalSizeNegotiator()
    neg.set_client_size("sess-1", "client-a", 100, 40)

    result_zero = neg.set_client_size("sess-1", "client-a", 0, 40)
    result_negative = neg.set_client_size("sess-1", "client-a", 100, -5)

    assert result_zero is None
    assert result_negative is None
    assert neg.effective_size("sess-1") == (100, 40)
    assert neg.client_count("sess-1") == 1


def test_second_client_reporting_a_larger_size_does_not_change_effective():
    """A client that isn't the constraint reports a no-op, not a resize.

    Guards the "skip a redundant backend resize" contract: if the new
    client's size doesn't move the minimum on either axis, callers should
    see None and not re-issue an unnecessary tmux resize.
    """
    neg = TerminalSizeNegotiator()
    neg.set_client_size("sess-1", "client-a", 80, 24)
    result = neg.set_client_size("sess-1", "client-b", 200, 60)

    assert result is None
    assert neg.effective_size("sess-1") == (80, 24)


def test_sessions_are_independent():
    """Two different sessions never influence each other's negotiation."""
    neg = TerminalSizeNegotiator()
    neg.set_client_size("sess-1", "client-a", 80, 24)
    neg.set_client_size("sess-2", "client-a", 200, 60)

    assert neg.effective_size("sess-1") == (80, 24)
    assert neg.effective_size("sess-2") == (200, 60)
    assert neg.client_count("sess-1") == 1
    assert neg.client_count("sess-2") == 1
