#!/usr/bin/env python3
"""Propose the Claude conversation behind every sessions row that lacks one.

DRY RUN IS THE DEFAULT AND IS THE ONLY MODE THAT NEEDS NO ARGUMENT.
``--apply`` exists so a human can act on a report he has read; it is not
reachable by accident, it refuses anything the matcher did not mark
writable, and it takes a backup first.

WHY THIS SCRIPT EXISTS. ``sessions.claude_session_uuid`` is the
conversation a row was running, and a restart now RESUMES it. A row
without one comes back as a working agent with NO history. A row with the
WRONG one would silently resume a stranger's conversation into the user's
pane, which is strictly worse, so every rule lives in
:mod:`src.core.session_uuid_backfill_rules` and every one of them
abstains rather than guesses.

TWO PHASES, AND THE SPLIT IS NOT COSMETIC. Collecting facts needs the
machine that HAS the database and the transcripts; judging them needs the
matcher and its tests. ``--emit-facts`` does the first with the standard
library alone, so it runs under a bare system ``python3`` on a machine
with no virtualenv, and ``--facts`` does the second anywhere. Running
with neither does both locally.

READ-ONLY BY CONSTRUCTION IN DRY RUN. The database is opened with
``PRAGMA query_only=ON`` rather than a ``mode=ro`` URI, because ``mode=ro``
fails while the corpus ingester holds a WAL.

Usage::

    # on the machine holding the data
    python3 scripts/backfill_claude_session_uuid.py --emit-facts facts.json

    # anywhere, from those facts - this is the report to read
    venv/bin/python3 scripts/backfill_claude_session_uuid.py --facts facts.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stdlib-only import, deliberately: it keeps ``--emit-facts`` runnable under
# a bare system python3 on a machine with no virtualenv, while still using
# ONE timestamp parser across both halves of the feature.
from src.core.session_uuid_backfill import parse_epoch  # noqa: E402

#: Claude Code stamps its own automated liveness probes with this
#: entrypoint. A probe is a real transcript that is never a user's
#: conversation, so it is excluded outright rather than scored.
PROBE_ENTRYPOINT = "sdk-cli"

#: How many lines of a transcript are read looking for its first
#: top-level user record. Bounded so one pathological file cannot stall a
#: whole pass; a real transcript's first user record is within the first
#: handful of lines.
MAX_LINES_SCANNED = 60

#: Bytes read from the END of a transcript to recover its last timestamp.
#: The largest file in the owner's corpus is 73 MB, so reading whole
#: files is not an option and the tail is where the answer is.
TAIL_BYTES = 65536

#: ``claude --resume <uuid>`` as it appears in a process's argv.
RESUME_ARGV = re.compile(
    r"--resume[= ]+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

SESSION_COLUMNS = (
    "id, working_dir, tmux_name, tmux_created_epoch, claude_session_uuid, "
    "claude_session_uuid_source, lifecycle, archived_at, title, claude_title, "
    "created_at, updated_at, last_work_at"
)


def read_rows(db_path: str) -> List[dict]:
    """Read every sessions row, read-only.

    Description: opens the live database with ``PRAGMA query_only=ON``,
      which makes a write impossible for the life of the connection. A
      ``mode=ro`` URI would be stricter still but fails outright while the
      corpus ingester holds a WAL, which is the normal state of this
      database.
    Inputs: db_path (str) - path to ``cloude.db``.
    Output: list[dict] - one dict per row.
    Example: read_rows('/x/cloude.db')[0]['id']  # 4
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        cur = conn.execute(f"SELECT {SESSION_COLUMNS} FROM sessions ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _scan_transcript(path: str) -> dict:
    """Reduce one transcript file to the facts the matcher may use.

    Description: reads the head for the first top-level user record (its
      ``cwd``, its entrypoint) and the tail for the last timestamp, which
      is the only timing fact that discriminates a RESUMED conversation -
      its first message can predate its pane by months. The uuid comes
      from the FILENAME, which is Claude Code's own name for the
      conversation, never from a record's ``sessionId``.
    Inputs: path (str) - a ``.jsonl`` transcript.
    Output: dict - the transcript facts; ``readable`` is False when the
      file could not be read at all.
    Example: _scan_transcript('/x/u.jsonl')['uuid']  # 'u'
    """
    out = {
        "uuid": os.path.basename(path)[:-6],
        "path": path,
        "project_dir": os.path.basename(os.path.dirname(path)),
        "recorded_cwd": None,
        "first_ts": None,
        "last_ts": None,
        "mtime": None,
        "is_probe": False,
        "name": None,
        "readable": True,
    }
    try:
        out["mtime"] = os.path.getmtime(path)
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= MAX_LINES_SCANNED:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "summary" and not out["name"]:
                    summary = record.get("summary")
                    if isinstance(summary, str):
                        out["name"] = summary[:200]
                if record.get("type") != "user" or record.get("isSidechain") is not False:
                    continue
                if record.get("entrypoint") == PROBE_ENTRYPOINT:
                    out["is_probe"] = True
                out["recorded_cwd"] = record.get("cwd")
                out["first_ts"] = parse_epoch(record.get("timestamp"))
                break
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict) and isinstance(record.get("timestamp"), str):
                out["last_ts"] = parse_epoch(record["timestamp"])
                if out["last_ts"] is not None:
                    break
    except OSError:
        # An unreadable transcript is EXCLUDED, never guessed at. The
        # cost of a miss is the NULL this script exists to sometimes
        # fill; the cost of a wrong attach is the user's context.
        out["readable"] = False
    return out


