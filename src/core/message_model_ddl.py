"""DDL for schema v16: the message identity / appearance model.

WHY THIS IS ITS OWN MODULE AND NOT db_models.py. Every earlier version's
DDL lives in ``src/core/db_models.py``, and that is where a reader will
look first. db_models.py is already 1,286 lines, well past this repo's
500-line file cap (CLAUDE.md), so folding eight more tables into it would
push a file that is already over the limit further over it. The DDL for
v16 therefore lives here, and db_models.py keeps only the version
constant. This is pure data, exactly like db_models.py: no connection
logic, no migration driver, no I/O.

WHAT v16 ADDS, AND WHY THE SHAPE IS WHAT IT IS. A message uuid is NOT a
row key. Measured on the owner's claude_history database (9.8 GB,
2026-08-29): 3,004,324 message rows carry 2,262,902 distinct uuids -
509,613 excess rows, about 18%. That is not corruption. Claude Code
replays a prior conversation verbatim, with the ORIGINAL uuids, into a
resumed session's file and into a subagent's sidechain file. The copies
differ only in their ENVELOPE - a raw-JSON diff of one pair found 12 top
level keys byte-identical (message, sessionId, uuid, timestamp,
parentUuid, cwd, gitBranch, slug, permissionMode, type, userType,
version) with only ``isSidechain`` and ``agentId`` differing. The
difference between two copies of a message IS the parent/child
relationship.

So the model splits a message in two:

  ``message_bodies``       one row per DISTINCT BODY, keyed by
                           (uuid, sha256-of-body). Two different bodies
                           under one uuid are two rows, never a merge and
                           never a keep-first - losing either is data
                           loss, and it is a real case: an independent
                           re-measurement here of 3,443 duplicate-uuid
                           groups that had raw JSON on both sides found
                           39 (1.13%) with genuinely different bodies.
  ``message_appearances``  one row per (transcript, line). Holds
                           seq_in_file, is_sidechain, agent_id, the
                           envelope values, the ORIGINAL top-level key
                           order, and the sha256 of the original line.
                           This is where the subagent linkage stops being
                           an implicit near-duplicate row and becomes an
                           explicit, queryable edge.

NOTHING IS DROPPED BECAUSE IT FAILS TO PARSE OR TO LINK. A blank line and
a line whose JSON does not parse both get an appearance row (with
``line_status`` saying which, ``body_id`` NULL and ``raw_line`` holding
the bytes), because byte-exact export of the whole transcript depends on
them and because "we could not parse it" is not a reason to discard it.

BYTE-EXACT EXPORT WITHOUT A SECOND COPY OF THE BYTES. 100.0000% of
134,464 sampled lines regenerate byte-exact from parsed JSON using one of
two serializer styles (2026-08-11 audit; re-verified here at
20,000/20,000 for the ``compact`` style). So an appearance stores a style
marker plus the sha256 of the original line, and export re-renders and
verifies against that hash. ``raw_line`` is populated ONLY when
re-rendering did not reproduce the hash - raw storage is the exception
that proves a failure, not the default.

ADDITIVE ONLY, like every step before it: eight CREATE TABLE and nine
CREATE INDEX statements, each carrying its own IF NOT EXISTS. No existing
table is altered, no column added to one, nothing dropped, renamed or
retyped.
"""

from __future__ import annotations

from typing import Tuple

# ---------------------------------------------------------------------------
# Lookup tables - the repeating-value normalisation
# ---------------------------------------------------------------------------
#
# Measured cardinality across 3,004,324 rows on 2026-08-29: record_type 19
# distinct, role 2, model 13, compact_subtype 2. BE HONEST ABOUT THE SIZE
# EFFECT: replacing four short strings with four small integers is worth
# roughly 1% of the database, not a meaningful saving. These tables exist
# for CORRECTNESS and QUERYABILITY - an unknown record_type becomes an
# insert into a table a human can enumerate rather than a string nobody
# notices, a model rename becomes one row, and a filter becomes an
# integer comparison against an indexed FK instead of a string scan.

DDL_MESSAGE_RECORD_TYPES = """
CREATE TABLE IF NOT EXISTS message_record_types (
  id     INTEGER PRIMARY KEY,
  value  TEXT NOT NULL UNIQUE
)
"""

DDL_MESSAGE_ROLES = """
CREATE TABLE IF NOT EXISTS message_roles (
  id     INTEGER PRIMARY KEY,
  value  TEXT NOT NULL UNIQUE
)
"""

