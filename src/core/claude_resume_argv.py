"""Find a Claude process's --resume uuid in a tmux pane's process tree.

THE CASE THIS EXISTS FOR. A RESUMED conversation predates the pane
hosting it, always - `claude --resume <uuid>` opens a conversation that
was created earlier, possibly in a different pane, possibly on a
different day. So no timing rule built on "when did the transcript
start" can ever find it: the transcript's first record is BEFORE the
pane, sometimes months before (measured on the owner's live fleet
2026-08-29: two months, for the tmux session `Media_Compression`). And a
resumed/recovered session is exactly the primary case this whole feature
exists to cover - see the module docstring on
src/core/claude_session_correlate_ladder.py for the full ladder this
module is rule 1 of.

THE EVIDENCE. The pane's own foreground process, or one of its
descendants, is the literal `claude` binary invoked with
`--resume <uuid>`. That argument IS the conversation being opened - a
direct read of what the process was told to do, not an inference from
timing. Two process topologies exist and both have to be handled:

  the pane's own pid IS the claude process     tmux ran the binary
                                                directly (measured: an
                                                externally-created
                                                session that ran
                                                `claude --resume ...` as
                                                its shell's exec target,
                                                or a bare `claude ...`
                                                command line).
  the pane's pid is a SHELL, claude is a CHILD  the app's own launch
                                                path (a shell is spawned,
                                                then `claude` is exec'd
                                                or forked as its child).

:func:`find_resume_uuid_in_tree` walks BOTH: it starts at the pane pid
itself (depth 0) and then breadth-first through its descendants, so
either topology is found by the same walk with no branch needed for
which one it is.

READING THE PROCESS TABLE. macOS has no `/proc`; the process table is
read via one `ps -A -o pid=,ppid=,command=` call
(:func:`list_process_table`), never per-pid, so walking a pane's whole
descendant tree costs one subprocess regardless of its depth. Every
failure - `ps` absent, a timeout, a non-zero exit, undecodable bytes -
answers None. None is a real "could not evaluate" here, and the caller
(the ladder) falls through to rule 2 exactly as it would for `ps`
answering "no candidate", never for "found and rejected".

NEVER GUESS ON A MALFORMED UUID. `--resume` is followed by whatever the
shell handed the process; a truncated copy-paste, a shell-quoting
artifact, or a stray flag value are all real possibilities. A string
that is not a canonical `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` shape is
rejected (:data:`_UUID_RE`) and this module answers None for that
process rather than writing a value it cannot vouch for - the same "if
you cannot classify a candidate, it is not a match" rule
claude_transcript_correlate.py applies to a garbled transcript.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import structlog

logger = structlog.get_logger()

#: Wall-clock budget for the one `ps` call. A wedged `ps` must not hold
#: an adopt open - matches the spirit of tmux_session_cwd's probe budget.
PS_TIMEOUT_SECONDS: float = 3.0

#: How many BFS generations :func:`find_resume_uuid_in_tree` will walk
#: past the pane pid itself before giving up. A real pane's process tree
#: is shallow (pane -> shell -> claude is the deepest case measured); this
#: is a defensive cap in the same spirit as session_lineage's
#: `_MAX_LINEAGE_DEPTH`, sized for a process tree rather than a lineage
#: chain.
MAX_TREE_DEPTH: int = 8

#: Canonical UUID shape only - no version/variant enforcement, because
#: Claude Code's own uuid4 output always matches this and a stricter
#: check would reject nothing additional. Case-insensitive; the matched
#: value is lower-cased before being returned.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Matches `--resume <uuid>` or `--resume=<uuid>`, capturing everything up
#: to the next whitespace. Deliberately permissive here - shape
#: validation happens in :func:`_extract_resume_uuid`, not in this regex,
#: so a truncated or garbled value is CAUGHT rather than never matched.
_RESUME_RE = re.compile(r"--resume(?:=|\s+)(\S+)")


@dataclass(frozen=True)
class ProcessRow:
    """One line of `ps` output, parsed.

    Inputs (constructor): pid (int). ppid (int). command (str) - the
      full command line as `ps` reported it (argv0 plus every argument,
      whitespace-joined; `ps`'s own `command=` field, not re-quoted).
    Output: a ProcessRow instance.
    """

    pid: int
    ppid: int
    command: str


def list_process_table(timeout: float = PS_TIMEOUT_SECONDS) -> Optional[List[ProcessRow]]:
    """Snapshot the whole process table in one `ps` call.

    Description: macOS has no `/proc`, so this is the one place a process
      tree is ever read from - one call, walked in memory afterwards,
      rather than one `ps` per pid. NEVER RAISES: every failure mode
      answers None, which the caller must treat as "could not evaluate",
      never as "no processes".
    Inputs: timeout (float) - seconds.
    Output: list[ProcessRow] | None - every process `ps` reported, or
      None when the table could not be read at all.
    Example: list_process_table()[0].command  # '/sbin/launchd'
    """
    try:
        completed = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,command="],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("process_table_probe_failed", error=str(exc))
        return None
    if completed.returncode != 0:
        logger.warning(
            "process_table_probe_nonzero_exit",
            returncode=completed.returncode,
        )
        return None
    try:
        text = completed.stdout.decode("utf-8", errors="replace")
    except (UnicodeDecodeError, AttributeError) as exc:
        logger.warning("process_table_probe_undecodable", error=str(exc))
        return None

    rows: List[ProcessRow] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        rows.append(ProcessRow(pid=pid, ppid=ppid, command=parts[2]))
    return rows


def _is_claude_command(command: str) -> bool:
    """Is this command line's own binary literally `claude`?

    Description: checked on the BASENAME of argv0 so both
      `/Users/x/.local/bin/claude ...` and a bare `claude ...` match, and
      a completely unrelated process whose arguments happen to mention
      `claude` somewhere does not.
    Inputs: command (str) - a ProcessRow.command.
    Output: bool.
    """
    stripped = command.strip()
    if not stripped:
        return False
    first = stripped.split(None, 1)[0]
    return Path(first).name == "claude"


def _extract_resume_uuid(command: str) -> Optional[str]:
    """Pull a validated uuid out of a `claude --resume ...` command line.

    Description: a malformed or truncated value is REJECTED, not
      returned - see the module docstring's "never guess" section. The
      caller must not fall back to a partial value.
    Inputs: command (str).
    Output: str | None - lower-cased canonical uuid, or None when
      `--resume` is absent or its value does not parse as one.
    """
    match = _RESUME_RE.search(command)
    if not match:
        return None
    candidate = match.group(1).strip().strip("'\"")
    if not _UUID_RE.match(candidate):
        logger.warning(
            "resume_argv_uuid_malformed",
            note="a --resume value was present but not a valid uuid shape; rejected",
        )
        return None
    return candidate.lower()


def find_resume_uuid_in_tree(
    root_pid: int,
    processes: Sequence[ProcessRow],
    *,
    max_depth: int = MAX_TREE_DEPTH,
) -> Optional[str]:
    """Walk a pane pid and its descendants for a `claude --resume` uuid.

    Description: breadth-first from `root_pid` INCLUSIVE (depth 0 is the
      pane pid itself), so both process topologies are covered by one
      walk - see the module docstring. The first claude process found
      carrying a valid `--resume` uuid wins; a claude process found with
      no `--resume` (the born-in-pane case) or a malformed one yields no
      match from that process and the walk continues to any siblings,
      though in practice a pane hosts exactly one claude process.
    Inputs: root_pid (int) - the pane's own foreground pid.
      processes (Sequence[ProcessRow]) - a snapshot from
      :func:`list_process_table`. max_depth (int) - BFS generation cap.
    Output: str | None - a validated, lower-cased uuid, or None when no
      qualifying process was found within the walk.
    Example: find_resume_uuid_in_tree(99871, table)
      # '82854c0e-a423-4591-a34f-a14cb92fbf41'
    """
    by_pid: Dict[int, ProcessRow] = {row.pid: row for row in processes}
    children: Dict[int, List[int]] = {}
    for row in processes:
        children.setdefault(row.ppid, []).append(row.pid)

    frontier = [root_pid]
    visited: set = set()
    depth = 0
    while frontier and depth <= max_depth:
        next_frontier: List[int] = []
        for pid in frontier:
            if pid in visited:
                continue
            visited.add(pid)
            row = by_pid.get(pid)
            if row is not None and _is_claude_command(row.command):
                uuid = _extract_resume_uuid(row.command)
                if uuid is not None:
                    return uuid
            next_frontier.extend(children.get(pid, ()))
        frontier = next_frontier
        depth += 1
    return None
