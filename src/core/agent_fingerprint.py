"""Agent fingerprint detector for externally-adopted tmux sessions.

Phase 7 of the theme system. Pure, side-effect-free string matcher: given
captured scrollback bytes (already decoded to str), return which AI CLI is
running inside, or ``None`` if we can't tell.

Patterns were authored from real captures saved during the dependency-
resolution phase at ``/Users/Adam/Dropbox/llmScratch/{agent}.txt``. Do NOT
modify them without re-deriving from fresh captures — ordering matters
(most-specific glyph wins) and anchors are tuned to avoid false positives
against generic box-drawing TUI frames.
"""
from __future__ import annotations

import re
from typing import Optional, Pattern, Union

# Priority order: openclaw → hermes → codex → claude. First match wins
# (across both agents and patterns within an agent). Python 3.7+ preserves
# dict insertion order, so the literal table order IS the priority order.
AGENT_FINGERPRINTS: dict[str, list[Union[str, Pattern[str]]]] = {
    "openclaw": [
        "🦞 OpenClaw",
        re.compile(r"^\s*openclaw tui - ws://[\d.]+:\d+ - agent \S+ - session \S+", re.M),
        "I'm not magic—I'm just extremely persistent with retries",
    ],
    "hermes": [
        re.compile(r"Hermes Agent v[\d.]+"),
        "Welcome to Hermes Agent! Type your message or /help",
        re.compile(r"⚕\s+\S+\s+│\s+ctx\s+\S+\s+│\s+\[[░▒▓█\s]*\]"),
    ],
    "codex": [
        ">_ OpenAI Codex (v",
        re.compile(r"^\s*gpt-[\d.]+\s+(default\s+)?fast\s+·\s+~", re.M),
        "/model to change",
    ],
    "claude": [
        "Claude Code'll be able to read, edit, and execute files",
        re.compile(r"^\s*❯\s*1\.\s*Yes, I trust this folder", re.M),
        "Security guide",
    ],
}


def _scan(tail: str) -> Optional[str]:
    """One pass over the fingerprint table against the given window."""
    for agent_type, patterns in AGENT_FINGERPRINTS.items():
        for pattern in patterns:
            if isinstance(pattern, str):
                if pattern in tail:
                    return agent_type
            else:  # compiled regex
                if pattern.search(tail):
                    return agent_type
    return None


def detect_agent_type(scrollback: Optional[str]) -> Optional[str]:
    """Identify which AI CLI produced ``scrollback``, or return ``None``.

    Two-pass strategy:
      1. Last 50 lines — catches steady-state prompts/status lines.
      2. Last 2000 lines — catches boot banners that haven't scrolled off.
    Returns ``None`` for empty / unrecognizable input. Never raises.
    """
    if not scrollback:
        return None
    lines = scrollback.splitlines()
    tail = "\n".join(lines[-50:])
    hit = _scan(tail)
    if hit is not None:
        return hit
    if len(lines) <= 50:
        return None  # already scanned everything
    tail2k = "\n".join(lines[-2000:])
    return _scan(tail2k)
