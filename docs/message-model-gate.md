# The message model gate

Design for the human-in-the-loop gate that ~/Development/claude-history's
ingest pipeline needs, built from a 2026-08-29 read-only audit of its live
database. This document explains the audit and the design; the checkable
version of the gate itself is `src/core/message_gate_contract.py`.

Scope: this is the AUDIT and the GATE. It is not the message schema. The
owner is rebuilding claude_history top-down with classes and contracts and
is deliberately shaping the schema from what this audit finds, not the
other way around - this document stops at "here is what is broken, here
is how a human stays in the loop for it," not "here is the new table."

## Part 1: the audit

Database: `~/Development/claude-history/data/claude_history.db`, 9.8 GB,
opened strictly read-only (`mode=ro`) for every query below. Measured
2026-08-29. Live counts at measurement time (grew slightly during the
audit from continuous ingest, which is why they differ from the 3,003,976
figure quoted at the start of this task):

| table | count |
|---|---|
| hosts | 1 |
| projects | 71 |
| sessions | 19,403 |
| messages | 3,004,324 |
| compaction_events | 1,882 |
| parse_failures | 2 |
| ingest_state | 19,521 |

### a) Parent chain

| check | result |
|---|---|
| messages with `parent_uuid IS NULL` (roots) | 250,969 |
| dangling parent: non-null `parent_uuid` with no matching `uuid` on the same host | **1,433** of 2,753,355 (0.052%) |
| sessions with more than one root message | 1,209 of 19,403 |
| sessions with messages but zero root messages | 818 |
| self-referential (`parent_uuid = uuid`) | 0 |
| `(session_id, uuid)` pairs duplicated WITHIN one session | 15,298 |

Positive control on the dangling-parent query: the query itself returned
a nonzero count (1,433), which is its own proof the `NOT EXISTS` shape can
fire. A second control against a synthetic uuid known to be absent
(`parent_uuid = '__definitely_not_a_real_uuid__'`) correctly returned 0,
confirming the query does not always return something.

Multi-root sessions are not necessarily broken: a compaction event
legitimately restarts a session's root chain within the same `session_id`
(938 `compact_boundary` + 944 `isCompactSummary` records = 1,882, matching
`compaction_events` exactly - see below). This is why `GATE_MULTIPLE_
SESSION_ROOTS` is advisory, not a stop condition, in the contract.

### b) Session attachment

| check | result |
|---|---|
| messages whose `session_id` points at no `sessions` row | 0 |
| sessions whose `project_id` points at no `projects` row | 0 |
| sessions whose `host_id` points at no `hosts` row | 0 |

All three are clean, and all three had a positive control (a sentinel
`session_id = -1`, confirmed absent) proving the `NOT EXISTS` shape is
capable of returning nonzero. A project-slug collision across hosts is
not observable today because there is exactly one host row - see "Two
machines" below for why the gate carries a condition for it anyway.

### c) Subagent linkage

This is the relationship the owner cares about most - parent to child to
grandchild - and it turned out to need correcting mid-audit: the obvious
column to join on (`messages.tool_use_id`) measures something else
entirely.

| check | result |
|---|---|
| `is_sidechain` distribution | false 1,449,344 / true 1,554,980 |
| subagent sessions (`is_subagent_session=1`) | 17,993 |
| ...with `parent_session_id` set | 17,975 (99.90%) |
| ...without | 18 |
| dangling `parent_session_id` (points at no session) | 0 |
| `spawn_link_status` distribution | linked 16,572 / not_applicable 2,074 / linked_session_only 511 / linked_ambiguous 207 / parent_not_archived 39 |
| sidechain messages with `agent_id` set | 1,554,980 of 1,554,980 (100%) |
| distinct `spawned_agent_id` values resolving to a `sessions.agent_id` | 15,892 of 15,914 (99.86%) |

**The tool_use_id correction.** `sessions.parent_tool_use_id` is the
`toolu_...` id of the Task-tool call that spawned a subagent session.
The naive join - `sessions.parent_tool_use_id = messages.tool_use_id` -
returns **zero** for every one of the 6,541 subagent sessions that carry
a `parent_tool_use_id`. That looked like total breakage. It is not:
`messages.tool_use_id` is populated ONLY on `progress` and `system`
records (384,238 + 574,073 + 37,084 rows respectively across the two
prefix forms measured), where it names the tool call a progress update
is REPORTING ON - it is never the id of the tool_use block an assistant
message itself emits. The real `toolu_...` ids an assistant message emits
live in `tool_use_ids_json`, a JSON array column, and joining against
that instead resolves:

