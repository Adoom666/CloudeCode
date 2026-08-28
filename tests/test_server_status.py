"""Unit tests for the server-status collectors.

WHAT THESE GUARD, in order of how much they have cost this project:

1. THE THIRD OUTCOME. Every collector must be able to say "I could not
   measure this", and that state must never be indistinguishable from a
   healthy reading. A probe that returns 0 bytes used because the helper
   was missing is the false-green class this repo keeps paying for, so
   the failure paths are asserted at least as hard as the happy ones.

2. TMUX WITH NO SERVER IS NOT AN ERROR. It is the ordinary state before
   the first session, and it must report ``server_running: False`` with
   ``available: True``. A tmux that could not run AT ALL must report
   ``server_running: None`` instead, because those are different facts.

3. OWNERSHIP IS MERGED, NEVER DERIVED. ``merge_ownership`` must take the
   server's verdict verbatim, key it by NAME, and leave a name it was not
   told about as None rather than False. The bug this replaces made an
   app-created session badge as external after a restart, because it read
   ownership off the ``adopted:`` id prefix.

4. THE PARSER IS BOUNDED. A working directory containing the field
   separator must not shift the fields in front of it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_status_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_status_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core import host_metrics, server_status
from tests.socket_guard import derive_test_socket

SEP = server_status._TMUX_FIELD_SEP


# --------------------------------------------------------------------------
# parse_etime - macOS ps has no etimes keyword, so this parser is the only
# way process uptime is known at all.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("00:05", 5),
        ("01:02", 62),
        ("1:02:03", 3723),
        ("2-03:04:05", 183845),
        ("  10:00  ", 600),
    ],
)
def test_parse_etime_accepts_every_ps_shape(raw, expected):
    assert server_status.parse_etime(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "1:2:3:4", "x-01:02", "5"])
def test_parse_etime_returns_none_rather_than_zero(raw):
    """Unparseable input is unknown, not a process that just started."""
    assert server_status.parse_etime(raw) is None


def test_process_uptime_is_measured_for_this_process():
    seconds = server_status.process_uptime_seconds(os.getpid())
    assert seconds is not None and seconds >= 0


def test_process_uptime_of_a_dead_pid_is_unknown():
    """A pid ps cannot report on is unknown, never 0 seconds of uptime."""
    assert server_status.process_uptime_seconds(999999) is None


# --------------------------------------------------------------------------
# parse_sessions - the bounded split
# --------------------------------------------------------------------------

def test_parse_sessions_reads_every_field():
    line = SEP.join(["work", "1700000000", "2", "120", "40", "3", "/tmp/x"])
    rows = server_status.parse_sessions(line)
    assert rows == [{
        "name": "work",
        "created_at_epoch": 1700000000,
        "attached_clients": 2,
        "pane_cols": 120,
        "pane_rows": 40,
        "window_count": 3,
        "working_dir": "/tmp/x",
    }]


def test_parse_sessions_split_is_bounded_so_a_path_cannot_shift_fields():
    """A separator inside the working dir stays inside the working dir."""
    weird = "/tmp/a" + SEP + "b"
    line = SEP.join(["s", "1", "0", "80", "24", "1", weird])
    row = server_status.parse_sessions(line)[0]
    assert row["name"] == "s"
    assert row["pane_cols"] == 80
    assert row["working_dir"] == weird


def test_parse_sessions_skips_short_rows_without_inventing_fields():
    rows = server_status.parse_sessions("only" + SEP + "two\n")
    assert rows == []


def test_parse_sessions_keeps_a_name_with_spaces_intact():
    line = SEP.join(["test pause", "1", "0", "80", "24", "1", "/tmp"])
    assert server_status.parse_sessions(line)[0]["name"] == "test pause"


def test_parse_sessions_non_numeric_field_degrades_to_zero_not_a_crash():
    line = SEP.join(["s", "notanumber", "0", "80", "24", "1", "/tmp"])
    assert server_status.parse_sessions(line)[0]["created_at_epoch"] == 0


# --------------------------------------------------------------------------
# merge_ownership - the one that has already shipped wrong twice
# --------------------------------------------------------------------------

def _rows(*names):
    return [{"name": n} for n in names]


def test_merge_ownership_takes_the_servers_verdict_verbatim():
    rows = server_status.merge_ownership(
        _rows("mine", "theirs"),
        {"mine": True, "theirs": False},
        {},
    )
    assert [r["created_by_cloude"] for r in rows] == [True, False]


def test_merge_ownership_ignores_the_adopted_id_prefix_entirely():
    """An app-created session re-adopted after a restart is still ours.

    Its id is ``adopted:<name>`` but its NAME is in owned_tmux_sessions,
    so the server says True and this must not second-guess it.
    """
    rows = server_status.merge_ownership(
        _rows("cloude_ses_ec5bf2a3"),
        {"cloude_ses_ec5bf2a3": True},
        {"cloude_ses_ec5bf2a3": "adopted:cloude_ses_ec5bf2a3"},
    )
    assert rows[0]["created_by_cloude"] is True
    assert rows[0]["session_id"] == "adopted:cloude_ses_ec5bf2a3"
    assert rows[0]["open_in_app"] is True


def test_merge_ownership_unknown_name_is_none_never_false():
    """Absent from the mapping means we could not determine it."""
    rows = server_status.merge_ownership(_rows("ghost"), {}, {})
    assert rows[0]["created_by_cloude"] is None


def test_merge_ownership_marks_sessions_not_open_in_this_process():
    rows = server_status.merge_ownership(_rows("idle"), {"idle": False}, {})
    assert rows[0]["open_in_app"] is False
    assert rows[0]["session_id"] is None


# --------------------------------------------------------------------------
# collect_tmux - three outcomes, not two
# --------------------------------------------------------------------------

def test_collect_tmux_no_server_is_available_and_not_running(monkeypatch):
    monkeypatch.setattr(
        server_status, "_run_full",
        lambda argv: (1, "", "no server running on /tmp/tmux-501/cloude"),
    )
    out = server_status.collect_tmux("cloude")
    assert out["available"] is True
    assert out["server_running"] is False
    assert out["sessions"] == []
    assert out["error"] is None


def test_collect_tmux_socket_never_created_is_also_not_an_error(monkeypatch):
    monkeypatch.setattr(
        server_status, "_run_full",
        lambda argv: (1, "", "error connecting to /x (No such file or directory)"),
    )
    assert server_status.collect_tmux("cloude")["server_running"] is False


def test_collect_tmux_real_failure_is_unavailable_not_empty(monkeypatch):
    monkeypatch.setattr(
        server_status, "_run_full",
        lambda argv: (1, "", "server exited unexpectedly"),
    )
    out = server_status.collect_tmux("cloude")
    assert out["available"] is False
    assert out["server_running"] is None
    assert "server exited unexpectedly" in out["error"]


def test_collect_tmux_unrunnable_reports_could_not_determine(monkeypatch):
    monkeypatch.setattr(server_status, "_run_full", lambda argv: None)
    out = server_status.collect_tmux("cloude")
    assert out["available"] is False
    assert out["server_running"] is None


def test_collect_tmux_missing_binary_is_unavailable(monkeypatch):
    monkeypatch.setattr(server_status.shutil, "which", lambda _n: None)
    out = server_status.collect_tmux("cloude")
    assert out["available"] is False
    assert "tmux is not installed" in out["error"]


def test_collect_tmux_history_limit_unknown_is_none_not_the_default(monkeypatch):
    """2000 is tmux's default; guessing it would invent a measurement."""
    def fake(argv):
        if "list-sessions" in argv:
            return (0, "", "")
        return (1, "", "nope")
    monkeypatch.setattr(server_status, "_run_full", fake)
    assert server_status.collect_tmux("cloude")["history_limit"] is None


