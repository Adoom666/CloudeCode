"""Star-chosen favorites replace the hand-picked ``common_slash_commands``.

WHAT HAS TO STAY TRUE. The key, its two entry forms and its migration
chain are unchanged; only the AUTHOR changed, from someone hand-editing
config.json to the user tapping a star. So every test here is really one
of three claims:

  1. an existing config keeps rendering exactly what it rendered, and a
     user's own hand-authored entry survives a write it was not part of;
  2. the THREE states stay distinguishable - key absent (never starred,
     show defaults), key present with entries (the favorites), key
     present and EMPTY (unstarred everything, which is a choice and must
     not be re-seeded);
  3. a toggle actually persists, including unstarring a default on a
     config that never declared the key.

State 3 is the one a naive implementation loses: ``AuthConfig`` defaults
the field to ``[]``, so once config.json has been parsed, "absent" and
"empty" are the same value. That is why every read goes to the file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_fav_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_fav_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core import slash_command_labels, slash_favorites

DEFAULTS = slash_command_labels.DEFAULT_COMMON_COMMANDS


@pytest.fixture
def config_file(tmp_path):
    """A minimal config.json with NO ``common_slash_commands`` key.

    Inputs: tmp_path (Path) - pytest tmp dir.
    Output: Path - the written config.json.
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 4, "projects": []}, indent=2))
    return path


def _read_key(path: Path):
    """Read the favorites key straight off disk.

    Inputs: path (Path) - config.json.
    Output: Any - the stored value; KeyError if absent.
    """
    return json.loads(path.read_text())[slash_favorites.FAVORITES_KEY]


# ---------------------------------------------------------------------------
# The three states
# ---------------------------------------------------------------------------

def test_absent_key_yields_the_built_in_defaults(config_file):
    """A fresh install gets a useful row, not an empty bar."""
    body = slash_favorites.payload(config_file)
    assert body["commands"] == DEFAULTS
    assert body["defaulted"] is True


def test_declared_entries_are_authoritative(config_file):
    data = json.loads(config_file.read_text())
    data[slash_favorites.FAVORITES_KEY] = ["/clear", "/diff"]
    config_file.write_text(json.dumps(data))
    body = slash_favorites.payload(config_file)
    assert body["commands"] == ["/clear", "/diff"]
    assert body["defaulted"] is False


def test_declared_but_EMPTY_is_not_re_seeded(config_file):
    """The state a naive implementation loses.

    Unstarring everything is a decision. Falling back to the defaults
    here would silently undo it, and the user would watch ten chips
    reappear after removing them one by one.
    """
    data = json.loads(config_file.read_text())
    data[slash_favorites.FAVORITES_KEY] = []
    config_file.write_text(json.dumps(data))
    body = slash_favorites.payload(config_file)
    assert body["commands"] == []
    assert body["defaulted"] is False


def test_read_raw_reports_key_presence_both_ways(config_file):
    raw, declared = slash_favorites.read_raw(config_file)
    assert (raw, declared) == ([], False)
    data = json.loads(config_file.read_text())
    data[slash_favorites.FAVORITES_KEY] = []
    config_file.write_text(json.dumps(data))
    assert slash_favorites.read_raw(config_file) == ([], True)


# ---------------------------------------------------------------------------
# Toggling
# ---------------------------------------------------------------------------

def test_starring_on_an_undeclared_config_materializes_the_defaults(config_file):
    raw, declared = slash_favorites.read_raw(config_file)
    out = slash_favorites.toggle(raw, declared, "/diff", True)
    assert out == DEFAULTS + ["/diff"]


def test_unstarring_a_default_on_an_undeclared_config_actually_removes_it(config_file):
    """The bug this guards: a toggle that silently does nothing.

    Without materializing the defaults first, there is nothing to remove
    from, the write stores a list that still contains the command, and
    the chip comes straight back.
    """
    raw, declared = slash_favorites.read_raw(config_file)
    out = slash_favorites.toggle(raw, declared, "/clear", False)
    assert "/clear" not in out
    assert set(out) == set(DEFAULTS) - {"/clear"}


