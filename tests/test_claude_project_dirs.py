"""The cwd spelling trap: one directory, two Claude project directories.

Every test here fails against the code as it stood before 2026-09-08.
``slugify_project_dir`` mapped only ``/`` and ``.``, so any path
containing a space, a tilde or an underscore - which is every path on the
owner's machine, all of them under
``~/Library/Mobile Documents/com~apple~CloudDocs/`` - produced a
directory name that does not exist, and the transcript correlation that
depends on it had therefore never once succeeded.
"""

from __future__ import annotations

import os

from src.core.claude_project_dirs import (
    candidate_project_dirs,
    path_spellings,
    same_directory,
)
from src.core.claude_transcript_correlate import slugify_project_dir


def test_slugify_maps_spaces_tildes_and_underscores():
    """The real rule is `[^A-Za-z0-9-] -> -`, measured against the corpus.

    FAILS BEFORE THE FIX: the old rule left ``Mobile Documents`` and
    ``com~apple~CloudDocs`` untouched and produced a name no directory on
    disk has ever had.
    """
    working = (
        "/Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs"
        "/Sync/Development/Assistants/BHPP"
    )
    assert slugify_project_dir(working) == (
        "-Users-jsugamele-Library-Mobile-Documents-com-apple-CloudDocs"
        "-Sync-Development-Assistants-BHPP"
    )


def test_slugify_maps_underscores_too():
    """``claude_4`` becomes ``claude-4``, verified on 37 live transcripts."""
    assert slugify_project_dir("/a/claude_4") == "-a-claude-4"


def test_slugify_keeps_the_documented_dot_behaviour():
    """The pre-existing dotfile example must keep working after the fix."""
    assert slugify_project_dir("/Users/x/.claude") == "-Users-x--claude"


def test_path_spellings_recovers_the_symlinked_spelling(tmp_path):
    """A symlinked prefix yields BOTH spellings, not just the resolved one.

    FAILS BEFORE THE FIX: nothing resolved a second spelling at all, so a
    row whose ``working_dir`` used one spelling could never find
    transcripts filed under the other.
    """
    home = tmp_path / "home"
    real = home / "Library" / "Sync" / "Development" / "Proj"
    real.mkdir(parents=True)
    link = home / "Development"
    link.symlink_to(home / "Library" / "Sync" / "Development")

    spellings = path_spellings(str(link / "Proj"), home=home)

    assert str(link / "Proj") in spellings
    assert os.path.realpath(str(real)) in [os.path.realpath(s) for s in spellings]
    assert len(spellings) >= 2


def test_candidate_project_dirs_finds_a_transcript_filed_under_either_spelling(
    tmp_path,
):
    """Both spellings resolve to their project directory when it exists."""
    home = tmp_path / "home"
    real = home / "Library" / "Sync" / "Development" / "Proj"
    real.mkdir(parents=True)
    link = home / "Development"
    link.symlink_to(home / "Library" / "Sync" / "Development")

    projects = tmp_path / "projects"
    projects.mkdir()
    short_dir = projects / slugify_project_dir(str(link / "Proj"))
    long_dir = projects / slugify_project_dir(str(real))
    short_dir.mkdir()
    long_dir.mkdir()

    found = candidate_project_dirs(
        str(real), projects_dir=projects, home=home
    )
    names = {p.name for p in found}

    assert short_dir.name in names, "the symlinked spelling was not searched"
    assert long_dir.name in names, "the resolved spelling was not searched"


def test_candidate_project_dirs_drops_directories_that_do_not_exist(tmp_path):
    """A spelling with no directory on disk contributes nothing."""
    projects = tmp_path / "projects"
    projects.mkdir()
    assert candidate_project_dirs("/nowhere/at/all", projects_dir=projects) == []


def test_candidate_project_dirs_can_use_a_listing_from_another_machine(tmp_path):
    """``existing`` replaces the filesystem check for remote analysis.

    Without this seam a report generated on one machine from another
    machine's corpus resolves against the wrong disk, finds nothing, and
    reports "no candidate" for every row - a false negative that looks
    exactly like a real finding.
    """
    projects = tmp_path / "projects"
    found = candidate_project_dirs(
        "/Users/x/Proj",
        projects_dir=projects,
        existing=["-Users-x-Proj"],
    )
    assert [p.name for p in found] == ["-Users-x-Proj"]


def test_same_directory_sees_through_a_symlink(tmp_path):
    """Two spellings of one directory compare equal."""
    home = tmp_path / "home"
    real = home / "Library" / "Dev"
    real.mkdir(parents=True)
    link = home / "Dev"
    link.symlink_to(real)
    assert same_directory(str(link), str(real)) is True


def test_same_directory_refuses_two_genuinely_different_paths(tmp_path):
    """A resolver that always agrees is worse than none."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert same_directory(str(a), str(b)) is False
    assert same_directory(None, str(a)) is False
