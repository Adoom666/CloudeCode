"""Project DISPLAY NAMES and the cross-host MERGED identity.

TWO PROBLEMS, ONE CAUSE. The rail addresses a project by its slug, an
encoded absolute path like
``-Users-jsugamele-Development-Assistants-Infrastructure``. In a 272px
rail that middle-truncates to
``-Users-jsugamele-Develo...t-Assistants-Developer``, which removes
precisely the segment that identifies it. And the same project collected
from two machines appears as two unrelated rows, because the tree is
keyed host -> corpus -> project.

THE SLUG IS A LOSSY ENCODING AND MUST NOT BE PARSED. This is the single
most important fact in this module. The slug replaces ``/`` with ``-``,
but a directory name may itself contain ``-``, and nothing marks which
is which. Measured against the live corpus, 2026-09-01, the slug is
provably NOT invertible:

    slug ...Production-bhpp-new-server   real path .../bhpp_new_server
                                         (underscores, not separators)
    slug ...Development-3D-Work          real path .../3D Work
                                         (a SPACE, destroyed entirely)
    slug ...Production-dev-tools-scripts real path .../tools/dev_tools/scripts
                                         (THREE segments, not two)

Splitting a slug on ``-`` and taking the last element yields ``server``,
``Work`` and ``scripts`` for those three - wrong every time, and wrong in
the direction that invents collisions between projects that do not
collide. So the display name is derived from ``observed_cwd``, which is
the real path and is present for 80 of 80 projects (measured). The slug
is carried through UNPARSED as ``full_path`` for tooltip and detail,
which is the only honest thing to do with a string you cannot decode.

A project whose ``observed_cwd`` is NULL is reported with a null
``display_name`` rather than a guess scraped out of its slug. The caller
renders the slug; it does not get a fabricated folder name.

COLLISIONS ARE REAL BUT RARE, AND MEASURED. Across the 77 distinct
projects there are 69 distinct final segments, so exactly 3 names
collide, covering 11 projects:

    '.claude'  x4   (~/.claude, ~/.dotfiles/.claude,
                     .../bhpp_new_server/.claude, ~/Scratch/.claude)
    'outputs'  x4   (four sibling agent-session dirs, distinguishable
                     only at the ``local_<uuid>`` segment TWO levels up)
    'scripts'  x3   (.../claude_4/scripts, .../dev_tools/scripts,
                     ~/scripts)

``disambiguate`` therefore walks LEFT one path segment at a time and
stops the moment a name is unique, rather than falling back to the whole
slug. ``.claude`` becomes ``.dotfiles/.claude``; ``outputs`` keeps
growing until it reaches the segment that actually differs. A project
with no collision keeps its bare folder name, which is the common case
(66 of 77).

THE MERGE IS KEYED ON ``observed_cwd``, NOT ON THE SLUG. Both were
checked. ``message_projects`` is ``UNIQUE (corpus_id, slug)`` and there
are 2 hosts across 3 corpora, so the same project genuinely appears as
separate rows. Measured: 80 project rows collapse to 77 nodes, and
exactly 3 projects exist on both machines:

    /Users/jsugamele
    /Users/jsugamele/Development/Assistants/Media
    /Users/jsugamele/Development/Assistants/Media/.claude/worktrees/vibrant-leakey-ea30bb

DOES THE SAME SLUG ON TWO HOSTS MEAN THE SAME PROJECT? Checked rather
than assumed: for all 3, the ``observed_cwd`` recorded on each host is
byte-identical. They are the same absolute path on two machines with the
same home layout. That is what justifies merging them, and it is why the
key is the real path - if a future host ever disagrees, the two paths
differ and the projects stay separate rather than being silently fused
on a slug that happened to match.

THE MACHINE IS DEMOTED, NEVER DISCARDED. Each merged node carries
``hosts`` (every machine it appears on) and ``members`` (the underlying
per-corpus project rows, each with its own id and counts), so the
information the old tree encoded as a LEVEL is still addressable as a
FIELD. A node's ``transcript_count`` is the sum across its members, and
``members`` is what a host filter narrows.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.core.message_activity import merge_activity

#: ``display_name`` when ``observed_cwd`` is NULL. The caller renders the
#: slug instead; it does not receive a folder name scraped out of a
#: string that cannot be decoded into one.
DISPLAY_NAME_UNKNOWN: Optional[str] = None

#: What the naming does, shipped in ``meta`` so a client cannot present a
#: disambiguated name as though it were the raw folder.
NAMES_MEAN: str = (
    "display_name is the final segment of observed_cwd, widened leftward "
    "only as far as needed to be unique among the projects in this "
    "response (measured 2026-09-01: 3 names collide across 77 projects). "
    "full_path is the original slug, carried through UNPARSED because it "
    "is a lossy encoding - it replaces '/' with '-' and cannot be "
    "inverted for a folder whose own name contains '-', a space or an "
    "underscore. display_name is null when observed_cwd is null; no "
    "folder name is guessed from the slug."
)

#: The ``session_ref_scheme`` value that means "a conversation the owner
#: had", as opposed to ``agent``, which is a sidechain file spawned by
#: one. Named rather than inlined because the number it produces is the
#: PRIMARY figure on a project card and the two differ by an order of
#: magnitude: measured 2026-09-02, 19,588 of 21,039 transcripts are
#: agent sidechains, so showing the total under a "sessions" label would
#: overstate every project by roughly 14x.
SESSION_SCHEME_OWN: str = "uuid"

#: What the session count counts, shipped in ``meta`` so the rail cannot
#: describe it as something else.
SESSIONS_MEAN: str = (
    "session_count is the number of transcripts in the project whose "
    "session_ref_scheme is 'uuid' - the owner's own conversations - and "
    "transcript_count is EVERY transcript including agent sidechains. "
    "Measured 2026-09-02: 1,451 of 21,039 transcripts are uuid-scheme. "
    "session_counted is false when the count was attempted and could "
    "not be established; session_count is then null and MUST NOT be "
    "rendered as 0, because a measured zero and an unmeasured one are "
    "different findings. A node's session_count is null when ANY of its "
    "members' counts is missing, for the same reason transcript_count is."
)

#: What merging does, likewise shipped rather than implied.
MERGE_MEANS: str = (
    "one node per distinct observed_cwd across every host and corpus. "
    "hosts names every machine the project appears on and members "
    "carries the per-corpus project rows, so the machine is a field "
    "rather than a level and nothing is discarded. Measured 2026-09-01: "
    "80 project rows collapse to 77 nodes; 3 projects exist on both "
    "machines, and on all 3 the observed_cwd recorded by each host is "
    "byte-identical."
)


def path_segments(path: Optional[str]) -> List[str]:
    """Split an absolute path into its non-empty segments.

    Description: the ONE place a path is decomposed, so the naming and
      the disambiguation cannot disagree about what a segment is. A
      trailing slash contributes no segment.
    Inputs: path (str|None).
    Output: list[str] - empty for None, '' or '/'.
    Example: path_segments('/Users/j/3D Work') -> ['Users', 'j', '3D Work']
    """
    if not isinstance(path, str):
        return []
    return [seg for seg in path.strip().split("/") if seg]


def leaf_name(path: Optional[str]) -> Optional[str]:
    """The final path segment - the folder's own name.

    Description: derived from ``observed_cwd`` ONLY. Never from a slug;
      see this module's header for why that is not a style preference.
    Inputs: path (str|None). Output: str|None. '/' answers '/'.
    Example: leaf_name('/Users/j/Development/CloudeCode') -> 'CloudeCode'
    """
    if not isinstance(path, str) or not path.strip():
        return DISPLAY_NAME_UNKNOWN
    segments = path_segments(path)
    if not segments:
        return "/" if path.strip().startswith("/") else DISPLAY_NAME_UNKNOWN
    return segments[-1]


def disambiguate(paths: Sequence[Optional[str]]) -> List[Optional[str]]:
    """Name every path by its folder, widening leftward only on collision.

    Description: pure, and the reason it takes the WHOLE set rather than
      one path is that uniqueness is a property of the set. Each path
      starts at 1 segment; any name shared by two or more paths grows to
      2 segments, then 3, and so on, until every name in the group is
      unique or no path in it has another segment to give. A path that
      runs out of segments keeps its fullest form - two genuinely
      identical paths cannot be told apart by more path, and inventing a
      suffix would be a distinction the filesystem does not make.
      Uniqueness is judged case-sensitively, because these are real paths
      on a filesystem the archive did not choose.
    Inputs: paths (sequence of str|None) - observed_cwd values.
    Output: list[str|None] - one name per input, SAME ORDER. None for an
      input with no usable path.
    Example: disambiguate(['/a/.claude', '/b/.claude'])
             -> ['a/.claude', 'b/.claude']
    """
    segments: List[List[str]] = [path_segments(p) for p in paths]
    widths: List[int] = [1] * len(paths)
    usable = [i for i, segs in enumerate(segments) if segs]

    def name_at(index: int) -> str:
        segs = segments[index]
        return "/".join(segs[-min(widths[index], len(segs)):])

    # Grow only the groups that actually clash. Bounded by the deepest
    # path, and every pass either separates a group or exhausts it.
    while True:
        groups: Dict[str, List[int]] = {}
        for index in usable:
            groups.setdefault(name_at(index), []).append(index)
        grew = False
        for members in groups.values():
            if len(members) < 2:
                continue
            # Only widen those that still have a segment left to add.
            can_grow = [i for i in members if widths[i] < len(segments[i])]
            if not can_grow:
                continue
            for i in can_grow:
                widths[i] += 1
            grew = True
        if not grew:
            break

    out: List[Optional[str]] = []
    for i in range(len(paths)):
        out.append(name_at(i) if segments[i] else DISPLAY_NAME_UNKNOWN)
    return out


def merge_projects(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse per-corpus project rows into one node per real project.

    Description: keyed on ``observed_cwd``. A row with no
      ``observed_cwd`` cannot be proved to be the same project as any
      other, so it keys on its own project id and stays separate - the
      safe direction, because wrongly SPLITTING a project shows two nodes
      and wrongly MERGING two shows a project that does not exist.
      ``hosts`` is sorted for a stable rail; ``transcript_count`` sums
      the members, and is None if ANY member's count is missing, because
      a total that silently omits an unmeasured member is a number
      nobody measured. ``session_count`` obeys the identical rule and
      carries ``session_counted`` alongside it, so a client can tell a
      measured zero from a count that could not be established without
      inferring it from a null.
    Inputs: rows (sequence of dict) - each with project_id, slug,
      observed_cwd, host_id, host_display_name, corpus_id,
      transcript_count.
    Output: list[dict] - merged nodes, ordered by display_name then
      full_path so the rail is stable across requests.
    Example: merge_projects(rows)[0]["hosts"] -> ['Joe-MBP-M1', ...]
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for row in rows:
        cwd = row.get("observed_cwd")
        key = f"cwd:{cwd}" if isinstance(cwd, str) and cwd.strip() else f"pid:{row.get('project_id')}"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(dict(row))

    keys = list(order)
    names = disambiguate([buckets[k][0].get("observed_cwd") for k in keys])

    nodes: List[Dict[str, Any]] = []
    for key, display in zip(keys, names):
        members = buckets[key]
        first = members[0]
        counts = [m.get("transcript_count") for m in members]
        total: Optional[int] = (
            sum(int(c) for c in counts)
            if all(isinstance(c, int) for c in counts)
            else None
        )
        # The SAME rule for the session count, and deliberately not a
        # softer one: a node whose members disagree about whether they
        # were counted has no honest total, so it reports none. A member
        # that says session_counted is False poisons the sum even if it
        # also carries a number, because that number was not measured.
        session_counts = [m.get("session_count") for m in members]
        session_counted = (
            all(m.get("session_counted") is not False for m in members)
            and all(isinstance(c, int) for c in session_counts)
        )
        session_total: Optional[int] = (
            sum(int(c) for c in session_counts) if session_counted else None
        )
        hosts = sorted({
            str(m.get("host_display_name"))
            for m in members
            if m.get("host_display_name") is not None
        })
        nodes.append({
            # Addressable by the FIRST member's project id, so an
            # existing per-project route still resolves. members carries
            # the rest; a caller must not assume one node is one id.
            "project_id": first.get("project_id"),
            "display_name": display,
            "full_path": first.get("slug"),
            "observed_cwd": first.get("observed_cwd"),
            "hosts": hosts,
            "host_count": len(hosts),
            "transcript_count": total,
            "session_count": session_total,
            "session_counted": session_counted,
            # The NEWEST across the machines this folder lives on: the
            # owner worked in it then, whichever box he was sitting at.
            # merge_activity also carries the unmeasured case up rather
            # than letting a maximum over the readable members pass for
            # the answer - see src/core/message_activity.py.
            **merge_activity(members),
            "members": [
                {
                    "project_id": m.get("project_id"),
                    "corpus_id": m.get("corpus_id"),
                    "host_id": m.get("host_id"),
                    "host_display_name": m.get("host_display_name"),
                    "slug": m.get("slug"),
                    "transcript_count": m.get("transcript_count"),
                    "session_count": m.get("session_count"),
                    "session_counted": m.get("session_counted"),
                    "newest_activity_at": m.get("newest_activity_at"),
                    "activity_counted": m.get("activity_counted"),
                }
                for m in members
            ],
        })
    nodes.sort(key=lambda n: (
        (n["display_name"] or "").lower(),
        str(n["full_path"] or ""),
    ))
    return nodes


def fetch_project_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Read every project in the archive with its host and both counts.

    Description: 80 rows measured, so this is deliberately NOT paginated
      - the merged tree is only meaningful whole, and a page of it would
      let a client believe a project exists on one machine because the
      row proving otherwise was on page 2.

      BOTH COUNTS COME FROM ONE GROUPED SCAN, NOT TWO AND NOT 77. The
      rail paints every project at once, so a per-project count query
      would be an N+1 on the only way into the archive (77 requests
      measured). A second grouped query would have been correct but
      wasteful, so the total and the own-session count are produced by
      the SAME statement with a conditional SUM - the route therefore
      issues exactly the number of statements it issued before this
      field existed. Measured on the live corpus 2026-09-02, 21,039
      transcripts: the old total-only statement ran at a 0.764ms median
      and this one runs at 4.862ms, so the added cost is 4.10ms and no
      extra round trip. The extra time is not the aggregate, it is the
      loss of the covering index - ``ix_message_transcripts_project``
      covers ``project_id`` alone, so reading ``session_ref_scheme``
      forces a row fetch. A covering ``(project_id, session_ref_scheme)``
      index would remove it and is deliberately NOT added here, because
      it would mean a new migration step for 4ms.

      THREE OUTCOMES ON THE SESSION COUNT. A project with no uuid-scheme
      transcripts is a measured ZERO. A database whose schema cannot
      answer the question at all - an older archive version without
      ``session_ref_scheme``, say - falls back to the total-only
      statement so the totals are still measured, and every row comes
      back ``session_count: None`` with ``session_counted: False``. The
      two are never rendered as each other: a zero is an answer and a
      null is the absence of one.
    Inputs: conn (sqlite3.Connection).
    Output: list[dict] shaped for :func:`merge_projects`.
    Example: fetch_project_rows(conn)[0]["session_count"]
    """
    totals: Dict[Any, int] = {}
    sessions: Dict[Any, int] = {}
    activity: Dict[Any, Optional[str]] = {}
    session_counted = True
    activity_counted = True
    try:
        for row in conn.execute(
            "SELECT project_id, COUNT(*) AS n_total, "
            "SUM(CASE WHEN session_ref_scheme = ? THEN 1 ELSE 0 END) AS n_uuid, "
            "MAX(newest_message_ts) AS newest "
            "FROM message_transcripts WHERE project_id IS NOT NULL "
            "GROUP BY project_id",
            (SESSION_SCHEME_OWN,),
        ).fetchall():
            totals[row["project_id"]] = int(row["n_total"])
            sessions[row["project_id"]] = int(row["n_uuid"] or 0)
            newest = row["newest"]
            activity[row["project_id"]] = newest if isinstance(newest, str) else None
    except sqlite3.Error:
        # The combined statement could not run. Fall back to the total
        # alone rather than losing both numbers, and say so - a session
        # count nobody produced must not arrive as 0. If the fallback
        # fails too the error propagates, which is correct: at that point
        # there is no project list to render at all.
        totals = {
            row["project_id"]: int(row["n"])
            for row in conn.execute(
                "SELECT project_id, COUNT(*) AS n FROM message_transcripts "
                "WHERE project_id IS NOT NULL GROUP BY project_id"
            ).fetchall()
        }
        sessions = {}
        session_counted = False
        activity = {}
        activity_counted = False

    rows = conn.execute(
        """
        SELECT p.id, p.slug, p.observed_cwd, p.corpus_id,
               k.host_id, h.display_name AS host_display_name
          FROM message_projects p
          JOIN message_corpora k ON k.id = p.corpus_id
          JOIN message_hosts h ON h.id = k.host_id
         ORDER BY p.id
        """
    ).fetchall()
    return [
        {
            "project_id": row["id"],
            "slug": row["slug"],
            "observed_cwd": row["observed_cwd"],
            "corpus_id": row["corpus_id"],
            "host_id": row["host_id"],
            "host_display_name": row["host_display_name"],
            "transcript_count": totals.get(row["id"], 0),
            "session_count": sessions.get(row["id"], 0) if session_counted else None,
            "session_counted": session_counted,
            # THREE OUTCOMES, and the two that look alike are kept apart
            # here rather than by the caller. A None with
            # activity_counted True is a MEASURED absence - the project's
            # transcripts were read and none of their messages carries a
            # timestamp. A None with activity_counted False is the
            # database declining to answer. Sorting them into the same
            # bucket would put a project we could not read next to one we
            # read and found genuinely undated.
            "newest_activity_at": (
                activity.get(row["id"]) if activity_counted else None
            ),
            "activity_counted": activity_counted,
        }
        for row in rows
    ]
