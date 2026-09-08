"""THE MESSAGE ARCHIVE IS OFF BY DEFAULT, AND THIS FILE IS WHY IT STAYS OFF.

THE SINGLE MOST IMPORTANT ASSERTION IN THE FEATURE lives here:
``test_the_default_is_off``. Everything else in this branch is machinery
for a switch; this is the assertion that a future edit cannot silently
flip that switch on for every existing user. The message archive creates
a schema, starts a background thread that walks ``~/.claude/projects``
and indexes the user's own conversations, mounts thirteen routes and
reveals a screen. A default of True would ship all of that to people who
upgraded and never asked.

IT IS ASSERTED THREE WAYS, ON PURPOSE, because the three could drift:

  1. ``MessageArchiveConfig().enabled`` - the pydantic field, which is
     what a settings surface or an API writer would read;
  2. ``message_archive_flag.DEFAULT_ENABLED`` - the documented constant;
  3. ``resolve()`` against a real ``config.json`` with no
     ``message_archive`` block - the answer the four gates actually act
     on, which is the only one of the three that can start a thread.

A test that checked only (3) would pass while the model default said
True, and a settings screen written against the model would then write
``true`` into every config it touched.

EVERY TEST HERE DELETES ``CLOUDE_MESSAGE_ARCHIVE`` FIRST. tests/conftest.py
sets it to "1" for the rest of the suite (the suite predates the flag and
several hundred assertions exercise the archive), so a default test that
did not delete it would be measuring conftest, not the default - a check
that cannot fail, which is the defect class this codebase names by name.
``_no_env`` is that deletion, and ``test_conftest_forces_it_on`` asserts
the deletion is load-bearing rather than decorative.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_maf_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_maf_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
import pytest

from src.core import message_archive_flag as flag


@pytest.fixture()
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the suite-wide env override so the default is measurable.

    Description: tests/conftest.py sets ``CLOUDE_MESSAGE_ARCHIVE=1`` for
      the whole suite. Without this fixture every assertion below would
      measure that line instead of the default it claims to check.
    Inputs: monkeypatch (pytest.MonkeyPatch).
    Output: None.
    Example: def test_x(no_env): ...
    """
    monkeypatch.delenv(flag.ENABLE_ENV, raising=False)


def _write_config(tmp_path: Path, payload: dict) -> Path:
    """Write a config.json and return its path.

    Inputs: tmp_path (Path) - pytest's throwaway dir. payload (dict) -
      the JSON object to write.
    Output: Path to the file written.
    Example: _write_config(tmp_path, {}) -> PosixPath('.../config.json')
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# THE DEFAULT
# ---------------------------------------------------------------------------


def test_the_default_is_off(no_env: None, tmp_path: Path) -> None:
    """Off by default, asserted at all three declarations at once."""
    from src.config import MessageArchiveConfig

    assert MessageArchiveConfig().enabled is False, (
        "the pydantic default for message_archive.enabled is True; every "
        "install that has never heard of this feature would get a "
        "background transcript indexer on upgrade"
    )
    assert flag.DEFAULT_ENABLED is False, (
        "message_archive_flag.DEFAULT_ENABLED is True"
    )
    resolution = flag.resolve(_write_config(tmp_path, {"jwt_expiry_minutes": 30}))
    assert resolution.state == flag.STATE_DISABLED, (
        "a config.json with no message_archive block resolved to "
        f"{resolution.state!r}; absent must mean off"
    )
    assert resolution.enabled is False


def test_the_shipped_example_config_ships_it_off(no_env: None) -> None:
    """config.example.json documents the key AND ships it false."""
    data = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert flag.CONFIG_KEY in data, (
        "config.example.json does not document the message_archive key, so "
        "nobody can discover how to turn the feature on"
    )
    assert data[flag.CONFIG_KEY][flag.CONFIG_ENABLED_FIELD] is False, (
        "the example config ships the message archive ON"
    )


def test_conftest_forces_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env deletion in `no_env` is load-bearing, not decoration.

    Without this, a future conftest edit that dropped the override would
    make every `no_env` fixture above a no-op and nobody would notice.
    """
    assert os.environ.get(flag.ENABLE_ENV) == "1", (
        "tests/conftest.py no longer forces the archive on; the no_env "
        "fixture is now measuring nothing"
    )


