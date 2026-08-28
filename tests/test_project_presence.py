"""The four-state presence model, against real errno's and one timeout.

THE TEST THAT MATTERS MOST is test_chmod_000_reports_unreachable_not_missing.
It is the sleeping-external-drive case stated plainly in the design doc:
telling the user his project is gone when the real answer is "I could not
look" is the same defect as telling him nothing is wrong. Every other test
in this file exists to pin down the boundary that test depends on: ENOENT
is the ONLY errno that produces 'missing', every other failure to
determine the answer - EACCES, EPERM, ELOOP, ENOTDIR, or a stat that
timed out - produces 'unreachable' and never gets read as 'missing'.
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_presence_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_presence_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db_models import (
    PROJECT_PRESENCE_MISSING,
    PROJECT_PRESENCE_PRESENT,
    PROJECT_PRESENCE_UNREACHABLE,
)
from src.core.project_presence import check_presence


def _raiser(exc: BaseException):
    """Build a stat_fn that raises the given exception unconditionally.

    Inputs: exc (BaseException) - already constructed (so an OSError can
      carry a specific .errno).
    Output: Callable[[str], NoReturn].
    """

    def _stat_fn(_path: str):
        raise exc

    return _stat_fn


def _oserror(code: int, message: str) -> OSError:
    """Build an OSError carrying a specific errno, the way os.stat would.

    Inputs: code (int) - an errno.E* constant. message (str).
    Output: OSError with .errno set.
    """
    return OSError(code, message)


# --- one test per errno, and the timeout ------------------------------


def test_enoent_is_missing():
    """The only errno that produces 'missing': the parent resolved, the
    entry is positively absent."""
    result = check_presence(
        "/does/not/exist", stat_fn=_raiser(_oserror(errno.ENOENT, "No such file or directory"))
    )
    assert result.presence == PROJECT_PRESENCE_MISSING
    assert "ENOENT" in result.detail


def test_eacces_is_unreachable_not_missing():
    """Permission denied could not tell - never read as 'the folder is gone'."""
    result = check_presence(
        "/root/secret", stat_fn=_raiser(_oserror(errno.EACCES, "Permission denied"))
    )
    assert result.presence == PROJECT_PRESENCE_UNREACHABLE
    assert "EACCES" in result.detail


def test_eperm_is_unreachable():
    result = check_presence(
        "/blocked", stat_fn=_raiser(_oserror(errno.EPERM, "Operation not permitted"))
    )
    assert result.presence == PROJECT_PRESENCE_UNREACHABLE
    assert "EPERM" in result.detail


def test_eloop_is_unreachable():
    """A symlink loop could not tell either - not the same as gone."""
    result = check_presence(
        "/looping", stat_fn=_raiser(_oserror(errno.ELOOP, "Too many levels of symbolic links"))
    )
    assert result.presence == PROJECT_PRESENCE_UNREACHABLE
    assert "ELOOP" in result.detail


def test_enotdir_is_unreachable():
    """A path component that turned out to be a file, not a directory."""
    result = check_presence(
        "/some/file/child", stat_fn=_raiser(_oserror(errno.ENOTDIR, "Not a directory"))
    )
    assert result.presence == PROJECT_PRESENCE_UNREACHABLE
    assert "ENOTDIR" in result.detail


def test_stat_timeout_is_unreachable():
    """A hung network mount reports unreachable, never missing."""
    result = check_presence("/mnt/asleep", stat_fn=_raiser(TimeoutError("no response")))
    assert result.presence == PROJECT_PRESENCE_UNREACHABLE
    assert "TIMEOUT" in result.detail


def test_no_two_of_the_five_collapse_into_each_other():
    """The crux assertion: ENOENT differs from all four unreachable causes,
    and the four unreachable causes each keep a distinct, named detail
    string even though they share one presence value. Never silently
    merged into one undifferentiated 'something is wrong'."""
    cases = {
        "ENOENT": _raiser(_oserror(errno.ENOENT, "gone")),
        "EACCES": _raiser(_oserror(errno.EACCES, "denied")),
        "ELOOP": _raiser(_oserror(errno.ELOOP, "loop")),
        "ENOTDIR": _raiser(_oserror(errno.ENOTDIR, "not a dir")),
        "TIMEOUT": _raiser(TimeoutError("hung")),
    }
    results = {name: check_presence("/x", stat_fn=fn) for name, fn in cases.items()}

    # ENOENT is the one and only 'missing'.
    assert results["ENOENT"].presence == PROJECT_PRESENCE_MISSING
    for name in ("EACCES", "ELOOP", "ENOTDIR", "TIMEOUT"):
        assert results[name].presence == PROJECT_PRESENCE_UNREACHABLE

    # Every one of the five carries its own errno/reason in the detail
    # string - "unreachable" alone would tell the UI nothing about WHAT
    # could not be measured.
    details = {name: r.detail for name, r in results.items()}
    assert len(set(details.values())) == 5, details


def test_unclassified_exception_from_the_probe_is_unreachable_not_present():
    """A probe failure this module was never written to name (anything
    that is not an OSError or TimeoutError) still resolves to
    'unreachable', carrying the exception's type name - never silently
    read as 'present', which would be the worst possible misclassification
    for an exception nobody planned for."""
    result = check_presence("/x", stat_fn=_raiser(ValueError("unexpected")))
    assert result.presence == PROJECT_PRESENCE_UNREACHABLE
    assert result.presence != PROJECT_PRESENCE_PRESENT
    assert "ValueError" in result.detail


def test_present_directory():
    with tempfile.TemporaryDirectory() as d:
        result = check_presence(d)
        assert result.presence == PROJECT_PRESENCE_PRESENT
        assert result.detail is None


def test_root_that_is_a_file_not_a_directory_is_unreachable():
    """An existing path that is not a directory is not 'missing' - it
    demonstrably exists - and not 'present' either, since a project root
    has to be a directory to be usable."""
    with tempfile.NamedTemporaryFile() as f:
        result = check_presence(f.name)
        assert result.presence == PROJECT_PRESENCE_UNREACHABLE
        assert "ENOTDIR" in result.detail


# --- the case that matters most ----------------------------------------


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses directory permission bits; the EACCES this test "
    "depends on never fires when tests run as root",
)
def test_chmod_000_reports_unreachable_not_missing():
    """The sleeping-external-drive case. A real chmod-000 directory with a
    real child path underneath it, probed with the REAL (non-injected)
    stat path - no stat_fn override - so this proves the production
    default classifies a permission wall as 'unreachable', not 'missing'.
    """
    with tempfile.TemporaryDirectory() as parent:
        blocked_dir = Path(parent) / "blocked"
        blocked_dir.mkdir()
        child = blocked_dir / "project"
        child.mkdir()
        os.chmod(blocked_dir, 0o000)
        try:
            result = check_presence(str(child))
        finally:
            # Restore permissions so the TemporaryDirectory cleanup can
            # actually delete the tree.
            os.chmod(blocked_dir, 0o755)

        assert result.presence == PROJECT_PRESENCE_UNREACHABLE, (
            "a project behind a permission wall must never be reported as "
            f"'missing' - got presence={result.presence!r} detail="
            f"{result.detail!r}"
        )
        assert result.presence != PROJECT_PRESENCE_MISSING


def test_unchecked_is_never_produced_by_check_presence():
    """'unchecked' is the DDL default for a never-probed row, not a value
    this probing function itself ever returns - every call to
    check_presence resolves to one of the other three states."""
    with tempfile.TemporaryDirectory() as d:
        assert check_presence(d).presence != "unchecked"
    assert (
        check_presence("/nope", stat_fn=_raiser(_oserror(errno.ENOENT, "x"))).presence
        != "unchecked"
    )
