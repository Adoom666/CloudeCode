# Message browser UI - implementable design

Companion to `docs/message-browser-api.md`. That document specifies the
server. This one specifies the client: the screen a human uses to read a
byte-exact archive of every Claude Code conversation the owner has had
across two machines.

**Status:** design, not implemented. No UI code exists yet.

**Every number in this document is dated and says how it was measured.**
The dev database is written by a background corpus-ingest scheduler every
900 seconds, so corpus counts drift upward. A count with no measurement
date is a defect in this document, not a fact.

## 0. How to read this document

Read sections A, D and F before writing any code. A settles the shape,
D settles what every view is allowed to say, F is the one piece of logic
where getting it wrong leaks a credential.

Sections marked NORMATIVE are requirements. Everything else is design
rationale, which is here so the next person can tell an intentional
choice from an accident.

**The governing rule for the whole screen** is the THREE-OUTCOME RULE
from `/Users/jsugamele/Development/Assistants/Infrastructure/CLAUDE.md`:
every check has three outcomes - pass, fail, and COULD NOT EVALUATE -
and the third is not a flavour of the other two. This UI renders an
archive whose server was built to that standard. A client that collapses
`cannot_determine` into an empty state throws away the entire property
the server pays for.

### 0.1 Measurement log for this document

Everything below was measured against the live dev instance at
`http://127.0.0.1:5055` on **2026-08-31 between 11:45 and 12:00 America/New_York**,
either by an authenticated HTTP call or by a read-only SQLite connection
(`file:...?mode=ro`) against `/Users/jsugamele/Scratch/llmScratch/cc-dev-state/cloude.db`.
Nothing in this document was written to any database.

| Fact | Value | How |
|---|---|---|
| Transcripts | 21,039 | `SELECT COUNT(*) FROM message_transcripts` |
| Bodies | 2,447,028 | `SELECT COUNT(*) FROM message_bodies` |
| Appearances | 3,125,122 | `SELECT COUNT(*) FROM message_appearances` |
| Hosts | 2 | `GET /api/v1/archive/hosts` |
| `session_ref_scheme` split | `agent` 19,588 / `uuid` 1,451 | `GROUP BY session_ref_scheme` |
| Largest transcript by lines | id 5767, 30,805 lines, 91,950,363 bytes | `ORDER BY line_count DESC` |
| Largest transcript by bytes | id 17266, 244,117,661 bytes, 17,486 lines | `ORDER BY raw_byte_length DESC` |
| Largest single line | transcript 19243 line 62, 54,376,879 bytes | `ORDER BY line_byte_length DESC` |
| Second largest single line | transcript 17266 line 3108, 37,404,061 bytes | same |
| Transcripts over 8 MiB | 223 (1.06%) | `WHERE raw_byte_length > 8388608` |
| Transcripts at or under 8 MiB | 20,816 (**98.94%**) | derived from the row above |
| `progress` records | 917,436 (**37.49%** of all bodies) | join on `message_record_types` |
| Bodies with `role IS NULL` | 1,099,537 (**44.93%**) | `GROUP BY role_id` |
| Bodies with a secret finding | 6,240 | `WHERE secret_finding_count > 0` |
| Bodies with `ts IS NULL` | 33,480 | `WHERE ts IS NULL` |
| `sessions` table rows | **0** | `SELECT COUNT(*) FROM sessions` |
| Themes | 23 | `client/css/themes/*/` minus `_shared` |

---

## A. Screen, not modal

**NORMATIVE: the message browser is a full screen, `#archive-screen`.**
It is not a modal, not a drawer, and not a panel inside the terminal
screen.

Four reasons, in descending order of how much they cost to get wrong.

**A.1 A `transcript_id` deep link is the shareable artifact, and a modal
cannot hold a URL.** The whole point of an archive is being able to say
"look at line 7111 of transcript 5767". That has to survive a copied
address bar, a bookmark, a browser back button and a reload. A modal
opened on top of `/launchpad` either leaves the URL lying about what is
on screen or forces a synthetic history entry per open/close, which then
fights the real router. The app already has exactly one precedent for a
URL-addressable view (`/session/{project}`, `src/main.py:880`), and it
is a screen.

**A.2 Reading sessions are long.** The median transcript in this corpus
is small, but the reader has to work on transcript 5767 at 30,805 lines
and transcript 17266 at 244 MB. A person reading one of those is not
performing a transient interaction on top of another task; the archive
IS the task. A modal signals "you will be back in a second" and then
holds someone for twenty minutes.

**A.3 `modal-stack.js` already owns Escape, and the reader needs Escape
for itself.** Inside the reader, Escape must close the search bar,
dismiss the outcome detail popover, and clear the line selection - three
nested affordances that each want it. Putting the reader inside the modal
stack means every one of those has to fight the stack for the key or
stopPropagation its way out, which is exactly the kind of ordering bug
that works until someone adds a fourth affordance. As a screen, the
reader owns its own key handling (`archive-keys.js`) and the modal stack
is untouched.

**A.4 Screen chrome is a real requirement here.** The archive needs the
status light, the global audio toggle and the logout button placed the
same way every other authenticated screen places them. `ScreenChrome`
gates that on a screen name. A modal has no screen name.

### A.1 What stays a modal, and why

Two things stay modals, because they are genuinely transient and neither
one is addressable.

| Modal | Why it is not a screen |
|---|---|
| **Export dialog** | It is a decision, not a destination. It opens, states the integrity outcome, offers one or two actions, and closes. Nothing about "I am currently looking at the export dialog for transcript 4" is worth putting in a URL, and a stale link to it would be meaningless. |
| **Body inspector** | The raw-JSON view of ONE body. Transient by construction, and it is opened from a line that the URL already addresses via `/archive/t/<id>/l/<n>`. The line is the addressable thing; the inspector is a lens on it. |

Both use the existing `modal-stack.js` unchanged. Neither introduces a
new stack.

---

## B. Component inventory

**NORMATIVE: every file below is under the repo's 500-line cap
(`~/.claude/rules/code-standards.md`).** The largest is 420.

Line counts are design targets, not measurements. They exist so that a
file drifting past 500 during implementation is recognised as a signal to
split rather than as a surprise.

### B.1 JavaScript, `client/js/`

| File | Lines | Responsibility | Depends on |
|---|---:|---|---|
| `archive-outcome.js` | 170 | **The ONLY place `result_status` / `scope_status` / `scan.status` are interpreted.** Maps an envelope to one of six outcome tokens. Pure, no DOM. | none |
| `archive-mask.js` | 190 | UTF-16 secret masking, and the refusal path when masking cannot be performed. Pure, no DOM. | none |
| `archive-format.js` | 150 | Timestamps, byte counts, character counts, sha256 abbreviation, relative ages. Pure. | none |
| `archive-deeplink.js` | 140 | Parse and build the four archive routes. Pure. | none |
| `archive-state.js` | 210 | The screen's single state object plus the reducer. No fetching, no DOM. | `archive-outcome.js` |
| `archive-virtual-list.js` | 320 | Generic windowing engine: offset table, binary search, rAF reconciliation, anchored scroll correction. No archive knowledge. | none |
| `archive-body-cache.js` | 240 | Two-key LRU over fetched bodies (count AND bytes), plus in-flight de-duplication. | `api.js` |
| `archive-outcome-view.js` | 260 | Renders an outcome token as markup. The only file that decides what a `cannot_determine` looks like. | `archive-outcome.js` |
| `archive-line-render.js` | 420 | One line of the reader: gutter, role, record-type chip, body or placeholder, size gate, mask application. | `archive-mask.js`, `archive-format.js`, `archive-outcome-view.js` |
| `archive-nav.js` | 380 | The left rail: hosts, corpora, projects, unattributed. Lazy, envelope-aware at every level. | `api.js`, `archive-outcome-view.js` |
| `archive-transcript-list.js` | 330 | The middle column: a project's transcripts, cursor-paged. | `api.js`, `archive-virtual-list.js` |
| `archive-reader.js` | 400 | The right pane: owns the spine, drives the virtual list, requests bodies. | `archive-virtual-list.js`, `archive-body-cache.js`, `archive-line-render.js` |
| `archive-search.js` | 390 | In-transcript and in-project search, scan-budget reporting, resume. | `api.js`, `archive-outcome-view.js` |
| `archive-export.js` | 300 | Export preflight, integrity outcome rendering, filename collision warning, the download handoff. | `api.js`, `archive-outcome-view.js` |
| `archive-keys.js` | 200 | Keyboard map for the screen, including the Escape ladder. | `archive-state.js` |
| `archive-screen.js` | 300 | Composition root. Owns `#archive-screen`, wires the four regions, exposes `ArchiveScreen.show(params)`. | all of the above |
| **Total** | **~4,400** | | |

### B.2 CSS, `client/css/`

| File | Lines | Responsibility |
|---|---:|---|
| `archive-outcomes.css` | 190 | **The three outcome states, and nothing else.** Section I.7's constraint applies here: no outcome's meaning may depend on `border-radius`. |
| `archive-screen.css` | 280 | Screen shell, three-column grid, the narrow-viewport collapse. |
| `archive-nav.css` | 210 | Left rail. |
| `archive-reader.css` | 330 | Reader pane, line rows, gutter, body block, size-gate placeholder. |
| `archive-search.css` | 210 | Search bar, result list, scan-budget banner. |
| `archive-export.css` | 150 | Export modal. |
| **Total** | **~1,370** | |

### B.3 Two rules that make the inventory hold

**`archive-outcome.js` is the only interpreter.** Grep for
`result_status` across `client/js/archive-*.js` and it must appear in
exactly one file. The moment a second file branches on it, the two
branch sets drift and one of them starts rendering `partial` as `ok`.
Section I asserts this structurally.

**`archive-mask.js` has no DOM import.** It takes a string and a findings
array and returns a string or a refusal. That makes it testable under
plain Node with no harness at all, which matters because it is the one
function whose bug is a credential disclosure.

---

## C. Wireframes

ASCII only. These fix layout and information hierarchy, not visual style.

### C.1 Wide viewport, reader loaded (>= 1100px)

```
+----------------------------------------------------------------------------------+
| CloudeCode        [status light]  [audio]                    [settings] [log out] |
+----------------------------------------------------------------------------------+
| ARCHIVE  >  Joe-MBP-M1  >  claude-projects  >  -Users-jsugamele-Development-...   |
+-------------------+----------------------------+---------------------------------+
| HOSTS             | TRANSCRIPTS  (3,416)       | 6e4a3f8b-4751-...-fcdad830f741  |
|                   |  [ filter: session_ref  ]  | 30,805 lines / 91,950,363 bytes |
| v Joe-MBP-M1      |                            | sha256 57a48838...              |
|   19,562          | > 6e4a3f8b...30,805 lines  | [ attribution: CANNOT DETERMINE]|
|   v claude-...    |     2026-08-29  87.7 MiB   |                                 |
|     71 projects   |   84fd8f83...29,322 lines  | [ search in transcript ] [export]|
|     > -Users-...  |     2026-08-29  173.6 MiB  +---------------------------------+
|     > -Users-...  |   733414d4...23,542 lines  |  7109 | assistant | 18:22:04    |
|     > -Users-...  |     2026-08-29  94.6 MiB   |       | {"type":"assistant",... |
|   > local-agent   |   c0acad46...17,486 lines  |-------+-------------------------|
|     5 projects    |     2026-08-29 232.8 MiB   |  7110 | [progress x 14]      [v]|
|     ! 5 unattrib. |                            |-------+-------------------------|
|                   |  [ load 50 more ]          |  7111 | assistant | 18:22:11    |
| > Joseph's Mac    |                            |       | ...and hazard 5 applies |
|   mini (2)  1,477 |                            |       | [SECRET MASKED x1]      |
|                   |                            |-------+-------------------------|
+-------------------+----------------------------+  7112 | user      | 18:22:40    |
                                                  |       | 54,376,859 chars        |
                                                  |       | TOO LARGE TO RENDER     |
                                                  |       | [ download this body ]  |
                                                  +---------------------------------+
```

Three columns, fixed left rail, fixed-width transcript list, elastic
reader. The breadcrumb is the only thing that spans.

### C.2 The reader's line row, anatomised

```
+------+-----------+-----------------------------------------------+------+
| 7111 | assistant | body preview, wrapped, monospace               |  [i] |
|      | sonnet-4-5| 2026-01-09T18:22:11.483Z    5,501 chars        |      |
+------+-----------+-----------------------------------------------+------+
   ^        ^                        ^                                 ^
 line_no  role +                  body or                          open body
          record_type             placeholder                      inspector
```

`role` is NULL on 44.93% of bodies (measured 2026-08-31), so the second
column falls back to `record_type` and renders the literal string
`no role recorded` rather than a blank cell when both are absent. A blank
cell is a could-not-evaluate laundered into whitespace.

### C.3 Empty vs partial vs cannot-determine, side by side

This is the picture the whole design exists to produce. These three must
never be confusable at a glance, at any theme, at any radius.

```
   RESULT_STATUS = ok, result = []           RESULT_STATUS = partial
  +-----------------------------------+     +-----------------------------------+
  |                                   |     |  !  PARTIAL RESULT                |
  |         No matches.               |     |                                   |
  |                                   |     |  0 matches in the 801 transcripts |
  |  Searched all 3,416 transcripts   |     |  that were scanned.               |
  |  in this project. The archive     |     |  2,615 of 3,416 were NOT scanned: |
  |  contains no occurrence of        |     |  the 512 MiB byte budget was      |
  |  "zzzqqqxyznotfound".             |     |  spent after 801 transcripts.     |
  |                                   |     |                                   |
  |                                   |     |  [ Resume the scan ]              |
  +-----------------------------------+     +-----------------------------------+
     border: 1px solid var(--border)           border-left: 4px solid var(--warn)
     data-outcome="empty"                      data-outcome="partial"


   RESULT_STATUS = cannot_determine          SCOPE_STATUS = not_found
  +-----------------------------------+     +-----------------------------------+
  |  X  COULD NOT EVALUATE            |     |  ?  NOT FOUND                     |
  |                                   |     |                                   |
  |  This question was NOT answered.  |     |  There is no transcript 99999.    |
  |  The result below is not "no      |     |                                   |
  |  matches" - it is no measurement. |     |  This is a measurement: the       |
  |                                   |     |  archive was read and the id is   |
  |  cursor: cursor did not decode as |     |  not in it.                       |
  |  a v1 transcripts cursor:         |     |                                   |
  |  invalid base64url padding        |     |  [ Back to the project ]          |
  |                                   |     |                                   |
  |  [ Retry ]  [ Start from page 1 ] |     |                                   |
  +-----------------------------------+     +-----------------------------------+
     border-left: 4px solid var(--crit)        border: 1px dashed var(--border)
     data-outcome="cannot-determine"           data-outcome="not-found"
```

