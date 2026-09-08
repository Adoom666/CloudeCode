"""Writing and reading the v17 host / corpus / project dimension.

WHAT THIS OWNS. Interning a host, a corpus and a project; attaching one
transcript to all three; deriving a project slug from a corpus layout;
and the two cross-host MEASUREMENTS the owner asked for - how many
project slugs collide between machines, and how many session uuids
appear on more than one.

THE TWO CROSS-HOST CASES ARE OPPOSITES AND MUST NOT BE TREATED ALIKE.

  Same SLUG on two hosts is a COLLISION. A slug is a lossy derived
  string: every non-alphanumeric character in a path becomes '-', so
  ``csj.dbexport``, ``csj_dbexport`` and ``csj dbexport`` all produce
  one slug. Two hosts running as the same unix user produce identical
  slugs for paths that are genuinely different directories on genuinely
  different machines. Merging them would fabricate a project that never
  existed. UNIQUE (corpus_id, slug) makes that impossible BY
  CONSTRUCTION rather than by a rule someone has to remember, and
  GATE_PROJECT_SLUG_COLLISION puts the ones that occur in front of a
  human, because only the owner knows whether the mini's copy of a
  project is the same work or different work.

  Same SESSION UUID on two hosts is THE SAME SESSION. A session uuid is
  uuid4 - 122 random bits - and 19,403 of them were measured on this
  corpus with zero duplicates. The probability of an accidental repeat
  is not small, it is nil. The owner also moves Claude files between his
  machines deliberately. So a repeat means the conversation was copied,
  and the right model is one session with appearances on two hosts. It
  is NOT gated, because there is nothing wrong; it is counted, because
  the count is exactly what tells the owner how much of his history is
  already duplicated before he moves anything else.

Getting these two backwards in either direction is the expensive
mistake: gate the sessions and the queue fills with the owner's own
filing habits, merge the slugs and two machines' work becomes one
project with no way back.
"""

from __future__ import annotations

import posixpath
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from src.core.message_gate_contract import GATE_PROJECT_SLUG_COLLISION
from src.core.message_host_identity import HostIdentity
from src.core.message_model_store import record_finding, utc_now

#: Corpus layouts this module knows how to read a project slug out of.
#: A layout is DECLARED per corpus rather than sniffed per path, because
#: sniffing would silently produce a plausible slug for a directory that
#: is not a project directory at all.
LAYOUT_CLAUDE_PROJECTS: str = "claude_projects"
LAYOUT_NESTED_CLAUDE_PROJECTS: str = "nested_claude_projects"
LAYOUTS: Tuple[str, ...] = (LAYOUT_CLAUDE_PROJECTS,
                            LAYOUT_NESTED_CLAUDE_PROJECTS)

#: message_host_ddl.PROJECT_ATTRIBUTION_VALUES, by name.
PROJ_DERIVED: str = "derived"
PROJ_NONE_DECLARED: str = "none_declared"
PROJ_CANNOT_DETERMINE: str = "cannot_determine"

#: The marker a nested corpus's own project directories sit under.
_NESTED_MARKER: Tuple[str, str] = (".claude", "projects")


def upsert_host(
    conn: sqlite3.Connection, identity: HostIdentity,
    now: Optional[str] = None,
) -> int:
    """Intern one machine, returning its host id.

    Description: keyed on ``machine_id`` alone. ``display_name``,
      ``hostname`` and ``platform`` are refreshed on every call because
      they are descriptive and allowed to change; the identity is not.
      A rename therefore updates a row instead of minting a second host
      for one machine, which is the failure a hostname-keyed table has.
    Inputs: conn, identity (HostIdentity), now (ISO-8601 str or None).
    Output: int - message_hosts.id.
    Example: upsert_host(conn, ident) == upsert_host(conn, ident) -> True
    """
    stamp = now or utc_now()
    row = conn.execute(
        "SELECT id FROM message_hosts WHERE machine_id = ?",
        (identity.machine_id,),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE message_hosts SET display_name = ?, hostname = ?, "
            "platform = ? WHERE id = ?",
            (identity.display_name, identity.hostname, identity.platform,
             int(row[0])),
        )
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO message_hosts (machine_id, machine_id_scheme, "
        "display_name, hostname, platform, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (identity.machine_id, identity.machine_id_scheme,
         identity.display_name, identity.hostname, identity.platform, stamp),
    )
    return int(cur.lastrowid)


