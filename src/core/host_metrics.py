"""Read-only host health probes for the server-status panel.

NO NEW DEPENDENCY. psutil would answer all of this in one line, but it is
a compiled wheel and this app already ships a subprocess convention that
covers the same ground (list argv, never ``shell=True``). Everything here
is stdlib plus two short reads of ``vm_stat`` / ``/proc/meminfo``.

NO SUDO, EVER. Every probe below is readable by the uid the server already
runs as. A probe that needed elevation would either hang on a password
prompt or fail with a misleading error, and a status panel is not worth
either.

THREE OUTCOMES, NOT TWO. Every collector returns a dict carrying
``available`` and ``error``. A probe that could not run reports
``available: False`` with the reason, and the panel renders that as
"cannot determine" rather than as a healthy zero. Reporting 0 bytes of
used memory because ``vm_stat`` was missing is the false-green this
codebase keeps paying for.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

#: Wall-clock seconds any one helper subprocess may take. Deliberately
#: short: the panel is opened interactively, so a wedged ``vm_stat`` must
#: degrade to "cannot determine" rather than hold the whole response.
PROBE_TIMEOUT_SECONDS: float = 3.0


def unavailable(reason: str) -> Dict[str, Any]:
    """Build the standard could-not-evaluate result.

    Args:
        reason: short lowercase explanation shown to the user.

    Returns:
        A dict with ``available`` False and ``error`` set.
    """
    return {"available": False, "error": reason}


def _run(argv: List[str]) -> Optional[str]:
    """Run a read-only helper and return its stdout, or None.

    List argv only and never ``shell=True``: nothing in this module ever
    interpolates a session name or any other untrusted string into a
    command, and this signature makes that structurally impossible.

    Args:
        argv: the command and its arguments.

    Returns:
        Decoded stdout on exit 0, otherwise None.
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
        logger.debug("host_metric_probe_failed", argv=argv[0], error=str(exc))
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _sysctl_int(name: str) -> Optional[int]:
    """Read one integer ``sysctl`` value.

    Args:
        name: the sysctl key, e.g. ``hw.memsize``.

    Returns:
        The integer value, or None when unreadable or not an integer.
    """
    out = _run(["sysctl", "-n", name])
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def collect_memory() -> Dict[str, Any]:
    """Total, used and available physical memory, in bytes.

    macOS is measured from ``vm_stat`` page counts rather than from
    "free", because free pages on a healthy Mac are near zero by design;
    what a reader wants is how much is reclaimable. ``available`` here is
    free plus inactive plus speculative plus purgeable, which is the same
    set the kernel will hand to a new allocation without swapping.

    Returns:
        ``{available, error, total_bytes, used_bytes, available_bytes,
        used_percent}``. On failure only ``available``/``error`` are set.
    """
    system = platform.system()
    if system == "Darwin":
        return _memory_darwin()
    if system == "Linux":
        return _memory_linux()
    return unavailable(f"no memory probe for {system.lower() or 'this os'}")


def _memory_darwin() -> Dict[str, Any]:
    """macOS memory, from ``sysctl hw.memsize`` plus ``vm_stat``.

    Returns:
        The shape documented on :func:`collect_memory`.
    """
    total = _sysctl_int("hw.memsize")
    if not total:
        return unavailable("sysctl hw.memsize unreadable")

    out = _run(["vm_stat"])
    if out is None:
        return unavailable("vm_stat unavailable")

    page_size = 4096
    counts: Dict[str, int] = {}
    for line in out.splitlines():
        if "page size of" in line:
            for token in line.replace(".", " ").split():
                if token.isdigit():
                    page_size = int(token)
                    break
            continue
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        digits = raw.strip().rstrip(".")
        if digits.isdigit():
            counts[key.strip().lower()] = int(digits)

    if not counts:
        return unavailable("vm_stat output unparseable")

    reclaimable = sum(
        counts.get(key, 0)
        for key in ("pages free", "pages inactive", "pages speculative",
                    "pages purgeable")
    )
    avail = min(reclaimable * page_size, total)
    used = total - avail
    return {
        "available": True,
        "error": None,
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": avail,
        "used_percent": round(used * 100.0 / total, 1),
    }


def _memory_linux() -> Dict[str, Any]:
    """Linux memory, from ``/proc/meminfo``.

    Returns:
        The shape documented on :func:`collect_memory`.
    """
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        return unavailable(f"/proc/meminfo unreadable: {exc.strerror}")

    fields: Dict[str, int] = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[key.strip()] = int(parts[0]) * 1024

    total = fields.get("MemTotal")
    avail = fields.get("MemAvailable")
    if not total or avail is None:
        return unavailable("/proc/meminfo missing MemTotal/MemAvailable")

    used = total - avail
    return {
        "available": True,
        "error": None,
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": avail,
        "used_percent": round(used * 100.0 / total, 1),
    }


def collect_disk(path: str = "/") -> Dict[str, Any]:
    """Free and total space on the filesystem holding ``path``.

    Args:
        path: any path on the filesystem of interest. Defaults to the
            root volume, which is where both the checkout and
            ``~/.claude/projects`` live on this box.

    Returns:
        ``{available, error, path, total_bytes, used_bytes, free_bytes,
        used_percent}``.
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return unavailable(f"disk usage for {path} unreadable: {exc.strerror}")
    if usage.total <= 0:
        return unavailable(f"disk usage for {path} reported zero total")
    return {
        "available": True,
        "error": None,
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(usage.used * 100.0 / usage.total, 1),
    }


def collect_load() -> Dict[str, Any]:
    """Load averages and the cpu count they should be read against.

    A raw load of 8 means nothing without the core count, so the two are
    reported together and never separately.

    Returns:
        ``{available, error, load_1, load_5, load_15, cpu_count}``.
    """
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError) as exc:
        return unavailable(f"load average unavailable: {exc}")
    return {
        "available": True,
        "error": None,
        "load_1": round(one, 2),
        "load_5": round(five, 2),
        "load_15": round(fifteen, 2),
        "cpu_count": os.cpu_count() or 0,
    }


def collect_host() -> Dict[str, Any]:
    """Identity and uptime of the machine the server runs on.

    Returns:
        ``{available, error, hostname, os, uptime_seconds}``.
        ``uptime_seconds`` is None when the boot time could not be read,
        which is reported as unknown rather than as zero uptime.
    """
    return {
        "available": True,
        "error": None,
        "hostname": platform.node() or "unknown",
        "os": f"{platform.system()} {platform.release()}".strip(),
        "uptime_seconds": _host_uptime_seconds(),
    }


def _host_uptime_seconds() -> Optional[int]:
    """Seconds since the machine booted, or None when unknown.

    Returns:
        Whole seconds of host uptime, or None.
    """
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as handle:
                return int(float(handle.read().split()[0]))
        except (OSError, ValueError, IndexError):
            return None
    if system == "Darwin":
        out = _run(["sysctl", "-n", "kern.boottime"])
        if not out:
            return None
        # "{ sec = 1786932138, usec = 273007 } Sun Aug 16 22:02:18 2026"
        marker = "sec ="
        if marker not in out:
            return None
        tail = out.split(marker, 1)[1].lstrip()
        digits = ""
        for char in tail:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return None
        return max(0, int(time.time()) - int(digits))
    return None


def collect() -> Dict[str, Any]:
    """Every host probe, each carrying its own availability.

    Returns:
        ``{host, memory, disk, load}``.
    """
    return {
        "host": collect_host(),
        "memory": collect_memory(),
        "disk": collect_disk(),
        "load": collect_load(),
    }