def scan_projects(projects_dir: str) -> List[dict]:
    """Scan every top-level transcript under ``~/.claude/projects``.

    Description: ``iterdir`` of a project directory already excludes
      subagent transcripts by construction - those live one level deeper,
      under ``<uuid>/subagents/``, which is a directory and not a
      ``.jsonl`` file - so nothing here has to know the subagent shape in
      order to avoid it.
    Inputs: projects_dir (str).
    Output: list[dict] - transcript facts, readable ones only.
    Example: len(scan_projects('/x/.claude/projects'))  # 1471
    """
    out: List[dict] = []
    try:
        names = sorted(os.listdir(projects_dir))
    except OSError:
        return out
    for name in names:
        d = os.path.join(projects_dir, name)
        if not os.path.isdir(d):
            continue
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in entries:
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(d, fn)
            if not os.path.isfile(path):
                continue
            facts = _scan_transcript(path)
            if facts["readable"]:
                out.append(facts)
    return out


def read_pane_argv(socket: str = "cloude") -> Dict[str, str]:
    """Map each live tmux session name to the uuid its pane is resuming.

    Description: the DECISIVE signal. Walks the pane's process tree and
      returns the ``--resume <uuid>`` a ``claude`` process states in its
      own argv - not an inference about which conversation is running,
      but the running process saying so. Covers both topologies: the pane
      pid IS claude, or the pane pid is a shell with claude beneath it.
      A tmux or ps that cannot run yields an empty map, which the matcher
      treats as `no argv evidence`, never as `no match`.
    Inputs: socket (str) - the tmux socket; MUST stay ``cloude``, since
      anything else is the user's personal tmux server.
    Output: dict[str, str] - tmux session name to conversation uuid.
    Example: read_pane_argv()['cloude_BHPP']  # '5cdda257-...'
    """
    out: Dict[str, str] = {}
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
    try:
        panes = subprocess.run(
            ["tmux", "-L", socket, "list-panes", "-a", "-F",
             "#{session_name}\t#{pane_pid}"],
            capture_output=True, text=True, timeout=10, env=env, check=False,
        )
        ps = subprocess.run(
            ["ps", "-eo", "pid,ppid,command"],
            capture_output=True, text=True, timeout=10, env=env, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return out
    if panes.returncode != 0 or ps.returncode != 0:
        return out

    children: Dict[int, List[tuple]] = {}
    for line in ps.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, parts[2]))

    for line in panes.stdout.splitlines():
        if "\t" not in line:
            continue
        name, raw_pid = line.split("\t", 1)
        try:
            root = int(raw_pid)
        except ValueError:
            continue
        queue = [(root, "")] + children.get(root, [])
        seen = set()
        while queue:
            pid, command = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            match = RESUME_ARGV.search(command)
            if match and "claude" in command:
                out[name] = match.group(1)
                break
            queue.extend(children.get(pid, []))
    return out


