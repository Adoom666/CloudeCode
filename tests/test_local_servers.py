"""Tests for the Local Servers detector + tightened pattern regex.

Plan v3.2: replaces ``tests/test_tunnel_manager.py``. The tunnel system
was demolished in favor of a detection-only ``LocalServersTracker``; the
critical regression we MUST guard against is the loose port regex that
used to match digits in unrelated TUI text (``"15.3k tokens"``,
``"31% context left"``) and bind tunnels for nonsense privileged ports.

Coverage:
    - false-positive resistance on TUI status text
    - valid extraction across Vite / node / flask / generic URL shapes
    - ``is_valid_dev_port`` privileged-port rejection
    - ``LocalServersTracker.record`` gates on listener probe
    - ``LocalServersTracker.clear_session`` wipes per-session state
    - janitor sweep removes entries whose listener has died
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.core import local_servers as ls_module
from src.core.local_servers import LocalServersTracker
from src.utils.patterns import (
    PatternDetector,
    extract_port,
    is_valid_dev_port,
    port_is_listening,
)


# --------------------------------------------------------------------------- #
# Pattern regression tests
# --------------------------------------------------------------------------- #


def test_extract_port_rejects_tui_status_noise():
    """The Claude TUI status row used to false-positive — must not regress."""
    assert extract_port("running ✦ 15.3k tokens · 31% context left") is None


def test_extract_port_rejects_percent_and_kilo_shapes():
    """``31%`` and ``15.3k`` were the original false-positive shapes."""
    assert extract_port("Build 31% complete") is None
    assert extract_port("Bundle size 15.3k") is None


def test_extract_port_vite_localhost_url():
    assert extract_port("  Local:   http://localhost:5173/") == 5173


def test_extract_port_node_listening_on_port():
    assert extract_port("Listening on port 3000") == 3000


def test_extract_port_flask_running_on_url():
    assert extract_port("Running on http://0.0.0.0:8000") == 8000


def test_extract_port_generic_https_url():
    assert extract_port("Server reachable at https://example.dev:8443/") == 8443


def test_extract_port_ipv4_loopback_in_url():
    assert extract_port("Now serving http://127.0.0.1:4200") == 4200


def test_extract_port_ipv6_localhost():
    assert extract_port("Server: http://[::1]:9090/") == 9090


def test_extract_port_serving_at_port():
    assert extract_port("Serving at port 7000") == 7000


def test_extract_port_bound_on_port():
    assert extract_port("bound on 5500") == 5500


def test_extract_port_is_validated_through_dev_port_gate():
    """A regex hit on a privileged port must NOT survive ``extract_port``."""
    assert extract_port("Local: http://localhost:80/") is None
    assert extract_port("Listening on port 22") is None


def test_is_valid_dev_port_boundaries():
    assert is_valid_dev_port(80) is False
    assert is_valid_dev_port(1023) is False
    assert is_valid_dev_port(1024) is True
    assert is_valid_dev_port(8080) is True
    assert is_valid_dev_port(65535) is True
    assert is_valid_dev_port(65536) is False
    assert is_valid_dev_port(0) is False
    assert is_valid_dev_port(-1) is False
    assert is_valid_dev_port("not-an-int") is False  # type: ignore[arg-type]


def test_pattern_detector_listening_on_port_does_not_match_running_tui():
    """``running`` was dropped as a verb; confirm the detector doesn't pick it up."""
    detector = PatternDetector()
    matches = detector.detect_patterns("running ✦ 15.3k tokens · 31% context left")
    assert all(m.pattern_name != "listening_on_port" for m in matches)


def test_port_is_listening_negative_when_nothing_bound():
    """A high random port should fail the listener probe."""
    # 65111 is unlikely to be bound on a CI host; if it ever is the test
    # tolerates a flake by retrying with a different unlikely port.
    for candidate in (65111, 64777, 63555):
        if not port_is_listening(candidate, timeout_ms=50):
            return
    pytest.skip("Could not find a free port to probe — environment too crowded")


# --------------------------------------------------------------------------- #
# LocalServersTracker
# --------------------------------------------------------------------------- #


