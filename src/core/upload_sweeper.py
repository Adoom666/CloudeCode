"""Periodic mtime-based TTL pruner for browser-paste image uploads.

The image-paste feature drops files into ``<working_dir>/.cloude_uploads/``.
Three layers cooperate to keep that directory from accumulating bytes
indefinitely:

1. ``SessionManager.destroy_session()`` rmtrees the session's bucket on
   the explicit-kill path. Fastest cleanup, runs synchronously.
2. ``SessionManager._sweep_orphan_uploads()`` runs once on lifespan
   startup. Catches buckets left behind by a force-killed previous run
   where layer 1 never executed.
3. This module's ``UploadSweeper`` runs as an asyncio background task
   for the lifetime of the FastAPI app. Wakes every
   ``interval_seconds``, prunes any file whose mtime is older than
   ``ttl_seconds``, and removes the bucket directory if it ends up
   empty. Safety net for long-running servers.

Layers 2 and 3 share their pruning core via ``sweep_now()`` so the
intent stays identical.

Cancellation discipline: ``run()`` catches ``asyncio.CancelledError``
explicitly to log shutdown intent, then re-raises so the FastAPI
lifespan's ``await task`` resolves cleanly. Single-iteration failures
are isolated - a bad project path or permission error logs at WARNING
and the loop continues.

Filesystem I/O (``os.lstat`` / ``os.unlink`` / ``os.scandir``) is
delegated to ``asyncio.to_thread`` so the event loop stays free for
HTTP and WebSocket handlers.

WHY THIS MODULE CARRIES A GUARD AND THE OTHER WRITE SITES DO NOT
----------------------------------------------------------------
This is the only place in ``src/`` that DELETES from a path the
operator supplied. Every other unisolated write site can at worst leave
a stray file behind; this one runs ``unlink`` over every configured
project root on a timer. During a test run that means the developer's
real work, so a wrong answer here is not recoverable by re-running.

A WRITE GUARD IS NOT ENOUGH FOR A DELETE
----------------------------------------
``test_write_guard.assert_test_write_allowed`` answers one question:
"is this destination inside a temp root". That is necessary and it is
not sufficient, because it says nothing about WHAT is being destroyed.
Three independent questions have to be answered before anything is
unlinked, and any one of them coming back unknown is a refusal:

* **Containment** - is the destination somewhere this run is allowed to
  write at all? (the shared guard, under test only)
* **Shape** - is the thing about to be pruned a real ``.cloude_uploads``
  directory sitting directly beneath the base, rather than a symlink
  pointing somewhere else, a regular file, or a path whose leaf is not
  the name we expect? A symlinked bucket is the one construction that
  lets ``iterdir``/``unlink`` reach outside the leaf entirely.
* **Provenance** - do we actually know which projects are configured? A
  sweeper that cannot answer that must delete NOTHING. The tempting
  reading of an unreadable project list is "there are no projects, so
  just do the default dir"; the honest reading is "I do not know what I
  would be touching". ``project_paths=None`` is that state and it
  disables the whole sweep, default dir included.

``sweep_verdict()`` renders all three as a pure query - it resolves and
stats, it never unlinks - so a caller (or a test) can ask about a path
it must not touch. ``_prune_base`` is the only function that deletes,
and it deletes only what ``sweep_verdict`` cleared.
"""

import asyncio
import os
import stat as stat_module
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import structlog

from src.core.test_write_guard import (
    OutsideTempWriteError,
    assert_test_write_allowed,
)

logger = structlog.get_logger()


UPLOAD_DIR_NAME = ".cloude_uploads"


class SweepOutcome(Enum):
    """The three outcomes of asking whether a base may be swept.

    ``SKIP`` is not a quiet ``REFUSED``: it means the question was
    answered and there is genuinely nothing there. ``REFUSED`` means the
    sweep could not be shown safe, which includes every "I could not
    tell" case.
    """

    SWEEP = "sweep"
    SKIP = "skip"
    REFUSED = "refused"


