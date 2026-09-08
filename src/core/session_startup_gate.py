"""Is this session blocked on an unanswered STARTUP prompt?

A freshly launched claude that is parked on its folder-trust dialog is a
live pane, running a real claude process, with a pid tmux will happily
report - and it has fired NO lifecycle hook at all. Every signal the
status machine reads therefore says "fine", so the row painted a green
Connected dot while the session sat there waiting for a keypress nobody
on a phone could see. That is punchlist item 19.

THE SIGNAL IS THE ABSENCE OF A HOOK, AND IT WAS MEASURED, NOT ASSUMED.
Controlled experiment on the owner's box, 2026-09-08, claude 2.1.263, on
a throwaway ``tmux -L cloude-test`` socket with the hook POSTs pointed at
a local listener:

  - Launched in an UNTRUSTED directory: pane born at epoch 1788880316,
    alive, ``#{pane_dead}=0``. At +15s the trust dialog was on screen and
    the listener had received ZERO POSTs. The dialog was answered at
    +34s; the FIRST hook of the session, ``SessionStart``, landed 1.76s
    later, at +35.8s.
  - Launched in the SAME directory once trusted, three consecutive runs:
    ``SessionStart`` landed 0.49s, 0.50s and 0.41s after pane birth.

So a hook for this instance is positive proof startup finished, and
roughly half a second is the normal cost of getting one.

WHY A SEPARATE FIELD AND NOT A SIXTH ``activity_status``. The activity
vocabulary in ``session_status.py`` describes what the AGENT is doing
once it is running. This describes whether it has started running at
all, which is a different question with a different evidence base (no
hook has EVER fired here, so the activity machine is by definition in
its tmux-fallback path). Folding them would make every existing consumer
of ``activity_status`` handle a state it has no vocabulary for.

THE THREE OUTCOMES, AND WHAT EACH ONE CLAIMS.

  ``ready``                    measured NO. Either a hook has fired for
                               this instance, or the pane's process is
                               gone, or the scrollback was READ and
                               carries no startup prompt. Note the exact
                               claim: "not blocked on a startup prompt".
                               It is NOT a claim that the session is
                               healthy - a dead pane answers ``ready``
                               here because a corpse is certainly not
                               waiting for a keypress, and
                               ``activity_status`` is the field that says
                               it is dead.
  ``awaiting_startup_prompt``  measured YES. A live pane, past the grace
                               window, no hook for this instance, and a
                               startup prompt POSITIVELY MATCHED in the
                               scrollback tail.
  ``unknown``                  could not determine. Pane liveness did not
                               answer, the instance could not be dated,
                               the grace window has not elapsed, or the
                               tail could not be captured. Not having
                               looked is never evidence of absence.

A HOOK OUTRANKS THE SCROLLBACK, ALWAYS, AND THAT ORDER IS THE POINT. The
trust dialog does not clear itself off the pane's history when it is
answered: ``capture-pane`` keeps showing it for as long as it is in
scrollback. So a session that answered the prompt an hour ago still has
the marker text sitting in its tail. Reading the tail first would pin
that session at ``awaiting_startup_prompt`` forever. Old scrollback is
STALE EVIDENCE, and the hook is the fresh fact that supersedes it.
``resolve_startup_gate`` therefore tests the hook before it ever looks at
the text, and ``tests/test_session_startup_gate.py`` pins that ordering.

THIS MODULE IS PURE. The per-instance bookkeeping (``StartupGateLedger``)
and the one tmux read (``capture_pane_tail``) live next door in
``src/core/session_startup_gate_ledger.py``, so the ladder above can be
tested without a tmux server and without mutable state.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Pattern, Union


# ---------------------------------------------------------------------------
# The vocabulary. Three values, defined once, imported everywhere - the
# same shape session_status.py uses for the activity vocabulary.
# ---------------------------------------------------------------------------

#: Measured NOT blocked on a startup prompt. See the module docstring for
#: the exact (narrow) claim this makes.
GATE_READY: str = "ready"

#: Measured blocked: a live pane, no hook, and a prompt matched in the tail.
GATE_AWAITING: str = "awaiting_startup_prompt"

#: Could not determine. Never a guess in either direction.
GATE_UNKNOWN: str = "unknown"

#: Every legal value, for validation and for the client's normalizer.
ALL_STARTUP_GATES: tuple[str, ...] = (GATE_READY, GATE_AWAITING, GATE_UNKNOWN)


#: How long a live pane may go with no hook at all before its scrollback
#: is worth reading.
#:
#: MEASURED, NOT PICKED. A trusted-folder launch fires ``SessionStart``
#: 0.41-0.50s after pane birth (three runs, 2026-09-08, claude 2.1.263,
#: binary invoked directly). This app does not invoke the binary directly:
#: it births the pane as ``zsh -c 'source ~/.zshrc ...; <wrapper>'``, and
#: sourcing a real ~/.zshrc plus a wrapper script costs seconds more on a
#: loaded box. 20s is forty times the measured bare-launch latency, which
#: is headroom no realistic shell init will eat, and it is still short
#: enough that a user on a phone learns the session wants a keypress
#: while they are still looking at it.
#:
#: The cost of setting it too LOW is a false ``awaiting`` during a slow
#: shell init; the cost of setting it too HIGH is a delayed toast. Neither
#: is a false ``ready``, because the prompt text must match either way.
STARTUP_HOOK_GRACE_SECONDS: int = 20

#: How much scrollback to read when the cheap gates say the tail is worth
#: looking at. The trust dialog is about fifteen rendered lines and sits
#: at the top of a pane that has produced nothing else, so 200 is already
#: generous. Deliberately far smaller than the 2000 the family
#: fingerprinter asks for: that one hunts for a banner that may have
#: scrolled away, this one looks for something that is on screen NOW.
STARTUP_TAIL_LINES: int = 200


# ---------------------------------------------------------------------------
# What a blocking startup prompt looks like. EVERY marker below was read
# off a real pane; provenance is recorded per marker because a pattern
# nobody has seen fire is unmeasured, not proven.
# ---------------------------------------------------------------------------

#: Markers whose presence means "claude is parked on a prompt it will not
#: move past until a human presses a key".
#:
#: The first four are the folder-trust dialog exactly as claude 2.1.263
#: renders it, captured 2026-09-08 from the probe described in the module
#: docstring. NOTE WHAT CHANGED SINCE THE FINGERPRINT TABLE WAS WRITTEN:
#: ``src/core/agent_fingerprint.py`` still carries
#: ``^\s*❯\s*1\.\s*Yes, I trust this folder``, and 2.1.263 does NOT number
#: the options and does NOT put the cursor on the "yes" row - the live
#: capture reads "❯ No, exit" first and "  Yes, I trust this folder"
#: second. That pattern cannot fire on a current claude. It is left alone
#: here (it still matches older versions, and this module does not own
#: that table) but it is why these markers are matched loosely on the
#: sentence text rather than on the option chrome.
#:
#: The last one is the OAuth device-login screen, the other startup state
#: that blocks on a keypress. Its URL shape comes from the 2026-08-18
#: live-capture audit recorded in ``agent_fingerprint.py``; it has NOT
#: been re-captured on 2.1.263, so it is corroborating evidence of a
#: blocking startup screen rather than a freshly measured one.
STARTUP_PROMPT_MARKERS: tuple[Union[str, Pattern[str]], ...] = (
    # Trust dialog, the question itself.
    "Quick safety check: Is this a project you created or one you trust?",
    # Trust dialog, the consequence sentence. Kept because the question
    # above wraps across lines at narrow pane widths and this one is
    # short enough to survive on one line at 80 columns.
    "Claude Code'll be able to read, edit, and execute files here.",
    # Trust dialog, the affirmative option. Matched WITHOUT the leading
    # cursor glyph or an option number, for the reason above.
    re.compile(r"Yes, I trust this folder", re.M),
    # Trust dialog, the key hint line - the literal proof that this
    # screen is waiting on a keystroke.
    "Enter to confirm · Esc to cancel",
    # OAuth device-login screen.
    re.compile(r"claude\.com/cai/oauth/authorize"),
)


def detect_startup_prompt(tail: Optional[str]) -> Optional[bool]:
    """Does this scrollback tail show a blocking startup prompt?

    Description: the matcher, split out from the ladder so it can be
        tested on text alone - including against a NEGATIVE CONTROL. A
        matcher that always finds something is worse than useless, so
        ``tests/test_session_startup_gate.py`` feeds it a steady-state
        claude footer and requires ``False``.
    Inputs:
        tail: captured scrollback text, or None when no capture was made.
    Output:
        bool | None - True when a marker matched, False when the text was
        read and no marker matched, None when there was no text to read.
        The None is not a convenience: "we did not look" and "we looked
        and it is not there" are different answers and the caller routes
        them differently.
    Example:
        >>> detect_startup_prompt("  Yes, I trust this folder")
        True
        >>> detect_startup_prompt(None) is None
        True
    """
    if tail is None:
        return None
    for marker in STARTUP_PROMPT_MARKERS:
        if isinstance(marker, str):
            if marker in tail:
                return True
        elif marker.search(tail):
            return True
    return False


def should_capture_tail(
    *,
    pane_alive: Optional[bool],
    first_hook_at: Optional[datetime],
    instance_age_seconds: Optional[float],
    grace_seconds: int = STARTUP_HOOK_GRACE_SECONDS,
) -> bool:
    """Is reading this pane's scrollback worth a subprocess call?

    Description: the COST GATE, and the reason this feature is free in
        steady state. Capturing a tail is one ``tmux capture-pane`` per
        session per listing poll, which would be an unacceptable tax on
        the home screen if it ran for every row. It does not need to: a
        session that has fired a hook can never be awaiting a startup
        prompt (see the module docstring on evidence ordering), and a
        healthy session fires one within half a second of birth. So the
        capture runs only for a pane that is alive, past the grace
        window, and has produced NO hook - which on a working box is the
        empty set, and on a broken one is exactly the rows in question.

        Deliberately a separate predicate from ``resolve_startup_gate``
        rather than an early return inside it: the resolver must stay
        pure so it can be tested without tmux, and the caller must be
        able to decide whether to pay for the tail BEFORE it has one.
    Inputs:
        pane_alive: True/False/None from the caller's liveness read.
        first_hook_at: when the first hook for THIS instance landed, or
            None if none has.
        instance_age_seconds: age of the tmux instance, or None when it
            could not be dated.
        grace_seconds: override for tests.
    Output:
        bool - True when the caller should capture a tail and pass it to
        ``resolve_startup_gate``.
    Example:
        >>> should_capture_tail(pane_alive=True, first_hook_at=None,
        ...                     instance_age_seconds=99.0)
        True
    """
    if pane_alive is not True:
        return False
    if first_hook_at is not None:
        return False
    if instance_age_seconds is None:
        return False
    return instance_age_seconds >= grace_seconds


def resolve_startup_gate(
    *,
    pane_alive: Optional[bool],
    first_hook_at: Optional[datetime],
    instance_age_seconds: Optional[float],
    tail: Optional[str],
    grace_seconds: int = STARTUP_HOOK_GRACE_SECONDS,
) -> str:
    """Answer "is this session blocked on an unanswered startup prompt?".

    Description: the whole ladder, pure and side-effect free. Rung order
        is load-bearing and is pinned by tests:

          1. A hook has landed for THIS instance -> ``ready``. Tested
             FIRST so lingering prompt text in old scrollback can never
             override the fresh fact.
          2. Pane liveness did not answer -> ``unknown``.
          3. Pane measured not alive -> ``ready`` (a gone process is not
             waiting for a keypress; see the module docstring on what
             ``ready`` does and does not claim).
          4. The instance could not be dated, or is younger than the
             grace window -> ``unknown``. A session that is merely young
             has not been observed to be stuck.
          5. No tail was captured -> ``unknown``.
          6. A marker matched -> ``awaiting_startup_prompt``.
          7. The tail was read and nothing matched -> ``ready``.

        Rung 7 is the one asymmetry worth naming, and it matches the rule
        this codebase already applies to transcripts: not having looked
        is not evidence of absence, but having looked and found nothing
        is. Rung 5 refuses; rung 7 answers.
    Inputs:
        pane_alive: True (measured alive) / False (measured not alive) /
            None (could not determine).
        first_hook_at: timestamp of the first hook event recorded for
            this instance, or None. Presence alone is what rung 1 reads;
            the value is carried for logging and for future rungs.
        instance_age_seconds: seconds since the tmux instance was
            created, or None when it could not be dated.
        tail: captured scrollback text, or None when no capture was made.
        grace_seconds: override for tests.
    Output:
        str - one of ``ALL_STARTUP_GATES``.
    Example:
        >>> resolve_startup_gate(pane_alive=True, first_hook_at=None,
        ...                      instance_age_seconds=60.0,
        ...                      tail="Yes, I trust this folder")
        'awaiting_startup_prompt'
    """
    if first_hook_at is not None:
        return GATE_READY
    if pane_alive is None:
        return GATE_UNKNOWN
    if pane_alive is False:
        return GATE_READY
    if instance_age_seconds is None:
        return GATE_UNKNOWN
    if instance_age_seconds < grace_seconds:
        return GATE_UNKNOWN
    matched = detect_startup_prompt(tail)
    if matched is None:
        return GATE_UNKNOWN
    return GATE_AWAITING if matched else GATE_READY


#: Toast kind for a session parked on a startup prompt. A kind of its own,
#: not a reused ``Notification``: a Notification says claude asked
#: something mid-conversation, and this says claude never got as far as a
#: conversation. The client keys severity and coalescing off the kind
#: string (``client/js/toast.js``), so the two must not be conflated.
STARTUP_TOAST_KIND: str = "StartupPrompt"


def startup_toast_copy() -> tuple[str, str]:
    """Title and body for the "needs a keypress" toast.

    Description: lowercase and plain, matching the rest of the UI copy,
        and it says what to DO rather than what was detected - the user
        on a phone cannot act on "no SessionStart hook observed".
    Inputs: none.
    Output: tuple[str, str] - (title, body).
    Example:
        >>> startup_toast_copy()[0]
        'needs a keypress'
    """
    return (
        "needs a keypress",
        "this session is waiting at a startup prompt and has not "
        "started yet. open it and answer the prompt.",
    )
