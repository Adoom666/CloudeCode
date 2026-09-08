# JSONL shape inventory

What every distinct structural shape in the transcript archive is, how many
times each occurs, and one exemplar per shape so a test can fetch it.

**Purpose.** Export is byte-exact by reassembling a line from stored parts, so
a "shape" is any distinction that could change how a line reassembles or that
exercises a different code path. Enumerating them turns test coverage into
something provable rather than assumed, and gives a drift check something to
assert against when a new shape appears in future ingest.

**Measured 2026-08-31 17:00 to 17:02 UTC** against
`/Users/jsugamele/Scratch/llmScratch/cc-dev-state/cloude.db`.

**Method, because the numbers rot.** A background ingest scheduler writes to
this database every 15 minutes. Every count below is bounded by a watermark
taken once at the start (`message_appearances.id <= 3125122`,
`message_bodies.id <= 2447028`) and every phase reuses it, so the phases cannot
disagree with each other even though the live corpus has moved on since. To
re-measure, re-run the scripts and take a fresh watermark; do not compare a new
number against an old one without checking both watermarks.

**Read-only, and proven so.** The connection sets `PRAGMA query_only=ON` and
READS THE PRAGMA BACK, refusing to continue unless it reads 1. A negative
control then attempts a real `CREATE TABLE` and confirms it is refused
(`attempt to write a readonly database`). A pragma that silently did not take
is exactly the false green this guard exists to prevent, so the guard is
measured rather than assumed.

**Privacy.** This document contains structure and counts only. No message text,
no body content, no field values beyond enumerated structural tokens (record
type names, block type names, JSON key names, closed enum values). Any example
line shown is synthetic and labelled as such.

---

## Headline result

| Question | Answer (2026-08-31) |
|---|---|
| Distinct full signatures | **1,347** |
| Signatures that round-trip byte-exact | **1,347 of 1,347** |
| Signatures that fail round-trip | **0** |
| Signatures with no secret-free exemplar | **0** |
| Rare signatures (fewer than 10 occurrences) | **464** (34.4 percent of signatures, 0.045 percent of appearances) |
| Singleton signatures (exactly 1 occurrence) | **175** |
| Distinct top-level key orderings | **447** |

The rare tail is the finding that matters for test design. 464 of 1,347
signatures account for 1,414 of 3,125,122 appearances. A random sample of any
practical size misses essentially all of them, and they are disproportionately
the odd shapes most likely to break export.

## Corpus totals

| Table | Rows |
|---|---|
| `message_transcripts` | 21,039 |
| `message_appearances` | 3,125,122 |
| `message_bodies` | 2,447,028 |
| `message_secret_findings` | 12,390 |
| `message_ingest_findings` | 142,888 |

---

## What a signature is

The signature is the full cross-product of the dimensions below, not each
dimension in isolation. It is a sha256 over a canonical JSON rendering of the
dimension dict, truncated to 24 hex characters. It is deterministic and
order-stable, and it is recomputable by a test from live rows, which is what
makes drift detection possible.

**Appearance side** (12 dimensions): `line_status`, `serializer_style`,
`render_source` (raw stored line versus rendered), `body_row` (present or
NULL), `envelope_keys` (the key SET, never the values), `key_order_digest`,
`key_order_len`, `is_sidechain`, `agent_id_form`, `fidelity_outcome`,
`line_ending`, `has_trailing_newline`.

**Body side** (13 dimensions): `record_type`, `role`, `model`,
`compact_subtype`, `is_compact_boundary`, `parent_uuid` set/null, `ts`
set/null, `message_uuid` set/null, `content_shape`, `content_block_types`
(the sorted distinct SET of block `type` values), `usage_shape`,
`stop_reason`, `body_toplevel_type`.

`secret_finding_count` is deliberately NOT a signature dimension. Making it one
would double the signature space for a property that is about content rather
than structure. It is tracked per signature instead, as the availability of a
secret-free exemplar.

---

## Signature count distribution

| Occurrences | Signatures | Appearances covered |
|---|---|---|
| exactly 1 | 175 | 175 |
| 2 to 9 | 289 | 1,239 |
| 10 to 99 | 366 | 13,831 |
| 100 to 999 | 305 | 103,966 |
| 1,000 to 9,999 | 152 | 552,502 |
| 10,000 or more | 60 | 2,453,409 |

Sixty signatures cover 78.5 percent of the corpus. The remaining 1,287 cover
the rest, and 464 of those are rare.