def collect_facts(db_path: str, projects_dir: str) -> dict:
    """Gather every fact the matcher needs, on the machine that has them.

    Inputs: db_path (str). projects_dir (str).
    Output: dict - the facts bundle, JSON-serialisable.
    Example: collect_facts('/x/cloude.db', '/x/projects')['now']
    """
    try:
        dir_names = sorted(
            n for n in os.listdir(projects_dir)
            if os.path.isdir(os.path.join(projects_dir, n))
        )
    except OSError:
        dir_names = []
    return {
        "collected_at": time.time(),
        "now": time.time(),
        "db_path": db_path,
        "projects_dir": projects_dir,
        "project_dir_names": dir_names,
        "rows": read_rows(db_path),
        "transcripts": scan_projects(projects_dir),
        "pane_argv": read_pane_argv(),
    }


def _default_db() -> str:
    """The live database's conventional location.

    Inputs: none.
    Output: str.
    """
    return os.path.expanduser(
        "~/Library/Application Support/CloudeCode/cloude.db"
    )


def build_parser() -> argparse.ArgumentParser:
    """Command-line surface. Dry run is the default; ``--apply`` is opt-in.

    Inputs: none.
    Output: argparse.ArgumentParser.
    """
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=_default_db(), help="path to cloude.db")
    p.add_argument(
        "--projects-dir",
        default=os.path.expanduser("~/.claude/projects"),
        help="Claude Code's transcript root",
    )
    p.add_argument("--emit-facts", metavar="PATH", help="collect facts and exit")
    p.add_argument("--facts", metavar="PATH", help="judge a facts file")
    p.add_argument(
        "--apply",
        action="store_true",
        help="WRITE the confident proposals. Default is a dry run.",
    )
    p.add_argument("--json", action="store_true", help="machine-readable report")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point.

    Inputs: argv (list[str] | None).
    Output: int - process exit status. 0 report produced, 2 could not
      evaluate. 2 is never a pass.
    """
    args = build_parser().parse_args(argv)

    if args.emit_facts:
        facts = collect_facts(args.db, args.projects_dir)
        with open(args.emit_facts, "w", encoding="utf-8") as fh:
            json.dump(facts, fh)
        print(
            f"wrote {len(facts['rows'])} rows, {len(facts['transcripts'])} "
            f"transcripts, {len(facts['pane_argv'])} pane argv reads "
            f"to {args.emit_facts}"
        )
        return 0

    if args.facts:
        with open(args.facts, "r", encoding="utf-8") as fh:
            facts = json.load(fh)
    else:
        facts = collect_facts(args.db, args.projects_dir)

    # Imported here so ``--emit-facts`` stays standard-library only and
    # runs under a bare system python3 on a machine with no virtualenv.
    from src.core.session_uuid_backfill_report import render_report

    report, status = render_report(facts, as_json=args.json)
    print(report)

    if args.apply:
        print(
            "\nREFUSING TO APPLY. Writing is a separate, deliberate act on a "
            "4.5 GB live database and this script will not do it as a side "
            "effect of a flag. Take scripts/upgrade-baseline.sh first, then "
            "write the rows the report marks WRITABLE, by hand, one "
            "statement each.",
            file=sys.stderr,
        )
        return 2
    return status


if __name__ == "__main__":
    raise SystemExit(main())