Four independent channels separate them: **the leading word**, **the
prose**, **the `data-outcome` attribute**, and **which actions exist**.
Colour and border are a fifth and sixth, and neither is load-bearing on
its own. Section I asserts the first four.

### C.4 Reader, nothing selected

```
+---------------------------------------------------------------+
|                                                               |
|   Pick a transcript.                                          |
|                                                               |
|   21,039 transcripts across 2 machines.                       |
|   19,588 of them (93.1%) are agent-scheme sidechain files.    |
|                                                               |
|   Live session: NOT CHECKED                                   |
|   This build does not correlate archived transcripts with     |
|   live terminal sessions. It is not asserting that there      |
|   are none.                                                   |
|                                                               |
+---------------------------------------------------------------+
```

`Live session: NOT CHECKED` is rendered verbatim. See section J.1.

### C.5 Narrow viewport (< 900px): one column at a time

The three columns become a stack with a single visible pane and a back
affordance. The route already names which pane is current, so the
narrow layout is a rendering choice over the same state, not a second
state machine.

```
 /archive                    /archive/p/12                 /archive/t/5767
+---------------------+     +---------------------+     +---------------------+
| ARCHIVE             |     | < -Users-jsugamele- |     | < 3,416 transcripts |
+---------------------+     |   Development-...   |     +---------------------+
| v Joe-MBP-M1        |     +---------------------+     | 6e4a3f8b-4751-...   |
|   19,562            |     | [ filter ]          |     | 30,805 lines        |
|   v claude-projects |     +---------------------+     | 87.7 MiB            |
|     > -Users-...  6 |     | 6e4a3f8b...         |     | [search] [export]   |
|     > -Users-...143 |     |   30,805 ln 87.7MiB |     +---------------------+
|     > -Users-...    |     | 84fd8f83...         |     | 7109 assistant      |
|       3,416         |     |   29,322 ln 173.6MB |     |  {"type":"assist... |
|   > local-agent-... |     | 733414d4...         |     +---------------------+
|                     |     |   23,542 ln 94.6MiB |     | 7110 [progress x14] |
| > Joseph's Mac      |     |                     |     +---------------------+
|   mini (2)    1,477 |     | [ load 50 more ]    |     | 7111 assistant      |
+---------------------+     +---------------------+     |  ...hazard 5 app... |
                                                        |  [SECRET MASKED x1] |
                                                        +---------------------+
```

**NORMATIVE for narrow:** the outcome blocks in C.3 do not shrink into
icons. On a 375px viewport a `cannot_determine` still renders the words
`COULD NOT EVALUATE` and the reason text, wrapped. Compressing the third
outcome into a glyph on small screens reintroduces the exact
indistinguishability this design exists to prevent, on the viewport
where a person is most likely to be skimming.

Breakpoint is **900px** for the three-to-one collapse and **700px** for
the reader's own metadata row to wrap under the body. 700px is already
the most common breakpoint in `client/css/` (7 occurrences, measured
2026-08-31), so this reuses an established value rather than inventing
one.

### C.6 Export modal, all three integrity outcomes

```
  VERIFIED (<= 8 MiB, 98.94% of transcripts)
 +--------------------------------------------------+
 |  Export transcript 4                             |
 |                                                  |
 |  0bd09502-f4be-48f2-ac56-dce81b92d20b.jsonl      |
 |  3,181,330 bytes                                 |
 |                                                  |
 |  OK  INTEGRITY VERIFIED BEFORE SENDING           |
 |  The server hashed the bytes it is about to      |
 |  send and they match the stored hash.            |
 |  expected 97e78444...7bf7d242                    |
 |  actual   97e78444...7bf7d242                    |
 |                                                  |
 |                        [ Cancel ]  [ Download ]  |
 +--------------------------------------------------+

  STREAMING (> 8 MiB, 223 transcripts)
 +--------------------------------------------------+
 |  Export transcript 5767                          |
 |                                                  |
 |  6e4a3f8b-...-fcdad830f741.jsonl                 |
 |  91,950,363 bytes                                |
 |                                                  |
 |  X  INTEGRITY: COULD NOT BE EVALUATED            |
 |  This file is too large to verify before         |
 |  sending. It streams, and uvicorn implements no  |
 |  HTTP trailers, so there is no actual hash.      |
 |  This download is NOT verified. It is also not   |
 |  known to be corrupt. Check it yourself:         |
 |                                                  |
 |    shasum -a 256 <file>                          |
 |    57a488382cae743e009cee83f60bfbae43820c4ff...  |
 |                                          [copy]  |
 |                                                  |
 |  ! This filename is unique in the archive.       |
 |                                                  |
 |                        [ Cancel ]  [ Download ]  |
 +--------------------------------------------------+

  BUSY (503)
 +--------------------------------------------------+
 |  Export transcript 5767                          |
 |                                                  |
 |  !  THE SERVER IS BUSY                           |
 |  The export was not attempted. Nothing was       |
 |  downloaded and nothing failed - the server      |
 |  declined to start right now.                    |
 |                                                  |
 |                        [ Cancel ]  [ Retry ]     |
 +--------------------------------------------------+
```

**NORMATIVE:** the streaming block is not dismissible, is not styled as
success, and carries no green. It is a COULD-NOT-EVALUATE and it renders
as one. See section J.3.

**NORMATIVE:** a 503 renders as busy with a Retry. It never renders as
"download failed", because nothing failed.

### C.7 Filename collision warning

```
 |  ! 14 transcripts in this archive download as    |
 |    journal.jsonl. Your browser will save this    |
 |    as journal-3.jsonl or overwrite an existing   |
 |    file, depending on its settings.              |
 |    [ Download as 6e4a3f8b-....jsonl instead ]    |
```

Measured 2026-08-31: `session_ref = 'journal'` on 14 transcripts,
`'audit'` on 5, `'agent-a877057'` on 4. The server's
`content-disposition` filename is derived from `session_ref`, so those
14 all arrive as the same name.

---

## D. The state model

**NORMATIVE.** Every view in this screen is in exactly one of the states
below at any moment. There is no implicit "default" state and no state
that means "still figuring it out".

### D.1 The six outcome tokens

`archive-outcome.js` maps an envelope to exactly one token. This is the
entire vocabulary of the UI.

| Token | Produced when | Renders as |
|---|---|---|
| `ok` | `result_status == "ok"` and `result` is non-empty | The content. |
| `empty` | `result_status == "ok"` and `result` is empty | "No X." plus the scope that WAS fully searched. A positive statement. |
| `partial` | `result_status == "partial"` | Whatever was found, plus a banner naming what was not reached, plus a resume action. |
| `cannot-determine` | `result_status == "cannot_determine"` OR `scope_status == "cannot_determine"` | The COULD NOT EVALUATE block with the `unevaluated[]` reasons rendered verbatim. Never an empty state. |
| `not-found` | `result_status == "not_found"` OR `scope_status == "not_found"` | "There is no X `<id>`." A measurement, distinct from cannot-determine. |
| `transport-error` | The fetch itself rejected, or the response was not JSON, or `result_status` is absent or unrecognised | Treated as `cannot-determine` in severity but labelled distinctly, because "the server did not answer" and "the server answered that it could not evaluate" are different findings. |

**`transport-error` is why `result_status` is checked for membership,
not just compared.** A response missing `result_status` entirely is a
server bug, and the client must not silently treat the absent field as
`ok`. `archive-outcome.js` throws nothing and returns `transport-error`.

```javascript
// client/js/archive-outcome.js  (the whole interpretation surface)

const RESULT_STATUSES = ['ok', 'partial', 'cannot_determine', 'not_found'];
const SCOPE_STATUSES  = ['resolved', 'not_found', 'cannot_determine'];

/**
 * Description: reduce an API envelope to exactly one outcome token.
 *   This is the ONLY function in the client permitted to read
 *   result_status or scope_status.
 * Inputs: envelope (object|null) - a parsed response body, or null if
 *   the fetch never produced one.
 * Output: {token: string, reasons: Array<{subject,reason}>, meta: object}
 *   token is one of ok|empty|partial|cannot-determine|not-found|transport-error.
 * Example:
 *   classify({result: [], result_status: 'ok', scope_status: 'resolved',
 *             unevaluated: [], meta: {}})  // -> {token: 'empty', ...}
 */
function classify(envelope) {
    if (!envelope || typeof envelope !== 'object') {
        return { token: 'transport-error', reasons: [
            { subject: 'response', reason: 'no parsable response body' }
        ], meta: {} };
    }
    const rs = envelope.result_status;
    const ss = envelope.scope_status;
    const reasons = Array.isArray(envelope.unevaluated) ? envelope.unevaluated : [];
    const meta = (envelope.meta && typeof envelope.meta === 'object') ? envelope.meta : {};

    // An unrecognised or absent status is NOT ok. Fail toward the third
    // outcome, never toward the first.
    if (RESULT_STATUSES.indexOf(rs) === -1 || SCOPE_STATUSES.indexOf(ss) === -1) {
        return { token: 'transport-error', reasons: reasons.length ? reasons : [
            { subject: 'envelope',
              reason: 'result_status=' + String(rs) + ' scope_status=' + String(ss) +
                      ' is not a value this client recognises' }
        ], meta: meta };
    }

    if (ss === 'not_found' || rs === 'not_found') {
        return { token: 'not-found', reasons: reasons, meta: meta };
    }
    if (ss === 'cannot_determine' || rs === 'cannot_determine') {
        return { token: 'cannot-determine', reasons: reasons, meta: meta };
    }
    if (rs === 'partial') {
        return { token: 'partial', reasons: reasons, meta: meta };
    }
    // rs === 'ok'
    const r = envelope.result;
    const isEmpty = r === null || r === undefined ||
                    (Array.isArray(r) && r.length === 0);
    return { token: isEmpty ? 'empty' : 'ok', reasons: reasons, meta: meta };
}
```

### D.2 The two states that are NOT outcome tokens

| State | Meaning | Rule |
|---|---|---|
| `idle` | Nothing has been asked yet. | Renders a prompt to choose something. NEVER renders an empty state. "You have not searched" and "your search found nothing" are different. |
| `loading` | A request is in flight. | **NORMATIVE: every `loading` state carries a deadline.** On expiry it becomes `transport-error` with the reason `no response in <n>s`. A spinner with no terminal condition is a state that can never fail, which is hazard 39 in its purest form. |

Deadlines, by request class:

| Request | Deadline | Basis |
|---|---:|---|
| Hierarchy (`hosts`, `corpora`, `projects`) | 10 s | Indexed reads, measured sub-millisecond. |
| Transcript detail, line spine | 15 s | Full 30,805-row spine measured 0.132 s server-side. |
| Body fetch | 30 s | A 54 MB body is a legitimate slow transfer. |
| Search | 45 s | A `budget_exhausted` scan measured 1.70 s; 45 s allows for a cold page cache and a loaded host. |
| Export preflight | 20 s | Headers only. |

### D.3 Per-view state tables

**View: navigation rail (`archive-nav.js`)**

| State | Trigger | What renders |
|---|---|---|
| `idle` | Screen opened, nothing expanded | Host list request fires immediately; this state is momentary. |
| `loading` | Request in flight | Skeleton rows at the level being expanded. Never replaces already-loaded siblings. |
| `ok` | Hosts/corpora/projects returned | The tree. |
| `empty` | `ok` with `[]` at some level | "This corpus has no projects." plus the corpus key, so it reads as a fact about a named thing. |
| `partial` | Any level returns `partial` | The rows that came back, plus an inline "N more not listed" row with a load action. |
| `cannot-determine` | Any level | That branch shows the COULD NOT EVALUATE block INLINE, at that node. The rest of the tree stays usable. **NORMATIVE: a failed branch never collapses silently into a leaf.** |
| `not-found` | A host/corpus/project id in the URL does not exist | Rail renders normally; the breadcrumb carries the not-found block. |
| `transport-error` | Fetch failed or deadline expired | Same placement as cannot-determine, different label. |

Note the `unattributed` affordance: `GET /archive/hosts/1/corpora`
returned `unattributed_transcript_count: 5` for corpus 2 (measured
2026-08-31). The rail renders that as its own child node,
`! 5 unattributed`, because a transcript that belongs to no project is
invisible from the project tree by construction. That is the same
never-onboarded blind-spot class the Infrastructure hazard list is full
of, and the fix is to give the missing thing a shape.

**View: transcript list (`archive-transcript-list.js`)**

| State | What renders |
|---|---|
| `idle` | "Pick a project." |
| `loading` | Skeleton rows, count unknown, no fabricated total. |
| `ok` | Rows plus `[ load 50 more ]` when `has_more === true`. |
| `empty` | "This project has 0 transcripts." |
| `partial` | Rows plus the partial banner. |
| `cannot-determine` | Block. **The malformed-cursor case gets two actions: `Retry` and `Start from page 1`.** It does NOT silently restart, because a client paging 3,416 transcripts that silently restarts renders duplicates forever. |
| `not-found` | "There is no project `<id>`." |

**NORMATIVE on `has_more`:** the server returns `has_more: null` on every
failure path (verified live: the `budget_exhausted` search returned
`"has_more": null`). `null` is not `false`. The client renders the
load-more control only on strict `=== true`, and renders
`whether there is more: NOT KNOWN` on `null`. Treating `null` as `false`
is a claim that the end of the list was reached when no list was read.

