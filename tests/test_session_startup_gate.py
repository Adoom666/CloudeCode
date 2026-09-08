"""Tests for src.core.session_startup_gate - punchlist item 19.

Pure unit tests, no tmux and no SessionManager, in the style of
tests/test_session_activity.py. They cover the four cases the gate exists
to get right:

  POSITIVE     a live pane, past the grace window, no hook, and the trust
               dialog in the tail -> awaiting_startup_prompt.
  NEGATIVE     a hook was seen -> ready, INCLUDING when the trust dialog
               is still sitting in the scrollback. Old scrollback is
               stale evidence and must never outrank a hook.
  FAILURE      pane liveness did not answer -> unknown, and nothing is
               toasted.
  IDEMPOTENCE  the same detection twice claims exactly one toast.

Plus a NEGATIVE CONTROL on the matcher itself. A matcher that always
finds something is worse than useless, so a steady-state claude footer
must read False, not True.

Run with:
    venv/bin/python3 -m pytest tests/test_session_startup_gate.py -v
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_sg_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_sg_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.session_startup_gate import (
    ALL_STARTUP_GATES,
    GATE_AWAITING,
    GATE_READY,
    GATE_UNKNOWN,
    STARTUP_HOOK_GRACE_SECONDS,
    STARTUP_TAIL_LINES,
    detect_startup_prompt,
    resolve_startup_gate,
    should_capture_tail,
    startup_toast_copy,
)
from src.core.session_startup_gate_ledger import StartupGateLedger

# ---------------------------------------------------------------------------
# Real captures. Both were read off a live pane on 2026-09-08 with claude
# 2.1.263, on a throwaway `tmux -L cloude-test` socket - never the app's
# own `cloude` socket. Trimmed to the lines that matter; the byte-for-byte
# framing of the box is not what the matcher keys on.
# ---------------------------------------------------------------------------

TRUST_PROMPT_TAIL = """
 Accessing workspace:

 /private/tmp/cc-probe/workdir

 Quick safety check: Is this a project you created or one you trust? (Like your own code, a
 well-known open source project, or work from your team). If not, take a moment to review what's in
 this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""

