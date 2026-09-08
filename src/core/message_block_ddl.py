"""DDL for schema v18: the derived content-block index.

WHAT THIS SOLVES. ``message_bodies.body_json`` holds an entire record as
one opaque TEXT column and is about 85% of the database. The scalar
fields are already normalised out of it (uuid, parent, ts, and four
integer FKs into small dimension tables). The CONTENT is not: every
question about ``message.content`` - "which messages spawned subagents",
"show me the tool calls", "order this message's subagent runs" - is a
2.4M row scan that parses 7.23 GiB of JSON to answer.

MEASURED ON THE OWNER'S CORPUS, 2026-09-01, 2,447,028 bodies:

  content is an array          1,302,387  (53.22%)  ->  1,303,116 blocks
  no ``message`` key at all    1,099,530  (44.93%)  ->  0 blocks
  content is a plain STRING       45,111  ( 1.84%)  ->  1 derived block
  unparseable / odd shape              0  ( 0.00%)

  Cross-check: 1,302,387 + 45,111 = 1,347,498, which is exactly
  assistant 845,778 + user 501,720. Every assistant and user body has
  ``message.content`` and no other record type does. The 1,099,530 with
  no ``message`` key are progress (917,436), attachment (73,012),
  queue-operation (50,163), system (26,091) and 12 smaller types, which
  carry ``data``, ``attachment`` or a TOP-LEVEL ``content`` instead.
  Those are deliberately OUT OF SCOPE here - they are a different shape
  and folding them in would put four unrelated things in one column.

  Blocks per body: min 1, p50 1, p95 1, p99 1, max 9. 1,301,852 of the
  1,302,387 array bodies hold exactly one block. The table is therefore
  roughly ONE row per content-bearing body, not the 10M row explosion
  the shape suggests.

  Block types, complete census: tool_result 452,443, tool_use 450,501,
  text 258,703, thinking 140,769, image 605, document 92, fallback 3.
  Seven types, no others.

THIS TABLE IS DERIVED AND IT IS NEVER AUTHORITATIVE. ``body_json``
remains the source of truth for byte-exact export. If the two ever
disagree, ``body_json`` wins and this table is rebuilt. Nothing in the
export path reads it - ``tests/test_message_block_derived_only.py``
asserts that structurally, by dropping the tables and exporting anyway.
That is the whole reason it may be rebuilt at will: dropping it can cost
query speed and can never cost data.

WHY A SEPARATE STATUS TABLE, AND NOT A NULLABLE COLUMN. Absence must
never read as emptiness. Three states are genuinely different and a
caller has to be able to tell them apart:

  body absent from message_body_block_status  NEVER PROCESSED
  status row, block_count 0                   processed, genuinely none
  status 'unparseable_body'                   COULD NOT EVALUATE

A single "0 blocks" answer collapses all three into the one that looks
like good news, which is the exact false-green this repo's THREE-OUTCOME
RULE exists to stop. The status table is also what makes the backfill
resumable: the work remaining is the antijoin against it, so an
interrupted run resumes instead of starting over.

``_string_content`` IS A DERIVED TYPE AND IS MARKED AS ONE. 45,111
bodies carry ``message.content`` as a plain string rather than an array.
The UI wants uniform access to "this message's text", so those get one
row at seq 0. Its type is spelled with a leading underscore because
there is no block in the source JSON at that position - a source type
and an invented one must not be indistinguishable in the dimension
table. The status row independently says ``content_string``.

TEXT IS PROJECTED, NOT COPIED. ``text`` holds only the human-readable
projection (see message_block_extract.project_block). An image's base64
source and a document's payload are NOT projected: they are bytes, not
text, and copying them would double the largest values in the corpus for
no query anyone wants. Measured projection total: 1,311,407,825
characters (1.221 GiB), of which tool_result is 792 MB, tool_use input
348 MB, text 125 MB, thinking 46 MB.

SECRETS. A block's text is a SUBSTRING of the body text that
``archive_snippet_gate`` already governs, so it inherits exactly the
same exposure and must inherit exactly the same gate. Every block row
carries ``body_id``, which is the key the gate's layer 1 already uses,
and its text is a valid gate WINDOW for layers 2 and 3. There is
therefore no new gate and no second policy - see
message_block_preview.gated_block_preview, which is the only supported
way to serve this text to a caller.
"""