DDL_MESSAGE_MODELS = """
CREATE TABLE IF NOT EXISTS message_models (
  id     INTEGER PRIMARY KEY,
  value  TEXT NOT NULL UNIQUE
)
"""

DDL_MESSAGE_COMPACT_SUBTYPES = """
CREATE TABLE IF NOT EXISTS message_compact_subtypes (
  id     INTEGER PRIMARY KEY,
  value  TEXT NOT NULL UNIQUE
)
"""

#: The four lookup tables, as (table_name, DDL) pairs, so the ingest code
#: can intern a value without a four-way if/elif that could disagree with
#: this list.
LOOKUP_TABLES: Tuple[Tuple[str, str], ...] = (
    ("message_record_types", DDL_MESSAGE_RECORD_TYPES),
    ("message_roles", DDL_MESSAGE_ROLES),
    ("message_models", DDL_MESSAGE_MODELS),
    ("message_compact_subtypes", DDL_MESSAGE_COMPACT_SUBTYPES),
)


# ---------------------------------------------------------------------------
# Transcripts - the container an appearance belongs to
# ---------------------------------------------------------------------------
#
# THREE OUTCOMES, TWO OF THEM IDENTITY SCHEMES. Sessions are named either
# by a uuid (a real session) or by the literal form 'agent-a00fdb4' (a
# subagent session). ``session_ref_scheme`` records which one this
# transcript carries, so 'agent-...' is never treated as a malformed
# uuid and a uuid is never treated as an agent id. The scheme is a stated
# fact about the row, not something a later reader has to guess from the
# string's shape.
#
# 'opaque' is the THIRD value and it is a measurement: the ref carries no
# agent prefix AND is not a well-formed uuid. It exists because the
# classifier used to answer 'uuid' by elimination, which put 19 refs that
# were literal filename stems ('audit', 'journal') into the owner's own
# sessions count. See message_model_serialize.OPAQUE_SCHEME. Adding a
# value here is a schema change: the CHECK is relaxed IN PLACE by schema
# step 19 -> 20, which never rewrites this table (see
# src/core/message_scheme_repair.py for why a rebuild is unsafe here).
#
# ``newest_message_ts`` is WHEN THE OWNER WAS LAST WORKING IN THIS
# TRANSCRIPT, which is not ``ingested_at`` - that is when this tool read
# the file, and on the live corpus every one of 80 projects ingested on
# one of just two days, so it cannot order anything. The real signal is
# ``message_bodies.ts``, but bodies are deduplicated and reach a
# transcript only through ``message_appearances`` (3.1M rows), so
# computing it per request costs 3.9s warm and 14.9s cold against a 10ms
# route. It is therefore DERIVED ONCE and stored here, by ingest and by
# schema step 20 -> 21. A NULL is a MEASURED absence - both writers set
# the column for every transcript they touch, NULL included - so it means
# "this transcript's messages carry no timestamp" and never "nobody
# looked". See src/core/message_activity.py for the measurements and for
# the third outcome, which is a fact about the database rather than the
# row and is therefore carried beside the value, not inside it.

DDL_MESSAGE_TRANSCRIPTS = """
CREATE TABLE IF NOT EXISTS message_transcripts (
  id                    INTEGER PRIMARY KEY,
  source_ref            TEXT NOT NULL UNIQUE,
  session_ref           TEXT NOT NULL,
  session_ref_scheme    TEXT NOT NULL
                         CHECK (session_ref_scheme IN ('uuid', 'agent', 'opaque')),
  line_ending           TEXT NOT NULL
                         CHECK (line_ending IN ('LF', 'CRLF', 'MIXED', 'NONE')),
  has_trailing_newline  INTEGER NOT NULL
                         CHECK (has_trailing_newline IN (0, 1)),
  line_count            INTEGER NOT NULL DEFAULT 0,
  content_sha256        TEXT NOT NULL,
  raw_byte_length       INTEGER NOT NULL,
  ingested_at           TEXT NOT NULL,
  newest_message_ts     TEXT
)
"""