#: THE NEGATIVE CONTROL. A claude that is up and idle, captured from the
#: same probe after the prompt was answered. Nothing here may match.
STEADY_STATE_TAIL = """
 ▐▛███▛█   Claude Code v2.1.263
▝▜██████▀  Opus 5 (1M context) · Claude Max
  ▝▝ ▝▝    /private/tmp/cc-probe/workdir

❯
  jsugamele@mac-mini-m4:/private/tmp/cc-probe/workdir
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""

PAST_GRACE = float(STARTUP_HOOK_GRACE_SECONDS + 5)


# ---------------------------------------------------------------------------
# The matcher, on text alone.
# ---------------------------------------------------------------------------


def test_matcher_finds_the_real_trust_dialog():
    """The 2.1.263 trust dialog capture must match."""
    assert detect_startup_prompt(TRUST_PROMPT_TAIL) is True


def test_matcher_negative_control_steady_state_claude():
    """A running, idle claude must NOT match.

    This is the control that makes a True meaningful. Without it the
    matcher could be matching on nothing in particular and every session
    would read as blocked.
    """
    assert detect_startup_prompt(STEADY_STATE_TAIL) is False


def test_matcher_finds_the_oauth_login_screen():
    """The other blocking startup screen also counts."""
    tail = "Browser didn't open? Use the url below:\nhttps://claude.com/cai/oauth/authorize?code=1"
    assert detect_startup_prompt(tail) is True


def test_matcher_separates_did_not_look_from_looked_and_found_nothing():
    """None and False are different answers and must stay different."""
    assert detect_startup_prompt(None) is None
    assert detect_startup_prompt("") is False


# ---------------------------------------------------------------------------
# POSITIVE.
# ---------------------------------------------------------------------------


def test_alive_pane_no_hook_past_threshold_with_prompt_is_awaiting():
    """The whole defect, in one assertion."""
    assert (
        resolve_startup_gate(
            pane_alive=True,
            first_hook_at=None,
            instance_age_seconds=PAST_GRACE,
            tail=TRUST_PROMPT_TAIL,
        )
        == GATE_AWAITING
    )


def test_alive_pane_no_hook_past_threshold_without_prompt_is_ready():
    """Looked at the scrollback, found no prompt: that is an answer.

    Mirrors the transcript-presence rule this codebase already applies -
    not having looked is not evidence of absence, but having looked and
    found nothing is.
    """
    assert (
        resolve_startup_gate(
            pane_alive=True,
            first_hook_at=None,
            instance_age_seconds=PAST_GRACE,
            tail=STEADY_STATE_TAIL,
        )
        == GATE_READY
    )


# ---------------------------------------------------------------------------
# NEGATIVE - a hook outranks the scrollback.
# ---------------------------------------------------------------------------


def test_hook_seen_is_ready():
    """A hook for this instance is positive proof startup finished."""
    assert (
        resolve_startup_gate(
            pane_alive=True,
            first_hook_at=datetime(2026, 9, 8, 15, 0, 0),
            instance_age_seconds=PAST_GRACE,
            tail=None,
        )
        == GATE_READY
    )


def test_hook_seen_beats_prompt_text_lingering_in_scrollback():
    """THE ORDERING TEST, and the reason the ladder is written this way.

    tmux does not erase the trust dialog when it is answered - it stays
    in scrollback for as long as it fits. A session that answered the
    prompt an hour ago still has the marker text in its tail, so reading
    the tail before the hook would pin it at awaiting forever.
    """
    assert (
        resolve_startup_gate(
            pane_alive=True,
            first_hook_at=datetime(2026, 9, 8, 15, 0, 0),
            instance_age_seconds=PAST_GRACE,
            tail=TRUST_PROMPT_TAIL,
        )
        == GATE_READY
    )


def test_a_young_session_showing_the_prompt_is_unknown_not_awaiting():
    """Merely young is not stuck.

    A trusted-folder launch fires SessionStart in about half a second
    (measured 2026-09-08), so anything inside the grace window has not
    been observed to be stuck - only observed to be starting.
    """
    assert (
        resolve_startup_gate(
            pane_alive=True,
            first_hook_at=None,
            instance_age_seconds=1.0,
            tail=TRUST_PROMPT_TAIL,
        )
        == GATE_UNKNOWN
    )


def test_a_gone_process_is_not_waiting_for_a_keypress():
    """A pane measured not alive answers the question with a measured no."""
    assert (
        resolve_startup_gate(
            pane_alive=False,
            first_hook_at=None,
            instance_age_seconds=PAST_GRACE,
            tail=TRUST_PROMPT_TAIL,
        )
        == GATE_READY
    )


# ---------------------------------------------------------------------------
# FAILURE - unmeasured inputs never manufacture a verdict.
# ---------------------------------------------------------------------------


def test_unknown_pane_liveness_is_unknown():
    """The required failure case: liveness did not answer."""
    assert (
        resolve_startup_gate(
            pane_alive=None,
            first_hook_at=None,
            instance_age_seconds=PAST_GRACE,
            tail=TRUST_PROMPT_TAIL,
        )
        == GATE_UNKNOWN
    )


def test_unknown_pane_liveness_never_claims_a_toast():
    """No toast may ride on a verdict nobody measured.

    The toast is claimed only on GATE_AWAITING, so an unknown gate must
    leave the claim untouched and available for a later, real detection.
    """
    ledger = StartupGateLedger()
    gate = resolve_startup_gate(
        pane_alive=None,
        first_hook_at=None,
        instance_age_seconds=PAST_GRACE,
        tail=TRUST_PROMPT_TAIL,
    )
    assert gate == GATE_UNKNOWN
    # Nothing claimed it, so the first real detection still can.
    assert ledger.claim_toast("cloude_a") is True


def test_undatable_instance_is_unknown():
    """No age means the grace window cannot be evaluated."""
    assert (
        resolve_startup_gate(
            pane_alive=True,
            first_hook_at=None,
            instance_age_seconds=None,
            tail=TRUST_PROMPT_TAIL,
        )
        == GATE_UNKNOWN
    )


def test_uncaptured_tail_is_unknown():
    """Not having looked is not evidence of absence."""
    assert (
        resolve_startup_gate(
            pane_alive=True,
            first_hook_at=None,
            instance_age_seconds=PAST_GRACE,
            tail=None,
        )
        == GATE_UNKNOWN
    )


def test_every_verdict_is_in_the_vocabulary():
    """No path may invent a fourth string."""
    for alive in (True, False, None):
        for hook in (None, datetime(2026, 9, 8)):
            for age in (None, 1.0, PAST_GRACE):
                for tail in (None, "", TRUST_PROMPT_TAIL, STEADY_STATE_TAIL):
                    verdict = resolve_startup_gate(
                        pane_alive=alive,
                        first_hook_at=hook,
                        instance_age_seconds=age,
                        tail=tail,
                    )
                    assert verdict in ALL_STARTUP_GATES


# ---------------------------------------------------------------------------
# The cost gate.
# ---------------------------------------------------------------------------


def test_capture_is_skipped_for_a_session_that_has_fired_a_hook():
    """Steady state must cost nothing - no capture-pane per row per poll."""
    assert (
        should_capture_tail(
            pane_alive=True,
            first_hook_at=datetime(2026, 9, 8),
            instance_age_seconds=PAST_GRACE,
        )
        is False
    )


def test_capture_runs_only_for_a_live_hookless_pane_past_the_window():
    assert (
        should_capture_tail(
            pane_alive=True,
            first_hook_at=None,
            instance_age_seconds=PAST_GRACE,
        )
        is True
    )
    assert (
        should_capture_tail(
            pane_alive=None,
            first_hook_at=None,
            instance_age_seconds=PAST_GRACE,
        )
        is False
    )
    assert (
        should_capture_tail(
            pane_alive=True, first_hook_at=None, instance_age_seconds=1.0
        )
        is False
    )
    assert (
        should_capture_tail(
            pane_alive=True, first_hook_at=None, instance_age_seconds=None
        )
        is False
    )


# ---------------------------------------------------------------------------
# IDEMPOTENCE - the ledger.
# ---------------------------------------------------------------------------


def test_the_same_detection_twice_toasts_once():
    """The required idempotence case.

    Detection re-runs on every listing poll and keeps answering awaiting
    for as long as the prompt is unanswered, so the claim - not the
    detection - is what makes one toast.
    """
    ledger = StartupGateLedger()
    claims = []
    for _ in range(5):
        gate = resolve_startup_gate(
            pane_alive=True,
            first_hook_at=ledger.first_hook_at("cloude_a"),
            instance_age_seconds=PAST_GRACE,
            tail=TRUST_PROMPT_TAIL,
        )
        assert gate == GATE_AWAITING
        if gate == GATE_AWAITING:
            claims.append(ledger.claim_toast("cloude_a"))
    assert claims == [True, False, False, False, False]


def test_a_duplicate_hook_does_not_move_the_first_hook_time():
    """Hook events are duplicated and re-delivered; first write wins."""
    ledger = StartupGateLedger()
    first = datetime(2026, 9, 8, 15, 0, 0)
    ledger.record_hook("cloude_a", now=first)
    ledger.record_hook("cloude_a", now=first + timedelta(minutes=10))
    assert ledger.first_hook_at("cloude_a") == first


def test_a_hook_makes_the_gate_ready_through_the_ledger():
    ledger = StartupGateLedger()
    ledger.record_hook("cloude_a", now=datetime(2026, 9, 8, 15, 0, 0))
    assert (
        resolve_startup_gate(
            pane_alive=True,
            first_hook_at=ledger.first_hook_at("cloude_a"),
            instance_age_seconds=PAST_GRACE,
            tail=TRUST_PROMPT_TAIL,
        )
        == GATE_READY
    )


def test_a_live_respawn_resets_the_instance():
    """A new pane pid under the same name and epoch is a new process.

    This is the case SessionActivityTracker.hooks_seen cannot see:
    respawn-pane -k keeps the session_id AND the tmux epoch and only
    moves the pane pid. Without the reset, a session restarted onto a
    wrapper that hits the trust dialog would read ready forever.
    """
    ledger = StartupGateLedger()
    ledger.record_hook("cloude_a", epoch=7, now=datetime(2026, 9, 8))
    ledger.observe_instance("cloude_a", epoch=7, pane_pid=100)
    assert ledger.first_hook_at("cloude_a") is not None
    assert ledger.claim_toast("cloude_a") is True

    ledger.observe_instance("cloude_a", epoch=7, pane_pid=200)
    assert ledger.first_hook_at("cloude_a") is None
    # The toast claim resets with it: the new process is a new thing to
    # tell the user about.
    assert ledger.claim_toast("cloude_a") is True


def test_a_reused_tmux_name_with_a_new_epoch_resets_the_instance():
    ledger = StartupGateLedger()
    ledger.record_hook("cloude_a", epoch=7, now=datetime(2026, 9, 8))
    ledger.observe_instance("cloude_a", epoch=7, pane_pid=100)
    ledger.observe_instance("cloude_a", epoch=9, pane_pid=100)
    assert ledger.first_hook_at("cloude_a") is None


def test_an_unmeasured_pid_is_adopted_not_treated_as_a_change():
    """A null must never reset, or the ledger resets on every poll.

    That failure mode is invisible in the happy path and shows up only as
    a toast that fires forever, which is exactly what claim_toast exists
    to prevent.
    """
    ledger = StartupGateLedger()
    ledger.record_hook("cloude_a", now=datetime(2026, 9, 8))
    for _ in range(5):
        ledger.observe_instance("cloude_a", epoch=None, pane_pid=None)
    assert ledger.first_hook_at("cloude_a") is not None


def test_observe_instance_on_an_unknown_name_is_a_no_op():
    ledger = StartupGateLedger()
    ledger.observe_instance("never_seen", epoch=1, pane_pid=2)
    assert ledger.first_hook_at("never_seen") is None


def test_forget_drops_the_instance():
    ledger = StartupGateLedger()
    ledger.record_hook("cloude_a", now=datetime(2026, 9, 8))
    ledger.forget("cloude_a")
    assert ledger.first_hook_at("cloude_a") is None
    ledger.forget("cloude_a")  # idempotent


def test_a_missing_tmux_name_never_records_or_claims():
    """The hook endpoint cannot always resolve a name; that is not a bug."""
    ledger = StartupGateLedger()
    ledger.record_hook(None, now=datetime(2026, 9, 8))
    assert ledger.first_hook_at(None) is None
    assert ledger.claim_toast(None) is False


# ---------------------------------------------------------------------------
# Copy.
# ---------------------------------------------------------------------------


def test_toast_copy_is_lowercase_plain_and_actionable():
    title, body = startup_toast_copy()
    assert title == "needs a keypress"
    assert title == title.lower()
    assert body == body.lower()
    # Escaped rather than literal: this repo forbids em/en dashes in its
    # own source, and a test that guards the rule must not break it.
    for banned in ("\u2014", "\u2013"):
        assert banned not in title and banned not in body


# ---------------------------------------------------------------------------
# SessionManager wiring. The pure ladder above is worthless if nothing
# calls it, and this repo has shipped that exact shape of defect before:
# a builder that returns perfect output nobody splices in.
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from src.core.session_manager import SessionManager  # noqa: E402
from src.core.session_status import LIVENESS_LIVE, LIVENESS_UNKNOWN  # noqa: E402
from src.models import Session, SessionStatus  # noqa: E402


class _StubSettings:
    """The narrow slice of Settings SessionManager reads at construction."""

    def __init__(self, root: Path):
        self._root = root
        (root / "logs").mkdir(parents=True, exist_ok=True)

    def get_pinned_themes_path(self) -> Path:
        return self._root / "pinned_themes.json"

    def get_unread_state_path(self) -> Path:
        return self._root / "unread_state.json"

    @property
    def log_directory(self) -> str:
        return str(self._root / "logs")

    def get_session_metadata_path(self) -> Path:
        return self._root / "logs" / "session_metadata.json"


class _FakeBackend:
    """Enough of a SessionBackend for the gate's two lookups."""

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session
        self.socket_name = "cloude-test-never-real"

    def is_alive(self) -> bool:
        return True