def upsert_corpus(
    conn: sqlite3.Connection, host_id: int, corpus_key: str, root_path: str,
    manifest_sha: Optional[str] = None, now: Optional[str] = None,
) -> int:
    """Intern one corpus on one host, returning its corpus id.

    Description: a host has more than one place Claude writes
      transcripts and they are not interchangeable, so the corpus is its
      own row rather than a prefix on a path. Keyed UNIQUE
      (host_id, corpus_key).
    Inputs: conn, host_id (int), corpus_key (str), root_path (str),
      manifest_sha (str or None), now (str or None).
    Output: int - message_corpora.id.
    Example: upsert_corpus(conn, 1, "k", "/r") -> 1
    """
    stamp = now or utc_now()
    row = conn.execute(
        "SELECT id FROM message_corpora WHERE host_id = ? AND corpus_key = ?",
        (host_id, corpus_key),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE message_corpora SET root_path = ?, manifest_sha = ? "
            "WHERE id = ?",
            (root_path, manifest_sha, int(row[0])),
        )
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO message_corpora (host_id, corpus_key, root_path, "
        "manifest_sha, collected_at) VALUES (?, ?, ?, ?, ?)",
        (host_id, corpus_key, root_path, manifest_sha, stamp),
    )
    return int(cur.lastrowid)


def upsert_project(
    conn: sqlite3.Connection, corpus_id: int, slug: str,
    observed_cwd: Optional[str] = None, now: Optional[str] = None,
) -> int:
    """Intern one project within one corpus, returning its project id.

    Description: UNIQUE (corpus_id, slug) is what makes the same slug on
      two hosts two distinct projects. ``observed_cwd`` is filled in the
      first time a record in that project states one and is left alone
      afterwards - it is evidence, not a key, and the first observation
      is as good as any later one.
    Inputs: conn, corpus_id (int), slug (str), observed_cwd (str or
      None), now (str or None).
    Output: int - message_projects.id.
    Example: upsert_project(conn, 1, "s") == upsert_project(conn, 1, "s")
    """
    stamp = now or utc_now()
    row = conn.execute(
        "SELECT id, observed_cwd FROM message_projects "
        "WHERE corpus_id = ? AND slug = ?",
        (corpus_id, slug),
    ).fetchone()
    if row is not None:
        if row[1] is None and observed_cwd:
            conn.execute(
                "UPDATE message_projects SET observed_cwd = ? WHERE id = ?",
                (observed_cwd, int(row[0])),
            )
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO message_projects (corpus_id, slug, observed_cwd, "
        "first_seen_at) VALUES (?, ?, ?, ?)",
        (corpus_id, slug, observed_cwd, stamp),
    )
    return int(cur.lastrowid)


def derive_slug(source_path: str, layout: str) -> Tuple[Optional[str], str]:
    """Read a project slug out of a path, per its corpus's layout.

    Description: three outcomes, never two. ``claude_projects`` puts the
      slug in the first path component. ``nested_claude_projects`` (the
      local-agent-mode corpus) buries whole ``.claude/projects`` trees
      inside per-session sandbox directories, so the slug is the
      component after the LAST ``.claude/projects`` marker; the same
      corpus also holds ``audit.jsonl`` files that sit under no project
      directory at all, and that is a fact about the source, reported as
      ``none_declared`` rather than as a failure or as a guessed slug.
    Inputs: source_path (str - relative to the corpus root, posix
      separators), layout (str - one of LAYOUTS).
    Output: (slug or None, attribution) where attribution is
      PROJ_DERIVED, PROJ_NONE_DECLARED or PROJ_CANNOT_DETERMINE.
    Raises: ValueError - unknown layout, because guessing which rule to
      apply is how a slug gets invented.
    Example: derive_slug("-Users-x/s.jsonl", LAYOUT_CLAUDE_PROJECTS)
      -> ("-Users-x", "derived")
    """
    if layout not in LAYOUTS:
        raise ValueError(f"unknown corpus layout: {layout!r}")
    parts = [p for p in posixpath.normpath(source_path).split("/") if p]
    if layout == LAYOUT_CLAUDE_PROJECTS:
        if len(parts) < 2:
            return None, PROJ_CANNOT_DETERMINE
        return parts[0], PROJ_DERIVED
    marker_at = -1
    for index in range(len(parts) - 2):
        if (parts[index], parts[index + 1]) == _NESTED_MARKER:
            marker_at = index
    if marker_at < 0:
        return None, PROJ_NONE_DECLARED
    slug_at = marker_at + 2
    if slug_at >= len(parts) - 1:
        return None, PROJ_CANNOT_DETERMINE
    return parts[slug_at], PROJ_DERIVED


