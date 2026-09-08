"""The status LED, asserted against hooks a real Claude Code actually fired.

WHY THIS FILE EXISTS. Every other test of this pipeline asserts against a
row somebody typed. ``tests/test_status_led.node.mjs`` proves the mapping,
``tests/test_session_activity.py`` proves the state machine and
``tests/test_hook_driven_status.py`` proves the wiring - all against
fixtures, and all of them would stay green if the real ``claude`` binary
stopped firing the events they name, or fired different ones, or fired
them at a session id the token no longer matches. This file closes that
gap the only way it can be closed: it launches a real agent, lets it POST
its own hooks over loopback at a real server, and asks
``GET /sessions/list`` and the SHIPPED ``ledStateFor`` what the user would
be looking at.

THE FINDING THAT SHAPED THE PROMPTS, and it is not obvious from the code.
``UserPromptSubmit`` does NOT set ``last_tool_event_ts`` - only
``PreToolUse`` / ``PostToolUse`` / ``SubagentStart`` / ``SubagentStop``
do. So a turn that calls no tool never paints ``working`` at all; it goes
straight from ``idle`` to ``Stop``. The working assertion therefore needs
a prompt that makes the agent USE something, and it needs the turn to
outlive a poll interval, which is why it reads several seeded files
instead of answering a question.

RUNNING IT. Opt in explicitly - it spends real Claude turns and about a
minute of wall clock::

    CLOUDE_REAL_HOOK_TESTS=1 venv/bin/python3 -m pytest \\
        tests/test_led_real_hooks.py -v -s

Without that variable, and without tmux / claude / node, every test here
skips with a reason NAMING what went unmeasured. A skip that just says
"skipped" is the silent pass this suite exists to prevent.

ORDER IS LOAD-BEARING. These run in file order against ONE live session,
because a fresh agent per assertion would multiply the cost by eight. An
early failure will cascade, and that is the right shape: the later states
genuinely were not reached.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_rht_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_rht_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from tests.real_hook_app import KEY_DOWN, KEY_ENTER, RealHookApp
from tests.real_hook_assertions import TIMELINE, await_state, record
from tests.real_hook_harness import led_state_for, poll_until, skip_reason

#: The prompt whose turn must outlive a poll interval. Several small
#: reads, each one a PreToolUse/PostToolUse pair, so the heartbeat that
#: paints ``working`` is refreshed rather than raced.
WORKING_PROMPT = (
    "read note-1.txt, note-2.txt, note-3.txt, note-4.txt and note-5.txt "
    "one at a time, and after each one tell me the single word it holds"
)

#: The prompt that must raise a REAL permission prompt. Bash is the one
#: tool this run's settings file puts behind an ``ask`` rule.
PERMISSION_PROMPT = "run the shell command: date +%s"

@pytest.fixture(scope="module")
def live() -> Iterator[RealHookApp]:
    """One real server, one real session, for the whole file.

    Description: skips with a NAMED reason before anything is started,
      seeds the files the working prompt reads, creates the session
      through the app's own create path, and tears the pane and the
      server down in ``finally`` whatever happened.
    Output: yields the live :class:`RealHookApp`.
    """
    reason = skip_reason()
    if reason:
        pytest.skip(reason)

    app = RealHookApp()
    with app:
        for index in range(1, 6):
            (app.work_dir / f"note-{index}.txt").write_text(f"word{index}\n")
        app.create_session()
        try:
            yield app
        finally:
            if TIMELINE:
                print("\n--- measured timeline ---")
                for line in TIMELINE:
                    print(line)
                print(f"hooks: {app.ledger.describe()}")


# =========================================================================== #
# 1. the gate, before any hook has ever fired                                  #
# =========================================================================== #


def test_a_pane_on_the_trust_dialog_is_awaiting_a_keypress(live: RealHookApp) -> None:
    """Punchlist 19, against live data rather than a fixture.

    The pane is alive, has a real pid, and every other field reads
    healthy. The ONLY signal that it is stuck is the ABSENCE of a hook,
    and the gate has to wait out ``STARTUP_HOOK_GRACE_SECONDS`` before it
    is allowed to say so - hence the longer timeout.
    """
    assert live.ledger.events(live.session_id) == [], (
        "a hook arrived before the trust dialog was answered, which would "
        "make the rest of this file measure the wrong thing: "
        f"{live.ledger.describe()}"
    )
    await_state(
        live,
        "trust dialog",
        want_status=("idle", "unknown", "running", "working"),
        want_inner=("waiting-input",),
        want_outer=("active",),
        want_gate=("awaiting_startup_prompt",),
        timeout=50.0,
    )


# =========================================================================== #
# 2. answering it - a real SessionStart                                        #
# =========================================================================== #


def test_answering_the_dialog_fires_session_start_and_opens_the_gate(
    live: RealHookApp,
) -> None:
    """The first REAL hook of the run flips the gate to ``ready``."""
    assert "trust this folder" in live.pane_tail(30), (
        "the pane is not on the trust dialog, so the keypress below would "
        f"be typed into something else. pane tail:\n{live.pane_tail(20)}"
    )
    live.send_keys(KEY_DOWN, KEY_ENTER, settle=0.6)

    matched, _ = poll_until(
        lambda: live.ledger.events(live.session_id),
        lambda seen: "SessionStart" in seen,
        timeout=45.0,
    )
    assert matched, (
        "no SessionStart hook ever reached the server after the dialog was "
        f"answered. hooks: {live.ledger.describe()}\n"
        f"pane tail:\n{live.pane_tail(15)}"
    )
    await_state(
        live,
        "SessionStart",
        want_status=("idle", "unknown"),
        want_inner=("done", "unknown"),
        want_outer=("steady", "dim"),
        want_gate=("ready",),
    )


# =========================================================================== #
# 3. a real turn, with real tool calls                                         #
# =========================================================================== #


def test_a_real_turn_with_tool_calls_paints_working(live: RealHookApp) -> None:
    """``PreToolUse`` is what makes the light say the agent is busy."""
    live.type_prompt(WORKING_PROMPT)
    await_state(
        live,
        "UserPromptSubmit+tools",
        want_status=("working", "working_subagent"),
        want_inner=("working",),
        want_outer=("active", "unread"),
        timeout=60.0,
    )


# =========================================================================== #
# 4. the turn ending with nobody watching                                      #
# =========================================================================== #


def test_a_real_stop_with_nobody_viewing_paints_finished_unread(
    live: RealHookApp,
) -> None:
    """``Stop`` sets the auto-unread flag, and the halo has to show it."""
    await_state(
        live,
        "Stop (unviewed)",
        want_status=("finished_unread",),
        want_inner=("done",),
        want_outer=("unread",),
        timeout=180.0,
    )
    assert "Stop" in live.ledger.events(live.session_id), (
        f"finished_unread was reached without a Stop hook: "
        f"{live.ledger.describe()}"
    )


# =========================================================================== #
# 5. what claude actually sends after a turn ends                              #
# =========================================================================== #


def test_a_trailing_subagent_stop_re_arms_the_working_heartbeat(
    live: RealHookApp,
) -> None:
    """A CHARACTERISATION TEST. It pins behaviour that is wrong.

    MEASURED on claude 2.1.265, twice independently and on a turn with no
    subagent anywhere in it: ``SubagentStop`` arrives 1.50s AFTER ``Stop``
    (Stop+38.96s, SubagentStop+40.46s in one run; Stop+11.61s,
    SubagentStop+13.24s in an isolated probe of a one-word answer). So the
    trailing SubagentStop is not an edge case, it is what every finished
    turn looks like on this binary.

    ``SessionActivityTracker.record_event`` handles ``SubagentStop`` by
    flooring ``subagent_depth`` AND stamping ``last_tool_event_ts = now``.
    ``Stop`` had just set that field to ``None`` precisely to say the turn
    was over. So the stamp re-arms the heartbeat and ``resolve`` reports
    ``working`` again - for the full ``WORKING_HEARTBEAT_TIMEOUT_SECONDS``
    of 120s, on a session that is sitting at an empty prompt.

    THE CONSEQUENCE IS A LIGHT THAT LIES: ``finished_unread`` is visible
    for about 1.5s and then a finished session paints ``working``/``active``
    for two minutes, and ``idle`` is unreachable in between. That is the
    "claiming work tmux cannot see" defect this LED work exists to remove,
    arriving through the hook stream instead of through the tmux fallback.

    WHEN THAT IS FIXED - a ``SubagentStop`` at ``subagent_depth == 0``
    should not stamp the heartbeat - this test MUST be inverted rather
    than loosened, and the ``idle``/``done``/``steady`` assertion that
    belongs here restored.
    """
    seen = live.ledger.events(live.session_id)
    assert "Stop" in seen, f"no Stop was ever recorded: {live.ledger.describe()}"
    matched, _ = poll_until(
        lambda: live.ledger.events(live.session_id),
        lambda events: "SubagentStop" in events,
        timeout=30.0,
    )
    assert matched, (
        "claude did not send a trailing SubagentStop this run. That is a "
        "CHANGE IN BEHAVIOUR, not a flake - re-measure and, if it has "
        "genuinely stopped, delete this test and assert idle/done/steady "
        f"in it instead. hooks: {live.ledger.describe()}"
    )
    seen = live.ledger.events(live.session_id)
    assert seen.index("SubagentStop") > seen.index("Stop"), (
        "the SubagentStop preceded the Stop this run, which would make the "
        f"heartbeat re-arm harmless: {live.ledger.describe()}"
    )
    await_state(
        live,
        "trailing SubagentStop",
        want_status=("working",),
        want_inner=("working",),
        want_outer=("unread", "active"),
    )


# =========================================================================== #
# 6. binding a terminal - the app's own mark-viewed seam                       #
# =========================================================================== #


def test_binding_a_terminal_clears_the_unread_halo(live: RealHookApp) -> None:
    """A WS terminal binding is the ONLY thing that clears auto-unread.

    The assertion is about the HALO alone, deliberately. The inner dot is
    still ``working`` here because of the trailing ``SubagentStop`` the
    previous test characterises, and folding that defect into this one
    would make the mark-viewed seam untestable until it is fixed - and
    would then need re-loosening rather than deleting. This asserts what
    binding a terminal is responsible for, and nothing else, so it stays
    true either side of that fix.
    """
    before = live.signals()
    assert before.get("unread") is True, (
        "the session was not unread before a terminal was bound, so this "
        f"test cannot have measured the clearing: {before}"
    )
    live.view()

    def cleared(observed: dict[str, Any]) -> bool:
        return (
            observed.get("unread") is False
            and led_state_for(observed)["outer"] != "unread"
        )

    matched, observed = poll_until(live.signals, cleared, timeout=30.0)
    record("terminal bound", observed or {}, led_state_for(observed or {}))
    assert matched, (
        "binding a WS terminal did not clear the unread halo.\n"
        f"  last signals: {observed}\n"
        f"  hooks seen:   {live.ledger.describe()}"
    )


# =========================================================================== #
# 6. a real permission prompt                                                  #
# =========================================================================== #


def test_a_real_permission_request_paints_the_waiting_state(
    live: RealHookApp,
) -> None:
    """A genuine ``PermissionRequest``, not a hand-POSTed one.

    Both spellings are accepted on purpose. The server's single
    ``question`` state is being split into ``question`` (a
    PermissionRequest - the agent is stopped) and ``notice`` (a plain
    Notification) in a change landing beside this one, and this test must
    measure the light either side of that seam rather than pin the
    version it happened to be written against.
    """
    live.type_prompt(PERMISSION_PROMPT)
    matched, _ = poll_until(
        lambda: live.ledger.events(live.session_id),
        lambda seen: "PermissionRequest" in seen or "Notification" in seen,
        timeout=90.0,
    )
    assert matched, (
        "no PermissionRequest and no Notification ever arrived, so the "
        "waiting state was never exercised. hooks: "
        f"{live.ledger.describe()}\npane tail:\n{live.pane_tail(15)}"
    )
    await_state(
        live,
        "PermissionRequest",
        want_status=("question", "notice"),
        want_inner=("waiting-permission", "waiting-input"),
        want_outer=("active",),
    )


# =========================================================================== #
# 7. the negative control                                                      #
# =========================================================================== #


def test_a_bogus_token_is_refused_and_moves_nothing(live: RealHookApp) -> None:
    """The control, and it asserts TWO things for a reason.

    A route that rejected the RESPONSE while having already mutated the
    state would satisfy an assertion about the 403 alone, and that is
    exactly the shape of defect a negative control exists to catch. So
    the signals are compared byte-for-byte either side of the refusal.
    """
    import httpx

    before = live.signals()
    real_token = live.app.state.session_manager._hook_tokens.get(live.session_id)
    assert real_token, "no hook token was minted, so nothing was controlled for"
    bogus = ("0" * len(real_token))[: len(real_token)]
    assert bogus != real_token

    resp = httpx.post(
        f"http://127.0.0.1:{live.port}/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": live.session_id,
            "X-Cloudecode-Token": bogus,
            "X-Cloudecode-Event": "Stop",
            "Content-Type": "application/json",
        },
        json={"hook_event_name": "Stop"},
        timeout=15.0,
    )
    assert resp.status_code == 403, (
        f"a bogus token was not refused: {resp.status_code} {resp.text[:200]}"
    )
    after = live.signals()
    assert after == before, (
        "a refused hook moved the session's state, so the 403 is cosmetic: "
        f"before={before} after={after}"
    )
    record("bogus token (403)", after, led_state_for(after))


# =========================================================================== #
# 8. the pane dying                                                            #
# =========================================================================== #


def test_a_killed_pane_leaves_the_live_list_rather_than_painting_dead(
    live: RealHookApp,
) -> None:
    """MEASURED, and it is not what the LED vocabulary implies.

    ``ledStateFor`` has a ``dead``/``off`` state and
    ``SessionActivityTracker.resolve`` returns ``dead`` for a pane tmux
    reports as ``#{pane_dead}``. Both are real. What this run measured is
    that NO LIVE ENDPOINT EVER CARRIES THAT ROW to the client, so the
    state is not reachable from live data by this path:

    ``_session_info_for`` runs ``resolve_listing_liveness`` and DROPS the
    row on ``LIVENESS_GONE`` - deliberately, with a comment saying so
    ("EXISTENCE IS NOT LIVENESS"), because a dead husk held open by
    ``remain-on-exit`` used to sit in the running list forever.
    ``/sessions/attachable`` does not pick it up either. So a killed
    session VANISHES from the sidebar; it does not turn into a dead light.

    This test asserts the behaviour that exists rather than the one the
    vocabulary suggests, and it is written to FAIL if that ever changes -
    at which point the honest fix is to assert ``dead``/``off`` here and
    delete this docstring, not to loosen the assertion.
    """
    assert live.row() is not None, (
        "the session had already left /sessions/list before it was killed, "
        "so this test measured nothing about the kill"
    )
    live.kill_agent()

    gone, last = poll_until(live.row, lambda row: row is None, timeout=30.0)
    assert gone, (
        "a pane whose process was SIGKILLed is still being listed as live "
        f"after 30s: {last}\npane tail:\n{live.pane_tail(6)}"
    )
    record("pane killed", live.signals(), led_state_for(live.signals()))

    attachable = live._httpx.get("/api/v1/sessions/attachable")
    assert attachable.status_code == 200, attachable.text
    names = {
        row.get("tmux_session") or row.get("name") for row in attachable.json()
    }
    assert live.tmux_name not in names, (
        "the dead pane reappeared on /sessions/attachable, which would make "
        "`dead` reachable after all - assert it here instead of asserting "
        f"its absence. rows: {names}"
    )
