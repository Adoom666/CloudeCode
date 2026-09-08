"""Rule 1 of the correlation ladder: --resume <uuid> in the pane's argv.

Fixtures build a synthetic ``ProcessRow`` table rather than shelling out
to real ``ps`` (macOS-only and non-deterministic across machines), per
``find_resume_uuid_in_tree`` being a pure function over an already-read
snapshot. ``list_process_table`` itself is exercised once, live, in
``test_list_process_table_reads_real_ps`` - this machine is macOS, so a
real ``ps`` call is a fair smoke test, not a fixture substitute.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.claude_resume_argv import (
    ProcessRow,
    find_resume_uuid_in_tree,
    list_process_table,
)

RESUME_UUID = "82854c0e-a423-4591-a34f-a14cb92fbf41"


def test_pane_pid_is_claude_directly_with_resume():
    """Topology 1: tmux ran the claude binary as the pane's own pid."""
    processes = [
        ProcessRow(
            pid=99871,
            ppid=1,
            command=(
                "/Users/jsugamele/.local/bin/claude "
                f"--resume {RESUME_UUID} --dangerously-skip-permissions"
            ),
        ),
    ]
    result = find_resume_uuid_in_tree(99871, processes)
    assert result == RESUME_UUID


def test_pane_pid_is_a_shell_claude_is_a_child():
    """Topology 2: the pane's pid is a shell, claude is its child."""
    processes = [
        ProcessRow(pid=59113, ppid=1, command="/bin/zsh -il"),
        ProcessRow(
            pid=59314,
            ppid=59113,
            command=f"claude --resume {RESUME_UUID} --dangerously-skip-permissions",
        ),
    ]
    result = find_resume_uuid_in_tree(59113, processes)
    assert result == RESUME_UUID


def test_grandchild_claude_is_still_found():
    """A deeper descent (shell -> wrapper -> claude) is still walked."""
    processes = [
        ProcessRow(pid=1, ppid=0, command="/bin/zsh -il"),
        ProcessRow(pid=2, ppid=1, command="/usr/bin/env some-wrapper"),
        ProcessRow(pid=3, ppid=2, command=f"claude --resume {RESUME_UUID}"),
    ]
    assert find_resume_uuid_in_tree(1, processes) == RESUME_UUID


def test_no_resume_falls_through_with_no_match():
    """Born-in-pane case: claude present, no --resume - rule 1 finds nothing."""
    processes = [
        ProcessRow(pid=59314, ppid=59113, command="claude --dangerously-skip-permissions"),
        ProcessRow(pid=59113, ppid=1, command="/bin/zsh -il"),
    ]
    assert find_resume_uuid_in_tree(59113, processes) is None


def test_no_claude_process_at_all_is_no_match():
    """A pane running something else entirely."""
    processes = [ProcessRow(pid=1, ppid=0, command="/bin/zsh -il")]
    assert find_resume_uuid_in_tree(1, processes) is None


def test_malformed_uuid_is_rejected_not_returned():
    """A truncated / garbled --resume value must never be written."""
    processes = [
        ProcessRow(pid=1, ppid=0, command="claude --resume not-a-uuid-at-all"),
    ]
    assert find_resume_uuid_in_tree(1, processes) is None


def test_truncated_uuid_is_rejected():
    """One character short of a real uuid - still rejected."""
    truncated = RESUME_UUID[:-1]
    processes = [ProcessRow(pid=1, ppid=0, command=f"claude --resume {truncated}")]
    assert find_resume_uuid_in_tree(1, processes) is None


def test_resume_equals_form_is_parsed():
    """--resume=<uuid> is accepted, not only the space-separated form."""
    processes = [ProcessRow(pid=1, ppid=0, command=f"claude --resume={RESUME_UUID}")]
    assert find_resume_uuid_in_tree(1, processes) == RESUME_UUID


def test_uuid_is_lowercased():
    """A mixed-case uuid in argv is normalised before it is returned."""
    upper = RESUME_UUID.upper()
    processes = [ProcessRow(pid=1, ppid=0, command=f"claude --resume {upper}")]
    assert find_resume_uuid_in_tree(1, processes) == RESUME_UUID


def test_an_unrelated_process_naming_claude_in_arguments_is_not_matched():
    """Only argv0's basename counts - not any mention of the word claude."""
    processes = [
        ProcessRow(
            pid=1,
            ppid=0,
            command=f"/usr/bin/grep claude --resume {RESUME_UUID} /some/file",
        ),
    ]
    assert find_resume_uuid_in_tree(1, processes) is None


def test_root_pid_absent_from_table_is_no_match_not_a_crash():
    """The pane pid itself was not in the snapshot (raced/exited)."""
    processes = [ProcessRow(pid=2, ppid=1, command=f"claude --resume {RESUME_UUID}")]
    assert find_resume_uuid_in_tree(999, processes) is None


def test_depth_cap_stops_a_pathological_chain():
    """A chain deeper than MAX_TREE_DEPTH does not hang; it just misses."""
    from src.core.claude_resume_argv import MAX_TREE_DEPTH

    processes = []
    depth = MAX_TREE_DEPTH + 5
    for i in range(depth):
        processes.append(ProcessRow(pid=i + 1, ppid=i, command="/bin/sh"))
    processes.append(
        ProcessRow(pid=depth + 1, ppid=depth, command=f"claude --resume {RESUME_UUID}")
    )
    assert find_resume_uuid_in_tree(1, processes) is None


def test_list_process_table_reads_real_ps():
    """A live smoke test: real ps on this machine parses into rows."""
    table = list_process_table()
    assert table is not None
    assert len(table) > 0
    assert all(isinstance(row.pid, int) and isinstance(row.ppid, int) for row in table)