# ---------------------------------------------------------------------------
# Message identity
# ---------------------------------------------------------------------------
#
# ``identity_key`` is (message_uuid or '') || ':' || body_sha256, stored
# rather than computed, because SQLite treats NULLs as DISTINCT in a
# UNIQUE index: a UNIQUE (message_uuid, body_sha256) would silently allow
# unlimited duplicate rows for uuid-less records, which is the opposite of
# what this table is for. An explicit key has no NULL in it and therefore
# no exemption.
#
# ``body_json`` holds the record with the per-appearance envelope keys
# REMOVED (see message_model_serialize.APPEARANCE_KEYS), rendered with key
# order PRESERVED at every depth.
#
# TWO HASHES, AND THEY ANSWER DIFFERENT QUESTIONS. ``body_bytes_sha256``
# is taken over that order-preserving rendering and is what identity_key
# is built from, because a nested object's key order is part of what has
# to come back byte-exactly and two orderings therefore cannot share one
# stored row. ``body_sha256`` is taken over a CANONICAL (sorted) rendering
# and is what the duplicate-uuid conflict check compares, because "are
# these two copies the same MESSAGE?" is an order-insensitive question.
# Collapsing the two was tried first and broke export: bodies were stored
# with their nested keys sorted, so a record written role/model/content
# came back content/model/role - valid JSON, same meaning, wrong bytes.