@dataclass(frozen=True)
class SweepVerdict:
    """Result of :func:`sweep_verdict`.

    Attributes:
        outcome: Which of the three states applies.
        bucket: The resolved directory to prune. Set ONLY for
            ``SweepOutcome.SWEEP``; ``None`` otherwise, so a caller
            cannot accidentally act on a refused path.
        reason: Human-readable explanation, always populated for
            ``SKIP`` and ``REFUSED``.
    """

    outcome: SweepOutcome
    bucket: Path | None
    reason: str


def datastore_project_paths(view) -> list[str] | None:
    """Read the project base paths out of a ProjectsView, or refuse.

    The provenance half of the sweeper's safety, now sourced from the
    datastore because as of v1.0.4 cloude.db's ``projects`` table is the
    only place projects live. Every failure to enumerate returns
    ``None``, which the sweeper treats as "delete nothing at all",
    rather than an empty list, which it would treat as "there are
    legitimately no projects, carry on with the default dir". Those two
    are very different statements and only one of them is evidence.

    A view whose ``read_only`` flag is set could not read the datastore
    at all, so its empty ``projects`` list is an absence of evidence and
    is reported as ``None``.

    Inputs:
        view: A ``ProjectsView`` (or anything exposing ``read_only`` and
            ``projects``) from ``project_authority.resolve_projects``.
    Outputs:
        list[str] - one base path per project, possibly empty when the
        datastore genuinely holds none.
        None - the list could not be determined.
    Example:
        >>> datastore_project_paths(resolve_projects(state_dir))
        ['/Users/me/Development/thing']
    """
    try:
        if view.read_only:
            logger.warning(
                "project_list_unreadable",
                error="datastore view is read-only, projects could not be read",
            )
            return None
        projects = view.projects
    except AttributeError as exc:
        logger.warning("project_list_unreadable", error=str(exc))
        return None
    if projects is None:
        logger.warning("project_list_unreadable", error="projects is None")
        return None

    paths: list[str] = []
    try:
        for project in projects:
            path = project["path"]
            if not isinstance(path, str) or not path.strip():
                # One unusable entry means the list as a whole is not
                # trustworthy. Silently dropping it would sweep a subset
                # while reporting a complete run.
                logger.warning(
                    "project_list_unreadable",
                    error=f"project entry has an unusable path: {path!r}",
                )
                return None
            paths.append(path)
    except (AttributeError, TypeError, KeyError) as exc:
        logger.warning("project_list_unreadable", error=str(exc))
        return None
    return paths


def sweep_verdict(base: str | os.PathLike | None) -> SweepVerdict:
    """Decide whether ``<base>/.cloude_uploads`` may be pruned. PURE QUERY.

    Performs no mutation of any kind - it resolves paths and stats them.
    That is deliberate and load-bearing: it is what makes it safe to ask
    this question about a path that must never be touched, including
    from a test.

    Inputs:
        base: Configured project root, or the default working dir. May
            be ``None`` or blank, which is treated as undetermined.
    Outputs:
        SweepVerdict - see :class:`SweepVerdict`.
    Example:
        >>> sweep_verdict("/etc").outcome  # during a test run
        <SweepOutcome.REFUSED: 'refused'>
    """
    # --- provenance of the base itself ---------------------------------
    if base is None:
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            "base path is None; an undetermined base is refused, never "
            "resolved against the process working directory",
        )
    raw = os.fspath(base)
    if not isinstance(raw, str) or not raw.strip():
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            "base path is empty or blank; Path('').resolve() would "
            "silently become the process working directory",
        )

    try:
        resolved_base = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            f"base path could not be resolved ({exc})",
        )

    if resolved_base == Path(resolved_base.anchor):
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            f"base path resolves to the filesystem root {resolved_base}; "
            "that is a misconfiguration, not a project",
        )

    bucket = resolved_base / UPLOAD_DIR_NAME

    # --- containment (inert outside a test run) ------------------------
    try:
        assert_test_write_allowed(bucket)
    except OutsideTempWriteError as exc:
        return SweepVerdict(SweepOutcome.REFUSED, None, str(exc))

    # --- shape ---------------------------------------------------------
    try:
        link_stat = os.lstat(bucket)
    except FileNotFoundError:
        return SweepVerdict(
            SweepOutcome.SKIP, None, "no uploads bucket beneath this base"
        )
    except OSError as exc:
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            f"could not stat {bucket} ({exc}); an unreadable target is "
            "refused, not assumed empty",
        )

    if stat_module.S_ISLNK(link_stat.st_mode):
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            f"{bucket} is a symlink. iterdir() and unlink() both follow "
            "it, so pruning would delete files outside the uploads leaf.",
        )
    if not stat_module.S_ISDIR(link_stat.st_mode):
        return SweepVerdict(
            SweepOutcome.SKIP, None,
            f"{bucket} exists but is not a directory; left untouched",
        )
    # NOT a tautology, though it reads like one. ``bucket`` is built as
    # ``resolved_base / UPLOAD_DIR_NAME``, so asserting its own name or
    # parent would be a check that cannot fail - the worst kind. These
    # two can:
    #
    #   * a configured base that itself ends in .cloude_uploads produces
    #     a nested bucket, which is a config error, not a sweep target;
    #   * realpath can still differ from the constructed path across a
    #     mount point or an APFS firmlink even with the leaf proven not
    #     to be a symlink.
    if resolved_base.name == UPLOAD_DIR_NAME:
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            f"base {resolved_base} is itself a {UPLOAD_DIR_NAME} "
            "directory; a nested uploads bucket is a misconfiguration",
        )
    try:
        real_bucket = Path(os.path.realpath(bucket))
    except OSError as exc:
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            f"could not resolve {bucket} ({exc})",
        )
    if real_bucket != bucket:
        return SweepVerdict(
            SweepOutcome.REFUSED, None,
            f"{bucket} really lives at {real_bucket}; the sweep target "
            "must be the path it claims to be",
        )

    return SweepVerdict(SweepOutcome.SWEEP, bucket, "")