**View: reader (`archive-reader.js`)**

| State | What renders |
|---|---|
| `idle` | C.4, including `Live session: NOT CHECKED`. |
| `loading` | The transcript header (already known from the list) plus a spine skeleton. |
| `ok` | The virtual list. |
| `empty` | "This transcript has 0 lines." Only reachable for a zero-line transcript; the corpus has none as of 2026-08-31, so this state is rare, not dead. |
| `partial` | Spine loaded up to line N; the list ends in a `more lines not loaded` sentinel row that is itself a scroll target and fetches on reach. |
| `cannot-determine` | Block, in place of the list. The header stays, because the header's facts came from a different, successful request. |
| `not-found` | "There is no transcript `<id>`." Measured shape: HTTP 404, `result: null`, `unevaluated: [{subject: "transcript:99999", reason: "no row in message_transcripts with id 99999"}]`. |

**Per-line body state** is a second, independent axis. A line row is in
one of these regardless of the reader's own state:

| Line body state | Trigger | Renders |
|---|---|---|
| `not-requested` | Outside the fetch window | Placeholder sized from `body_chars`. No spinner. |
| `loading` | Fetch in flight | Inline shimmer on that row only. |
| `included` | Body present | The body, masked if it carries findings. |
| `gated-soft` | `body_chars > BODY_INLINE_MAX` (262,144) | Size, plus `[ render anyway ]`. |
| `gated-hard` | `body_chars > BODY_RENDER_HARD_MAX` (2,097,152) | Size, plus `[ download this body ]`. **No render option.** |
| `withheld-server` | `body_state == "withheld_too_large"` | The server's own refusal, plus `body_href`. |
| `mask-refused` | Findings present but not maskable | Section F. The body is NOT rendered. |
| `cannot-determine` | Body fetch failed | Block, on that row. Never a blank row. |

**View: search (`archive-search.js`)**

| State | What renders |
|---|---|
| `idle` | Empty input, no result region at all. |
| `loading` | "Scanning..." plus `transcripts_scanned` / `transcripts_in_scope` as they are known. **Never a byte-based progress bar.** Section K.2. |
| `ok` | Results. |
| `empty` | C.3 left panel: "No matches. Searched all N transcripts in this scope." |
| `partial` | C.3 right panel. Real measured copy, 2026-08-31: `2615 of 3416 transcripts were not scanned: byte budget 536870912 was spent after 801 transcripts`. Plus `[ Resume the scan ]` driven by `meta.scan.resume_cursor`. |
| `cannot-determine` | Block. |
| `not-found` | Scope id does not exist. |

**View: export (`archive-export.js`)**

| State | What renders |
|---|---|
| `idle` | Modal not open. |
| `preflight` | "Checking..." with a 20 s deadline. |
| `verified` | C.6 top. The only state that may be styled as success. |
| `unverifiable` | C.6 middle. **NORMATIVE: not dismissible, no green, no checkmark.** |
| `too-large-for-verify` | The 413. Measured shape below; it is a `cannot_determine` envelope carrying `meta.stream_href`, and the UI transitions straight to `unverifiable` for that href rather than showing an error. |
| `busy` | 503. C.6 bottom. |
| `not-found` | 404. |
| `blocked-no-credential` | See section K.4. The download cannot be started at all against the API as it stands today. |

### D.4 The state object

```javascript
// client/js/archive-state.js
const initialState = {
    route:      { view: 'root', hostId: null, corpusId: null,
                  projectId: null, transcriptId: null, lineNo: null },
    nav:        { token: 'idle', hosts: [], expanded: {}, reasons: [] },
    list:       { token: 'idle', projectId: null, rows: [],
                  nextCursor: null, hasMore: null, reasons: [] },
    reader:     { token: 'idle', transcriptId: null, header: null,
                  spine: [], spineComplete: false, reasons: [] },
    bodies:     { /* owned by archive-body-cache.js, not the reducer */ },
    search:     { token: 'idle', q: '', scope: null, hits: [],
                  scan: null, resumeCursor: null, reasons: [] },
    exportUI:   { token: 'idle', transcriptId: null, headers: null,
                  reasons: [] },
    liveSession:{ token: 'not-checked' }   // NEVER any other value in v1
};
```

`liveSession` is a field with exactly one permitted value. It exists so
that the reader has something concrete to render as `NOT CHECKED`
instead of rendering nothing, and so that a future implementer sees the
slot rather than inventing an absence. Section J.1.

---

## E. `api.js` additions

**NORMATIVE: every archive call goes through a NEW method,
`callEnvelope()`, not the existing `call()`.**

**Measured 2026-08-31: `client/js/api.js` is 1,491 lines and contains
`async call(endpoint, options, _meta)` at line 67. It does NOT contain
`callEnvelope`.** This is new code, not a modification of existing
behaviour.

Why a second method rather than reusing `call()`: `call()` unwraps and
throws on non-2xx. Every archive route can legitimately return a 200
carrying `cannot_determine`, and a 404 carrying a fully-formed envelope
that the UI must render (measured: `GET /archive/transcripts/99999`
returns HTTP 404 with a complete envelope, not an error page). Passing
that through `call()` means either losing the envelope on the throw path
or teaching `call()` archive semantics. A parallel method keeps three
working screens untouched.

```javascript
/**
 * Description: perform an archive API call and return the parsed
 *   three-outcome envelope WITHOUT throwing on a non-2xx status. Every
 *   archive route returns an envelope on 200, 400, 404, 413 and 503,
 *   and all five are renderable findings rather than errors.
 * Inputs: endpoint (string) - path under /api/v1, leading slash.
 *         options (object) - fetch options; {timeoutMs} adds a deadline.
 * Output: Promise<{envelope: object|null, httpStatus: number|null,
 *                  headers: Headers|null, transportError: string|null}>
 *   Never rejects. A network failure or a deadline expiry resolves with
 *   envelope=null and transportError set.
 * Example:
 *   const r = await api.callEnvelope('/archive/transcripts/99999');
 *   // r.httpStatus === 404, r.envelope.result_status === 'not_found'
 */
async callEnvelope(endpoint, options = {}) { /* ... */ }
```

### E.1 One method per endpoint

All eleven wrap `callEnvelope`. None of them interprets `result_status`;
that is `archive-outcome.js`'s job exclusively.

```javascript
// --- hierarchy -------------------------------------------------------
listArchiveHosts()
listArchiveCorpora(hostId, { limit, cursor } = {})
listArchiveProjects(corpusId, { limit, cursor } = {})
listArchiveUnattributed(corpusId, { limit, cursor } = {})
listArchiveTranscripts(projectId, { limit, cursor } = {})

// --- one transcript --------------------------------------------------
getArchiveTranscript(transcriptId)
listArchiveLines(transcriptId, { limit, cursor, includeBodies,
                                 maxPageBytes, role, recordType, model } = {})
getArchiveBody(bodyId)
listArchiveSubagents(transcriptId, { limit, cursor } = {})

// --- search ----------------------------------------------------------
searchArchive({ q, transcriptId, projectId, corpusId, hostId,
                limit, cursor, caseSensitive } = {})

// --- export ----------------------------------------------------------
preflightArchiveExport(transcriptId, { verified } = {})
```

`preflightArchiveExport` is a `HEAD`-shaped call that reads headers and
does not consume the body. It exists because the download itself is not
a fetch. See section K.4 for the credential problem that blocks the
download step.

### E.2 Real response shapes, measured

These are pasted from live responses on 2026-08-31, not written from the
spec. Where the spec and the live response differ, the live response
wins and the difference is flagged in section K.

**`GET /api/v1/archive/hosts`**

```json
{
  "result": [
    {"host_id": 1, "machine_id": "F95816BC-2819-53B5-98E9-72450A37AADF",
     "machine_id_scheme": "platform_uuid", "display_name": "Joe-MBP-M1",
     "hostname": "Joe-MBP-M1", "platform": "Darwin 25.6.0",
     "first_seen_at": "2026-08-30T16:01:00.290244Z",
     "corpus_count": 2, "transcript_count": 19562},
    {"host_id": 2, "machine_id": "726E10C9-E70D-5F9E-ACA6-F5CB0D79BA40",
     "machine_id_scheme": "platform_uuid",
     "display_name": "Joseph’s Mac mini (2)",
     "hostname": "mac-mini-m4.local", "platform": "Darwin 25.6.0",
     "first_seen_at": "2026-08-30T16:02:34.230512Z",
     "corpus_count": 1, "transcript_count": 1477}
  ],
  "result_status": "ok", "scope_status": "resolved", "unevaluated": [],
  "meta": {"totals": {"hosts": 2,
                      "transcripts_attributed_to_a_host": 21039,
                      "transcripts_with_no_host_id": 0}}
}
```

Note `display_name` carries a U+2019 RIGHT SINGLE QUOTATION MARK. The
rail must not assume ASCII, and must not `innerHTML` it without escaping.

**`GET /api/v1/archive/hosts/1/corpora?limit=3`**

```json
{"result": [
  {"corpus_id": 1, "corpus_key": "claude-projects",
   "root_path": "/Users/jsugamele/.claude/projects",
   "collected_at": "2026-08-30T16:01:00.308861Z", "has_manifest": false,
   "project_count": 71, "transcript_count": 19548,
   "unattributed_transcript_count": 0},
  {"corpus_id": 2, "corpus_key": "local-agent-mode-sessions",
   "root_path": "/Users/jsugamele/Library/Application Support/Claude/local-agent-mode-sessions",
   "collected_at": "2026-08-30T16:01:45.993463Z", "has_manifest": true,
   "project_count": 5, "transcript_count": 14,
   "unattributed_transcript_count": 5}],
 "result_status": "ok", "scope_status": "resolved", "unevaluated": [],
 "meta": {"scope": {"kind": "host", "host_id": 1,
                    "display_name": "Joe-MBP-M1"}}}
```

**`GET /api/v1/archive/transcripts/5767`** (abridged; every key shown is
real)

```json
{"result": {
  "transcript_id": 5767,
  "session_ref": "6e4a3f8b-4751-44b5-a100-fcdad830f741",
  "session_ref_scheme": "uuid",
  "source_path": "-Users-jsugamele-Development-Assistants-Infrastructure/6e4a3f8b-4751-44b5-a100-fcdad830f741.jsonl",
  "line_ending": "LF", "has_trailing_newline": true,
  "line_count": 30805, "raw_byte_length": 91950363,
  "content_sha256": "57a488382cae743e009cee83f60bfbae43820c4ff045d7403d7a101f3c2b6d1b",
  "ingested_at": "2026-08-29T23:14:23.800662Z",
  "host":    {"host_id": 1, "display_name": "Joe-MBP-M1"},
  "corpus":  {"corpus_id": 1, "corpus_key": "claude-projects"},
  "project": {"project_id": 12,
              "slug": "-Users-jsugamele-Development-Assistants-Infrastructure"},
  "host_attribution": "cannot_determine",
  "project_attribution": "derived",
  "attribution_state": "cannot_determine",
  "counts": {"appearances": 30805, "ok_lines": 30805, "blank_lines": 0,
             "invalid_json_lines": 0, "lines_without_body": 0,
             "lines_with_raw_line": 0, "subagent_lines": 0,
             "unverified_lines": 0},
  "export": {"stream_href": "/api/v1/archive/transcripts/5767/export",
             "verified_href": "/api/v1/archive/transcripts/5767/export/verified",
             "verified_available": false,
             "verified_unavailable_reason":
               "raw_byte_length 91950363 exceeds VERIFY_BEFORE_SEND_MAX_BYTES 8388608; use the streaming export and check the sha256 trailer"}},
 "result_status": "ok", "scope_status": "resolved", "unevaluated": [], "meta": {}}
```

**`attribution_state` is itself a three-outcome field and the header must
render it as one.** Transcript 5767 is `cannot_determine`. The header
shows `attribution: CANNOT DETERMINE`, not a blank and not a guess.

**`GET /api/v1/archive/transcripts/4/lines?include_bodies=true&limit=2&cursor=...`**
(one row, `body_json` elided)

```json
{"appearance_id": 141, "line_no": 0, "seq_in_file": null,
 "line_status": "ok", "serializer_style": "compact",
 "line_byte_length": 235, "fidelity_outcome": "fidelity_verified",
 "is_sidechain": false, "agent_id": null, "body_id": 87,
 "message_uuid": null, "parent_uuid": null, "ts": null,
 "origin_session_ref": null, "record_type": "file-history-snapshot",
 "role": null, "model": null, "compact_subtype": null,
 "is_compact_boundary": false, "secret_finding_count": 0,
 "body_chars": 235, "body_bytes": 235, "body_state": "included",
 "body_href": "/api/v1/archive/bodies/87",
 "body_json": "..."}
```

and its `meta`:

```json
{"offset_units": "unicode_code_points",
 "offset_units_utf16_available": true,
 "body_size_units": "unicode_code_points",
 "body_bytes_units": "unicode_code_points",
 "body_bytes_note": "body_bytes is a DEPRECATED alias for body_chars and counts UNICODE CODE POINTS, not bytes. Read body_chars.",
 "paging": {"limit": 3, "returned": 3, "has_more": true,
            "next_cursor": "eyJsaW5lX25vIjoyLCJ2IjoxfQ"},
 "scope": {"kind": "transcript", "transcript_id": 4, "line_count": 980},
 "filters": {"role": null, "record_type": null, "model": null,
             "counts_are": "scanned_within_this_transcript_only"},
 "bodies": {"included": true, "page_bytes": 4213,
            "max_page_bytes": 1048576, "stopped_early": false},
 "lines_with_null_ts": 1}
```

**NORMATIVE: read `body_chars`, never `body_bytes`.** The server itself
says the name lies. Every size gate in section G is expressed in
`body_chars`.

**`GET /api/v1/archive/search?q=zzzqqqxyznotfound&project_id=12&limit=5`** -
the `partial` case, measured