Rare signatures broken down by record type: `assistant` 298, `user` 112,
`system` 34, `progress` 12, then single-digit counts for `frame-link`,
`file-history-delta`, `artifact-comment-monitor`, `result`, `cost-state`, and
one with a NULL record type.

---

## Dimension by dimension

### line_status

| Value | Count |
|---|---|
| `ok` | 3,125,121 |
| `invalid_json` | 1 |
| `blank` | **0** |

**Finding: `blank` is unrepresented.** The schema permits it and the parser
produces it, but this corpus contains no blank line. A test needing a blank-line
exemplar must synthesize one; it cannot be drawn from this archive.

### serializer_style

| Value | Count |
|---|---|
| `compact` | 3,124,401 |
| `spaced` | 720 |
| NULL | 1 |

**Finding: `compact_ascii` and `spaced_ascii` are unrepresented.** Two of the
four registered styles never occur. The `spaced` style does occur, on 720 rows,
which is worth knowing because the module docstring records a measurement of
20,000 of 20,000 lines reproducing under `compact` alone. That measurement was
a sample; at full-corpus scale the second style is real.

### The single anomalous row

Three separate anomalies turn out to be one row, which is worth stating plainly
because counting them separately would suggest three problems:

| Column | Value |
|---|---|
| appearance id | 1,392,773 |
| transcript_id / line_no | 9,378 / 0 |
| `line_status` | `invalid_json` |
| `serializer_style` | NULL |
| `raw_line` | present (the only one in the corpus) |
| `body_id` | NULL (the only one in the corpus) |
| `line_byte_length` | 14 |
| `key_order_json` | NULL |
| `fidelity_outcome` | `fidelity_verified` |

This is the model behaving exactly as designed: a line that does not parse is
not dropped, it gets an appearance row with the bytes kept in `raw_line`, and
export replays those bytes verbatim. It is the only row that exercises the
`raw_line` branch of `_render_row`, so it is the only exemplar available for
that entire code path.

### fidelity_outcome

| Value | Count |
|---|---|
| `fidelity_verified` | 3,125,122 |
| `fidelity_failed` | **0** |
| `fidelity_unverifiable` | **0** |

Every appearance in the corpus is fidelity-verified. There is nothing to name
as a failure. Both other outcomes are unrepresented and cannot be exemplified
from this archive.

### is_sidechain and agent_id

| `is_sidechain` | Count |
|---|---|
| 1 | 1,627,108 |
| 0 | 1,498,014 |

| `agent_id` | Count |
|---|---|
| set | 1,627,995 (18,271 distinct) |
| NULL | 1,497,127 |

Cross-checked: `is_sidechain=1 AND agent_id IS NULL` is 0 rows, and
`is_sidechain=0 AND agent_id IS NOT NULL` is 887 rows. That 887 matches the
`agentId`-only envelope count exactly, so the two measurements corroborate.

**Finding, and it corrects a prior claim.** The brief expected `agent:` and
`agent-` prefixes on agent ids with very different counts. Measured, neither
prefix occurs on `message_appearances.agent_id` at all. Zero agent ids contain
a colon. The prefixes live on `message_transcripts.session_ref`, a different
column in a different table, and there the split is not what was expected
either:

| `session_ref` prefix | Scheme | Count |
|---|---|---|
| `agent-` | `agent` | 19,588 |
| none (uuid) | `uuid` | 1,451 |
| `agent:` | | **0** |

`AGENT_REF_PREFIXES` in `message_model_serialize.py` documents `agent:` on
17,996 rows and `agent-` on 224, describing the older `sessions` table in
`claude_history`. In `message_transcripts` the `agent:` form does not appear
once and `agent-` carries all 19,588. Both constants are still correct to keep,
since the checker must tolerate either, but the counts in that comment do not
describe this table.

Agent id forms, characterized structurally (values never printed):

| Form | Rows | Distinct |
|---|---|---|
| `a` + 16 hex (length 17) | 1,059,961 | 12,775 |
| `a` + 6 hex (length 7) | 500,569 | 4,608 |
| `acompact-` + 16 hex (length 25) | 65,139 | 193 |
| length 25, mixed slug charset | 1,814 | 527 |
| `acompact-` + 6 hex (length 15) | 512 | 168 |

The only character outside `[a-z0-9-]` appearing in any agent id is the
underscore.

### envelope_json key sets