@pytest.fixture
def tracker():
    return LocalServersTracker()


@pytest.mark.asyncio
async def test_record_skips_invalid_port(tracker):
    """Privileged ports never enter the tracker, even when probe is mocked True."""
    with patch.object(ls_module, "port_is_listening", return_value=True):
        result = await tracker.record("session-a", 80)
    assert result is None
    assert tracker.list_for_session("session-a") == []


@pytest.mark.asyncio
async def test_record_skips_when_listener_absent(tracker):
    """A valid dev port with no listener gets dropped silently."""
    with patch.object(ls_module, "port_is_listening", return_value=False):
        result = await tracker.record("session-a", 5173)
    assert result is None
    assert tracker.list_for_session("session-a") == []


@pytest.mark.asyncio
async def test_record_adds_when_listener_present(tracker):
    """Successful detect — entry shows up under the session and event fires."""
    queue = tracker.subscribe()
    with patch.object(ls_module, "port_is_listening", return_value=True):
        result = await tracker.record("session-a", 5173)

    assert result is not None
    assert result.port == 5173
    assert result.url.startswith("http://")
    entries = tracker.list_for_session("session-a")
    assert len(entries) == 1
    assert entries[0].port == 5173

    # Event broadcast
    raw = queue.get_nowait()
    assert '"local_server_detected"' in raw
    assert '"port": 5173' in raw


@pytest.mark.asyncio
async def test_record_is_idempotent(tracker):
    """Re-detection of an already-tracked port must NOT re-broadcast."""
    queue = tracker.subscribe()
    with patch.object(ls_module, "port_is_listening", return_value=True):
        await tracker.record("session-a", 5173)
        await tracker.record("session-a", 5173)
    # First detect emits one event; the second should be a refresh-only
    # no-op as far as broadcasts go.
    queue.get_nowait()
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


@pytest.mark.asyncio
async def test_clear_session_wipes_state_and_emits_lost(tracker):
    queue = tracker.subscribe()
    with patch.object(ls_module, "port_is_listening", return_value=True):
        await tracker.record("session-a", 5173)
        await tracker.record("session-a", 3000)
    # drain detected events
    queue.get_nowait()
    queue.get_nowait()

    await tracker.clear_session("session-a")

    assert tracker.list_for_session("session-a") == []
    # Two LOST events expected (order: insertion order)
    msg1 = queue.get_nowait()
    msg2 = queue.get_nowait()
    assert '"local_server_lost"' in msg1
    assert '"local_server_lost"' in msg2


@pytest.mark.asyncio
async def test_forget_returns_false_when_entry_missing(tracker):
    assert await tracker.forget("nope", 1234) is False


@pytest.mark.asyncio
async def test_janitor_sweeps_dead_entries():
    """Janitor must drop entries whose listener stopped responding."""
    # Patch the constant so the test doesn't sit for 30 seconds.
    with patch.object(ls_module, "JANITOR_INTERVAL_SECONDS", 0.1):
        tracker = LocalServersTracker()
        queue = tracker.subscribe()
        # First the record path probes True; then the janitor probes False.
        probe_returns = iter([True, False, False, False])

        def probe(port, timeout_ms=250):
            try:
                return next(probe_returns)
            except StopIteration:
                return False

        with patch.object(ls_module, "port_is_listening", side_effect=probe):
            await tracker.record("session-a", 5173)
            assert tracker.list_for_session("session-a")
            # Drain detected event
            queue.get_nowait()

            await tracker.start()
            # Wait long enough for at least one janitor tick.
            await asyncio.sleep(0.4)
            await tracker.stop()

    assert tracker.list_for_session("session-a") == []


@pytest.mark.asyncio
async def test_snapshot_returns_per_session_map(tracker):
    with patch.object(ls_module, "port_is_listening", return_value=True):
        await tracker.record("session-a", 5173)
        await tracker.record("session-b", 3000)
    snap = tracker.snapshot()
    assert set(snap.keys()) == {"session-a", "session-b"}
    assert snap["session-a"][0].port == 5173
    assert snap["session-b"][0].port == 3000