def attribute_transcript(
    conn: sqlite3.Connection, transcript_id: int, *, host_id: int,
    corpus_id: int, project_id: Optional[int], source_path: str,
    host_attribution: str, project_attribution: str,
) -> None:
    """Attach one already-stored transcript to its host, corpus, project.

    Description: attribution is a SECOND write, deliberately. Storage
      happens first and unconditionally (message_model_ingest), so a
      file whose provenance cannot be evidenced is still fully in the
      database with its attribution withheld, rather than being dropped
      for failing to link. Store first, classify second, gate third.
    Inputs: conn, transcript_id (int), host_id (int), corpus_id (int),
      project_id (int or None), source_path (str), host_attribution
      (str), project_attribution (str).
    Output: None.
    Example: attribute_transcript(conn, 1, host_id=1, corpus_id=1,
      project_id=None, source_path="a.jsonl",
      host_attribution="declared", project_attribution="none_declared")
    """
    conn.execute(
        "UPDATE message_transcripts SET host_id = ?, corpus_id = ?, "
        "project_id = ?, source_path = ?, host_attribution = ?, "
        "project_attribution = ? WHERE id = ?",
        (host_id, corpus_id, project_id, source_path, host_attribution,
         project_attribution, transcript_id),
    )


def global_source_ref(machine_id: str, corpus_key: str, rel: str) -> str:
    """The globally unique locator a transcript is stored under.

    Description: v16's ``source_ref`` is UNIQUE and was a bare path
      relative to one corpus root. With two machines running as the same
      unix user that string is no longer unique - the mini and the
      laptop both hold ``-Users-jsugamele/<uuid>.jsonl`` shaped paths -
      so the constraint would either reject a genuinely distinct file or
      be read as meaning two different files are one. Qualifying it with
      the machine id and the corpus key restores the property the
      constraint is asserting, with no schema change.
    Inputs: machine_id (str), corpus_key (str), rel (str).
    Output: str.
    Example: global_source_ref("M", "claude-projects", "a.jsonl")
      -> "M::claude-projects::a.jsonl"
    """
    return f"{machine_id}::{corpus_key}::{rel}"


@dataclass(frozen=True)
class SlugCollision:
    """One slug that exists under more than one host.

    - ``slug``: the colliding derived string.
    - ``host_count``: how many distinct hosts hold it.
    - ``project_ids``: every message_projects row it produced.
    - ``cwds``: the distinct observed cwds, which is the evidence about
      whether the two might mean the same directory.
    """

    slug: str
    host_count: int
    project_ids: Tuple[int, ...]
    cwds: Tuple[str, ...]


def find_slug_collisions(conn: sqlite3.Connection) -> List[SlugCollision]:
    """Every project slug that occurs under more than one host.

    Description: the measurement behind GATE_PROJECT_SLUG_COLLISION. It
      groups by slug across corpora and keeps the ones spanning two or
      more HOSTS. Two corpora on ONE host sharing a slug is not a
      cross-host collision and is excluded - that is the same machine's
      own directory naming, not two machines being conflated.
    Inputs: conn.
    Output: list[SlugCollision], sorted by slug.
    Example: find_slug_collisions(conn) -> []
    """
    rows = conn.execute(
        "SELECT p.slug, COUNT(DISTINCT c.host_id) AS hosts, "
        "       GROUP_CONCAT(p.id), "
        "       GROUP_CONCAT(COALESCE(p.observed_cwd, '')) "
        "  FROM message_projects p "
        "  JOIN message_corpora c ON c.id = p.corpus_id "
        " GROUP BY p.slug HAVING hosts > 1 ORDER BY p.slug"
    ).fetchall()
    out: List[SlugCollision] = []
    for slug, hosts, ids, cwds in rows:
        out.append(SlugCollision(
            slug=str(slug), host_count=int(hosts),
            project_ids=tuple(int(x) for x in str(ids).split(",") if x),
            cwds=tuple(sorted({c for c in str(cwds).split(",") if c})),
        ))
    return out


