"""Pattern detection for Claude Code output.

Plan v3.2: tunnel system was demolished and replaced with a detection-only
"Local Servers" panel. This module is now the single source of truth for
turning raw pane bytes into validated dev-server port numbers.

False-positive history (do not regress):
    The previous ``listening_on_port`` regex was loose enough to match
    digits in the Claude TUI status row — e.g. ``"running ✦ 15.3k tokens
    · 31% context left"`` would extract port ``15`` or ``31``. The current
    pattern set is anchored on URL-shaped tokens, server-ready verbs, or
    a literal ``port`` keyword so unrelated digits in TUI chrome never
    match.
"""

import re
import socket
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass
import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Port helpers (used by both the regex layer and the LocalServersTracker
# janitor / record path).
# ---------------------------------------------------------------------------

def is_valid_dev_port(port: int) -> bool:
    """True iff ``port`` is a non-privileged TCP port (1024-65535).

    Single chokepoint that gates EVERY value entering the local-servers
    tracker. Privileged ports (1-1023) are rejected outright — dev servers
    don't bind there, and matching a small int from TUI text (``"15.3k
    tokens"``) can't survive this guard. Anything outside the int range or
    not strictly numeric returns False rather than raising.
    """
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        return False
    return 1024 <= port_int <= 65535


def port_is_listening(port: int, timeout_ms: int = 250) -> bool:
    """Return True iff a TCP listener is accepting on ``127.0.0.1:port``.

    Uses ``socket.connect_ex`` with a short timeout so the call never
    hangs the caller. Designed to be cheap enough to run from both the
    initial detection path AND the periodic janitor sweep that retires
    dead entries.

    Args:
        port: TCP port to probe. Caller should have already gated through
            ``is_valid_dev_port``; we re-check defensively.
        timeout_ms: connect timeout in milliseconds. Default 250ms — long
            enough for a same-host listener to ack, short enough that a
            full janitor sweep across N tracked ports stays under a second.

    Returns:
        True if ``connect_ex`` returns 0 (connection accepted).
        False on any other return value, exception, or invalid port.
    """
    if not is_valid_dev_port(port):
        return False

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(max(0.001, timeout_ms / 1000.0))
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


@dataclass
class PatternMatch:
    """Represents a pattern match result."""
    pattern_name: str
    matched_text: str
    groups: tuple
    line_number: int


class PatternDetector:
    """Detects patterns in terminal output."""

    # Regex patterns for detection.
    #
    # Port detection uses tight alternations rather than a loose verb-then-
    # digits sweep. Each alternative requires a structural anchor (URL
    # host:port, ``port`` keyword, or a server-ready verb followed by ``on``
    # / ``at``) so digits embedded in TUI status text never match.
    PATTERNS = {
        # Detect localhost server URLs with ports (including IPv6 format).
        # Shape: optional scheme + literal host + ``:`` + 2-5 digits.
        # Digit-length bound prevents matching things like timestamps.
        "localhost_server": re.compile(
            r"(?:https?://)?localhost:(\d{2,5})\b|"
            r"(?:https?://)?127\.0\.0\.1:(\d{2,5})\b|"
            r"(?:https?://)?0\.0\.0\.0:(\d{2,5})\b|"
            r"(?:https?://)?(?:\[::\]|\[::1\]):(\d{2,5})\b"
        ),

        # Detect "server ready" type messages (no capture group).
        "server_ready": re.compile(
            r"(?:server|development server|dev server).*?"
            r"(?:running|listening|started|ready|available)",
            re.IGNORECASE,
        ),

        # Detect ``listening on port N``, ``serving at 8000``, etc. The
        # set of verbs is intentionally narrow — ``running`` and ``started``
        # were dropped because they appear in the Claude TUI status row
        # (``running ✦ 15.3k tokens``) and produced false-positive ports.
        "listening_on_port": re.compile(
            r"\b(?:listening|serving|bound|bind)\s+(?:on|at)\s+"
            r"(?:port\s+)?(\d{2,5})\b",
            re.IGNORECASE,
        ),

        # Bare ``port N`` keyword. Anchored on a word boundary on both
        # sides so ``"31%"`` / ``"15.3k"`` never match.
        "port_keyword": re.compile(
            r"\bport\s+(\d{2,5})\b",
            re.IGNORECASE,
        ),

        # Detect URLs with explicit ports — covers cases like
        # ``http://example.dev:8080/`` where the host is neither localhost
        # nor a loopback IP. The host token excludes ``/`` and ``:`` so
        # it never spans a path or another colon.
        "url_with_port": re.compile(
            r"https?://[^\s/:]+:(\d{2,5})\b",
        ),

        # Detect errors
        "error": re.compile(
            r"(?:ERROR|Error|error|FAIL|Failed|failed):",
            re.IGNORECASE,
        ),

        # Detect warnings
        "warning": re.compile(
            r"(?:WARNING|Warning|warning|WARN):",
            re.IGNORECASE,
        ),

        # Detect file creation/writing
        "file_created": re.compile(
            r"(?:Created|Writing|Saved|Wrote).*?(?:file|to):\s*(.+)",
            re.IGNORECASE,
        ),

        # Detect build/compile completion
        "build_complete": re.compile(
            r"(?:build|compilation|compile).*?(?:complete|successful|finished|done)",
            re.IGNORECASE,
        ),

        # Detect test results
        "test_result": re.compile(
            r"(?:tests?|specs?).*?(?:passed|failed|complete|finished)",
            re.IGNORECASE,
        ),
    }

    def __init__(self):
        """Initialize the pattern detector."""
        self.callbacks: Dict[str, List[Callable]] = {}

    def register_callback(self, pattern_name: str, callback: Callable[[PatternMatch], None]):
        """
        Register a callback for a specific pattern.

        Args:
            pattern_name: Name of the pattern to watch
            callback: Function to call when pattern matches
        """
        if pattern_name not in self.callbacks:
            self.callbacks[pattern_name] = []
        self.callbacks[pattern_name].append(callback)
        logger.debug("pattern_callback_registered", pattern=pattern_name)

    def detect_patterns(self, text: str, line_number: int = 0) -> List[PatternMatch]:
        """
        Detect all patterns in the given text.

        Args:
            text: Text to search for patterns
            line_number: Line number for context

        Returns:
            List of pattern matches
        """
        matches = []

        for pattern_name, regex in self.PATTERNS.items():
            match = regex.search(text)
            if match:
                pattern_match = PatternMatch(
                    pattern_name=pattern_name,
                    matched_text=match.group(0),
                    groups=match.groups(),
                    line_number=line_number,
                )
                matches.append(pattern_match)

                logger.debug(
                    "pattern_detected",
                    pattern=pattern_name,
                    text=text[:100] + "..." if len(text) > 100 else text,
                    line=line_number,
                )

                # Trigger callbacks
                if pattern_name in self.callbacks:
                    for callback in self.callbacks[pattern_name]:
                        try:
                            callback(pattern_match)
                        except Exception as e:
                            logger.error(
                                "pattern_callback_error",
                                pattern=pattern_name,
                                error=str(e),
                            )

        return matches

    def extract_port(self, text: str) -> Optional[int]:
        """Extract a port number from terminal text.

        Walks the structural patterns in priority order (most specific to
        least specific) and returns the first match that survives
        ``is_valid_dev_port``. Privileged-port matches (e.g. ``80``,
        ``443``) and out-of-range numbers are rejected even when the
        regex hits.

        Returns:
            The validated dev port (1024-65535) or None if nothing matched.
        """
        for pattern_name in (
            "localhost_server",
            "url_with_port",
            "listening_on_port",
            "port_keyword",
        ):
            regex = self.PATTERNS[pattern_name]
            match = regex.search(text)
            if not match:
                continue
            for group in match.groups():
                if not group:
                    continue
                try:
                    candidate = int(group)
                except (TypeError, ValueError):
                    continue
                if is_valid_dev_port(candidate):
                    return candidate
        return None

    def is_server_ready(self, text: str) -> bool:
        """
        Check if text indicates server is ready.

        Args:
            text: Text to check

        Returns:
            True if server ready message detected
        """
        return bool(self.PATTERNS["server_ready"].search(text))

    def has_error(self, text: str) -> bool:
        """
        Check if text contains an error.

        Args:
            text: Text to check

        Returns:
            True if error detected
        """
        return bool(self.PATTERNS["error"].search(text))

    def has_warning(self, text: str) -> bool:
        """
        Check if text contains a warning.

        Args:
            text: Text to check

        Returns:
            True if warning detected
        """
        return bool(self.PATTERNS["warning"].search(text))


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------
# Tests and the LocalServersTracker import these directly so they don't
# need to instantiate a PatternDetector to validate one chunk of text.
_DEFAULT_DETECTOR: Optional[PatternDetector] = None


def _detector() -> PatternDetector:
    global _DEFAULT_DETECTOR
    if _DEFAULT_DETECTOR is None:
        _DEFAULT_DETECTOR = PatternDetector()
    return _DEFAULT_DETECTOR


def extract_port(text: str) -> Optional[int]:
    """Module-level convenience — see :meth:`PatternDetector.extract_port`."""
    return _detector().extract_port(text)
