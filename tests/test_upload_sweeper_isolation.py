"""Containment tests for the upload sweeper - the only DESTRUCTIVE write site.

WHY A SEPARATE FILE FROM ``test_upload_sweeper.py``
--------------------------------------------------
That file tests what the sweeper DOES. This one tests what it must
REFUSE to do. They fail for different reasons and are read by different
people: a break here means the blast radius of a delete has grown, not
that TTL arithmetic drifted.

HOW EVERY DESTRUCTIVE ASSERTION HERE IS AIMED
---------------------------------------------
Two rules, both learned the hard way:

1. **Assert on the filesystem, never on the call.** A test proving
   ``unlink`` was invoked with the right argument proves nothing about
   what survived. Every check below seeds a real tree, runs the real
   sweep, and then asks the filesystem what is still there.

2. **A destructive test must never be able to cause the incident it
   tests for.** If a test called the real pruner with a real project
   path, then neutering the guard would make the test PERFORM the damage
   it exists to prevent - the failing assertion runs after the delete.
   So every call that can delete is aimed at a disposable tree under
   ``tmp_path``, and the verdict for a real, non-disposable path is read
   through ``sweep_verdict()``, which is a pure query that mutates
   nothing.

   The "outside a temp root" case is arranged by narrowing what the
   guard CONSIDERS a temp root (monkeypatching ``temp_roots``) rather
   than by widening what the test writes to. The guard logic under test
   is the real one; only the disposable tree is at stake.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ---- env bootstrap (matches sibling tests) -----------------------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_swi_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_swi_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core import test_write_guard  # noqa: E402
from src.core.upload_sweeper import (  # noqa: E402
    UPLOAD_DIR_NAME,
    SweepOutcome,
    UploadSweeper,
    datastore_project_paths,
    sweep_verdict,
)


def _seed_old_file(base: Path, name: str = "stale.png") -> Path:
    """Create ``<base>/.cloude_uploads/<name>`` back-dated well past any TTL.

    Inputs:
        base: Disposable directory that plays the part of a project root.
        name: Filename to create inside the bucket.
    Outputs:
        Path - the seeded file, guaranteed to exist on return.
    """
    bucket = base / UPLOAD_DIR_NAME
    bucket.mkdir(parents=True, exist_ok=True)
    target = bucket / name
    target.write_bytes(b"payload-that-must-survive")
    old = time.time() - 86_400
    os.utime(target, (old, old))
    assert target.exists()
    return target


def _sweeper(bases, default_dir: Path, ttl_seconds: int = 60) -> UploadSweeper:
    """Build a one-shot sweeper over disposable paths only."""
    return UploadSweeper(
        ttl_seconds=ttl_seconds,
        interval_seconds=0,
        project_paths=bases,
        default_dir=default_dir,
    )


# ---- the decisive test -------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_outside_every_temp_root_refuses_and_deletes_nothing(
    tmp_path, monkeypatch
):
    """A test-run sweep aimed outside every temp root must delete nothing.

    This is the defect in one assertion. Against the unguarded sweeper
    the stale file is removed and the sweep reports success; with the
    guard the base is refused, the bytes survive, and the refusal is
    counted rather than swallowed.

    The tree is disposable: if the guard were neutered the only casualty
    would be this ``tmp_path``.
    """
    allowed_root = tmp_path / "the-only-temp-root"
    allowed_root.mkdir()
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [allowed_root])

    project = tmp_path / "pretend-real-project"
    project.mkdir()
    survivor = _seed_old_file(project)

    empty_default = tmp_path / "default-dir"
    empty_default.mkdir()

    result = await _sweeper([str(project)], empty_default).sweep_now()

    assert survivor.exists(), (
        "The sweeper deleted a file under a base outside every temp root. "
        "That is the defect this test exists to catch."
    )
    assert survivor.read_bytes() == b"payload-that-must-survive"
    assert (project / UPLOAD_DIR_NAME).is_dir()
    assert result["files_pruned"] == 0
    assert result["bases_refused"] >= 1


@pytest.mark.asyncio
async def test_guard_does_not_neuter_a_legitimate_sweep(tmp_path, monkeypatch):
    """Positive control: inside the permitted root the prune still happens.

    Without this, every assertion above would also pass if the guard
    refused unconditionally - a check that cannot distinguish is not a
    check.
    """
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [tmp_path])

    project = tmp_path / "legit"
    project.mkdir()
    doomed = _seed_old_file(project)

    empty_default = tmp_path / "default-dir"
    empty_default.mkdir()

    result = await _sweeper([str(project)], empty_default).sweep_now()

    assert not doomed.exists()
    assert result["files_pruned"] == 1
    assert result["bases_refused"] == 0


# ---- the real-path verdict, read without deleting ----------------------


@pytest.mark.parametrize(
    "real_path",
    [
        str(Path.home()),
        str(Path.home() / "Development"),
        str(ROOT),
    ],
)
def test_real_non_disposable_paths_are_refused_by_query_only(real_path):
    """A real directory outside every temp root gets a REFUSED verdict.

    ``sweep_verdict`` is a pure query - it stats and resolves, it never
    unlinks. That is what makes it safe to point at the developer's own
    home directory. Nothing in this test can delete anything, by
    construction, no matter how the guard behaves.
    """
    verdict = sweep_verdict(real_path)
    assert verdict.outcome is SweepOutcome.REFUSED, (
        f"{real_path} was not refused during a test run: {verdict.reason}"
    )
    assert verdict.bucket is None


# ---- provenance: an undetermined project list sweeps NOTHING -----------


@pytest.mark.asyncio
async def test_undetermined_project_list_sweeps_nothing(tmp_path, monkeypatch):
    """``project_paths=None`` means "I could not tell", so nothing is swept.

    The dangerous reading of an unreadable config is "there are no
    projects, so just do the default dir". The safe reading is "I do not
    know what I would be touching". The default dir is deliberately NOT
    swept either.
    """
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [tmp_path])

    default_dir = tmp_path / "default-dir"
    default_dir.mkdir()
    survivor = _seed_old_file(default_dir)

    result = await _sweeper(None, default_dir).sweep_now()

    assert survivor.exists(), (
        "The sweeper pruned the default dir despite not knowing which "
        "projects were configured."
    )
    assert result["files_pruned"] == 0
    assert result["project_list_determined"] is False


# ---- shape and containment --------------------------------------------


@pytest.mark.asyncio
async def test_symlinked_bucket_is_refused_and_its_target_survives(
    tmp_path, monkeypatch
):
    """A ``.cloude_uploads`` that is a SYMLINK must not be followed.

    ``Path.iterdir`` and ``Path.unlink`` both follow a symlinked
    directory, so a symlinked bucket is the one way the pruner can
    delete files that are not inside a ``.cloude_uploads`` leaf at all.
    Both trees here are disposable.
    """
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [tmp_path])

    elsewhere = tmp_path / "somebody-elses-data"
    elsewhere.mkdir()
    survivor = elsewhere / "important.txt"
    survivor.write_bytes(b"not an upload")
    old = time.time() - 86_400
    os.utime(survivor, (old, old))

    project = tmp_path / "project"
    project.mkdir()
    (project / UPLOAD_DIR_NAME).symlink_to(elsewhere, target_is_directory=True)

    result = await _sweeper([str(project)], tmp_path / "nope").sweep_now()

    assert survivor.exists(), "A symlinked bucket was followed and its target pruned."
    assert result["files_pruned"] == 0
    assert result["bases_refused"] >= 1


@pytest.mark.parametrize("bad_base", ["", "   ", None])
def test_empty_or_missing_base_is_refused(bad_base):
    """An empty base resolves to the process CWD, which is nobody's project."""
    verdict = sweep_verdict(bad_base)
    assert verdict.outcome is SweepOutcome.REFUSED
    assert verdict.bucket is None