| Key set | Count |
|---|---|
| `isSidechain`, `agentId` | 1,627,108 |
| `isSidechain` | 1,250,031 |
| empty `{}` | 247,095 |
| `agentId` | 887 |
| NULL | 1 |

All five possible states of the closed two-key `APPEARANCE_KEYS` set occur,
including the `agentId`-without-`isSidechain` case, which is easy to assume
away.

### key_order_json

**447 distinct top-level key orderings**, the longest tail of any dimension.
Key counts per ordering range from 3 to 26.

The `SELECT COUNT(DISTINCT key_order_json)` form of this query returns 446,
because SQL `COUNT(DISTINCT ...)` excludes NULL. The NULL ordering belongs to
the single `invalid_json` row. 446 plus that one is 447. Both numbers are
right; they answer slightly different questions.

Head of the distribution (key names are structural tokens, so they are shown):

| Count | Keys | Ordering |
|---|---|---|
| 369,102 | 15 | parentUuid, isSidechain, userType, cwd, sessionId, version, gitBranch, agentId, slug, type, data, toolUseID, parentToolUseID, uuid, timestamp |
| 277,703 | 14 | same without agentId |
| 200,430 | 16 | parentUuid, isSidechain, promptId, agentId, type, message, uuid, timestamp, sourceToolAssistantUUID, userType, entrypoint, cwd, sessionId, version, gitBranch, slug |
| 174,346 | 16 | parentUuid, isSidechain, agentId, message, requestId, attributionAgent, type, uuid, timestamp, userType, entrypoint, cwd, sessionId, version, gitBranch, slug |
| 169,996 | 14 | as above without agentId and attributionAgent |
| 169,588 | 14 | parentUuid, isSidechain, userType, cwd, sessionId, version, gitBranch, agentId, slug, message, requestId, type, uuid, timestamp |
| 138,797 | 15 | ... type, data, parentToolUseID, toolUseID, timestamp, uuid |
| 125,916 | 17 | ... attributionAgent, type, uuid, timestamp, effort, userType, ... |

Tail: **137 of the 447 orderings occur fewer than 10 times, and 50 occur
exactly once.** Note the third and seventh entries above: the same key set in a
different order is a different ordering, and reassembly is order-driven, so
these are genuinely distinct reassembly paths and not cosmetic variants.

### line_ending x has_trailing_newline

The full cross product is 4 x 2 = 8 cells. **Two are occupied.**

| `line_ending` | `has_trailing_newline` | Transcripts |
|---|---|---|
| `LF` | 1 | 21,021 |
| `LF` | 0 | 18 |
| `CRLF` | either | **0** |
| `MIXED` | either | **0** |
| `NONE` | either | **0** |

**Finding: CRLF, MIXED and NONE are entirely unrepresented.** Six of the eight
cells cannot be exemplified from this corpus. This matters more than the other
gaps: `split_lines` and `join_lines` in `message_model_serialize.py` handle
only `"\n"`, so the CRLF path has no corpus coverage AND no obvious
implementation. Any test of CRLF behaviour must use a synthetic transcript, and
the question of what the code should even do there is open.

### record_type

All 26 interned record types occur. The lookup table's own comment records 19
distinct at an earlier measurement, so seven have been added since.

| Type | Bodies | Type | Bodies |
|---|---|---|---|
| `progress` | 917,436 | `rate_limit_event` | 50 |
| `assistant` | 845,778 | `ai-title` | 45 |
| `user` | 501,720 | `permission-mode` | 30 |
| `attachment` | 73,012 | `atis-latch` | 29 |
| `queue-operation` | 50,163 | `artifact-autoreact-ledger` | 24 |
| `system` | 26,091 | `tool_use_summary` | 11 |
| `last-prompt` | 18,144 | `frame-link` | 6 |
| `file-history-snapshot` | 9,892 | `file-history-delta` | 6 |
| `pr-link` | 1,831 | `bridge-session` | 4 |
| `summary` | 1,253 | `artifact-comment-monitor` | 3 |
| `started` | 474 | `agent-name` | 3 |
| `result` | 461 | `cost-state` | 1 |
| `custom-title` | 389 | | |
| `mode` | 172 | | |

`cost-state` occurs on exactly one body in the entire corpus. Nine record types
occur fewer than 50 times.

### role

| Value | Bodies | Share |
|---|---|---|
| NULL | 1,099,537 | 44.93 percent |
| `assistant` | 845,771 | 34.56 percent |
| `user` | 501,720 | 20.50 percent |