def _manager(monkeypatch, tmp_path: Path) -> SessionManager:
    monkeypatch.setattr(
        "src.core.session_manager.settings", _StubSettings(tmp_path)
    )
    return SessionManager()


def _register(mgr: SessionManager, sid: str, name: str, tmp_path: Path) -> None:
    mgr.sessions[sid] = Session(
        id=sid,
        pty_pid=None,
        working_dir=str(tmp_path),
        status=SessionStatus.RUNNING,
        tmux_session=name,
    )
    mgr.backends[sid] = _FakeBackend(name)
    mgr._subscribers.setdefault(sid, [])


def _stub_tail(monkeypatch, text):
    """Replace the one tmux call the gate makes. NEVER touches a socket."""
    monkeypatch.setattr(
        "src.core.session_manager.capture_pane_tail",
        lambda **kwargs: text,
    )


def test_manager_reports_awaiting_and_toasts_exactly_once(
    monkeypatch, tmp_path
):
    """The end-to-end wiring, and the idempotence claim on the real path."""
    mgr = _manager(monkeypatch, tmp_path)
    _register(mgr, "ses1", "cloude_proj", tmp_path)
    _stub_tail(monkeypatch, TRUST_PROMPT_TAIL)

    # The gate ages an instance against time.time(), so the fixture has
    # to be a real unix epoch. datetime.utcnow().timestamp() is NOT:
    # it reads a naive UTC value as local time, which on this box put
    # the pane's birth in the future and made every verdict 'unknown'.
    old_epoch = int(time.time()) - 600
    row = {"created_at_epoch": old_epoch, "pid": 4321}

    verdicts = []
    for _ in range(3):
        verdicts.append(
            mgr._startup_gate_for(
                session_id="ses1",
                backend=mgr.backends["ses1"],
                tmux_name="cloude_proj",
                row=row,
                liveness=LIVENESS_LIVE,
            )
        )

    assert verdicts == [GATE_AWAITING, GATE_AWAITING, GATE_AWAITING]
    # Three detections, ONE toast. The detection repeats for as long as
    # the user leaves the prompt unanswered; the toast must not.
    assert len(mgr._pending_startup_toasts) == 1
    assert len(mgr.get_toasts("ses1")) == 1
    assert mgr.get_toasts("ses1")[0].title == "needs a keypress"


