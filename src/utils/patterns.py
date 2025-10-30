"""Pattern detection for Claude Code output."""

import re
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass
import structlog

logger = structlog.get_logger()


@dataclass
class PatternMatch:
    """Represents a pattern match result."""
    pattern_name: str
    matched_text: str
    groups: tuple
    line_number: int


class PatternDetector:
    """Detects patterns in terminal output."""

    # Regex patterns for detection
    PATTERNS = {
        # Detect localhost server URLs with ports (including IPv6 format)
        "localhost_server": re.compile(
            r"(?:https?://)?localhost:(\d+)|"
            r"(?:https?://)?127\.0\.0\.1:(\d+)|"
            r"(?:https?://)?0\.0\.0\.0:(\d+)|"
            r"(?:https?://)?(?:\[::\]|\[::1\]):(\d+)"  # IPv6 localhost
        ),

        # Detect "server ready" type messages
        "server_ready": re.compile(
            r"(?:server|development server|dev server).*?"
            r"(?:running|listening|started|ready|available)",
            re.IGNORECASE
        ),

        # Detect port numbers in "listening on" messages
        "listening_on_port": re.compile(
            r"(?:listening|running|started|serving).*?(?:port|on|:)\s*(\d+)",
            re.IGNORECASE
        ),

        # Detect errors
        "error": re.compile(
            r"(?:ERROR|Error|error|FAIL|Failed|failed):",
            re.IGNORECASE
        ),

        # Detect warnings
        "warning": re.compile(
            r"(?:WARNING|Warning|warning|WARN):",
            re.IGNORECASE
        ),

        # Detect file creation/writing
        "file_created": re.compile(
            r"(?:Created|Writing|Saved|Wrote).*?(?:file|to):\s*(.+)",
            re.IGNORECASE
        ),

        # Detect build/compile completion
        "build_complete": re.compile(
            r"(?:build|compilation|compile).*?(?:complete|successful|finished|done)",
            re.IGNORECASE
        ),

        # Detect test results
        "test_result": re.compile(
            r"(?:tests?|specs?).*?(?:passed|failed|complete|finished)",
            re.IGNORECASE
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
                    line_number=line_number
                )
                matches.append(pattern_match)

                logger.debug(
                    "pattern_detected",
                    pattern=pattern_name,
                    text=text[:100] + "..." if len(text) > 100 else text,
                    line=line_number
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
                                error=str(e)
                            )

        return matches

    def extract_port(self, text: str) -> Optional[int]:
        """
        Extract port number from text.

        Args:
            text: Text to search for port

        Returns:
            Port number if found, None otherwise
        """
        # Try localhost_server pattern first
        match = self.PATTERNS["localhost_server"].search(text)
        if match:
            # Check which group matched (since we have multiple alternatives)
            for group in match.groups():
                if group:
                    return int(group)

        # Try listening_on_port pattern
        match = self.PATTERNS["listening_on_port"].search(text)
        if match and match.group(1):
            return int(match.group(1))

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