# --------------------------------------------------------------------------
# claude cli and commit
# --------------------------------------------------------------------------

def test_claude_cli_not_found_is_unavailable_not_an_empty_version(monkeypatch):
    monkeypatch.setattr(server_status.shutil, "which", lambda _n: None)
    monkeypatch.setattr(server_status.os.path, "exists", lambda _p: False)
    out = server_status.collect_claude_cli()
    assert out["available"] is False
    assert "version" not in out


def test_commit_outside_a_checkout_is_unavailable(tmp_path):
    out = server_status.collect_commit(tmp_path)
    assert out["available"] is False
    assert out["error"] == "not a git checkout"


# --------------------------------------------------------------------------
# host metrics
# --------------------------------------------------------------------------

def test_memory_is_internally_consistent():
    mem = host_metrics.collect_memory()
    if not mem["available"]:
        pytest.skip(f"no memory probe here: {mem['error']}")
    assert mem["total_bytes"] > 0
    assert mem["used_bytes"] + mem["available_bytes"] == mem["total_bytes"]
    assert 0 <= mem["used_percent"] <= 100


def test_disk_on_a_missing_path_is_unavailable():
    out = host_metrics.collect_disk("/no/such/path/at/all")
    assert out["available"] is False
    assert "free_bytes" not in out


def test_load_reports_the_core_count_alongside_the_average():
    """A load of 8 means nothing without knowing how many cores there are."""
    load = host_metrics.collect_load()
    if not load["available"]:
        pytest.skip(f"no loadavg here: {load['error']}")
    assert load["cpu_count"] >= 1


def test_unavailable_helper_never_claims_a_reading():
    out = host_metrics.unavailable("because")
    assert out == {"available": False, "error": "because"}


# A socket name this run owns but never creates a server on. Exercises
# the "no server" branch without naming a socket the guard cannot vouch
# for.
_ABSENT_SOCKET = derive_test_socket("absent")


def test_collect_returns_every_section():
    snap = server_status.collect(
        host="127.0.0.1", port=8001, socket_name=_ABSENT_SOCKET,
        ownership_by_name={}, open_ids_by_name={},
    )
    for section in ("server", "tmux", "claude_cli", "host", "memory", "disk", "load"):
        assert section in snap, section
        assert "available" in snap[section], section
    assert snap["server"]["lan_reachable"] is False


def test_collect_flags_a_wildcard_bind_as_lan_reachable():
    snap = server_status.collect(
        host="0.0.0.0", port=8000, socket_name=_ABSENT_SOCKET,
        ownership_by_name={}, open_ids_by_name={},
    )
    assert snap["server"]["lan_reachable"] is True
