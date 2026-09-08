"""DDL for schema v17: the host / corpus / project dimension.

WHY THIS EXISTS. v16 stores a transcript under a ``source_ref`` and
nothing else. That is correct for exactly one machine and silently wrong
for two. The owner has two: this laptop (~/.claude/projects, 19,540
files) and mac-mini-m4 (~/.claude/projects, 1,477 files). Both run as
the same unix user, so both corpora are full of ``/Users/jsugamele/...``
paths, and three project slugs are byte-identical between them
(measured 2026-08-30: ``-Users-jsugamele``,
``-Users-jsugamele-Development-Assistants-Media`` and
``-Users-jsugamele-Development-Assistants-Media--claude-worktrees-vibrant-leakey-ea30bb``).
Without a host column those are one project. They are not one project.

WHAT A HOST IDENTITY IS, AND WHY IT IS THE PLATFORM UUID. Three
candidates were measured on both machines on 2026-08-30:

  hostname          laptop ``Joe-MBP-M1``, mini ``mac-mini-m4.local``.
                    REJECTED. Every macOS box carries THREE names that
                    can disagree - ComputerName, LocalHostName and
                    ``hostname`` - and the laptop's already do
                    (``Joe-MBP-M1`` / ``Joe-MBP-M1-2`` / ``Joe-MBP-M1``).
                    A name is also mutable in System Settings with no
                    trace, so a rename would silently mint a second host
                    for one machine, which is the exact failure this
                    dimension exists to prevent.
  serial number     laptop ``M4CK9YN4TY``, mini ``DWHX6QKXQF``.
                    Stable and unique, and REJECTED anyway: it is a
                    warranty and support identifier printed on the
                    chassis, so it is the one value here worth not
                    copying into an 11 GB database that gets moved
                    around.
  IOPlatformUUID    laptop ``F95816BC-2819-53B5-98E9-72450A37AADF``,
                    mini ``726E10C9-E70D-5F9E-ACA6-F5CB0D79BA40``.
                    CHOSEN. Stable across renames and OS upgrades,
                    distinct per machine (measured, not assumed), and
                    opaque - it identifies the machine to this database
                    and to nothing else.

So ``machine_id`` is the platform uuid and it is the ONLY identity.
``display_name`` and ``hostname`` are descriptive columns that are
allowed to change; nothing keys off them.

MAKING A WRONG HOST IDENTITY DETECTABLE RATHER THAN SILENT. An opaque
stable id fixes the "two machines look like one" half. It does nothing
about the other half, which is the one that actually bites: bytes get
copied between machines, so the machine a file is SITTING ON is not
evidence of the machine it CAME FROM. Nothing in a .jsonl file names its
host. If ingest simply asserted "I read this under the mini's rsync
directory, therefore it is the mini's", that assertion could never be
wrong in a way anything could see - a verification step that cannot
fail.

The fix is that host attribution needs evidence GENERATED ON THE SOURCE
MACHINE, not inferred by the reader. A collection manifest is captured
on each host: its platform uuid, its corpus root, and one
(relpath, size, sha256) row per file, all produced by code running
there. Ingest hashes the bytes it actually reads and compares. Three
outcomes, recorded per transcript in ``host_attribution``:

  ``manifest_verified``  the bytes read hash to what the source machine
                         said was at that path. Attribution is evidenced.
  ``declared``           no manifest covers this corpus, so the host is
                         the operator's claim and is stored AS a claim.
  ``cannot_determine``   a manifest exists and this file is not in it, or
                         is in it with a different hash. NEVER silently
                         upgraded to ``declared``.

That is the three-outcome rule applied to attribution itself. A wrong
host identity now has to survive a hash comparison against a manifest
written on the other machine, instead of being unfalsifiable.

WHY THREE TABLES AND NOT ONE COLUMN. ``message_corpora`` sits between
host and project because a host has more than one place Claude writes
transcripts, and they are not the same kind of thing. The laptop has
``~/.claude/projects`` (19,540 files) and
``~/Library/Application Support/Claude/local-agent-mode-sessions``
(14 files), and the second one has never been indexed by anything,
because every scanner ever written looks only at the first. Folding it
into the first would make it invisible again by a different route.
``message_projects`` is keyed UNIQUE (corpus_id, slug), which is what
makes "same slug, different host" two rows BY CONSTRUCTION rather than
by a rule someone has to remember to apply.

ADDITIVE ONLY, like every step before it. Three CREATE TABLE, six ALTER
TABLE ADD COLUMN (SQLite's ADD COLUMN is a header-only rewrite, so this
is O(1) even on the 11 GB corpus database), four CREATE INDEX and two
CREATE VIEW. No existing column is altered, dropped, renamed or retyped,
and no existing row's meaning changes: a v16 row simply has NULL in the
new columns, which is the honest "not yet attributed" state and is
reported as CANNOT DETERMINE rather than defaulted to a host.
"""