def record_slug_collisions(
    conn: sqlite3.Connection, now: Optional[str] = None,
) -> int:
    """Raise GATE_PROJECT_SLUG_COLLISION for every cross-host slug.

    Description: one finding per PROJECT involved, so the queue's review
      unit is "a project a human must adjudicate" rather than "a slug",
      and both sides of a collision are individually visible. The detail
      states whether the observed cwds are identical, because that is
      the fact the human needs and it is the fact that reads BACKWARDS
      across hosts: an identical path string on two machines is not
      evidence of one directory, it is precisely the ambiguity, since
      both machines run as the same unix user.

      THE SUBJECT IS A TRANSCRIPT, NOT A PROJECT, AND THAT IS A
      COMPROMISE WORTH KNOWING ABOUT. ``message_ingest_findings`` has a
      CHECK constraint permitting only 'transcript', 'body' and
      'appearance', and SQLite cannot widen a CHECK without rebuilding
      the table - which on an 11 GB database is not an additive
      migration. So the subject recorded is the project's lowest
      transcript id, as a stable representative, and the detail names
      the project explicitly rather than leaving the reader to infer it.

      Deliberately re-derived from the finished database rather than
      raised during ingest: a collision is not a property of the file
      being ingested, and raising it per-file would have fired it for
      the first host too, before the second host existed.
    Inputs: conn, now (str or None).
    Output: int - findings written.
    Example: record_slug_collisions(conn) -> 0
    """
    stamp = now or utc_now()
    written = 0
    for collision in find_slug_collisions(conn):
        same = len(collision.cwds) == 1
        for project_id in collision.project_ids:
            row = conn.execute(
                "SELECT MIN(id) FROM message_transcripts WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None or row[0] is None:
                continue
            record_finding(
                conn, code=GATE_PROJECT_SLUG_COLLISION,
                subject_kind="transcript", subject_id=int(row[0]),
                detail=(
                    f"project id {project_id} (slug {collision.slug!r}) - "
                    f"this slug exists on {collision.host_count} hosts as "
                    f"{len(collision.project_ids)} distinct projects; "
                    f"observed cwd(s) {list(collision.cwds) or 'none recorded'}"
                    + (" - IDENTICAL path string on different machines, "
                       "which is the ambiguity, not proof of one directory"
                       if same else " - different paths")
                ),
                now=stamp,
            )
            written += 1
    return written


def cross_host_sessions(
    conn: sqlite3.Connection,
) -> List[Tuple[str, int, int]]:
    """Session refs that appear on more than one host.

    Description: NOT a gate. A uuid4 session ref repeating across the
      owner's two machines is the same conversation copied, and the
      count is what tells him how much history is already duplicated.
      Restricted to the ``uuid`` scheme: an ``agent-a00fdb4`` id is a
      short per-session ordinal, not a random value, so two hosts
      sharing one carries no such implication and counting it here would
      inflate the number with an entirely different phenomenon.
    Inputs: conn.
    Output: list of (session_ref, host_count, transcript_count), sorted.
    Example: cross_host_sessions(conn) -> []
    """
    return [
        (str(r[0]), int(r[1]), int(r[2]))
        for r in conn.execute(
            "SELECT session_ref, host_count, transcript_count "
            "  FROM message_session_hosts "
            " WHERE session_ref_scheme = 'uuid' AND host_count > 1 "
            " ORDER BY session_ref"
        )
    ]


def attribution_summary(conn: sqlite3.Connection) -> Dict[str, int]:
    """How many transcripts sit in each host-attribution state.

    Description: publishes the third outcome instead of hiding it. A
      v16 row that has never been attributed has NULL here and is
      counted under ``unattributed``, which is a CANNOT DETERMINE and is
      never folded into ``declared``.
    Inputs: conn.
    Output: dict - attribution value (or "unattributed") -> count.
    Example: attribution_summary(conn) -> {}
    """
    return {
        (str(row[0]) if row[0] is not None else "unattributed"): int(row[1])
        for row in conn.execute(
            "SELECT host_attribution, COUNT(*) FROM message_transcripts "
            "GROUP BY host_attribution"
        )
    }


def host_rollup(conn: sqlite3.Connection) -> List[Tuple[str, str, int, int]]:
    """Per host: display name, machine id, transcripts, projects.

    Description: the one-line-per-machine view a reader wants first.
    Inputs: conn.
    Output: list of (display_name, machine_id, transcripts, projects).
    Example: host_rollup(conn) -> []
    """
    return [
        (str(r[0]), str(r[1]), int(r[2]), int(r[3]))
        for r in conn.execute(
            "SELECT h.display_name, h.machine_id, "
            "  (SELECT COUNT(*) FROM message_transcripts t "
            "    WHERE t.host_id = h.id), "
            "  (SELECT COUNT(*) FROM message_projects p "
            "     JOIN message_corpora c ON c.id = p.corpus_id "
            "    WHERE c.host_id = h.id) "
            "  FROM message_hosts h ORDER BY h.display_name"
        )
    ]


def unseen_manifest_paths(
    conn: sqlite3.Connection, corpus_id: int, manifest_paths: Sequence[str],
) -> List[str]:
    """Paths the source machine listed that were never ingested here.

    Description: the antijoin nothing normally runs. Every check asks
      "is what I ingested correct"; this one asks "is what I ingested
      COMPLETE", which is the direction a missing file hides in - an
      absent file contributes no row, trips no branch and reads exactly
      like a clean corpus.
    Inputs: conn, corpus_id (int), manifest_paths (sequence of str).
    Output: list[str] - sorted paths present in the manifest and absent
      from the database.
    Example: unseen_manifest_paths(conn, 1, []) -> []
    """
    have = {
        str(row[0])
        for row in conn.execute(
            "SELECT source_path FROM message_transcripts "
            "WHERE corpus_id = ? AND source_path IS NOT NULL", (corpus_id,)
        )
    }
    return sorted(set(manifest_paths) - have)