| check | result |
|---|---|
| `sessions.parent_tool_use_id` found inside some message's `tool_use_ids_json` | **6,538 of 6,541 (99.95%)** |
| ...not found in any message's `tool_use_ids_json` | 3 |

This is worth carrying forward past this one audit: **a relationship that
looks completely broken can be a measurement against the wrong column**,
not a broken relationship. The `messages.tool_use_id` scalar column and
`messages.tool_use_ids_json` are two different things that happen to sit
one column apart in the schema.

Session identity also carries TWO namespaces, not one:
`session_uuid` is a real UUIDv4 for a genuine session (19,403 rows,
19,403 distinct - see "Two machines" below) and the literal form
`agent:<hex>` for a subagent session row (17,329 `session_kind='subagent'`
rows; 0 of them have a UUID-form `session_uuid`, confirming the two forms
never mix). This is not malformed data; it is a second, deliberate
identity scheme for the same table, and the gate does not flag it.

### d) Tool pairing

Same correction as (c) applies here: the literal `tool_use_id`/
`tool_result_id` columns do not name the relationship the task asked
for. `tool_use_id` is progress/system-only (see above); the real call
ids live in `tool_use_ids_json`. Measuring the ACTUAL call-id/result-id
relationship (distinct ids, host-scoped):

| check | naive (wrong column) | real (tool_use_ids_json vs tool_result_id) |
|---|---|---|
| distinct call ids | 169,083 | 435,215 |
| distinct result ids | 434,815 | 434,859 |
| calls with no matching result | 611,436 (61%) | **360 (0.083%)** |
| results with no matching call | 363,274 | **4 (0.0009%)** |

The naive numbers are not a smaller version of the truth; they are a
measurement of the wrong relationship and are reported here only to show
how badly a plausible-looking column name can mislead. The real pairing
is essentially complete. `has_tool_use`/`tool_use_ids_json` and
`has_tool_result`/`tool_result_id` boolean flags agree with their id
columns 100% of the time (511,454/511,454 and 510,981/510,981).

### e) Compaction

| check | result |
|---|---|
| `compaction_events` referencing a nonexistent session | 0 |
| `compaction_events` referencing a nonexistent message | 0 |
| `is_compact_boundary=1` messages with a matching `compaction_events` row | 1,882 of 1,882 (100%) |

Both zero-count checks had a positive control (`message_id = -1`,
confirmed absent).

### f) Ordering

| check | result |
|---|---|
| sessions with a duplicate `seq_in_file` value | 636 (895 excess rows) |
| sessions with a gap in `seq_in_file` span | 1 |

### g) Timestamps

