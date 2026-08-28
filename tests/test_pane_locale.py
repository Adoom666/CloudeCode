"""Tests for pane locale resolution.

The bug these guard: a LaunchAgent-spawned server has no LANG, the tmux
pane inherits that, and zsh prints "character not in range" once per line
of any function that touches a multibyte character.

The requirement is not merely "set a UTF-8 locale". It is "prefer the
user's OWN locale", because the app has more than one user and a
hardcoded en_US.UTF-8 silently retitles someone else's shell.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core import pane_locale  # noqa: E402
from src.core.pane_locale import (  # noqa: E402
    apply_pane_locale,
    is_utf8_locale,
    resolve_pane_locale,
    to_utf8_locale,
)


@pytest.fixture(autouse=True)
def _no_subprocess_probes(monkeypatch):
    """Keep every test hermetic: no login shell, no `defaults`, no locale -a.

    Each test opts back in by monkeypatching the specific probe it cares
    about. Without this the results would depend on the developer's own
    machine, which is exactly the coupling the module exists to remove.
    """
    monkeypatch.setattr(pane_locale, "_user_shell_locale", lambda: "")
    monkeypatch.setattr(pane_locale, "_os_preference_locale", lambda: "")
    monkeypatch.setattr(
        pane_locale,
        "_available_locales",
        lambda: ["C", "POSIX", "C.UTF-8", "en_US.UTF-8", "de_DE.UTF-8", "ja_JP.UTF-8"],
    )
    monkeypatch.setattr(pane_locale, "_probe_cache", {})


@pytest.mark.parametrize(
    "value,expected",
    [
        ("en_US.UTF-8", True),
        ("en_US.utf8", True),
        ("C.UTF-8", True),
        ("en_US", False),
        ("C", False),
        ("de_DE.ISO8859-1", False),
        ("", False),
    ],
)
def test_is_utf8_locale(value, expected):
    assert is_utf8_locale(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("en_US", "en_US.UTF-8"),
        ("de_DE.ISO8859-1", "de_DE.UTF-8"),
        ("ja_JP.eucJP@modifier", "ja_JP.UTF-8@modifier"),
        ("", ""),
    ],
)
def test_to_utf8_locale(value, expected):
    assert to_utf8_locale(value) == expected


def test_existing_utf8_env_is_left_alone():
    """An operator who already set a UTF-8 locale must not be overridden."""
    assert resolve_pane_locale({"LANG": "ja_JP.UTF-8"}) == ""
    assert resolve_pane_locale({"LC_ALL": "de_DE.UTF-8"}) == ""


def test_users_own_shell_locale_wins(monkeypatch):
    """The user's login shell is preferred over any invented default."""
    monkeypatch.setattr(pane_locale, "_user_shell_locale", lambda: "de_DE.UTF-8")
    monkeypatch.setattr(pane_locale, "_os_preference_locale", lambda: "en_US")
    assert resolve_pane_locale({}) == "de_DE.UTF-8"


def test_user_shell_non_utf8_is_upgraded_not_replaced(monkeypatch):
    """A user working in de_DE keeps de_DE, only the codeset changes."""
    monkeypatch.setattr(pane_locale, "_user_shell_locale", lambda: "de_DE.ISO8859-1")
    assert resolve_pane_locale({}) == "de_DE.UTF-8"


def test_env_non_utf8_beats_os_preference(monkeypatch):
    """A deliberate LANG=ja_JP outranks the machine's regional setting."""
    monkeypatch.setattr(pane_locale, "_os_preference_locale", lambda: "en_US")
    assert resolve_pane_locale({"LANG": "ja_JP"}) == "ja_JP.UTF-8"


def test_os_preference_used_when_user_says_nothing(monkeypatch):
    monkeypatch.setattr(pane_locale, "_os_preference_locale", lambda: "de_DE")
    assert resolve_pane_locale({}) == "de_DE.UTF-8"


def test_last_resort_is_a_neutral_locale():
    """Nobody expressed a preference: pick C.UTF-8, not a language."""
    assert resolve_pane_locale({}) == "C.UTF-8"


def test_unsupported_candidates_fall_through(monkeypatch):
    """A locale the C library lacks is skipped, not exported blindly."""
    monkeypatch.setattr(pane_locale, "_user_shell_locale", lambda: "xx_XX.UTF-8")
    monkeypatch.setattr(pane_locale, "_available_locales", lambda: ["C", "en_US.UTF-8"])
    assert resolve_pane_locale({}) == "en_US.UTF-8"


def test_nothing_supported_returns_empty(monkeypatch):
    """No UTF-8 locale exists at all: say nothing rather than lie."""
    monkeypatch.setattr(pane_locale, "_available_locales", lambda: ["C", "POSIX"])
    assert resolve_pane_locale({}) == ""


def test_apply_sets_lang_not_lc_all():
    """LANG is a default; LC_ALL is an override that would break the user."""
    env = {"PATH": "/usr/bin"}
    apply_pane_locale(env)
    assert env["LANG"] == "C.UTF-8"
    assert "LC_ALL" not in env


def test_apply_is_a_noop_when_already_utf8():
    env = {"LANG": "ja_JP.UTF-8"}
    apply_pane_locale(env)
    assert env["LANG"] == "ja_JP.UTF-8"


def test_probe_failures_are_survivable(monkeypatch):
    """Every helper subprocess failing must still yield a usable locale."""
    monkeypatch.setattr(pane_locale, "_run", lambda argv: "")
    monkeypatch.setattr(pane_locale, "_user_shell_locale", pane_locale._user_shell_locale)
    monkeypatch.setattr(pane_locale, "_os_preference_locale", pane_locale._os_preference_locale)
    monkeypatch.setattr(pane_locale, "_available_locales", pane_locale._available_locales)
    assert resolve_pane_locale({}) == "C.UTF-8"