from __future__ import annotations

from typing import Tuple

# ---------------------------------------------------------------------------
# The three dimension tables
# ---------------------------------------------------------------------------

DDL_MESSAGE_HOSTS = """
CREATE TABLE IF NOT EXISTS message_hosts (
  id                 INTEGER PRIMARY KEY,
  machine_id         TEXT NOT NULL UNIQUE,
  machine_id_scheme  TEXT NOT NULL
                      CHECK (machine_id_scheme IN ('platform_uuid',
                                                   'declared')),
  display_name       TEXT NOT NULL,
  hostname           TEXT,
  platform           TEXT,
  first_seen_at      TEXT NOT NULL
)
"""

#: ``machine_id_scheme`` is stored rather than implied because a platform
#: uuid and an operator-declared string are not the same quality of fact
#: and a reader must not have to guess which one a row holds from the
#: shape of the string.

DDL_MESSAGE_CORPORA = """
CREATE TABLE IF NOT EXISTS message_corpora (
  id            INTEGER PRIMARY KEY,
  host_id       INTEGER NOT NULL REFERENCES message_hosts(id),
  corpus_key    TEXT NOT NULL,
  root_path     TEXT NOT NULL,
  manifest_sha  TEXT,
  collected_at  TEXT NOT NULL,
  UNIQUE (host_id, corpus_key)
)
"""

DDL_MESSAGE_PROJECTS = """
CREATE TABLE IF NOT EXISTS message_projects (
  id             INTEGER PRIMARY KEY,
  corpus_id      INTEGER NOT NULL REFERENCES message_corpora(id),
  slug           TEXT NOT NULL,
  observed_cwd   TEXT,
  first_seen_at  TEXT NOT NULL,
  UNIQUE (corpus_id, slug)
)
"""


# ---------------------------------------------------------------------------
# Attributing the existing v16 transcript table
# ---------------------------------------------------------------------------
#
# ``source_path`` is the path RELATIVE TO ITS CORPUS ROOT, which is the
# only form that means the same thing on both machines. It is kept
# separate from v16's ``source_ref`` rather than replacing it, because
# ``source_ref`` carries a UNIQUE constraint that is now doing a
# different job: with two hosts a bare relative path is no longer unique,
# so ``source_ref`` becomes the globally unique locator (host, corpus and
# path together) and ``source_path`` holds the part a human recognises.
# Overloading one column to be both would have forced a choice between a
# key that collides and a path that is unreadable.

ALTER_TRANSCRIPTS: Tuple[str, ...] = (
    "ALTER TABLE message_transcripts ADD COLUMN host_id INTEGER "
    "REFERENCES message_hosts(id)",
    "ALTER TABLE message_transcripts ADD COLUMN corpus_id INTEGER "
    "REFERENCES message_corpora(id)",
    "ALTER TABLE message_transcripts ADD COLUMN project_id INTEGER "
    "REFERENCES message_projects(id)",
    "ALTER TABLE message_transcripts ADD COLUMN source_path TEXT",
    "ALTER TABLE message_transcripts ADD COLUMN host_attribution TEXT",
    "ALTER TABLE message_transcripts ADD COLUMN project_attribution TEXT",
)

#: The permitted values, declared here so the writer and the tests read
#: one list. They are NOT a CHECK constraint: SQLite cannot add a CHECK
#: to an existing table without rebuilding it, and rebuilding an 11 GB
#: table to police a string is a worse trade than validating on write.
HOST_ATTRIBUTION_VALUES: Tuple[str, ...] = (
    "manifest_verified", "declared", "cannot_determine",
)