def test_filesystem_root_as_base_is_refused():
    """``/`` as a project root is a misconfiguration, not a sweep target."""
    verdict = sweep_verdict("/")
    assert verdict.outcome is SweepOutcome.REFUSED


@pytest.mark.asyncio
async def test_bucket_that_is_a_regular_file_is_left_alone(tmp_path, monkeypatch):
    """When the leaf is not a directory the sweeper skips it, never unlinks it."""
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [tmp_path])

    project = tmp_path / "project"
    project.mkdir()
    impostor = project / UPLOAD_DIR_NAME
    impostor.write_bytes(b"i am a file, not a bucket")
    old = time.time() - 86_400
    os.utime(impostor, (old, old))

    await _sweeper([str(project)], tmp_path / "nope").sweep_now()

    assert impostor.is_file()
    assert impostor.read_bytes() == b"i am a file, not a bucket"


@pytest.mark.asyncio
async def test_nested_directory_inside_bucket_is_not_recursed_into(
    tmp_path, monkeypatch
):
    """The pruner is one level deep by design. Prove it stays that way."""
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [tmp_path])

    project = tmp_path / "project"
    project.mkdir()
    _seed_old_file(project)
    nested = project / UPLOAD_DIR_NAME / "subdir"
    nested.mkdir()
    deep = nested / "deep.txt"
    deep.write_bytes(b"deep")
    old = time.time() - 86_400
    os.utime(deep, (old, old))

    await _sweeper([str(project)], tmp_path / "nope").sweep_now()

    assert deep.exists(), "The pruner recursed below the bucket."