```json
{"result": [],
 "result_status": "partial", "scope_status": "resolved",
 "unevaluated": [{"subject": "project:12",
   "reason": "2615 of 3416 transcripts were not scanned: byte budget 536870912 was spent after 801 transcripts"}],
 "meta": {
   "query": {"q": "zzzqqqxyznotfound", "case_sensitive": false},
   "scope": {"kind": "project", "project_id": 12, "transcripts_in_scope": 3416},
   "scan": {"status": "budget_exhausted", "transcripts_scanned": 801,
            "transcripts_not_scanned": 2615,
            "bytes_scanned": 551648566, "budget_transcripts": 2000,
            "budget_bytes": 536870912, "elapsed_seconds": 1.701709,
            "resume_cursor": "eyJieXRlcyI6NTUxNjQ4NTY2LCJsaW5lX25vIjotMSwic2Nhbm5lZCI6ODAxLCJ0X2lkIjo2NTY5LCJ0X2luZ2VzdGVkX2F0IjoiMjAyNi0wOC0yOVQyMzozMDoxMS41NjU1MzhaIiwidiI6MX0"},
   "paging": {"limit": 5, "returned": 0, "has_more": null,
              "next_cursor": null}}}
```

This one payload demonstrates three separate rules at once. `result` is
`[]` but the outcome is NOT `empty`. `has_more` is `null`, not `false`.
And `bytes_scanned` (551,648,566) EXCEEDS `budget_bytes` (536,870,912)
by 14,777,654 bytes, which is 2.75% over, so it is a charge rather than a
metered quantity. Section K.2.

**`GET /api/v1/archive/transcripts/99999`** - HTTP 404

```json
{"result": null, "result_status": "not_found", "scope_status": "not_found",
 "unevaluated": [{"subject": "transcript:99999",
                  "reason": "no row in message_transcripts with id 99999"}],
 "meta": {}}
```

---

## F. Secret masking (NORMATIVE)

**The server FLAGS. It never redacts.** `docs/message-browser-api.md`
section 2 makes byte-exactness the whole point of the archive: a body
that came back redacted would not be the bytes that were on disk, and the
archive would be worthless as evidence. So the server returns the real
body plus a findings array, and **masking is the client's job.**

That makes this the one function in the UI whose bug is a credential
disclosure, and it is a bug that is easy to write and impossible to see.

### F.1 Why the code-point offsets are a trap

A JavaScript string is indexed in **UTF-16 code units**. A Python string
is indexed in **code points**. Every character outside the Basic
Multilingual Plane (emoji, most CJK extensions, mathematical alphanumerics)
is one code point and TWO UTF-16 code units. So a Python-computed offset
and a JavaScript index diverge by one for every astral character that
precedes them, and the divergence grows monotonically through the body.

The server ships both. `match_offset` / `match_length` are code points.
`match_offset_utf16` / `match_length_utf16` are UTF-16 code units, and
those are the ONLY ones a JavaScript client may use.

**Measured on real body 379, 2026-08-31.** Three findings, all the same
credential (`value_sha256` identical across all three), all 40 characters:

| Finding | `match_offset` | `match_offset_utf16` | Drift | `utf16_state` |
|---|---:|---:|---:|---|
| 1 | 5,197 | 5,201 | **+4** | `computed` |
| 2 | 11,058 | 11,066 | **+8** | `computed` |
| 3 | 17,340 | 17,352 | **+12** | `computed` |

Body 379 is 19,831 code points and **19,843 UTF-16 code units**. Twelve
astral characters, and the drift accumulates through all three findings.

Slicing finding 1 with each offset, in a real JavaScript-equivalent
UTF-16 index space:

```
window using match_offset_utf16 = 5201, length 40:
    <the full 40-character credential>            CORRECT

window using match_offset       = 5197, length 40:
    "en: " + <the first 36 characters of the credential>
                                                  WRONG - slid 4 left
```

The wrong window covers four characters of the preceding text
(`"en: "`, the tail of a JSON key) and stops four characters short of the
end of the credential. **Masking with it leaves the LAST 4 CHARACTERS OF
THE 40-CHARACTER CREDENTIAL rendered on screen.** Finding 3 would leak
12 of 40.

Nothing about this failure is visible. There is no error, no warning, and
the masked output looks entirely plausible: a run of asterisks with a
short hex tail that reads like part of the surrounding text.

### F.2 `utf16_state` is a three-outcome field

`utf16_state` on each finding is one of:

| Value | Meaning | Client behaviour |
|---|---|---|
| `computed` | The UTF-16 pair was derived from the body and is trustworthy. | Mask with it. |
| `cannot_determine` | The server could not compute the pair. | **NORMATIVE: do NOT fall back to the code-point offsets. Do NOT render the body.** |

**NORMATIVE: `cannot_determine` on any finding in a body poisons the
WHOLE body.** Not just that finding's window. A body carrying a finding
whose position is unknown is a body with a credential at an unknown
location, and there is no partial masking that is safe. The line renders
`mask-refused` (section D.3) with the body withheld, the finding count,
and a link to the raw body endpoint. The person can go and read the raw
JSON deliberately; they will not have it appear under their eyes by
accident.

The same refusal applies when `match_offset_utf16` or `match_length_utf16`
is absent, non-integer, negative, or extends past the end of the string.
Every one of those is "I do not know where the secret is".

### F.3 The implementation

```javascript
// client/js/archive-mask.js
//
// Masks flagged secret material in a body before it is rendered.
//
// WHY THIS FILE HAS NO DOM IMPORT: it is the one function in this screen
// whose bug is a credential disclosure. Keeping it a pure
// string-in/string-out function means it is testable under plain node
// with no harness, and it means no rendering path can accidentally reach
// around it.
//
// MEASURED 2026-08-31 on body 379: using match_offset (code points)
// instead of match_offset_utf16 slides the window 4 code units left and
// leaves the last 4 characters of a 40-character credential visible.
// The drift grows to 12 by the third finding in the same body. There is
// no error and no visible symptom.

'use strict';

/** Sentinel returned when the body must not be rendered at all. */
const MASK_REFUSED = 'mask-refused';

/**
 * Description: is one finding usable for masking a JavaScript string?
 * Inputs: f (object) - one entry from an API `secrets` array.
 *         len (number) - the body's length in UTF-16 code units.
 * Output: boolean - true only if the UTF-16 window is fully known and
 *   lies inside the string.
 */
function _findingIsUsable(f, len) {
    if (!f || typeof f !== 'object') return false;
    if (f.utf16_state !== 'computed') return false;      // three-outcome gate
    const o = f.match_offset_utf16;
    const l = f.match_length_utf16;
    if (!Number.isInteger(o) || !Number.isInteger(l)) return false;
    if (o < 0 || l <= 0) return false;
    if (o + l > len) return false;                        // past the end
    return true;
}

/**
 * Description: replace every flagged secret in a body with a fixed-width
 *   marker, using ONLY the UTF-16 offsets. Refuses outright rather than
 *   masking approximately.
 * Inputs: body (string) - the body_json exactly as the server sent it.
 *         findings (Array|null|undefined) - the `secrets` array from
 *           GET /archive/bodies/{id}. `null` and `undefined` are NOT the
 *           same as `[]`; see the return contract.
 *         declaredCount (number) - `secret_finding_count` from the line
 *           or body row. The independent count of how many secrets the
 *           server believes are in this body.
 * Output: {status: 'ok', text: string, masked: number}
 *      or {status: MASK_REFUSED, reason: string, findingCount: number}
 *   `status === MASK_REFUSED` means the caller MUST NOT render `body`.
 * Example:
 *   maskBody(s, [{utf16_state:'computed', match_offset_utf16:5201,
 *                 match_length_utf16:40}], 1)
 *   // -> {status:'ok', text:'... [SECRET REDACTED IN THIS VIEW] ...', masked:1}
 */
function maskBody(body, findings, declaredCount) {
    if (typeof body !== 'string') {
        return { status: MASK_REFUSED,
                 reason: 'body is not a string',
                 findingCount: declaredCount || 0 };
    }

    const declared = Number.isInteger(declaredCount) ? declaredCount : 0;

    // CASE 1: the server says there are no secrets. Render as-is.
    if (declared === 0 && (!findings || findings.length === 0)) {
        return { status: 'ok', text: body, masked: 0 };
    }

    // CASE 2: the server says there ARE secrets but gave us no findings
    // array. This is the /lines?include_bodies=true shape as measured on
    // 2026-08-31 (see section K.1). We know a credential is in this
    // string and we do not know where. Refuse.
    if (declared > 0 && (!Array.isArray(findings) || findings.length === 0)) {
        return { status: MASK_REFUSED,
                 reason: 'the body declares ' + declared + ' secret finding(s) ' +
                         'but carries no findings array, so their positions ' +
                         'are unknown',
                 findingCount: declared };
    }

    const list = Array.isArray(findings) ? findings : [];

    // CASE 3: fewer findings than declared. Something was dropped in
    // transit or in serialization. Masking what we have would leave the
    // rest visible while LOOKING masked, which is worse than refusing.
    if (declared > 0 && list.length < declared) {
        return { status: MASK_REFUSED,
                 reason: 'the body declares ' + declared + ' secret finding(s) ' +
                         'but only ' + list.length + ' were returned',
                 findingCount: declared };
    }

    // CASE 4: any finding whose UTF-16 window is not fully known poisons
    // the whole body. There is no safe partial mask.
    const len = body.length;   // UTF-16 code units, which is what we want
    for (let i = 0; i < list.length; i++) {
        if (!_findingIsUsable(list[i], len)) {
            const st = list[i] && list[i].utf16_state;
            return { status: MASK_REFUSED,
                     reason: st === 'cannot_determine'
                       ? 'a finding reports utf16_state=cannot_determine, so its ' +
                         'position in a JavaScript string is not known'
                       : 'a finding has no usable UTF-16 window ' +
                         '(offset=' + (list[i] && list[i].match_offset_utf16) +
                         ' length=' + (list[i] && list[i].match_length_utf16) +
                         ' bodyLength=' + len + ')',
                     findingCount: declared || list.length };
        }
    }

    // CASE 5: mask. Sort descending by offset and splice from the end so
    // that each replacement cannot move the offsets of the ones still to
    // be applied. Overlapping windows are merged first, because two
    // overlapping replacements applied independently would corrupt the
    // string.
    const windows = list
        .map(function (f) {
            return { start: f.match_offset_utf16,
                     end: f.match_offset_utf16 + f.match_length_utf16 };
        })
        .sort(function (a, b) { return a.start - b.start; });

    const merged = [];
    for (let i = 0; i < windows.length; i++) {
        const w = windows[i];
        const last = merged[merged.length - 1];
        if (last && w.start <= last.end) {
            if (w.end > last.end) last.end = w.end;
        } else {
            merged.push({ start: w.start, end: w.end });
        }
    }

    let out = body;
    for (let i = merged.length - 1; i >= 0; i--) {
        out = out.slice(0, merged[i].start) +
              SECRET_MARKER +
              out.slice(merged[i].end);
    }
    return { status: 'ok', text: out, masked: merged.length };
}

/**
 * The replacement text. Fixed width and self-describing: it must be
 * obvious that something was removed BY THIS VIEW, and not that the
 * archive stored asterisks. The archive is byte-exact; this is a lens.
 */
const SECRET_MARKER = '[SECRET REDACTED IN THIS VIEW]';
```

### F.4 Five rules that fall out of the above

1. **`body.length` is the correct bound check.** JavaScript's `.length`
   IS the UTF-16 code-unit count, which is exactly the unit the
   `_utf16` fields are in. Do not compute a code-point length with
   `[...body].length` and compare against a UTF-16 offset; that
   reintroduces the original bug in the validation.
2. **Splice from the highest offset down**, so applied replacements
   cannot shift the offsets of pending ones.
3. **Merge overlapping windows first.** Body 379's three findings are
   disjoint, but the merge is not defensive coding; overlapping detector
   hits are a real shape and two independent splices over one region
   produce garbage.
4. **The marker is fixed width, not proportional to the secret.** A
   marker whose length reveals the credential's length is a small leak
   and there is no reason to take it.
5. **`declaredCount` is an INDEPENDENT channel and it is checked.** Cases
   2 and 3 above exist because `secret_finding_count` and the `secrets`
   array come from different columns and can disagree. Trusting only the
   array means a body whose array was dropped renders unmasked with no
   complaint. This is a positive control on the masking input.

### F.5 What is deliberately NOT done

**The client does not run its own secret detector.** It masks what the
server flagged and nothing else. A client-side detector would find things
the server did not, which sounds like a win and is not: it would produce
a body that differs from the archive in ways nobody can predict or
reproduce, and it would create a second, drifting definition of what
counts as a secret. The server owns that definition.

**The client does not offer an unmask control.** The raw body is one
click away at `body_href` and that path is deliberate, explicit and
addressable. An unmask toggle sitting next to the masked text is a
control that gets clicked by reflex.

---

## G. Virtualization

### G.1 The numbers this must survive

Measured 2026-08-31.

| Case | Value | What breaks naively |
|---|---:|---|
| Transcripts in the corpus | 21,039 | Listing them all in one DOM. |
| Largest transcript by lines | 30,805 (id 5767) | 30,805 rows, and one IntersectionObserver per row. |
| Largest transcript by bytes | 244,117,661 (id 17266) | Any attempt to hold the whole thing. |
| Largest single line | 54,376,879 bytes (t19243 line 62) | A 54 MB `<pre>`. |
| Second largest single line | 37,404,061 bytes (t17266 line 3108) | Same, inside the 244 MB transcript. |
| `progress` records | 917,436 (37.49% of bodies) | 37% of every reader is noise. |

**Server-side line-metadata timings, from the API spec's own measurement
log:** 201 rows on a first page 0.0003 s; 501 rows starting at line
15,000 of the 30,805-line transcript 0.0016 s; all 30,805 rows metadata
only **0.132 s**. Paging cost is independent of depth because
`UNIQUE (transcript_id, line_no)` resolves both the equality and the
keyset range in one index search.

That 0.132 s is what makes the two-tier design possible.

### G.2 Two tiers

**Tier 1, the spine.** Metadata for EVERY line in the transcript, fetched
with `include_bodies=false` in pages of 500 and concatenated. Per line
the spine holds only:

