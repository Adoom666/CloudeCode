"""Schema definition for cloude.db - DDL, version constant, table names.

This module is pure data. It holds no connection logic, no migration
driver, and no I/O, so it can be imported by a test, a CLI entry point,
or a documentation generator without touching the filesystem.

SCOPE OF SCHEMA v1. Three tables:

  ``meta``            key/value scalars, including ``schema_version``.
  ``migration_trail`` the queryable MIRROR of migration_trail.jsonl.
  ``projects``        one row per project root (design doc section 3.2).
                       Added in build step S3. ``config.json`` stays
                       authoritative for writes; this table shadows it
                       and is read by src/core/project_store.py and the
                       GET /projects/presence route.

SCOPE OF SCHEMA v2. One more table:

  ``sessions``        one row per session (design doc section 3.3).
                       Added in build step S4, as its OWN additive step
                       (v1 -> v2) rather than folded into v1, because by
                       the time it was written v1 had shipped WITH a
                       reader: src/core/project_store.py and the
                       GET /projects/presence route both depend on it.
                       That is the exact condition that closes v1 to
                       further edits.

``projects`` was folded into v1 rather than opening a v2 migration
because, at the time it was added, schema v1 had shipped with no reader
depending on it yet (confirmed against HEAD before this table was
added) - the same condition db_migration.py's docstring already treats
as the bar for "still extendable". Sessions and everything else in
section 3.3 remain deferred to their own additive steps; this file's
job is still to grow ADDITIVELY, never to retype what shipped before.

ADDITIVE ONLY. Every future step may CREATE a table, CREATE an index, or
ALTER TABLE ADD COLUMN. No step may ever drop, rename or retype anything.
A step must inspect ``sqlite_master`` / ``PRAGMA table_info`` before it
acts so that re-running it after an interrupted attempt is a no-op rather
than an error - see src/core/db_migration.py.
"""

from __future__ import annotations

from typing import Tuple

# The schema version this code knows how to produce and read.
#
# Bumping this REQUIRES adding a matching _step_vN_to_vM function to
# src/core/db_migration.py's STEPS table in the same commit. The two are
# cross-checked by a test, because a bumped constant with no step is a
# database that can never reach the version the code demands.
CURRENT_SCHEMA_VERSION: int = 25

# meta keys this schema version defines. Listed so a reader does not have
# to grep for string literals to learn what can be in the table.
META_SCHEMA_VERSION = "schema_version"
META_CREATED_AT = "created_at"
META_INSTALL_ID = "install_id"

# Recognised values for migration_trail.kind. 'code' entries are written
# by scripts/upgrade.sh and scripts/rollback.sh, not by the app, which is
# what makes the trail unified rather than two parallel histories.
TRAIL_KINDS: Tuple[str, ...] = ("bootstrap", "config", "schema", "import", "code")

# meta keys the S3 config-projects import stage reads and writes. Named
# exactly per design section 3.1's DDL comment so a later step (S4-S7,
# which imports sessions/themes/unread state) can extend the SAME flag
# rather than invent a second "have we imported yet" marker.
META_IMPORTED_FROM_JSON_AT = "imported_from_json_at"
META_IMPORTED_FROM_JSON_RESULT = "imported_from_json_result"

# projects.source - where a row came from. 'config_import' is written
# once, at first run, by src/core/project_store.py's import step.
# 'adoption' is reserved for the client/js/launchpad.js:953-973 side
# effect (design section 3.2); not written by anything in this step.
#
# 'transcript_import' is written by scripts/import_transcript_sessions.py
# for a directory that a real Claude Code conversation ran in and that no
# existing project contains. It is NOT 'config_import' (that names the
# one-shot first-run read of config.json) and NOT 'adoption' (that names
# the launcher side effect); saying either would be a claim about where
# the row came from that is simply false, and `source` exists to answer
# exactly that question. Such a project is created ARCHIVED - see the
# importer - so it stays off the user's screens until he asks for it.
PROJECT_SOURCE_CONFIG_IMPORT = "config_import"
PROJECT_SOURCE_USER = "user"
PROJECT_SOURCE_ADOPTION = "adoption"
PROJECT_SOURCE_TRANSCRIPT_IMPORT = "transcript_import"
PROJECT_SOURCES: Tuple[str, ...] = (
    PROJECT_SOURCE_CONFIG_IMPORT,
    PROJECT_SOURCE_USER,
    PROJECT_SOURCE_ADOPTION,
    PROJECT_SOURCE_TRANSCRIPT_IMPORT,
)

# projects.presence - the four-state model, design section 4.1. 'missing'
# and 'unreachable' must never collapse into each other: the first means
# a positively-absent entry (ENOENT, parent readable), the second means
# the probe could not tell (EACCES, EPERM, ELOOP, ENOTDIR, an unmounted
# volume, or a stat that timed out). See src/core/project_presence.py.
PROJECT_PRESENCE_PRESENT = "present"
PROJECT_PRESENCE_MISSING = "missing"
PROJECT_PRESENCE_UNREACHABLE = "unreachable"
PROJECT_PRESENCE_UNCHECKED = "unchecked"
PROJECT_PRESENCE_STATES: Tuple[str, ...] = (
    PROJECT_PRESENCE_PRESENT,
    PROJECT_PRESENCE_MISSING,
    PROJECT_PRESENCE_UNREACHABLE,
    PROJECT_PRESENCE_UNCHECKED,
)

# Recognised values for migration_trail.status. Exactly two of these
# CLOSE an entry in the normal path; 'interrupted' closes one that was
# never closed by the process that opened it.
TRAIL_STATUS_STARTED = "started"
TRAIL_STATUS_COMPLETED = "completed"
TRAIL_STATUS_FAILED = "failed"
TRAIL_STATUS_INTERRUPTED = "interrupted"
TRAIL_STATUS_COMPLETED_AFTER_INTERRUPT = "completed_after_interrupt"

TRAIL_STATUSES: Tuple[str, ...] = (
    TRAIL_STATUS_STARTED,
    TRAIL_STATUS_COMPLETED,
    TRAIL_STATUS_FAILED,
    TRAIL_STATUS_INTERRUPTED,
    TRAIL_STATUS_COMPLETED_AFTER_INTERRUPT,
)

# A status that ends the life of the entry_uuid it names. Anything with a
# 'started' line and no line in this set is an INTERRUPTED step: the
# process died between announcing the work and recording its outcome.
TRAIL_CLOSING_STATUSES: Tuple[str, ...] = (
    TRAIL_STATUS_COMPLETED,
    TRAIL_STATUS_FAILED,
    TRAIL_STATUS_INTERRUPTED,
    TRAIL_STATUS_COMPLETED_AFTER_INTERRUPT,
)


# --- sessions (schema v2, design section 3.3) ------------------------------

# sessions.origin - REPLACES the in-memory ``owned_tmux_sessions`` set as
# the source of truth for "is this session ours". Three values, and the
# split between the first two is deliberately kept in the column even
# though the row badge collapses them (design section 4.6, decided
# 2026-08-17):
#
#   created   the app ran ``tmux new-session`` for it.
#   adopted   the user claimed a session the app did not start. Written
#             ONCE and never recomputed. NOT written by the S4 import -
#             past adoptions were never persisted anywhere, so importing
#             one would be inventing a fact.
#   observed  a session on our socket we have seen and never claimed.
#             The ONLY value that renders as external on a row.
#   imported  NO TMUX SESSION EVER EXISTED FOR THIS ROW as far as this
#             app is concerned. The row was reconstructed from a Claude
#             Code transcript on disk by
#             scripts/import_transcript_sessions.py: a conversation the
#             owner really had, in a real directory, that this install
#             never watched. It is a FOURTH kind and not a flavour of
#             `observed` - observed means we saw a live pane on our
#             socket and did not claim it, which is a measurement of a
#             process. An imported row has no pane, no socket presence
#             and no epoch, and never had one; it carries a
#             claude_session_uuid and nothing else that could identify a
#             process. Folding it into `observed` would put a row in the
#             "sessions on this socket we have not claimed" bucket that
#             is not on the socket at all.
SESSION_ORIGIN_CREATED = "created"
SESSION_ORIGIN_ADOPTED = "adopted"
SESSION_ORIGIN_OBSERVED = "observed"
SESSION_ORIGIN_IMPORTED = "imported"
SESSION_ORIGINS: Tuple[str, ...] = (
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_OBSERVED,
    SESSION_ORIGIN_IMPORTED,
)

# The origins that badge as OURS. Both of the first two, per 4.6 - an
# adopted session becomes ours for good. Kept as a tuple so no call site
# re-spells the membership test and drifts from the others; the badge was
# already hand-repaired across three sites once.
#
# `imported` IS DELIBERATELY NOT HERE, and the reason is what this tuple
# is read for: session_store.owned_names/owned_instances use it to answer
# "which tmux sessions on this socket are ours", and an imported row has
# no tmux session to own. Adding it would put a nameless row into a list
# of names.
SESSION_OWNED_ORIGINS: Tuple[str, ...] = (
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_ADOPTED,
)

# sessions.lifecycle - design section 4.2. 'unknown' is a FIRST-CLASS
# state, not a flavour of 'stopped': it means the probe did not answer,
# and a row in it offers NO lifecycle actions. Collapsing it into
# 'stopped' is the false-green that made a broken tmux render as
# "everything stopped" with a RESTART button that could not work.
SESSION_LIFECYCLE_RUNNING = "running"
SESSION_LIFECYCLE_STOPPED = "stopped"
SESSION_LIFECYCLE_UNKNOWN = "unknown"
SESSION_LIFECYCLES: Tuple[str, ...] = (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_LIFECYCLE_UNKNOWN,
)