| check | result |
|---|---|
| messages with NULL timestamp | 124,835 (4.15%) |
| ...concentrated in non-conversational record types | 100% - zero NULLs among assistant/user/progress/attachment/queue-operation/system/pr-link |
| causality violation (child timestamp < parent's), **session-scoped** | **26,793 of 2,781,511 (0.96%)** |
| same check, naive host-scoped join | 222,336 of 9,070,856 pairs |

The host-scoped version of the causality check is inflated by the
duplicate-uuid/appearance phenomenon (below): a host-scoped join matches
a child against EVERY row anywhere on the host that happens to share its
parent's uuid, not just the one row in its own session. 9,070,856 pairs
against only 2,753,355 messages that have a parent_uuid at all is the
tell. The session-scoped version (`p.session_id = m.session_id AND
p.uuid = m.parent_uuid`) is the real relationship and gives the 26,793 /
0.96% figure the gate contract uses.

### Record types and the fidelity vocabulary

19 distinct `record_type` values exist in the live database, zero rows
outside them: `assistant`, `progress`, `user`, `attachment`,
`queue-operation`, `system`, `custom-title`, `last-prompt`, `mode`,
`file-history-snapshot`, `pr-link`, `ai-title`, `summary`, `atis-latch`,
`permission-mode`, `artifact-autoreact-ledger`, `bridge-session`,
`frame-link`, `artifact-comment-monitor`. This is `KNOWN_RECORD_TYPES` in
the contract - the vocabulary `GATE_UNKNOWN_RECORD_TYPE` checks new
ingest against.

## The big finding: duplicate uuids are two different things

2,772,692 message rows carry non-null `uuid`; only 2,263,079 are distinct
(host-scoped). 162,891 `(host_id, uuid)` pairs are duplicated, accounting
for 509,613 excess rows - about 18% of all uuid-bearing rows are a
"repeat" of some other row's uuid.

The first hypothesis (mine, and independently the same shape as the
coordinator's parallel measurement) was that this is corruption or a
storage bug. It is not. It is **fork, resume, and subagent replay**:
Claude Code writes the SAME message, with the SAME uuid, into more than
one transcript file, because a resumed session's file starts with a
verbatim copy of the conversation it resumed from, and a subagent's
sidechain transcript carries a copy of the message that spawned it.

That single sentence is not enough on its own, and measuring it mattered:
an initial hash-based check that omitted `is_sidechain` and `agent_id`
from the comparison found 4,000/4,000 sampled duplicate-uuid groups
"identical" and would have supported "duplicates are always identical,
safe to dedupe by uuid alone." A second, corrected measurement that
included those columns found the opposite shape: of 4,000 groups (text
over 200 chars, to avoid trivial one-word rows), only 17 were identical
across every column; **3,983 differed in exactly `is_sidechain`,
`agent_id`, and the DB `session_id` FK — never in the message body — and
0 differed in body.** A second independent sample (4,000 more groups,
different portion of the uuid ordering) found the same shape: 0 body
differences.

**This is the deliverable the coordinator asked for, stated plainly: of
the sampled duplicate-uuid groups, the fraction that differ in the
message BODY is 0/8,000 across two independent samples. The fraction
that differ only in ENVELOPE fields (is_sidechain, agent_id, session_id)
is effectively all of them once the body is held constant.** Two
positive controls back this: (1) the comparison function was verified
against two synthetic differing bodies before either sample was trusted,
and (2) a raw-JSON diff of one specific differing pair (session 10574 vs
10705, same uuid) showed 12 keys byte-identical (`message`, `sessionId`,
`uuid`, `timestamp`, `parentUuid`, `cwd`, `gitBranch`, `slug`,
`permissionMode`, `type`, `userType`, `version`) with only `isSidechain`
and `agentId` differing - the envelope fields ARE the parent/child
linkage the owner's hierarchy needs, encoded as the difference between
two copies of the same message.

**Model consequence.** A duplicate uuid whose body matches is normal and
must not be a gate condition - gating it would put roughly 160,000 groups
in front of a human for something that happens on every resume and every
subagent spawn. A duplicate uuid whose body differs IS a gate condition
(`GATE_DUPLICATE_UUID_BODY_CONFLICT`) - the id would then be naming two
different messages, which is a genuine identity violation, currently
unobserved but not structurally impossible (a hash collision, a bug, or
eventually two hosts). The natural shape this points to for the eventual
schema - noted here because it is the direct consequence of this
measurement, not designed further, per this document's scope - is a
message-identity row per uuid holding the body, plus an appearance row
per `(uuid, session_id)` holding `seq_in_file`, `is_sidechain`,
`agent_id`, and whatever else varies per context. That preserves
byte-exact reconstruction (each appearance's envelope reconstructs its
own line) and promotes the subagent linkage from an implicit
near-duplicate row into an explicit, queryable edge.

One more thing the raw-JSON diff surfaced: the JSON's own `sessionId`
field stays IDENTICAL between a message's copies - it still names the
originating session even inside a subagent's own transcript. That is a
second, independent rooting signal, distinct from the DB's `session_id`
foreign key (which correctly points at whichever transcript file this
particular copy was ingested from). Worth checking generally in the
schema work as a possibly-stronger root than directory structure, but
not measured exhaustively here - flagged, not claimed.

## Two machines, one database

The owner is importing his workstation's data (which he says is "almost
all good") alongside mac-mini-m4's, into the same `claude_history.db`
that currently holds one host. Two identity questions follow from that,
and they get different answers.

**Session uuid collisions across hosts: NOT gated.** `sessions.
session_uuid` is measured unique today - 19,403 rows, 19,403 distinct,
zero duplicates. UUIDv4 carries 122 random bits; a genuine two-host
collision needs on the order of 2.3×10^18 uuids sampled for a 50% birthday
chance, and the fleet has 3 million. The owner's own operating fact
settles the modeling question on top of the statistics: he moves
Claude's project files between machines and they work, so the same
session uuid arriving from a second host means the SAME session was
copied or moved, not two different things colliding. The correct
behavior is to recognize it as one session, not to gate it as a conflict.
Gating a matching uuid would put every moved session in front of a human
for a decision that has only one right answer.

**Project slug collisions across hosts: gated.** `projects.slug` is a
derived, lossy string (every non-alphanumeric character becomes `-`), and
both machines have `/Users/jsugamele/...` paths, so `csj.dbexport`,
`csj_dbexport`, and `csj dbexport` all collide to the same slug from
different real directories. A uuid is an identity; a slug is a guess, and
that is the entire reason the gate treats them differently
(`GATE_PROJECT_SLUG_COLLISION`). Unobservable today (one host row), but
reachable the moment the second host's data lands - the condition exists
in the contract before that day, not after.

## Fidelity: a third outcome the eventual disk cleanup forces open

1,306,979 of 3,004,324 messages have `raw_stored=0` (a deliberate,
documented policy - progress/system-adjacent record types are
normalized-only) and no per-line hash exists anywhere for them today.
That is fine AS LONG AS the source JSONL file is still on disk to
regenerate against and re-verify - measured 2026-08-29: all 19,403
sessions still have their source transcript on disk (19,535 `.jsonl`
files present, some sessions span multiple files) and 25/25 sampled
compacted sessions have identical record counts in the database and on
disk. Today, database and disk agree everywhere.

The owner has since clarified he is **not** planning to wipe disk - he
will keep backups of files, database, and code - so this is not an
emergency ordering constraint the way it looked at first. It is still a
real fact about what CAN be proven: a "regenerate and verify" fidelity
claim is only checkable while the source file exists (or while a
per-line hash captured from it still exists). The moment both are gone
for a given line, verifying that line's fidelity becomes structurally
impossible, not merely inconvenient - there is nothing left to compare
against. That is why `classify_fidelity` in the contract has three
outcomes, not two: `FIDELITY_VERIFIED`, `FIDELITY_FAILED`, and
`FIDELITY_UNVERIFIABLE`, the last one meaning "the check could not run,"
never collapsed into either a pass or a fail.

**Recommendation, not a blocker**: capture a per-line sha256 for every
message while the source files still exist. It is cheap now (the
2026-08-11 audit already proved 100.0000% byte-exact regeneration across
134,464 sampled lines, so the check is fast and reliable) and it is the
only thing that keeps a FUTURE fidelity check from landing in
`FIDELITY_UNVERIFIABLE` for a file that has since been archived,
compressed, or moved off the machine even without an active disk-wipe
plan.

## Reaping and test/single-use sessions

The owner has stated two real-but-testing projects are live now, and
that test and single-use sessions will need reaping as a future feature.
This interacts with the gate directly and dangerously if built naively:
**an item sitting at the gate is there because we could not establish
what it is** - a dangling parent, an unresolved subagent link, an
ambiguous spawn candidate. That is the opposite of evidence that the item
is disposable. A reaper that treats "no established link" as "safe to
delete" would preferentially destroy exactly the data a human most needs
to look at, which is the worst possible failure mode for a cleanup
feature.

The contract's `reap_eligibility` function enforces this as a totality
rule over two independent facts:

1. **Gated status always wins.** `REAP_BLOCKED_GATED` regardless of any
   disposability signal, if the item currently has an active STOP
   condition. Not overridable by a signal, by design - see
   `test_reap_blocked_while_gated_regardless_of_signal`.
2. **Absent that, eligibility still requires a POSITIVE signal** - one of
   an sdk-cli entrypoint, a known test-project root, or an explicit
   recorded human decision (`DisposabilitySignal`, three kinds, each
   carrying required evidence, never a bare boolean). No signal means
   `REAP_BLOCKED_NO_SIGNAL`, never a default-allow.

A gated item is therefore structurally un-reapable until a human resolves
it - not by convention, by the fact that `apply_decision` is the only
function that can move an item's status out of `STATUS_GATED`, and
`reap_eligibility` checks that status before it ever looks at a signal.

## Part 2: the gate

### What stops an item

Every condition below is a row in `GATE_CONDITIONS` in
`src/core/message_gate_contract.py`, each carrying its severity
(`stop` blocks linkage; `advisory` is recorded but does not),
whether it can be cleared automatically by new ingest data
(`auto_resolvable`), a plain-language description, and the measured
count that justified adding it, dated 2026-08-29. Restating the full
list here would drift from the code the moment either changes; read the
registry directly; the summary is:

**Stop conditions**: dangling parent, unrootable session, orphan
session/project/host id, ambiguous spawn link, pending parent session,
unresolved sidechain link, duplicate-uuid body conflict, in-session
duplicate uuid, unknown record type, unexpected null timestamp, fidelity
check failed, project slug collision.

**Advisory conditions** (recorded, do not block linkage): tool call
without result, tool result without call, ordering anomaly, timestamp
causality violation, multiple session roots.

### What is stored for a gated item

Complete, byte-exact, unconditionally - a gate condition affects LINKAGE
only, never storage. This follows directly from the fidelity design
above: a message is stored (raw or regenerable-and-verified) before
anything asks whether it can be linked to a parent, a session, or a tool
call. Never dropped, never guessed at, and never blocked from being
stored because it cannot be linked - the audit's own numbers are the
argument for this: 1,433 dangling parents and 3 unresolved subagent links
are a rounding error against 3 million messages, and refusing to store
any of them over a linkage question would throw away real conversation
content over a bookkeeping gap.

### What the human is shown

A `GatedItem`: the item's own reference, its active condition codes (so
a human sees the DIAGNOSIS, not a raw error), `known_fields` (whatever
was actually established - never blank, never a guess dressed as a
fact), and `candidates` - a tuple of `Candidate(ref, reason, confidence)`
entries. Candidates are explicitly hints, never a pre-selected guess:
nothing in the contract auto-applies the highest-confidence candidate,
and `apply_decision` requires the human (or an "auto" actor acting on a
newly-satisfied auto-resolvable condition) to name the `chosen_ref`
explicitly.

### What applying a decision does

`apply_decision(item, decision)` is pure - it returns a new `GatedItem`,
never mutates the one passed in. A `resolve` or `supersede` decision
clears every currently active condition on the item at once (a human
looking at the item's full context is resolving the WHOLE item, not
picking off conditions one at a time) and moves status to
`STATUS_RESOLVED`. A `dismiss` does the same, recording that a human
determined no link exists rather than that one was found - still
resolved, never silently re-gated by the same unchanged facts recurring.
Every decision - resolve, dismiss, or supersede - is appended to
`item.decisions`, an append-only tuple; a correction is a NEW
`GateDecision` appended after the one it corrects, matching this repo's
own convention for `.claude/TODO.md` and the Infrastructure CLAUDE.md
hazard list (mark superseded, never silently edit history).

### Automatic resolution when new information arrives

`auto_resolve(item, now_resolved_codes, timestamp=...)` clears only the intersection of
an item's currently active conditions, the codes the caller reports as
now-satisfied, and `AUTO_RESOLVABLE_CODES` (derived from the registry, so
it can never disagree with it). This is the "a parent that gets ingested
afterwards should un-gate its children" mechanism: `GATE_DANGLING_
PARENT`, `GATE_UNROOTABLE_SESSION`, `GATE_PENDING_PARENT_SESSION`,
`GATE_UNRESOLVED_SIDECHAIN_LINK`, `GATE_TOOL_CALL_WITHOUT_RESULT`, and
`GATE_TOOL_RESULT_WITHOUT_CALL` are all marked auto-resolvable, because
each is fundamentally an ordering problem (the linked-to thing had not
been ingested yet), not a data-quality problem. `GATE_DUPLICATE_UUID_
BODY_CONFLICT`, `GATE_IN_SESSION_DUPLICATE_UUID`, `GATE_UNKNOWN_
RECORD_TYPE`, `GATE_UNEXPECTED_NULL_TIMESTAMP`, `GATE_FIDELITY_CHECK_
FAILED`, and `GATE_PROJECT_SLUG_COLLISION` are not - each of those is a
genuine ambiguity or conflict that later ingest cannot resolve on its
own; only a human decision clears them. A caller naming a code the item
does not currently carry is a no-op, not a silent success, so a caller
bug cannot manufacture a resolution that never happened.

## What could not be evaluated, and why

- **Fidelity beyond the 2026-08-11 sample.** The 100.0000% / 134,464-line
  figure is a sample, not a full-population check; a full-population
  regenerate-and-verify pass was out of scope for this audit (it would
  mean reading every raw JSONL line for every message, which is a
  materially larger job than the aggregate SQL used here) and was not
  run. Reported as the existing measurement, not re-verified.
- **Project slug collision across hosts.** Structurally unobservable
  with one host row in the database today - CANNOT DETERMINE, not a
  pass. The condition is in the contract so it activates the moment a
  second host's data is ingested, rather than needing to be invented
  after the fact.
- **Body-diff fraction on the FULL population of 162,891 duplicate-uuid
  groups.** Measured on two independent samples of 4,000 groups each
  (8,000 total, roughly 5% of the population), not the full set - a
  full-population byte-for-byte comparison of every duplicate-uuid group
  was not run given the time budget for this task. The sampled rate (0
  body differences in 8,000) is strong enough to inform the gate design
  (a body-diff condition exists and is a STOP, not silently assumed
  never to fire) but is not a claim that zero exists fleet-wide.
- **`GATE_ORDERING_ANOMALY`'s single seq_in_file gap.** Identified by
  count, not by session - the specific session was not traced down to
  characterize whether the gap is a parse_failures-adjacent event or
  something else. Left as a measured count with a named, non-blocking
  advisory condition rather than a diagnosed root cause.

## Verification

`./venv/bin/python -m pytest tests/ -q` from the CloudeCode repo root.
Baseline before this change: 3,429 passed, 0 failed (two known-flaky
tests excluded from that count per the task brief:
`test_shell_init.py::test_the_old_prefix_produces_a_completely_blank_pane`
and the tmux "server exited unexpectedly" flake in
`test_session_backend.py`). This change adds `tests/
test_message_gate_contract.py` and touches no other test file.

## Part 3: the schema this became (v16), added 2026-08-29

This document originally said it deliberately did NOT define the message
table schema, because the owner was doing that top-down. That is now
SUPERSEDED, not deleted: the schema below was built directly on the
measurements above, and the section is left in place so the order the
work actually happened in stays readable.

**Schema v15 -> v16** (`src/core/message_model_ddl.py`, step in
`src/core/db_steps.py`) is additive-only: nine CREATE statements, each
with its own `IF NOT EXISTS`, nothing altered.

- `message_bodies` - one row per DISTINCT BODY. Identity is
  `(uuid, sha256-of-the-body-as-stored)`, so two different bodies under
  one uuid are two rows. Never merged, never keep-first.
- `message_appearances` - one row per `(transcript, line)`. Holds
  `seq_in_file`, `is_sidechain`, `agent_id`, the envelope values, the
  original top-level key order, the serializer style marker, and the
  sha256 of the original line. This is where the subagent linkage stops
  being an implicit near-duplicate row and becomes an explicit edge.
- `message_transcripts` - the container, carrying `session_ref` and
  `session_ref_scheme` so the uuid and agent identity schemes are a
  stated fact rather than a shape a later reader has to guess.
- `message_record_types` / `message_roles` / `message_models` /
  `message_compact_subtypes` - the repeating-value lookups. Worth about
  1% of size; kept for correctness and cheap filtering, not for bytes.
- `message_ingest_findings` / `message_secret_findings` - the gate
  findings, using this contract's vocabulary and no other.

**Two hashes, deliberately.** `body_sha256` is order-insensitive
(canonical, sorted) and answers "is this the same message?", which is
what the duplicate-uuid conflict check compares. `body_bytes_sha256` is
order-sensitive and is what identity keys on, because a nested object's
key order is part of what must come back byte-exactly. Collapsing them
was tried and broke export: bodies stored with nested keys sorted came
back valid, meaning-identical and byte-wrong.

**Three corrections to the measurements above, all made 2026-08-29 by
re-measuring rather than by reasoning:**

1. **A duplicate uuid CAN carry a differing body, at about 1.1%.** The
   8,000-group sample above found 0. An independent pass over 3,443
   duplicate-uuid groups that had raw JSON on both sides found 39
   (1.13%). The mechanism is not post-hoc editing: 14 differ only in
   `stop_reason`/`usage` (a streaming snapshot versus the completed
   message), 15 differ in `content` representation (a bare string versus
   a single `{"type":"text"}` block), 4 in `usage` alone. No
   credential-redaction case appeared in that sample.
2. **There are THREE session identity forms, not two.** The brief named
   `agent-<hex>`. The live `sessions` table holds `agent:` on 17,996 rows
   and `agent-` on 224 - the rarer form is the one that was documented.
3. ~~**The JSON's own `sessionId` does stay stable across copies.**
   Flagged above as "worth checking, not claimed". Checked on the
   ingested sample: 11 uuids carried more than one stored body and 0 of
   them disagreed on `sessionId`.~~ **SUPERSEDED 2026-08-30 - this is
   wrong, and it was wrong because 11 groups is not a measurement.**
   Against the whole corpus, `sessionId` is the SINGLE MOST COMMON thing
   two copies of one message disagree about: 40,607 of the 45,246
   conflicting uuid groups. It could not have been otherwise - a resumed
   or forked session replays prior messages under its own new session id
   by construction. The sample of 11 was drawn from sessions the proof
   script chose for other reasons and happened to contain no replays.

**Proof, not assertion.** `scripts/message_model_sample_proof.py` opens
the history database strictly read-only, draws whole sessions covering
every awkward case, ingests them, and re-exports. Latest run: 40
transcripts, 9,706 lines, **40 of 40 reconstructed byte-exact**, 0
fidelity failures. `messages.raw_json` was verified as byte-identical to
the on-disk `.jsonl` line for 36 of 36 rows before being trusted as the
source of "the original bytes".


## The duplicate-uuid queue, and what shrank it - 2026-08-30

**The problem.** Ingesting the whole corpus (19,541 transcripts,
2,432,762 bodies, 3,006,908 appearances) produced a 49,905-item review
queue, and `duplicate_uuid_body_conflict` was 45,246 of it - 90.7
percent. The condition fired whenever two bodies stored under one uuid
were not byte-identical after key sorting. A queue that size is a queue
nobody opens, and a queue nobody opens is worse than no queue.

**What the 45,246 actually were, measured on the whole corpus rather
than sampled.** Every difference in all 45,246 groups is drawn from a
CLOSED SET OF 27 JSON PATHS, arranged into only 75 distinct signatures.
The per-path counts:

| path | groups | verdict |
|---|---|---|
| `sessionId` | 40,607 | recording context |
| `slug` | 30,853 | recording context |
| `version` | 29,078 | recording context |
| `forkedFrom.sessionId` / `.messageUuid` | 17,372 | recording context |
| `promptId` | 4,422 | recording context |
| `gitBranch` | 2,365 | recording context |
| `parentUuid` | 1,494 | **gates** |
| `cwd` | 684 | recording context |
| `message.usage.*` | 1,013 | recording context |
| `message.stop_reason` | 426 | recording context |
| `message.context_management` | 240 | recording context, null only |
| `entrypoint` | 226 | recording context |
| `message.content` (shape) | 18 | recording context |
| `message.content` (real) | 55 | **gates** |
| `sourceToolAssistantUUID` | 63 | recording context |
| `attachment.displayPath` | 30 | recording context |

**The equivalence.** `src/core/message_body_equivalence.py` declares one
rule per measured class, each carrying the count that justifies it, and
refuses any rule that cannot cite one. It is a pure canonicalisation and
it decides only what is GATED - two bodies under one uuid are two rows in
`message_bodies` whatever it says, because the evidence that a transcript
was edited after the fact is exactly the pair.

**Two decisions worth reading before changing it.**

`message.stop_reason` is dropped rather than null-gated. Measured across
all 426 groups where it differs, EVERY one has null on one side (289
`tool_use` against null, 74 `end_turn` against null, 63 with three or
more copies and the same shape) and ZERO have two different non-null
values. The residual risk is a `max_tokens` against `end_turn` pair, and
that pair cannot hide: a truncated completion differs in
`message.content`, which still gates. Measured co-occurrence of a
stop_reason difference with a content difference is also ZERO, as is the
co-occurrence of a usage difference with a content difference.

`parentUuid` was nearly normalised and then was not. 1,155 of the
affected groups also carry a `forkedFrom`, which reads as fork
bookkeeping. Measuring settled it: 1,474 of the 1,494 groups name TWO
REAL PARENTS, and only 20 are the null-against-a-value shape a re-rooted
fork would produce. Two copies of one message naming different
predecessors is the conversation graph differing, not the recording. It
keeps gating, and it is now 96 percent of what is left.

**The result, re-derived from the finished corpus by
`scripts/message_model_duplicate_reclass.py` without re-ingesting:**

```
  uuids stored with more than one body      45246
  genuine conflict  (STOP)                   1534
  recording variant (ADVISORY)              43712

  STOP items a human must work               2824   (was 46,536)
  ADVISORY items, reported not queued       47081
  every item, both severities                49905
```

The 1,534 that remain are 1,478 parent-graph differences, 40 real
content differences, and 16 that are both.

**The benign 43,712 are reported, not dropped.**
`duplicate_uuid_recording_variant` is a registered ADVISORY condition
with its own finding row. The owner is entitled to know once that tens of
thousands of his messages were replayed by resume and fork; he is not
required to read them one at a time. Nothing about which bodies are
stored changed in either direction.

---

## The host dimension, measured 2026-08-30 (schema v17)

The single-host numbers above stand as history. This is what the same
gate reports once a SECOND MACHINE is in the same database.

**What is in it.** 21,039 transcripts across three corpora: this laptop's
`~/.claude/projects` (19,548), mac-mini-m4's `~/.claude/projects` (1,477),
and this laptop's `~/Library/Application Support/Claude/local-agent-mode-sessions`
(14) - a corpus nothing had ever indexed, because every scanner written
so far looks only at the first path. All 21,039 reconstruct byte-exact.

**Host identity is the IOPlatformUUID.** A hostname is mutable and this
laptop already answers to three different names; a serial number is
stable but is the one value not worth copying into an 11 GB database.
A wrong host identity is made detectable by a COLLECTION MANIFEST
captured on the source machine (its platform uuid plus one sha256 per
file), which ingest compares against the bytes it actually read:
21,036 `manifest_verified`, 3 `cannot_determine`, 0 declared. The 3 are
live session files that grew between collection and read, including the
session that performed the run.

**The slug collision is real and now fires.** 3 of the mini's 4 project
slugs also exist on the laptop, because both machines run as the same
unix user and a slug is a lossy derived string. All 3 carry the SAME
observed cwd on both machines, which is the ambiguity rather than
evidence of one directory. `UNIQUE (corpus_id, slug)` keeps them as
distinct projects by construction; the gate raises 6 findings, one per
project on each side, because only the owner can say whether the mini's
copy is the same work.

**A session uuid on two hosts is NOT gated: 213 of them are.** That is
14.4 percent of the mini's files already duplicated on the laptop.

**The queue, and where the increase actually came from:**

```
                                   single host   two hosts
  duplicate uuid, genuine conflict        1534        3123
  unrootable_session                       909         909
  in_session_duplicate_uuid                 68          74
  dangling_parent (settled)                311         311
  unknown_record_type (distinct)             2           7
  project_slug_collision                     0           3
  STOP items a human would work           2824        4427
```

The obvious reading - "the second machine tripled the hardest block" -
is wrong, and it took an attribution pass to see that. Splitting the
3,123 genuine conflicts by which corpus their bodies appear in:

```
  laptop ~/.claude/projects only          1528
  involves local-agent-mode-sessions      1589
  involves mac-mini-m4                       6
```

**The second HOST cost 6 items. The 14-file corpus cost 1,589.** And
those 1,589 are one recognisable class: 1,578 of them are the same
message recorded twice in two different envelope DIALECTS. The
local-agent-mode corpus writes an `audit.jsonl` with `session_id`,
`parent_tool_use_id` and `_audit_timestamp`, alongside a nested
`.claude/projects` tree writing the standard `sessionId` / `parentUuid`
envelope for the same messages. `message_body_equivalence` has never
been measured against that dialect, because nothing had ever read this
corpus. It is a new equivalence class to measure, not corruption, and
deliberately NOT absorbed by a rule here - a rule added without the
measurement behind it is what that module forbids.

The 5 new `unknown_record_type` values are similarly informative rather
than alarming: `agent-name`, `cost-state` and `file-history-delta` come
from the mini (a newer Claude Code version), `rate_limit_event` and
`tool_use_summary` from local-agent-mode.
