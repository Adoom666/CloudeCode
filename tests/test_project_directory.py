"""Tests for the folder a NEW project is created in.

The bug these lock down, measured in the browser 2026-09-08: "start
empty" had no folder step, so a project named "Punchlist Test" was
created at ``/Users/jsugamele/Development/ses_5a756046`` - a random
session id, in the SHORT symlink spelling - and that path was written
into ``sessions.working_dir`` as the project's permanent home.

Two independent defects, so two independent groups of tests: the path is
now CHOSEN (parent + name, validated against allowed roots), and it is
CANONICAL (realpath, so a symlinked parent records the long spelling).

The symlink test is the direct regression test for gotcha 6 in
CLAUDE.md: ``~/Development`` is a symlink into iCloud, and
``Path.expanduser()`` - which is what the old code used - resolves the
tilde and stops, leaving the short spelling behind.
"""

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_pd_wd_"))

from src.core.project_directory import (  # noqa: E402
    FALLBACK_PROJECTS_ROOT,
    MAX_NAME_BYTES,
    ProjectDirectoryVerdict,
    allowed_roots,
    ensure_project_directory,
    resolve_project_directory,
    validate_project_dir_name,
)


class _FakeSettings:
    """Minimal stand-in for Settings exposing only get_working_dir()."""

    def __init__(self, working_dir: Path):
        self._working_dir = working_dir

    def get_working_dir(self) -> Path:
        return self._working_dir


@pytest.fixture()
def roots(tmp_path):
    """A projects root inside tmp_path, plus settings pointing at it."""
    root = tmp_path / "projects"
    root.mkdir()
    return root, _FakeSettings(root)


# ---------------------------------------------------------------- names


@pytest.mark.parametrize(
    "name",
    [
        "My Awesome Project",  # spaces are legal, the owner's projects have them
        "cloude-code",
        "proj_2026",
        "a.b.c",  # dots are fine anywhere but the front
        "プロジェクト",
    ],
)
def test_legal_names_are_accepted_as_typed(name):
    assert validate_project_dir_name(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "a/b",
        "../escape",
        "..",
        ".",
        ".hidden",
        "bad\nname",
        "bad\tname",
        "back\\slash",
        "nul\x00byte",
        "x" * (MAX_NAME_BYTES + 1),
    ],
)
def test_illegal_names_are_refused_with_a_sentence(name):
    problem = validate_project_dir_name(name)
    assert problem, f"expected {name!r} to be refused"
    assert problem == problem.lower(), "ui copy is lowercase"