from __future__ import annotations

from typing import Tuple

#: Bumped whenever :func:`message_block_extract.project_block` changes
#: what it would produce for the same input. Stored on every status row
#: so a projection change is a queryable set of stale rows rather than a
#: silent mixture of two vocabularies in one table.
EXTRACTOR_VERSION: int = 1

#: The derived type given to the single row minted for a body whose
#: ``message.content`` is a plain string. The leading underscore marks
#: it as not present in the source JSON.
DERIVED_TYPE_STRING_CONTENT = "_string_content"

#: The derived type given to a content array element that is not a JSON
#: object. Never observed in the measured corpus (0 occurrences) and
#: still represented, because "we have never seen it" is not "it cannot
#: happen" and the alternative is dropping a block silently.
DERIVED_TYPE_NON_OBJECT = "_non_object_block"

#: The derived type given to a block object with no ``type`` key.
DERIVED_TYPE_UNTYPED = "_untyped_block"

#: Every value ``message_body_block_status.status`` may hold. Kept here
#: rather than only in the CHECK constraint so the extractor and the
#: tests can enumerate them without re-listing a second copy.
STATUS_BLOCKS_EXTRACTED = "blocks_extracted"
STATUS_CONTENT_STRING = "content_string"
STATUS_NO_MESSAGE_CONTENT = "no_message_content"
STATUS_UNPARSEABLE_BODY = "unparseable_body"
STATUS_UNEXPECTED_CONTENT_SHAPE = "unexpected_content_shape"

BLOCK_STATUSES: Tuple[str, ...] = (
    STATUS_BLOCKS_EXTRACTED,
    STATUS_CONTENT_STRING,
    STATUS_NO_MESSAGE_CONTENT,
    STATUS_UNPARSEABLE_BODY,
    STATUS_UNEXPECTED_CONTENT_SHAPE,
)

#: The two statuses that mean "this body was looked at and the answer is
#: that it legitimately has no content blocks". Distinct from the two
#: that mean "we could not evaluate it".
STATUS_COULD_NOT_EVALUATE: Tuple[str, ...] = (
    STATUS_UNPARSEABLE_BODY,
    STATUS_UNEXPECTED_CONTENT_SHAPE,
)


DDL_MESSAGE_BLOCK_TYPES = """
CREATE TABLE IF NOT EXISTS message_block_types (
  id     INTEGER PRIMARY KEY,
  value  TEXT NOT NULL UNIQUE
)
"""

#: ``seq`` is the 0-based position in the source array, so ordering is
#: stable and does not depend on rowid allocation order. UNIQUE
#: (body_id, seq) is both the ordering guarantee and the idempotency
#: guarantee: re-running the extractor for one body cannot double it.
#:
#: ``tool_use_id`` deliberately carries TWO senses - a tool_use block's
#: own ``id`` and a tool_result block's ``tool_use_id``, the id it
#: answers - because that is precisely what makes the call/result join a
#: single indexed self-join rather than two columns a query has to
#: COALESCE. ``block_type_id`` disambiguates which sense a row is in.
#:
#: ``is_error`` is NULLABLE and NULL means THE KEY WAS ABSENT, which is
#: 179,223 of 452,443 tool_result blocks (39.6%). Defaulting those to 0
#: would assert every one of them succeeded, which nothing measured.
DDL_MESSAGE_CONTENT_BLOCKS = """
CREATE TABLE IF NOT EXISTS message_content_blocks (
  id             INTEGER PRIMARY KEY,
  body_id        INTEGER NOT NULL
                  REFERENCES message_bodies(id) ON DELETE CASCADE,
  seq            INTEGER NOT NULL,
  block_type_id  INTEGER NOT NULL REFERENCES message_block_types(id),
  text           TEXT,
  text_length    INTEGER NOT NULL DEFAULT 0,
  tool_name      TEXT,
  tool_use_id    TEXT,
  is_error       INTEGER CHECK (is_error IN (0, 1)),
  UNIQUE (body_id, seq)
)
"""