# ---- production inertness ---------------------------------------------


def test_guard_is_inert_when_not_under_test(tmp_path, monkeypatch):
    """Outside a test run the temp-root restriction does not apply.

    Checked through the pure query and against a disposable path, so
    this cannot delete anything even though it simulates production.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(test_write_guard.TEST_MODE_ENV_VAR, raising=False)
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [tmp_path / "nowhere"])

    project = tmp_path / "prod-project"
    (project / UPLOAD_DIR_NAME).mkdir(parents=True)

    verdict = sweep_verdict(str(project))
    assert verdict.outcome is SweepOutcome.SWEEP
    assert verdict.bucket == (project / UPLOAD_DIR_NAME).resolve()


# ---- provenance at the source: reading the datastore project list -----


class _FakeView:
    """Stands in for project_authority.ProjectsView.

    read_only mirrors the real property: True whenever the datastore
    could not be read, which makes an empty projects list an absence of
    evidence rather than a statement that there are no projects.
    """

    def __init__(self, projects, read_only=False):
        self.projects = projects
        self.read_only = read_only


def test_datastore_project_paths_reads_a_good_list():
    """The ordinary case still produces a plain list of strings."""
    view = _FakeView([{"path": "/a"}, {"path": "/b"}])
    assert datastore_project_paths(view) == ["/a", "/b"]


def test_datastore_project_paths_distinguishes_empty_from_unknown():
    """No projects is a fact. It must not be confused with not knowing."""
    assert datastore_project_paths(_FakeView([])) == []
    assert datastore_project_paths(_FakeView(None)) is None


def test_datastore_project_paths_refuses_an_unreadable_datastore():
    """A read-only view read nothing, so its empty list is not evidence."""
    assert datastore_project_paths(_FakeView([], read_only=True)) is None
    assert (
        datastore_project_paths(_FakeView([{"path": "/a"}], read_only=True)) is None
    )


@pytest.mark.parametrize(
    "projects",
    [
        [{"path": "/a"}, {"path": ""}],
        [{"path": "/a"}, {"path": None}],
        [{"path": "/a"}, {}],
        [{"path": "/a"}, object()],
    ],
)
def test_datastore_project_paths_refuses_a_partial_list(projects):
    """One unusable entry poisons the whole list.

    Dropping the bad entry and returning the rest would sweep a subset
    while reporting a complete run - a partial answer wearing a
    complete answer's clothes.
    """
    assert datastore_project_paths(_FakeView(projects)) is None


# ---- the OTHER delete site: destroy_session's recursive rmtree ---------


async def _destroy_with_uploads_dir(tmp_path, working_dir: Path):
    """Drive ``SessionManager.destroy_session`` for a session in working_dir.

    Inputs:
        tmp_path: pytest tmp dir, used for the metadata redirect.
        working_dir: Disposable directory holding a ``.cloude_uploads``.
    Outputs:
        None. The session is destroyed; the caller inspects the tree.
    """
    from unittest.mock import MagicMock

    import src.core.session_manager as sm_mod
    from src.core.session_manager import SessionManager
    from src.models import Session, SessionStatus

    object.__setattr__(
        sm_mod.settings,
        "get_session_metadata_path",
        lambda: tmp_path / "session_metadata.json",
    )

    manager = SessionManager()
    backend = MagicMock()

    async def _stop():
        return None

    backend.stop = _stop
    backend.is_alive = MagicMock(return_value=True)
    backend.tmux_session = "cloude_iso01"
    manager._register_session(
        Session(
            id="ses_iso01",
            working_dir=str(working_dir),
            status=SessionStatus.RUNNING,
        ),
        backend,
    )
    await manager.destroy_session()


@pytest.mark.asyncio
async def test_destroy_session_rmtree_refuses_outside_every_temp_root(
    tmp_path, monkeypatch
):
    """Layer 1 is an rmtree, and it gets the same verdict as the sweeper.

    ``destroy_session`` recursively deletes ``<working_dir>/.cloude_uploads``
    with ``ignore_errors=True``, which used to make any refusal invisible.
    The tree here is disposable.
    """
    allowed_root = tmp_path / "the-only-temp-root"
    allowed_root.mkdir()
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [allowed_root])

    working_dir = tmp_path / "pretend-real-project"
    working_dir.mkdir()
    survivor = _seed_old_file(working_dir, "keep.png")

    await _destroy_with_uploads_dir(tmp_path, working_dir)

    assert survivor.exists(), (
        "destroy_session rmtree'd an uploads dir outside every temp root."
    )


@pytest.mark.asyncio
async def test_destroy_session_still_cleans_a_permitted_uploads_dir(
    tmp_path, monkeypatch
):
    """Positive control for the rmtree site: the normal path still cleans."""
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [tmp_path])

    working_dir = tmp_path / "legit"
    working_dir.mkdir()
    doomed = _seed_old_file(working_dir, "gone.png")

    await _destroy_with_uploads_dir(tmp_path, working_dir)

    assert not doomed.exists()
    assert not (working_dir / UPLOAD_DIR_NAME).exists()


def test_base_that_is_itself_an_uploads_dir_is_refused(tmp_path, monkeypatch):
    """A nested ``.cloude_uploads/.cloude_uploads`` is a config error."""
    monkeypatch.setattr(test_write_guard, "temp_roots", lambda: [tmp_path])
    nested = tmp_path / UPLOAD_DIR_NAME
    (nested / UPLOAD_DIR_NAME).mkdir(parents=True)
    verdict = sweep_verdict(str(nested))
    assert verdict.outcome is SweepOutcome.REFUSED


@pytest.mark.asyncio
async def test_run_starts_when_the_project_list_is_undetermined(tmp_path):
    """The startup log must survive project_paths=None.

    The run loop opened by logging ``len(self.project_paths) + 1``, which
    raises TypeError on the very CANNOT DETERMINE state the rest of the
    class is built to handle. Startup then failed at shutdown teardown
    rather than at the sweep, which pointed nowhere near the cause.
    """
    sweeper = UploadSweeper(
        ttl_seconds=1,
        interval_seconds=3600,
        project_paths=None,
        default_dir=str(tmp_path),
    )
    task = asyncio.create_task(sweeper.run())
    await asyncio.sleep(0)
    assert not task.done(), "run() died before its first sleep"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