**The prior measurement of roughly 44.9 percent NULL is confirmed exactly.**
NULL is the plurality value, not an edge case: `role` lives inside the nested
`message` object and the majority record type (`progress`) has no `message`
object at all.

### model

Thirteen non-null values plus NULL, as expected.

| Model | Bodies | Model | Bodies |
|---|---|---|---|
| NULL | 1,601,250 | `claude-sonnet-5` | 52,413 |
| `claude-opus-4-8` | 187,873 | `claude-haiku-4-5-20251001` | 31,923 |
| `claude-opus-4-7` | 136,055 | `claude-sonnet-4-5-20250929` | 31,779 |
| `claude-opus-4-6` | 123,427 | `claude-fable-5` | 5,027 |
| `claude-opus-5` | 99,939 | `<synthetic>` | 1,694 |
| `claude-opus-4-5-20251101` | 98,851 | `claude-opus-4-5` | 360 |
| `claude-sonnet-4-6` | 76,430 | `nemotron-3-super` | 7 |

Two are worth flagging for anyone writing a model-aware test. `<synthetic>` is
a literal model name in the data, not a placeholder this document introduced.
`nemotron-3-super` occurs 7 times and is not an Anthropic model, so any code
that assumes a `claude-` prefix on this column is wrong 7 times over.

### compact_subtype and is_compact_boundary

| `compact_subtype` | Bodies |
|---|---|
| NULL | 2,445,582 |
| `isCompactSummary` | 739 |
| `compact_boundary` | 707 |

`is_compact_boundary` is 1 on 1,446 bodies, which is exactly 739 + 707, so the
derived flag and the subtype agree with no drift.

### Nullable scalars

| Column | NULL | Set |
|---|---|---|
| `ts` | **33,480** | 2,413,548 |
| `parent_uuid` | 104,790 | 2,342,238 |
| `message_uuid` | 83,160 | 2,363,868 |

**The prior measurement of 33,480 NULL timestamps is confirmed exactly.**

---

## Content shapes

### message.content

| Shape | Bodies |
|---|---|
| array | 1,302,387 |
| no `message` object at all | 1,099,530 |
| string | 45,111 |

**Finding: there is no body where `message` exists but `content` is absent,
null, a number, or an object.** Content is a string or an array, or the whole
`message` object is missing. That is a narrower state space than the schema
permits, and it is the kind of fact that should be asserted by a test rather
than assumed, because it is exactly what would change silently.

The string versus array distinction is load-bearing: it is the shape that
`message_body_equivalence`'s `text_block_shape` rule normalizes, rewriting a
bare string into a single `text` block.

### Content block types

Seven distinct block types occur. Counts are bodies containing at least one
block of that type.

| Block type | Bodies |
|---|---|
| `tool_result` | 452,443 |
| `tool_use` | 450,458 |
| `text` | 258,676 |
| `thinking` | 140,769 |
| `image` | 460 |
| `document` | 92 |
| `fallback` | **3** |

`fallback` appears on three bodies in 2.45 million and `document` on 92. These
are the clearest example of why a random sample cannot prove coverage. No
non-object block and no non-string block `type` was found.

### message.usage and message.stop_reason

| `usage` | Bodies |
|---|---|
| object | 845,778 |
| absent | 501,720 |
| no `message` object | 1,099,530 |

`usage` is never null and never a non-object when present. It is dropped
wholesale by the equivalence rules, so its internal shape does not affect
identity, but it does affect bytes and therefore export.

| `stop_reason` | Bodies |
|---|---|
| no `message` object | 1,099,530 |
| absent | 502,080 |
| null | 474,128 |
| `tool_use` | 335,338 |
| `end_turn` | 34,190 |
| `stop_sequence` | 1,667 |
| `max_tokens` | 60 |
| `refusal` | 35 |

Absent and explicit null are distinct states here and both are common, which is
why the equivalence table treats `message.stop_reason` as a `drop` rather than
a `drop_if_null`. `max_tokens` at 60 and `refusal` at 35 are rare enough to be
missed by sampling.

---

## Unicode and extremes

| Measure | Value |
|---|---|
| Bodies where UTF-8 byte length differs from code point count | **481,142** (19.66 percent) |
| Bodies containing astral-plane characters (above U+FFFF) | **29,807** (1.22 percent) |
| Largest body | 54,376,859 bytes (body id 2,396,142) |
| Longest single line | 54,376,879 bytes |
| Largest transcript | 244,117,661 bytes |
| Most lines in one transcript | 30,805 |
| Deepest JSON nesting | 10 levels (body id 2,084) |