# sessions.lifecycle_source - which measurement produced ``lifecycle``.
SESSION_LIFECYCLE_SOURCE_TMUX_LIST = "tmux_list"
SESSION_LIFECYCLE_SOURCE_PROBE_FAILED = "probe_failed"
SESSION_LIFECYCLE_SOURCE_TMUX_MISSING = "tmux_missing"
SESSION_LIFECYCLE_SOURCE_IMPORT = "import"

# sessions.project_attribution - design section 3.3 / 5.3 step 7.
# 'none' and 'unknown' are NOT the same answer and must never collapse:
# 'none' means we probed the working directory and it matched no known
# project root; 'unknown' means we could not read the working directory
# at all. Only 'unknown' lands the row in NEEDS ATTENTION, because only
# 'unknown' is a measurement we failed to take.
SESSION_ATTRIBUTION_EXPLICIT = "explicit"
SESSION_ATTRIBUTION_DERIVED_DEEPEST = "derived_deepest"
SESSION_ATTRIBUTION_NONE = "none"
SESSION_ATTRIBUTION_UNKNOWN = "unknown"
SESSION_ATTRIBUTIONS: Tuple[str, ...] = (
    SESSION_ATTRIBUTION_EXPLICIT,
    SESSION_ATTRIBUTION_DERIVED_DEEPEST,
    SESSION_ATTRIBUTION_NONE,
    SESSION_ATTRIBUTION_UNKNOWN,
)

# sessions.agent_family_source - how ``agent_family`` was resolved. An
# unresolved family renders as UNKNOWN and NEVER as 'claude', even though
# agent_families.DEFAULT_FAMILY is the right LAUNCH fallback. Launch
# fallback and display truth are different questions.
SESSION_FAMILY_SOURCE_WRAPPER = "wrapper"
SESSION_FAMILY_SOURCE_RESERVED_NAME = "reserved_name"
SESSION_FAMILY_SOURCE_FINGERPRINT = "fingerprint"
#: THE APP LAUNCHED IT AND KNOWS WHAT IT RAN. A fact, and the strongest
#: one available: the launcher resolved an ``agent_type``, built the
#: command from it and executed that command. Nothing was inferred.
#: Written only by the create path, which is the only code that can
#: honestly assert it.
SESSION_FAMILY_SOURCE_LAUNCHED = "launched"

#: THE APP MADE THE SESSION AND DELIBERATELY STARTED NO AGENT
#: (``auto_start_claude=False`` - a bare shell). Also a fact, and NOT the
#: same fact as ``unknown``: "there is no agent here" is a measurement,
#: "we do not know what is running here" is the absence of one. Keeping
#: them apart is what stops a shell rendering as a guessed agent.
SESSION_FAMILY_SOURCE_NOT_LAUNCHED = "not_launched"

#: COULD NOT DETERMINE. Reserved for a session the app never started, so
#: it has no launch choice to read. Never write this from a create path.
SESSION_FAMILY_SOURCE_UNKNOWN = "unknown"

# sessions.claude_session_uuid_source - how the uuid on THIS row was
# learned, mirroring the agent_family_source pattern above: the fact and
# its provenance are different questions, and a caller rendering a
# "restart this conversation" control needs to know which kind of
# evidence it is trusting.
#
# THREE STRENGTHS, NOT TWO. A uuid told to us directly by Claude Code
# (the SessionStart hook, via src/core/session_lineage.py) is a FACT: the
# CLI reported its own session id on a structured, versioned payload.
# Nothing correlated can outrank that.
#
# Below the hook, the two correlated sources are NOT equally strong, and
# collapsing them into one label would hide that. A uuid read from the
# pane's own process argv (`claude --resume <uuid>`, see
# src/core/claude_resume_argv.py and
# src/core/claude_session_correlate_ladder.py) is a DIRECT READ of what
# the process was actually told to open - the same kind of fact the hook
# reports, just read from `ps` instead of a hook POST, and materially
# stronger than any timestamp-based inference: it is what closes the gap
# for a RESUMED or RECOVERED session, which is the primary case this
# whole feature exists for (measured against the owner's live fleet
# 2026-08-29 - a resumed conversation predates the pane hosting it by
# construction, so no timing rule can ever find it).
#
# A uuid resolved by src/core/claude_transcript_correlate.py from
# filesystem TIMING evidence - working directory plus tmux creation time,
# matched against ~/.claude/projects/<slug>/*.jsonl, with no argv
# available - is the weakest of the three: an inference, strong enough to
# act on only when the candidate set was decisive (see that module), but
# never as strong as a direct read of either kind above.
SESSION_CLAUDE_UUID_SOURCE_HOOK = "hook"
SESSION_CLAUDE_UUID_SOURCE_CORRELATED_ARGV = "correlated_argv"
SESSION_CLAUDE_UUID_SOURCE_CORRELATED = "correlated"
SESSION_CLAUDE_UUID_SOURCES: Tuple[str, ...] = (
    SESSION_CLAUDE_UUID_SOURCE_HOOK,
    SESSION_CLAUDE_UUID_SOURCE_CORRELATED_ARGV,
    SESSION_CLAUDE_UUID_SOURCE_CORRELATED,
)

# The socket every session row defaults to. Stored per row rather than
# assumed globally because the instance identity triple starts with it.
DEFAULT_TMUX_SOCKET = "cloude"

# meta key recording the SESSIONS half of the first-run import when it
# could not run. Separate from META_IMPORTED_FROM_JSON_AT on purpose:
# the latch says the import COMPLETED, this says why a given attempt did
# not, and a reader must never infer one from the other.
META_SESSION_IMPORT_PENDING_REASON = "session_import_pending_reason"

# --- DDL ------------------------------------------------------------------

DDL_META = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
)
"""

# from_version / to_version are TEXT, not INTEGER, and that is not an
# oversight: a kind='code' entry's version is a git tag or semver string
# ("v0.8.2"), while a kind='schema' entry's is an integer. Widening the
# column is strictly better than forcing one type to serve both and
# parsing it back out at every read.
DDL_MIGRATION_TRAIL = """
CREATE TABLE IF NOT EXISTS migration_trail (
  id               INTEGER PRIMARY KEY,
  entry_uuid       TEXT NOT NULL UNIQUE,
  kind             TEXT NOT NULL,
  from_version     TEXT,
  to_version       TEXT,
  status           TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  completed_at     TEXT,
  backup_path      TEXT,
  backup_verified  INTEGER,
  app_version      TEXT,
  error            TEXT,
  detail           TEXT
)
"""

DDL_MIGRATION_TRAIL_KIND_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_migration_trail_kind "
    "ON migration_trail (kind)"
)

# projects - design section 3.2, verbatim except for the IF NOT EXISTS
# idempotence guard every step in this file carries. Column notes:
#
#   root      - identity. Path(raw_path).expanduser() normalised, NEVER
#               resolve() - see src/core/project_store.py's
#               normalize_root(), which is the one place this
#               normalisation happens. UNIQUE because the root IS the
#               project; a second config.json entry for the same root
#               is a duplicate, not a second project (see 3.2's dedupe
#               rule and project_store.import_from_config()).
#   raw_path  - the user's string, verbatim, never normalised.
#   presence  - default 'unchecked' (PROJECT_PRESENCE_UNCHECKED, see
#               above) because a freshly-imported row has not been
#               probed by this process yet - 'unchecked' is its own
#               state, not a stand-in for 'present'.
DDL_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
  id                  INTEGER PRIMARY KEY,
  root                TEXT NOT NULL UNIQUE,
  raw_path            TEXT NOT NULL,
  display_name        TEXT NOT NULL,
  description         TEXT,
  default_agent_type  TEXT,
  source              TEXT NOT NULL,
  presence            TEXT NOT NULL DEFAULT 'unchecked',
  presence_checked_at TEXT,
  presence_detail     TEXT,
  archived_at         TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
)
"""

# Ordered DDL for a v0 (empty file) -> v1 database. Kept as a tuple so
# _step_v0_to_v1 is a loop over declarations rather than a wall of
# inline SQL, and so a test can assert the exact set of objects v1
# creates without re-parsing the migration driver.
DDL_V1: Tuple[str, ...] = (
    DDL_META,
    DDL_MIGRATION_TRAIL,
    DDL_MIGRATION_TRAIL_KIND_INDEX,
    DDL_PROJECTS,
)

# What a REVERSE of v0 -> v1 would run. Recorded per section 5.1's
# "a REVERSAL_SQL block written down alongside" rule. It is NOT wired to
# any UI in this step: reversing to v0 means having no database at all,
# for which deleting the file is both simpler and exactly equivalent. It
# is written down so the idiom exists for step 2, which will need it.
REVERSAL_SQL_V1: Tuple[str, ...] = (
    "DROP TABLE IF EXISTS projects",
    "DROP INDEX IF EXISTS ix_migration_trail_kind",
    "DROP TABLE IF EXISTS migration_trail",
    "DROP TABLE IF EXISTS meta",
)


