"""A session's NAME is a label. The tmux name is an implementation detail.

WHY THE TWO WERE SPLIT. Renaming used to mean ``tmux rename-session``,
which moves the one field the identity key was built on, which split one
session into two rows. That whole failure mode is UNREACHABLE once the
user-facing name is a label stored on the row: the label moves, the tmux
name and the creation epoch never do, and the identity triple is never
touched by a rename at all.

It also fixes a second thing the user asked for directly - "i want to
make the name allow any chars including spaces. its a label" - which the
old ``^[A-Za-z0-9_-]{1,64}$`` rename validator forbade outright.

WHAT WAS MEASURED, NOT ASSUMED. tmux itself accepts spaces, colons,
dots, quotes, slashes and dollars in a session name (checked on a
throwaway socket). What it does NOT preserve is non-ASCII: an emoji in a
session name comes back as underscores. And ``:`` and ``.`` are tmux
TARGET syntax separators, so a name carrying them is awkward for every
``-t`` in the codebase even though the name itself is legal. Both are
reasons the derived tmux name is sanitised while the label is not.

THE FILTER IS LOSSY, SO IT IS NOT AN IDENTITY. Two labels can sanitise
to the same tmux name, and that is fine because the tmux name is not
what identifies the session - a uniquifying suffix is appended and
nothing about the row moves.

Run with:
    ./venv/bin/python3 -m pytest tests/test_session_label.py -v
"""

from __future__ import annotations

import sys

import pytest

from tests.lifecycle_helpers import ROOT, add_row, conn, row_by_uuid

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402,F401

from src.core.session_label import (
    LABEL_MAX_CHARS,
    InvalidLabel,
    label_from_tmux_name,
    sanitize_tmux_name,
    set_label,
    unique_tmux_name,
    validate_label,
)


# ---------------------------------------------------------------------
# PART 1 - THE LABEL ACCEPTS WHAT HE ASKED FOR.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "Media Compression",
        "the one with the weird ffmpeg bug",
        "client: acme (v2)",
        "cost/benefit 50%",
        "emoji is fine here",
        "a" * LABEL_MAX_CHARS,
    ],
)
def test_a_label_may_contain_anything_printable(label):
    """Spaces, punctuation and case, all of which the old regex refused."""
    assert validate_label(label) == label


@pytest.mark.parametrize(
    "label",
    ["", "   ", "line\nbreak", "tab\there", "nul\x00byte", "a" * (LABEL_MAX_CHARS + 1)],
)
def test_a_label_is_still_refused_when_it_cannot_be_rendered(label):
    """Not a free-for-all: control characters and empty are still out.

    A newline in a label breaks every single-line surface that shows it
    and a NUL breaks the C string underneath, so these are rejected as
    unusable rather than accepted and silently mangled.
    """
    with pytest.raises(InvalidLabel):
        validate_label(label)


def test_a_label_is_stripped_of_surrounding_whitespace():
    """Leading/trailing space is almost always a paste artefact."""
    assert validate_label("  spaced out  ") == "spaced out"


# ---------------------------------------------------------------------
# PART 2 - THE FILTER. Label in, tmux-safe name out.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Media Compression", "Media_Compression"),
        ("client: acme", "client_acme"),
        ("dot.separated", "dot_separated"),
        ("lots    of     space", "lots_of_space"),
        ("__trim me__", "trim_me"),
        ("cost/benefit", "cost_benefit"),
    ],
)
def test_the_filter_produces_a_tmux_safe_name(label, expected):
    """`:` and `.` are tmux TARGET separators, so they cannot survive."""
    assert sanitize_tmux_name(label) == expected


def test_a_label_that_sanitises_to_nothing_still_yields_a_usable_name():
    """Never returns empty - tmux would reject it and the caller has no
    fallback of its own."""
    out = sanitize_tmux_name("!!!")
    assert out
    assert ":" not in out and "." not in out and " " not in out


def test_the_filter_drops_characters_tmux_would_mangle_anyway():
    """Measured: tmux stores an emoji session name as underscores."""
    out = sanitize_tmux_name("build 🚀 now")
    assert out.isascii()


def test_two_different_labels_that_collide_get_distinct_tmux_names():
    """THE LOSSY CASE, named. The filter is not injective and must not
    pretend to be: the second one is suffixed, not rejected, because the
    tmux name is not what identifies the session."""
    first = unique_tmux_name("client: acme", taken=set())
    second = unique_tmux_name("client. acme", taken={first})
    assert first != second
    assert second.startswith(first)


def test_a_free_name_is_not_suffixed():
    """No cosmetic mangling when there is no collision."""
    assert unique_tmux_name("solo", taken=set()) == "solo"


def test_the_uniquifier_terminates_under_heavy_collision():
    """A bounded suffix walk, then a hash - never an unbounded loop."""
    taken = {"busy"} | {f"busy_{n}" for n in range(2, 60)}
    out = unique_tmux_name("busy", taken=taken)
    assert out not in taken


# ---------------------------------------------------------------------
# PART 3 - THE LABEL IS STORED, AND IDENTITY DOES NOT MOVE.
# ---------------------------------------------------------------------


def test_setting_a_label_writes_title_and_touches_no_identity_column(conn):
    """The whole reason the split fixes defect 1."""
    add_row(conn, uuid="u-lab", name="cloude_Media", epoch=1787686975,
            tmux_session_id="$0")

    assert set_label(conn, session_uuid="u-lab", label="Media Compression")

    row = row_by_uuid(conn, "u-lab")
    assert row["title"] == "Media Compression"
    assert row["tmux_name"] == "cloude_Media", (
        "renaming a LABEL must not touch the tmux name - that is the "
        "field whose movement split one session into two rows"
    )
    assert row["tmux_created_epoch"] == 1787686975
    assert row["tmux_session_id"] == "$0"
    assert row["session_uuid"] == "u-lab"


def test_a_label_can_be_changed_repeatedly(conn):
    """Unlike the lineage seed, a user SET overwrites."""
    add_row(conn, uuid="u-twice", name="n", epoch=1, tmux_session_id="$1")
    set_label(conn, session_uuid="u-twice", label="first")
    set_label(conn, session_uuid="u-twice", label="second")
    assert row_by_uuid(conn, "u-twice")["title"] == "second"


def test_setting_a_label_on_a_row_that_is_not_there_reports_false(conn):
    """A definite negative, not a silent success and not an exception."""
    assert set_label(conn, session_uuid="nope", label="x") is False


def test_an_invalid_label_never_reaches_the_table(conn):
    """Validation happens before the write, not after it."""
    add_row(conn, uuid="u-bad", name="n", epoch=1)
    with pytest.raises(InvalidLabel):
        set_label(conn, session_uuid="u-bad", label="bad\nlabel")
    assert row_by_uuid(conn, "u-bad")["title"] is None


# ---------------------------------------------------------------------
# PART 4 - WHAT AN EXISTING ROW'S LABEL BECOMES.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "tmux_name,expected",
    [
        ("cloude_Media", "Media"),
        ("Media_Compression", "Media Compression"),
        ("cloude_my_long_thing", "my long thing"),
        ("cloude_", "cloude"),
        ("plain", "plain"),
    ],
)
def test_a_label_derived_from_a_tmux_name_drops_the_app_prefix(
    tmux_name, expected
):
    """``cloude_`` is an app-added artefact, not something he typed.

    ``cloude_`` alone keeps its stem rather than becoming empty: the
    prefix is only removed when something survives it, because a blank
    label is worse than an ugly one.
    """
    assert label_from_tmux_name(tmux_name) == expected
