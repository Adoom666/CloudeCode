"""Phase 7 - agent fingerprint detector tests.

Verifies ``detect_agent_type`` against real captured scrollback for Claude
Code (nine live tmux sessions captured 2026-08-18, redacted, committed as
fixtures under ``tests/fixtures/agent_captures/``), plus synthetic edge
cases: empty input, unrelated TUI output, a banner buried under noise, a
plain shell whose scrollback merely mentions "claude", an editor buffer
listing agent family names in prose, one agent's banner text quoted inside
another agent's transcript, and generic box-drawing frames.

openclaw/hermes/codex have no real captures available (see the audit note
in ``src/core/agent_fingerprint.py``) and are intentionally not covered by
a "real capture detects" case here - only synthetic pattern-shape checks.
"""
from __future__ import annotations

from pathlib import Path

from src.core.agent_fingerprint import detect_agent_type

FIXTURES = Path(__file__).parent / "fixtures" / "agent_captures"


def _load(fname: str) -> str:
    """Read a committed real-capture fixture.

    Inputs:
        fname (str): file name under ``tests/fixtures/agent_captures/``.

    Outputs:
        str: fixture text, decoded permissively (real terminal dumps can
        carry stray bytes).
    """
    return (FIXTURES / fname).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Real captures - all nine live Claude Code screen shapes seen on the fleet
# resolve to "claude". These are the fixtures that were impossible to make
# pass before the steady-state patterns were added: none of them shows the
# onboarding trust dialog, all are long-running sessions.
# ---------------------------------------------------------------------------


def test_real_capture_running_bypass_mode() -> None:
    assert detect_agent_type(_load("claude_running_bypass.txt")) == "claude"


def test_real_capture_running_manual_mode() -> None:
    assert detect_agent_type(_load("claude_running_manual_mode.txt")) == "claude"


def test_real_capture_narrow_terminal_truncated_footer() -> None:
    assert detect_agent_type(_load("claude_narrow_terminal.txt")) == "claude"


def test_real_capture_oauth_login_screen() -> None:
    assert detect_agent_type(_load("claude_login_oauth.txt")) == "claude"


def test_real_capture_verbose_transcript_view() -> None:
    assert detect_agent_type(_load("claude_verbose_transcript.txt")) == "claude"


def test_real_capture_partial_repaint_duplicated_banner() -> None:
    assert detect_agent_type(_load("claude_partial_repaint.txt")) == "claude"


def test_real_capture_plain_shell_mentioning_claude_is_none() -> None:
    """A real captured shell prompt whose banner/paths say "claude" in
    several places (.claude/ dir, "Claude Config" status line) but is not
    running Claude Code at all. Must not false-positive."""
    assert detect_agent_type(_load("negative_plain_shell_mentions_claude.txt")) is None


def test_real_capture_blank_pane_is_none() -> None:
    """A genuinely empty pane (idle shell, nothing on screen). No evidence
    either way - None, not a guess."""
    assert detect_agent_type(_load("blank_pane.txt")) is None


# ---------------------------------------------------------------------------
# Per-anchor isolation. Each real capture above legitimately carries several
# markers at once (a real running screen always does), so deleting any one
# pattern often still leaves another pattern from the same fixture matching
# and the composite test still passes for the wrong reason. These cases
# isolate exactly one anchor per scrollback with nothing else Claude-shaped
# present, so each pattern has its own independent proof of necessity.
# ---------------------------------------------------------------------------


def test_pattern_bypass_footer_alone_detects_claude() -> None:
    assert detect_agent_type("⏵⏵ bypass permissions on (shift+tab to cycle)") == "claude"


def test_pattern_manual_mode_footer_alone_detects_claude() -> None:
    assert detect_agent_type("⏸ manual mode on · ← for agents") == "claude"


def test_pattern_verbose_transcript_footer_alone_detects_claude() -> None:
    assert detect_agent_type(
        "Showing detailed transcript · ctrl+o to toggle · ↑↓ scroll"
    ) == "claude"


def test_pattern_oauth_url_alone_detects_claude() -> None:
    assert detect_agent_type(
        "https://claude.com/cai/oauth/authorize?code=true&client_id=x"
    ) == "claude"


def test_pattern_wide_box_banner_with_version_alone_detects_claude() -> None:
    assert detect_agent_type(
        "╭─── Claude Code v2.1.223 ───────────────────────────╮"
    ) == "claude"


def test_pattern_narrow_box_banner_without_version_alone_detects_claude() -> None:
    """The compact box header has no version number at all - a pattern
    that silently required one would miss this real, live layout."""
    assert detect_agent_type("╭─ Claude Code ───────────────────────────╮") == "claude"


def test_pattern_compact_one_line_banner_alone_detects_claude() -> None:
    assert detect_agent_type(" ▐▛███▜▌   Claude Code v2.1.223") == "claude"


def test_pattern_box_banner_does_not_match_plain_prose_mentioning_claude_code() -> None:
    """The box-drawing anchor is what keeps this from matching a sentence
    that merely mentions the product name."""
    assert detect_agent_type(
        "The docs say Claude Code is a terminal tool, nothing box-drawn here."
    ) is None


# ---------------------------------------------------------------------------
# Synthetic edge cases
# ---------------------------------------------------------------------------