DDL_MESSAGE_BODIES = """
CREATE TABLE IF NOT EXISTS message_bodies (
  id                    INTEGER PRIMARY KEY,
  identity_key          TEXT NOT NULL UNIQUE,
  message_uuid          TEXT,
  body_sha256           TEXT NOT NULL,
  body_bytes_sha256     TEXT NOT NULL,
  body_json             TEXT NOT NULL,
  record_type_id        INTEGER REFERENCES message_record_types(id),
  role_id               INTEGER REFERENCES message_roles(id),
  model_id              INTEGER REFERENCES message_models(id),
  compact_subtype_id    INTEGER REFERENCES message_compact_subtypes(id),
  parent_uuid           TEXT,
  ts                    TEXT,
  origin_session_ref    TEXT,
  is_compact_boundary   INTEGER NOT NULL DEFAULT 0,
  secret_finding_count  INTEGER NOT NULL DEFAULT 0,
  first_seen_at         TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Appearances - one row per (transcript, line)
# ---------------------------------------------------------------------------
#
# ``seq_in_file`` is kept SEPARATE from ``line_no`` deliberately.
# ``line_no`` is this model's own dense 0-based position and is what makes
# export deterministic; ``seq_in_file`` is the source's own claimed
# ordinal, which is measurably NOT dense - 636 sessions have a duplicate
# seq_in_file value (895 excess rows) and one has a gap. Overloading one
# column to mean both would have forced a choice between refusing real
# data and lying about the ordering.

DDL_MESSAGE_APPEARANCES = """
CREATE TABLE IF NOT EXISTS message_appearances (
  id                 INTEGER PRIMARY KEY,
  transcript_id      INTEGER NOT NULL
                      REFERENCES message_transcripts(id) ON DELETE CASCADE,
  line_no            INTEGER NOT NULL,
  seq_in_file        INTEGER,
  line_status        TEXT NOT NULL
                      CHECK (line_status IN ('ok', 'blank', 'invalid_json')),
  body_id            INTEGER REFERENCES message_bodies(id),
  envelope_json      TEXT,
  key_order_json     TEXT,
  serializer_style   TEXT,
  line_sha256        TEXT NOT NULL,
  line_byte_length   INTEGER NOT NULL,
  raw_line           TEXT,
  fidelity_outcome   TEXT NOT NULL
                      CHECK (fidelity_outcome IN
                             ('fidelity_verified', 'fidelity_failed',
                              'fidelity_unverifiable')),
  is_sidechain       INTEGER NOT NULL DEFAULT 0,
  agent_id           TEXT,
  UNIQUE (transcript_id, line_no)
)
"""


# ---------------------------------------------------------------------------
# Findings - gate conditions and secret material
# ---------------------------------------------------------------------------
#
# ``condition_code`` is validated against message_gate_contract.BY_CODE by
# the writer, not by a CHECK constraint, because the contract is the one
# authority for that vocabulary and duplicating it in DDL would create a
# second declaration that can drift from the first.

DDL_MESSAGE_INGEST_FINDINGS = """
CREATE TABLE IF NOT EXISTS message_ingest_findings (
  id              INTEGER PRIMARY KEY,
  observed_at     TEXT NOT NULL,
  condition_code  TEXT NOT NULL,
  severity        TEXT NOT NULL CHECK (severity IN ('stop', 'advisory')),
  subject_kind    TEXT NOT NULL
                   CHECK (subject_kind IN ('transcript', 'body', 'appearance')),
  subject_id      INTEGER NOT NULL,
  detail          TEXT NOT NULL
)
"""

#: NO MATCHED VALUE IS EVER STORED HERE, only where it was and a hash of
#: it. The hash exists so the same credential appearing in many records is
#: enumerable as ONE credential (which is what makes the eventual rotation
#: a clean cut) without the database becoming a second place the secret
#: lives. Redacting on the way in was rejected: it would break byte-exact
#: fidelity, which is the whole point of the model.
DDL_MESSAGE_SECRET_FINDINGS = """
CREATE TABLE IF NOT EXISTS message_secret_findings (
  id            INTEGER PRIMARY KEY,
  body_id       INTEGER NOT NULL
                 REFERENCES message_bodies(id) ON DELETE CASCADE,
  detector      TEXT NOT NULL,
  match_offset  INTEGER NOT NULL,
  match_length  INTEGER NOT NULL,
  value_sha256  TEXT NOT NULL,
  observed_at   TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

DDL_IX_MESSAGE_BODIES_UUID = (
    "CREATE INDEX IF NOT EXISTS ix_message_bodies_uuid "
    "ON message_bodies (message_uuid)"
)

DDL_IX_MESSAGE_BODIES_SHA = (
    "CREATE INDEX IF NOT EXISTS ix_message_bodies_sha "
    "ON message_bodies (body_sha256)"
)

DDL_IX_MESSAGE_BODIES_PARENT = (
    "CREATE INDEX IF NOT EXISTS ix_message_bodies_parent "
    "ON message_bodies (parent_uuid)"
)

DDL_IX_MESSAGE_APPEARANCES_BODY = (
    "CREATE INDEX IF NOT EXISTS ix_message_appearances_body "
    "ON message_appearances (body_id)"
)

DDL_IX_MESSAGE_APPEARANCES_AGENT = (
    "CREATE INDEX IF NOT EXISTS ix_message_appearances_agent "
    "ON message_appearances (agent_id)"
)

DDL_IX_MESSAGE_TRANSCRIPTS_SESSION = (
    "CREATE INDEX IF NOT EXISTS ix_message_transcripts_session "
    "ON message_transcripts (session_ref)"
)

DDL_IX_MESSAGE_INGEST_FINDINGS_CODE = (
    "CREATE INDEX IF NOT EXISTS ix_message_ingest_findings_code "
    "ON message_ingest_findings (condition_code)"
)

DDL_IX_MESSAGE_INGEST_FINDINGS_SUBJECT = (
    "CREATE INDEX IF NOT EXISTS ix_message_ingest_findings_subject "
    "ON message_ingest_findings (subject_kind, subject_id)"
)

DDL_IX_MESSAGE_SECRET_FINDINGS_BODY = (
    "CREATE INDEX IF NOT EXISTS ix_message_secret_findings_body "
    "ON message_secret_findings (body_id)"
)


#: Ordered DDL for a v15 -> v16 database. Every statement carries its own
#: IF NOT EXISTS, so the step needs no PRAGMA inspection to be safe on a
#: retry - the same idiom v7/v8/v14 already use for the same reason.
DDL_V16: Tuple[str, ...] = (
    DDL_MESSAGE_RECORD_TYPES,
    DDL_MESSAGE_ROLES,
    DDL_MESSAGE_MODELS,
    DDL_MESSAGE_COMPACT_SUBTYPES,
    DDL_MESSAGE_TRANSCRIPTS,
    DDL_MESSAGE_BODIES,
    DDL_MESSAGE_APPEARANCES,
    DDL_MESSAGE_INGEST_FINDINGS,
    DDL_MESSAGE_SECRET_FINDINGS,
    DDL_IX_MESSAGE_BODIES_UUID,
    DDL_IX_MESSAGE_BODIES_SHA,
    DDL_IX_MESSAGE_BODIES_PARENT,
    DDL_IX_MESSAGE_APPEARANCES_BODY,
    DDL_IX_MESSAGE_APPEARANCES_AGENT,
    DDL_IX_MESSAGE_TRANSCRIPTS_SESSION,
    DDL_IX_MESSAGE_INGEST_FINDINGS_CODE,
    DDL_IX_MESSAGE_INGEST_FINDINGS_SUBJECT,
    DDL_IX_MESSAGE_SECRET_FINDINGS_BODY,
)

#: Table names v16 creates, in creation order. Used by the migration test
#: to assert every one exists after the step, without that test having to
#: re-list them (a second list that can drift from this one).
V16_TABLE_NAMES: Tuple[str, ...] = (
    "message_record_types",
    "message_roles",
    "message_models",
    "message_compact_subtypes",
    "message_transcripts",
    "message_bodies",
    "message_appearances",
    "message_ingest_findings",
    "message_secret_findings",
)
