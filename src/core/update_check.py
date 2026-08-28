"""Background release-tag self check.

Answers one question - "is a newer release tag available?" - and answers it
with THREE OUTCOMES, NEVER TWO:

    current           we fetched the remote tag list, compared, we are newest
    update_available  we fetched the list and a newer tag exists (named)
    unknown           we could NOT look: offline, no git, remote unreachable,
                      timed out, or the local version did not resolve

``unknown`` is its own visible state and must never render as "up to date".
Collapsing "I could not look" into "nothing is wrong" is the single defect
class this project has spent the most time killing.

DESIGN NOTES

* Never blocks. The HTTP endpoint returns whatever is cached; the network
  call happens on a background thread kicked off after startup and then on a
  refresh interval. A page load never waits on a remote.
* The cached answer carries ``checked_at``, and the client shows it, so a
  stale answer is visibly stale rather than silently wrong.
* NEVER auto-updates. It reports, and offers the command. Pulling code out
  from under a running agent session unattended is not acceptable. The
  distribution channel is the signed-by-nobody .dmg built by
  ``macOS/package.json``, so the offered command opens the releases page
  rather than pretending an in-place upgrader exists.
* Which remote: the checkout's own ``origin`` when the source root IS a git
  work tree, because a dev running from a fork should be told about that
  fork's tags. A packaged install has no ``origin`` (see
  ``discover_origin_remote``) and falls through to FALLBACK_REMOTE, the
  public upstream the .dmg is released from. ``updates.remote`` in
  config.json overrides both with an explicit URL.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import structlog

from src.core.version import (
    is_git_root,
    normalize_tag,
    repo_root,
    resolve_version,
)

logger = structlog.get_logger()

# Outcome names. Kept as plain strings because they cross the wire to the
# client and appear in its copy table.
STATUS_CURRENT = "current"
STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_UNKNOWN = "unknown"

# How long between background checks. Six hours: often enough to be useful,
# rare enough that a laptop waking on a phone hotspot is not hammering github.
DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60

# A hung `git ls-remote` must not pin a thread forever.
LS_REMOTE_TIMEOUT_SECONDS = 20.0

# Only plain release tags are considered. A tag like "v0.9.0-rc1" is a
# pre-release and is deliberately NOT offered as an upgrade.
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_LS_REMOTE_REF_RE = re.compile(r"refs/tags/(\S+?)(?:\^\{\})?$")

# Fallback when the checkout has no usable origin. The public upstream.
FALLBACK_REMOTE = "https://github.com/Adoom666/CloudeCode.git"

# What a human runs to upgrade. There is no in-place upgrader: the app ships
# as a .dmg you drag to /Applications, so the honest instruction is to open
# the releases page. `open` is a real, copy-pasteable macOS command.
DEFAULT_UPGRADE_COMMAND = "open https://github.com/Adoom666/CloudeCode/releases/latest"

# Cache filename. It is written INSIDE the resolved source root rather than a
# private dot-directory of its own, deliberately: in production that root IS
# the app's state dir (~/Library/Application Support/cloude-code-menubar/
# server/), alongside config.json and .env, and inventing a second state root
# is how two installs of the same app start disagreeing about themselves.
CACHE_FILENAME = ".update-check.json"


def default_cache_path(root: Optional[Path] = None) -> Path:
    """Return where the cached answer lives for a given source root.

    Args:
        root: repository/source root, defaults to :func:`repo_root`.

    Returns:
        Path to the cache file. The file need not exist.
    """
    return (root or repo_root()) / CACHE_FILENAME


class UpdateCheckError(RuntimeError):
    """Raised internally when the remote tag list cannot be obtained."""


@dataclass
class UpdateStatus:
    """One answer from the self check.

    Attributes:
        status: one of STATUS_CURRENT, STATUS_UPDATE_AVAILABLE, STATUS_UNKNOWN.
        current_version: the installed tag, or "" if it did not resolve.
        latest_version: the newest remote release tag, or "" when unknown.
        remote: the remote URL that was consulted.
        checked_at: unix seconds when this answer was produced.
        reason: for STATUS_UNKNOWN, a short human sentence saying what failed.
            Empty for the other two states.
        upgrade_command: the command a human should run. Never run for them.
    """

    status: str
    current_version: str
    latest_version: str
    remote: str
    checked_at: float
    reason: str
    upgrade_command: str

    def to_dict(self) -> dict:
        """Return the JSON-serialisable shape sent to the client."""
        return asdict(self)


def parse_version(tag: str) -> Optional[tuple[int, int, int]]:
    """Parse a release tag into a comparable tuple.

    Args:
        tag: a tag such as "v0.8.1" or "0.8.1".

    Returns:
        ``(major, minor, patch)``, or None when the tag is not a plain
        three-part release tag (pre-releases and dev describes return None).
    """
    match = _TAG_RE.match(tag.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def read_configured_remote(config_path: Path) -> str:
    """Read ``updates.remote`` from config.json.

    Read directly rather than through the settings model so this optional key
    cannot break config validation for installs that do not set it.

    Args:
        config_path: path to config.json.

    Returns:
        The configured remote URL, or "" when absent or unreadable.
    """
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    updates = data.get("updates")
    if not isinstance(updates, dict):
        return ""
    return str(updates.get("remote", "")).strip()


def discover_origin_remote(root: Path) -> str:
    """Ask git for the checkout's ``origin`` URL.

    Pinned to ``root`` via :func:`is_git_root` for the same reason the version
    resolver is: ``git -C`` walks upwards, so an unversioned source root
    sitting inside an unrelated checkout would otherwise hand back THAT
    project's origin, and the self check would then compare this app's version
    against a stranger's tags and confidently offer a bogus upgrade.

    Args:
        root: repository root.

    Returns:
        The origin URL, or "" when ``root`` is not itself a work tree root,
        git is missing, or there is no origin.
    """
    if not is_git_root(root):
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def fetch_remote_tags(remote: str) -> list[str]:
    """List release tags on a remote without cloning or fetching objects.

    Args:
        remote: a git remote URL.

    Returns:
        Sorted-by-version list of plain release tags, newest last.

    Raises:
        UpdateCheckError: git is absent, the remote is unreachable, the call
            timed out, or the output could not be parsed.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", remote],
            capture_output=True,
            text=True,
            timeout=LS_REMOTE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise UpdateCheckError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateCheckError("the release remote did not answer in time") from exc
    except OSError as exc:
        raise UpdateCheckError(f"could not run git: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "no detail"
        raise UpdateCheckError(f"could not reach the release remote: {tail}")

    tags: list[tuple[tuple[int, int, int], str]] = []
    for line in result.stdout.splitlines():
        match = _LS_REMOTE_REF_RE.search(line)
        if not match:
            continue
        tag = match.group(1)
        parsed = parse_version(tag)
        if parsed is not None:
            tags.append((parsed, normalize_tag(tag)))
    tags.sort()
    return [tag for _, tag in tags]


class UpdateChecker:
    """Owns the cached answer and the background thread that refreshes it.

    The app holds one instance. ``status()`` is cheap and never touches the
    network; ``refresh()`` does the work and is what the background thread and
    an explicit user-triggered refresh both call.
    """

    def __init__(
        self,
        config_path: Path,
        root: Optional[Path] = None,
        cache_path: Optional[Path] = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        upgrade_command: str = DEFAULT_UPGRADE_COMMAND,
    ) -> None:
        """Create a checker.

        Args:
            config_path: path to config.json, read for ``updates.remote``.
            root: repository root, defaults to the resolved source tree root.
            cache_path: where the answer is persisted between restarts.
                Defaults to :func:`default_cache_path` under ``root``.
            interval_seconds: delay between background refreshes.
            upgrade_command: the command shown to the user. Never executed.
        """
        self._config_path = config_path
        self._root = root or repo_root()
        self._cache_path = cache_path or default_cache_path(self._root)
        self._interval = interval_seconds
        self._upgrade_command = upgrade_command
        self._lock = threading.Lock()
        self._status: UpdateStatus = self._load_cache() or self._unknown(
            "no check has run yet"
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- public API --------------------------------------------------------

    def status(self) -> UpdateStatus:
        """Return the cached answer. Never blocks, never hits the network."""
        with self._lock:
            return self._status

    def start(self) -> None:
        """Start the background refresh thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="update-check", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to exit. Safe to call when not started."""
        self._stop.set()

    def refresh(self) -> UpdateStatus:
        """Run one check now and store the result.

        Returns:
            The new status. Any failure produces STATUS_UNKNOWN with a reason,
            never a silent fall-back to STATUS_CURRENT.
        """
        remote = self.resolve_remote()
        current = resolve_version(self._root)
        if not current:
            return self._store(self._unknown("the installed version did not resolve", remote))

        parsed_current = parse_version(current)
        if parsed_current is None:
            return self._store(
                self._unknown(
                    f"this install is at {current}, which is not a release tag", remote
                )
            )

        try:
            tags = fetch_remote_tags(remote)
        except UpdateCheckError as exc:
            return self._store(self._unknown(str(exc), remote, current))

        if not tags:
            return self._store(
                self._unknown("the remote published no release tags", remote, current)
            )

        # Pick the maximum here rather than trusting the list order. Version
        # ordering is numeric, not lexical (1.10.0 beats 1.9.0), and a caller
        # handing back an unsorted list must not silently produce a wrong
        # "latest".
        ranked = [(parse_version(tag), tag) for tag in tags]
        ranked = [(parsed, tag) for parsed, tag in ranked if parsed is not None]
        if not ranked:
            return self._store(
                self._unknown("the remote published no release tags", remote, current)
            )
        parsed_latest, latest = max(ranked)
        newer = parsed_latest > parsed_current
        return self._store(
            UpdateStatus(
                status=STATUS_UPDATE_AVAILABLE if newer else STATUS_CURRENT,
                current_version=current,
                latest_version=latest,
                remote=remote,
                checked_at=time.time(),
                reason="",
                upgrade_command=self._upgrade_command,
            )
        )

    def resolve_remote(self) -> str:
        """Decide which remote to consult. See the module docstring.

        Returns:
            An explicit ``updates.remote`` from config.json, else the
            checkout's ``origin``, else FALLBACK_REMOTE.
        """
        return (
            read_configured_remote(self._config_path)
            or discover_origin_remote(self._root)
            or FALLBACK_REMOTE
        )

    # -- internals ---------------------------------------------------------

    def _loop(self) -> None:
        """Background thread body: refresh, then sleep, until stopped."""
        # A short initial delay keeps the check off the startup critical path.
        if self._stop.wait(30):
            return
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - a checker crash must not kill the app
                logger.warning("update_check_refresh_failed", exc_info=True)
            if self._stop.wait(self._interval):
                return

    def _unknown(self, reason: str, remote: str = "", current: str = "") -> UpdateStatus:
        """Build an UNKNOWN status.

        Args:
            reason: short human sentence describing what could not be done.
            remote: the remote that was being consulted, if known.
            current: the installed version, if it resolved.

        Returns:
            An UpdateStatus in STATUS_UNKNOWN.
        """
        return UpdateStatus(
            status=STATUS_UNKNOWN,
            current_version=current,
            latest_version="",
            remote=remote,
            checked_at=time.time(),
            reason=reason,
            upgrade_command=self._upgrade_command,
        )

    def _store(self, status: UpdateStatus) -> UpdateStatus:
        """Cache a status in memory and on disk.

        Args:
            status: the answer to store.

        Returns:
            The same status, for call chaining.
        """
        with self._lock:
            self._status = status
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(status.to_dict(), indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("update_check_cache_write_failed", error=str(exc))
        return status

    def _load_cache(self) -> Optional[UpdateStatus]:
        """Load the persisted answer from a previous run.

        Returns:
            The cached UpdateStatus, or None when absent or malformed. A
            malformed cache is treated as no cache, not as a passing check.
        """
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return UpdateStatus(
                status=str(data["status"]),
                current_version=str(data.get("current_version", "")),
                latest_version=str(data.get("latest_version", "")),
                remote=str(data.get("remote", "")),
                checked_at=float(data.get("checked_at", 0.0)),
                reason=str(data.get("reason", "")),
                upgrade_command=str(data.get("upgrade_command", self._upgrade_command)),
            )
        except (KeyError, TypeError, ValueError):
            return None
