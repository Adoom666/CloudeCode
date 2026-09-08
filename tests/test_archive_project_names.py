"""Folder-name display, collision disambiguation, and the cross-host merge.

THE SLUG IS NOT INVERTIBLE, which is the fact every test here is built
around. Measured on the live corpus 2026-09-01:

    slug ...Production-bhpp-new-server   -> real .../bhpp_new_server
    slug ...Development-3D-Work          -> real .../3D Work
    slug ...Production-dev-tools-scripts -> real .../tools/dev_tools/scripts

Splitting a slug on '-' yields 'server', 'Work' and 'scripts' - wrong
every time, and wrong in the direction that invents collisions between
projects that do not collide. So display names come from observed_cwd,
and these tests pin that rather than the convenient string operation.

Collisions measured across the 77 merged projects: 69 distinct final
segments, 3 colliding names covering 11 projects ('.claude' x4,
'outputs' x4, 'scripts' x3).
"""

from __future__ import annotations

import sqlite3

from src.core.archive_project_names import (
    disambiguate,
    fetch_project_rows,
    leaf_name,
    merge_projects,
    path_segments,
)


def _row(pid, slug, cwd, host_id, host_name, corpus_id=1, count=1):
    """One project row in the shape merge_projects consumes.

    Inputs: pid, slug, cwd, host_id, host_name, corpus_id, count.
    Output: dict.
    """
    return {
        "project_id": pid, "slug": slug, "observed_cwd": cwd,
        "corpus_id": corpus_id, "host_id": host_id,
        "host_display_name": host_name, "transcript_count": count,
    }


# --- display names come from the path, never from the slug ---------------

def test_the_folder_name_is_the_last_real_path_segment():
    """The whole point: 'Infrastructure', not the encoded slug."""
    assert leaf_name("/Users/jsugamele/Development/Assistants/Infrastructure") \
        == "Infrastructure"


def test_a_folder_name_containing_a_hyphen_survives():
    """THE CASE SLUG-SPLITTING GETS WRONG.

    The slug for this path is '...-Production-bhpp-new-server'. Splitting
    on '-' gives 'server'. The real folder is 'bhpp_new_server', and only
    observed_cwd knows that.
    """
    assert leaf_name("/Users/jsugamele/Development/Production/bhpp_new_server") \
        == "bhpp_new_server"


def test_a_folder_name_containing_a_space_survives():
    """'3D Work' - the slug destroyed the space into a hyphen."""
    assert leaf_name("/Users/jsugamele/Development/3D Work") == "3D Work"


def test_a_null_cwd_yields_a_null_name_rather_than_a_guess():
    """No observed_cwd means no folder name. The caller shows the slug.

    Fabricating a leaf from an un-invertible slug would put a wrong,
    confident name on screen, which is worse than showing the raw slug.
    """
    assert leaf_name(None) is None
    assert leaf_name("") is None
    assert leaf_name("   ") is None


def test_path_segments_ignores_trailing_and_repeated_slashes():
    """One decomposition, so naming and disambiguation cannot disagree."""
    assert path_segments("/a/b/c/") == ["a", "b", "c"]
    assert path_segments("/a//b") == ["a", "b"]
    assert path_segments(None) == []


# --- collisions ----------------------------------------------------------

def test_names_that_do_not_collide_stay_bare():
    """The common case - 66 of 77 projects - keeps just the folder."""
    assert disambiguate(["/x/Infrastructure", "/y/CloudeCode"]) \
        == ["Infrastructure", "CloudeCode"]


def test_a_collision_widens_by_exactly_one_segment():
    """Enough parent path to be unique, and not the whole slug."""
    assert disambiguate(["/Users/j/.claude", "/Users/j/.dotfiles/.claude"]) \
        == ["j/.claude", ".dotfiles/.claude"]


def test_the_real_four_way_claude_collision_resolves_uniquely():
    """The measured '.claude' x4 case, from the live corpus."""
    got = disambiguate([
        "/Users/jsugamele/.claude",
        "/Users/jsugamele/.dotfiles/.claude",
        "/Users/jsugamele/Development/Production/bhpp_new_server/.claude",
        "/Users/jsugamele/Scratch/.claude",
    ])
    assert got == [
        "jsugamele/.claude", ".dotfiles/.claude",
        "bhpp_new_server/.claude", "Scratch/.claude",
    ]
    assert len(set(got)) == 4


def test_a_collision_widens_as_far_as_it_must_and_no_further():
    """The measured 'outputs' x4 case differs only two levels up.

    A one-segment widening is not enough here - all four share their
    parent's parent - so this pins that the loop keeps going rather than
    giving up after a single pass, while projects that separated at one
    segment do NOT keep growing.
    """
    base = "/Users/j/Library/Application Support/Claude/local-agent-mode-sessions/S/T"
    got = disambiguate([
        base + "/local_aaa/outputs",
        base + "/local_bbb/outputs",
        base + "/local_ccc/outputs",
    ])
    assert got == ["local_aaa/outputs", "local_bbb/outputs", "local_ccc/outputs"]
    assert len(set(got)) == 3


def test_only_the_colliding_group_widens():
    """A unique name is not lengthened because some other pair clashed."""
    got = disambiguate(["/a/.claude", "/b/.claude", "/c/Infrastructure"])
    assert got == ["a/.claude", "b/.claude", "Infrastructure"]