class UploadSweeper:
    """Background TTL pruner for ``.cloude_uploads/`` buckets."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        interval_seconds: int,
        project_paths: list[str] | None,
        default_dir: Path,
    ):
        """Configure the sweeper.

        Args:
            ttl_seconds: Files older than this (by mtime) are pruned.
            interval_seconds: Sleep between sweeps in ``run()``. Not
                used by ``sweep_now()``; pass ``0`` for one-shot use.
            project_paths: Configured project base paths to scan. Each
                gets a ``.cloude_uploads/`` lookup beneath it. Pass
                ``None`` - NOT ``[]`` - when the project list could not
                be determined; that disables the entire sweep, default
                dir included. ``[]`` means "there are genuinely no
                projects" and still sweeps the default dir.
            default_dir: Fallback working dir (typically
                ``settings.get_working_dir()``) added to the scan list.
        """
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        self.project_paths = None if project_paths is None else list(project_paths)
        self.default_dir = default_dir

    async def run(self) -> None:
        """Main loop. Sleeps then sweeps until cancelled."""
        logger.info(
            "upload_sweeper_started",
            ttl_seconds=self.ttl_seconds,
            interval_seconds=self.interval_seconds,
            # None is the CANNOT DETERMINE state, not zero bases. Saying
            # so here rather than counting it keeps the startup line
            # readable as "this sweeper will delete nothing" instead of
            # crashing on len(None), which is what it used to do.
            base_paths=(
                "undetermined"
                if self.project_paths is None
                else len(self.project_paths) + 1
            ),
        )

        while True:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self._sweep_once()
            except asyncio.CancelledError:
                logger.info("upload_sweeper_stopping")
                raise
            except Exception as exc:
                logger.warning(
                    "upload_sweep_iteration_failed",
                    error=str(exc),
                )

    async def sweep_now(self) -> dict:
        """Run one sweep immediately. Returns aggregate stats.

        Public single-pass sweep used by the startup orphan hook and
        unit tests. Same prune logic as the periodic loop.
        """
        return await self._sweep_once()

    async def _sweep_once(self) -> dict:
        """Iterate every base path and prune expired files.

        Returns:
            ``{"files_pruned": int, "bytes_freed": int,
            "bases_refused": int, "project_list_determined": bool}``.
            ``project_list_determined`` is False when the sweep was
            skipped wholesale because the configured project list was
            unknown - that is a CANNOT DETERMINE, not a clean zero.
        """
        started = time.perf_counter()

        if self.project_paths is None:
            # Provenance failure. Deleting nothing is the only honest
            # response: we cannot enumerate what we would be touching,
            # and "sweep just the default dir" would be acting on a
            # guess. Logged at WARNING so it cannot pass for a quiet
            # healthy sweep.
            logger.warning(
                "upload_sweep_skipped_project_list_undetermined",
                default_dir=str(self.default_dir),
            )
            return {
                "files_pruned": 0,
                "bytes_freed": 0,
                "bases_refused": 0,
                "project_list_determined": False,
            }

        cutoff = time.time() - self.ttl_seconds
        bases = (*self.project_paths, str(self.default_dir))

        total_files = 0
        total_bytes = 0
        refused = 0

        for base in bases:
            try:
                files, freed, was_refused = await asyncio.to_thread(
                    _prune_base, base, cutoff
                )
                total_files += files
                total_bytes += freed
                refused += int(was_refused)
            except Exception as exc:
                # An unexpected failure is a base we could not evaluate,
                # so it counts as refused rather than vanishing into a
                # warning nobody totals up.
                refused += 1
                logger.warning(
                    "upload_sweep_partial_error",
                    base_path=base,
                    error=str(exc),
                )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "upload_sweep_complete",
            files_pruned=total_files,
            bytes_freed=total_bytes,
            bases_refused=refused,
            elapsed_ms=elapsed_ms,
        )
        return {
            "files_pruned": total_files,
            "bytes_freed": total_bytes,
            "bases_refused": refused,
            "project_list_determined": True,
        }


def _prune_base(base: str, cutoff: float) -> tuple[int, int, bool]:
    """Synchronous prune helper. Runs inside ``asyncio.to_thread``.

    The ONLY function in this module that deletes anything, and it
    deletes only what :func:`sweep_verdict` cleared. It never recurses:
    a directory entry inside the bucket is skipped, not walked, so the
    blast radius is exactly one level of one ``.cloude_uploads`` leaf.

    Every entry is inspected with ``lstat``, never ``stat``. Following a
    symlink here would mean judging a file's age by its TARGET's mtime,
    and ``unlink`` would then remove a link whose lifetime has nothing
    to do with the age we measured.

    Inputs:
        base: Project / working-dir base path.
        cutoff: Epoch seconds; entries with ``st_mtime < cutoff`` go.
    Outputs:
        ``(files_pruned, bytes_freed, refused)``. ``refused`` is True
        when the base could not be shown safe and nothing was touched.
    Example:
        >>> _prune_base("/tmp/proj", time.time() - 3600)
        (2, 4096, False)
    """
    verdict = sweep_verdict(base)

    if verdict.outcome is SweepOutcome.REFUSED:
        logger.warning(
            "upload_sweep_base_refused",
            base_path=str(base),
            reason=verdict.reason,
        )
        return (0, 0, True)

    if verdict.outcome is SweepOutcome.SKIP:
        return (0, 0, False)

    bucket = verdict.bucket
    assert bucket is not None  # SWEEP always carries one; keeps type checkers honest

    files_pruned = 0
    bytes_freed = 0

    try:
        with os.scandir(bucket) as entries:
            candidates = list(entries)
    except OSError as exc:
        logger.warning(
            "upload_sweep_base_refused",
            base_path=str(base),
            reason=f"bucket became unreadable ({exc})",
        )
        return (0, 0, True)

    for entry in candidates:
        try:
            entry_stat = entry.stat(follow_symlinks=False)
            mode = entry_stat.st_mode
            if not (
                stat_module.S_ISREG(mode) or stat_module.S_ISLNK(mode)
            ):
                # Directories and anything exotic are left alone. The
                # pruner is one level deep on purpose.
                continue
            if entry_stat.st_mtime >= cutoff:
                continue
            size = entry_stat.st_size
            os.unlink(entry.path)
            files_pruned += 1
            bytes_freed += size
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                "upload_sweep_file_error",
                path=entry.path,
                error=str(exc),
            )

    try:
        if not any(os.scandir(bucket)):
            bucket.rmdir()
    except OSError:
        pass

    return (files_pruned, bytes_freed, False)