def test_empty_returns_none() -> None:
    assert detect_agent_type("") is None
    assert detect_agent_type(None) is None


def test_unrelated_returns_none() -> None:
    assert detect_agent_type(
        "just some random TUI output\n$ ls\nfile.txt"
    ) is None


def test_banner_buried_under_noise_caught_only_by_second_pass() -> None:
    """Banner at line 0, then 60+ lines of noise → first pass (50 lines)
    misses; second pass (2000 lines) catches it."""
    banner = "\U0001f99e OpenClaw something something boot line\n"
    noise = "\n".join(f"noise line {i}" for i in range(80))
    scrollback = banner + noise

    # Confirm the 50-line tail does NOT contain the banner.
    tail = "\n".join(scrollback.splitlines()[-50:])
    assert "OpenClaw" not in tail

    # But the full detector (two-pass) finds it.
    assert detect_agent_type(scrollback) == "openclaw"


def test_first_pass_window_is_actually_limited_to_last_50_lines() -> None:
    """A stale marker from a DIFFERENT family that has scrolled out of the
    last 50 lines must not be pulled back into the first pass and turn a
    confident match into a spurious ambiguity. If the tail-50 window were
    accidentally widened to the full 2000-line window on the first pass,
    this would see both the old openclaw line and the fresh claude footer
    at once and wrongly return None instead of "claude"."""
    stale_openclaw_marker = "\U0001f99e OpenClaw something something boot line\n"
    noise = "\n".join(f"noise line {i}" for i in range(80))
    fresh_claude_marker = "\n⏵⏵ bypass permissions on"
    scrollback = stale_openclaw_marker + noise + fresh_claude_marker

    tail50 = "\n".join(scrollback.splitlines()[-50:])
    assert "OpenClaw" not in tail50  # the stale marker really did scroll off

    assert detect_agent_type(scrollback) == "claude"


def test_box_drawing_frames_are_not_a_false_positive() -> None:
    """Generic TUI box-drawing alone must not trigger any agent.

    The fingerprints anchor on agent-specific glyphs, not on
    ``╭─`` / ``╰─`` corners which appear in countless CLIs.
    """
    junk = "\n".join(
        [
            "╭─ some box ─╮",
            "│ inner txt │",
            "╰────────────╯",
            "$ generic shell prompt",
        ]
    )
    assert detect_agent_type(junk) is None


def test_editor_buffer_listing_agent_names_in_prose_is_none() -> None:
    """A doc/editor buffer that discusses all four agent families by name,
    in ordinary prose, with no rendered TUI chrome from any of them. This
    is the shape a README or a status report takes - must not match."""
    prose = "\n".join(
        [
            "  1  # Supported agent families",
            "  2  This tool detects Claude Code, Codex, Hermes and OpenClaw",
            "  3  sessions by fingerprinting their scrollback.",
            "  4  claude, codex, hermes, openclaw are the four keys.",
            "  5  # end of file",
        ]
    )
    assert detect_agent_type(prose) is None


def test_openclaw_banner_quoted_inside_claude_conversation_is_ambiguous_none() -> None:
    """Another agent's real banner text genuinely present in the tail (e.g.
    pasted into a Claude Code conversation as a quote) makes the window
    carry markers from two different families at once. That is ambiguous
    evidence, not a tiebreak - the detector must not silently prefer
    whichever family happens to sort first in the priority table. Precision
    over recall: an honest None beats a confident wrong family name."""
    scrollback = (
        "╭─── Claude Code v2.1.223 ───╮\n"
        "│ user@example.com's Org      │\n"
        "╰──────────────────────────────╯\n"
        "❯ what's the openclaw banner look like again?\n"
        "⮕ Something like this: \U0001f99e OpenClaw\n"
        "⏵⏵ bypass permissions on\n"
    )
    assert detect_agent_type(scrollback) is None


def test_ambiguous_evidence_between_two_families_returns_none_not_a_coin_flip() -> None:
    """Two distinct, independently-real markers in the same tail (a
    Codex-shaped model line plus Claude Code's own steady-state footer) is
    conflicting evidence, not a coin flip to be settled by table order.
    Deterministic across repeated calls, and the deterministic answer is
    always None for this input - never one family picked arbitrarily."""
    scrollback = (
        "gpt-4 default fast · ~\n"
        "⏵⏵ bypass permissions on\n"
    )
    first = detect_agent_type(scrollback)
    second = detect_agent_type(scrollback)
    assert first is None
    assert second is None


def test_ambiguous_match_logs_a_warning_distinct_from_silent_absence(caplog) -> None:
    """Both the zero-match case and the multi-match case return None, but
    only the multi-match (ambiguous) case is observable as a distinct event
    in the log - that is how ambiguity stays distinguishable from plain
    absence without changing the public return type."""
    import logging

    scrollback = (
        "gpt-4 default fast · ~\n"
        "⏵⏵ bypass permissions on\n"
    )
    with caplog.at_level(logging.WARNING, logger="src.core.agent_fingerprint"):
        assert detect_agent_type(scrollback) is None
    assert "ambiguous" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.core.agent_fingerprint"):
        assert detect_agent_type("just some random TUI output\n$ ls\nfile.txt") is None
    assert "ambiguous" not in caplog.text
