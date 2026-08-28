"""Gather the evidence the ladder reasons from. Filesystem and tmux side.

Kept apart from :mod:`src.core.session_import_ladder` so the RULES stay
pure and testable without a filesystem, and so the failure modes of
READING live in one place. Every function here answers with a value or
with None, and None always means the same thing: THE MEASUREMENT COULD
NOT BE TAKEN. It never means "nothing found" - an empty log directory
returns an empty set, an unreadable one returns None, and the ladder
treats those two very differently.

That distinction is the whole reason this module exists as its own file.
``_load_session_metadata`` gets it wrong today: it logs and carries on
with an empty owned set, so "we could not read your ownership record" and
"you own nothing" produce identical behaviour, and the identical
behaviour is "everything you made is external".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence, Set, Tuple

import structlog

from src.core.session_import_ladder import LadderEvidence, LiveSession

logger = structlog.get_logger()

#: Filename shape the app writes for a session it CREATED, and for one it
#: adopted or observed. Both live in LOG_DIRECTORY.
PIPE_PREFIX = "tmux_"
PIPE_EXT_PREFIX = "tmux_ext_"
PIPE_SUFFIX = ".pipe"


def scan_pipe_files(
    log_dir: Optional[Path],
) -> Tuple[Optional[frozenset], Optional[frozenset]]:
    """Read LOG_DIRECTORY and split the pipe files into created and ext.

    Description: THREE OUTCOMES. A readable directory returns two sets,
      either of which may legitimately be empty. An absent or unreadable
      one returns ``(None, None)`` - the ladder then reports tier 3 as
      unevaluated rather than as a miss, because "there is no created
      pipe for this session" and "we could not look for one" are
      different claims and only the first is evidence.
    Inputs: log_dir (Path | None) - the resolved LOG_DIRECTORY.
    Output: tuple[frozenset | None, frozenset | None] - created-pipe
      slugs, then ext-pipe slugs.
    Example: scan_pipe_files(Path('~/Library/Logs/cloude-code'))
    """
    if log_dir is None:
        return (None, None)
    try:
        names = [p.name for p in Path(log_dir).iterdir() if p.is_file()]
    except (OSError, ValueError) as exc:
        logger.warning(
            "pipe_scan_unreadable",
            log_dir=str(log_dir),
            error=str(exc),
            note=(
                "tier 3 of the evidence ladder is UNEVALUATED for every "
                "session on this run, not a miss"
            ),
        )
        return (None, None)

    created: Set[str] = set()
    ext: Set[str] = set()
    for name in names:
        if not name.startswith(PIPE_PREFIX) or not name.endswith(PIPE_SUFFIX):
            continue
        if name.startswith(PIPE_EXT_PREFIX):
            ext.add(name[len(PIPE_EXT_PREFIX):-len(PIPE_SUFFIX)])
        else:
            created.add(name[len(PIPE_PREFIX):-len(PIPE_SUFFIX)])
    return (frozenset(created), frozenset(ext))


def ext_pipe_live_names(
    live_names: Iterable[str], ext_slugs: Optional[frozenset]
) -> frozenset:
    """Resolve ext-pipe SLUGS back onto the live tmux names they describe.

    Description: an ext pipe is named after ``_slugify(tmux_name)``, so
      ``cloude_test pause`` becomes ``tmux_ext_cloude_test_pause.pipe``.
      The ladder compares against live names, so the mapping is done here
      rather than teaching the pure module how the app slugifies.

      THE RESULT IS ADMISSIBLE FOR NOTHING. It sets ``readopted`` on a
      verdict that some other tier already decided, and that is all - see
      the ladder's module docstring for why an ``ext_`` pipe can never be
      evidence of authorship in either direction.
    Inputs: live_names (Iterable[str]). ext_slugs (frozenset | None).
    Output: frozenset[str] - live names that have an ext pipe; empty when
      ext_slugs is None, because an unmeasured history explains nothing.
    """
    if not ext_slugs:
        return frozenset()
    from src.core.tmux_backend import _slugify

    return frozenset(n for n in live_names if _slugify(n) in ext_slugs)


def probe_origin_markers(
    live_names: Sequence[str],
    probe: Optional[Callable[[str], Optional[str]]],
) -> Tuple[Dict[str, str], frozenset]:
    """Read ``CLOUDECODE_ORIGIN`` per session, separating absent from unreadable.

    Description: a probe that returns None means the variable is NOT SET
      on that session, which is a measured absence and a tier-4 miss. A
      probe that RAISES means we could not ask, which is a tier-4
      unevaluated. Collapsing the second into the first would report a
      broken measurement as a clean negative.

      NO PROBE AT ALL returns two empty answers, not a failure set: an
      install that cannot read tmux environments has nothing to measure
      here and every session misses tier 4 on the same honest grounds as
      one whose sessions simply carry no marker.
    Inputs: live_names (Sequence[str]). probe (callable | None) -
      name -> marker value or None.
    Output: tuple[dict[str, str], frozenset[str]] - markers read, then
      the names whose probe could not be run.
    """
    if probe is None:
        return ({}, frozenset())
    markers: Dict[str, str] = {}
    failures: Set[str] = set()
    for name in live_names:
        try:
            value = probe(name)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning(
                "origin_marker_probe_failed", tmux_name=name, error=str(exc)
            )
            failures.add(name)
            continue
        if value is not None:
            markers[name] = str(value)
    return (markers, frozenset(failures))


def gather(
    conn: sqlite3.Connection,
    *,
    live_names: Sequence[str],
    owned_tmux_names: Optional[Iterable[str]],
    log_dir: Optional[Path],
    project_roots: Sequence[str] = (),
    origin_probe: Optional[Callable[[str], Optional[str]]] = None,
) -> LadderEvidence:
    """Assemble one :class:`LadderEvidence` for a whole import run.

    Description: the single place the ladder's inputs are collected, so a
      new tier's evidence has one obvious home and cannot be smuggled in
      at a call site.
    Inputs: conn (sqlite3.Connection) - read for the Stage-A boundary
      only. live_names (Sequence[str]) - every live tmux session name on
      our socket. owned_tmux_names (Iterable[str] | None) - the persisted
      owned set; None means it could NOT BE READ, which is not the same
      as empty and must not be passed as ``set()``. log_dir (Path | None).
      project_roots (Sequence[str]) - tier 6 hint input. origin_probe
      (callable | None) - tier 4 reader.
    Output: LadderEvidence.
    Example: gather(conn, live_names=['cloude_a'], owned_tmux_names=None,
                    log_dir=None)
    """
    from src.core.session_stage_a_boundary import read_boundary

    created_slugs, ext_slugs = scan_pipe_files(log_dir)
    markers, failures = probe_origin_markers(list(live_names), origin_probe)
    return LadderEvidence(
        owned_tmux_names=(
            None if owned_tmux_names is None else frozenset(owned_tmux_names)
        ),
        created_pipe_slugs=created_slugs,
        ext_pipe_names=ext_pipe_live_names(live_names, ext_slugs),
        origin_markers=markers,
        origin_probe_failures=failures,
        stage_a_boundary_epoch=read_boundary(conn),
        project_roots=tuple(project_roots),
    )


def live_sessions_from(rows: Iterable[dict]) -> Tuple[LiveSession, ...]:
    """Turn parsed tmux listing rows into ladder inputs.

    Inputs: rows (Iterable[dict]) - each with ``name``,
      ``tmux_created_epoch`` and optionally ``working_dir``.
    Output: tuple[LiveSession, ...] - rows with no name are dropped,
      because a nameless session cannot be attributed to anyone.
    """
    out = []
    for row in rows:
        name = row.get("name")
        if not name:
            continue
        out.append(
            LiveSession(
                tmux_name=str(name),
                epoch=row.get("tmux_created_epoch"),
                working_dir=row.get("working_dir"),
            )
        )
    return tuple(out)