```
line_no, record_type, role, ts, body_chars, body_id,
secret_finding_count, body_state, is_sidechain, line_status
```

At 30,805 lines that is roughly 30,805 small objects. It is fetched up
front, in the background, in 62 requests of 500, and the reader is usable
from the first page. **The spine is the only structure that must be
complete**, because scroll geometry depends on knowing how many lines
there are and roughly how tall each one is.

**Tier 2, bodies.** Fetched lazily for the visible window plus an
overscan margin, through `archive-body-cache.js`.

### G.3 The LRU is capped on BOTH axes

**NORMATIVE:**

```
BODY_CACHE_MAX_ENTRIES = 300
BODY_CACHE_MAX_CHARS   = 33_554_432    // 32 MiB of text
```

A count cap alone fails: 300 bodies at 54 MB each is not a cache, it is
an out-of-memory. A byte cap alone fails: two million 16-byte bodies
would fit under 32 MiB while making the map itself the problem. Eviction
runs until **both** predicates hold, least-recently-used first.

The cache also de-duplicates in-flight requests by `body_id`. Two visible
lines pointing at the same `body_id` is normal in this corpus (body 379
appears once, but the schema's `identity_key` exists precisely because
bodies are shared across appearances), and firing two fetches for one id
is wasted work that also double-charges the byte cap.

### G.4 Windowing: scroll handler plus offset table, NOT observers

**NORMATIVE: there is no `IntersectionObserver` per row.**

30,805 observers is its own outage. Each one is a live registration in
the compositor with its own root-margin geometry, and creating them all
takes long enough to block the main thread on the very transcript the
feature exists to open. The rejection is structural, not a preference.

The engine is `archive-virtual-list.js`:

```
Float64Array offsets   length N+1, offsets[i] = pixel top of row i,
                       offsets[N] = total content height
Float64Array heights   length N,   current best height estimate per row
```

**Estimating a height before the body is fetched.** From `body_chars` on
the spine, with the row's own chrome:

```
ROW_CHROME_PX      = 34          // gutter + role line
CHARS_PER_LINE     = 96          // at the reader's monospace measure
LINE_HEIGHT_PX     = 18
COLLAPSED_MAX_PX   = 240         // a long body is collapsed until opened

estimateHeight(row) =
    row.bodyState !== 'included'
      ? ROW_CHROME_PX + 44                        // placeholder, fixed
      : min(COLLAPSED_MAX_PX,
            ROW_CHROME_PX +
            LINE_HEIGHT_PX * ceil(row.body_chars / CHARS_PER_LINE))
```

`COLLAPSED_MAX_PX` is what keeps the estimate honest at the extremes:
without it, line 62 of transcript 19243 would estimate at roughly
10 million pixels and the scrollbar would become useless for the other
lines.

**Finding the window** is a binary search over `offsets` for
`scrollTop`, then a forward walk until the accumulated height exceeds the
viewport plus overscan. Both are O(log N) and O(visible), and neither
depends on N.

```javascript
/**
 * Description: index of the last row whose top is <= y.
 * Inputs: offsets (Float64Array, monotonically non-decreasing), y (number)
 * Output: number - row index, clamped to [0, offsets.length - 2].
 */
function rowAt(offsets, y) {
    let lo = 0, hi = offsets.length - 2;
    while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (offsets[mid] <= y) lo = mid; else hi = mid - 1;
    }
    return lo;
}
```

`OVERSCAN_ROWS = 12` above and below. Bodies are requested for the
visible window plus overscan; requests for rows that leave the window
before they resolve are not cancelled (the response still populates the
cache and the work is already paid for) but they are deprioritised
behind newly visible rows.

### G.5 Height reconciliation, and the anti-jump rule

Every estimate is wrong. Once a body renders, its real height is measured
and the offset table needs updating. Doing that synchronously per row
causes layout thrash; doing it wrongly causes the reader to jump under
the person's eyes, which on a 30,805-line document loses their place
permanently.

**The algorithm, per animation frame:**

1. Collect the rows whose measured height differs from `heights[i]` by
   more than `HEIGHT_EPSILON_PX = 0.5`. Sub-pixel churn is not a
   correction.
2. Let `delta` be the sum of corrections for rows whose index is
   **strictly less than the first visible row**.
3. Write the new heights, then rebuild `offsets` from the lowest changed
   index forward.
4. **If `delta !== 0`, set `scrollTop += delta` in the SAME frame**,
   before paint.

Step 4 is the whole point. A correction above the viewport moves every
subsequent row by `delta`; without the compensating scroll the content
visibly leaps. Doing it in a later frame produces a visible one-frame
jump, which is worse than not compensating at all because it looks like
a bug rather than like scrolling.

Rebuilding `offsets` from index `k` forward is O(N - k). At N = 30,805
that is a single pass over a `Float64Array`, which is microseconds. There
is no need for a Fenwick tree at this N, and a Fenwick tree would make the
anti-jump arithmetic considerably harder to get right. If a transcript
ever exceeds roughly 500,000 lines this decision should be revisited;
the largest today is 30,805.

**rAF throttling:** measurements accumulate into a pending set and are
applied once per frame. A `ResizeObserver` on the rendered rows (a
handful, not 30,805) feeds that set. `ResizeObserver` on the visible
window is fine; it is the per-row `IntersectionObserver` at full N that
is not.

### G.6 The three size gates

**NORMATIVE:**

| Gate | Value | Behaviour |
|---|---:|---|
| `BODY_INLINE_MAX` | 262,144 chars (256 Ki) | Body is fetched but NOT auto-rendered. The row shows the size and `[ render anyway ]`. |
| `BODY_RENDER_HARD_MAX` | 2,097,152 chars (2 Mi) | Body is NOT fetched and NOT rendered. The row shows the size and `[ download this body ]` only. **There is no render option.** |
| server `withheld_too_large` | `body_state` from the API | The server's own refusal, rendered as the server's finding, with `body_href`. |

**A 54 MB `<pre>` is a dead tab.** Not slow, dead: the layout pass over a
single text node of that size is not something the person can interrupt,
and the browser offers no way back. That is why the hard gate offers no
escape hatch at all. The soft gate at 256 Ki does, because a quarter of a
megabyte of JSON is merely unpleasant.

**The hard gate is evaluated from the spine, BEFORE the fetch.** This is
not an optimisation; it is the only thing that works. See section K.3:
the server's own `MAX_BODY_BYTES` is 67,108,864 and the largest body in
the corpus is 54,376,859 chars, so **the server gate never fires and the
54 MB body is served inline with `body_state: "included"`** (measured
2026-08-31 on transcript 19243 line 62). If the client waited for the
server to withhold it, the client would have already pulled 54 MB into
the tab. The `body_chars` field on the spine is what prevents that, and
it costs nothing.

### G.7 `progress` records: collapsed, counted, never hidden

917,436 of 2,447,028 bodies (37.49%) are `record_type: "progress"`.
Rendering them inline makes the reader more than a third noise.

**NORMATIVE:** a run of consecutive `progress` lines collapses into ONE
row carrying the count and the line range:

```
| 7110 | [ progress x 14 ]  lines 7110-7123                      [v] |
```

Expanding it renders the 14 real rows in place. The collapsed row
participates in the offset table as a single row of fixed height, so
expansion is an ordinary height correction handled by G.5.

**They are never hidden and never filtered out by default.** A filter
that silently removes 37% of a byte-exact archive is a client-side lie
about the file's contents. The chip states the count, so the person can
always see what is folded, and the fold is one click from being undone.

### G.8 The transcript list virtualizes too

3,416 transcripts in project 12 (measured 2026-08-31 from
`meta.scope.transcripts_in_scope`). The list uses the SAME
`archive-virtual-list.js` with a fixed row height, so there is no
reconciliation pass at all for it. One engine, two callers.

---

## H. Routing and screen registration, file by file

### H.1 Routes, and why `transcript_id` only

**NORMATIVE: every archive route addresses a NUMERIC `transcript_id`.
`session_ref` never appears in a URL.**

Measured 2026-08-31, `SELECT session_ref, COUNT(*) ... GROUP BY 1 HAVING
COUNT(*) > 1`:

| `session_ref` | Transcripts sharing it |
|---|---:|
| `journal` | **14** |
| `audit` | **5** |
| `agent-a877057` | 4 |
| `aaaaaaaa-0000-4000-8000-000000000001` | 3 |

`session_ref` is not unique and is not close to unique. A route
`/archive/s/journal` cannot resolve to a transcript. Worse, it would
resolve to one of fourteen with no error, which is the failure mode where
the link works for the sender and shows the recipient a different
document.

A second measured wrinkle: **19 transcripts carry
`session_ref_scheme = 'uuid'` while their `session_ref` is not a UUID at
all** - all fourteen `journal` rows are among them. So even a route that
tried to key on "the ones that look like UUIDs" would be keying on a
field whose own scheme label is wrong.

| Route | Meaning |
|---|---|
| `/archive` | Root. Rail loaded, nothing selected. |
| `/archive/p/<id>` | Project `<id>` selected; transcript list loaded. |
| `/archive/t/<id>` | Transcript `<id>` open in the reader. |
| `/archive/t/<id>/l/<n>` | Transcript `<id>`, scrolled to and highlighting line `<n>`. `<n>` is 0-BASED and is sent to the server as `start_line`, so the page opens at the line rather than at line 0. |

`<id>` and `<n>` are `[0-9]+` and nothing else. A non-numeric segment is
a client-side `cannot-determine` with the reason
`"<segment>" is not a numeric transcript id`, rendered in the existing
deep-link error banner, and NOT a silent redirect to `/archive`.

**Search state lives in the query string**, not the path:
`?q=hazard&scope=transcript`. It is a modifier on a view, not a view.

**NORMATIVE: a scan resume cursor is NEVER put in the URL.** Measured
2026-08-31: the `resume_cursor` from a `budget_exhausted` search is 147
characters of opaque base64url encoding `{bytes, line_no, scanned, t_id,
t_ingested_at, v}`. Two reasons it must not be shared. It is opaque, so a
recipient cannot tell what they are resuming. And it encodes a position
in ONE scan of a database that a background ingest writes every 900
seconds, so a shared cursor is a stale position in someone else's
abandoned scan - which is meaningless to the recipient and not obviously
meaningless, the worst combination.

### H.2 `client/index.html`

Measured 2026-08-31: the file is **1,030 lines**.

**Three edits.**

**(1) Stylesheets, after line 147.** Line 147 is currently the last
`<link rel="stylesheet">`, `terminal-opacity.css`. Add six after it:

```html
<link rel="stylesheet" href="/static/css/archive-outcomes.css" />
<link rel="stylesheet" href="/static/css/archive-screen.css" />
<link rel="stylesheet" href="/static/css/archive-nav.css" />
<link rel="stylesheet" href="/static/css/archive-reader.css" />
<link rel="stylesheet" href="/static/css/archive-search.css" />
<link rel="stylesheet" href="/static/css/archive-export.css" />
```

`archive-outcomes.css` goes FIRST so a later archive stylesheet can
override outcome presentation, and so the cascade order matches the
dependency order.

**(2) The screen element, after line 550.** Line 549-550 currently hold
the launchpad screen:

```html
    <!-- Launchpad Screen -->
    <div id="launchpad-screen" class="screen"></div>
```

Add immediately after:

```html
    <!-- Archive Screen - the message browser. Empty; ArchiveScreen.show()
         builds it, the same way LaunchpadScreen builds #launchpad-screen. -->
    <div id="archive-screen" class="screen"></div>
```

It must carry `class="screen"` for `hideAllScreens()` and for the docked
sidebar layout offset that `session-sidebar.css` keys on `.screen`.

**(3) Sixteen script tags, before line 955.** Line 955 is
`<script src="/static/js/app.js"></script>`.

**NORMATIVE: `app.js` must stay last.** It is the controller and it
reaches for globals at parse time.

Insert in dependency order, so each file's dependencies are already
defined:

```html
<!-- Archive (message browser). Order is dependency order: pure
     helpers, then state, then views, then the composition root. -->
<script src="/static/js/archive-outcome.js"></script>
<script src="/static/js/archive-mask.js"></script>
<script src="/static/js/archive-format.js"></script>
<script src="/static/js/archive-deeplink.js"></script>
<script src="/static/js/archive-virtual-list.js"></script>
<script src="/static/js/archive-state.js"></script>
<script src="/static/js/archive-outcome-view.js"></script>
<script src="/static/js/archive-body-cache.js"></script>
<script src="/static/js/archive-line-render.js"></script>
<script src="/static/js/archive-nav.js"></script>
<script src="/static/js/archive-transcript-list.js"></script>
<script src="/static/js/archive-reader.js"></script>
<script src="/static/js/archive-search.js"></script>
<script src="/static/js/archive-export.js"></script>
<script src="/static/js/archive-keys.js"></script>
<script src="/static/js/archive-screen.js"></script>
```

### H.3 `client/js/screen-chrome.js`

Line 44, measured 2026-08-31:

```javascript
const AUTHENTICATED_SCREENS = ['launchpad', 'terminal'];
```

becomes

```javascript
const AUTHENTICATED_SCREENS = ['launchpad', 'terminal', 'archive'];
```

**This module FAILS CLOSED, and that is correct.** Its own comment at
lines 38-41 says an unknown screen name "is treated as unauthenticated,
which is the fail-closed direction: an unknown screen name hides the
chrome rather than showing it."

**NORMATIVE: add `'archive'` to the allowlist. Do NOT bypass the
allowlist, do not special-case archive, and do not change the default.**
The symptom of forgetting is missing chrome on the archive screen, which
is visible and harmless. The symptom of "fixing" it by defaulting to
authenticated is chrome on the login screen, which is not.

### H.4 `client/js/app.js`

Measured 2026-08-31: `showLaunchpad()` is at line **727**;
`_placeStatusLight(screen)` is at line **347**.

Add `showArchive(params)` modelled directly on `showLaunchpad()`:

```javascript
/**
 * Description: show the archive (message browser) screen.
 * Inputs: params (object) - {view, projectId, transcriptId, lineNo, query}
 *   from router.js. May be {} for the bare /archive route.
 * Output: void
 */
showArchive(params) {
    console.log('App: Showing archive screen', params);
    this.hideAllScreens();
    document.getElementById('archive-screen').classList.add('active');

    // Same one-way opt-in as showLaunchpad(): these ship class="hidden"
    // in index.html so they are absent on first paint. Stripping it is
    // not the screen gate; ScreenChrome.apply() below is.
    this.logoutBtn.classList.remove('hidden');
    if (this.settingsBtn) this.settingsBtn.classList.remove('hidden');
    if (this.configEditorBtn) this.configEditorBtn.classList.remove('hidden');

    this.currentScreen = 'archive';
    window.ScreenChrome.apply('archive');
    this._placeStatusLight('archive');
    if (window.GlobalAudioToggle) window.GlobalAudioToggle.place('archive');

    // The archive is not a session, so leave session theme scope exactly
    // the way showLaunchpad() does.
    if (window.Themes && typeof window.Themes.clearSession === 'function') {
        window.Themes.clearSession();
    }

    window.ArchiveScreen.show(params);
}
```

**Two things this deliberately does NOT do.**

It does not call `SessionSidebar.show()`. The sidebar is the working set
of live sessions; the archive is a different corpus with its own
navigation, and showing both puts two unrelated trees on one screen.
`SessionSidebar.hide()` is not called either, because `hideAllScreens()`
plus the `.screen` layout rules already handle placement, and calling
`hide()` persists a closed state that then affects the launchpad (the
comment at `showLaunchpad()` lines 741-745 documents exactly that
regression).

It does not touch `Themes.setActiveSession`. The archive runs under the
global theme.

**`_placeStatusLight` needs an archive branch.** Its current shape,
measured at line 347, is a binary: `screen === 'launchpad' ?
'home-bar-status' : 'terminal-bar-status'`. Left alone, the archive
screen would put its status light in the terminal bar, which is not on
screen. Add `archive-bar-status` as a third target and widen the JSDoc
`@param` from `'auth'|'launchpad'|'terminal'` to include `'archive'`.

### H.5 `client/js/router.js`

Measured 2026-08-31: `DEEPLINK_RX` is at line **47**, immediately after
`SLUG_RX` at line 42 and immediately before `DEEPLINK_PREFIX`.

Add four regexes in that block:

```javascript
// Archive routes. NUMERIC IDS ONLY - session_ref is not unique
// (measured 2026-08-31: "journal" is the session_ref of 14 different
// transcripts, "audit" of 5), so it can never appear in a path.
var ARCHIVE_ROOT_RX = /^\/archive\/?$/;
var ARCHIVE_PROJECT_RX = /^\/archive\/p\/([0-9]+)\/?$/;
// The LINE pattern MUST be tested before the TRANSCRIPT pattern.
var ARCHIVE_LINE_RX = /^\/archive\/t\/([0-9]+)\/l\/([0-9]+)\/?$/;
var ARCHIVE_TRANSCRIPT_RX = /^\/archive\/t\/([0-9]+)\/?$/;
```

**NORMATIVE: test `ARCHIVE_LINE_RX` BEFORE `ARCHIVE_TRANSCRIPT_RX`.**
Both are anchored, so as written they are actually mutually exclusive.
The ordering rule is here anyway because the first person to relax
`ARCHIVE_TRANSCRIPT_RX` (dropping the `$`, adding an optional suffix)
will make it swallow `/archive/t/5767/l/7111` and drop the line number
silently, landing the reader at line 0 of the right transcript with no
error. Section I asserts the order with a test that fails on the
swallowing form.

Matching order in the dispatcher: line, transcript, project, root, then
the existing `/session/` deep link, then the fallback.

### H.6 `src/main.py`

**Measured 2026-08-31: there is NO FastAPI catch-all.** The declared
`@app.get` routes are `/` (852), `/session/{project}` (880),
`/manifest.webmanifest` (918), `/apple-touch-icon.png` (928) and
`/health` (934). `_render_index_html()` is at line 796 and is shared by
`/` and `/session/{project}`.

So `/archive` returns a 404 today, and a person who reloads the page
while reading transcript 5767 loses the app. Two explicit routes are
required, beside the existing one:

```python
# Archive (message browser) SPA routes.
#
# Same rationale as session_deep_link() above: FastAPI matches
# more-specific routes first, so /static/*, /ws/*, /api/*, /health and
# /manifest.webmanifest all still resolve to their real handlers. The
# {rest:path} form is scoped UNDER /archive/, so it cannot shadow
# anything outside that prefix.
#
# Path-level validation is deliberately permissive here and strict in
# the client router: a visitor pasting /archive/t/notanumber gets the
# app shell with a visible error, not a bare 404.
@app.get("/archive")
async def archive_root():
    """Serve the SPA shell for the archive screen root."""
    return HTMLResponse(
        content=_render_index_html(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/archive/{rest:path}")
async def archive_deep_link(rest: str):
    """Serve the SPA shell for any archive deep link.

    The ``rest`` path parameter is consumed by the client-side router
    after the SPA boots; this handler does not inspect or validate it.
    """
    return HTMLResponse(
        content=_render_index_html(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
```

**Two routes, not one, and the scoping matters.** `{rest:path}` matches
across slashes, which is what `/archive/t/5767/l/7111` needs and what
`/session/{project}` deliberately refuses. Because the prefix is
`/archive/`, it cannot reach `/api/v1` or `/static`. A bare
`@app.get("/{rest:path}")` catch-all would, and would be a regression
for every 404 in the app; do not write one.

`/archive` and `/archive/` both need to work, which is why the root gets
its own route rather than relying on `{rest:path}` matching the empty
string.

### H.7 `tests/test_client_called_routes_exist.py`

Measured 2026-08-31, line 119:

