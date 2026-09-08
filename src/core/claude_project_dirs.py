"""Which ``~/.claude/projects`` directories can hold ONE directory's transcripts.

THE PROBLEM, AND IT HAS ALREADY COST THIS PROJECT TWO JUNK ROWS. Claude
Code derives its project-directory name from the LITERAL cwd string it
was started in, not from the resolved path. ``~/Development`` on this
machine is a symlink into iCloud::

    /Users/jsugamele/Development
      -> /Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs/Sync/Development

so the SAME directory produces TWO different project directories
depending on which spelling the user happened to type. Both exist on the
live machine right now - measured 2026-09-08, three pairs
(``Assistants/BHPP``, ``Assistants/Hirschfeld``,
``Assistants/Infrastructure``). That is also what manufactured the
retired ``Mac (old path)`` and ``Hirschfeld (old path)`` project rows.

A reader that slugifies ONE spelling therefore misses transcripts that
exist, and - far worse for anything that then writes - a reader that
picks a directory without checking WHICH real directory it denotes can
attach a conversation belonging to a different row.

TWO INDEPENDENT SIGNALS, AND NEITHER IS TRUSTED ALONE.

  FORWARD, by name.  Slugify every spelling of the directory and keep
    the project directories that exist. See :func:`path_spellings`.
  BACKWARD, by content.  A transcript record carries the ``cwd`` Claude
    was actually in. Canonicalising THAT and comparing is a second route
    to the same answer, and it is the only one that survives a session
    whose cwd moved after startup.

Measured against the live corpus (919 transcripts carrying a ``cwd``, 86
project directories): the forward rule alone explains 908; the 11
residuals are all cwd-moved-after-startup, which only the backward rule
catches. Callers that must not miss a transcript use both and take the
union; callers that must not attach the WRONG one intersect the answer
with independent evidence before writing.

NOTHING HERE WRITES, PROBES A PROCESS, OR RAISES. Every filesystem call
is wrapped: a permissions error or a vanished directory degrades to
"this spelling contributes nothing", never to an exception, because the
callers run inside an adopt path and a live hook request.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Sequence

import structlog

from src.core.claude_transcript_correlate import (
    default_projects_dir,
    slugify_project_dir,
)

logger = structlog.get_logger()

#: How deep under ``$HOME`` to look for the symlinks that create a second
#: spelling. One level is not a guess: a path alias a human types is a
#: top-level shortcut (``~/Development``, ``~/Code``), and widening this
#: turns a bounded ~80-entry listdir into a filesystem walk on the
#: critical path of an adopt. A deeper alias simply yields one fewer
#: spelling, which costs recall and can never cost precision.
HOME_ALIAS_SCAN_DEPTH = 1


def _real(path: str) -> Optional[str]:
    """Resolve a path to its canonical spelling, or None if unresolvable.

    Description: ``os.path.realpath`` does not raise for a missing path,
      but it can raise ``OSError`` on a permission failure or a symlink
      loop, and this module's contract is that it never raises.
    Inputs: path (str) - any path string.
    Output: str | None - the canonical path, or None.
    Example: _real('/Users/x/Development')  # '/Users/x/Library/.../Development'
    """
    if not path:
        return None
    try:
        return os.path.realpath(path)
    except OSError as exc:
        logger.debug("claude_project_dir_realpath_failed", path=path, error=str(exc))
        return None


def _home_dir_aliases(home: Optional[Path] = None) -> List[tuple]:
    """Every top-level symlink under ``$HOME`` that points at a directory.

    Description: the raw material for reconstructing the spelling a user
      typed. Returns ``(link_path, target_real_path)`` pairs so a caller
      can rewrite a canonical path back into its aliased form. A home
      directory that cannot be listed yields an empty list - no aliases
      is a correct, safe answer, and it degrades recall only.
    Inputs: home (Path | None) - override for tests; defaults to
      ``Path.home()``, read at call time so a test's ``$HOME`` override
      taken after import is still seen.
    Output: list[tuple[str, str]] - (link path, resolved target) pairs.
    Example: _home_dir_aliases()  # [('/Users/x/Development', '/Users/x/Library/.../Development')]
    """
    base = home if home is not None else Path.home()
    out: List[tuple] = []
    try:
        entries = sorted(os.listdir(base))
    except OSError as exc:
        logger.debug("claude_project_dir_home_scan_failed", home=str(base), error=str(exc))
        return out
    for name in entries:
        link = os.path.join(str(base), name)
        try:
            if not os.path.islink(link) or not os.path.isdir(link):
                continue
        except OSError:
            # A dangling or unreadable link contributes no spelling.
            continue
        target = _real(link)
        if target and target != link:
            out.append((link, target))
    return out


def path_spellings(
    working_dir: Optional[str], *, home: Optional[Path] = None
) -> List[str]:
    """Every literal path string that denotes this one directory.

    Description: the answer to "which cwd could the user have typed to
      land here". Always includes the input itself and its resolved form;
      adds one alias per top-level ``$HOME`` symlink whose target is a
      prefix of the resolved path. ORDER IS STABLE AND MEANINGFUL - the
      literal spelling first, then the resolved one, then aliases in
      directory order - so a caller reporting evidence can say which
      spelling produced a hit and two runs agree.

      DE-DUPLICATED, NEVER COLLAPSED. When the input is already
      canonical and no alias exists, the result is a one-element list
      rather than an empty one; "one spelling" and "no spellings" are
      different facts and only the second means the input was unusable.
    Inputs: working_dir (str | None) - an absolute path, or None. home
      (Path | None) - override for tests.
    Output: list[str] - one or more path strings; empty only when
      ``working_dir`` is falsy.
    Example: path_spellings('/Users/x/Library/Mobile Documents/.../Sync/Development/P')
      # ['/Users/x/Library/.../Development/P', '/Users/x/Development/P']
    """
    if not working_dir:
        return []
    seen: List[str] = [working_dir]
    resolved = _real(working_dir)
    if resolved and resolved not in seen:
        seen.append(resolved)
    anchor = resolved or working_dir
    for link, target in _home_dir_aliases(home=home):
        if anchor == target:
            alias = link
        elif anchor.startswith(target.rstrip("/") + "/"):
            alias = link.rstrip("/") + anchor[len(target.rstrip("/")):]
        else:
            continue
        if alias not in seen:
            seen.append(alias)
    return seen


def candidate_project_dirs(
    working_dir: Optional[str],
    *,
    projects_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    existing: Optional[Sequence[str]] = None,
) -> List[Path]:
    """Every project directory that could hold this working dir's transcripts.

    Description: slugifies each spelling from :func:`path_spellings` and
      keeps the ones that EXIST. A spelling whose directory is absent is
      dropped silently - Claude Code only creates a project directory
      once a session has run from that spelling, so absence is the
      ordinary case, not a fault.

      ``existing`` REPLACES the filesystem check, and exists for the one
      real case that needs it: analysing a machine's corpus from a
      DIFFERENT machine, where the paths are identical but the
      ``~/.claude/projects`` listing is not. Passing a listing collected
      on the source machine keeps the answer honest instead of silently
      resolving against the wrong disk - which would drop every real
      directory and report "no candidate" for every row.
    Inputs: working_dir (str | None). projects_dir (Path | None) -
      override for tests; defaults to :func:`default_projects_dir`. home
      (Path | None) - override for tests. existing (Sequence[str] | None)
      - bare directory names known to exist; when given, no filesystem
      call is made.
    Output: list[Path] - existing directories, in the stable order
      :func:`path_spellings` defines; empty when nothing exists.
    Example: candidate_project_dirs('/Users/x/Development/P')
      # [PosixPath('/Users/x/.claude/projects/-Users-x-...-P')]
    """
    base = projects_dir if projects_dir is not None else default_projects_dir()
    known = set(existing) if existing is not None else None
    out: List[Path] = []
    for spelling in path_spellings(working_dir, home=home):
        candidate = base / slugify_project_dir(spelling)
        if candidate in out:
            continue
        if known is not None:
            if candidate.name in known:
                out.append(candidate)
            continue
        try:
            if candidate.is_dir():
                out.append(candidate)
        except OSError as exc:
            logger.debug(
                "claude_project_dir_stat_failed",
                candidate=str(candidate),
                error=str(exc),
            )
    return out


def same_directory(
    left: Optional[str], right: Optional[str], *, home: Optional[Path] = None
) -> bool:
    """True iff two path strings denote the same real directory.

    Description: the BACKWARD signal - compares a transcript's recorded
      ``cwd`` against a session row's ``working_dir`` after resolving
      both, so the two spellings of a symlinked path compare equal. A
      path that cannot be resolved compares equal only to itself, never
      to a different string, so an unresolvable input can never widen a
      match.
    Inputs: left (str | None), right (str | None) - path strings. home
      (Path | None) - accepted for signature symmetry with the other
      helpers; unused, since resolution needs no alias table.
    Output: bool.
    Example: same_directory('/Users/x/Development/P',
        '/Users/x/Library/.../Sync/Development/P')  # True
    """
    if not left or not right:
        return False
    if left == right:
        return True
    lr = _real(left)
    rr = _real(right)
    if lr is None or rr is None:
        return False
    return lr == rr


def project_dir_names(dirs: Sequence[Path]) -> List[str]:
    """Bare directory names for a list of project directories, for logging.

    Description: a full project path is ~120 characters of mostly iCloud
      boilerplate, which makes a log line unreadable; the name alone is
      the part that identifies it.
    Inputs: dirs (Sequence[Path]).
    Output: list[str].
    Example: project_dir_names([Path('/a/b/-Users-x-p')])  # ['-Users-x-p']
    """
    return [d.name for d in dirs]
