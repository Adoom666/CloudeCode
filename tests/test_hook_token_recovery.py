"""A mint under a running agent must be recoverable, and only that.

THE INCIDENT, measured on the owner's box 2026-09-08 and traced to the
millisecond. At 16:16:40.633984Z an adopt registered the live pane
``cloude_Agent_-_Cloude_Code`` under a DERIVED id and minted a token for
it. At 16:16:40.763005Z - 130 ms later - the first
``hook_post_rejected_invalid_token`` for that id was logged. 4,325 more
followed over the next 4h24m, ending only when the owner restarted the
pane by hand at 20:40:23Z and a new process inherited the current
environment. Before the mint the same id was ACCEPTED (``toast_recorded``
at 16:11:28Z and 16:12:56Z), which is what makes this a rotation and not
a misconfiguration.

``_mint_hook_token`` REPLACES the token held for an id. The same value is
baked into the pane's environment at ``new-session`` time and read from
there at hook-fire time, so it is fixed for the life of that process and
cannot be re-issued to it. The agent has no retry, no error surface, and
no way to learn what happened.

WHAT IS BEING TESTED, and the negative control is the load-bearing half.
A recovery that accepted broadly would be a credential bypass wearing a
bugfix's clothes, and it would pass the positive test perfectly. So:

  * positive - a token this server superseded for this id, on this pane,
    is accepted once and the store is re-bound to it
  * negative control - a token that matches nothing still rejects, and
    so does a superseded token presented for a DIFFERENT pane
  * idempotence - duplicate delivery re-binds exactly once
  * ordering - a respawn writes the pane environment BEFORE the new
    process starts, because tmux copies it at spawn and a write
    afterwards reaches the next restart instead of this one

The route is exercised for the accept/reject cases rather than
``validate_hook_token`` alone, because the 403 this file exists to
prevent is produced by the route.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_htr_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_htr_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
import src.api.routes as routes_mod
from src.api.auth import require_auth
from src.config import settings
from src.core.hook_token_recovery import (
    RECOVERY_ACCEPTED,
    RECOVERY_NO_MATCH,
    RECOVERY_UNAVAILABLE,
    SupersededHookTokens,
)
from src.core.session_manager import SessionManager

SESSION_ID = "ses_fb8dd410"
TMUX_NAME = "cloude_Agent_-_Cloude_Code"
#: What the running agent is holding: the token minted BEFORE the mint
#: that revoked it. Not a real credential - no value here ever was.
CARRIED = "tok_the_running_agent_still_carries"


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """A writable state dir so the token store can persist.

    Inputs: tmp_path, monkeypatch.
    Output: Path - the directory ``settings.get_state_dir()`` resolves to.
    """
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    state.mkdir()
    logs.mkdir()
    monkeypatch.setattr(settings, "log_directory", str(logs))
    monkeypatch.setattr(settings, "state_dir_override", str(state))
    return state


def _manager_with_superseded_token(state_dir: Path) -> SessionManager:
    """A manager whose live token was minted over a still-running agent.

    Description: reproduces the incident exactly - the id holds a token,
      the pane is bound, and then a mint replaces it. What the agent
      carries is ``CARRIED``; what the store now holds is whatever the
      mint produced.
    Inputs: state_dir (Path).
    Output: SessionManager.
    """
    mgr = SessionManager()
    mgr._hook_tokens[SESSION_ID] = CARRIED
    mgr._hook_tmux_names[SESSION_ID] = TMUX_NAME
    # The defect itself, called exactly as the adopt path called it.
    mgr._mint_hook_token(SESSION_ID, tmux_name=TMUX_NAME)
    assert mgr.get_hook_token(SESSION_ID) != CARRIED, (
        "setup: the mint did not actually replace the token"
    )
    return mgr


def _hook_app(manager: SessionManager) -> FastAPI:
    """Mount the real hook route over a given manager.

    Inputs: manager (SessionManager).
    Output: FastAPI app with the v1 router mounted.
    """
    app = FastAPI()
    app.state.session_manager = manager
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return app


def _post_hook(app: FastAPI, session_id: str, token: str):
    """POST one activity-only hook event as the loopback client.

    Inputs: app (FastAPI). session_id (str). token (str).
    Output: httpx.Response.
    """
    client = TestClient(app, client=("127.0.0.1", 12345))
    return client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": session_id,
            "X-Cloudecode-Token": token,
            "X-Cloudecode-Event": "PreToolUse",
        },
        json={},
    )


# --------------------------------------------------------------------- #
# 1. positive - the superseded token is accepted once and re-bound
# --------------------------------------------------------------------- #


def test_a_superseded_token_is_accepted_and_the_store_is_rebound(state_dir):
    """The incident, end to end, through the real route.

    A mint revoked the credential the agent is holding. The very next
    hook it sends must be answered 200, and the store must afterwards
    hold the token the PROCESS has - not a third value, and not the one
    the mint produced.
    """
    mgr = _manager_with_superseded_token(state_dir)
    minted = mgr.get_hook_token(SESSION_ID)
    app = _hook_app(mgr)

    resp = _post_hook(app, SESSION_ID, CARRIED)

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert mgr.get_hook_token(SESSION_ID) == CARRIED, (
        "the store was not re-bound to the token the process holds"
    )
    assert mgr.get_hook_token(SESSION_ID) != minted


def test_recovery_never_mints_a_replacement(state_dir):
    """NOTHING IS MINTED, because minting is the defect being recovered.

    A recovery that minted would revoke the agent's credential a second
    time while logging that it had repaired something - the failure would
    return with every log line reading correctly. The token after the
    recovery must be the one that went in, byte for byte.
    """
    mgr = _manager_with_superseded_token(state_dir)

    assert mgr.recover_hook_token(SESSION_ID, CARRIED) == RECOVERY_ACCEPTED
    after = mgr.get_hook_token(SESSION_ID)
    assert after == CARRIED

    # And a second, unrelated event does not rotate it either.
    app = _hook_app(mgr)
    assert _post_hook(app, SESSION_ID, CARRIED).status_code == 200
    assert mgr.get_hook_token(SESSION_ID) == after


def test_the_rebind_survives_a_restart(state_dir):
    """The correction is DURABLE, or the next restart undoes it.

    ``_load_hook_tokens`` rehydrates from disk, so a recovery that only
    fixed memory would leave the running agent 403ing again after the
    very next restart - with the store still holding the token nothing
    can present.
    """
    mgr = _manager_with_superseded_token(state_dir)
    assert mgr.recover_hook_token(SESSION_ID, CARRIED) == RECOVERY_ACCEPTED

    reloaded = SessionManager()
    assert reloaded.get_hook_token(SESSION_ID) == CARRIED


# --------------------------------------------------------------------- #
# 2. negative controls - THE HALF THAT MAKES THE POSITIVE MEAN ANYTHING
# --------------------------------------------------------------------- #


def test_a_token_that_matches_nothing_still_rejects(state_dir):
    """A recovery that always finds something is worse than useless."""
    mgr = _manager_with_superseded_token(state_dir)
    app = _hook_app(mgr)

    resp = _post_hook(app, SESSION_ID, "tok_nobody_ever_minted_this")

    assert resp.status_code == 403, resp.text
    assert mgr.get_hook_token(SESSION_ID) != "tok_nobody_ever_minted_this"


def test_an_id_with_no_superseded_token_still_rejects(state_dir):
    """Nothing to search in is not the same as searched and cleared."""
    mgr = SessionManager()
    mgr._hook_tokens[SESSION_ID] = "tok_live"
    mgr._hook_tmux_names[SESSION_ID] = TMUX_NAME

    assert (
        mgr.recover_hook_token(SESSION_ID, CARRIED) == RECOVERY_UNAVAILABLE
    )
    assert _post_hook(_hook_app(mgr), SESSION_ID, CARRIED).status_code == 403


def test_a_superseded_token_from_another_pane_is_refused(state_dir):
    """ONE PANE, ONE CREDENTIAL. The scope is a refusal, not a hint.

    A token superseded while the id sat on a different tmux session says
    nothing about the pane the id is on now, and accepting it would let
    one pane's retired credential authenticate another's hooks.
    """
    mgr = SessionManager()
    mgr._hook_tokens[SESSION_ID] = CARRIED
    mgr._hook_tmux_names[SESSION_ID] = "cloude_some_other_pane"
    mgr._mint_hook_token(SESSION_ID, tmux_name="cloude_some_other_pane")
    # The id has since moved to the pane under test.
    mgr._hook_tmux_names[SESSION_ID] = TMUX_NAME

    assert (
        mgr.recover_hook_token(SESSION_ID, CARRIED) == RECOVERY_UNAVAILABLE
    )
    assert _post_hook(_hook_app(mgr), SESSION_ID, CARRIED).status_code == 403


def test_another_sessions_superseded_token_is_refused(state_dir):
    """The ring is per id. A neighbour's retired token is not evidence."""
    mgr = SessionManager()
    other = "ses_neighbour"
    mgr._hook_tokens[other] = CARRIED
    mgr._hook_tmux_names[other] = TMUX_NAME
    mgr._mint_hook_token(other, tmux_name=TMUX_NAME)

    mgr._hook_tokens[SESSION_ID] = "tok_live"
    mgr._hook_tmux_names[SESSION_ID] = TMUX_NAME

    assert (
        mgr.recover_hook_token(SESSION_ID, CARRIED) == RECOVERY_UNAVAILABLE
    )


