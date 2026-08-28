"""What this server process and its tmux socket currently look like.

Companion to :mod:`src.core.host_metrics`, which answers the same kind of
question about the BOX. Split in two because the two halves have nothing
in common except the panel that renders them, and one file holding both
would be the junk drawer the code standards forbid.

OWNERSHIP IS NOT COMPUTED HERE. Whether the app created a tmux session is
decided in exactly one place, ``TmuxBackend.list_attachable_sessions()``,
by membership of the SessionManager's persisted ``owned_tmux_sessions``
set. This module reads tmux for the fields that path does not carry
(working directory, pane size, attached clients) and the caller merges the
authoritative ``created_by_cloude`` onto it by NAME. Re-deriving ownership
from the ``adopted:`` id prefix is wrong and has already shipped as a bug
twice: after a server restart the app re-attaches to its own still-running
sessions through the adopt path, so an app-created session ends up with an
``adopted:`` id while still being in ``owned_tmux_sessions``. The NAME is
the stable key; the id is not.

NO UNTRUSTED STRING EVER REACHES A COMMAND. Session names come from tmux
and from user input. Nothing in this module interpolates one into a
subprocess: the only tmux calls made here are ``list-sessions`` and
``show-options``, both of which take a fixed argv and read names OUT.
Killing a session is not done here at all - that is the SessionManager's
existing job.

THREE OUTCOMES. Every section carries ``available`` and ``error``. A probe
that could not run says so; it never renders as a healthy zero.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from src.core.host_metrics import PROBE_TIMEOUT_SECONDS, unavailable

logger = structlog.get_logger()

#: Field separator for the tmux list-sessions format. Chosen because tmux
#: forbids neither spaces nor most punctuation in a session name, but the
#: fields we ask for cannot themselves contain a newline, and the split is
#: bounded so a working directory containing the separator cannot shift
#: the earlier fields.
_TMUX_FIELD_SEP = "\x1f"

#: The tmux format string, in the order the parser expects. The path goes
#: LAST for the bounded-split reason above.
_TMUX_FORMAT = _TMUX_FIELD_SEP.join([
    "#{session_name}",
    "#{session_created}",
    "#{session_attached}",
    "#{window_width}",
    "#{window_height}",
    "#{session_windows}",
    "#{session_path}",
])

#: Absolute paths tried, in order, for the claude CLI. ``claude`` is not
#: on a non-interactive shell's PATH on this box, so ``shutil.which``
#: alone finds nothing; it is still consulted first for boxes where the
#: PATH is correct.
_CLAUDE_CANDIDATES = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)


def _run_full(argv: List[str]) -> Optional[tuple]:
    """Run a read-only helper and return ``(rc, stdout, stderr)``.

    List argv only, never ``shell=True``. Every caller in this module
    passes a literal argv with no interpolated user data.

    Args:
        argv: the command and its arguments.

    Returns:
        ``(returncode, stdout, stderr)`` with both streams decoded, or
        None when the binary is missing or the call could not be made at
        all. None means "could not measure"; a non-zero rc means the
        command ran and said no.
    """
    if not argv or not shutil.which(argv[0]):
        return None
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("server_status_probe_failed", argv=argv[0], error=str(exc))
        return None
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def _run(argv: List[str]) -> Optional[str]:
    """Run a read-only helper and return its stdout, or None.

    Args:
        argv: the command and its arguments.

    Returns:
        Decoded stdout on exit 0, otherwise None.
    """
    result = _run_full(argv)
    if result is None or result[0] != 0:
        return None
    return result[1]


def parse_etime(raw: str) -> Optional[int]:
    """Parse a BSD/GNU ``ps`` elapsed time into whole seconds.

    macOS ``ps`` has no ``etimes`` keyword, so the human format is the
    only one available and has to be parsed. Accepted shapes are
    ``mm:ss``, ``hh:mm:ss`` and ``dd-hh:mm:ss``.

    Args:
        raw: the raw ``etime`` field.

    Returns:
        Seconds, or None when the field is absent or unparseable.

    Example:
        parse_etime("2-03:04:05") -> 183845
    """
    text = (raw or "").strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = text.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        numbers.insert(0, 0)
    hours, minutes, seconds = numbers
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def collect_process(host: str, port: int, repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """The web server process itself: how long it has run and where it binds.

    ``lan_reachable`` is the fact a reader actually wants from the bind
    address: ``0.0.0.0`` means every device on the network can reach this
    app, and the app's TOTP auth is the only thing standing in front of
    it. It is derived from the configured host rather than asserted.

    Args:
        host: the configured bind host, from ``settings.host``.
        port: the configured bind port, from ``settings.port``.
        repo_root: checkout to read the deployed commit from. Defaults to
            the repo this module lives in.

    Returns:
        ``{available, error, pid, uptime_seconds, host, port,
        lan_reachable, python_version, platform, commit}``.
    """
    pid = os.getpid()
    root = repo_root or Path(__file__).resolve().parents[2]
    return {
        "available": True,
        "error": None,
        "pid": pid,
        "uptime_seconds": process_uptime_seconds(pid),
        "host": host,
        "port": port,
        "lan_reachable": host in ("0.0.0.0", "::", ""),
        "python_version": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}".strip(),
        "commit": collect_commit(root),
    }


def process_uptime_seconds(pid: int) -> Optional[int]:
    """Seconds this process has been running, or None when unknown.

    Deliberately NOT the time since this module was imported: a value
    that resets on a code reload would quietly misreport how long the
    server has actually been up, which is the one thing it is asked.

    Args:
        pid: the process id to measure.

    Returns:
        Whole seconds, or None.
    """
    out = _run(["ps", "-p", str(int(pid)), "-o", "etime="])
    if out is None:
        return None
    return parse_etime(out)


def collect_commit(repo_root: Path) -> Dict[str, Any]:
    """The commit this checkout is deployed at.

    Args:
        repo_root: directory of the checkout.

    Returns:
        ``{available, error, sha, dirty}``. Not a git checkout, or no git
        binary, reports unavailable rather than an empty sha.
    """
    if not (repo_root / ".git").exists():
        return unavailable("not a git checkout")
    sha = _run(["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"])
    if sha is None:
        return unavailable("git rev-parse failed")
    status = _run(["git", "-C", str(repo_root), "status", "--porcelain"])
    return {
        "available": True,
        "error": None,
        "sha": sha.strip(),
        "dirty": bool(status and status.strip()),
    }


def collect_claude_cli() -> Dict[str, Any]:
    """Version of the claude CLI this box would launch.

    Worth showing because it is the thing every session actually runs and
    it updates out from under the app. Resolution walks absolute
    candidates because ``claude`` is not on a non-interactive shell's
    PATH here.

    Returns:
        ``{available, error, version, path}``.
    """
    found = shutil.which("claude")
    candidates = [found] if found else []
    candidates += [str(Path(p).expanduser()) for p in _CLAUDE_CANDIDATES]

    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        out = _run([candidate, "--version"])
        if not out:
            continue
        return {
            "available": True,
            "error": None,
            "version": out.strip().splitlines()[0],
            "path": candidate,
        }
    return unavailable("claude cli not found on any known path")


def collect_tmux(socket_name: str) -> Dict[str, Any]:
    """Everything about the app's dedicated tmux socket.

    A socket with no server running is NOT an error: it is the normal
    state before the first session is created. That case reports
    ``available: True`` with ``server_running: False`` and an empty list,
    because it is a measured fact rather than a failed measurement.

    Args:
        socket_name: the socket passed to ``tmux -L``.

    Returns:
        ``{available, error, socket, server_running, history_limit,
        sessions}``.
    """
    if not shutil.which("tmux"):
        return dict(unavailable("tmux is not installed"), socket=socket_name,
                    server_running=None, history_limit=None, sessions=[])

    base = ["tmux", "-L", socket_name]
    result = _run_full(base + ["list-sessions", "-F", _TMUX_FORMAT])
    if result is None:
        return dict(unavailable("tmux could not be run"), socket=socket_name,
                    server_running=None, history_limit=None, sessions=[])

    rc, out, err = result
    if rc != 0:
        # tmux exits non-zero both for "no server running", which is the
        # ordinary state before the first session and a MEASURED fact,
        # and for a genuine failure, which is not. Only its own message
        # tells the two apart, so the third outcome is kept distinct
        # instead of both collapsing into an empty list.
        # Two different strings for the same benign fact: tmux says "no
        # server running on <path>" when the socket file exists with
        # nothing behind it, and "error connecting to <path> (No such
        # file or directory)" before it has ever been created.
        lowered = err.lower()
        if "no server running" in lowered or "no such file or directory" in lowered:
            return {
                "available": True,
                "error": None,
                "socket": socket_name,
                "server_running": False,
                "history_limit": None,
                "sessions": [],
            }
        return dict(unavailable(f"tmux list-sessions failed: {err.strip()[:120]}"),
                    socket=socket_name, server_running=None,
                    history_limit=None, sessions=[])

    return {
        "available": True,
        "error": None,
        "socket": socket_name,
        "server_running": True,
        "history_limit": _history_limit(base),
        "sessions": parse_sessions(out),
    }


def _history_limit(base: List[str]) -> Optional[int]:
    """The socket's global ``history-limit``, or None when unreadable.

    This is how many lines of scrollback tmux keeps per pane, so it is
    the ceiling on what the terminal can ever scroll back to. None means
    unknown, never the tmux default - guessing 2000 would be inventing a
    measurement.

    Args:
        base: the ``tmux -L <socket>`` argv prefix.

    Returns:
        The limit in lines, or None.
    """
    out = _run(base + ["show-options", "-gv", "history-limit"])
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def parse_sessions(raw: str) -> List[Dict[str, Any]]:
    """Parse ``list-sessions`` output into session rows.

    ``created_by_cloude`` is deliberately ABSENT from these rows. The
    caller merges it in from the SessionManager's authoritative path;
    inventing it here would be the third place that answer is computed.

    Args:
        raw: stdout of the formatted ``list-sessions`` call.

    Returns:
        One dict per parseable line, with ``name``, ``created_at_epoch``,
        ``attached_clients``, ``pane_cols``, ``pane_rows``,
        ``window_count`` and ``working_dir``.

    Example:
        parse_sessions("a\\x1f1\\x1f0\\x1f80\\x1f24\\x1f1\\x1f/tmp")
          -> [{"name": "a", ..., "working_dir": "/tmp"}]
    """
    rows: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(_TMUX_FIELD_SEP, 6)
        if len(parts) < 7:
            logger.debug("server_status_unparseable_session_row", raw=line)
            continue
        name, created, attached, cols, rows_, windows, path = parts
        rows.append({
            "name": name,
            "created_at_epoch": _as_int(created),
            "attached_clients": _as_int(attached),
            "pane_cols": _as_int(cols),
            "pane_rows": _as_int(rows_),
            "window_count": _as_int(windows),
            "working_dir": path,
        })
    return rows


def _as_int(raw: str) -> int:
    """Best-effort integer, 0 when the field was not a number.

    Args:
        raw: the raw field text.

    Returns:
        The integer value, or 0.
    """
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return 0


def merge_ownership(
    sessions: List[Dict[str, Any]],
    ownership_by_name: Dict[str, bool],
    open_ids_by_name: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Attach the server's ownership verdict and open-in-app state by NAME.

    Args:
        sessions: rows from :func:`parse_sessions`, mutated in place.
        ownership_by_name: tmux name -> ``created_by_cloude``, sourced
            from ``SessionManager.list_attachable_sessions()``. A name
            missing from this mapping gets None, meaning "could not
            determine", never False.
        open_ids_by_name: tmux name -> session id for every session bound
            to a live backend in THIS process.

    Returns:
        The same list, each row gaining ``created_by_cloude``,
        ``open_in_app`` and ``session_id``.
    """
    for row in sessions:
        name = row.get("name", "")
        row["created_by_cloude"] = ownership_by_name.get(name)
        session_id = open_ids_by_name.get(name)
        row["session_id"] = session_id
        row["open_in_app"] = session_id is not None
    return sessions


def collect(
    host: str,
    port: int,
    socket_name: str,
    ownership_by_name: Dict[str, bool],
    open_ids_by_name: Dict[str, str],
) -> Dict[str, Any]:
    """Assemble the whole snapshot the status panel renders.

    Args:
        host: configured bind host.
        port: configured bind port.
        socket_name: the app's tmux socket.
        ownership_by_name: tmux name -> ``created_by_cloude``, from the
            SessionManager. See the module docblock for why it is not
            computed here.
        open_ids_by_name: tmux name -> session id for live backends.

    Returns:
        ``{collected_at, server, tmux, claude_cli, host, memory, disk,
        load}``.
    """
    from src.core import host_metrics  # local import keeps the cycle impossible

    tmux = collect_tmux(socket_name)
    tmux["sessions"] = merge_ownership(
        tmux.get("sessions", []), ownership_by_name, open_ids_by_name
    )
    snapshot: Dict[str, Any] = {
        "collected_at": int(time.time()),
        "server": collect_process(host, port),
        "tmux": tmux,
        "claude_cli": collect_claude_cli(),
    }
    snapshot.update(host_metrics.collect())
    return snapshot
