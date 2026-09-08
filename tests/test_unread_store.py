"""Tests for src.core.unread_store.UnreadStore — pure I/O + dict logic,
isolated from SessionManager (which is covered in
tests/test_hook_driven_status.py).

Run with:
    python3 -m pytest tests/test_unread_store.py -v
"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_us_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_us_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.unread_store import UnreadStore


def test_missing_file_is_empty_store(tmp_path):
    store = UnreadStore(tmp_path / "unread_state.json")
    assert store.is_unread("anything") is False
    assert store.raw == {}


def test_set_flag_persists_and_is_unread_true(tmp_path):
    path = tmp_path / "unread_state.json"
    store = UnreadStore(path)
    store.set_flag("proj", "auto", True)
    assert store.is_unread("proj") is True
    assert path.exists()


def test_clearing_only_sub_flag_still_unread_if_other_set(tmp_path):
    store = UnreadStore(tmp_path / "unread_state.json")
    store.set_flag("proj", "auto", True)
    store.set_flag("proj", "manual", True)
    store.set_flag("proj", "auto", False)
    assert store.is_unread("proj") is True  # manual still set


def test_clearing_both_flags_drops_the_entry(tmp_path):
    path = tmp_path / "unread_state.json"
    store = UnreadStore(path)
    store.set_flag("proj", "auto", True)
    store.set_flag("proj", "auto", False)
    assert store.is_unread("proj") is False
    assert "proj" not in store.raw
    data = json.loads(path.read_text())
    assert "proj" not in data


def test_set_flag_falsy_name_is_a_safe_noop(tmp_path):
    store = UnreadStore(tmp_path / "unread_state.json")
    store.set_flag("", "auto", True)
    store.set_flag(None, "auto", True)
    assert store.raw == {}


def test_new_instance_reads_persisted_state(tmp_path):
    path = tmp_path / "unread_state.json"
    store1 = UnreadStore(path)
    store1.set_flag("proj", "manual", True)

    store2 = UnreadStore(path)
    assert store2.is_unread("proj") is True


def test_load_tolerates_malformed_json(tmp_path):
    path = tmp_path / "unread_state.json"
    path.write_text("{ not valid json")
    store = UnreadStore(path)  # must not raise
    assert store.raw == {}


def test_load_tolerates_non_dict_top_level(tmp_path):
    path = tmp_path / "unread_state.json"
    path.write_text(json.dumps(["not", "a", "dict"]))
    store = UnreadStore(path)
    assert store.raw == {}


def test_load_drops_malformed_entries(tmp_path):
    path = tmp_path / "unread_state.json"
    path.write_text(json.dumps({
        "good": {"auto": True, "manual": False},
        "bad": "not-a-dict",
        "empty": {"auto": False, "manual": False},
    }))
    store = UnreadStore(path)
    assert store.is_unread("good") is True
    assert "bad" not in store.raw
    assert "empty" not in store.raw  # both-false rows are pruned on load


def test_prune_drops_names_not_in_alive_set(tmp_path):
    store = UnreadStore(tmp_path / "unread_state.json")
    store.set_flag("alive_proj", "auto", True)
    store.set_flag("dead_proj", "auto", True)
    store.prune({"alive_proj"})
    assert store.is_unread("alive_proj") is True
    assert store.is_unread("dead_proj") is False


def test_prune_with_no_entries_is_a_safe_noop(tmp_path):
    store = UnreadStore(tmp_path / "unread_state.json")
    store.prune(set())  # must not raise, no file write needed


# ---------------------------------------------------------------------------
# THE KEY IS THE INSTANCE, NOT THE NAME.
#
# tmux names are reused. Keyed on the name alone, an unread flag set on a
# killed session reappeared on the next session to take its name, pointing
# the user at a Stop that happened in a conversation that no longer exists.
# The key is now ``<tmux_name>@<#{session_created}>``, the same identity
# rule ``sessions.tmux_created_epoch`` uses throughout src/core.
#
# The three cases that matter are: two instances of one name do not share a
# flag; an unmeasurable epoch degrades to the legacy key rather than
# minting a second entry; and a store written before the re-key still
# answers and migrates on write rather than being read as empty.
# ---------------------------------------------------------------------------


def test_compose_key_shapes(tmp_path):
    assert UnreadStore.compose_key("cloude_a", 1757000000) == "cloude_a@1757000000"
    assert UnreadStore.compose_key("cloude_a", None) == "cloude_a"


def test_name_of_recovers_the_name_and_leaves_a_literal_at_alone(tmp_path):
    assert UnreadStore.name_of("cloude_a@17") == "cloude_a"
    # A tmux name may legitimately contain an "@" and must not be
    # truncated at it - only a trailing all-digits tail is an epoch.
    assert UnreadStore.name_of("user@host") == "user@host"
    assert UnreadStore.name_of("cloude_a") == "cloude_a"


def test_two_instances_of_one_name_do_not_share_a_flag(tmp_path):
    """The whole point of the re-key: a recycled tmux name starts clean."""
    store = UnreadStore(tmp_path / "u.json")
    store.set_flag("cloude_a", "auto", True, epoch=100)
    assert store.is_unread("cloude_a", 100) is True
    # A new session that took the same name is a different instance.
    assert store.is_unread("cloude_a", 200) is False


def test_a_restart_in_place_keeps_the_flag(tmp_path):
    """``respawn-pane -k`` replaces the pane's PROCESS; #{session_created}
    belongs to the SESSION and does not move, so the flag survives exactly
    the operation the user calls "restart"."""
    store = UnreadStore(tmp_path / "u.json")
    store.set_flag("cloude_a", "manual", True, epoch=100)
    assert store.is_unread("cloude_a", 100) is True


def test_an_unmeasurable_epoch_degrades_to_the_legacy_key(tmp_path):
    """None is an UNKNOWN instance, not a NEW one. It must not mint a
    second entry for a session that already has one."""
    store = UnreadStore(tmp_path / "u.json")
    store.set_flag("cloude_a", "auto", True, epoch=None)
    assert list(store.raw.keys()) == ["cloude_a"]
    assert store.is_unread("cloude_a", None) is True
    # And a later read that DOES know the epoch still finds it, rather
    # than reporting a session read when it is not.
    assert store.is_unread("cloude_a", 100) is True


def test_a_legacy_name_keyed_store_still_answers(tmp_path):
    """An upgrade must not silently drop every flag on disk."""
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"cloude_a": {"auto": True, "manual": False}}))
    store = UnreadStore(path)
    assert store.is_unread("cloude_a", 12345) is True


def test_a_legacy_entry_migrates_on_write_rather_than_duplicating(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"cloude_a": {"auto": True, "manual": False}}))
    store = UnreadStore(path)
    store.set_flag("cloude_a", "manual", True, epoch=777)
    assert list(store.raw.keys()) == ["cloude_a@777"]
    # Both sub-flags survived the move.
    assert store.raw["cloude_a@777"] == {"auto": True, "manual": True}


def test_clearing_both_flags_drops_the_composite_entry(tmp_path):
    store = UnreadStore(tmp_path / "u.json")
    store.set_flag("cloude_a", "auto", True, epoch=5)
    store.set_flag("cloude_a", "auto", False, epoch=5)
    assert store.raw == {}


def test_set_flag_is_idempotent(tmp_path):
    """Hook events are duplicated and droppable, so writing the same value
    twice must reach the same file."""
    store = UnreadStore(tmp_path / "u.json")
    store.set_flag("cloude_a", "auto", True, epoch=5)
    first = json.loads((tmp_path / "u.json").read_text())
    store.set_flag("cloude_a", "auto", True, epoch=5)
    assert json.loads((tmp_path / "u.json").read_text()) == first


def test_prune_compares_on_the_name_half_of_a_composite_key(tmp_path):
    """Pruning on the raw key would delete every composite entry on the
    first pass and silently wipe the whole store."""
    store = UnreadStore(tmp_path / "u.json")
    store.set_flag("cloude_alive", "auto", True, epoch=5)
    store.set_flag("cloude_gone", "auto", True, epoch=6)
    store.prune({"cloude_alive"})
    assert list(store.raw.keys()) == ["cloude_alive@5"]