def test_an_unknown_pane_binding_refuses_rather_than_guesses(state_dir):
    """A check that could not be scoped is not a check that passed."""
    ring = SupersededHookTokens()
    ring.record(SESSION_ID, CARRIED, tmux_name=TMUX_NAME)

    decision = ring.decide(SESSION_ID, CARRIED, current_tmux_name=None)

    assert decision.outcome == RECOVERY_UNAVAILABLE
    assert decision.accepted is False
    assert decision.token is None


def test_no_match_and_unavailable_are_not_collapsed():
    """Two refusals, two meanings, and only one is about the token."""
    ring = SupersededHookTokens()
    ring.record(SESSION_ID, CARRIED, tmux_name=TMUX_NAME)

    searched = ring.decide(
        SESSION_ID, "tok_forged", current_tmux_name=TMUX_NAME
    )
    nothing_to_search = ring.decide(
        "ses_unheard_of", CARRIED, current_tmux_name=TMUX_NAME
    )

    assert searched.outcome == RECOVERY_NO_MATCH
    assert nothing_to_search.outcome == RECOVERY_UNAVAILABLE


# --------------------------------------------------------------------- #
# 3. idempotence - hook events are duplicated by design
# --------------------------------------------------------------------- #


def test_duplicate_delivery_rebinds_once_and_both_are_accepted(state_dir):
    """Hooks arrive unordered, duplicated and droppable (see CLAUDE.md).

    Both copies must be answered 200 - the token is genuine for both -
    while the store is re-bound exactly once and the ring does not keep a
    second credential alive behind it.
    """
    mgr = _manager_with_superseded_token(state_dir)
    app = _hook_app(mgr)

    first = _post_hook(app, SESSION_ID, CARRIED)
    second = _post_hook(app, SESSION_ID, CARRIED)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert mgr.get_hook_token(SESSION_ID) == CARRIED
    assert mgr._superseded_hook_tokens.size(SESSION_ID) == 0, (
        "the accepted entry outlived its one use"
    )