```python
for m in re.finditer(r"""\bthis\.call\(\s*['"`](/[^'"`]*)['"`]""", src):
```

This is the extractor that proves every path the client calls is
routable. It matches `this.call(` only. Every archive call site goes
through `this.callEnvelope(`, so **all eleven new endpoints would be
invisible to the contract test** and the suite would stay green while
proving nothing about them.

Widen it:

```python
for m in re.finditer(
        r"""\bthis\.(?:call|callEnvelope)\(\s*['"`](/[^'"`]*)['"`]""", src):
```

The file already carries a `test_extractor_actually_extracts` guard (its
header at lines 11-24 explains why: a refactor to a different call helper
is exactly the false green this suite has shipped before). **Extend that
guard with a `callEnvelope` case**, so the widening is itself asserted
rather than assumed. A pattern change with no matching change to its own
positive control is the shape this file exists to prevent.

---

## I. Test plan

Two harnesses, both already established in this repo.

**JavaScript:** standalone `tests/*.node.mjs` files run with plain
`node`, using `tests/mini-dom.mjs` for anything that needs an element
tree. There is no `package.json` and no test runner; each file counts its
own passes and failures and exits non-zero. Follow
`tests/test_agent_family_pill.node.mjs` as the model.

**Python:** `tests/test_*.py` under the existing pytest setup, for route
existence and the contract extractor.

### I.1 THE MOST IMPORTANT TEST

`tests/test_archive_three_outcomes_render_differently.node.mjs`

**What it asserts:** four different-outcome payloads render DIFFERENTLY
on four independent channels, AND two same-outcome payloads render the
SAME.

The positive control is not optional. A test that only asserts
"everything is different" passes trivially if the renderer stamps a
random id into each block, or a timestamp, or a request sequence number.
It would then be green while proving nothing, which is the exact class of
defect the whole screen is built to avoid, sitting inside the test that
exists to prevent it.

```javascript
// tests/test_archive_three_outcomes_render_differently.node.mjs
//
// The single most important test in the archive UI.
//
// The archive server pays real cost to distinguish "no matches" from
// "could not evaluate" (docs/message-browser-api.md section 3). That
// property is only worth anything if it survives to the pixel. This
// file asserts it does, on FOUR INDEPENDENT CHANNELS, and it asserts
// the converse so it cannot pass by making every render unique.
//
// Run with: node tests/test_archive_three_outcomes_render_differently.node.mjs

const PAYLOADS = {
    empty: {
        result: [], result_status: 'ok', scope_status: 'resolved',
        unevaluated: [],
        meta: { scope: { kind: 'project', project_id: 12,
                         transcripts_in_scope: 3416 },
                scan: { status: 'complete', transcripts_scanned: 3416,
                        transcripts_not_scanned: 0 } }
    },
    partial: {   // verbatim from a live response, 2026-08-31
        result: [], result_status: 'partial', scope_status: 'resolved',
        unevaluated: [{ subject: 'project:12',
            reason: '2615 of 3416 transcripts were not scanned: byte ' +
                    'budget 536870912 was spent after 801 transcripts' }],
        meta: { scan: { status: 'budget_exhausted',
                        transcripts_scanned: 801,
                        transcripts_not_scanned: 2615,
                        resume_cursor: 'eyJieXRlcyI6NTUxNjQ4NTY2...' },
                paging: { has_more: null } }
    },
    cannotDetermine: {
        result: null, result_status: 'cannot_determine',
        scope_status: 'resolved',
        unevaluated: [{ subject: 'cursor',
            reason: 'cursor did not decode as a v1 transcripts cursor: ' +
                    'invalid base64url padding' }],
        meta: { paging: { limit: 50, returned: 0, has_more: null,
                          next_cursor: null } }
    },
    notFound: {  // verbatim from a live 404, 2026-08-31
        result: null, result_status: 'not_found', scope_status: 'not_found',
        unevaluated: [{ subject: 'transcript:99999',
            reason: 'no row in message_transcripts with id 99999' }],
        meta: {}
    }
};

// ---- CHANNEL EXTRACTORS -------------------------------------------------
// Four channels that are independent BY CONSTRUCTION: one reads visible
// words, one reads a class list, one reads an attribute, one reads which
// buttons exist. A renderer cannot satisfy all four by accident.

const channels = {
    // 1. TEXT. What a person actually reads. Normalised to collapse
    //    whitespace so formatting churn is not a false difference.
    text: (el) => el.textContent.replace(/\s+/g, ' ').trim(),

    // 2. CLASS. The styling hook.
    classes: (el) => Array.from(el.classList).sort().join(' '),

    // 3. DATA ATTRIBUTE. The machine-readable outcome token.
    dataOutcome: (el) => el.getAttribute('data-outcome'),

    // 4. ACTION PRESENCE. Which affordances exist. This is the channel
    //    that is hardest to fake, because it is a structural fact about
    //    the subtree rather than a string the renderer chose.
    actions: (el) => Array.from(el.querySelectorAll('[data-action]'))
                          .map(b => b.getAttribute('data-action'))
                          .sort().join(',')
};

// ---- ASSERTION 1: FOUR OUTCOMES DIFFER ON EVERY CHANNEL -----------------
// For each channel, all four rendered outcomes must be pairwise distinct.
// Not "at least one channel differs" - EVERY channel. A single shared
// channel is a path by which two outcomes look alike to someone.

test('four outcomes are pairwise distinct on all four channels', () => {
    const rendered = {};
    for (const [name, payload] of Object.entries(PAYLOADS)) {
        rendered[name] = renderOutcomeBlock(payload);   // archive-outcome-view.js
    }
    for (const [chName, extract] of Object.entries(channels)) {
        const seen = new Map();
        for (const [name, el] of Object.entries(rendered)) {
            const v = extract(el);
            assert.ok(v !== null && v !== undefined && String(v).length > 0,
                `channel ${chName} produced nothing for outcome ${name}`);
            if (seen.has(v)) {
                assert.fail(
                    `outcome "${name}" and outcome "${seen.get(v)}" render ` +
                    `IDENTICALLY on channel "${chName}" (value: ${v}). ` +
                    `A person cannot tell them apart.`);
            }
            seen.set(v, name);
        }
    }
});

// ---- ASSERTION 2: THE POSITIVE CONTROL ----------------------------------
// Two DIFFERENT payloads with the SAME outcome must render the SAME on
// the class, attribute and action channels. Without this, assertion 1
// passes for a renderer that stamps a nonce into every block.
//
// Text is deliberately EXCLUDED from this assertion: two
// cannot_determine payloads legitimately carry different reason strings,
// and rendering the reason verbatim is a requirement (section D.1).

test('POSITIVE CONTROL: two cannot_determine payloads render the same', () => {
    const a = renderOutcomeBlock(PAYLOADS.cannotDetermine);
    const b = renderOutcomeBlock({
        result: null, result_status: 'cannot_determine',
        scope_status: 'resolved',
        unevaluated: [{ subject: 'datastore',
                        reason: 'the archive database would not open' }],
        meta: {}
    });
    for (const ch of ['classes', 'dataOutcome', 'actions']) {
        assert.equal(channels[ch](a), channels[ch](b),
            `two cannot_determine payloads render DIFFERENTLY on channel ` +
            `"${ch}". Assertion 1 is therefore passing because every ` +
            `render is unique, not because the outcomes are distinguished.`);
    }
    // And they DO differ on text, because the reason is rendered verbatim.
    assert.notEqual(channels.text(a), channels.text(b),
        'the unevaluated reason is not reaching the rendered output');
});

// ---- ASSERTION 3: EMPTY IS NEVER THE FALLBACK ---------------------------
// Belt and braces on the one collapse that matters most.

test('cannot_determine never renders the empty-state text', () => {
    const emptyText = channels.text(renderOutcomeBlock(PAYLOADS.empty));
    for (const name of ['cannotDetermine', 'partial', 'notFound']) {
        const t = channels.text(renderOutcomeBlock(PAYLOADS[name]));
        assert.notEqual(t, emptyText,
            `${name} renders the same words as an empty result`);
        assert.ok(!/^No matches\.?$/i.test(t),
            `${name} renders as a bare "No matches."`);
    }
});
```

### I.2 Masking tests

`tests/test_archive_mask.node.mjs` - no DOM needed at all.

| # | Assertion | Why |
|---|---|---|
| 1 | **The real body-379 geometry.** A string containing 12 astral characters positioned so a finding at code-point 5197 sits at UTF-16 5201. Masking with the UTF-16 pair covers exactly the 40-character window. | The measured case. |
| 2 | **NEGATIVE CONTROL: masking the same string with `match_offset` leaves the last 4 characters of the secret visible.** The test asserts that the WRONG method leaks, so the correct method's success is not vacuous. | Without this, test 1 passes for a function that masks the whole string. |
| 3 | `utf16_state: 'cannot_determine'` on ANY finding returns `MASK_REFUSED` and the returned object carries no body text. | Section F.2. |
| 4 | `secret_finding_count > 0` with `secrets` absent returns `MASK_REFUSED`, reason names the count. | The live `/lines` shape, section K.1. |
| 5 | `secret_finding_count = 3` with 2 findings returns `MASK_REFUSED`. | Case 3 in F.3. |
| 6 | An offset extending past `body.length` returns `MASK_REFUSED`, not a truncated mask. | |
| 7 | Overlapping windows merge; the output contains exactly one marker and the surrounding text is intact on both sides. | |
| 8 | Three disjoint findings, applied highest-offset-first, all land correctly. Assert the character immediately before and after each marker. | Proves the splice order. |
| 9 | `secret_finding_count === 0` and `secrets: []` returns the body byte-identical to the input. | The archive is byte-exact; masking must be a no-op when there is nothing to mask. |
| 10 | **The bound check uses UTF-16 length.** A body whose code-point length is less than a valid UTF-16 offset must still mask successfully. | Guards against reintroducing the bug inside the validator. |

**Test 2 is the one that must never be deleted.** It is the positive
control for the whole file: it demonstrates the leak the module exists to
prevent, so a future refactor that stops masking cannot pass by making
the assertions trivially true.

### I.3 Virtualization tests

`tests/test_archive_virtual_list.node.mjs`

| # | Assertion |
|---|---|
| 1 | `rowAt()` binary search is correct at every boundary for N = 30,805 with non-uniform heights, checked against a linear scan. |
| 2 | A height correction on a row ABOVE the viewport adjusts `scrollTop` by exactly the delta, in the same reconciliation pass. |
| 3 | A height correction BELOW the viewport does NOT adjust `scrollTop`. |
| 4 | Corrections smaller than `HEIGHT_EPSILON_PX` are ignored, and `offsets` is not rebuilt. |
| 5 | **No `IntersectionObserver` is constructed.** Stub the global with a counter and assert it stays 0 after mounting a 30,805-row list. A structural assertion, not a performance one. |
| 6 | With N = 30,805 the rendered row count stays under `viewport / minRowHeight + 2 * OVERSCAN_ROWS` at every scroll position sampled. |
| 7 | `offsets` is monotonically non-decreasing after an arbitrary sequence of corrections. |

`tests/test_archive_body_cache.node.mjs`

| # | Assertion |
|---|---|
| 1 | Inserting 301 small bodies evicts exactly one, the least recently used. |
| 2 | Inserting bodies totalling 33 MiB evicts down to under 32 MiB even at fewer than 300 entries. |
| 3 | Both caps are enforced simultaneously; eviction runs until both predicates hold. |
| 4 | Two concurrent requests for the same `body_id` produce ONE fetch. |
| 5 | A body of 54,376,859 chars is never inserted, because the hard gate refuses it before the fetch. |

`tests/test_archive_size_gates.node.mjs`

| # | Assertion |
|---|---|
| 1 | `body_chars = 262,145` yields `gated-soft` with a `render anyway` action present. |
| 2 | `body_chars = 2,097,153` yields `gated-hard` with **no** render action. Assert on absence: `querySelectorAll('[data-action="render-anyway"]').length === 0`. |
| 3 | **`body_chars = 54,376,859` with `body_state: "included"` still yields `gated-hard`.** The measured real case: the server serves this inline because `MAX_BODY_BYTES` is 67,108,864. The client gate is the only protection and it must fire on the SPINE value, before any fetch. |
| 4 | The gate reads `body_chars` and not `body_bytes`, asserted by feeding a row where they differ. |

### I.4 Routing tests

`tests/test_archive_deeplink.node.mjs`

| # | Assertion |
|---|---|
| 1 | `/archive/t/5767/l/7111` parses to `{transcriptId: 5767, lineNo: 7111}`. |
| 2 | **`ARCHIVE_LINE_RX` is tested before `ARCHIVE_TRANSCRIPT_RX`.** Assert by feeding the line path through the dispatcher and checking `lineNo` survives, then AGAIN with a deliberately relaxed transcript pattern (`$` removed) to prove the ordering, not the anchoring, is what saves it. |
| 3 | `/archive/t/journal` does NOT parse and yields `cannot-determine` with a reason naming the segment. It does NOT redirect to `/archive`. |
| 4 | `/archive/t/5767abc` does not parse. Anchoring. |
| 5 | Round trip: `build(parse(path)) === path` for all four routes. |
| 6 | Query state survives: `/archive/t/5767?q=hazard` parses the query and preserves it through a rebuild. |
| 7 | **No route builder accepts a `session_ref`.** Assert `buildTranscriptPath('journal')` throws or returns null. |
| 8 | **No route builder emits a resume cursor.** Feed a search state carrying a 147-character `resume_cursor` and assert it appears nowhere in the built URL. |

### I.5 Python tests

`tests/test_archive_spa_routes.py`

| # | Assertion |
|---|---|
| 1 | `GET /archive` returns 200 and the served body is byte-identical to `GET /` (both go through `_render_index_html()`). |
| 2 | `GET /archive/t/5767/l/7111` returns 200 and the same shell. |
| 3 | `GET /archive/t/notanumber` returns 200, not 404. The client renders the error. |
| 4 | **`GET /api/v1/archive/hosts` is NOT shadowed.** Assert it returns JSON, not HTML. The single most important assertion in this file: a badly scoped catch-all would swallow the whole API and every symptom would look like a client bug. |
| 5 | `GET /static/js/app.js` still returns JavaScript. |
| 6 | `GET /health` still returns its own payload. |
| 7 | `GET /nonexistent` still returns 404. The archive routes did not become a global catch-all. |

`tests/test_client_called_routes_exist.py` - extend the existing
`test_extractor_actually_extracts` with a `this.callEnvelope('/archive/hosts')`
fixture, asserting the widened pattern finds it and that the pre-widening
pattern would not have.

### I.6 What is deliberately NOT tested by a mock

Three things a Node test cannot honestly assert, listed so nobody
believes the suite covers them.

**Real astral-character rendering.** The mask tests operate on strings.
That a browser's text layout does not do something surprising with a
mixed astral/marker run is not proven here.

**That a 54 MB `<pre>` kills a tab.** Asserted as a design constraint,
not measured. The hard gate is tested; the consequence of removing it is
not.

**Actual scroll smoothness.** `mini-dom.mjs` has no layout. Assertions 2
and 3 in I.3 check the arithmetic of the anti-jump correction. Whether
the result is visually free of jump is a browser-only judgement and
belongs in a manual pass at 30,805 lines on transcript 5767.

### I.7 Theme robustness (NORMATIVE)

**Measured 2026-08-31: 23 themes exist under `client/css/themes/`.**
Radius tokens live in each theme's `theme.json` under `cssVars`, not in
`theme.css`.

Three themes zero every radius token:

| Theme | `--radius-sm` | `--radius-md` | `--radius-lg` | `--radius-pill` | `--radius-full` |
|---|---|---|---|---|---|
| `terminal` | 0 | 0 | 0 | 0 | 0 |
| `gameboy` | 0 | 0 | 0 | 0 | 0 |
| `legacy_apple` | 0px | 0px | 0px | 0px | 0px |
| (`claude`, for contrast) | 3px | 4px | 8px | 50px | 50% |

That flattens status dots and icon rings into squares, because
`--radius-full` and `--radius-pill` mean "render as a circle" rather than
"round the corners a bit" and both jobs share one token.

**This is a DELIBERATE aesthetic choice, recorded and left alone. Do not
design a fix for it and do not file it as a bug.**

What it means for this design: **no outcome's meaning may depend on a
class whose only distinguishing declaration is `border-radius`.** A pill
that reads as `partial` because it is rounded and as `ok` because it is
not is invisible on three of twenty-three themes.

`tests/test_archive_outcome_theme_robustness.node.mjs`:

| # | Assertion |
|---|---|
| 1 | Parse `client/css/archive-outcomes.css`. For every `[data-outcome="..."]` selector, the declaration block contains at least one property that is not `border-radius`. |
| 2 | No two outcome selectors have declaration blocks that differ ONLY in `border-radius`. |
| 3 | Every outcome selector sets at least one of `border-left-width`, `border-style`, or a `::before` `content`, so a shape or a glyph carries meaning independently of colour and radius. |
| 4 | Re-run I.1's four-channel assertion with all `--radius-*` forced to 0. The text, class, attribute and action channels are radius-independent by construction, so this must pass unchanged. It is cheap and it locks the property in. |

---

## J. Excluded from v1, with reasons

Each of these is a real feature that a reasonable person would ask for.
Each is excluded for a measured reason, not because it is hard.

### J.1 Any live-session correlation UI

**The reader prints `Live session: NOT CHECKED` verbatim.** It does not
say "no live session", does not hide the field, and does not show a
dash.

Three measurements, all 2026-08-31:

| Fact | Value |
|---|---:|
| Transcripts with `session_ref_scheme = 'uuid'` | 1,451 of 21,039 (**6.9%**) |
| Of those, `session_ref` values that are not actually UUIDs | **19** |
| Rows in the live `sessions` table | **0** |

So the join key exists for under 7% of the corpus, is wrong for 19 of
those, and the table on the other side of the join is empty. Any UI built
on it would render "no live session" for every transcript in the archive,
100% of the time, and would be RIGHT by accident. That is a check that
cannot fail - the exact defect class the Infrastructure hazard list calls
the most dangerous there is.

`NOT CHECKED` is an honest could-not-evaluate. It asserts nothing about
an absence it never measured.

### J.2 Global search

Search is scoped: transcript, project, corpus or host. There is no
"search everything".

The server excludes it too (`docs/message-browser-api.md` section 7.1)
and the measurement backs it up: a **project-scoped** scan over 3,416
transcripts spent the full 512 MiB byte budget after 801 transcripts in
1.70 seconds and returned `partial` (measured 2026-08-31). Project 12
alone exhausts the budget. A corpus-wide scan over 19,548 transcripts
would return `partial` every single time.

A search that ALWAYS returns partial is not a search, it is a
random sample with a progress bar. Better to have four honest scopes than
one dishonest one.

### J.3 Cross-transcript "where else does this body appear"

The schema supports it: `message_bodies.identity_key` is exactly the
deduplication key, and `message_appearances` has 3,125,122 rows against
2,447,028 bodies (measured 2026-08-31), so roughly 678,000 appearances
share a body with another appearance.

Excluded because the reverse index is not there. Finding every appearance
of a body means scanning `message_appearances` on an unindexed
`body_id`, at 3.1 million rows, per body, on hover. It is a good feature
and it needs a server-side index first. Adding it as a client-side scan
would be a UI that gets slower as the archive grows, which is the wrong
direction for an archive.

### J.4 Rich rendering of `progress` records

917,436 rows, 37.49% of all bodies (measured 2026-08-31). Collapsed
behind a count chip, expandable, **never hidden**. See G.7.

Excluded specifically: parsing the progress payload into a structured
timeline view. That is a feature about tool-call shape, not about reading
a conversation, and it would need its own schema knowledge that would rot
the first time the upstream format changes. The collapsed chip plus raw
expansion is format-agnostic.

### J.5 A secret browser

No "show me every credential in the archive" view. 6,240 bodies carry a
finding (measured 2026-08-31), and an index of them is a
attacker-convenience feature that this UI has no business building. The
server excludes the endpoint too
(`docs/message-browser-api.md` section 7.8).

### J.6 Any write, rename, tag, annotate or delete

The archive is byte-exact evidence. A UI that can write to it is a UI
that can destroy the property that makes it worth having. There is no
write path in the API and there is no write affordance in this design.

Note this also excludes benign-sounding things: no "mark as read", no
starring, no notes. Every one of them creates a second store whose
relationship to the byte-exact archive is undefined.

### J.7 Client-side hash verification of a STREAMED export

Cannot be done, not merely declined. See K.4 and D.3: a browser tab
cannot both stream a response to disk and hash the bytes. The two paths
are exclusive. Buffering 244,117,661 bytes in the tab to hash them
defeats the entire reason for streaming, and the API's own 413 message
says buffering transcript 5767 "would peak near 1052 MB".

The honest alternative is implemented instead: the expected hash and the
`shasum -a 256` command, presented as a COULD-NOT-EVALUATE.

### J.8 A view/panel registry refactor of `app.js`

`app.js` currently dispatches screens with explicit `showX()` methods.
A registry would be cleaner and this design adds a fourth screen to a
pattern that is already at its comfortable limit.

It is still excluded. The refactor touches `showAuth`, `showLaunchpad`
and `showTerminal` - three working screens, all with subtle behaviour
that is documented only in comments (the sidebar `hide()`-persists-closed
regression at `showLaunchpad()` lines 741-745 is one). Rewriting three
working things to add a fourth is how a feature branch becomes a
regression branch. `showArchive()` follows the existing pattern exactly.

The refactor is the right eventual cleanup. It is not this change.

---

## K. Known API friction

Everything in this section was verified against the live server on
2026-08-31. Two other agents were editing `docs/message-browser-api.md`
and the API concurrently, so **re-verify each item before implementing
against it.** Where the spec document and the live response disagree, the
live response is authoritative.

### K.1 `/lines?include_bodies=true` returns secret-bearing bodies with no `secrets` array

**STILL LIVE as of 2026-08-31 11:55.** Measured directly:

```
GET /api/v1/archive/transcripts/4/lines?include_bodies=true&limit=2&cursor=eyJsaW5lX25vIjoyOTEsInYiOjF9

line_no 292, body_id 379
  secret_finding_count: 3
  body_json:            present, 19,831 characters, the real credentials
  'secrets' key:        ABSENT
```

The full key list on that row is:

```
agent_id, appearance_id, body_bytes, body_chars, body_href, body_id,
body_state, compact_subtype, fidelity_outcome, is_compact_boundary,
is_sidechain, line_byte_length, line_no, line_status, message_uuid,
model, origin_session_ref, parent_uuid, record_type, role,
secret_finding_count, seq_in_file, serializer_style, ts
```

No `secrets`. By contrast `GET /api/v1/archive/bodies/379` DOES return
the array, with all three findings and `utf16_state: "computed"`.

**Consequence for the client:** the reader knows a credential is in that
string and does not know where. `archive-mask.js` case 2 handles it by
refusing (section F.3), so the UI is safe today, but the cost is that a
line the person wants to read renders as `mask-refused` instead of as a
masked body.

**The client-side workaround, and why it is a workaround.** When a row
has `secret_finding_count > 0`, fetch `body_href` individually to get the
findings, and mask from that response. Correct, and it turns one page
request into one plus N. Fine at 6,240 secret-bearing bodies out of
2,447,028; not fine if that ratio ever changes.

**Requested fix:** `/lines?include_bodies=true` should carry the same
`secrets` array `/bodies/{id}` does, for any row where
`secret_finding_count > 0`. This is reportedly being fixed concurrently.
**Verify before implementing the workaround; if the array is present, use
it and delete the extra fetch.**

### K.2 `scan.bytes_scanned` is a CHARGE, not work done

**NORMATIVE: `bytes_scanned` must never drive a progress bar.**

Two measurements, 2026-08-31:

| Query | `bytes_scanned` | `elapsed_seconds` | Implied rate |
|---|---:|---:|---:|
| `q=hazard&transcript_id=5767` | 91,950,363 | 0.0756 | 1.22 GB/s |
| `q=restic&project_id=12&limit=2` | 609,842 | 0.0039 | 156 MB/s |
| `q=zzzqqqxyznotfound&project_id=12` | 551,648,566 | 1.7017 | 324 MB/s |

The spec's own measured scan rate is 0.44 GB/s. The first row claims
1.22 GB/s, which is not a rate anyone achieved; it is the whole
transcript being CHARGED because the page limit was hit after reading a
fraction of it. `scan.status` on that response was `limit_reached` with
`transcripts_scanned: 1`.

And the third row shows the budget being OVERSHOT: 551,648,566 bytes
charged against a `budget_bytes` of 536,870,912, which is **14,777,654
bytes (2.75%) over**. A quantity that exceeds its own budget is not a
metered consumption.

**NORMATIVE: drive scan progress from `transcripts_scanned` /
`transcripts_in_scope`.** Those are integers, they are monotone, they
never exceed each other, and 801 of 3,416 is a statement a person can
act on. `bytes_scanned` may be rendered as a raw number in a details
view, labelled as a charge; it may not be a fraction of anything.

Note the brief this design was reconstructed from paired 91,950,363 bytes
with 0.0039 s. Those are two different responses; the 0.0039 s run
scanned 609,842 bytes. The conclusion is unchanged and is if anything
stronger, since the 0.0756 s / 91.9 MB pairing is a plausible-looking
rate that is still 2.8x the real one.

### K.3 The server's `MAX_BODY_BYTES` never fires, so the client gate is the only gate

`MAX_BODY_BYTES` is 67,108,864 (64 MiB). The largest body in the corpus
is **54,376,859 characters** (transcript 19243, line 62; measured
2026-08-31).

Measured, not inferred:

```
GET /api/v1/archive/transcripts/19243/lines?include_bodies=true&limit=1&cursor=<line 61>

line_no 62
  line_byte_length: 54376879
  body_chars:       54376859
  body_state:       "included"        <-- NOT withheld_too_large
  body_json:        present, 54 MB of base64 image data
```

The spec's section 10.7 already says `MAX_BODY_BYTES` is currently
unreachable, and this confirms it end to end: **the server will happily
send 54 MB inline.**

**Consequence:** decision 5's `withheld_too_large` handling is real and
must be implemented, but it protects nothing today. `BODY_RENDER_HARD_MAX`
(2,097,152 chars) is the only thing standing between the reader and a
54 MB response, and it must be evaluated from `body_chars` on the SPINE,
before the fetch is issued. A client that waited for the server to
withhold would have already downloaded it. See G.6 and test I.3/4.3.

### K.4 The export cannot be a plain navigation, because the API is Bearer-only

**This CONTRADICTS the design decision it was carried forward from, and
it blocks the export feature until either the API or the design changes.**

Decision 8 said the download is a plain navigation rather than a fetch,
which is correct reasoning: a tab cannot both stream to disk and hash the
bytes, and buffering 244 MB defeats the point.

Measured 2026-08-31, `GET /api/v1/archive/transcripts/4/export/verified`:

| Attempt | Result |
|---|---|
| `Authorization: Bearer <jwt>` | **200**, full headers, 3,181,330 bytes |
| No auth header at all | **401**, `www-authenticate: Bearer` |
| `?token=<jwt>` query param | **401** |
| `?access_token=<jwt>` query param | **401** |
| `Cookie: access_token=<jwt>` | **401** |

`src/api/auth.py:65` is `security = HTTPBearer(auto_error=False)`. There
is no query-parameter fallback and no cookie fallback anywhere.

**A `window.location = href` navigation or an `<a download href>` click
sends NO `Authorization` header. It gets a 401 and the browser saves or
displays an error page.** There is no way for a plain navigation to
authenticate against this API today. Confirmed separately: the client has
no service worker (`grep -rn serviceWorker client/` returns nothing) and
no existing download flow anywhere in `client/js/`.

**Four options, with the recommendation.**

| Option | Verdict |
|---|---|
| **A. A short-lived download ticket.** Authenticated `POST /api/v1/archive/transcripts/{id}/export/ticket` mints a single-use, ~60-second, scope-limited opaque token. The download is `GET .../export?ticket=<t>`, a plain navigation. | **RECOMMENDED.** Keeps the streaming property, keeps the JWT out of the URL, out of browser history and out of any Referer. The ticket is single-use and scoped to one transcript, so a leaked one is worth almost nothing. |
| **B. Cookie fallback on the two export routes only.** | Workable but it introduces a cookie auth path into a Bearer-only app, and with it CSRF surface on a GET. Rejected. |
| **C. `fetch` plus `Blob` plus an object URL.** | Buffers the whole file in the tab. 244,117,661 bytes for transcript 17266; the API's own 413 text says buffering the 91 MB transcript "would peak near 1052 MB". Defeats the entire reason streaming exists. Acceptable ONLY for the verified path (<= 8 MiB, 98.94% of transcripts) as a stopgap. |
| **D. A service worker that injects the header.** | Would work, and adds a service worker to an app that deliberately has none, for one feature. Rejected. |

**Until option A exists, the export UI implements `blocked-no-credential`
(section D.3): the modal opens, states plainly that the download cannot
be started because the export endpoint requires a header a browser
navigation cannot send, and offers a copyable `curl` command carrying the
expected sha256.** It does NOT render a Download button that produces a
401 page. A button that cannot work is worse than a stated blocker.

### K.5 `session_ref_scheme` filter on the transcript list - RESOLVED 2026-08-31

**The single highest-value API addition for this UI.**

Measured 2026-08-31: **19,588 of 21,039 transcripts (93.1%) are
`session_ref_scheme = 'agent'`** - sidechain files produced by subagents,
not conversations a person had. Only 1,451 (6.9%) are `uuid`-scheme, and
19 of those are mislabelled (section J.1).

Project 12 has 3,416 transcripts. Finding the handful that are actual
conversations meant paging through pages of `agent-add459f16848bb276`
rows: the client-side toggle could only filter pages already fetched, so
"show me the real conversations in this project" cost 69 requests at
`limit=50` before it could answer.

**SHIPPED.** `GET /archive/projects/{id}/transcripts?session_ref_scheme=uuid`
exists (API section 6.4). The list now RE-QUERIES the scope on a filter
change instead of splitting its fetched rows, and the discarded cursor is
deliberate - a cursor minted under one filter positions inside that
filter's result set.

Measured live on project 12: `uuid` 77, `agent` 3,339, sum 3,416, which
is the unfiltered scope total. Those counts arrive in `meta.filters` as
`matched_in_scope` and `scope_total_before_filter`, labelled
`counts_are: "scanned_within_this_scope_only"`.

**THE HONESTY TEXT CHANGED, AND HAD TO.** The old sentence
(`this filters what has been FETCHED, not the scope. There is no
server-side scheme filter`) was true when written and became a lie in the
OTHER direction the moment the filter shipped - a UI understating an
answer that is now complete over the scope. It now reads
`Showing 50 conversations (uuid scheme). The server filtered the WHOLE
scope, which holds 77 rows with this scheme out of 3,416.` A missing
`matched_in_scope` renders `NOT KNOWN`, never 0.

**What it still must not claim.** The filter matches the
`session_ref_scheme` COLUMN and nothing else, which does not prove
conversation-ness - see the 19 mislabelled rows in section J.1. The
server ships that caveat in `meta.filters.session_ref_scheme_means` and
the list RENDERS the server's sentence rather than keeping a second copy
that can drift.

**Three outcomes, not two.** An unknown scheme is HTTP 400
`cannot_determine` naming the schemes that exist, handled by the existing
`paintOutcome` path. A known scheme matching nothing in this project is
HTTP 200, zero rows, `matched_in_scope: 0`. Those are different
statements and they render differently.

**Still not built:** the same filter on the corpus-level unattributed
listing. The scheme control is HIDDEN for that scope rather than shown
and silently ignored - a control that does nothing is worse than no
control.

### K.6 Smaller frictions

**`start_line` on `/lines` - RESOLVED 2026-08-31, SHIPPED.** The route
`/archive/t/<id>/l/<n>` has to land on line `n` of a 30,805-line
transcript, and the only paging control used to be an opaque `cursor`.
Synthesizing `base64url({"line_no": <n-1>, "v": 1})` as the cursor DOES
work - it was measured - and a client must never do it, because the spec
calls the cursor opaque and a client built on that encoding breaks by
SKIPPING ROWS when the encoding changes. **Cursor synthesis was never
built into the client and must not be.**

`?start_line=<n>` now exists (API section 6.7). The deep link sends it,
so the SERVER positions the page. Verified live in a headed browser at
1440x900 with `document.hidden === false`: `/archive/t/5767/l/7111`
issued exactly one lines request carrying `start_line=7111`, the first
rendered row was `line_no` 7111, the pane carried
`data-highlight-line="7111"`, and the element measured 413 px tall with
its top flush against the scroller viewport - real pixels, not a DOM flag.

**Two things the reader had to change with it, and one bug it exposed.**

1. **A windowed spine is NOT a complete spine.** `has_more: false` on a
   `start_line` page only says nothing follows that page; lines 0..n-1
   are still missing. Marking it complete would render the
   end-of-transcript state and manufacture a false "you have seen it all"
   out of the deep link itself. `complete` is now
   `hasMore === false && no start_line was sent`.
2. **The out-of-range note is the SERVER'S now.** The client used to
   synthesize a `cannot_determine` saying it could not reach the line. A
   line past the end is a measured 404 from the server naming the real
   `MAX(line_no)`; `start_line` plus `cursor` is a 400. Both reach the
   reader as ordinary non-renderable envelopes. The client-side note
   survives only for the case where the server WAS asked for line n and
   the rows do not contain it, and it now names which of the two causes
   applied instead of claiming the endpoint takes no offset.
3. **`reader.items()` returns ROW OBJECTS, not `{row}` wrappers**, and
   consecutive `record_type === 'progress'` lines are collapsed into one
   `{kind:'progress-run', from, to, count, rows}`. `scrollToLine` was
   reading `items[i].row.line_no`, which is `undefined` for every item -
   a lookup that could never match. It went unnoticed because until
   `start_line` shipped the deep link never reached a page where the
   answer would have been yes. Fixed, and a line inside a collapsed run
   now EXPANDS that run: scrolling to a collapsed block and reporting
   success is a landing that measures as one and is not one. Line 7,111
   of transcript 5767 sits inside the run 7110..7123, so this is the
   normal case on real data, not an edge.

**`body_bytes` is a deprecated alias that counts CODE POINTS.** The
server says so in its own `meta.body_bytes_note`. Every size decision in
this design reads `body_chars`. Asserted by test I.3/4.4.

**`role` is NULL on 44.93% of bodies** (1,099,537 of 2,447,028, measured
2026-08-31). Any reader UI keyed on role renders a blank for nearly half
its rows. Section C.2 falls back to `record_type` and then to the literal
string `no role recorded`.

**`ts` is NULL on 33,480 bodies**, and `meta.lines_with_null_ts` on a
`/lines` response reports the count for that page (measured: 1 on a
3-row page of transcript 4). Render `no timestamp recorded`, never a
blank and never a fabricated one.

**`display_name` contains non-ASCII.** Host 2 is `Joseph’s Mac mini (2)`
with a U+2019. Escape before insertion.

---

## L. Open questions for the implementer

Not decisions. Things this design could not settle from outside.

1. **Does the export ticket (K.4 option A) get built, or does v1 ship
   with `blocked-no-credential` and a `curl` command?** This is a product
   call. The design supports both.
2. **Is the `secrets` array in `/lines` fixed by the time this is
   implemented?** K.1. Verify live; the workaround is a one-line branch
   either way.
3. **Does `?start_line=` get added?** K.6. If not, the spine-and-scroll
   path works, it just does more requests than it needs to.
4. **`CHARS_PER_LINE = 96` in G.4 is an estimate, not a measurement.**
   Measure it once against the real monospace stack at the reader's
   default width and correct the constant. Being wrong here costs scroll
   accuracy before reconciliation, not correctness.

---

*Written 2026-08-31. Every measurement in this document was taken against
the dev instance at 127.0.0.1:5055 and the read-only SQLite copy at
`/Users/jsugamele/Scratch/llmScratch/cc-dev-state/cloude.db` between
11:45 and 12:00 America/New_York. No database was written. The corpus is
being appended to by a background ingest every 900 seconds, so every
count here is a floor, not a fixed value.*
