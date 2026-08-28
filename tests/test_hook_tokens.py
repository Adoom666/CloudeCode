"""The durable hook-token store.

Pins the behaviours that decide whether a session's hooks survive a
server restart - the defect measured live on 2026-08-28, where every
pre-restart agent 403'd forever and nothing anywhere reported it.
"""

import json
import os
import pathlib
import tempfile

import pytest

from src.core.hook_tokens import (
    LOAD_ABSENT,
    LOAD_OK,
    LOAD_UNREADABLE,
    load_tokens,
    save_tokens,
    tokens_path,
)


@pytest.fixture
def state_dir():
    return pathlib.Path(tempfile.mkdtemp())


def test_a_token_survives_a_round_trip(state_dir):
    """THE WHOLE POINT: what a restart must not lose."""
    assert save_tokens(state_dir, {"ses_a": "tok-a"})[0] is True
    reloaded = load_tokens(state_dir)
    assert reloaded.status == LOAD_OK
    assert reloaded.tokens == {"ses_a": "tok-a"}


def test_a_fresh_install_is_absent_not_broken(state_dir):
    """No file yet is the normal state, not a failure.

    Reporting it as unreadable would make every first run warn about a
    condition that is fine, which is how a real warning gets ignored.
    """
    r = load_tokens(state_dir)
    assert r.status == LOAD_ABSENT
    assert r.tokens == {}
    assert r.durable is True


def test_an_unreadable_store_is_not_an_empty_one(state_dir):
    """The three-outcome rule at the point it bites.

    Both cases yield zero tokens. Only one of them means 'nothing was
    stored'. Collapsing them recreates the original bug exactly - the
    server would believe it is durable, restore nothing, and 403 every
    surviving agent with no warning.
    """
    tokens_path(state_dir).write_text("{ not json at all")
    r = load_tokens(state_dir)
    assert r.status == LOAD_UNREADABLE
    assert r.tokens == {}
    assert r.durable is False, "an unreadable store must NOT claim durability"
    assert r.detail


def test_an_unknown_schema_is_ignored_not_guessed_at(state_dir):
    """A partially-understood store must not authenticate anybody.

    Half-parsing would validate some sessions and reject others, which is
    harder to diagnose than losing all of them.
    """
    tokens_path(state_dir).write_text(
        json.dumps({"schema": 999, "tokens": {"ses_a": "tok"}})
    )
    r = load_tokens(state_dir)
    assert r.status == LOAD_UNREADABLE
    assert r.tokens == {}


def test_tokens_for_dead_sessions_are_garbage_collected(state_dir):
    """A token whose session is gone can never be presented again.

    Keeping it is pure credential accumulation - the file would grow for
    the life of the install.
    """
    save_tokens(state_dir, {"ses_a": "a", "ses_b": "b", "ses_c": "c"})
    r = load_tokens(state_dir, live_session_ids=["ses_a", "ses_c"])
    assert sorted(r.tokens) == ["ses_a", "ses_c"]


def test_no_gc_when_the_session_list_is_unknown(state_dir):
    """None means 'I do not know yet', which must not delete anything.

    Treating an unknown session list as an empty one would wipe every
    token at startup - the bug, with extra steps.
    """
    save_tokens(state_dir, {"ses_a": "a", "ses_b": "b"})
    assert sorted(load_tokens(state_dir, live_session_ids=None).tokens) == [
        "ses_a", "ses_b",
    ]


def test_the_file_is_owner_only(state_dir):
    """Credential material. Group- or world-readable is silent and permanent."""
    save_tokens(state_dir, {"ses_a": "tok"})
    mode = os.stat(tokens_path(state_dir)).st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_a_failed_save_reports_rather_than_pretending(state_dir):
    """A store that fails quietly recreates the bug it was built to fix."""
    ok, reason = save_tokens(state_dir / "no" / "such" / "tree" / "x", {"a": "b"})
    # Either it created the tree (fine) or it refused and SAID so.
    assert ok is True or (ok is False and reason)


def test_a_write_leaves_no_temp_files_behind(state_dir):
    save_tokens(state_dir, {"ses_a": "tok"})
    leftovers = [p.name for p in state_dir.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_junk_entries_are_dropped_not_carried(state_dir):
    tokens_path(state_dir).write_text(
        json.dumps({"schema": 1, "tokens": {"ok": "t", "bad": 5, "": "x"}})
    )
    assert load_tokens(state_dir).tokens == {"ok": "t"}


# ---- schema 2: the tmux name is the other half ------------------------

def test_the_tmux_name_round_trips_beside_the_token(state_dir):
    """Surviving a restart needs BOTH halves.

    The token gets a hook past authentication. The tmux name is what lets
    the server work out which session it belongs to. Persisting only the
    token was measured live: hooks authenticated, returned 200, and
    resolved to nothing - which records exactly as much as a 403 did,
    while looking like success from the agent's side.
    """
    save_tokens(state_dir, {"ses_a": "tok"}, tmux_names={"ses_a": "cloude_x"})
    r = load_tokens(state_dir)
    assert r.tokens == {"ses_a": "tok"}
    assert r.tmux_names == {"ses_a": "cloude_x"}


def test_a_v1_file_still_authenticates_and_says_it_has_no_names(state_dir):
    """Old files must not be discarded.

    A v1 entry can authenticate and cannot be resolved. That is strictly
    better than throwing it away, and reporting an empty tmux_names map
    is the honest description of it rather than a silent gap.
    """
    tokens_path(state_dir).write_text(
        json.dumps({"schema": 1, "tokens": {"ses_a": "tok"}})
    )
    r = load_tokens(state_dir)
    assert r.status == LOAD_OK
    assert r.tokens == {"ses_a": "tok"}
    assert r.tmux_names == {}


def test_a_token_with_no_name_recorded_is_not_invented(state_dir):
    save_tokens(state_dir, {"ses_a": "tok"}, tmux_names=None)
    r = load_tokens(state_dir)
    assert r.tokens == {"ses_a": "tok"}
    assert r.tmux_names == {}


def test_names_are_garbage_collected_with_their_tokens(state_dir):
    save_tokens(
        state_dir,
        {"ses_a": "a", "ses_b": "b"},
        tmux_names={"ses_a": "n_a", "ses_b": "n_b"},
    )
    r = load_tokens(state_dir, live_session_ids=["ses_a"])
    assert sorted(r.tokens) == ["ses_a"]
    assert sorted(r.tmux_names) == ["ses_a"]
