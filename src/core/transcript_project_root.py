"""Project-level rooting: a distinct, weaker root than session rooting.

WHY THIS EXISTS. Most of the real corpus's session-kind transcripts predate
this app's own ``claude_session_uuid`` capture, so
transcript_corpus_ingest.root_pending_archives leaves them 'unrooted' - a
correct verdict, but one that dumps every such file into a single flat
manual-inspection queue with no further structure. A transcript's PROJECT is
frequently determinable even when its SESSION is not: Claude Code names each
corpus directory after the working directory the session ran in, and that
naming rule is stable and mechanical - see :func:`project_slug_for_root`.

THE RULE, VERIFIED AGAINST REAL DATA, NOT ASSUMED. Every non-alphanumeric
character in the absolute working-directory path is replaced with a single
``-``; alphanumeric characters and existing ``-`` characters pass through
unchanged. This was measured, not read from documentation: 78 real corpus
directories on a live ``~/.claude/projects`` tree were checked by comparing
each directory's slug name against the ``cwd`` field recorded inside its own
first transcript line. The naive rule the task started from - "replace only
``/`` and ``.``" - matched only 60 of 78 (it missed every directory name
containing an underscore or a literal space, e.g.
``bhpp_new_server`` -> ``bhpp-new-server``, ``3D Work`` -> ``3D-Work``). The
GENERAL rule used here - substitute every character outside ``[A-Za-z0-9]``
- matched 76 of 78; the remaining 2 mismatches were traced to the project
directory having been RENAMED or MOVED after the session was recorded (the
slug is stamped once, at session creation, and never updated to track a
later rename or move), not to a substitution-rule failure.

THE DOT CASE, EXPLICITLY CHECKED (the task's own stop-case: "verify ...
including a path that contains a dot in a real directory name, because that
substitution is not reversible in general and that is exactly where it will
break"). A real corpus directory, ``/Users/jsugamele/Development/Python/
csj.dbexport``, produced slug ``-Users-jsugamele-Development-Python-csj-
dbexport`` - confirmed against the live corpus, not invented. This also
demonstrates exactly where the rule breaks: it is NOT reversible. The paths
``csj.dbexport``, ``csj_dbexport``, ``csj dbexport``, and ``csj/dbexport``
(as a nested directory) all substitute to the identical slug
``csj-dbexport``. Two DIFFERENT real project roots can therefore collide on
one slug. This module never guesses which one a colliding slug means -
:func:`resolve_project_for_slug` reports ``ambiguous`` and leaves the
archive exactly as unrooted as it started, per this project's standing rule
that nothing here ever infers an attribution a human did not confirm.

WEAKER THAN SESSION ROOTING, AND NEVER CONFUSED WITH IT. ``root_state`` on
transcript_archives keeps meaning exactly what it always meant - see
transcript_archive.py's module docstring - so this module never writes to
it. Project-level attribution lives entirely in the sibling ``project_id`` /
``project_rooted_at`` / ``project_rooted_by`` columns added in schema v15
(see db_models.py's "schema v14 -> v15" comment). An archive can therefore
be simultaneously root_state='unrooted' (session-level attribution still
genuinely pending) AND carry a non-NULL project_id (a real, weaker hint a
human does not have to rediscover) - the two facts live in different
columns and are never collapsed into one. When session-level rooting later
becomes possible for such an archive, a plain call to
transcript_archive.root_archive() upgrades it (root_state -> 'rooted', a
NEW transcript_root_decisions row) while this row's project_id and its
earlier decision row are left untouched - the audit trail is never lost,
only added to.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Dict, List, Optional

try:
    import structlog

    logger = structlog.get_logger()
except ImportError:  # pragma: no cover - see transcript_archive.py's own guard
    class _NoOpLogger:
        def __getattr__(self, _name: str):
            return lambda *a, **k: None

    logger = _NoOpLogger()

from src.core.db import transaction
from src.core.project_store import list_projects
from src.core.transcript_archive import utc_now

#: Decided-by tag written on every project-level transcript_root_decisions
#: row this module writes, so a human reviewing the audit trail can tell a
#: structural project match apart from a session-level one (DECIDED_BY_
#: INGESTER in transcript_corpus_ingest.py) or a manual human decision.
DECIDED_BY_PROJECT_INGESTER = "corpus_ingest:project_structural"

#: Any character outside this set becomes a single '-'. See the module
#: docstring for the real-corpus measurement behind this exact rule.
_SLUG_SUBSTITUTE_RE = re.compile(r"[^A-Za-z0-9]")


def project_slug_for_root(root: str) -> str:
    """Compute the corpus directory slug Claude Code would use for a root.

    Description: pure string substitution, no filesystem access - see the
      module docstring for the real-corpus measurement that produced this
      exact rule (every non-alphanumeric character becomes '-', existing
      '-' characters pass through unchanged, nothing is collapsed or
      lowercased).
    Inputs: root (str) - a project's normalised root path (see
      project_store.normalize_root), or any absolute working-directory
      string in the same shape.
    Output: str - the slug, matching a real corpus directory name when
      one exists for this exact root.
    Example: project_slug_for_root("/Users/j/dev/csj.dbexport")
      -> "-Users-j-dev-csj-dbexport"
    """
    return _SLUG_SUBSTITUTE_RE.sub("-", root)


def build_project_slug_index(conn) -> Dict[str, List[int]]:
    """Map every known project's computed slug to the project id(s) sharing it.

    Description: computed once per rooting run rather than per archive,
      since the project list is small and stable relative to the number
      of archives being resolved. A slug mapping to MORE than one project
      id is the collision case the module docstring describes - callers
      must treat that as ambiguous, never pick one.
    Inputs: conn - sqlite3.Connection.
    Output: dict[str, list[int]] - slug -> project ids (len >= 2 means
      ambiguous for that slug). Archived projects are excluded, matching
      project_store.list_projects's own default: an archived project is
      not a live attribution target.
    Example: build_project_slug_index(conn)
      -> {"-Users-j-dev-app": [3]}
    """
    index: Dict[str, List[int]] = {}
    for project in list_projects(conn, include_archived=False):
        slug = project_slug_for_root(project["root"])
        index.setdefault(slug, []).append(int(project["id"]))
    return index


def slug_from_source_path(source_path: str) -> Optional[str]:
    """Extract the corpus-slug directory component from a source_path.

    Description: the slug is always the FIRST path component of a
      source_path, for both shapes transcript_corpus_discover.py
      produces (``<slug>/<file>.jsonl`` for a session,
      ``<slug>/<uuid>/subagents/<file>.jsonl`` for a subagent) - pure
      path arithmetic, no content read, matching this project's rule
      that structural rooting never reads a file to make its decision.
    Inputs: source_path (str) - corpus-relative, POSIX separators.
    Output: str | None - None only for an empty source_path, which this
      module has never observed in the real corpus but refuses to guess
      about rather than assume away.
    Example: slug_from_source_path("slug/x.jsonl") -> "slug"
    """
    parts = PurePosixPath(source_path).parts
    if not parts:
        return None
    return parts[0]


def resolve_project_for_slug(
    slug: str, slug_index: Dict[str, List[int]]
) -> Dict[str, object]:
    """Resolve one slug against a prebuilt slug index, three outcomes only.

    Description: never guesses - see the module docstring's collision
      discussion. Split out from :func:`root_pending_archives_by_project`
      so the resolution logic itself is directly unit-testable against a
      synthetic index without a database.
    Inputs: slug (str). slug_index (dict[str, list[int]]) - from
      :func:`build_project_slug_index`.
    Output: dict with keys "outcome" (one of "matched", "no_match",
      "ambiguous") and "project_id" (int | None - set only when
      outcome == "matched").
    Example: resolve_project_for_slug("x", {"x": [1]})
      -> {"outcome": "matched", "project_id": 1}
    """
    candidates = slug_index.get(slug, [])
    if len(candidates) == 0:
        return {"outcome": "no_match", "project_id": None}
    if len(candidates) > 1:
        return {"outcome": "ambiguous", "project_id": None}
    return {"outcome": "matched", "project_id": candidates[0]}


def root_pending_archives_by_project(
    conn, *, decided_by: str = DECIDED_BY_PROJECT_INGESTER
) -> Dict[str, int]:
    """Apply project-level rooting to every archive still unrooted and unresolved.

    Description: DB-driven, safe to re-run - only visits archives with
      root_state='unrooted' AND project_id IS NULL, so an archive already
      given a project (or since session-rooted, or since orphaned) is
      never revisited. Never touches root_state (see module docstring);
      only ever writes project_id / project_rooted_at / project_rooted_by
      on transcript_archives and appends one transcript_root_decisions
      row per resolution, using the SAME 'rooted' action value
      session/subagent rooting already uses - disambiguated by
      project_id being the only FK column populated on that row (see
      db_models.py's schema v14 -> v15 comment for why this reuses
      'rooted' rather than adding a new action value).
    Inputs: conn - sqlite3.Connection. decided_by (str) - recorded on
      every transcript_root_decisions row this call writes.
    Output: dict[str, int] - counts keyed by outcome: project_rooted,
      project_no_match (slug matches no known project - stays unrooted,
      still queued), project_ambiguous (slug matches more than one known
      project - refused, stays unrooted, still queued), project_bad_path
      (source_path could not be parsed - refused, stays unrooted).
    Example: root_pending_archives_by_project(conn)
      -> {"project_rooted": 213, "project_no_match": 7,
          "project_ambiguous": 0, "project_bad_path": 0}
    """
    counts: Dict[str, int] = {
        "project_rooted": 0,
        "project_no_match": 0,
        "project_ambiguous": 0,
        "project_bad_path": 0,
    }

    slug_index = build_project_slug_index(conn)

    rows = conn.execute(
        "SELECT id, source_path FROM transcript_archives"
        " WHERE root_state = 'unrooted' AND project_id IS NULL"
        " ORDER BY id ASC"
    ).fetchall()

    for row in rows:
        archive_id = int(row["id"])
        slug = slug_from_source_path(row["source_path"])
        if slug is None:
            counts["project_bad_path"] += 1
            continue

        resolution = resolve_project_for_slug(slug, slug_index)
        outcome = resolution["outcome"]
        if outcome == "no_match":
            counts["project_no_match"] += 1
            continue
        if outcome == "ambiguous":
            counts["project_ambiguous"] += 1
            continue

        project_id = resolution["project_id"]
        now = utc_now()
        with transaction(conn):
            conn.execute(
                "INSERT INTO transcript_root_decisions ("
                "  archive_id, decided_at, decided_by, action, project_id,"
                "  note"
                ") VALUES (?, ?, ?, 'rooted', ?, ?)",
                (
                    archive_id,
                    now,
                    decided_by,
                    project_id,
                    f"structural: corpus slug '{slug}' matches exactly"
                    " one known project root",
                ),
            )
            conn.execute(
                "UPDATE transcript_archives SET project_id = ?,"
                " project_rooted_at = ?, project_rooted_by = ?"
                " WHERE id = ?",
                (project_id, now, decided_by, archive_id),
            )
        logger.info(
            "transcript_archive_project_rooted",
            archive_id=archive_id,
            project_id=project_id,
            slug=slug,
            decided_by=decided_by,
        )
        counts["project_rooted"] += 1

    return counts