#: One row per body the extractor has looked at, whatever the answer
#: was. A body with no row here has NEVER BEEN PROCESSED, and that is a
#: different fact from having no blocks.
DDL_MESSAGE_BODY_BLOCK_STATUS = """
CREATE TABLE IF NOT EXISTS message_body_block_status (
  body_id            INTEGER PRIMARY KEY
                      REFERENCES message_bodies(id) ON DELETE CASCADE,
  status             TEXT NOT NULL
                      CHECK (status IN ('blocks_extracted', 'content_string',
                                        'no_message_content',
                                        'unparseable_body',
                                        'unexpected_content_shape')),
  block_count        INTEGER NOT NULL DEFAULT 0,
  detail             TEXT,
  extractor_version  INTEGER NOT NULL,
  processed_at       TEXT NOT NULL
)
"""

#: Joins a tool_result back to the tool_use it answers, and is what makes
#: "order this message's subagent runs" an index seek.
DDL_IX_MESSAGE_CONTENT_BLOCKS_TOOL_USE_ID = (
    "CREATE INDEX IF NOT EXISTS ix_message_content_blocks_tool_use_id "
    "ON message_content_blocks (tool_use_id)"
)

#: Answers "which messages spawned subagents" directly: the tool_use
#: rows whose tool_name is a subagent-spawning tool. Measured tool-name
#: cardinality on a 1% sample is 69 distinct names, so the second column
#: is highly selective within a type.
DDL_IX_MESSAGE_CONTENT_BLOCKS_TYPE_TOOL = (
    "CREATE INDEX IF NOT EXISTS ix_message_content_blocks_type_tool "
    "ON message_content_blocks (block_type_id, tool_name)"
)

#: Lets the backfill find its remaining work, and lets a reader count
#: could-not-evaluate bodies, without scanning message_bodies.
DDL_IX_MESSAGE_BODY_BLOCK_STATUS_STATUS = (
    "CREATE INDEX IF NOT EXISTS ix_message_body_block_status_status "
    "ON message_body_block_status (status)"
)


#: Ordered DDL for a v17 -> v18 database. Every statement carries its own
#: IF NOT EXISTS, so the step needs no PRAGMA inspection to be safe on a
#: retry - the same idiom v7/v8/v14/v16 already use for the same reason.
#: NOTHING HERE TOUCHES message_bodies: no ALTER, no UPDATE, no rewrite.
DDL_V18: Tuple[str, ...] = (
    DDL_MESSAGE_BLOCK_TYPES,
    DDL_MESSAGE_CONTENT_BLOCKS,
    DDL_MESSAGE_BODY_BLOCK_STATUS,
    DDL_IX_MESSAGE_CONTENT_BLOCKS_TOOL_USE_ID,
    DDL_IX_MESSAGE_CONTENT_BLOCKS_TYPE_TOOL,
    DDL_IX_MESSAGE_BODY_BLOCK_STATUS_STATUS,
)

#: Table names v18 creates, in creation order. Used by the migration test
#: so it does not re-list them (a second list that can drift from this).
V18_TABLE_NAMES: Tuple[str, ...] = (
    "message_block_types",
    "message_content_blocks",
    "message_body_block_status",
)

#: Index names v18 creates. Same reason as V18_TABLE_NAMES.
V18_INDEX_NAMES: Tuple[str, ...] = (
    "ix_message_content_blocks_tool_use_id",
    "ix_message_content_blocks_type_tool",
    "ix_message_body_block_status_status",
)