# sessions - design section 3.3, verbatim except the IF NOT EXISTS
# idempotence guard. See the column notes above DDL_SESSIONS_INSTANCE_INDEX
# for why the identity story is split between two columns and an index.
DDL_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
  id                    INTEGER PRIMARY KEY,
  session_uuid          TEXT NOT NULL UNIQUE,

  project_id            INTEGER REFERENCES projects(id),
  project_attribution   TEXT NOT NULL DEFAULT 'unknown',
  working_dir           TEXT,

  tmux_socket           TEXT NOT NULL DEFAULT 'cloude',
  tmux_name             TEXT,
  tmux_created_epoch    INTEGER,

  origin                TEXT NOT NULL,
  adopted_at            TEXT,
  legacy_session_id     TEXT,

  agent_type            TEXT,
  agent_family          TEXT,
  agent_family_source   TEXT NOT NULL DEFAULT 'unknown',
  model                 TEXT,

  claude_session_uuid   TEXT,
  -- HOW claude_session_uuid WAS LEARNED. 'hook' (Claude Code told us,
  -- via SessionStart), 'correlated_argv' (read from the pane's own
  -- process argv - --resume <uuid> - at adopt time, a direct read) or
  -- 'correlated' (inferred from filesystem transcript timing at adopt
  -- time when no argv was available - the weakest of the three). NULL
  -- alongside a NULL uuid means none of the three has ever run for this
  -- row. See db_models.py's SESSION_CLAUDE_UUID_SOURCE_* block.
  claude_session_uuid_source TEXT,
  parent_session_id     INTEGER REFERENCES sessions(id),
  fork_kind             TEXT,

  lifecycle             TEXT NOT NULL DEFAULT 'unknown',
  lifecycle_checked_at  TEXT,
  lifecycle_source      TEXT,
  last_seen_running_at  TEXT,

  archived_at           TEXT,
  pinned_theme          TEXT,
  audio_enabled         INTEGER,
  unread_auto           INTEGER NOT NULL DEFAULT 0,
  unread_manual         INTEGER NOT NULL DEFAULT 0,
  -- TWO NAMES, TWO OWNERS. `title` is the USER's label (v9 made it so).
  -- `claude_title` is whatever Claude Code currently calls the session on
  -- its own side, arriving on the SessionStart payload as `session_title`.
  -- They were one column until v10, which meant Claude's auto-generated
  -- name could become the user's displayed label, a user rename silently
  -- discarded Claude's, and - because the write was guarded write-once to
  -- stop exactly that - a LATER Claude-side /rename could never land.
  -- One field cannot hold two authorities. `claude_title` is free to
  -- change as often as Claude changes it; `title` changes only when the
  -- user says so.
  title                 TEXT,
  claude_title          TEXT,
  -- DURABLE ACTIVITY STATUS. The hook-derived state used to live only in
  -- SessionActivityTracker, an in-memory dict, so a server restart forgot
  -- what every session was doing. That is worse than it sounds here: the
  -- fallback is the tmux tier, and under this app's launch path
  -- pane_current_command is a CONSTANT, so a session whose state was
  -- forgotten reports `idle` forever and is indistinguishable from one
  -- genuinely sitting at a prompt.
  -- `activity_state_at` is what makes the value judgeable rather than
  -- merely present: a state with no timestamp cannot be told apart from a
  -- stale one, and a stale `working` is a lie about right now.
  activity_state        TEXT,
  activity_state_at     TEXT,
  -- WHEN WORK LAST HAPPENED IN THIS SESSION. Stamped ONLY from a Claude
  -- Code hook event that represents the conversation doing something
  -- (see claude_hooks.WORK_EVENTS): a prompt submitted, a tool called, a
  -- subagent run, a turn stopped, a permission asked for.
  --
  -- IT IS NOT AN "OPENED" TIME AND MUST NEVER BECOME ONE. Attaching to a
  -- session, selecting it in the sidebar, deep-linking to it or
  -- re-binding it after a server restart all leave this column alone.
  -- That is the entire reason it exists: the session list is read as a
  -- TIMELINE of what has been worked on, and a list that reshuffles when
  -- you merely look at a row destroys the recall it is read for.
  --
  -- IT IS NOT `updated_at` AND NOT `activity_state_at`. `updated_at` is
  -- written by unrelated bookkeeping, including a restart's rebind, so a
  -- restart would read as work. `activity_state_at` is written by the
  -- LISTING path too, whenever a settled state changes - including the
  -- working -> idle expiry, which is a session going quiet, not a
  -- session working.
  --
  -- NULL means NO WORK HAS BEEN RECORDED, which is a third outcome and
  -- not a zero: such a row sorts BELOW every row that has a value,
  -- in its own stable order, and is labelled as such in the UI rather
  -- than blended in at the bottom of the worked rows.
  last_work_at          TEXT,

  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
)
"""

# THE MOST LOAD-BEARING OBJECT IN THIS SCHEMA, and it costs one integer.
#
# A tmux NAME is not an identity: names are reusable, and the app itself
# re-mints them with a -2/-3 uniquifier. The TMUX INSTANCE is
# (tmux_socket, tmux_name, tmux_created_epoch), where the epoch is
# ``#{session_created}``.
#
# Without the epoch in the key, a session that died and had its name
# taken by a new one would MATCH the old row, and an UPDATE meant for the
# session the user adopted would land on a stranger's process - the user
# then sees a session badged as his that he never claimed. With the epoch
# in the key, the new instance simply does not match, so no statement
# touches the old row: it gets its own row, origin='observed', and the
# old row keeps its adoption history and falls to 'stopped'.
#
# PARTIAL INDEX. The WHERE clause is required, not cosmetic. A session
# row is allowed to exist with no tmux instance at all (an imported
# stopped session from session_metadata.json has no live name/epoch pair,
# design 5.3 step 5). SQLite treats every NULL as distinct in a UNIQUE
# index, so those rows would not collide anyway - but stating the
# predicate makes the index describe exactly the rows it is an identity
# for, and keeps it out of the way of the rows it is not.
DDL_SESSIONS_INSTANCE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_tmux_instance "
    "ON sessions (tmux_socket, tmux_name, tmux_created_epoch) "
    "WHERE tmux_name IS NOT NULL AND tmux_created_epoch IS NOT NULL"
)

DDL_SESSIONS_PROJECT_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions (project_id)"
)

DDL_SESSIONS_LIFECYCLE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_sessions_lifecycle ON sessions (lifecycle)"
)

DDL_SESSIONS_PARENT_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_sessions_parent "
    "ON sessions (parent_session_id) WHERE parent_session_id IS NOT NULL"
)

# Ordered DDL for a v1 -> v2 database. Additive only: one CREATE TABLE
# and four CREATE INDEX, every one guarded by IF NOT EXISTS so a re-run
# after an interrupted attempt finishes the rest and no-ops on the done.
DDL_V2: Tuple[str, ...] = (
    DDL_SESSIONS,
    DDL_SESSIONS_INSTANCE_INDEX,
    DDL_SESSIONS_PROJECT_INDEX,
    DDL_SESSIONS_LIFECYCLE_INDEX,
    DDL_SESSIONS_PARENT_INDEX,
)

# ---- schema v3: the session-id discriminator -------------------------------
#
# WHY A NEW VERSION RATHER THAN AN EDIT TO v2. v2 has shipped and has
# readers. Redefining it in place would leave any file already at v2
# describing a schema the code no longer expects, with nothing to detect
# the difference. So this is its own additive step, per this module's
# additive-only rule.
#
# WHAT IT ADDS, AND WHAT IT DELIBERATELY DOES NOT. One nullable column:
# ``tmux_session_id``, tmux's ``#{session_id}`` (``$0``, ``$1``, ...).
#
# THE IDENTITY KEY IS UNCHANGED. ux_sessions_tmux_instance still keys on
# (tmux_socket, tmux_name, tmux_created_epoch) and this step does not
# touch it. That is a decision, not an omission, and it cuts against the
# obvious reading that session_id is "the real identity":
#
#   session_id is unique per SERVER LIFETIME and restarts at $0 when the
#   server does. $3 today and $3 after a reboot are DIFFERENT sessions
#   with the same id. As a durable key stored on disk it is therefore
#   strictly WORSE than the creation epoch, which does not repeat.
#
#   It is strictly BETTER at separating two sessions that exist at the
#   same moment, which is the one thing a one-second epoch cannot do.
#
# So it is stored as a DISCRIMINATOR, never as a key: it can only ever
# cause a merge to be REFUSED (the stored row names a different live
# session than the one in front of us), never cause one to be accepted.
# A refusal on bad evidence costs one row; an acceptance on bad evidence
# hands one session's history and ownership badge to a stranger. The
# asymmetry decides which way an uncertain answer must fall.
#
# NULLABLE ON PURPOSE. Every row written before this column existed has
# NULL here, and NULL means "not recorded", never "different". The
# refusal fires only when BOTH sides carry an id and they disagree.
DDL_SESSIONS_ADD_SESSION_ID = (
    "ALTER TABLE sessions ADD COLUMN tmux_session_id TEXT"
)

#: Ordered DDL for a v2 -> v3 database. ALTER TABLE ADD COLUMN has no
#: IF NOT EXISTS in SQLite, so the step in db_steps.py inspects
#: PRAGMA table_info first; that inspection is what makes this idempotent,
#: not the statement itself.
DDL_V3: Tuple[str, ...] = (DDL_SESSIONS_ADD_SESSION_ID,)

#: SQLite cannot drop a column without rebuilding the table, and this
#: module is additive-only, so a REVERSE of v2 -> v3 is a RESTORE from the
#: verified backup rather than a statement. Stated as an empty tuple with
#: this comment rather than omitted, so the absence is a decision on the
#: record and not a gap someone fills in later with a DROP.
REVERSAL_SQL_V3: Tuple[str, ...] = ()

# --- v3 -> v4: projects.last_opened_at ------------------------------------
#
# WHY A COLUMN AND NOT A REUSE OF updated_at. feat/db-is-authoritative
# makes the projects table the source of truth for the launcher's project
# list, and that list is ordered most-recently-used first - the behaviour
# config.json got from its array order plus the old
# ``move_project_to_top``. The table had no field that could carry it.
#
# ``updated_at`` cannot: GET /projects/presence re-stats every root on
# every call and writes the result back, so updated_at is touched on a
# plain page load and would sort the list by "last probed" while claiming
# to sort it by "last opened".
#
# NULLABLE ON PURPOSE, and NULL means "never opened in this build", never
# "opened at the epoch". Rows imported from config.json carry NULL and
# fall back to their insert order, which IS the config array order, which
# was the user's MRU order - so the very first render after this
# migration shows exactly the order the old code showed.
DDL_PROJECTS_ADD_LAST_OPENED_AT = (
    "ALTER TABLE projects ADD COLUMN last_opened_at TEXT"
)

#: Ordered DDL for a v3 -> v4 database. Purely additive, one nullable
#: column, no index. As with v3, ALTER TABLE ADD COLUMN has no
#: IF NOT EXISTS, so the step inspects PRAGMA table_info first.
DDL_V4: Tuple[str, ...] = (DDL_PROJECTS_ADD_LAST_OPENED_AT,)

#: Same reasoning as REVERSAL_SQL_V3: additive-only forward, RESTORE
#: backward. Stated explicitly so the absence is a decision, not a gap.
REVERSAL_SQL_V4: Tuple[str, ...] = ()


# --- v4 -> v5: project_tombstones -----------------------------------------
#
# WHY THIS TABLE EXISTS. The projects import used to run once per install,
# behind the sessions latch, so a project the OLD version created during a
# downgrade never reached the table on re-upgrade and the next
# snapshot_projects() deleted it from config.json too. The fix is to
# reconcile config.json against the table on EVERY start - and that fix is
# only safe if the reconcile can tell these two apart:
#
#   a root absent because it was NEVER IMPORTED      -> import it
#   a root absent because the user DELETED it        -> leave it deleted
#
# Nothing in the v4 schema could. ``delete_project`` is a hard DELETE (see
# its docstring for why, and that reasoning still holds), so a deleted row
# leaves no trace anywhere: no ``deleted_at``, no trail entry, no archive.
# The two cases are BYTE-IDENTICAL to a set comparison. A reconcile built
# on the sets alone would resurrect every deleted project on the next
# start, trading one silent data defect for another.
#
# WHY A SEPARATE TABLE AND NOT A SOFT DELETE. Reusing ``archived_at``
# would leave the row in place, and the row holds the UNIQUE(root). A user
# who deleted a project and then added the same folder back would hit
# ProjectRootConflict from create_project() against a row nothing renders.
# A tombstone keeps ``projects`` meaning exactly what it meant before -
# every row is a live project - and keeps snapshot_projects() honest,
# since it builds config.json from that table and must not learn a second
# exclusion rule.
#
# root is the identity here for the same reason it is in ``projects``:
# display names are mutable and were never unique.
DDL_PROJECT_TOMBSTONES = """
CREATE TABLE IF NOT EXISTS project_tombstones (
  root         TEXT PRIMARY KEY,
  display_name TEXT,
  deleted_at   TEXT NOT NULL
)
"""

#: When deletion tracking began on THIS database, ISO-8601. Written once,
#: by the v4 -> v5 step.
META_PROJECT_TOMBSTONES_SINCE = "project_tombstones_since"

#: "1" when this database already held project history before the
#: tombstone table existed, so deletions made before that point left no
#: trace and CANNOT be told apart from a project that was never imported.
#: "0" when the database was created at v5 or later, where no such
#: deletion can exist and every absence is unambiguous.
META_PROJECT_TOMBSTONES_LEGACY_GAP = "project_tombstones_legacy_gap"

#: JSON list of roots the reconcile could not classify, captured on the
#: FIRST reconcile after the legacy gap was recorded. Finite and bounded:
#: a root outside this list appeared after tracking began and is therefore
#: unambiguous. See src/core/project_reconcile.py.
META_PROJECT_RECONCILE_UNDETERMINED = "project_reconcile_undetermined_roots"

#: JSON summary of the last reconcile, for GET /projects/authority.
META_PROJECT_RECONCILE_LAST = "project_reconcile_last"

#: Ordered DDL for a v4 -> v5 database. One new table, nothing altered.
DDL_V5: Tuple[str, ...] = (DDL_PROJECT_TOMBSTONES,)

#: A REVERSE of v4 -> v5 drops the table, which is exactly the inverse of
#: creating it. Unlike v3 and v4 this one CAN be stated, because the step
#: adds an object rather than a column - and dropping it destroys only the
#: record of which projects were deleted, never a project itself.
REVERSAL_SQL_V5: Tuple[str, ...] = ("DROP TABLE IF EXISTS project_tombstones",)


# What a REVERSE of v1 -> v2 would run. Dropping the table drops its
# indexes with it; the index drops are listed anyway so the block reads
# as the exact inverse of DDL_V2 rather than relying on a side effect.
REVERSAL_SQL_V2: Tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_sessions_parent",
    "DROP INDEX IF EXISTS ix_sessions_lifecycle",
    "DROP INDEX IF EXISTS ix_sessions_project",
    "DROP INDEX IF EXISTS ux_sessions_tmux_instance",
    "DROP TABLE IF EXISTS sessions",
)

# ---- schema v6: sessions.user_declined_at ---------------------------------
#
# THE ANSWER "LEAVE IT AS EXTERNAL" HAS TO BE DURABLE, OR IT IS NOT AN
# ANSWER. Stage C asks the user about every session the evidence ladder
# could not attribute. Two of the three answers write ``origin``, which
# is already durable. The third - "leave as external" - writes the SAME
# value the row already carries, so without a second field it is
# indistinguishable from "never asked" and the prompt returns on every
# boot until the user gives one of the other two answers. That is not a
# prompt, it is a nag with no off switch.
#
# ONE NULLABLE COLUMN, ISO-8601, NULL MEANING "NEVER ASKED OR NEVER
# ANSWERED". It is read by the Stage-D re-run gate, which re-examines
# only rows still at ``origin='observed'`` with this column NULL - so a
# later, better import can PROMOTE a row it can now prove, and can never
# re-ask a question the user has already closed.
DDL_SESSIONS_ADD_USER_DECLINED_AT = (
    "ALTER TABLE sessions ADD COLUMN user_declined_at TEXT"
)

#: Ordered DDL for a v5 -> v6 database. Additive, one nullable column.
#: ALTER TABLE ADD COLUMN has no IF NOT EXISTS in SQLite, so the step in
#: db_steps.py inspects PRAGMA table_info first - that inspection is what
#: makes it idempotent, not the statement.
DDL_V6: Tuple[str, ...] = (DDL_SESSIONS_ADD_USER_DECLINED_AT,)

#: SQLite cannot drop a column without rebuilding the table and this
#: module is additive-only, so a REVERSE of v5 -> v6 is a RESTORE from the
#: verified backup, exactly as v3 and v4 are. Stated rather than omitted
#: so the absence is a decision on the record.
REVERSAL_SQL_V6: Tuple[str, ...] = ()

#: The evidence ladder's key inside ``meta``: a JSON list of
#: ``{tmux_name, epoch, hints, reason}`` for every live session the ladder
#: could not attribute. THE THIRD OUTCOME, WRITTEN DOWN. Empty list and
#: absent key are different: absent means the ladder has never run.
META_SESSION_IMPORT_UNATTRIBUTED = "session_import_unattributed"

# ---- schema v7: an index on sessions.claude_session_uuid ------------------
#
# THE COLUMNS FOR LINEAGE SHIPPED IN v2 AND NOTHING EVER WROTE THEM.
# ``claude_session_uuid``, ``parent_session_id`` and ``fork_kind`` have
# been in DDL_SESSIONS since the table was created; the first two reads of
# them are added in this same change (src/core/session_lineage.py). What
# was missing is an index: the lineage write path's FIRST question on
# every Claude ``SessionStart`` is "does any row already carry this
# uuid", which is both the idempotence guard against a duplicate hook
# POST and the fork detector. Without an index that is a full table scan,
# and this table is the one that grows fastest once every fork is a row.
#
# NOT UNIQUE, DELIBERATELY. A UNIQUE index here would turn a duplicate
# hook delivery into an IntegrityError raised out of a telemetry write -
# an exception on a path whose entire contract is that it cannot disturb
# a live session. Uniqueness is enforced by the lookup that precedes the
# insert, where a collision is a NO-OP with a name (``continued``) rather
# than an error. Partial on NOT NULL because every pre-lineage row - and
# every session that is not a Claude session at all - carries NULL, and
# indexing those would be indexing the majority of the table to answer a
# question never asked of them.
DDL_SESSIONS_CLAUDE_UUID_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_sessions_claude_uuid "
    "ON sessions (claude_session_uuid) "
    "WHERE claude_session_uuid IS NOT NULL"
)

#: Ordered DDL for a v6 -> v7 database. Additive, one partial index, no
#: new column: CREATE INDEX IF NOT EXISTS is idempotent by the statement
#: itself, unlike every ALTER TABLE step before it.
DDL_V7: Tuple[str, ...] = (DDL_SESSIONS_CLAUDE_UUID_INDEX,)

#: A REVERSE of v6 -> v7 destroys no data at all - an index is derived,
#: not stored fact - so unlike v3..v6 this one has a real inverse.
REVERSAL_SQL_V7: Tuple[str, ...] = ("DROP INDEX IF EXISTS ix_sessions_claude_uuid",)

# sessions.fork_kind - HOW a new Claude session came out of the one
# before it in the same tmux session. Written only by
# src/core/session_lineage.py, and only on a row that also carries
# ``parent_session_id``.
#
# The vocabulary is Claude Code's own ``SessionStart.source``, verified
# against the shipped binary rather than taken from prose: the enum in
# 2.1.236 is ["startup", "resume", "clear", "compact", "fork"]. Only the
# values that can accompany a CHANGED session uuid are listed here.
# 'startup' and 'resume' never can - startup has no predecessor and
# resume continues the same uuid - so they are not fork kinds.
SESSION_FORK_KIND_FORK = "fork"
SESSION_FORK_KIND_CLEAR = "clear"
SESSION_FORK_KIND_COMPACT = "compact"

#: COULD NOT EVALUATE, as a stored value. A uuid provably changed, so a
#: new session exists and must get a row, but the ``source`` that came
#: with it is absent or is a string this build has never heard of - a
#: newer Claude Code adding a sixth kind is the expected way to get here.
#: Recorded rather than guessed at: writing 'fork' for an unrecognised
#: source would be inventing the one fact the column exists to hold.
#: A conversation that started WITHOUT the pane leaving the previous one
#: - Claude's own ``/fork``, which spawns a background agent and keeps
#: the terminal where it was.
#:
#: It is a real child of the conversation it was forked from, so it earns
#: a lineage row. What it must NOT do is become the lineage HEAD: the
#: pane is still running the parent, so the next event in that pane
#: belongs to the parent, not to this. Recording it as an ordinary fork
#: is what made a later /branch attach to the background agent instead of
#: to the conversation it actually branched from.
#:
#: Distinguished from a /branch purely by the hook sequence, measured
#: against 2.1.248:
#:   /branch -> SessionEnd(old) then SessionStart(new)   pane moved
#:   /fork   -> SessionStart(new) alone                  pane stayed
SESSION_FORK_KIND_BACKGROUND = "background"

SESSION_FORK_KIND_UNKNOWN = "unknown"

SESSION_FORK_KINDS: Tuple[str, ...] = (
    SESSION_FORK_KIND_FORK,
    SESSION_FORK_KIND_BACKGROUND,
    SESSION_FORK_KIND_CLEAR,
    SESSION_FORK_KIND_COMPACT,
    SESSION_FORK_KIND_UNKNOWN,
)

#: The unix epoch at or after which a tmux ``CLOUDECODE_ORIGIN`` marker is
#: admissible on THIS install - the moment a build carrying the Stage-A
#: create-path write site first ran here. Stamped once and never moved.
#: ABSENT MEANS CANNOT DETERMINE, which makes tier 4 inadmissible rather
#: than assumed valid; see src/core/session_stage_a_boundary.py.
META_STAGE_A_BOUNDARY_EPOCH = "stage_a_origin_marker_boundary_epoch"



# ---- schema v6 -> v7: the lineage lookup index ----------------------------
#
# THIS STEP IS NOT THIS FEATURE'S. It belongs to the session-lineage
# change, which took v7 first. It is reproduced here because this branch
# was cut from v1.0.3 (v6) and needs an unbroken 6 -> 7 -> 8 chain to be
# independently correct: a STEPS table with a hole at 6 cannot migrate any
# database at all, so omitting it would break every install rather than
# just this feature.
#
# THE MERGE RESOLUTION IS "KEEP EXACTLY ONE COPY", and it is safe either
# way because the statement is idempotent BY THE STATEMENT (CREATE INDEX
# IF NOT EXISTS), not by a PRAGMA inspection. Two copies running in
# sequence produce the same index and the same row count. If the two
# definitions ever disagree, lineage's is the authority - it owns the
# column being indexed.
DDL_SESSIONS_CLAUDE_UUID_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_sessions_claude_uuid "
    "ON sessions (claude_session_uuid) "
    "WHERE claude_session_uuid IS NOT NULL"
)

#: Ordered DDL for a v6 -> v7 database. Additive, one partial index, no
#: new column: CREATE INDEX IF NOT EXISTS is idempotent by the statement
#: itself, unlike every ALTER TABLE step before it.
DDL_V7: Tuple[str, ...] = (DDL_SESSIONS_CLAUDE_UUID_INDEX,)

#: A REVERSE of v6 -> v7 destroys no data at all - an index is derived,
#: not stored fact - so unlike v3..v6 this one has a real inverse.
REVERSAL_SQL_V7: Tuple[str, ...] = ("DROP INDEX IF EXISTS ix_sessions_claude_uuid",)


# ---- schema v7 -> v8: user-defined sidebar groups -------------------------
#
# WHY THIS IS IN THE DATABASE AND NOT IN config.json. Projects were moved
# to DB-only precisely because a second source of truth in config.json
# produced a UI that contradicted itself, and a group model carries the
# same hazard in the same place: the sidebar would have to decide which
# of two disagreeing lists of groups to draw. There is one list, it is
# here, and config.json never learns the word "group".
#
# MEMBERSHIP IS KEYED BY tmux_name, NOT BY sessions.id, and that is the
# decision worth reading. The obvious shape is a nullable
# ``sessions.group_id`` column, and it is wrong here: the sidebar's rows
# come from ``GET /sessions/attachable``, which is a live tmux probe, and
# a tmux session that this install has never adopted HAS NO ROW IN
# ``sessions`` at all. A foreign key to a table that cannot represent the
# thing being grouped is a model that silently cannot hold half the
# user's data - the same shape as this project's own CubeBackup and
# nut-b findings, where the thing that mattered was outside the structure
# doing the enumerating. tmux_name is also exactly the key the sidebar's
# existing pin set and manual order already use, so all three pieces of
# per-row state agree on what identifies a row.
#
# ONE GROUP PER SESSION, ENFORCED BY THE SCHEMA. ``tmux_name`` is the
# PRIMARY KEY of the membership table, so a second membership for the
# same session is impossible at the database level rather than by
# convention. See the module docblock in src/core/session_group_store.py
# for why one-per-session rather than many.
#
# DELETING A GROUP MUST NEVER DELETE A CONVERSATION. ``ON DELETE
# CASCADE`` here deletes the MEMBERSHIP row, which is the only thing this
# table owns; the session itself is not referenced by it and cannot be
# reached from it. So the strongest destructive act available - a raw
# ``DELETE FROM session_groups`` with no application code involved -
# returns those sessions to ungrouped and can do nothing worse. The API
# path does the same thing explicitly; this is the guarantee underneath
# it, not a substitute for it.
DDL_SESSION_GROUPS = """
CREATE TABLE IF NOT EXISTS session_groups (
  id         INTEGER PRIMARY KEY,
  group_uuid TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  position   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT
)
"""

DDL_SESSION_GROUP_MEMBERS = """
CREATE TABLE IF NOT EXISTS session_group_members (
  tmux_name  TEXT PRIMARY KEY,
  group_id   INTEGER NOT NULL REFERENCES session_groups(id) ON DELETE CASCADE,
  added_at   TEXT NOT NULL
)
"""

DDL_SESSION_GROUPS_POSITION_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_session_groups_position "
    "ON session_groups (position)"
)

DDL_SESSION_GROUP_MEMBERS_GROUP_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_session_group_members_group "
    "ON session_group_members (group_id)"
)

#: Ordered DDL for a v7 -> v8 database. Two new tables and two indexes,
#: nothing altered and no column added to an existing table - so, like
#: v7 and unlike v3..v6, every statement is idempotent BY THE STATEMENT
#: and the step needs no PRAGMA inspection to be safe on a retry.
DDL_V8: Tuple[str, ...] = (
    DDL_SESSION_GROUPS,
    DDL_SESSION_GROUP_MEMBERS,
    DDL_SESSION_GROUPS_POSITION_INDEX,
    DDL_SESSION_GROUP_MEMBERS_GROUP_INDEX,
)

#: A REVERSE of v7 -> v8 drops both tables, which is the exact inverse of
#: creating them. It destroys the user's groups and their membership -
#: real loss, named in REVERSAL_DESTROYS - and destroys no conversation,
#: because neither table holds one.
REVERSAL_SQL_V8: Tuple[str, ...] = (
    "DROP TABLE IF EXISTS session_group_members",
    "DROP TABLE IF EXISTS session_groups",
)

#: The longest a group name may be. Not a UI nicety: an unbounded name is
#: an unbounded row and an unbounded header, and the sidebar has one
#: column of width to render it in.
SESSION_GROUP_NAME_MAX = 40

#: The most groups one install may hold. A sidebar with more section
#: headers than rows has stopped being a sidebar, and a bound here is
#: what stops a stuck client from growing the table without limit.
SESSION_GROUP_MAX = 50


# Columns a REVERSE of each step permanently destroys, keyed by the
# to_version it undoes. Used to generate the typed-confirmation text in
# section 9.5 ("permanently deletes the values stored in: ..."), which
# must name the real columns rather than a static string. v1 drops whole
# tables rather than columns, so its entry names the tables.
REVERSAL_DESTROYS: dict = {
    1: (
        "meta (whole table)",
        "migration_trail (whole table)",
        "projects (whole table)",
    ),
    2: ("sessions (whole table)",),
    3: ("sessions.tmux_session_id",),
    5: ("project_tombstones (whole table)",),
    6: ("sessions.user_declined_at",),
    # v7 adds an INDEX, not a column. A reverse drops derived data and
    # destroys no stored value, so the tuple is deliberately empty rather
    # than absent: absent would read as "nobody considered this step".
    7: (),
    8: (
        "session_groups (whole table)",
        "session_group_members (whole table)",
    ),
}


# ---- schema v11 -> v12: one conversation, one row, ENFORCED -------------
#
# THE OWNER'S REQUIREMENT, VERBATIM: "everything should be stored and
# parented by id... if we do 1 for 1, this should never be an issue." Up
# to v11 that was a CONVENTION the code hoped for: ix_sessions_claude_uuid
# (v7, above) is a PLAIN index, so nothing in SQLite stops a second row
# from claiming a claude_session_uuid another row already holds. The only
# thing enforcing 1:1 was src/core/session_lineage.py checking
# row_for_claude_uuid() before every write - correct today, but a
# convention a future write path can violate by simply forgetting to
# check, with no error and no test failure until someone notices the
# data is wrong. This step turns that convention into a constraint the
# database itself refuses to violate.
#
# WHY v7's "NOT UNIQUE, DELIBERATELY" WAS RIGHT THEN AND IS SUPERSEDED
# HERE. That comment's fear was real: a UNIQUE index would turn a
# duplicate hook delivery into an IntegrityError out of a telemetry
# write. It is superseded because record_claude_session's idempotence
# check (row_for_claude_uuid, inside the SAME transaction as the write)
# already makes a duplicate delivery a no-op BEFORE any INSERT is
# attempted - the uuid is never re-offered to the database at all, so
# the constraint below has nothing to collide with on that path. See
# tests/test_claude_session_uuid_unique.py for the empirical proof this
# holds across every reachable lineage transition, not just the argument.
#
# FAIL-SOFT, LIKE EVERY STEP IN THIS CHAIN, BUT WITH A TWIST: this step
# must not raise, because the caller (db_steps.run_chain, driven from
# src/core/db_migration.py) commits every step from `current` to
# CURRENT_SCHEMA_VERSION as ONE transaction - an exception here would
# roll back not just this step but every step before it in the same
# migration run, and leave the whole app in DEGRADED_MIGRATION_FAILED /
# read-only. A DUPLICATE FOUND ON A LIVE DATABASE IS NOT A REASON TO DO
# THAT. src/core/db_steps.py detects duplicates BEFORE issuing the
# CREATE UNIQUE INDEX, and when it finds any, it leaves
# ix_sessions_claude_uuid (the v7 plain index) in place, does not attempt
# to create ux_sessions_claude_uuid at all, and records the exact
# uuid/row-id groups it found under META_SESSIONS_CLAUDE_UUID_DUPLICATES
# - a named COULD NOT EVALUATE, per the three-outcome rule, never a
# silently-picked winner and never a crash.
DDL_SESSIONS_CLAUDE_UUID_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_claude_uuid "
    "ON sessions (claude_session_uuid) "
    "WHERE claude_session_uuid IS NOT NULL"
)

#: The v7 plain index this step replaces once the UNIQUE index above is
#: successfully created. Dropped rather than kept alongside, because a
#: plain index and a unique index over the same column answer the same
#: query and keeping both would just be a second index to maintain on
#: every lineage write for no reader that needs it.
DDL_SESSIONS_CLAUDE_UUID_PLAIN_INDEX_DROP = (
    "DROP INDEX IF EXISTS ix_sessions_claude_uuid"
)

#: Present only when this step has RUN at least once; absent means "this
#: install has never reached v12's duplicate check" (same absent-vs-empty
#: convention as META_SESSION_IMPORT_UNATTRIBUTED, above). Value is a
#: JSON list: ``[]`` means the check ran and found nothing, so
#: ux_sessions_claude_uuid is live and enforcing; a non-empty list is the
#: COULD-NOT-EVALUATE outcome, one ``{"claude_session_uuid", "row_ids",
#: "count"}`` entry per uuid the check found on more than one row, and on
#: that install the v7 plain index is still what is doing the work.
META_SESSIONS_CLAUDE_UUID_DUPLICATES = "sessions_claude_uuid_duplicates_v12"


# ---- schema v13 -> v14: transcript archive, byte-exact fidelity store ----
#
# THE OWNER'S REQUIREMENT, VERBATIM: "once we can confirm all my history is
# preserved, even without the web interface i can consider it 100% backed
# up... id like the ability to export a conversation from the database and
# its byte for byte intact for the ingested session". Measured against the
# real corpus (~/.claude/projects on mac-mini-m4, 1477 files, 111,061 JSONL
# lines, 2026-08-29): re-serializing a parsed line with json.dumps() came
# back byte-identical on 0 of 111,061 lines - every single one differs, all
# from json.dumps's default `", "` / `": "` separators versus the source's
# unspaced `","` / `":"`. Storing only parsed fields is therefore not an
# approximation of byte-exact, it is a guarantee that NO line round-trips.
#
# THE RESOLUTION: one copy, not two. ``content_gzip`` on
# ``transcript_archives`` holds the ENTIRE source file's ORIGINAL BYTES,
# zlib-compressed - export is a decompress, nothing is re-serialized, so
# CRLF, a missing trailing newline, a trailing blank line and a line that
# is not valid JSON all survive automatically, because none of them are
# interpreted to reconstruct the file. ``transcript_records`` holds no
# second copy of any line's content - only scalar fields DERIVED by
# parsing at ingest time (type, uuid, parentUuid, timestamp, byte range
# into the decompressed blob) so the database can be queried and searched
# without ever re-parsing the blob for a list view. Real-corpus
# measurement of zlib level 9 against the whole corpus: ratio ~3.3x
# (303 MB raw -> ~90 MB compressed). See
# src/core/transcript_archive.py for the ingest/export/verify code and
# scripts/transcript-archive/corpus_roundtrip_harness.py for the
# real-corpus proof run.
#
# ROOTING. A transcript file is not always attributable the moment it is
# read - it may name a Claude session uuid this database has never seen,
# or (for a subagent transcript) a parent this database cannot yet find.
# ``root_state`` is never a boolean: 'unrooted' (default at ingest,
# pending human attribution), 'rooted' (attribution recorded - a session
# transcript points at ``root_session_id``, a subagent transcript points
# at ``parent_archive_id``), or 'orphaned' (a human looked and found no
# root - terminal, so it stops appearing in the pending queue, but the
# row and its bytes are untouched; per this project's own rule, a check
# that never clears is furniture, and an unrootable item that keeps
# nagging forever is the same defect). transcript_root_decisions is an
# APPEND-ONLY audit trail of every attribution decision a human makes -
# never overwritten, so a later re-root is a new row, not an edit.
#
# NOTHING IS GUESSED. No step here, and no function in
# transcript_archive.py, ever assigns root_session_id or
# parent_archive_id without an explicit caller-supplied value - there is
# no heuristic that silently picks a "most likely" parent. That is the
# owner's stop-case, verbatim: "if a message or session cannot be rooted
# we stop and i can do an inspection and i can point to where it
# belongs."
DDL_TRANSCRIPT_ARCHIVES = """
CREATE TABLE IF NOT EXISTS transcript_archives (
  id                         INTEGER PRIMARY KEY,
  archive_uuid               TEXT NOT NULL UNIQUE,
  kind                       TEXT NOT NULL
                              CHECK (kind IN ('session', 'subagent')),
  source_path                TEXT NOT NULL,
  content_gzip                BLOB NOT NULL,
  content_sha256             TEXT NOT NULL,
  raw_byte_length            INTEGER NOT NULL,
  compressed_byte_length     INTEGER NOT NULL,
  line_ending                TEXT NOT NULL
                              CHECK (line_ending IN
                                     ('LF', 'CRLF', 'MIXED', 'NONE')),
  has_trailing_newline       INTEGER NOT NULL
                              CHECK (has_trailing_newline IN (0, 1)),
  trailing_blank_line_count  INTEGER NOT NULL DEFAULT 0,
  record_count               INTEGER NOT NULL DEFAULT 0,
  invalid_json_line_count    INTEGER NOT NULL DEFAULT 0,
  claude_session_uuid        TEXT,
  root_state                 TEXT NOT NULL DEFAULT 'unrooted'
                              CHECK (root_state IN
                                     ('unrooted', 'rooted', 'orphaned')),
  root_session_id            INTEGER
                              REFERENCES sessions(id),
  parent_archive_id          INTEGER
                              REFERENCES transcript_archives(id),
  ingested_at                TEXT NOT NULL,
  ingest_source_mtime        TEXT,
  rooted_at                  TEXT,
  rooted_by                  TEXT
)
"""

#: One row per JSONL line of a transcript, but only DERIVED scalar
#: fields - never a second copy of the line's own bytes. ``byte_offset``
#: / ``byte_length`` locate the line inside the DECOMPRESSED
#: ``transcript_archives.content_gzip`` blob, so a reader that wants the
#: raw bytes of one specific line can seek into the decompressed blob
#: rather than re-splitting the whole file.
DDL_TRANSCRIPT_RECORDS = """
CREATE TABLE IF NOT EXISTS transcript_records (
  id               INTEGER PRIMARY KEY,
  archive_id        INTEGER NOT NULL
                    REFERENCES transcript_archives(id) ON DELETE CASCADE,
  line_no          INTEGER NOT NULL,
  byte_offset      INTEGER NOT NULL,
  byte_length      INTEGER NOT NULL,
  status           TEXT NOT NULL
                    CHECK (status IN ('ok', 'invalid_json', 'blank')),
  record_type      TEXT,
  record_uuid      TEXT,
  parent_uuid      TEXT,
  ts               TEXT,
  UNIQUE (archive_id, line_no)
)
"""

#: APPEND-ONLY. Every attribution decision a human makes, kept forever -
#: a re-root is a new row, never an UPDATE over the previous decision.
DDL_TRANSCRIPT_ROOT_DECISIONS = """
CREATE TABLE IF NOT EXISTS transcript_root_decisions (
  id                  INTEGER PRIMARY KEY,
  archive_id           INTEGER NOT NULL
                       REFERENCES transcript_archives(id),
  decided_at          TEXT NOT NULL,
  decided_by          TEXT NOT NULL,
  action              TEXT NOT NULL
                       CHECK (action IN ('rooted', 'orphaned', 'reopened')),
  root_session_id     INTEGER,
  parent_archive_id   INTEGER,
  note                TEXT
)
"""

DDL_TRANSCRIPT_ARCHIVES_ROOT_STATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_transcript_archives_root_state "
    "ON transcript_archives (root_state)"
)

DDL_TRANSCRIPT_ARCHIVES_CLAUDE_UUID_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_transcript_archives_claude_uuid "
    "ON transcript_archives (claude_session_uuid)"
)

DDL_TRANSCRIPT_ARCHIVES_PARENT_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_transcript_archives_parent "
    "ON transcript_archives (parent_archive_id)"
)

DDL_TRANSCRIPT_RECORDS_UUID_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_transcript_records_uuid "
    "ON transcript_records (record_uuid)"
)

DDL_TRANSCRIPT_ROOT_DECISIONS_ARCHIVE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_transcript_root_decisions_archive "
    "ON transcript_root_decisions (archive_id)"
)

#: Ordered DDL for a v13 -> v14 database. Three new tables and five new
#: indexes, nothing altered on any existing table and no column added to
#: one - like v7/v8, every statement carries its own IF NOT EXISTS and
#: the step needs no PRAGMA inspection to be safe on a retry.
DDL_V14: Tuple[str, ...] = (
    DDL_TRANSCRIPT_ARCHIVES,
    DDL_TRANSCRIPT_RECORDS,
    DDL_TRANSCRIPT_ROOT_DECISIONS,
    DDL_TRANSCRIPT_ARCHIVES_ROOT_STATE_INDEX,
    DDL_TRANSCRIPT_ARCHIVES_CLAUDE_UUID_INDEX,
    DDL_TRANSCRIPT_ARCHIVES_PARENT_INDEX,
    DDL_TRANSCRIPT_RECORDS_UUID_INDEX,
    DDL_TRANSCRIPT_ROOT_DECISIONS_ARCHIVE_INDEX,
)


# ---- schema v14 -> v15: prefix dedupe for growing files, project rooting ----
#
# TWO INDEPENDENT ADDITIONS, both additive-only (ALTER TABLE ADD COLUMN plus
# new indexes; no existing column altered, no table rebuilt), because this
# project's own migration rule is that every step must be additive - see
# db_steps.py's module docstring.
#
# PART A - PREFIX DEDUPE. A growing JSONL transcript is APPENDED to, so an
# older ingested version's bytes are frequently a strict byte PREFIX of a
# later version's bytes. Before v15 every re-ingest of a changed file wrote
# a second full ``content_gzip`` copy, which is the dominant storage cost
# for a live, daily-growing transcript (measured: the corpus's largest
# single file is 72.7 MB). ``superseded_by_archive_id`` lets an OLD row's
# ``content_gzip`` be replaced with a near-empty sentinel once a NEWER row
# for the same ``source_path`` is proven (by an explicit byte comparison at
# ingest time, never assumed from size or mtime) to hold the old row's
# bytes as its own prefix - export walks this pointer forward to whichever
# row still holds real content, decompresses ONCE, and slices to the
# ORIGINAL row's own ``raw_byte_length`` (already stored, needed no new
# column). See src/core/transcript_archive.py's updated ``export_archive``
# and src/core/transcript_prefix_dedupe.py for the ingest-time comparison
# and the supersede write. ``growth_kind`` records the explicit finding
# rather than leaving it inferred: 'initial' (first version of this
# source_path - the correct default for every pre-v15 row, since each one
# WAS the only version at the time it was written), 'append' (proven
# prefix relationship, old row now superseded), or 'non_append_rewrite'
# (content changed but was NOT a byte-prefix extension - a truncation, a
# mid-file edit, or a rewriting tool; both full copies are kept, and this
# is a surfaced finding, never a silent fallback per the three-outcome
# rule).
#
# PART B - PROJECT-LEVEL ROOTING. A weaker, DISTINCT root than session-level
# rooting (``root_state`` / ``root_session_id`` / ``parent_archive_id``,
# unchanged by this migration). ``project_id`` on transcript_archives is
# populated only when a transcript's corpus-slug maps, unambiguously, to
# exactly one ``projects.root`` row (see
# src/core/transcript_project_root.py) - ``root_state`` keeps meaning
# EXACTLY what it always meant (session/subagent attribution truth), so a
# project-rooted-but-not-session-rooted archive still correctly shows
# root_state='unrooted' (session-level attribution is still genuinely
# pending) while also carrying a project_id (a real, weaker hint a human
# does not have to rediscover). The two axes cannot be confused because
# they live in different columns with different meanings, never
# overloading one flag to mean both. transcript_root_decisions gains its
# own nullable ``project_id`` so a project-level decision is recorded
# alongside session/subagent decisions using the SAME 'rooted' action
# value already used for both those kinds - disambiguated the same way
# session vs subagent already are, by which FK column is populated
# (root_session_id vs parent_archive_id vs this new project_id), not by
# a new action string (adding one would require rewriting the action
# CHECK constraint, which SQLite cannot do via ALTER TABLE ADD COLUMN
# without rebuilding the table - forbidden by this migration's
# additive-only rule). A later session-level rooting decision on an
# already project-rooted archive is a plain call to the existing
# root_archive() (moves root_state to 'rooted', writes a NEW
# transcript_root_decisions row) - the project_id column and its earlier
# decision row are left untouched, so the upgrade never loses the audit
# trail that got it there.
DDL_V15_TRANSCRIPT_ARCHIVES_SUPERSEDED_BY = (
    "ALTER TABLE transcript_archives ADD COLUMN superseded_by_archive_id "
    "INTEGER REFERENCES transcript_archives(id)"
)

DDL_V15_TRANSCRIPT_ARCHIVES_GROWTH_KIND = (
    "ALTER TABLE transcript_archives ADD COLUMN growth_kind TEXT NOT NULL "
    "DEFAULT 'initial' CHECK (growth_kind IN "
    "('initial', 'append', 'non_append_rewrite'))"
)

DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ID = (
    "ALTER TABLE transcript_archives ADD COLUMN project_id "
    "INTEGER REFERENCES projects(id)"
)

DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ROOTED_AT = (
    "ALTER TABLE transcript_archives ADD COLUMN project_rooted_at TEXT"
)

DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ROOTED_BY = (
    "ALTER TABLE transcript_archives ADD COLUMN project_rooted_by TEXT"
)

DDL_V15_TRANSCRIPT_ROOT_DECISIONS_PROJECT_ID = (
    "ALTER TABLE transcript_root_decisions ADD COLUMN project_id "
    "INTEGER REFERENCES projects(id)"
)

DDL_V15_TRANSCRIPT_ARCHIVES_SUPERSEDED_BY_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_transcript_archives_superseded_by "
    "ON transcript_archives (superseded_by_archive_id)"
)

DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_transcript_archives_project "
    "ON transcript_archives (project_id)"
)

#: Ordered DDL for a v14 -> v15 database. Six ALTER TABLE ADD COLUMN
#: statements plus two new indexes, nothing altered on any existing
#: column and no table rebuilt. Unlike v14's CREATE TABLE IF NOT EXISTS
#: idiom, ALTER TABLE ADD COLUMN has no IF NOT EXISTS in SQLite, so the
#: step function guards each one with PRAGMA table_info before running
#: it - same idiom v3/v10/v11/v13 already use for the same reason.
DDL_V15: Tuple[str, ...] = (
    DDL_V15_TRANSCRIPT_ARCHIVES_SUPERSEDED_BY,
    DDL_V15_TRANSCRIPT_ARCHIVES_GROWTH_KIND,
    DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ID,
    DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ROOTED_AT,
    DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_ROOTED_BY,
    DDL_V15_TRANSCRIPT_ROOT_DECISIONS_PROJECT_ID,
    DDL_V15_TRANSCRIPT_ARCHIVES_SUPERSEDED_BY_INDEX,
    DDL_V15_TRANSCRIPT_ARCHIVES_PROJECT_INDEX,
)


# ---------------------------------------------------------------------------
# v21 -> v22 - CONTENT-ADDRESSED INGEST IDEMPOTENCY
# ---------------------------------------------------------------------------
#
# THE INCIDENT THIS EXISTS FOR, MEASURED. transcript_corpus_ingest's
# idempotency key was ``(source_path, content_sha256)`` evaluated in that
# order: the path lookup came FIRST, and the hash was only ever compared
# when that lookup hit. So a file whose bytes this database already held,
# arriving under a source_path it had never seen, produced
# ``existing = None`` and fell straight through to a full second copy
# recorded as ``growth_kind='initial'``. When ``~/Development`` became a
# symlink every corpus slug directory under ``~/.claude/projects`` was
# renamed, every ``source_path`` changed, and 19,294 files whose
# ``content_sha256`` was ALREADY stored were archived a second time -
# 3.78 GB, the whole corpus held twice under two path encodings, with no
# error, no warning and no finding anywhere.
#
# WHY PATH CANONICALISATION IS NOT THE FIX, stated here so the next
# reader does not reach for it. The two encodings are two different
# DIRECTORY NAMES inside ~/.claude/projects (Claude Code derives a slug
# from the cwd, and the symlinked cwd produces a different slug), not two
# paths to one file. ``Path.resolve()`` has nothing to resolve: both
# directories exist, independently, side by side. Content addressing is
# the only key that recognises them as the same transcript.
#
# THE INDEX. ``content_sha256`` has been on transcript_archives since
# v14 and was never indexed, because until now nothing ever looked a row
# up by it - the only reads were by id or by source_path. The
# content-addressed check queries it on EVERY file of every pass, so
# without this index each pass is one full table scan per file.
DDL_V22_TRANSCRIPT_ARCHIVES_CONTENT_SHA_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_transcript_archives_content_sha "
    "ON transcript_archives (content_sha256)"
)

# WHY A NEW COLUMN AND NOT A NEW growth_kind VALUE. ``growth_kind`` carries
# a CHECK constraint listing exactly ('initial', 'append',
# 'non_append_rewrite'). SQLite cannot widen a CHECK with ALTER TABLE ADD
# COLUMN, and rebuilding transcript_archives is forbidden by this
# migration chain's additive-only rule (and would be reckless against a
# table holding the owner's whole history). A content-duplicate row is
# genuinely the FIRST archive for its source_path, so 'initial' is the
# honest growth_kind for it; what needed saying separately is WHY its
# ``content_gzip`` is a sentinel, and that is what this column says.
#
# NULL on every row that predates this and on every ordinary ingest. A
# non-null value means: this row stores no bytes of its own, its content
# lives at ``superseded_by_archive_id``, and the reason is named here
# rather than inferred from the shape of two other columns.
DDL_V22_TRANSCRIPT_ARCHIVES_DEDUPE_KIND = (
    "ALTER TABLE transcript_archives ADD COLUMN dedupe_kind TEXT"
)

#: The one value this codebase writes into ``dedupe_kind``: the row's
#: bytes were already stored, verbatim, under a different source_path.
DEDUPE_KIND_CONTENT_DUPLICATE = "content_duplicate"

#: Ordered DDL for a v21 -> v22 database. One index (idempotent by its
#: own IF NOT EXISTS) and one ALTER TABLE ADD COLUMN (guarded by
#: PRAGMA table_info in the step, same idiom as v3/v10/v11/v13/v15).
DDL_V22: Tuple[str, ...] = (
    DDL_V22_TRANSCRIPT_ARCHIVES_CONTENT_SHA_INDEX,
    DDL_V22_TRANSCRIPT_ARCHIVES_DEDUPE_KIND,
)


# --- v22 -> v23: sessions.last_work_at ------------------------------------
#
# THE ORDERING KEY FOR THE SESSION AND PROJECT LISTS, and the reason it is
# a new column rather than a reuse of one already present.
#
# The lists are read as a TIMELINE - "what have I been working on, most
# recent first" - and every existing candidate answers a DIFFERENT
# question:
#
#   projects.last_opened_at   is written by POST /sessions, i.e. by
#                             CLICKING a project. Ordering by it means the
#                             list reshuffles when you merely look at it.
#   sessions.updated_at       is unrelated bookkeeping. A restart's rebind
#                             writes it, so a restart would read as work.
#   sessions.activity_state_at is written by the listing path as well as
#                             the hook path, including the working -> idle
#                             expiry. Going quiet is not working.
#   sessions.last_seen_running_at is a tmux LIVENESS probe. It advances on
#                             a session nobody has touched in a week.
#
# So there was no column that meant "work happened here", and inferring
# one from a column that means something else is how an ordering silently
# stops describing what its label claims.
#
# NULLABLE, AND NULL IS A THIRD OUTCOME. It means no work has been
# recorded for this session yet - not "worked on at the epoch" and not
# "worked on now". Rows carrying NULL sort below every row that has a
# value, keep a stable order among themselves, and are LABELLED as
# unrecorded in the UI rather than being blended into the tail of the
# worked rows. Every row predating this migration carries NULL, which is
# the honest answer for all of them: nothing was measuring this before.
#
# NO INDEX. The sessions table is tens of rows on a real install (8 on the
# machine this was written against); an index would cost a write on every
# tool call to speed up a scan that never becomes expensive.
DDL_V23_SESSIONS_LAST_WORK_AT = (
    "ALTER TABLE sessions ADD COLUMN last_work_at TEXT"
)

#: Ordered DDL for a v22 -> v23 database. One ALTER TABLE ADD COLUMN,
#: guarded by PRAGMA table_info in the step because SQLite's ALTER TABLE
#: ADD COLUMN has no IF NOT EXISTS - same idiom as v3/v10/v11/v13/v15/v22.
DDL_V23: Tuple[str, ...] = (DDL_V23_SESSIONS_LAST_WORK_AT,)

#: Same reasoning as REVERSAL_SQL_V3 and V4: additive-only forward,
#: RESTORE backward. Stated so the absence is a decision, not a gap.
REVERSAL_SQL_V23: Tuple[str, ...] = ()


# ---- schema v23 -> v24: group membership keyed on DURABLE identity ------
#
# THE DEFECT THIS CLOSES. ``session_group_members`` (v8, above) declares
# ``tmux_name TEXT PRIMARY KEY`` - a SCHEMA asserting that an ephemeral
# tmux name IS a durable identity. tmux recycles a name the moment a
# session is recreated after its pane dies, so two rows with two
# histories legitimately share one name, and the primary key forces one
# of them to win. It is an ENFORCED INCORRECT contract, which is worse
# than no contract: every INSERT has to already have picked a winner.
# Measured on the owner's live database 2026-09-08 the collision is not
# hypothetical - ``cloude_Mac`` and ``cloude_Fantasy Football 2026`` each
# carry TWO sessions rows and each is a filed group member.
#
# It also makes a whole class of row UNFILEABLE. A session imported from
# a transcript has no tmux name at all, so under a name-keyed primary key
# it cannot be put in a group even in principle.
#
# THE NEW TABLE KEYS ON ``sessions.session_uuid``, which is NOT NULL
# UNIQUE and is one of the three durable keys this schema recognises (the
# other two being the instance triple and claude_session_uuid). One group
# per session is still enforced by the primary key, exactly as before -
# the rule did not change, only what identifies the session.
#
# ``position`` IS DURABLE ORDER WITHIN A GROUP, and it is new. Order
# within a group used to live in localStorage, which means it was
# per-device and was lost with the browser profile. It is stored here for
# the same reason membership is: it is a fact about the conversation the
# user arranged, not a property of the screen they arranged it on.
# DEFAULT 0 is deliberate - a tie is broken by ``session_uuid`` in the
# read, so a backfilled or hand-inserted row still has a TOTAL order
# rather than whatever sqlite returns.
#
# ADDITIVE, LIKE EVERY OTHER STEP. The v8 table is NOT dropped, renamed
# or retyped. It is left exactly as it is, read once by the backfill and
# never again, so a rollback is a RESTORE and never a hand-written
# reversal - see this module's DDL block for v3/v4 and db_steps.py's
# docstring for the whole argument.
DDL_SESSION_GROUP_MEMBERSHIP = """
CREATE TABLE IF NOT EXISTS session_group_membership (
  session_uuid TEXT PRIMARY KEY,
  group_id     INTEGER NOT NULL REFERENCES session_groups(id) ON DELETE CASCADE,
  position     INTEGER NOT NULL DEFAULT 0,
  added_at     TEXT NOT NULL
)
"""

#: The one index the read needs. ``list_groups`` buckets every membership
#: by group and orders within the bucket, so the composite covers both
#: halves of that query and a per-group count needs no table scan.
DDL_SESSION_GROUP_MEMBERSHIP_ORDER_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_session_group_membership_order "
    "ON session_group_membership (group_id, position)"
)

#: Ordered DDL for a v23 -> v24 database. One CREATE TABLE and one CREATE
#: INDEX, each carrying its own IF NOT EXISTS, so the step is idempotent
#: BY THE STATEMENT and needs no PRAGMA inspection - same shape as
#: v7/v8/v14/v16/v18/v19. The row backfill that follows them in the step
#: is idempotent separately, by INSERT ... WHERE NOT EXISTS.
DDL_V24: Tuple[str, ...] = (
    DDL_SESSION_GROUP_MEMBERSHIP,
    DDL_SESSION_GROUP_MEMBERSHIP_ORDER_INDEX,
)

#: A REVERSE of v23 -> v24 drops the new table, which is the exact
#: inverse of creating it. It destroys the memberships and the in-group
#: order recorded since the migration, and destroys no conversation - the
#: table holds no reference to one. The v8 table is untouched by both
#: directions, so a reversed install falls back to the membership
#: snapshot the migration read, which is stale but never wrong about a
#: session that has not moved.
REVERSAL_SQL_V24: Tuple[str, ...] = (
    "DROP TABLE IF EXISTS session_group_membership",
)

REVERSAL_DESTROYS[24] = ("session_group_membership (whole table)",)


# ---------------------------------------------------------------------------
# v24 -> v25: sessions.kind, so a list can be the OWNER'S work only
# ---------------------------------------------------------------------------
#
# THE OWNER'S RULE, VERBATIM (2026-09-08): "lists should always just be
# mine. the rest can be found in the archive explorer." After the
# transcript import gave every conversation on this machine a row, 895 of
# them, the lists carried work nobody sat at a keyboard for: 259
# scheduler runs and 11 headless `claude -p` probes, measured.
#
# THE VOCABULARY IS THREE WORDS AND A FOURTH IS A BUG: 'interactive',
# 'automated', 'unknown'. src/core/session_kind.py owns the ladder that
# produces them and the argument for each rung.
#
# NULLABLE, WITH NO SQL DEFAULT, AND THAT IS THE SAFETY PROPERTY. SQLite
# fills every existing row with a column default when one is given, which
# would have stamped 'interactive' on all 895 imported rows and left the
# backfill unable to tell "classified as the owner's" from "never
# looked at". NULL means exactly "not classified", and every reader
# excludes on `kind = 'automated'` ALONE - so NULL, 'unknown' and
# 'interactive' all keep their place in the lists. An unwritten value can
# never hide a session.
#
# NO INDEX, same reasoning as v23's last_work_at: the sessions table is
# under a thousand rows on the largest install measured, and the filter
# rides along on scans the lists already pay for.
DDL_V25_SESSIONS_KIND = "ALTER TABLE sessions ADD COLUMN kind TEXT"

#: Stamp the rows this app created or adopted ITSELF. Every one of them
#: was launched or attached by the owner at a keyboard, so 'interactive'
#: is a measurement about them rather than a guess - the app has no other
#: way to make a session. Imported rows are deliberately left NULL for
#: scripts/classify_session_kind.py, which reads the transcript.
DDL_V25_SESSIONS_KIND_BACKFILL = (
    "UPDATE sessions SET kind = 'interactive' "
    "WHERE kind IS NULL AND origin IN ('created', 'adopted')"
)

#: Ordered DDL for a v24 -> v25 database. One ALTER TABLE ADD COLUMN
#: guarded by PRAGMA table_info in the step (SQLite has no IF NOT EXISTS
#: for it - same idiom as v3/v10/v11/v13/v15/v22/v23), then one UPDATE
#: whose `kind IS NULL` clause makes it idempotent by itself.
DDL_V25: Tuple[str, ...] = (
    DDL_V25_SESSIONS_KIND,
    DDL_V25_SESSIONS_KIND_BACKFILL,
)

#: Additive-only forward, RESTORE backward - same as V3, V4 and V23.
#: Stated so the absence is a decision rather than a gap.
REVERSAL_SQL_V25: Tuple[str, ...] = ()