#: ``derived`` - the corpus layout named a project directory and the slug
#: was read off it. ``none_declared`` - this corpus genuinely has no
#: project layer at that path (an audit log, for instance), which is a
#: fact about the source, not a failure. ``cannot_determine`` - a project
#: layer was expected and could not be read. Never collapsed into
#: ``none_declared``.
PROJECT_ATTRIBUTION_VALUES: Tuple[str, ...] = (
    "derived", "none_declared", "cannot_determine",
)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
#
# The unique index on (corpus_id, source_path) is the constraint that
# says a file is stored once per corpus. It tolerates the pre-existing
# all-NULL v16 rows because SQLite treats NULLs in a UNIQUE index as
# distinct - which is normally a hazard and is here exactly right: rows
# that have not been attributed yet must not collide with each other.

DDL_IX_TRANSCRIPTS_HOST = (
    "CREATE INDEX IF NOT EXISTS ix_message_transcripts_host "
    "ON message_transcripts (host_id)"
)

DDL_IX_TRANSCRIPTS_PROJECT = (
    "CREATE INDEX IF NOT EXISTS ix_message_transcripts_project "
    "ON message_transcripts (project_id)"
)

DDL_UX_TRANSCRIPTS_CORPUS_PATH = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_message_transcripts_corpus_path "
    "ON message_transcripts (corpus_id, source_path)"
)

DDL_IX_PROJECTS_SLUG = (
    "CREATE INDEX IF NOT EXISTS ix_message_projects_slug "
    "ON message_projects (slug)"
)


# ---------------------------------------------------------------------------
# Views - attribution for bodies and sessions, derived not stored
# ---------------------------------------------------------------------------
#
# A BODY IS NOT OWNED BY ONE HOST AND MUST NOT BE GIVEN A host_id COLUMN.
# A body is content identity: the same message copied to the other
# machine is the SAME body row, correctly, and it genuinely came from
# both. Its attribution is therefore a SET of hosts, which is what these
# views return. Storing it as a column would have forced a first-writer-
# wins answer that is wrong for every copied session - and the copied
# sessions are the whole reason the owner wanted two hosts in one
# database. A view also cannot drift from the appearance rows it is
# computed from, which a materialised table would.

DDL_VIEW_BODY_HOSTS = """
CREATE VIEW IF NOT EXISTS message_body_hosts AS
SELECT DISTINCT a.body_id AS body_id, t.host_id AS host_id
  FROM message_appearances a
  JOIN message_transcripts t ON t.id = a.transcript_id
 WHERE a.body_id IS NOT NULL AND t.host_id IS NOT NULL
"""

#: The cross-host session view. A session uuid arriving from two hosts is
#: NOT a collision and is deliberately not gated: uuid4 carries 122
#: random bits and 19,403 session uuids were measured with zero
#: duplicates, so a repeat is the owner's own conversation copied between
#: his machines. This view is how that is counted rather than guessed at.
DDL_VIEW_SESSION_HOSTS = """
CREATE VIEW IF NOT EXISTS message_session_hosts AS
SELECT session_ref            AS session_ref,
       session_ref_scheme     AS session_ref_scheme,
       COUNT(DISTINCT host_id) AS host_count,
       COUNT(*)               AS transcript_count
  FROM message_transcripts
 WHERE host_id IS NOT NULL
 GROUP BY session_ref, session_ref_scheme
"""


#: Ordered DDL for a v16 -> v17 database.
DDL_V17: Tuple[str, ...] = (
    DDL_MESSAGE_HOSTS,
    DDL_MESSAGE_CORPORA,
    DDL_MESSAGE_PROJECTS,
) + ALTER_TRANSCRIPTS + (
    DDL_IX_TRANSCRIPTS_HOST,
    DDL_IX_TRANSCRIPTS_PROJECT,
    DDL_UX_TRANSCRIPTS_CORPUS_PATH,
    DDL_IX_PROJECTS_SLUG,
    DDL_VIEW_BODY_HOSTS,
    DDL_VIEW_SESSION_HOSTS,
)

#: Table names v17 creates, in creation order. The migration test asserts
#: against THIS rather than re-listing them, so the two cannot drift.
V17_TABLE_NAMES: Tuple[str, ...] = (
    "message_hosts",
    "message_corpora",
    "message_projects",
)

#: Columns v17 adds to message_transcripts, in the order added.
V17_TRANSCRIPT_COLUMNS: Tuple[str, ...] = (
    "host_id", "corpus_id", "project_id", "source_path",
    "host_attribution", "project_attribution",
)

#: Views v17 creates.
V17_VIEW_NAMES: Tuple[str, ...] = (
    "message_body_hosts", "message_session_hosts",
)