Nearly a fifth of all bodies contain non-ASCII text. That is not a corner case,
and it is the axis the `ensure_ascii` half of the style table exists to handle.
Note that `compact_ascii` never occurs despite this, meaning every non-ASCII
body in the corpus was written with escaping OFF.

Body nesting depth histogram:

| Depth | Bodies | Depth | Bodies |
|---|---|---|---|
| 2 | 78,063 | 7 | 442,376 |
| 3 | 834,685 | 8 | 99,500 |
| 4 | 36,522 | 9 | 4,314 |
| 5 | 707,076 | 10 | 3,188 |
| 6 | 241,304 | | |

The single longest line is 54 MB. Any test that loads a line into memory
without bounding it will meet this row eventually.

---

## Round-trip verification

**Every one of the 1,347 distinct signatures was verified, not a sample.** One
exemplar per signature, all of them.

Method: fetch the exemplar row through the same columns
`message_model_export._EXPORT_ROWS_SQL` selects, render it with the REAL
`_render_row` and `sha256_text` functions imported from the repo (not a
reimplementation, which would be a second rendering path that could agree with
itself while both are wrong), then compare the produced bytes against BOTH the
stored `line_sha256` and the stored `line_byte_length`.

| Result | Signatures |
|---|---|
| exact match on hash and byte length | **1,347** |
| hash mismatch | 0 |
| byte length mismatch | 0 |
| could not render | 0 |

**Negative control.** A comparison that cannot fail is not a measurement. The
verifier takes one exemplar, confirms the unmutated render matches the stored
hash (True, as expected), then flips exactly one byte of the rendered text and
re-compares. The mutated text does NOT match (False, as expected). The
comparison is therefore demonstrably capable of detecting a difference, and the
1,347 passes mean something.

---

## Exemplars and secret-free fixtures

Every signature carries an exemplar in the manifest as
`(transcript_id, line_no)` plus its `line_byte_length`.

**All 1,347 signatures have a secret-free exemplar available.** Zero signatures
are constrained to an exemplar whose body has `secret_finding_count > 0`. There
is no signature for which fixture construction requires touching a body that
contains secret material, so that constraint on test design does not exist.

Where a signature had both kinds available, the secret-free exemplar was
selected in preference, so every exemplar coordinate in the manifest points at
a body with `secret_finding_count = 0`.

For context, 6,240 of 2,447,028 bodies carry at least one secret finding
(3,829 with exactly one, 2,411 with two or more), across 12,390 findings:

| Detector | Findings | Distinct values |
|---|---|---|
| `high_entropy_assignment` | 12,307 | 723 |
| `op_service_account_token` | 70 | 11 |
| `github_token` | 6 | 1 |
| `slack_token` | 2 | 1 |
| `google_api_key` | 2 | 1 |
| `aws_access_key_id` | 2 | 1 |
| `cloudflare_api_token` | 1 | 1 |

No secret value is stored or reproduced anywhere, here or in the database, by
design.

---

## Ingest findings present in the corpus

Not a shape dimension, but relevant context for anyone testing against this
archive: 142,888 findings are recorded.

| Condition | Severity | Count |
|---|---|---|
| `duplicate_uuid_body_conflict` | stop | 87,982 |
| `timestamp_causality_violation` | advisory | 27,181 |
| `in_session_duplicate_uuid` | stop | 15,156 |
| `secret_material_present` | advisory | 6,240 |
| `unexpected_null_timestamp` | stop | 2,495 |
| `dangling_parent` | stop | 1,560 |
| `unknown_record_type` | stop | 1,056 |
| `unrootable_session` | stop | 909 |
| `multiple_session_roots` | advisory | 303 |
| `project_slug_collision` | stop | 6 |

`unknown_record_type` at 1,056 is worth watching: it is the gate that fires
when a record type outside the interned set appears, which is the same class of
event this inventory's drift check is meant to catch one level up.

---

## What is NOT represented, and therefore cannot be tested from this corpus

This is the most useful section for whoever writes the tests, because these are
the shapes that a coverage claim based on this archive silently will not cover.

| Dimension | Unrepresented value |
|---|---|
| `line_status` | `blank` |
| `serializer_style` | `compact_ascii`, `spaced_ascii` |
| `fidelity_outcome` | `fidelity_failed`, `fidelity_unverifiable` |
| `line_ending` | `CRLF`, `MIXED`, `NONE` |
| `session_ref` prefix | `agent:` |
| `message.content` | absent, null, number, object |
| `message.usage` | null, non-object |
| key order | non-object line (`__not_an_object__`) |