def test_manager_reports_ready_once_a_hook_lands(monkeypatch, tmp_path):
    """A hook through the real endpoint path flips the gate."""
    mgr = _manager(monkeypatch, tmp_path)
    _register(mgr, "ses1", "cloude_proj", tmp_path)
    _stub_tail(monkeypatch, TRUST_PROMPT_TAIL)

    mgr.record_hook_event("ses1", "SessionStart", {})

    row = {
        "created_at_epoch": int(time.time()) - 600,
        "pid": 4321,
    }
    assert (
        mgr._startup_gate_for(
            session_id="ses1",
            backend=mgr.backends["ses1"],
            tmux_name="cloude_proj",
            row=row,
            liveness=LIVENESS_LIVE,
        )
        == GATE_READY
    )
    assert mgr._pending_startup_toasts == []


def test_manager_refuses_to_guess_when_liveness_is_unknown(
    monkeypatch, tmp_path
):
    """An unmeasured pane produces no verdict and no toast."""
    mgr = _manager(monkeypatch, tmp_path)
    _register(mgr, "ses1", "cloude_proj", tmp_path)
    _stub_tail(monkeypatch, TRUST_PROMPT_TAIL)

    row = {
        "created_at_epoch": int(time.time()) - 600,
        "pid": 4321,
    }
    assert (
        mgr._startup_gate_for(
            session_id="ses1",
            backend=mgr.backends["ses1"],
            tmux_name="cloude_proj",
            row=row,
            liveness=LIVENESS_UNKNOWN,
        )
        == GATE_UNKNOWN
    )
    assert mgr._pending_startup_toasts == []
    assert mgr.get_toasts("ses1") == []