# ---------------------------------------------------------------------------
# THE THIRD OUTCOME
# ---------------------------------------------------------------------------


def test_an_unreadable_config_is_cannot_determine_not_off(
    no_env: None, tmp_path: Path
) -> None:
    """Broken config resolves to cannot_determine, and gates closed."""
    path = tmp_path / "config.json"
    path.write_text("{not json at all", encoding="utf-8")
    resolution = flag.resolve(path)
    assert resolution.state == flag.STATE_CANNOT_DETERMINE, (
        "an unparseable config.json was reported as a definite answer; a "
        "user who turned the feature on cannot then be told it is off"
    )
    assert resolution.enabled is False, "cannot_determine must gate closed"
    assert "could not be read" in resolution.reason


def test_a_non_boolean_value_is_cannot_determine(
    no_env: None, tmp_path: Path
) -> None:
    """`"enabled": "yes"` is not a boolean and is not guessed at."""
    path = _write_config(tmp_path, {flag.CONFIG_KEY: {"enabled": "yes"}})
    resolution = flag.resolve(path)
    assert resolution.state == flag.STATE_CANNOT_DETERMINE
    assert resolution.enabled is False


def test_an_unrecognised_env_value_is_cannot_determine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typo in the kill switch must not fail open."""
    monkeypatch.setenv(flag.ENABLE_ENV, "enabled-please")
    resolution = flag.resolve(_write_config(tmp_path, {}))
    assert resolution.state == flag.STATE_CANNOT_DETERMINE
    assert resolution.enabled is False


def test_a_missing_config_file_is_a_definite_off(
    no_env: None, tmp_path: Path
) -> None:
    """No config.json means nobody opted in - a definite off, not unknown."""
    resolution = flag.resolve(tmp_path / "nothing-here.json")
    assert resolution.state == flag.STATE_DISABLED
    assert resolution.source == flag.SOURCE_DEFAULT


# ---------------------------------------------------------------------------
# TURNING IT ON, AND PRECEDENCE
# ---------------------------------------------------------------------------


def test_true_in_config_turns_it_on(no_env: None, tmp_path: Path) -> None:
    """The documented way in."""
    path = _write_config(tmp_path, {flag.CONFIG_KEY: {"enabled": True}})
    resolution = flag.resolve(path)
    assert resolution.state == flag.STATE_ENABLED
    assert resolution.enabled is True
    assert resolution.source == flag.SOURCE_CONFIG


@pytest.mark.parametrize("value", ["1", "true", "ON", " yes "])
def test_env_can_force_it_on_over_a_false_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    """The env override wins, in both directions - here, on."""
    monkeypatch.setenv(flag.ENABLE_ENV, value)
    path = _write_config(tmp_path, {flag.CONFIG_KEY: {"enabled": False}})
    assert flag.resolve(path).state == flag.STATE_ENABLED


@pytest.mark.parametrize("value", ["0", "false", "OFF", " no "])
def test_env_can_force_it_off_over_a_true_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    """The env override wins, in both directions - here, off."""
    monkeypatch.setenv(flag.ENABLE_ENV, value)
    path = _write_config(tmp_path, {flag.CONFIG_KEY: {"enabled": True}})
    assert flag.resolve(path).state == flag.STATE_DISABLED


def test_settings_loads_the_block_from_config_json(
    no_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The typed model reads the same key the resolver reads.

    Two readers of one key can drift. This pins them together: a config
    that the resolver calls enabled must also present enabled on the
    typed AuthConfig a settings surface would edit.
    """
    from src.config import Settings

    path = _write_config(
        tmp_path,
        {
            "jwt_expiry_minutes": 30,
            flag.CONFIG_KEY: {"enabled": True},
        },
    )
    settings = Settings(
        auth_config_file=str(path),
        totp_secret="testsecretnotreal",
        jwt_secret="testjwtnotreal",
    )
    assert settings.load_auth_config().message_archive.enabled is True
    assert flag.resolve(path).enabled is True
