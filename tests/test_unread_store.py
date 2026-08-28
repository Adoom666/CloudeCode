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