def test_a_refused_name_is_never_rewritten(roots):
    """A slash in a name is a refusal, never a silently sanitised folder."""
    root, settings = roots
    verdict = resolve_project_directory(str(root), "a/b", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "illegal_name"
    assert verdict.path is None
    assert not (root / "a-b").exists()


# ------------------------------------------------------- happy path


def test_allowed_parent_plus_name_composes_the_directory(roots):
    root, settings = roots
    verdict = resolve_project_directory(str(root), "Punchlist Test", settings=settings)
    assert verdict.ok is True
    assert verdict.code == "ok"
    assert verdict.path == str(Path(os.path.realpath(root)) / "Punchlist Test")
    # Resolution is pure: nothing exists until ensure_ is called.
    assert not Path(verdict.path).exists()


def test_ensure_creates_the_directory(roots):
    root, settings = roots
    verdict = ensure_project_directory(
        resolve_project_directory(str(root), "New Thing", settings=settings)
    )
    assert verdict.ok is True
    assert Path(verdict.path).is_dir()


def test_ensure_refuses_to_create_for_a_refused_verdict(roots):
    root, _settings = roots
    refused = ProjectDirectoryVerdict(
        ok=False, path=str(root / "nope"), code="illegal_name", message="no"
    )
    out = ensure_project_directory(refused)
    assert out.ok is False
    assert not (root / "nope").exists()


def test_a_name_with_spaces_survives_to_the_directory(roots):
    root, settings = roots
    verdict = ensure_project_directory(
        resolve_project_directory(str(root), "My Awesome Project", settings=settings)
    )
    assert Path(verdict.path).name == "My Awesome Project"
    assert Path(verdict.path).is_dir()


# ------------------------------------------- the long-spelling guarantee


def test_a_symlinked_parent_records_the_long_spelling(tmp_path):
    """The `~/Development` case: short spelling in, long spelling out.

    ``Path.expanduser()`` - what the old code used - would hand back the
    symlink path unchanged, which is how the short spelling got into
    ``sessions.working_dir`` and split one directory into two projects.
    """
    real_parent = tmp_path / "iCloud" / "Sync" / "Development"
    real_parent.mkdir(parents=True)
    link = tmp_path / "Development"
    link.symlink_to(real_parent, target_is_directory=True)

    settings = _FakeSettings(tmp_path)
    verdict = resolve_project_directory(str(link), "Punchlist Test", settings=settings)

    assert verdict.ok is True
    assert verdict.path == str(real_parent / "Punchlist Test")
    assert str(link) + "/" not in verdict.path


def test_the_fallback_projects_root_is_the_long_spelling():
    """The named fallback must never carry the short symlink spelling."""
    assert FALLBACK_PROJECTS_ROOT.startswith("/Users/")
    assert "Library/Mobile Documents/com~apple~CloudDocs" in FALLBACK_PROJECTS_ROOT
    assert not FALLBACK_PROJECTS_ROOT.startswith("~")


# ----------------------------------------------------------- refusals


def test_traversal_out_of_the_roots_is_refused(tmp_path):
    """`..` cannot survive realpath, so it lands outside and is refused."""
    root = tmp_path / "projects"
    root.mkdir()
    settings = _FakeSettings(root)
    outside = str(root / ".." / ".." / ".." / ".." / "etc")
    verdict = resolve_project_directory(outside, "evil", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "outside_allowed_roots"


def test_a_parent_outside_the_roots_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    settings = _FakeSettings(root)
    # Home must not accidentally contain tmp_path, or "outside" is not
    # outside. Point it somewhere unrelated and definitely real.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: root))

    verdict = resolve_project_directory(str(elsewhere), "x", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "outside_allowed_roots"


def test_a_symlink_escaping_the_roots_is_refused(tmp_path, monkeypatch):
    """Realpath is applied BEFORE the comparison, so a bridge out fails."""
    root = tmp_path / "projects"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    bridge = root / "bridge"
    bridge.symlink_to(outside, target_is_directory=True)
    settings = _FakeSettings(root)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: root))

    verdict = resolve_project_directory(str(bridge), "x", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "outside_allowed_roots"


def test_containment_is_component_wise_not_a_string_prefix(tmp_path, monkeypatch):
    """`/x/projectsevil` must not read as living under `/x/projects`."""
    root = tmp_path / "projects"
    root.mkdir()
    lookalike = tmp_path / "projectsevil"
    lookalike.mkdir()
    settings = _FakeSettings(root)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: root))

    verdict = resolve_project_directory(str(lookalike), "x", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "outside_allowed_roots"


def test_a_missing_parent_is_refused(roots):
    root, settings = roots
    verdict = resolve_project_directory(str(root / "nope"), "x", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "parent_missing"


def test_a_parent_that_is_a_file_is_refused(roots):
    root, settings = roots
    afile = root / "afile"
    afile.write_text("x")
    verdict = resolve_project_directory(str(afile), "x", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "parent_not_dir"


def test_an_empty_parent_dir_argument_is_refused(roots):
    _root, settings = roots
    verdict = resolve_project_directory("", "x", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "parent_missing"


# --------------------------------------------------- existing targets


def test_an_existing_empty_directory_is_accepted(roots):
    root, settings = roots
    (root / "Reuse Me").mkdir()
    verdict = resolve_project_directory(str(root), "Reuse Me", settings=settings)
    assert verdict.ok is True


def test_an_existing_non_empty_directory_is_refused(roots):
    root, settings = roots
    target = root / "Taken"
    target.mkdir()
    (target / "file.txt").write_text("hello")
    verdict = resolve_project_directory(str(root), "Taken", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "exists_non_empty"
    assert "Taken" in verdict.message


def test_an_existing_file_at_the_target_is_refused(roots):
    root, settings = roots
    (root / "Taken").write_text("x")
    verdict = resolve_project_directory(str(root), "Taken", settings=settings)
    assert verdict.ok is False
    assert verdict.code == "exists_not_dir"


# ------------------------------------------------------- allowed roots


def test_home_is_an_allowed_root(roots):
    _root, settings = roots
    assert os.path.realpath(str(Path.home())) in allowed_roots(settings)


def test_the_projects_root_is_an_allowed_root(roots):
    root, settings = roots
    assert os.path.realpath(str(root)) in allowed_roots(settings)


def test_extra_roots_widen_the_boundary(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    extra = tmp_path / "work"
    extra.mkdir()
    settings = _FakeSettings(root)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: root))

    refused = resolve_project_directory(str(extra), "x", settings=settings)
    assert refused.ok is False

    allowed = resolve_project_directory(
        str(extra), "x", settings=settings, extra_roots=[str(extra)]
    )
    assert allowed.ok is True


def test_unreadable_settings_fall_back_to_the_named_root(tmp_path):
    """A settings object that raises still yields a usable root, logged."""

    class _Broken:
        def get_working_dir(self):
            raise OSError("no such directory")

    assert os.path.realpath(FALLBACK_PROJECTS_ROOT) in allowed_roots(_Broken())