def test_manager_never_captures_a_tail_for_a_session_with_a_hook(
    monkeypatch, tmp_path
):
    """Steady state must not pay one capture-pane per row per poll."""
    mgr = _manager(monkeypatch, tmp_path)
    _register(mgr, "ses1", "cloude_proj", tmp_path)

    calls = []

    def _explode(**kwargs):
        calls.append(kwargs)
        return TRUST_PROMPT_TAIL

    monkeypatch.setattr(
        "src.core.session_manager.capture_pane_tail", _explode
    )
    mgr.record_hook_event("ses1", "PreToolUse", {})

    row = {
        "created_at_epoch": int(time.time()) - 600,
        "pid": 4321,
    }
    mgr._startup_gate_for(
        session_id="ses1",
        backend=mgr.backends["ses1"],
        tmux_name="cloude_proj",
        row=row,
        liveness=LIVENESS_LIVE,
    )
    # FILTERED BY `lines`, NOT COUNTED WHOLE, and the distinction is the
    # point of the DRY refactor: capture_pane_tail now has TWO callers -
    # this gate, which asks for STARTUP_TAIL_LINES, and the family
    # fingerprinter, which asks for 2000 - so one stub intercepts both.
    # Asserting the list was empty made this test pass alone and fail in
    # a full run, where a session left behind by an earlier test got
    # fingerprinted through the same stub while the manager was being
    # built. The claim being made here is about the GATE's capture.
    gate_calls = [c for c in calls if c.get("lines") == STARTUP_TAIL_LINES]
    assert gate_calls == []


def test_manager_forgets_the_gate_when_the_session_is_wiped(
    monkeypatch, tmp_path
):
    """The ledger is keyed by tmux name and must not outlive the process."""
    mgr = _manager(monkeypatch, tmp_path)
    _register(mgr, "ses1", "cloude_proj", tmp_path)
    mgr.record_hook_event("ses1", "SessionStart", {})
    assert mgr._startup_gate_ledger.first_hook_at("cloude_proj") is not None

    mgr._wipe_session_state("ses1")
    assert mgr._startup_gate_ledger.first_hook_at("cloude_proj") is None