Every one of these needs a synthetic fixture. None of them can be exemplified
from real data, so a test suite built purely from corpus exemplars has a hole
exactly the shape of this table. The `CRLF` row is the one with a genuine open
question behind it, since the implementation does not currently handle it.

---

## The machine-readable companion

`tests/fixtures/jsonl_shape_manifest.json`, 1.27 MB, `schema_version: 1`.

Structure: `schema_version`, `generated_at`, `measured_at`, `corpus` (paths,
watermarks, totals), `signature_definition` (how to recompute an id),
`key_orders` (all 447, with digest, count and key-name list), `totals`,
`negative_control`, and `signatures` (1,347 entries, sorted by count
descending).

Each signature entry carries `signature_id`, `count`, the full `dimensions`
dict, `exemplar` (`transcript_id`, `line_no`, `line_byte_length`),
`exemplar_is_secret_free`, and `roundtrip`.

**How a drift test uses it.** Recompute the signature for live rows exactly as
`signature_definition` describes (sha256 of the canonical JSON of the
dimensions dict, first 24 hex chars) and assert every id produced appears in
`signatures[]`. An id that does not appear is a shape that did not exist when
this inventory was taken, which is the drift the manifest exists to detect.

**The manifest was verified to actually do this, not merely claimed to.** Five
checks, all passing as of 2026-08-31:

1. All 1,347 stored `dimensions` dicts regenerate their own `signature_id`. If
   the hashing recipe in the document disagreed with the one used to build the
   file, this would fail.
2. The signature counts sum to 3,125,122, matching the measured appearance
   total exactly. Nothing was dropped or double counted.
3. Every `key_order_digest` referenced by a signature exists in the
   `key_orders` catalogue. Zero dangling references.
4. **Negative control:** a deliberately novel signature (one real signature
   with `record_type` replaced by a value that does not exist) is NOT found in
   the manifest, while the unmodified original IS found. The membership test
   can therefore actually report drift rather than passing everything.
5. 400 exemplar coordinates (the 200 most common and 200 rarest signatures)
   were resolved against the live database and every one returned a row whose
   `line_byte_length` matched the recorded value. Zero misses.

---

## Reproducing this

Scripts are in `/Users/jsugamele/Scratch/llmScratch/shape-inv/`:

| Script | Does |
|---|---|
| `conn.py` | read-only connection, pragma readback, write negative control |
| `dims.py`, `dims2.py` | per-dimension marginal counts |
| `phaseA.py` | one structural signature per body, plus unicode and extremes (182 body signatures, 182s) |
| `phaseB.py` | appearance signature combined with the body signature (1,347 full signatures, 13s) |
| `phaseC.py` | round-trip every signature's exemplar through the real export code, plus negative control |
| `report.py` | emits this document's numbers and the JSON manifest |
| `verify_manifest.py` | the five manifest checks above |

Run them with `./venv/bin/python3`, not bare `python3`.

Two notes for the next person. The two-phase split exists because body_json is
the bulk of a 15 GB database and there are 2.45 million bodies against 3.13
million appearances; parsing each body once rather than once per appearance is
what keeps the full pass at about three minutes. And take a fresh watermark
rather than reusing the one in this document, since the corpus grows every 15
minutes.

---

## Synthetic example, clearly labelled

The following is SYNTHETIC. It is not from the corpus. It shows the structural
arrangement only, with all content replaced by placeholders. It corresponds to
the general shape of an assistant record whose content is an array carrying one
`text` block.

```json
{"parentUuid":"<uuid>","isSidechain":false,"message":{"role":"assistant","model":"<model>","content":[{"type":"text","text":"<placeholder>"}],"stop_reason":"end_turn","usage":{"input_tokens":0,"output_tokens":0}},"requestId":"<id>","type":"assistant","uuid":"<uuid>","timestamp":"<iso8601>","userType":"external","cwd":"<path>","sessionId":"<uuid>","version":"<semver>","gitBranch":"<branch>","slug":"<slug>"}
```

The two `APPEARANCE_KEYS` (`isSidechain`, `agentId`) are split into the
envelope at ingest and put back at their original position by `reassemble`
walking `key_order`, which is why the ordering is stored per appearance rather
than derived.
