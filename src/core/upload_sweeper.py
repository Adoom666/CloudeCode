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
are isolated — a bad project path or permission error logs at WARNING
and the loop continues.

Filesystem I/O (``os.walk`` / ``os.unlink`` / ``Path.stat``) is
delegated to ``asyncio.to_thread`` so the event loop stays free for
HTTP and WebSocket handlers.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Iterable

import structlog

logger = structlog.get_logger()


UPLOAD_DIR_NAME = ".cloude_uploads"


class UploadSweeper:
    """Background TTL pruner for ``.cloude_uploads/`` buckets."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        interval_seconds: int,
        project_paths: list[str],
        default_dir: Path,
    ):
        """Configure the sweeper.

        Args:
            ttl_seconds: Files older than this (by mtime) are pruned.
            interval_seconds: Sleep between sweeps in ``run()``. Not
                used by ``sweep_now()``; pass ``0`` for one-shot use.
            project_paths: Configured project base paths to scan. Each
                gets a ``.cloude_uploads/`` lookup beneath it.
            default_dir: Fallback working dir (typically
                ``settings.get_working_dir()``) added to the scan list.
        """
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        self.project_paths = list(project_paths)
        self.default_dir = default_dir

    async def run(self) -> None:
        """Main loop. Sleeps then sweeps until cancelled."""
        logger.info(
            "upload_sweeper_started",
            ttl_seconds=self.ttl_seconds,
            interval_seconds=self.interval_seconds,
            base_paths=len(self.project_paths) + 1,
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
            ``{"files_pruned": int, "bytes_freed": int}``
        """
        started = time.perf_counter()
        cutoff = time.time() - self.ttl_seconds
        bases: Iterable[str] = (*self.project_paths, str(self.default_dir))

        total_files = 0
        total_bytes = 0

        for base in bases:
            try:
                files, freed = await asyncio.to_thread(
                    _prune_base, base, cutoff
                )
                total_files += files
                total_bytes += freed
            except Exception as exc:
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
            elapsed_ms=elapsed_ms,
        )
        return {"files_pruned": total_files, "bytes_freed": total_bytes}


def _prune_base(base: str, cutoff: float) -> tuple[int, int]:
    """Synchronous prune helper. Runs inside ``asyncio.to_thread``.

    Args:
        base: Project / working-dir base path. ``.cloude_uploads`` is
            looked up directly underneath.
        cutoff: Epoch seconds; files with ``st_mtime < cutoff`` go.

    Returns:
        ``(files_pruned, bytes_freed)``.
    """
    bucket = Path(base).expanduser().resolve() / UPLOAD_DIR_NAME
    if not bucket.exists() or not bucket.is_dir():
        return (0, 0)

    files_pruned = 0
    bytes_freed = 0

    try:
        entries = list(bucket.iterdir())
    except OSError:
        return (0, 0)

    for entry in entries:
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
            if stat.st_mtime < cutoff:
                size = stat.st_size
                entry.unlink()
                files_pruned += 1
                bytes_freed += size
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                "upload_sweep_file_error",
                path=str(entry),
                error=str(exc),
            )

    try:
        if not any(bucket.iterdir()):
            bucket.rmdir()
    except OSError:
        pass

    return (files_pruned, bytes_freed)