def test_the_accepted_entry_is_consumed_so_it_cannot_fire_twice(state_dir):
    """One-shot. After the re-bind the ordinary path is what validates."""
    mgr = _manager_with_superseded_token(state_dir)

    assert mgr.recover_hook_token(SESSION_ID, CARRIED) == RECOVERY_ACCEPTED
    # Rotate again; the value the agent carries is now the SUPERSEDED one
    # only because of this second mint, not the first.
    mgr._mint_hook_token(SESSION_ID, tmux_name=TMUX_NAME)
    assert mgr._superseded_hook_tokens.size(SESSION_ID) == 1


# --------------------------------------------------------------------- #
# 4. the ring itself - bounded, deduped, never a growing credential set
# --------------------------------------------------------------------- #


def test_the_ring_is_bounded():
    """Credential material must not accumulate for the life of an install."""
    ring = SupersededHookTokens(ring_size=2)
    for i in range(5):
        ring.record(SESSION_ID, f"tok_{i}", tmux_name=TMUX_NAME)

    assert ring.size(SESSION_ID) == 2
    assert (
        ring.decide(SESSION_ID, "tok_0", current_tmux_name=TMUX_NAME).outcome
        == RECOVERY_NO_MATCH
    ), "an evicted token stayed recoverable"
    assert ring.decide(
        SESSION_ID, "tok_4", current_tmux_name=TMUX_NAME
    ).accepted


def test_recording_the_same_token_twice_does_not_consume_the_ring():
    """A repeated mint of one value must not evict the others."""
    ring = SupersededHookTokens(ring_size=2)
    ring.record(SESSION_ID, "tok_a", tmux_name=TMUX_NAME)
    for _ in range(4):
        ring.record(SESSION_ID, "tok_b", tmux_name=TMUX_NAME)

    assert ring.size(SESSION_ID) == 2
    assert ring.decide(
        SESSION_ID, "tok_a", current_tmux_name=TMUX_NAME
    ).accepted


def test_minting_a_first_token_records_no_superseded_entry(state_dir):
    """There is no credential to recover when nothing was replaced."""
    mgr = SessionManager()
    mgr._mint_hook_token("ses_brand_new", tmux_name=TMUX_NAME)

    assert mgr._superseded_hook_tokens.size("ses_brand_new") == 0