def test_two_genuinely_identical_paths_stop_instead_of_looping_forever():
    """More path cannot separate the inseparable, so widening terminates.

    Inventing a suffix would assert a distinction the filesystem does not
    make.
    """
    got = disambiguate(["/a/b", "/a/b"])
    assert got[0] == got[1]
    assert got[0].endswith("b")


def test_a_null_path_in_the_set_does_not_break_the_others():
    """One unnamed project must not cost every sibling its name."""
    assert disambiguate(["/x/A", None, "/y/B"]) == ["A", None, "B"]


# --- the cross-host merge ------------------------------------------------

def test_the_same_project_on_two_machines_is_one_node_naming_both():
    """The measured case: 3 projects exist on both machines."""
    nodes = merge_projects([
        _row(1, "-Users-j-Media", "/Users/j/Media", 1, "Joe-MBP-M1", 1, 10),
        _row(2, "-Users-j-Media", "/Users/j/Media", 2, "Mac mini", 3, 5),
    ])
    assert len(nodes) == 1
    assert nodes[0]["display_name"] == "Media"
    assert nodes[0]["hosts"] == ["Joe-MBP-M1", "Mac mini"]
    assert nodes[0]["host_count"] == 2
    assert nodes[0]["transcript_count"] == 15
    assert len(nodes[0]["members"]) == 2


def test_the_merge_keys_on_the_real_path_not_on_the_slug():
    """Two hosts whose slugs match but whose real paths differ stay APART.

    Wrongly splitting a project shows two nodes; wrongly merging two
    shows a project that does not exist. Only the second is unrecoverable
    from the UI, so the key is the path the database actually recorded.
    """
    nodes = merge_projects([
        _row(1, "-Users-j-a-b", "/Users/j/a/b", 1, "H1"),
        _row(2, "-Users-j-a-b", "/Users/j/a-b", 2, "H2"),
    ])
    assert len(nodes) == 2


def test_a_project_on_one_machine_still_names_that_machine():
    """The machine was demoted to a badge, not deleted."""
    nodes = merge_projects([_row(1, "-s", "/Users/j/Solo", 1, "Joe-MBP-M1")])
    assert nodes[0]["hosts"] == ["Joe-MBP-M1"]
    assert nodes[0]["host_count"] == 1


def test_full_path_carries_the_slug_through_unparsed():
    """The contract field, and it must be the raw slug."""
    nodes = merge_projects([
        _row(1, "-Users-j-Development-3D-Work", "/Users/j/Development/3D Work", 1, "H"),
    ])
    assert nodes[0]["full_path"] == "-Users-j-Development-3D-Work"
    assert nodes[0]["display_name"] == "3D Work"


def test_a_total_over_an_unmeasured_member_is_null_not_a_partial_sum():
    """A sum that silently omits a member is a number nobody measured."""
    nodes = merge_projects([
        _row(1, "-a", "/a", 1, "H1", 1, 10),
        {**_row(2, "-a", "/a", 2, "H2"), "transcript_count": None},
    ])
    assert nodes[0]["transcript_count"] is None


def test_rows_with_no_cwd_stay_separate_rather_than_fusing():
    """Two projects that cannot be PROVED identical are not merged."""
    nodes = merge_projects([
        _row(1, "-a", None, 1, "H1"),
        _row(2, "-b", None, 2, "H2"),
    ])
    assert len(nodes) == 2


def test_fetch_project_rows_joins_host_and_counts():
    """The read side produces exactly what merge_projects consumes."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE message_hosts (id INTEGER PRIMARY KEY, display_name TEXT);
        CREATE TABLE message_corpora (id INTEGER PRIMARY KEY, host_id INTEGER);
        CREATE TABLE message_projects (
            id INTEGER PRIMARY KEY, corpus_id INTEGER, slug TEXT, observed_cwd TEXT);
        CREATE TABLE message_transcripts (
            id INTEGER PRIMARY KEY, project_id INTEGER,
            session_ref_scheme TEXT, newest_message_ts TEXT);
        INSERT INTO message_hosts VALUES (1, 'H1');
        INSERT INTO message_corpora VALUES (1, 1);
        INSERT INTO message_projects VALUES (1, 1, '-Users-j-Infra', '/Users/j/Infra');
        INSERT INTO message_transcripts VALUES
            (1, 1, 'uuid', '2026-08-30T10:00:00Z'),
            (2, 1, 'agent', NULL);
        """
    )
    rows = fetch_project_rows(conn)
    assert rows == [{
        "project_id": 1, "slug": "-Users-j-Infra",
        "observed_cwd": "/Users/j/Infra", "corpus_id": 1, "host_id": 1,
        "host_display_name": "H1", "transcript_count": 2,
        # 2 transcripts, exactly 1 of them the owner's own. The two
        # numbers differ HERE, in the smallest possible fixture, so a
        # change that made session_count an alias of the total fails
        # this assertion rather than passing it.
        "session_count": 1, "session_counted": True,
        # Transcript 1 is dated and transcript 2 is not, so the project's
        # timestamp is the newest of the ones that HAVE one - a NULL
        # member is not evidence against the project as a whole.
        "newest_activity_at": "2026-08-30T10:00:00Z",
        "activity_counted": True,
    }]
    assert merge_projects(rows)[0]["display_name"] == "Infra"