def test_starring_is_idempotent(config_file):
    once = slash_favorites.toggle(["/clear"], True, "/clear", True)
    assert once == ["/clear"]


def test_unstarring_something_absent_is_a_no_op():
    assert slash_favorites.toggle(["/clear"], True, "/diff", False) == ["/clear"]


def test_a_missing_leading_slash_is_normalized():
    assert slash_favorites.toggle([], True, "diff", True) == ["/diff"]
    assert slash_favorites.toggle(["/diff"], True, "diff", False) == []


def test_blank_command_is_refused():
    with pytest.raises(slash_favorites.FavoritesError):
        slash_favorites.toggle([], True, "   ", True)


def test_the_favorites_cap_is_enforced():
    full = [f"/c{i}" for i in range(slash_favorites.MAX_FAVORITES)]
    with pytest.raises(slash_favorites.FavoritesError):
        slash_favorites.toggle(full, True, "/one-too-many", True)
    # Re-starring one already in the list must NOT trip the cap.
    assert slash_favorites.toggle(full, True, "/c0", True) == full


# ---------------------------------------------------------------------------
# Backward compatibility with the two entry forms
# ---------------------------------------------------------------------------

def test_object_form_entries_survive_a_write_byte_for_byte(config_file):
    """A user's own wording must not be rewritten by starring something else."""
    mine = {"command": "/deploy", "description": "ship it"}
    out = slash_favorites.toggle([mine, "/clear"], True, "/diff", True)
    assert out[0] is mine or out[0] == mine
    assert out == [mine, "/clear", "/diff"]


def test_object_form_entries_can_be_unstarred(config_file):
    mine = {"command": "/deploy", "description": "ship it"}
    assert slash_favorites.toggle([mine], True, "/deploy", False) == []


def test_mixed_forms_resolve_with_the_users_description_winning():
    details = slash_favorites.resolve(
        ["/clear", {"command": "/clear2", "description": "mine"}], True
    )
    assert details[0]["description"] == "wipe conversation"
    assert details[1]["description"] == "mine"


def test_is_favorite_reads_both_forms():
    raw = ["/clear", {"command": "/deploy"}]
    assert slash_favorites.is_favorite(raw, True, "/clear")
    assert slash_favorites.is_favorite(raw, True, "deploy")
    assert not slash_favorites.is_favorite(raw, True, "/nope")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_write_persists_and_leaves_the_rest_of_the_config_alone(config_file):
    slash_favorites.write(config_file, ["/clear"])
    data = json.loads(config_file.read_text())
    assert data[slash_favorites.FAVORITES_KEY] == ["/clear"]
    assert data["version"] == 4
    assert data["projects"] == []


def test_write_leaves_a_one_generation_backup(config_file):
    slash_favorites.write(config_file, ["/clear"])
    backup = config_file.with_suffix(config_file.suffix + ".bak")
    assert backup.exists()
    assert slash_favorites.FAVORITES_KEY not in json.loads(backup.read_text())


def test_write_then_read_round_trips_an_empty_list(config_file):
    slash_favorites.write(config_file, [])
    body = slash_favorites.payload(config_file)
    assert body["commands"] == []
    assert body["defaulted"] is False


def test_a_missing_config_raises_rather_than_inventing_an_answer(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError):
        slash_favorites.read_raw(missing)
    with pytest.raises(FileNotFoundError):
        slash_favorites.write(missing, [])


def test_invalid_json_raises_rather_than_defaulting(tmp_path):
    broken = tmp_path / "config.json"
    broken.write_text("{not json")
    with pytest.raises(ValueError):
        slash_favorites.read_raw(broken)


def test_a_non_list_stored_value_degrades_to_empty_rather_than_crashing(config_file):
    """A hand-edited string must not take the chip row down with it."""
    data = json.loads(config_file.read_text())
    data[slash_favorites.FAVORITES_KEY] = "/clear"
    config_file.write_text(json.dumps(data))
    assert slash_favorites.payload(config_file)["commands"] == []
