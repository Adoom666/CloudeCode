"""Giving every live session a project, and never writing half an answer.

THE INVARIANT, IN THE OWNER'S WORDS: "all sessions belong to projects,
the root folder ... its impossible to not have a project." This module is
where that is enforced for a session the app can see, and it exists
because two separate defects were letting a session render with no
project on a box where the project plainly existed.

DEFECT ONE - A HALF-WRITE. ``session_identity.claim_instance`` applies
each column "only when not None", which reads as safe and is not: the
adopt path derived ``(None, 'none')`` for a directory it could not match
and handed both to the claim, so the ``None`` project id was SKIPPED and
the ``'none'`` attribution was WRITTEN. The row kept the project id it
already had and acquired an attribution that contradicts it. The client
resolves that contradiction attribution-first
(``_buildProjectSessionGroups`` in client/js/launchpad.js tests
``attribution === 'none'`` before it looks at ``project_id``), so the
session moved to "no project" while still carrying a perfectly good one.
Measured on the owner's box 2026-09-08: rows 7 and 8, ``project_id`` 1
and 2, ``project_attribution`` ``'none'``.

The rule this module adds: THE PAIR MOVES TOGETHER OR NEITHER MOVES.
A derivation that cannot beat what the row already holds writes nothing
at all.

DEFECT TWO - A SPELLING THE LEXICAL RULE CANNOT CROSS.
``project_attribution.attribute`` is deliberately, correctly lexical: it
expands ``~`` and never resolves a symlink, because resolving rewrites a
path the user chose. But ``~/Development`` on this machine is a symlink
into iCloud Drive, so a session whose probed cwd is
``/Users/jsugamele/Development/Assistants/Media`` can never match the
project declared at
``/Users/jsugamele/Library/Mobile Documents/.../Assistants/Media``. The
lexical answer ``'none'`` is locally true and globally wrong, and it is
the direct cause of defect one's contradiction.

So the ladder gets a SECOND rung: try the path as written, and only if
that matches nothing, try it canonicalised. Canonicalising is a
FALLBACK, never the primary comparison - the as-written match still wins
- so a project declared at a symlink keeps collecting the sessions
declared at that same symlink. ``transcript_import_paths.canonical`` is
the spelling function, imported rather than re-implemented, because this
codebase already decided realpath is "the one spelling this codebase
stores for a directory".

THREE OUTCOMES SURVIVE, AND THE FOURTH IS NEW.

  ``matched``            the directory as written sits under a project.
  ``matched_canonical``  it does once the symlinks are resolved. Same
                         attribution value on the row; a separate rule
                         name so a reader can tell which rung fired,
                         because a rung nobody has ever observed to fire
                         is unmeasured rather than proven.
  ``created``            it sits under nothing, so a project was minted
                         at it. Only ever with ``allow_create=True``.
  ``none``               read, and deliberately not given a project: the
                         one exclusion is a per-run scratch directory,
                         which can never be a project root.
  ``unknown``            could not be read or situated. NOT an answer,
                         and it writes nothing, ever.

WHY SCRATCH IS THE ONLY EXCLUSION. It is the same single exclusion the
transcript importer names, and it is about the PATH, not about the work:
``/private/tmp``, ``/tmp`` and ``/var/folders`` hold per-run directories
that are gone by tomorrow, and minting a project at one would put a dead
folder in the launcher forever.

AN ARCHIVED PROJECT TAKES NO PART IN MATCHING, and that was measured
rather than reasoned. The owner's install carries an archived catch-all
project rooted at ``$HOME``, which contains every session on the machine
and therefore won the as-written rung for the very rows this module was
written to repair - so the canonical rung never ran. The repair script's
dry run caught it by refusing to relocate them, which is the whole
reason it refuses. See :func:`matchable_roots`.

MINTING IS IDEMPOTENT ACROSS BOTH SPELLINGS, which is the only reason it
is safe to run automatically. ``projects.root`` is UNIQUE and
``project_writes.create_project`` refuses a duplicate root, so a mint
that checked only one spelling would either raise or grow the second
node for one folder that the launcher fight already paid for once.
:func:`ensure_project_row` looks for an existing project at the
as-written form AND at the canonical form before it inserts anything,
and it inserts at the CANONICAL form - punchlist item 16's own note,
verbatim: "Any auto-derived path must use the resolved iCloud spelling."
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import structlog

from src.core.db_models import (
    PROJECT_SOURCE_ADOPTION,
    SESSION_ATTRIBUTION_DERIVED_DEEPEST,
    SESSION_ATTRIBUTION_NONE,
    SESSION_ATTRIBUTION_UNKNOWN,
)
from src.core.project_attribution import attribute, normalize_path_for_match
from src.core.transcript_import_paths import canonical, git_top_level, is_scratch
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

#: The directory matched a project root exactly as the session spells it.
BINDING_MATCHED = "matched"
#: It matched only once both sides were canonicalised (symlinks resolved).
BINDING_MATCHED_CANONICAL = "matched_canonical"
#: No project contained it, so one was minted at it.
BINDING_CREATED = "created"
#: Read, situated, and deliberately given no project. Scratch only.
BINDING_NONE = "none"
#: Could not be read or situated. Never written.
BINDING_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProjectBinding:
    """What one session's directory resolves to, and whether to write it.

    Description: the pair plus the provenance of the pair. ``rule`` names
      which rung of the ladder answered, which is the only way a reader
      can tell a canonical-spelling rescue from an ordinary match after
      the fact - both store the same ``derived_deepest`` on the row.
    Inputs (constructor): project_id (int | None) - the resolved project.
      attribution (str) - a ``sessions.project_attribution`` value.
      rule (str) - one of the ``BINDING_*`` constants.
      created_project (bool) - True only when this call inserted a
      projects row. root (str | None) - the project root that matched or
      was created. detail (str | None) - human-readable reason.
    Output: a ProjectBinding instance.
    Example: resolve_project_binding(conn, '/a/b').project_id
    """

    project_id: Optional[int] = None
    attribution: str = BINDING_UNKNOWN
    rule: str = BINDING_UNKNOWN
    created_project: bool = False
    root: Optional[str] = None
    detail: Optional[str] = None

    @property
    def determined(self) -> bool:
        """Whether this binding is a measurement rather than a shrug.

        Inputs: none.
        Output: bool - False only for the ``unknown`` rung.
        Example: ProjectBinding().determined  # False
        """
        return self.rule != BINDING_UNKNOWN


def matchable_roots(conn: sqlite3.Connection) -> Dict[str, int]:
    """Project roots a LIVE session may be attributed to: not archived.

    Description: MEASURED, NOT ASSUMED, AND IT CHANGED THE ANSWER. The
      shared ``_project_roots`` returns every row, archived included,
      and the owner's install carries an ARCHIVED catch-all project
      rooted at ``$HOME``. That root contains literally every session,
      so it won the as-written rung for the two rows this module was
      written to repair - and winning there meant the canonical rung,
      the one that finds the project they actually belong to, never ran
      at all. The repair's own dry run is what caught it, by refusing to
      relocate them.

      So an archived project takes no part in matching. It is not a
      place to put a running session: it is hidden behind "show
      archived", and attributing a live session there would move it from
      "no project" to "a project you cannot see", which is not an
      improvement. A project at exactly the right root is still REUSED
      rather than duplicated when one is minted - see
      :func:`ensure_project_row`, which looks a root up without this
      filter because ``projects.root`` is UNIQUE.
    Inputs: conn (sqlite3.Connection).
    Output: dict[str, int] - root -> project id, archived rows omitted.
      Empty when the projects table is absent, which is the same
      could-not-read answer ``_project_roots`` gives.
    Example: matchable_roots(conn)  # {'/Users/j/code/app': 7}
    """
    try:
        rows = conn.execute(
            "SELECT root, id FROM projects WHERE archived_at IS NULL"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


def _spellings(working_dir: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """The as-written and canonical forms of one directory.

    Description: the two forms every comparison in this module runs
      against, in the order they are tried. The canonical form is None
      when it adds nothing (identical to the as-written form) or cannot
      be computed, so a caller can test it for "is there a second thing
      to try" without comparing strings itself.
    Inputs: working_dir (str | None).
    Output: tuple[str | None, str | None] - (as written, canonical-or-None).
    Example: _spellings('/tmp')  # ('/tmp', '/private/tmp')
    """
    written = normalize_path_for_match(working_dir)
    if written is None:
        return None, None
    resolved = canonical(written)
    if not resolved or resolved == written:
        return written, None
    return written, resolved


def ensure_project_row(
    conn: sqlite3.Connection,
    working_dir: str,
    *,
    now: Optional[str] = None,
) -> Tuple[Optional[int], bool, Optional[str]]:
    """Find or mint the project a directory should belong to.

    Description: the last rung of the ladder, and the one that makes the
      owner's invariant true rather than aspirational. The root is the
      git top level when the directory sits in a repository and the
      directory itself otherwise - the same choice the transcript
      importer makes, imported rather than restated - and it is stored
      CANONICAL, per punchlist item 16.

      IDEMPOTENT ACROSS SPELLINGS. A project already registered at
      either the as-written or the canonical form of the chosen root is
      REUSED, never duplicated, because ``projects.root`` is UNIQUE and
      a second node for one folder is the bug the launcher already paid
      for once. The insert is therefore only ever reached for a root no
      spelling of which is known.

      NOT ARCHIVED, unlike the transcript importer's projects: this is
      minted for a session that is running right now, and hiding the
      project of a live session behind "show archived" would be a worse
      lie than the missing project it replaces.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      working_dir (str) - the session's directory, already situated.
      now (str | None) - ISO-8601 stamp for the new row.
    Output: tuple[int | None, bool, str | None] - (project id, whether
      this call inserted it, the root). The id is None only when the
      projects table cannot be written, which is a could-not-determine
      and never a "belongs to nothing".
    Example: ensure_project_row(conn, '/Users/j/code/app')  # (7, True, ...)
    """
    stamp = now or utc_now()
    top = git_top_level(working_dir)
    chosen = top or working_dir
    root = canonical(chosen) or chosen
    # BOTH SPELLINGS, BEFORE INSERTING ANYTHING. Checking only the
    # canonical form would insert a second row for a project the user
    # registered at the symlink, which is exactly the duplication this
    # whole subsystem exists to prevent.
    lookups = [root]
    written = str(Path(chosen).expanduser())
    if written != root:
        lookups.append(written)
    try:
        for candidate in lookups:
            found = conn.execute(
                "SELECT id FROM projects WHERE root = ?", (candidate,)
            ).fetchone()
            if found is not None:
                return int(found[0]), False, candidate
        cursor = conn.execute(
            "INSERT INTO projects (root, raw_path, display_name, source, "
            "presence, created_at, updated_at, last_opened_at) "
            "VALUES (?, ?, ?, ?, 'unchecked', ?, ?, ?)",
            (
                root,
                root,
                _display_name_for(root, conn),
                PROJECT_SOURCE_ADOPTION,
                stamp,
                stamp,
                stamp,
            ),
        )
    except sqlite3.Error as exc:
        # COULD NOT WRITE, which is not the same as "belongs to nothing".
        # Logged with context and reported as an absence of an answer so
        # the caller writes nothing rather than recording a guess.
        logger.warning(
            "session_project_mint_failed",
            working_dir=working_dir,
            root=root,
            error=str(exc),
            note="the projects table could not be read or written",
        )
        return None, False, None
    return int(cursor.lastrowid), True, root


def _display_name_for(root: str, conn: sqlite3.Connection) -> str:
    """A display name for a minted project that no existing row uses.

    Description: the folder's own basename, which is what a user would
      have typed anyway, with a numeric suffix only when that name is
      already taken - ``create_project`` treats a duplicate display name
      as a conflict, so a mint that ignored this would raise on the
      second project called "src".
    Inputs: root (str) - the canonical project root. conn - for the
      uniqueness check.
    Output: str - a display name no projects row currently carries.
    Example: _display_name_for('/Users/j/code/app', conn)  # 'app'
    """
    base = Path(root).name or root
    candidate = base
    suffix = 2
    while True:
        try:
            taken = conn.execute(
                "SELECT 1 FROM projects WHERE display_name = ?", (candidate,)
            ).fetchone()
        except sqlite3.Error:
            return candidate
        if taken is None:
            return candidate
        candidate = f"{base} ({suffix})"
        suffix += 1


def resolve_project_binding(
    conn: sqlite3.Connection,
    working_dir: Optional[str],
    *,
    stored_project_id: Optional[int] = None,
    allow_create: bool = False,
    now: Optional[str] = None,
    roots: Optional[Dict[str, int]] = None,
) -> ProjectBinding:
    """Resolve one session's directory onto a project, spelling included.

    Description: the whole ladder. As-written match, then canonical
      match, then - only when asked - a minted project, then the two
      honest refusals.

      ``stored_project_id`` is what the row ALREADY holds, and it is not
      a tie-breaker: it is the reason a refusal may be silent. When the
      ladder ends in ``none`` or ``unknown`` and the row already carries
      a project, this returns that project unchanged with no attribution
      claim to write, so the caller cannot half-overwrite a good row
      with a worse answer. That single rule is what fixes the rows this
      module's docstring names.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      working_dir (str | None) - the probed cwd; None means the probe
      did not answer. stored_project_id (int | None) - the project the
      row already holds, if any. allow_create (bool) - whether the last
      rung may mint a project. now (str | None) - clock for the mint.
      roots (dict[str, int] | None) - project root -> id, read from the
      connection when not supplied.
    Output: ProjectBinding.
    Example:
        resolve_project_binding(conn, '/Users/j/Development/app').rule
    """
    known = matchable_roots(conn) if roots is None else roots
    written, resolved = _spellings(working_dir)

    if written is None:
        return ProjectBinding(
            project_id=stored_project_id,
            attribution=SESSION_ATTRIBUTION_UNKNOWN,
            rule=BINDING_UNKNOWN,
            detail="the working directory could not be read or situated",
        )

    matched_id, matched_attr = attribute(written, known)
    if matched_attr == SESSION_ATTRIBUTION_DERIVED_DEEPEST:
        return ProjectBinding(
            project_id=matched_id,
            attribution=SESSION_ATTRIBUTION_DERIVED_DEEPEST,
            rule=BINDING_MATCHED,
            root=written,
        )

    # SECOND RUNG. Only reached when the path as written matched nothing,
    # so a project declared at a symlink still wins for a session
    # declared at that same symlink.
    if resolved is not None:
        canon_id, canon_attr = attribute(resolved, known)
        if canon_attr == SESSION_ATTRIBUTION_DERIVED_DEEPEST:
            logger.info(
                "session_project_matched_canonical",
                working_dir=written,
                canonical_dir=resolved,
                project_id=canon_id,
                note=(
                    "the path as written matched no project root; it "
                    "matched once symlinks were resolved"
                ),
            )
            return ProjectBinding(
                project_id=canon_id,
                attribution=SESSION_ATTRIBUTION_DERIVED_DEEPEST,
                rule=BINDING_MATCHED_CANONICAL,
                root=resolved,
            )

    if is_scratch(written):
        # THE ONE EXCLUSION, and it is about the path rather than the
        # work. A per-run scratch directory is gone tomorrow; a project
        # minted at one would outlive it in the launcher forever.
        return ProjectBinding(
            project_id=stored_project_id,
            attribution=SESSION_ATTRIBUTION_NONE,
            rule=BINDING_NONE,
            detail="a per-run scratch directory can never be a project root",
        )

    if allow_create:
        project_id, created, root = ensure_project_row(conn, written, now=now)
        if project_id is not None:
            if created:
                logger.info(
                    "session_project_minted",
                    working_dir=written,
                    root=root,
                    project_id=project_id,
                    note=(
                        "no project contained this session's directory, so "
                        "one was created at it: every session belongs to a "
                        "project"
                    ),
                )
            return ProjectBinding(
                project_id=project_id,
                attribution=SESSION_ATTRIBUTION_DERIVED_DEEPEST,
                rule=BINDING_CREATED if created else BINDING_MATCHED,
                created_project=created,
                root=root,
            )
        # The mint could not be evaluated. Not a "belongs to nothing".
        return ProjectBinding(
            project_id=stored_project_id,
            attribution=SESSION_ATTRIBUTION_UNKNOWN,
            rule=BINDING_UNKNOWN,
            detail="a project could not be created for this directory",
        )

    return ProjectBinding(
        project_id=stored_project_id,
        attribution=SESSION_ATTRIBUTION_NONE,
        rule=BINDING_NONE,
        detail="no known project root contains this directory",
    )


def columns_to_write(
    binding: ProjectBinding, stored_project_id: Optional[int]
) -> Tuple[Optional[int], Optional[str]]:
    """The (project_id, attribution) a caller may hand to a row update.

    Description: THE PAIR MOVES TOGETHER OR NEITHER MOVES. A ``None`` in
      either position means "leave the stored column alone", which is
      what ``session_identity.claim_instance`` and
      ``session_store.record_instance`` both already do with a None -
      and that skipping is precisely what turned a half-derived answer
      into a row whose id and attribution contradict each other.

      Three cases. A resolved project is written as a pair. An
      ``unknown`` writes nothing, because the absence of an answer is
      not a fact. A ``none`` writes nothing WHEN THE ROW ALREADY HAS A
      PROJECT - the stored id is evidence somebody once matched this
      session, and a probe that just failed to reproduce it does not
      outrank that - and writes the bare ``none`` otherwise, which
      changes no id because there is none to change.
    Inputs: binding (ProjectBinding) - from
      :func:`resolve_project_binding`. stored_project_id (int | None) -
      what the row holds now.
    Output: tuple[int | None, str | None] - values for the row update,
      where None means do not touch that column.
    Example: columns_to_write(binding, 4)  # (7, 'derived_deepest')
    """
    if binding.attribution == SESSION_ATTRIBUTION_DERIVED_DEEPEST \
            and binding.project_id is not None:
        return binding.project_id, SESSION_ATTRIBUTION_DERIVED_DEEPEST
    if binding.attribution == SESSION_ATTRIBUTION_NONE \
            and stored_project_id is None:
        return None, SESSION_ATTRIBUTION_NONE
    return None, None


def backfill_sessions_under_root(
    conn: sqlite3.Connection,
    *,
    root: str,
    project_id: int,
    now: Optional[str] = None,
) -> int:
    """Adopt every project-less live session that sits under a new root.

    Description: the other half of punchlist item 16. A session created
      15 ms before its own project row attributed against a projects
      table that did not yet contain it, landed ``project_id NULL``, and
      nothing ever re-probed. Ordering the two writes fixes one
      direction; this fixes the other, so the race cannot land wrong
      whichever way it resolves.

      PURELY ADDITIVE, WHICH IS THE SAFETY ARGUMENT. Only rows with NO
      project id are considered, so this can never move a session off a
      project it already has, and never re-opens a settled deepest-match
      decision. Archived rows are left alone: an archived row is a
      decision about the user's list.

      Both spellings of the root are matched, for the same reason the
      binding ladder has a second rung.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      root (str) - the new project's root. project_id (int) - its id.
      now (str | None) - ISO-8601 stamp for ``updated_at``.
    Output: int - how many session rows were given this project.
    Example: backfill_sessions_under_root(conn, root='/a', project_id=3)
    """
    stamp = now or utc_now()
    prefixes = set()
    for spelling in (normalize_path_for_match(root), canonical(root)):
        if spelling:
            prefixes.add(spelling.rstrip("/"))
    if not prefixes:
        return 0
    written = 0
    try:
        rows = conn.execute(
            "SELECT id, working_dir FROM sessions "
            "WHERE project_id IS NULL AND archived_at IS NULL "
            "AND working_dir IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(
            "project_backfill_unreadable",
            root=root,
            error=str(exc),
            note="no session was re-attributed; nothing was written",
        )
        return 0
    for row in rows:
        candidates = {
            spelling
            for spelling in (
                normalize_path_for_match(row[1]),
                canonical(row[1]),
            )
            if spelling
        }
        if not any(
            candidate == prefix or candidate.startswith(prefix + "/")
            for candidate in candidates
            for prefix in prefixes
        ):
            continue
        conn.execute(
            "UPDATE sessions SET project_id = ?, project_attribution = ?, "
            "updated_at = ? WHERE id = ?",
            (project_id, SESSION_ATTRIBUTION_DERIVED_DEEPEST, stamp, int(row[0])),
        )
        written += 1
    if written:
        logger.info(
            "project_backfill_adopted_sessions",
            root=root,
            project_id=project_id,
            sessions=written,
            note=(
                "sessions that had no project and sit under this new root; "
                "no session was moved off a project it already had"
            ),
        )
    return written
