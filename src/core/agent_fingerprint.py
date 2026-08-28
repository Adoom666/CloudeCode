"""Agent fingerprint detector for externally-adopted tmux sessions.

Phase 7 of the theme system. Pure, side-effect-free string matcher: given
captured scrollback bytes (already decoded to str), return which AI CLI is
running inside, or ``None`` if we can't tell.

Evidence status per family, current as of the 2026-08-18 audit:

- ``claude``: patterns are derived from nine real live tmux captures of
  Claude Code v2.1.223 (see ``tests/fixtures/agent_captures/``), covering
  the wide two-column welcome box, the narrow single-line welcome box, the
  bare-name compact box (no version number), the steady-state status footer
  in both permission modes, the verbose/transcript-view footer, the OAuth
  device-login screen, and a partial-repaint frame with a duplicated banner.
  The original patterns only matched the first-launch trust dialog, so
  every one of those nine real sessions - all long past onboarding -
  fingerprinted as ``None``. The patterns below add steady-state anchors
  while keeping the original onboarding anchors intact.
- ``openclaw``, ``hermes``, ``codex``: UNVERIFIED. No live instance of any
  of these three was reachable to capture (no running process, no binary on
  PATH, no other tmux socket) during this audit. Their patterns are
  unchanged from the prior author's best-effort guesses and carry the same
  known risk class the claude patterns had (anchored to what looks like a
  boot banner, which may never appear again once the pane scrolls past
  it). Do not treat them as validated. Re-derive from a real capture before
  trusting them the way the claude patterns are now trusted.

Anchoring rules that keep this precise rather than merely permissive:
  - Every banner/box pattern requires the box-drawing corner or the
    multi-glyph logo art directly adjacent to the agent name, so a plain
    sentence that mentions "Claude Code" (a doc, a commit message, another
    agent's quoted output) does not match - only an actually-rendered
    banner does.
  - The steady-state footer patterns key off Claude Code's own chrome
    (the "⏵⏵ bypass permissions on" / "⏸ manual mode on" mode glyphs, the
    "Showing detailed transcript · ctrl+o to toggle" verbose-view line),
    not off the model name shown next to them - a model name alone
    ("Sonnet 5", "claude-sonnet-5") is not proof of which CLI is driving
    it, since another tool could point at the same API and print the same
    model string.
  - Known accepted gap: because the fingerprint table itself is Python
    source containing these exact literal strings, a pane showing this
    very file's source (e.g. this module open in a different agent's
    editor) can self-match. No text-substring fingerprinting scheme can
    fully escape that; it is not attempted here.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Pattern, Union

# Priority order: openclaw → hermes → codex → claude. First match wins
# (across both agents and patterns within an agent). Python 3.7+ preserves
# dict insertion order, so the literal table order IS the priority order.
AGENT_FINGERPRINTS: dict[str, list[Union[str, Pattern[str]]]] = {
    "openclaw": [
        "🦞 OpenClaw",
        re.compile(r"^\s*openclaw tui - ws://[\d.]+:\d+ - agent \S+ - session \S+", re.M),
        "I'm not magic-I'm just extremely persistent with retries",
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
        # First-launch trust dialog (original patterns, unchanged).
        "Claude Code'll be able to read, edit, and execute files",
        re.compile(r"^\s*❯\s*1\.\s*Yes, I trust this folder", re.M),
        "Security guide",
        # Welcome banner, drawn at session start and redrawn after /clear.
        # Two real box layouts: the wide two-column "tips" box and the
        # narrow single box, both with or without a trailing version
        # number. Anchored to the box corner so it cannot match "Claude
        # Code" mentioned in running prose.
        re.compile(r"^\s*╭[─\s]*Claude Code(?:\s+v[\d.]+)?\s*[─╮]", re.M),
        # Ultra-compact single-line banner used at very narrow terminal
        # widths: the block-art logo glyph and the name on one line, no
        # box at all. The 6-character glyph run is distinctive enough on
        # its own that requiring it adjacent to the name is sufficient.
        re.compile(r"▐▛███▜▌\s+Claude Code(?:\s+v[\d.]+)?"),
        # Steady-state status-line footer: Claude Code's permission-mode
        # indicator, present on every live session regardless of what
        # conversation content is on screen above it. Tolerates line
        # truncation at narrow widths (the phrase itself is never cut).
        "⏵⏵ bypass permissions on",
        "⏸ manual mode on",
        # Verbose/transcript-view footer - a distinct steady-state screen
        # reached via ctrl+o, no permission-mode glyph shown there instead.
        "Showing detailed transcript · ctrl+o to toggle",
        # OAuth device-login screen.
        re.compile(r"claude\.com/cai/oauth/authorize"),
    ],
}


logger = logging.getLogger(__name__)


def _matching_families(tail: str) -> set[str]:
    """Every agent family with at least one fingerprint hit in ``tail``.

    Inputs:
        tail (str): a window of scrollback text to search.

    Outputs:
        set[str]: agent family names (keys of ``AGENT_FINGERPRINTS``) whose
        pattern list has at least one hit. Empty when nothing matched. Two
        or more entries means the window carries markers from more than one
        family at once (ambiguous), which the caller must not resolve by
        picking one - see ``_resolve``.
    """
    hits: set[str] = set()
    for agent_type, patterns in AGENT_FINGERPRINTS.items():
        for pattern in patterns:
            if isinstance(pattern, str):
                matched = pattern in tail
            else:  # compiled regex
                matched = pattern.search(tail) is not None
            if matched:
                hits.add(agent_type)
                break
    return hits


def _resolve(tail: str) -> Optional[str]:
    """Collapse ``_matching_families`` into a single verdict for one window.

    Inputs:
        tail (str): a window of scrollback text to search.

    Outputs:
        Optional[str]: the single matching family, or ``None`` when either
        no family matched (absence of evidence) or more than one did
        (conflicting evidence - a wrong confident guess is worse than an
        honest unknown here, so this is never resolved by table-order
        priority). The two ``None`` cases are distinguished in the log, not
        in the return value: absence is silent, ambiguity logs a warning
        naming every family that matched so the conflict is diagnosable
        without changing the caller-facing contract.
    """
    hits = _matching_families(tail)
    if len(hits) == 1:
        return next(iter(hits))
    if len(hits) > 1:
        logger.warning(
            "agent_fingerprint: ambiguous match across families %s - "
            "returning None rather than guessing",
            ", ".join(sorted(hits)),
        )
    return None


def detect_agent_type(scrollback: Optional[str]) -> Optional[str]:
    """Identify which AI CLI produced ``scrollback``, or return ``None``.

    Two-pass strategy:
      1. Last 50 lines - catches steady-state prompts/status lines.
      2. Last 2000 lines - catches boot banners that haven't scrolled off.
    Returns ``None`` for empty / unrecognizable input, and also for
    ambiguous input where more than one family's patterns matched the same
    window - never falls back to a default or a priority-order guess (see
    ``_resolve``). Never raises.
    """
    if not scrollback:
        return None
    lines = scrollback.splitlines()
    tail = "\n".join(lines[-50:])
    hit = _resolve(tail)
    if hit is not None:
        return hit
    if len(lines) <= 50:
        return None  # already scanned everything
    tail2k = "\n".join(lines[-2000:])
    return _resolve(tail2k)
