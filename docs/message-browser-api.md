# Message browser API - implementable specification

Read-only HTTP API over the ingested Claude Code transcript archive.
Schema v17. Written 2026-08-31 against the live dev corpus at
`/Users/jsugamele/Scratch/llmScratch/cc-dev-state/cloude.db`
(21,039 transcripts, 2,447,028 bodies, 3,125,122 appearances, 11 GB).

Every number in this document is either MEASURED on that database and
labelled with its timing, or explicitly labelled PREDICTION or
CANNOT DETERMINE. Nothing is asserted from the brief alone.

## 0. How to read this document

| Section | What it settles |
|---|---|
| 1 | Byte-exactness (normative) |
| 2 | Secrets and redaction (normative) |
| 3 | The `envelope()` contract every route uses |
| 4 | Constants |
| 5 | Cursors and pagination |
| 6 | The endpoints, one subsection each |
| 7 | What is excluded from v1 and why |
| 8 | File plan |
| 9 | Test plan |
| 10 | Where the schema fights the design |
| 11 | Corrections to the prior design pass |
| 12 | Measurement log |

An engineer should be able to implement section 6 without reading
anything else first, but sections 1 through 5 are load-bearing for every
route and a route written without them will be wrong.

**EVERY COUNT IN THIS DOCUMENT IS A DATED SAMPLE, NOT A CONSTANT, AND THE
DEV CORPUS IS BEING WRITTEN WHILE YOU READ IT.** The development database
at `~/Scratch/llmScratch/cc-dev-state/cloude.db` has a background
corpus-ingest scheduler appending transcripts every ~15 minutes, so
`transcript_count`, `archive_rows`, body counts and finding counts all
move between one request and the next. Treat a number here as evidence
that a measurement was taken on the date beside it, never as a value to
assert against.

That has a practical consequence for anyone correcting this document:
**prefer stating the SHAPE and the METHOD over pasting a count.** "The
first page of transcript 4 returns `line_no` 0, 1, 2, so numbering is
0-based" stays true; "the corpus holds 21,039 transcripts" was true at
16:00 on 2026-08-31 and is already wrong. Where a count is genuinely the
finding - 1,100 of 12,390 findings sit after an astral character, so the
UTF-16 conversion is not optional - keep it, date it, and say what it is
evidence FOR. A test must never assert one.

---

## 1. Byte-exactness (NORMATIVE)

The product guarantee is that a transcript exported through this API is
byte-identical to the file that was ingested. Everything below follows
from that and none of it is negotiable.

1. **Export is reassemble-only.** Export renders each line through
   `message_model_serialize.render_line()`, which is
   `render_with_style(reassemble(body, envelope, key_order), style)`.
   There is no second serialization path and there must never be one. A
   route that builds output from parsed values instead of from the
   stored `(body_json, envelope_json, key_order_json, serializer_style)`
   tuple has broken the guarantee even if its output happens to match.
2. **A raw line, where one is stored, wins.** `raw_line` is populated
   ONLY when re-rendering failed at ingest, so preferring it is not a
   shortcut. Measured: exactly 1 of 3,125,122 appearance rows has a
   non-null `raw_line`, and exactly 1 has a null `body_id`. Both paths
   are live and both must be implemented.
3. **No response anywhere returns a prefix of a body in `body_json`.**
   That field is the whole body or `null` beside an explicit
   `body_state` and a `body_href`. Never a "first N bytes", never an
   ellipsis. A truncated body that a client mistakes for the real one is
   exactly the failure this guarantee exists to prevent.

   **This rule is about `body_json`, and it used to be written as "no
   response anywhere returns a PREFIX of a body", which is FALSE.**
   Search snippets do carry body text, and a snippet whose window starts
   at `match_offset: 0` carries the body's leading characters. MEASURED
   2026-08-31 on body 1068 (138 characters): a hit at `match_offset: 0`
   returned a 70-character `snippet` whose first 67 characters are the
   body's first 67, followed by a literal `...` truncation marker - so a
   byte-for-byte `startswith` is false while the disclosure the old
   wording denied is real.

   The implementation was never wrong; the sentence was. A snippet is a
   SEPARATELY NAMED field, it carries its own `snippet_state`, it always
   ships beside a `body_href`, and it is gated by `snippet_gate` - so a
   client can never mistake one for `body_json`. Keep the guarantee
   narrow enough to be true: state it about `body_json`, and let
   `snippet` carry its own contract.
4. **There is no `?redact=` parameter on export. Ever.** A redacted
   transcript is not the transcript. A switch that produces one would
   create a mode in which the guarantee is false, and a guarantee with a
   mode is not a guarantee.
5. **Verification is a comparison that actually ran.** Any route that
   reports a transcript as verified must carry the two hashes it
   compared, so the caller can re-run the comparison itself. A boolean
   with no operands is an assertion, not a measurement.

Measured proof that the reassembly path is currently sound, run against
the live corpus on 2026-08-31:

| Transcript | Lines | Bytes | Export time | `content_sha256` match | Byte count match |
|---|---|---|---|---|---|
| 1 | 67 | 58,968 | 0.001s | yes | yes |
| 5767 | 30,805 | 91,950,363 | 1.43s | yes | yes |
| 10902 | 29,322 | 182,077,926 | 2.38s | yes | yes |

---

## 2. Secrets: FLAG, NEVER REDACT (NORMATIVE)

`message_secret_findings` stores where a secret was and a hash of it. It
never stores the matched value, and the DDL says so in a comment that
must not be edited away. This API inherits that position exactly.

1. **No matched value is ever returned, in any field, on any route.**
2. **No matched value is ever written to a log line, an exception
   message, or a `repr`.** A stack trace that carries a body fragment is
   a leak with a different shape.
3. **`GET /bodies/{id}` returns `body_json` WHOLE**, plus a `secrets`
   block of `{detector, match_offset, match_length, match_offset_utf16,
   match_length_utf16, utf16_state, value_sha256}`. The CLIENT masks
   using the offsets, and a JavaScript client MUST use the `_utf16`
   pair - see 2.2. The server does not cut the string,
   because cutting it would violate section 1 rule 3 and because an
   offset-masking client can be verified while a server-side redaction
   cannot.
4. **`/lines` carries `secret_finding_count` per row.** It is a column
   on `message_bodies`, so this costs no join.
5. **Search snippets are the ONE place the server withholds.** The
   snippet is `null`, `snippet_state` names the layer that refused, and
   the hit is STILL REPORTED with its transcript, line, offset and
   length. Withholding the snippet must never withhold the hit: a search
   that silently dropped secret-bearing matches would make the corpus's
   most sensitive material the least findable.

   **The snippet gate is BEST EFFORT and the response says so.** Until
   2026-08-31 the whole gate was `secret_finding_count > 0`. That column
   is a PROXY for "this body contains a credential" and it was measured
   wrong: one credential occupies 762 bodies of which **415 carry ZERO
   findings**, and a single `transcript_id=4` search returned **21 of 43
   hits** with that credential in cleartext. The cause is NOT stale
   findings - re-scanning those 415 bodies with the current detectors
   produced 0 findings, and all 347 flagged bodies still flag. The value
   is 40 characters with no vendor marker, so only the contextual
   `high_entropy_assignment` detector can see it, and only when a name
   saying key/token/secret sits beside it. All 533 detected occurrences
   had that context; all 587 occurrences in unflagged bodies did not. The
   gap is STRUCTURAL, so no re-scan closes it.

   The gate now has three layers, declared in `meta.snippet_gate`:

   | Layer | What it checks | What it can miss |
   |---|---|---|
   | `body_secret_finding_count` | the stored flag on the body | any occurrence the detectors could not see |
   | `detectors_over_window` | the detectors, re-run over the preview window at serve time | the same recall limit - a window is a subset of a body that already scanned clean |
   | `known_credential_value_hash` | sha256 membership of every substring of every credential-alphabet run in the window against **every value this corpus has ever detected anywhere** | a credential this corpus has NEVER detected |

   **What is guaranteed:** a value this corpus has detected anywhere will
   not appear in a snippet, even from a body carrying no finding of its
   own. That layer holds hashes and lengths only, never a value.

   **What is NOT guaranteed:** a credential never detected anywhere is
   invisible to all three layers, for the same recall reason it was never
   detected. The response states this in `meta.snippet_gate.limitation`
   rather than implying a promise nobody can keep.

   **The only hard guarantee is `snippets=false`**, which returns no
   preview text on any hit (`snippet_state: "withheld_by_request"`) while
   still reporting every hit with its coordinates. Use it for anything
   that will be screenshotted, logged or pasted.

   `snippet_state` values: `included`, `withheld_secret_bearing`
   (layer 1), `withheld_window_detector` (layer 2),
   `withheld_known_secret_value` (layer 3), `withheld_by_request`
   (`snippets=false`), `withheld_gate_unavailable` (the index could not
   be built - a could-not-evaluate, and it withholds, because "I could
   not check" must never render as "this is safe"), `cannot_determine`
   (the body row vanished between the two queries).

   **Corollary for the rotation inventory:** `message_secret_findings`
   is a LOWER BOUND on where a credential appears, not the set, so any
   count taken from it undercounts. Measured 2026-08-31 by recovering
   each detected value in memory and counting the bodies that contain it
   with `INSTR`, against the bodies flagged for it - a census of the 20
   values with the most findings plus a random sample of 40 of the
   remaining 719:

   | Stratum | Bodies containing | Bodies flagged | Missed | Recall |
   |---|---|---|---|---|
   | head, 20 values, census | 5,224 | 3,732 | 1,492 | 71.4% |
   | tail, 40 of 719 sampled | 492 | 184 | 308 | 37.4% |

   Measured lower bound, exact over those 60 values: **1,800 missed
   body-occurrences**. Extrapolating the tail mean of 7.7 missed bodies
   per value across all 719: **about 7,000 missed body-occurrences**
   corpus-wide, against 12,390 findings recorded - so the inventory sees
   roughly two thirds of the occurrences it should. The extrapolation is
   the weak half: the distribution is heavy-tailed (one value is missed
   in 131 of 133 bodies, 1.5% recall), so treat 7,000 as an order of
   magnitude and 1,800 as the floor. Re-scanning does NOT fix this - the
   gap is the detectors' contextual recall, not stale rows. Widening
   ingest detection is separate work and is not done here.

Measured on 2026-08-31: 6,240 bodies carry at least one finding (2.21s,
full scan), across 12,390 findings. Detectors in use:

| Detector | Findings |
|---|---|
| `high_entropy_assignment` | 12,307 |
| `op_service_account_token` | 70 |
| `github_token` | 6 |
| `slack_token` | 2 |
| `google_api_key` | 2 |
| `aws_access_key_id` | 2 |
| `cloudflare_api_token` | 1 |

### 2.1 Offset units (NORMATIVE, and this was measured)

**Offsets are UNICODE CODE POINT offsets into `body_json`. They are NOT
byte offsets.** An earlier revision of this document said byte offsets;
that was wrong, and it was corrected by measurement on 2026-08-31 rather
than by argument.

The evidence. Slicing the stored body by
`[match_offset : match_offset + match_length]` and hashing the result
reproduces the recorded `value_sha256` for **12,390 of 12,390 findings**
on a code-point slice, and for **0 of the 5,821 findings** where the
code-point and byte interpretations differ. **0 findings matched
neither.** The check was positive-controlled: given a synthetic row whose
offset genuinely is a byte offset, the same classifier reports "byte", so
a byte result was reachable and simply did not occur.

That is what the producers do by construction:

- **Secrets**: `message_model_secrets.scan_text` runs `re.finditer` over
  a Python `str`, so `match.start()` and `len(value)` are code-point
  counts.
- **Search**: a hit's `match_offset` is `INSTR(...) - 1`, and SQLite's
  `INSTR`, `SUBSTR` and `LENGTH` are character-based on a TEXT value
  (verified on sqlite 3.53.4: for a body whose match sits at code point 5
  and byte 8, `INSTR` returned 5).

**The two agree.** `match_offset` means one thing everywhere in this API.
Every response that carries an offset or a body size also carries the
declaration in `meta`, built by `archive_read.offset_units_meta()` so the
two sides cannot drift:

```json
"offset_units": "unicode_code_points",
"offset_units_utf16_available": true,
"body_size_units": "unicode_code_points"
```

### 2.2 JavaScript clients must mask with the UTF-16 pair

A JavaScript string is UTF-16, so `String.prototype.slice` counts an
astral-plane character (emoji, rare CJK) as TWO units where Python counts
one. **Measured: 1,100 of the 12,390 findings sit in a body carrying an
astral character BEFORE the match.** On those, masking with the raw
`match_offset` misaligns by the number of astral characters in the
prefix, which exposes the head of the credential and blanks unrelated
text.

So every finding on `GET /bodies/{id}` carries a converted pair,
computed server-side from the body already in memory:

| Field | Meaning |
|---|---|
| `match_offset` / `match_length` | code points, as stored |
| `match_offset_utf16` / `match_length_utf16` | UTF-16 code units, what JS indexes with |
| `utf16_state` | `"computed"`, or `"cannot_determine"` when the body was withheld |

The correct client recipe:

```js
const s = finding.match_offset_utf16;
const masked = body_json.slice(0, s)
  + "*".repeat(finding.match_length_utf16)
  + body_json.slice(s + finding.match_length_utf16);
```

When `utf16_state` is `"cannot_determine"` the body was withheld, so
there is no text to mask and nothing was guessed.

### 2.3 `body_bytes` is a CHARACTER count, and the name lies

`body_bytes` comes from SQLite `LENGTH(body_json)`, which counts
characters on a TEXT value. **Read `body_chars` instead** - the same
number under a name that does not have to be re-checked against the code
to be believed. `body_bytes` is retained as a deprecated alias so
existing clients do not break, and `meta.body_bytes_units` says what it
really counts.

The same caveat applies to `MAX_BODY_BYTES`: the 64 MiB withhold gate
compares against a character count, so a body of mostly multi-byte text
is admitted at up to roughly four times that many real bytes.

A real example, values withheld as required: body 119 is **5,543
characters and 5,645 bytes**, and carries two findings at offsets 2,321
and 5,060, both length 40, both the same `value_sha256`. Two occurrences
of ONE credential, which is what the hash is for.

---

## 3. The `envelope()` contract

One constructor in `src/core/archive_read.py` owns this shape. No route
builds a response dict by hand, so no route can omit a field.

```python
def envelope(
    *,
    result: Any,
    result_status: str,
    scope_status: str = SCOPE_RESOLVED,
    unevaluated: Optional[List[Dict[str, str]]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Wrap any route's payload in the three-outcome envelope.

    Description: the ONLY place this shape is constructed. A route that
      returns a bare dict is a bug the test suite fails on, because a
      response with no result_status cannot be distinguished by a client
      from one that was never evaluated.
    Inputs: result (the payload, may be [] or None), result_status (one
      of RESULT_STATUSES), scope_status (one of SCOPE_STATUSES),
      unevaluated (list of {subject, reason} - what could not be looked
      at), meta (route-specific extras: paging, scan, timing).
    Output: dict with exactly the keys below.
    Raises: ValueError - an unknown status string.
    """
```

Emitted keys, all always present:

| Key | Type | Meaning |
|---|---|---|
| `result` | any | The payload. May be `[]`, `{}` or `null`. |
| `result_status` | str | See table below. |
| `scope_status` | str | Whether the scope itself resolved. |
| `unevaluated` | list | `[{subject, reason}]`. Empty list means nothing was skipped. NEVER omitted. |
| `meta` | object | Route-specific. Always an object, never null. |

`result_status` permitted values, and nothing else:

| Value | Meaning | HTTP |
|---|---|---|
| `ok` | The question was asked and fully answered. An empty `result` here means GENUINELY EMPTY. | 200 |
| `partial` | Answered, but some work was not reached. `unevaluated` names what, and `meta` carries the resume cursor. An empty `result` here means NOTHING WAS FOUND IN THE PART THAT WAS LOOKED AT. | 200 |
| `cannot_determine` | The question could not be evaluated. `result` is `null` or `[]` and `unevaluated` says why. NEVER rendered as a healthy empty list. | 200, or 400 for a malformed cursor |
| `not_found` | The named subject does not exist. Distinct from `cannot_determine`: "there is no transcript 99999" is a measurement, "the database would not open" is not. | 404 |

`scope_status` permitted values:

| Value | Meaning |
|---|---|
| `resolved` | The scope (project, transcript, corpus, host) exists and was used. |
| `not_found` | The scope id does not exist. |
| `cannot_determine` | The scope could not be resolved, for example the datastore would not open. |

**The rule that makes this worth having.** An empty `result` is
meaningless on its own. `("ok", [])` and `("partial", [])` and
`("cannot_determine", [])` render identically to a client that reads only
`result`, and they mean three completely different things. A client MUST
branch on `result_status` before rendering an empty state, and the test
suite asserts that the three are structurally distinguishable.

### 3.1 Worked example of all three outcomes, one endpoint

Endpoint: `GET /api/v1/archive/projects/{project_id}/transcripts`.

**Outcome 1, `ok`.** Project 1 exists and has 143 transcripts. Asking for
the last page returns the tail and says so.

```json
{
  "result": [
    {
      "transcript_id": 143,
      "session_ref": "f094e663-9301-431b-be5b-687988171116",
      "session_ref_scheme": "uuid",
      "source_path": "-Users-jsugamele--claude/f094e663-9301-431b-be5b-687988171116.jsonl",
      "line_count": 89,
      "raw_byte_length": 299984,
      "content_sha256": "ad5d1d109342beb5a93960d0706da29b470821cc9eee445d57d832afa0dfaa04",
      "ingested_at": "2026-08-29T22:17:15.825831Z",
      "host_attribution": "manifest_verified",
      "project_attribution": "derived"
    }
  ],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {
    "paging": {"limit": 50, "returned": 1, "has_more": false, "next_cursor": null},
    "scope": {"kind": "project", "project_id": 1, "slug": "-Users-jsugamele--claude"}
  }
}
```

**Outcome 2, `cannot_determine` (malformed cursor).** HTTP 400. The
cursor did not parse. This is NOT a silent restart at page 1, because a
client that pages through 3,416 transcripts and silently restarts will
render duplicates forever and never finish.

```json
{
  "result": null,
  "result_status": "cannot_determine",
  "scope_status": "resolved",
  "unevaluated": [
    {
      "subject": "cursor",
      "reason": "cursor did not decode as a v1 transcripts cursor: invalid base64url padding"
    }
  ],
  "meta": {"paging": {"limit": 50, "returned": 0, "has_more": null, "next_cursor": null}}
}
```

**Outcome 3, `not_found`.** HTTP 404. Project 99999 does not exist. Note
`result` is `[]` and NOT `null`, and it is the `scope_status` that
carries the finding. A client rendering "no transcripts" here would be
wrong: there is no project to have transcripts.

```json
{
  "result": [],
  "result_status": "not_found",
  "scope_status": "not_found",
  "unevaluated": [
    {"subject": "project:99999", "reason": "no row in message_projects with id 99999"}
  ],
  "meta": {"paging": {"limit": 50, "returned": 0, "has_more": null, "next_cursor": null}}
}
```

Note `has_more` is `null`, not `false`, in both failure cases. `false`
would be a claim that the end of the list was reached, and no list was
read.

#### 3.1.1 The shape of `result`, per status (NORMATIVE)

The three examples above are one endpoint. The rule they illustrate has
to hold on ALL of them, because a client cannot classify an outcome from
the shape of `result` unless the shape is consistent. Stated once:

| `result_status` | `result` MUST be | Why |
|---|---|---|
| `ok` | the natural payload; `[]` on a collection route means GENUINELY EMPTY | an empty page is a measurement |
| `partial` | the rows that WERE read, possibly `[]` | `unevaluated` names what was not reached |
| `not_found` | the route's SUCCESS shape: `[]` for a collection route, `null` for a single-object route | the subject provably not existing is a MEASUREMENT, and `scope_status` carries it |
| `cannot_determine` | `null`, ON EVERY ROUTE, collection or not | the question was NOT evaluated, so there is no payload of any shape |

**The asymmetry between the last two rows is deliberate and is the point
of the rule.** `not_found` keeps the list shape because the API did look
and can report what it found. `cannot_determine` does not, because
returning `[]` lets a client that reads only `result` - and plenty do -
render a confident empty state over a question nobody answered. `null`
makes that client crash instead, which is the correct outcome for code
that ignores `result_status`.

**A single-object route is not an exception to the `not_found` row, it is
an instance of it.** `GET /transcripts/{id}` and `GET /bodies/{id}`
answer `null` on `not_found` because `null` IS their success shape - they
return an object, not a list. Comparing their `null` against a
collection route's `[]` looks like an inconsistency and is not one; the
rule is "match your own success shape", not "always a list".

**Conformance, MEASURED live 2026-08-31 against every archive endpoint.**
Two routes violated the `cannot_determine` row: a malformed cursor on
`GET /projects/{id}/transcripts` and on `GET /corpora/{id}/unattributed`
returned `result: []`. Both are served by one helper
(`archive_hierarchy._transcript_page`), so both were one line, and both
now return `null`. Every other route already complied, and the
`not_found` row had no violations anywhere. `tests/test_archive_result_shape.py`
holds the rule: it checks each route, and it also parses every
`archive*.py` module and fails on any `cannot_determine_envelope(...)`
or `cursor_error_envelope(...)` call site passing `result=[]`, so a route
added later is caught when it is written rather than when a client
mis-renders it.

**One route takes no cursor at all.** `GET /hosts/{host_id}/corpora` is
deliberately unpaginated - measured 3 rows across the fleet - so it has
no cursor contract, and a `cursor` query parameter passed to it is
IGNORED rather than rejected. It answers `ok` with the full list. That is
not a violation of the rule above, which governs routes that page, but a
client that pages generically over every endpoint should know that this
one will never hand back a `next_cursor` and never refuse a cursor.

---

## 4. Constants

Declared once in `src/core/archive_read.py` and imported everywhere.

| Constant | Value | Why this number |
|---|---|---|
| `MAX_PAGE_LIMIT` | 200 | Hierarchy and transcript pages. |
| `MAX_LINE_LIMIT` | 500 | Line pages. 501 rows of line metadata from the largest transcript measured 0.0016s. |
| `DEFAULT_PAGE_BYTES` | 1048576 | 1 MiB. Soft cap on a `/lines` page when bodies are included; the page stops early and reports it. |
| `MAX_BODY_BYTES` | 67108864 | 64 MiB. Above this a body is withheld with a `body_href`. See section 10: NO body in the corpus currently exceeds this. |
| `MAX_SCAN_BUDGET` | 2000 | Transcripts. Retained as a SECONDARY cap only. See `MAX_SCAN_BYTES`. |
| `MAX_SCAN_BYTES` | 536870912 | 512 MiB. **NEW, and it is the primary search governor.** Measured scan rate is 0.44 GB/s, so this is about 1.2s of work. See section 11 correction 2. |
| `VERIFY_BEFORE_SEND_MAX_BYTES` | 8388608 | 8 MiB. Above this, verify-before-send is refused and the caller must stream. 223 of 21,039 transcripts exceed it. |
| `SCAN_BYTES_PER_SECOND` | 440000000 | Measured constant, used only to render a predicted cost in `meta`. Never used to decide anything. |

---

## 5. Cursors and pagination

Keyset, never offset. `OFFSET n` makes SQLite walk and discard n rows, so
page 100 costs 100 pages of work, and a row inserted during paging shifts
every later page by one and silently skips a row.

### 5.1 The cursor module

`src/core/archive_cursor.py` is the ONLY place a cursor is encoded or
parsed. Two functions, and nothing else may build one.

```python
def encode_cursor(kind: str, payload: Dict[str, Any]) -> str:
    """Encode a keyset cursor as an opaque base64url string.

    Description: the payload is JSON with sorted keys, so the same
      position always encodes to the same string. ``kind`` is embedded
      and checked on decode, which is what stops a transcripts cursor
      being replayed against the lines endpoint and silently paging from
      a position that means something else there.
    Inputs: kind (str), payload (dict).
    Output: str - base64url, unpadded.
    Example: encode_cursor("transcripts", {"v": 1, "ingested_at": "...", "id": 143})
    """


def decode_cursor(kind: str, raw: str) -> Dict[str, Any]:
    """Parse a cursor, or raise.

    Description: raises on ANY defect - bad base64, bad JSON, wrong
      kind, wrong version, a missing key, a key of the wrong type. The
      caller turns that into a 400 cannot_determine. There is no
      recovery path and there must not be one: silently treating a
      malformed cursor as "start at the beginning" turns a client bug
      into an infinite duplicate-rendering loop that looks like it is
      working.
    Inputs: kind (str), raw (str).
    Output: dict - the payload.
    Raises: CursorError - naming which part failed.
    """
```

Encoding is `base64url(json.dumps(payload, sort_keys=True,
separators=(",", ":")))`, unpadded. It is OPAQUE: clients must not
construct or inspect one. It is not encrypted and not a security
boundary, because every value in it is already visible in the response.

### 5.2 The ISO-8601 dependency, stated out loud

`ingested_at`, `ts` and `first_seen_at` are all TEXT columns. Ordering
them with `<` and `>` works ONLY because every value is a
zero-padded, fixed-width, UTC, Z-suffixed ISO-8601 string, which makes
lexicographic order identical to chronological order. **This is a
property of the data, not of the schema. There is no CHECK constraint
enforcing it.**

Measured on 2026-08-31:

| Column | Rows | Length | Last char | Verdict |
|---|---|---|---|---|
| `message_transcripts.ingested_at` | 21,039 | 27, uniform | `Z`, uniform | Safe to order lexicographically |
| `message_bodies.first_seen_at` | 2,447,028 | 27, uniform | `Z`, uniform | Safe |
| `message_bodies.ts` | 2,413,548 | 24, uniform | `Z`, uniform | Safe |
| `message_bodies.ts` | 33,480 | NULL | NULL | **NOT orderable. See section 10.** |

27 characters is `2026-08-29T22:17:03.086206Z` (microseconds). 24 is
`2025-12-29T06:50:35.600Z` (milliseconds). Both are fixed-width within
their column, which is what matters; the two columns are never compared
against each other.

If a future ingest writes a local-time or offset-suffixed timestamp into
any of these columns, every keyset cursor in this API silently starts
skipping and duplicating rows with no error. `test_archive_cursor.py`
asserts the uniformity invariant against the live schema so that change
fails a test rather than corrupting paging.

### 5.3 Cursor payloads, per endpoint

Every payload carries `"v": 1`. A payload whose `v` is not 1 is a
`CursorError`.

| Endpoint | Sort | Tie-break | Payload |
|---|---|---|---|
| `/hosts` | `id ASC` | none needed (`id` is unique) | not paginated, 2 rows |
| `/hosts/{id}/corpora` | `id ASC` | none needed | not paginated, max 3 rows |
| `/corpora/{id}/projects` | `slug ASC` | `slug` is UNIQUE per corpus, so it IS the tie-break | `{"v":1,"slug":"-Users-jsugamele-Development"}` |
| `/projects/{id}/transcripts` | `ingested_at DESC` | `id DESC` | `{"v":1,"ingested_at":"2026-08-29T22:17:15.825831Z","id":143}` |
| `/corpora/{id}/unattributed` | `ingested_at DESC` | `id DESC` | same as above |
| `/transcripts/{id}/lines` | `line_no ASC` | `line_no` is UNIQUE per transcript, so it IS the tie-break | `{"v":1,"line_no":15000}` |
| `/transcripts/{id}/subagents` | `id ASC` | `id` is unique | `{"v":1,"appearance_id":2764357}` |
| `/search` | scan order | `(transcript_id, line_no)` | `{"v":1,"t_ingested_at":"...","t_id":903,"line_no":412,"scanned":118,"bytes":49221873}` |

Two of these need no synthetic tie-break because the schema already
guarantees uniqueness, and that is worth stating rather than adding a
redundant `id` to the cursor:

- `message_projects` has `UNIQUE (corpus_id, slug)`.
- `message_appearances` has `UNIQUE (transcript_id, line_no)`.

`ingested_at` has NO such guarantee. Measured: 21,039 transcripts were
ingested in a single batch and timestamps repeat at microsecond
resolution across rows. `id DESC` as a tie-break is therefore mandatory
there, and `test_archive_keyset.py` walks a real tie to prove no row is
visited twice or skipped.

### 5.4 The keyset predicate

For a DESC page on `(ingested_at, id)`:

```sql
WHERE project_id = :project_id
  AND (ingested_at < :cur_ingested_at
       OR (ingested_at = :cur_ingested_at AND id < :cur_id))
ORDER BY ingested_at DESC, id DESC
LIMIT :limit_plus_one
```

The first page omits the `AND (...)` clause entirely rather than passing
a sentinel like `'9999'`. A sentinel works today and is a landmine: the
day a timestamp sorts above it, page 1 silently returns nothing.

### 5.5 How `has_more` is computed

**Fetch `limit + 1` rows. If `limit + 1` came back, `has_more` is true
and the extra row is DISCARDED, not returned.** `next_cursor` is built
from the LAST RETURNED row, not from the discarded one.

This is the only method that does not lie. The alternatives both do:

| Method | Why it is wrong |
|---|---|
| `returned == limit` | A page that lands exactly on the end reports `has_more: true` and the next page is empty. The client renders a "load more" that does nothing. |
| `COUNT(*)` of the scope | A second query, racing the first, and on the largest project it is a 3,416-row count on every page for a number the client does not need. |

`has_more` is `null`, never `false`, whenever no list was actually read
(`not_found`, `cannot_determine`). `false` is a claim about the end of a
list; a route that never read the list has no basis for it.

---

## 6. The endpoints

All mounted under `/api/v1` by one `app.include_router` in `src/main.py`.
Router prefix is `/archive`, so full paths are `/api/v1/archive/...`.

**Every route, without exception:**

- carries `response_model=None`. A FastAPI `response_model` is a FILTER,
  not a passthrough: it silently deletes any field the model does not
  declare. That has bitten this project twice, most recently
  `ThemeManifest` dropping `themeCss` from every `/api/v1/themes`
  response while the value existed the whole way up to serialization.
  The envelope's `unevaluated` and `meta` blocks are exactly the kind of
  optional, route-varying structure a response model eats.
- carries `dependencies=[Depends(require_auth)]`, matching
  `corpus_routes.py`. This is not optional and not configurable. The
  archive is a complete record of the owner's work, including 6,240
  bodies with credential material in them.
- does its SQLite work inside `await asyncio.to_thread(...)`. SQLite
  calls block, and a 1.2s search on the event loop stalls every live
  terminal WebSocket in the process.
- opens its connection with `archive_read.open_read_only(state_dir)` and
  closes it in a `contextlib.closing`. One connection per request, never
  shared across threads.

### 6.0 Endpoint index

| # | Method | Path | Fast today? |
|---|---|---|---|
| 6.1 | GET | `/archive/hosts` | Yes, 0.0006s |
| 6.2 | GET | `/archive/hosts/{host_id}/corpora` | Yes, 0.0016s |
| 6.3 | GET | `/archive/corpora/{corpus_id}/projects` | Yes, 0.0102s |
| 6.4 | GET | `/archive/projects/{project_id}/transcripts` | Yes, 0.0018s; `session_ref_scheme` filter 0.0012s |
| 6.5 | GET | `/archive/corpora/{corpus_id}/unattributed` | Yes, 0.0079s |
| 6.6 | GET | `/archive/transcripts/{transcript_id}` | Yes, 0.0003s |
| 6.7 | GET | `/archive/transcripts/{transcript_id}/lines` | Yes, 0.0016s; `start_line` adds one 0.0008s index seek |
| 6.8 | GET | `/archive/bodies/{body_id}` | Yes, <0.001s |
| 6.9 | GET | `/archive/transcripts/{transcript_id}/export` | Streaming, 2.38s for 182 MB |
| 6.10 | GET | `/archive/transcripts/{transcript_id}/export/verified` | 8 MiB cap, refuses above |
| 6.11 | GET | `/archive/search` | Scoped, budgeted, 0.44 GB/s |
| 6.12 | GET | `/archive/transcripts/{transcript_id}/subagents` | Yes, 0.058s worst case |

None of these depend on the unbuilt `message_bodies(ts)` index. The only
feature that would is the global message feed, which is EXCLUDED from v1
(section 7).

---

### 6.1 GET /archive/hosts

Top of the hierarchy. Not paginated: measured 2 rows, and the table is
bounded by the number of physical machines the owner owns.

**Params:** none.

**SQL:**

```sql
SELECT h.id, h.machine_id, h.machine_id_scheme, h.display_name,
       h.hostname, h.platform, h.first_seen_at,
       (SELECT COUNT(*) FROM message_corpora k WHERE k.host_id = h.id)
         AS corpus_count,
       (SELECT COUNT(*) FROM message_transcripts t WHERE t.host_id = h.id)
         AS transcript_count
  FROM message_hosts h
 ORDER BY h.id
```

**Measured plan (2026-08-31):**

```
SCAN h
CORRELATED SCALAR SUBQUERY 1
  SEARCH k USING COVERING INDEX sqlite_autoindex_message_corpora_1 (host_id=?)
CORRELATED SCALAR SUBQUERY 2
  SEARCH t USING COVERING INDEX ix_message_transcripts_host (host_id=?)
```

`SCAN h` over 2 rows is correct, not a defect. Both subqueries are
covering index searches. **Measured 0.0006s.**

**Response:**

```json
{
  "result": [
    {
      "host_id": 1,
      "machine_id": "F95816BC-2819-53B5-98E9-72450A37AADF",
      "machine_id_scheme": "platform_uuid",
      "display_name": "Joe-MBP-M1",
      "hostname": "Joe-MBP-M1",
      "platform": "Darwin 25.6.0",
      "corpus_count": 2,
      "transcript_count": 19562
    },
    {
      "host_id": 2,
      "machine_id": "726E10C9-E70D-5F9E-ACA6-F5CB0D79BA40",
      "machine_id_scheme": "platform_uuid",
      "display_name": "Joseph’s Mac mini (2)",
      "hostname": "mac-mini-m4.local",
      "platform": "Darwin 25.6.0",
      "corpus_count": 1,
      "transcript_count": 1477
    }
  ],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {
    "totals": {"hosts": 2, "transcripts_attributed_to_a_host": 21039,
               "transcripts_with_no_host_id": 0}
  }
}
```

`transcripts_with_no_host_id` is emitted ALWAYS, including when it is 0.
A client must be able to tell "every transcript is attributed" from "the
question was not asked". Measured today: 0.

---

### 6.2 GET /archive/hosts/{host_id}/corpora

**Params:** `host_id` (int, path). Not paginated: measured 3 rows total
across the fleet, bounded by the number of directories Claude writes to.

**SQL:**

```sql
SELECT k.id, k.corpus_key, k.root_path, k.collected_at,
       k.manifest_sha IS NOT NULL AS has_manifest,
       (SELECT COUNT(*) FROM message_projects p WHERE p.corpus_id = k.id)
         AS project_count,
       (SELECT COUNT(*) FROM message_transcripts t WHERE t.corpus_id = k.id)
         AS transcript_count,
       (SELECT COUNT(*) FROM message_transcripts t
         WHERE t.corpus_id = k.id AND t.project_id IS NULL)
         AS unattributed_transcript_count
  FROM message_corpora k
 WHERE k.host_id = :host_id
 ORDER BY k.id
```

**Measured plan:**

```
SEARCH k USING INDEX sqlite_autoindex_message_corpora_1 (host_id=?)
CORRELATED SCALAR SUBQUERY 1
  SEARCH p USING COVERING INDEX sqlite_autoindex_message_projects_1 (corpus_id=?)
CORRELATED SCALAR SUBQUERY 2
  SEARCH t USING COVERING INDEX ux_message_transcripts_corpus_path (corpus_id=?)
USE TEMP B-TREE FOR ORDER BY
```

The temp b-tree sorts at most 3 rows. **Measured 0.0016s.**

`manifest_sha` itself is NOT returned, only whether one exists. The hash
identifies a collection manifest and is of no use to a browser client.

**Response:**

```json
{
  "result": [
    {
      "corpus_id": 1,
      "corpus_key": "claude-projects",
      "root_path": "/Users/jsugamele/.claude/projects",
      "collected_at": "2026-08-30T16:01:00.308861Z",
      "has_manifest": false,
      "project_count": 71,
      "transcript_count": 19548,
      "unattributed_transcript_count": 0
    },
    {
      "corpus_id": 2,
      "corpus_key": "local-agent-mode-sessions",
      "root_path": "/Users/jsugamele/Library/Application Support/Claude/local-agent-mode-sessions",
      "collected_at": "2026-08-30T16:01:45.993463Z",
      "has_manifest": true,
      "project_count": 5,
      "transcript_count": 14,
      "unattributed_transcript_count": 5
    }
  ],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {"scope": {"kind": "host", "host_id": 1, "display_name": "Joe-MBP-M1"}}
}
```

**`has_manifest: false` on corpus 1, and its transcripts still say
`manifest_verified`. FLAGGED, NOT FIXED - somebody should look at this.**
This example said `true` and live says `false`; the live value is the
correct rendering of what is stored, because `message_corpora.manifest_sha`
IS NULL for corpus 1 and the endpoint returns
`manifest_sha IS NOT NULL AS has_manifest`. The number is not the
interesting part. MEASURED 2026-08-31: corpus 1 holds 19,548 transcripts
and 19,545 of them carry `host_attribution: "manifest_verified"`, which
this API maps to `attribution_state: "evidenced"`. So a corpus with NO
manifest is the source of 19,545 transcripts claiming to have been
verified against one. Fleet-wide only 3 transcripts of 21,039 are
`cannot_determine` and every other one is `manifest_verified`.

Either the attribution was written by a pass that had a manifest which
was never persisted, or `manifest_verified` does not mean what its name
says. Both readings are worth knowing and neither is safe to guess at, so
this is recorded rather than papered over by editing a boolean: an
`evidenced` state that no evidence backs is precisely the false-green
class this document's three-outcome rule exists to kill, and quietly
changing `true` to `false` here would have hidden the question. The
ingest side owns the answer; this API is reporting the stored values
correctly.

Note also that `manifest_sha` itself is deliberately NOT returned by this
endpoint - only whether one exists. A client cannot cross-check the two.

**`unattributed_transcript_count` is the whole reason this field exists.**
Corpus 2 holds 14 transcripts of which 5 have no project. Without this
count a client renders 5 projects and 9 transcripts and the other 5 are
invisible by construction, which is the exact defect class this repo
calls a missing-row-shape bug. Endpoint 6.5 is where they are read.

---

### 6.3 GET /archive/corpora/{corpus_id}/projects

**Params:**

| Param | Type | Default | Bounds |
|---|---|---|---|
| `corpus_id` | int, path | required | must exist |
| `limit` | int, query | 50 | 1 to `MAX_PAGE_LIMIT` (200) |
| `cursor` | str, query | none | opaque; malformed is 400 |

**SQL (page):**

```sql
SELECT p.id, p.slug, p.observed_cwd, p.first_seen_at
  FROM message_projects p
 WHERE p.corpus_id = :corpus_id
   AND (:cursor_slug IS NULL OR p.slug > :cursor_slug)
 ORDER BY p.slug
 LIMIT :limit_plus_one
```

**Measured plan:**

```
SEARCH p USING INDEX sqlite_autoindex_message_projects_1 (corpus_id=? AND slug>?)
```

A single index search covering BOTH the equality and the range, with no
temp b-tree, because `UNIQUE (corpus_id, slug)` is exactly the composite
this query needs. **Measured 0.0001s for 51 rows.**

**SQL (transcript counts), issued once per page for the page's project
ids:**

```sql
SELECT project_id, COUNT(*) AS n
  FROM message_transcripts
 WHERE corpus_id = :corpus_id
 GROUP BY project_id
```

**Measured plan:**

```
SEARCH message_transcripts USING INDEX ux_message_transcripts_corpus_path (corpus_id=?)
USE TEMP B-TREE FOR GROUP BY
```

**Measured 0.0102s** for corpus 1 (19,548 transcripts, 71 groups). This
is the most expensive query in the hierarchy and it is still 10ms. It
counts the WHOLE corpus rather than just the page's projects because the
grouped form is one indexed range scan, while an `IN (...)` over 50
project ids would be 50 separate index probes on `ix_message_transcripts_project`.
Both are acceptable; the measured one is simpler and its cost does not
grow with `limit`.

**Response:**

```json
{
  "result": [
    {
      "project_id": 69,
      "slug": "-Users-jsugamele",
      "observed_cwd": "/Users/jsugamele",
      "first_seen_at": "2026-08-30T16:01:23.769297Z",
      "transcript_count": 41
    },
    {
      "project_id": 1,
      "slug": "-Users-jsugamele--claude",
      "observed_cwd": "/Users/jsugamele/.claude",
      "first_seen_at": "2026-08-30T16:01:00.318581Z",
      "transcript_count": 143
    }
  ],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {
    "paging": {"limit": 50, "returned": 2, "has_more": true,
               "next_cursor": "eyJzbHVnIjoiLVVzZXJzLWpzdWdhbWVsZS0tY2xhdWRlIiwidiI6MX0"},
    "scope": {"kind": "corpus", "corpus_id": 1, "corpus_key": "claude-projects"},
    "unattributed": {
      "transcript_count": 0,
      "href": "/api/v1/archive/corpora/1/unattributed"
    }
  }
}
```

The `meta.unattributed` block is emitted on EVERY page, so a client
paging projects can never finish the list believing it has seen
everything in the corpus.

---

### 6.4 GET /archive/projects/{project_id}/transcripts

**Params:**

| Param | Type | Default | Bounds |
|---|---|---|---|
| `project_id` | int, path | required | must exist |
| `limit` | int, query | 50 | 1 to 200 |
| `cursor` | str, query | none | opaque |
| `session_ref_scheme` | str, query | none | a scheme the archive holds |

**`session_ref_scheme` (added 2026-08-31).** A POST-FILTER inside the
already-indexed project range, deliberately the same shape as the
`role` / `record_type` / `model` filters on 6.7 rather than a second
mechanism. Measured on the live corpus: 19,588 of 21,039 transcripts
(93.1%) are `agent`-scheme sidechain files and only 1,451 are `uuid`, so
a person hunting for a conversation was paging a list that is 93 percent
noise.

* **Unknown value is `cannot_determine`, not an empty `ok`.** "there is
  no scheme called `convo` in this archive" and "no transcript in this
  project has that scheme" are different findings. The first is a 400
  under the subject `filter:session_ref_scheme`, naming the schemes that
  do exist. The second is a 200 with `result: []` and
  `meta.filters.matched_in_scope: 0`.
* **Existence is resolved against the DATA**, by a `LIMIT 1` probe, for
  the same reason 6.7 resolves against `message_roles`: a constant in
  the code is a guess that ages into a lie the day the ingest learns a
  third scheme. Measured 0.0000s for a value that exists, 0.0037s warm
  for one that does not.
* **THE COUNTS ARE SCOPED AND SAY SO.** `meta.filters` carries
  `matched_in_scope`, `scope_total_before_filter` and
  `counts_are: "scanned_within_this_scope_only"`. Measured on project
  12: 77 `uuid` + 3,339 `agent` = 3,416, the unfiltered scope total.
  These are never corpus totals.
* **IT FILTERS ON THE COLUMN AND ON NOTHING ELSE**, and the response
  says so in `meta.filters.session_ref_scheme_means`. The column is not
  a guarantee of conversation-ness: 19 of the 1,451 `uuid`-scheme
  transcripts carry a `session_ref` that is not a UUID at all (literal
  values such as `audit` and `journal`). A UI must not render this
  filter as "these are the conversations".
* **Keyset is unaffected.** The predicate sits inside the `WHERE`, so
  SQLite applies it before `LIMIT`: the query still fetches `size + 1`
  MATCHING rows, `has_more` still means "a matching row exists past this
  page", and `next_cursor` still names the last MATCHING row. A cursor
  minted under one filter positions inside that filter's result set, so
  a client changing the filter must start a new walk, not replay its
  cursor.
* `meta.filters` is emitted on EVERY response, including unfiltered ones
  (`applied: false`, `matched_in_scope: null`), so a client can tell "I
  did not filter" from "this build has no filter".

**SQL:**

```sql
SELECT t.id, t.session_ref, t.session_ref_scheme, t.source_path,
       t.line_count, t.raw_byte_length, t.content_sha256, t.ingested_at,
       t.line_ending, t.has_trailing_newline,
       t.host_attribution, t.project_attribution
  FROM message_transcripts t
 WHERE t.project_id = :project_id
   AND (:scheme_value IS NULL OR t.session_ref_scheme = :scheme_value)
   AND (:cur_ts IS NULL
        OR t.ingested_at < :cur_ts
        OR (t.ingested_at = :cur_ts AND t.id < :cur_id))
 ORDER BY t.ingested_at DESC, t.id DESC
 LIMIT :limit_plus_one
```

**Measured plan:**

```
SEARCH t USING INDEX ix_message_transcripts_project (project_id=?)
USE TEMP B-TREE FOR ORDER BY
```

**The temp b-tree is accepted deliberately, with a measured bound.**
`ix_message_transcripts_project` is on `(project_id)` alone, so it
resolves the equality but carries no ordering, and SQLite must sort every
matching row on every page. The bound is the largest project.

| Project | Transcripts | Measured page time |
|---|---|---|
| 1 | 143 | 0.0004s |
| 12 (largest in the corpus) | 3,416 | 0.0018s |

1.8ms on the worst project in an 11 GB corpus does not justify a second
index. A composite `(project_id, ingested_at DESC, id DESC)` would remove
the sort, and it is NOT sanctioned: it costs write amplification on every
ingest and about 700 KB, to save 1.8ms. If a project ever reaches roughly
100,000 transcripts this should be re-measured, and that is the trigger
condition, not a hunch.

**Response:** see section 3.1 outcome 1 for the literal shape. Each row
additionally carries:

```json
{
  "transcript_id": 4,
  "session_ref": "0bd09502-f4be-48f2-ac56-dce81b92d20b",
  "session_ref_scheme": "uuid",
  "source_path": "-Users-jsugamele--claude/0bd09502-f4be-48f2-ac56-dce81b92d20b.jsonl",
  "line_count": 980,
  "raw_byte_length": 3181330,
  "content_sha256": "97e784442e92b8fd770b469310557666c551bd140fc6b4787c1703fd7bf7d242",
  "ingested_at": "2026-08-29T22:17:03.142046Z",
  "line_ending": "LF",
  "has_trailing_newline": true,
  "host_attribution": "manifest_verified",
  "project_attribution": "derived",
  "attribution_state": "evidenced"
}
```

`attribution_state` is DERIVED by the API, not stored, and it is how the
three-outcome rule reaches the client for attribution:

| `host_attribution` | `attribution_state` | Meaning |
|---|---|---|
| `manifest_verified` | `evidenced` | Bytes hashed to what the source machine said was at that path. |
| `declared` | `claimed` | No manifest covers this corpus. The host is the operator's claim, stored AS a claim. |
| `cannot_determine` | `cannot_determine` | A manifest exists and this file is not in it, or is in it with a different hash. **Never upgraded to `claimed`.** |

Measured distribution across all 21,039 transcripts on 2026-08-31:

| `host_attribution` | `project_attribution` | Rows | `host_id` NULL | `project_id` NULL |
|---|---|---|---|---|
| `manifest_verified` | `derived` | 21,031 | 0 | 0 |
| `manifest_verified` | `none_declared` | 5 | 0 | 5 |
| `cannot_determine` | `derived` | 3 | 0 | 0 |

**The 3 `cannot_determine` rows still have a `host_id`.** They appear
under that host in the hierarchy and they are NOT hidden, because hiding
them would be the silent drop this design forbids. Their
`attribution_state` says the attribution is unevidenced, and a client
must render that distinctly. This is the single most easily missed
detail in the whole hierarchy: a transcript can be attributed and
unevidenced at the same time.

---

### 6.5 GET /archive/corpora/{corpus_id}/unattributed

The transcripts that belong to a corpus but to NO project. Without this
route they are unreachable by navigation, because every other path into
a transcript goes through a project.

**Params:** `corpus_id` (int, path), `limit` (1 to 200, default 50),
`cursor` (opaque).

**SQL:**

```sql
SELECT t.id, t.session_ref, t.session_ref_scheme, t.source_path,
       t.line_count, t.raw_byte_length, t.content_sha256, t.ingested_at,
       t.host_attribution, t.project_attribution
  FROM message_transcripts t
 WHERE t.corpus_id = :corpus_id
   AND t.project_id IS NULL
   AND (:cur_ts IS NULL
        OR t.ingested_at < :cur_ts
        OR (t.ingested_at = :cur_ts AND t.id < :cur_id))
 ORDER BY t.ingested_at DESC, t.id DESC
 LIMIT :limit_plus_one
```

**Measured plan:**

```
SEARCH t USING INDEX ux_message_transcripts_corpus_path (corpus_id=?)
USE TEMP B-TREE FOR ORDER BY
```

**Measured 0.0079s** against corpus 1 (19,548 rows scanned, 0 matched).
`project_id IS NULL` is an unindexed post-filter on the corpus range, so
the cost is proportional to the corpus, not to the answer. At 8ms on the
largest corpus that is fine, and it is worth naming: this is the one
route whose cost is unrelated to how much it returns.

**Response when genuinely empty (corpus 1):**

```json
{
  "result": [],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {
    "paging": {"limit": 50, "returned": 0, "has_more": false, "next_cursor": null},
    "scope": {"kind": "corpus", "corpus_id": 1},
    "unattributed_transcript_count": 0,
    "note": "every transcript in this corpus resolved to a project"
  }
}
```

`result_status: "ok"` with an empty list here means GENUINELY EMPTY, and
`note` says so in words. Contrast with a `cannot_determine`, where
`result` would be `null`.

---

### 6.6 GET /archive/transcripts/{transcript_id}

One transcript's header, without its lines. This is what a client loads
before deciding whether to page the conversation or export it.

**Params:** `transcript_id` (int, path).

**SQL:**

```sql
SELECT t.*, p.slug AS project_slug, k.corpus_key, k.root_path,
       h.display_name AS host_display_name, h.machine_id
  FROM message_transcripts t
  LEFT JOIN message_projects p ON p.id = t.project_id
  LEFT JOIN message_corpora  k ON k.id = t.corpus_id
  LEFT JOIN message_hosts    h ON h.id = t.host_id
 WHERE t.id = :transcript_id
```

Plan is `SEARCH t USING INTEGER PRIMARY KEY (rowid=?)` plus three
LEFT-JOIN primary key lookups. **Measured under 0.001s.**

A second query supplies the counts a client needs to size its paging:

```sql
SELECT COUNT(*) AS appearances,
       SUM(a.line_status = 'ok') AS ok_lines,
       SUM(a.line_status = 'blank') AS blank_lines,
       SUM(a.line_status = 'invalid_json') AS invalid_json_lines,
       SUM(a.body_id IS NULL) AS lines_without_body,
       SUM(a.raw_line IS NOT NULL) AS lines_with_raw_line,
       SUM(a.agent_id IS NOT NULL OR a.is_sidechain = 1) AS subagent_lines,
       SUM(a.fidelity_outcome != 'fidelity_verified') AS unverified_lines
  FROM message_appearances a
 WHERE a.transcript_id = :transcript_id
```

Plan: `SEARCH a USING INDEX sqlite_autoindex_message_appearances_1
(transcript_id=?)`. **Measured 0.132s on the 30,805-line transcript**,
which is the corpus maximum, and 0.0003s on a 67-line one.

**Response:**

```json
{
  "result": {
    "transcript_id": 4,
    "source_ref": "F95816BC-2819-53B5-98E9-72450A37AADF::claude-projects::-Users-jsugamele--claude/0bd09502-f4be-48f2-ac56-dce81b92d20b.jsonl",
    "session_ref": "0bd09502-f4be-48f2-ac56-dce81b92d20b",
    "session_ref_scheme": "uuid",
    "source_path": "-Users-jsugamele--claude/0bd09502-f4be-48f2-ac56-dce81b92d20b.jsonl",
    "line_ending": "LF",
    "has_trailing_newline": true,
    "line_count": 980,
    "raw_byte_length": 3181330,
    "content_sha256": "97e784442e92b8fd770b469310557666c551bd140fc6b4787c1703fd7bf7d242",
    "ingested_at": "2026-08-29T22:17:03.142046Z",
    "host": {"host_id": 1, "machine_id": "F95816BC-2819-53B5-98E9-72450A37AADF",
             "display_name": "Joe-MBP-M1"},
    "corpus": {"corpus_id": 1, "corpus_key": "claude-projects",
               "root_path": "/Users/jsugamele/.claude/projects"},
    "project": {"project_id": 1, "slug": "-Users-jsugamele--claude"},
    "host_attribution": "manifest_verified",
    "project_attribution": "derived",
    "attribution_state": "evidenced",
    "counts": {
      "appearances": 980, "ok_lines": 980, "blank_lines": 0,
      "invalid_json_lines": 0, "lines_without_body": 0,
      "lines_with_raw_line": 0, "subagent_lines": 0, "unverified_lines": 0
    },
    "export": {
      "stream_href": "/api/v1/archive/transcripts/4/export",
      "verified_href": "/api/v1/archive/transcripts/4/export/verified",
      "verified_available": true,
      "verified_unavailable_reason": null
    }
  },
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {}
}
```

`export.verified_available` is `raw_byte_length <=
VERIFY_BEFORE_SEND_MAX_BYTES`. When false,
`verified_unavailable_reason` names the size and the cap, so a client
never has to discover the refusal by making the request. Measured: 223 of
21,039 transcripts (1.1 percent) have `verified_available: false`.

`project` is `null` for the 5 transcripts with `project_id IS NULL`, and
`project_attribution` is `none_declared`. That is a fact about the
source, not a failure, and it must not be rendered as an error.

---

### 6.7 GET /archive/transcripts/{transcript_id}/lines

The conversation reader. Line METADATA plus, optionally, whole bodies.

**Params:**

| Param | Type | Default | Bounds |
|---|---|---|---|
| `transcript_id` | int, path | required | must exist |
| `limit` | int, query | 100 | 1 to `MAX_LINE_LIMIT` (500) |
| `cursor` | str, query | none | opaque |
| `include_bodies` | bool, query | `false` | |
| `max_page_bytes` | int, query | `DEFAULT_PAGE_BYTES` (1048576) | 1024 to 8388608 |
| `role` | str, query | none | `user` or `assistant` |
| `record_type` | str, query | none | one of the 26 values |
| `model` | str, query | none | one of the 13 values |
| `start_line` | int, query | none | 0-based; MUTUALLY EXCLUSIVE with `cursor` |

**`start_line` (added 2026-08-31).** Opens the page at a 0-BASED
`line_no` instead of at line 0. Before it, this endpoint took `limit` and
an opaque `cursor` and nothing else, so there was NO SUPPORTED WAY to
open a transcript at line N - the UI's own deep link `/archive/t/<id>/l/<n>`
rendered a client-side `cannot_determine` for every line past the first
page, and transcript 5767 has 30,805 lines. Hand-synthesising
`base64url({"line_no": N-1, "v": 1})` does position the page and a client
must never do it: section 5.1 declares a cursor OPAQUE, so a client built
on its payload shape breaks by SKIPPING ROWS the day that shape changes.

*Implementation:* `start_line` is applied as `cur_line_no = start_line - 1`
against the EXISTING `line_no > :cur_line_no` predicate. The SQL below is
unchanged, so the index path (one search on
`UNIQUE (transcript_id, line_no)`, no temp b-tree) and the keyset
guarantee are untouched. `start_line=0` yields `-1`, and `line_no > -1`
admits line 0.

*Composition with `cursor`: REFUSED.* Supplying both is a 400
`cannot_determine` under the subject `start_line`. They are two absolute
statements about where the page begins and every silent reconciliation is
worse than refusing: letting `cursor` win discards a position the caller
asked for with no way to tell; letting `start_line` win restarts a paging
walk and repeats rows; treating it as a floor invents a third rule whose
result depends on which number happened to be larger. Open a walk with
`start_line`, continue it with the `next_cursor` you are handed.

*Out of range is a NAMED outcome.* An empty `ok` at `start_line=99999` is
indistinguishable from the end of a transcript. `meta.start_line.state`
names one of six states on every response:

| state | result_status | HTTP | Meaning |
|---|---|---|---|
| `not_requested` | as usual | as usual | no `start_line` was sent |
| `in_range` | `ok` / `partial` | 200 | the page opens at that line |
| `past_last_line` | `not_found` | 404 | measured absence; the reason names the real `MAX(line_no)` |
| `transcript_has_no_lines` | `ok` | 200 | genuinely empty; there is no range to be outside of |
| `negative` | `cannot_determine` | 400 | below 0; not clamped |
| `conflicts_with_cursor` | `cannot_determine` | 400 | both parameters were sent |

The range is MEASURED with `SELECT MAX(line_no) FROM message_appearances
WHERE transcript_id = ?`, not derived from the header's `line_count`:
those agree today (transcript 5767, 30,805 rows, max 30804) and nothing
in the schema forces them to. It is an index seek to the last entry,
measured 0.0008s, and it runs ONLY when `start_line` is supplied. Note
`start_line` is deliberately NOT declared `ge=0` at the route: a FastAPI
bound answers 422 with a validation body that is not an envelope, and
every outcome on this route must be renderable by the same client code.

The three lookup filters are POST-FILTERS applied inside the scope. They are
free here because the scope is already an indexed range. Their counts are
reported as SCANNED, never as a corpus total. See section 7.

**SQL:**

```sql
SELECT a.id, a.line_no, a.seq_in_file, a.line_status, a.serializer_style,
       a.line_byte_length, a.fidelity_outcome, a.is_sidechain, a.agent_id,
       a.body_id,
       b.message_uuid, b.parent_uuid, b.ts, b.origin_session_ref,
       b.is_compact_boundary, b.secret_finding_count,
       LENGTH(b.body_json) AS body_bytes,
       rt.value AS record_type, ro.value AS role, mo.value AS model,
       cs.value AS compact_subtype
  FROM message_appearances a
  LEFT JOIN message_bodies       b  ON b.id  = a.body_id
  LEFT JOIN message_record_types rt ON rt.id = b.record_type_id
  LEFT JOIN message_roles        ro ON ro.id = b.role_id
  LEFT JOIN message_models       mo ON mo.id = b.model_id
  LEFT JOIN message_compact_subtypes cs ON cs.id = b.compact_subtype_id
 WHERE a.transcript_id = :transcript_id
   AND (:cur_line_no IS NULL OR a.line_no > :cur_line_no)
   AND (:role_id IS NULL OR b.role_id = :role_id)
   AND (:record_type_id IS NULL OR b.record_type_id = :record_type_id)
   AND (:model_id IS NULL OR b.model_id = :model_id)
 ORDER BY a.line_no
 LIMIT :limit_plus_one
```

Lookup values are resolved to ids in Python BEFORE this query runs, from
tables of 26, 2 and 13 rows. A filter naming a value that does not exist
is a `cannot_determine` with the reason, NOT an empty `ok`: "there is no
model called `gpt-4`" and "no line in this transcript used that model"
are different findings.

**Measured plan:**

```
SEARCH a USING INDEX sqlite_autoindex_message_appearances_1 (transcript_id=? AND line_no>?)
SEARCH b USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
```

**This is the single best query in the API.** The `UNIQUE (transcript_id,
line_no)` index resolves both the equality and the keyset range in one
search, with NO temp b-tree, so ordering is free and paging cost is
independent of how deep into the transcript the page sits.

| Case | Measured |
|---|---|
| 201 rows, first page, small transcript | 0.0003s |
| 501 rows starting at line 15,000 of the 30,805-line transcript | 0.0016s |
| All 30,805 rows, metadata only | 0.132s |

**Bodies and the byte budget.** With `include_bodies=true`, bodies are
appended in line order until `max_page_bytes` would be exceeded. The page
then STOPS EARLY, returns `result_status: "partial"`, and its
`next_cursor` resumes at the first line not included. Bodies are never
cut; a body that alone exceeds `max_page_bytes` is returned whole if it
is the first on the page, and otherwise deferred to the next page.

A body larger than `MAX_BODY_BYTES` is withheld:

```json
{
  "body_state": "withheld_too_large",
  "body_json": null,
  "body_bytes": 54376859,
  "body_href": "/api/v1/archive/bodies/2396142"
}
```

**Measured: NO body in the corpus currently exceeds `MAX_BODY_BYTES`.**
Max is 54,376,859 against a 67,108,864 cap. This path is therefore
UNREACHABLE with today's data and must be tested against a synthetic
oversized row. See section 10.

**Response:**

```json
{
  "result": [
    {
      "appearance_id": 142,
      "line_no": 1,
      "seq_in_file": null,
      "line_status": "ok",
      "serializer_style": "compact",
      "line_byte_length": 529,
      "fidelity_outcome": "fidelity_verified",
      "is_sidechain": false,
      "agent_id": null,
      "body_id": 88,
      "message_uuid": "95c5a2be-244e-4d75-9a26-61dada6f2ed9",
      "parent_uuid": null,
      "ts": "2025-12-29T06:50:35.600Z",
      "origin_session_ref": "0bd09502-f4be-48f2-ac56-dce81b92d20b",
      "record_type": "user",
      "role": "user",
      "model": null,
      "compact_subtype": null,
      "is_compact_boundary": false,
      "secret_finding_count": 0,
      "body_chars": 509,
      "body_bytes": 509,
      "body_state": "not_requested",
      "body_json": null,
      "body_href": "/api/v1/archive/bodies/88",
      "secrets": null
    },
    {
      "appearance_id": 143,
      "line_no": 2,
      "seq_in_file": null,
      "line_status": "ok",
      "serializer_style": "compact",
      "line_byte_length": 3489,
      "fidelity_outcome": "fidelity_verified",
      "is_sidechain": false,
      "agent_id": null,
      "body_id": 89,
      "message_uuid": "6f175ae8-0c83-446c-9a32-ad8b26934cc7",
      "parent_uuid": "95c5a2be-244e-4d75-9a26-61dada6f2ed9",
      "ts": "2025-12-29T06:50:48.226Z",
      "origin_session_ref": "0bd09502-f4be-48f2-ac56-dce81b92d20b",
      "record_type": "assistant",
      "role": "assistant",
      "model": "claude-sonnet-4-5-20250929",
      "compact_subtype": null,
      "is_compact_boundary": false,
      "secret_finding_count": 0,
      "body_bytes": 3469,
      "body_state": "not_requested",
      "body_json": null,
      "body_href": "/api/v1/archive/bodies/89"
    }
  ],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {
    "paging": {"limit": 100, "returned": 2, "has_more": true,
               "next_cursor": "eyJsaW5lX25vIjoyLCJ2IjoxfQ"},
    "scope": {"kind": "transcript", "transcript_id": 4, "line_count": 980},
    "filters": {"role": null, "record_type": null, "model": null,
                "counts_are": "scanned_within_this_transcript_only"},
    "bodies": {"included": false, "page_bytes": 0,
               "max_page_bytes": 1048576, "stopped_early": false}
  }
}
```

`body_state` permitted values: `not_requested`, `included`,
`withheld_too_large`, `absent`. **`absent` is the `body_id IS NULL` case**
(a blank or invalid-JSON line), and it is distinct from
`withheld_too_large`: one means there is no body, the other means there
is one and it is not in this response. Measured: exactly 1 appearance row
in 3,125,122 has a null `body_id`, and it is the corpus's single
`invalid_json` line. Both states are live and both need a test.

#### 6.7.1 `secrets` on a `/lines` row (NORMATIVE)

**A row that carries a body carries the offsets that mask it.** This did
not used to be true and the gap was a credential-disclosure bug. MEASURED
2026-08-31 on transcript 4, line 32: the row returned
`body_state: "included"` with the whole 5,543-character `body_json` and
`secret_finding_count: 2`, and NO `secrets` array. The offsets existed
only on `GET /bodies/{id}`, so a client had two options - a second
request PER ROW, or render the credential. A bulk-read path that can only
be used safely by not using it in bulk is not a bulk-read path.

`secrets` is now present on every `/lines` row, and it has THREE states,
because "no secrets here" and "nobody looked" are different answers:

| `body_state` | `secrets` | Meaning |
|---|---|---|
| `included` with `secret_finding_count > 0` | array of findings | masking data for this body |
| `included` with `secret_finding_count == 0` | `[]` | MEASURED clean |
| `not_requested`, `absent`, `withheld_too_large` | `null` | NOT EVALUATED - never render this as clean |

The `[]` is a measurement rather than an assumption: `secret_finding_count`
is a denormalized column, and it was checked against the findings table
before being trusted as a skip. MEASURED 2026-08-31 across all 2,447,028
bodies - zero bodies claiming 0 have a findings row, and zero bodies
claiming more than 0 disagree with their real count - so a body claiming
0 is not queried at all. If that ever stops being true the `[]` becomes a
false green, so the check is worth re-running before relying on it.

Each entry is the SAME shape `GET /bodies/{id}` returns, computed by the
same code (`archive_body.secret_findings_for_bodies`). There is one
implementation of the offset arithmetic on purpose: two implementations
of a masking contract diverge, and the divergence is invisible because
the wrong one still returns plausible integers.

```json
{
  "detector": "high_entropy_assignment",
  "match_offset": 2321,
  "match_length": 40,
  "value_sha256": "0236d0f520b4c7373d7c62dd056373304f8cac3b160103c523132587832454f1",
  "match_offset_utf16": 2321,
  "match_length_utf16": 40,
  "utf16_state": "computed"
}
```

**NO MATCHED VALUE IS EVER RETURNED**, on this route or any other. Only
offsets, lengths and a hash. Two entries sharing a `value_sha256` are two
occurrences of ONE credential, which is what the hash is for - both
findings on body 119 above share theirs.

**Mask with the `_utf16` pair, not with `match_offset`** - see 2.2. The
code-point offset and the UTF-16 offset are equal only for a body with no
astral-plane character before the match, and 1,100 of this corpus's
12,390 findings are not in that case. `utf16_state` is `computed` when
the conversion was performed and `cannot_determine` when the body was
withheld, in which case there is no text on the client to mask anyway.

**The array is ordered by `match_offset`**, so a client masking left to
right never has to sort, and one body appearing on several lines of a
page gets the same array on every one of those rows.

**Cost, MEASURED 2026-08-31 on a 500-row page.** The findings for a whole
page are read in ONE query against `ix_message_secret_findings_body`, not
one per row, and only for bodies whose count is non-zero:

| Page | Bodies with findings | Extra rows joined | Without | With | Delta |
|---|---|---|---|---|---|
| transcript 4 (the reported case) | 6 | 10 | 5.17 ms | 5.36 ms | +0.19 ms (+3.7%) |
| transcript 18508 (worst first page in the corpus) | 106 | 433 | 4.92 ms | 6.37 ms | +1.45 ms (+29.4%) |

The percentage looks large and the absolute cost is a millisecond and a
half on a page already carrying 1.4 MB of bodies. **The whole thing is
conditional on `include_bodies=true`**, because a row with no body has
nothing to mask - a default `/lines` page issues no extra query and pays
nothing.

One trap found while measuring, worth recording because it was NOT
theoretical. The UTF-16 conversion originally encoded the whole prefix
once per finding, which is O(findings x body length); on body 2182335
(5,111,955 characters, 205 findings) that measured **723 ms for a single
body**. Because the findings arrive ordered by `match_offset`, the prefix
is now walked ONCE and accumulated, which is O(body length) regardless of
finding count: the same body now measures **3.12 ms**, a 232x
improvement, and `GET /bodies/{id}` got it too. Equivalence against the
old per-finding arithmetic was verified on 1,354 real findings across 400
bodies, 24 of which contain astral-plane characters, with zero
mismatches.

#### 6.7.2 Line numbering and two fields that are not what they look like

* **`line_no` IS 0-BASED.** MEASURED live: the first page of transcript 4
  returns `line_no` `0, 1, 2`. Some examples in this document start at 1
  and are describing the second line, not the first. A client rendering
  "line 1" from `line_no` will be off by one against every other tool
  that counts a file's lines from 1.
* **`seq_in_file` is `null` on live data**, not a sequence number. The
  column exists and is not populated by the current ingest, so it must
  not be used for ordering. `line_no` is the ordering key, it is UNIQUE
  per transcript, and it is what the cursor keys on.
* **`body_chars` and `body_bytes` are THE SAME NUMBER**, and it is a
  character count - `LENGTH(body_json)` counts characters on a TEXT
  value. `body_chars` is the truthful name; `body_bytes` is kept only so
  existing clients do not break, and a client sizing a download from it
  will be under by whatever UTF-8 expansion the body carries. See 2.3.
  `meta.offset_units` states the real unit on every response.

---

### 6.8 GET /archive/bodies/{body_id}

One whole body, plus its secret findings as offsets.

**Params:** `body_id` (int, path).

**SQL:**

```sql
SELECT id, identity_key, message_uuid, body_sha256, body_bytes_sha256,
       parent_uuid, ts, origin_session_ref, is_compact_boundary,
       secret_finding_count, first_seen_at,
       LENGTH(body_json) AS body_bytes, body_json
  FROM message_bodies
 WHERE id = :body_id
```

Plan: `SEARCH message_bodies USING INTEGER PRIMARY KEY (rowid=?)`.
**Measured under 0.001s.**

```sql
SELECT detector, match_offset, match_length, value_sha256
  FROM message_secret_findings
 WHERE body_id = :body_id
 ORDER BY match_offset
```

Plan: `SEARCH message_secret_findings USING INDEX
ix_message_secret_findings_body (body_id=?)` plus a temp b-tree over at
most a handful of rows. **Measured 0.0002s.**

An optional third query, only when `?with_appearances=true`, answers
"where else does this body appear", which is the payoff of the
identity/appearance split:

```sql
SELECT a.transcript_id, a.line_no, a.is_sidechain, a.agent_id,
       t.session_ref, t.host_id
  FROM message_appearances a
  JOIN message_transcripts t ON t.id = a.transcript_id
 WHERE a.body_id = :body_id
 ORDER BY a.transcript_id, a.line_no
 LIMIT 200
```

Plan uses `ix_message_appearances_body (body_id=?)`.

**Response (a real secret-bearing body, values withheld as required):**

```json
{
  "result": {
    "body_id": 119,
    "identity_key": "95c5a2be-244e-4d75-9a26-61dada6f2ed9:cedcbd71c11469e5ccfbc3feb6c196b2846b0f654bcb4bc09f639e9db05da71c",
    "message_uuid": "95c5a2be-244e-4d75-9a26-61dada6f2ed9",
    "body_sha256": "02ea8f38cd105336c9ffc7034ffcf166ec933cff99eef29788c28dcf548232b2",
    "body_bytes_sha256": "cedcbd71c11469e5ccfbc3feb6c196b2846b0f654bcb4bc09f639e9db05da71c",
    "parent_uuid": null,
    "ts": "2025-12-29T06:50:35.600Z",
    "origin_session_ref": "0bd09502-f4be-48f2-ac56-dce81b92d20b",
    "is_compact_boundary": false,
    "first_seen_at": "2026-08-29T22:17:03.142046Z",
    "body_chars": 5543,
    "body_bytes": 5543,
    "body_state": "included",
    "body_json": "{\"parentUuid\":null,\"userType\":\"external\", ... }",
    "secret_finding_count": 2,
    "secrets": [
      {"detector": "high_entropy_assignment", "match_offset": 2321,
       "match_length": 40, "match_offset_utf16": 2321,
       "match_length_utf16": 40, "utf16_state": "computed",
       "value_sha256": "0236d0f520b4c7373d7c62dd056373304f8cac3b160103c523132587832454f1"},
      {"detector": "high_entropy_assignment", "match_offset": 5060,
       "match_length": 40, "match_offset_utf16": 5060,
       "match_length_utf16": 40, "utf16_state": "computed",
       "value_sha256": "0236d0f520b4c7373d7c62dd056373304f8cac3b160103c523132587832454f1"}
    ]
  },
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {
    "secrets_note": "body_json is returned WHOLE and unmodified. match_offset and match_length are UNICODE CODE POINT offsets into body_json, not byte offsets. The client masks; the server never cuts the string.",
    "masking_recipe": "In JavaScript use match_offset_utf16 / match_length_utf16 with String.prototype.slice. Do NOT use match_offset with slice.",
    "appearances_included": false,
    "offset_units": "unicode_code_points",
    "offset_units_utf16_available": true,
    "body_size_units": "unicode_code_points",
    "body_bytes_units": "unicode_code_points",
    "body_bytes_note": "body_bytes is a DEPRECATED alias for body_chars and counts UNICODE CODE POINTS, not bytes. Read body_chars."
  }
}
```

Both findings share one `value_sha256`: two occurrences of one
credential in one body. That is what makes an eventual rotation a clean
cut, and it is why the hash is stored at all.

---

### 6.9 GET /archive/transcripts/{transcript_id}/export (streaming)

Byte-exact reconstruction, streamed. This is the DEFAULT export and the
only one that works on a large transcript.

**Params:** `transcript_id` (int, path). No others. In particular there
is no `?redact=` and there never will be (section 1 rule 4).

**Response:** `200`, `Content-Type: application/x-ndjson`,
`Content-Disposition: attachment; filename="<session_ref>.jsonl"`. The
body is the transcript's exact bytes, not JSON, not an envelope.

**Headers:**

| Header | Value |
|---|---|
| `X-Archive-Transcript-Id` | `4` |
| `X-Archive-Expected-Sha256` | the stored `content_sha256` |
| `X-Archive-Expected-Bytes` | the stored `raw_byte_length` |
| `X-Archive-Verification` | `expected_only` |
| `X-Archive-Trailer-Unavailable` | why no trailer is coming, in prose |

**THERE IS NO TRAILER. THIS SECTION USED TO SAY THERE WAS.** It
documented `X-Archive-Verification: trailer`, a `Trailer:` header
announcing `X-Archive-Actual-Sha256`, and a client that MUST compare it.
None of that is ever sent. uvicorn implements no
`http.response.trailers` ASGI extension, the code checks for that
extension and finds it absent, and the response falls back to declaring
its own limitation. MEASURED live 2026-08-31, the full header set on
`GET /transcripts/4/export`:

```
x-archive-transcript-id: 4
x-archive-expected-sha256: 97e784442e92b8fd770b469310557666c551bd140fc6b4787c1703fd7bf7d242
x-archive-expected-bytes: 3181330
x-archive-verification: expected_only
x-archive-trailer-unavailable: uvicorn implements no http.response.trailers
                               extension; compare the bytes you received
                               against X-Archive-Expected-Sha256 yourself
transfer-encoding: chunked
```

A client written to the old text would wait for a trailer that never
arrives and then have to decide what to do with a file it could not
verify - and the likely decision is to trust it, which is the worst of
the three outcomes.

**A STREAMED DOWNLOAD IS A COULD-NOT-EVALUATE FOR INTEGRITY.** Say it
that way rather than calling it verified or calling it broken. The server
states the hash it EXPECTS before the first byte and never states what it
actually sent, so the server has made a claim and performed no
measurement. Only the client can close that gap, and until it does the
correct status of the file is "unknown", not "good".

**What the user runs to verify it, on the machine that received it:**

```bash
# The value to match is the x-archive-expected-sha256 response header.
shasum -a 256 0bd09502-f4be-48f2-ac56-dce81b92d20b.jsonl
```

To capture the headers and the body in one request, and compare without
reading anything off by eye:

```bash
curl -sS -D headers.txt -o transcript.jsonl \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5055/api/v1/archive/transcripts/4/export
expected=$(awk -F": " "/^x-archive-expected-sha256:/ {print \$2}" headers.txt | tr -d "\r")
actual=$(shasum -a 256 transcript.jsonl | cut -d" " -f1)
[ "$expected" = "$actual" ] && echo "VERIFIED" || echo "MISMATCH: $expected != $actual"
```

`shasum` is the macOS spelling; on Linux it is `sha256sum`. A client that
cannot do this comparison must use 6.10 instead, which verifies before
sending and is bounded to `VERIFY_BEFORE_SEND_MAX_BYTES`.

**6.10 really does send the actual hash**, and that is the difference
between the two endpoints. MEASURED on the same transcript:
`x-archive-verification: before_send`, `x-archive-actual-sha256:
97e7844...` equal to the expected value, `x-archive-actual-bytes:
3181330`, `x-archive-verified: true`, and a real `content-length` instead
of `transfer-encoding: chunked`. Those headers are on the LEADING
response because the whole file was hashed before any of it was sent.

#### 6.9.1 Concurrency limit (NORMATIVE, and it can 503)

Streaming exports are capped process-wide. This is not documented
anywhere else and a client that does not know about it will write the
refusal to disk as though it were a transcript.

| Constant | Value | Meaning |
|---|---|---|
| `MAX_CONCURRENT_EXPORTS` | `2` | streaming exports in flight in this process at once |
| `EXPORT_SLOT_WAIT_SECONDS` | `30.0` | how long a request waits for a slot before being refused |

The bound is sized against a MEASURED ~600 MB peak RSS for the worst
transcript in the corpus - a 37 MB line inside a 244 MB file - so two
concurrent worst-case exports is about 1.2 GB and three would be 1.8 GB
on a machine also running the app. A request that waits
`EXPORT_SLOT_WAIT_SECONDS` without a slot coming free is REFUSED rather
than queued forever, because a client that hangs indefinitely cannot tell
a slow export from a wedged one.

**The refusal is HTTP 503 carrying a `cannot_determine` envelope**, with
`meta.limit.max_concurrent_exports` naming the bound:

```json
{
  "result": null,
  "result_status": "cannot_determine",
  "scope_status": "resolved",
  "unevaluated": [
    {
      "subject": "transcript:4",
      "reason": "2 streaming exports are already in flight and no slot came free within 30.0s. The bound exists because one worst-case export was measured at about 600 MB peak RSS. Retry."
    }
  ],
  "meta": {"limit": {"max_concurrent_exports": 2}}
}
```

**A CLIENT MUST PREFLIGHT, OR IT WILL SAVE THIS JSON AS THE TRANSCRIPT.**
The 503 is a normal response body on the same URL that otherwise streams
a file, and the request already carries a
`Content-Disposition: attachment; filename="<session_ref>.jsonl"` on the
success path. A browser download, a `curl -o`, or any client that writes
the body to the filename without checking the status will end up with a
few hundred bytes of JSON in a file named like a transcript - and it will
look like a truncated export rather than a refusal, which is the failure
that costs an hour. Check the status code BEFORE opening the output file:
only `200` is a transcript, `503` is "retry later", `404` is a real
absence, and `200` with `Content-Type: application/json` never happens on
this route.

Note the limit applies to 6.9 only. 6.10 buffers and verifies before
sending, and is bounded by `VERIFY_BEFORE_SEND_MAX_BYTES` instead.

**SQL:**

```sql
SELECT a.line_no, a.raw_line, a.serializer_style, a.envelope_json,
       a.key_order_json, a.line_sha256, b.body_json
  FROM message_appearances a
  LEFT JOIN message_bodies b ON b.id = a.body_id
 WHERE a.transcript_id = :transcript_id
 ORDER BY a.line_no
```

This is EXACTLY the query `export_transcript` already runs. Plan:
`SEARCH a USING INDEX sqlite_autoindex_message_appearances_1
(transcript_id=?)` plus a primary key LEFT JOIN. No temp b-tree.

**The mandatory refactor.** `export_transcript` calls `.fetchall()` and
accumulates every `LineExport` (each holding its own full `text`), plus a
separate `texts` list, plus the joined result. That is three retained
copies plus Python string overhead. Measured on 2026-08-31:

| Transcript | Bytes | `export_transcript` peak RSS | Ratio |
|---|---|---|---|
| 5767 | 91,950,363 | 1,139 MB | 12.4x |
| 10902 | 182,077,926 | 2,205 MB | 12.1x |

A prototype `iter_export_lines` using `fetchmany(256)` and yielding,
measured on the same 182 MB transcript in the same process:

| Approach | Peak RSS | Time | sha256 correct | Byte count correct |
|---|---|---|---|---|
| `export_transcript` | 2,205 MB | 2.38s | yes | yes |
| `iter_export_lines` streaming | **78 MB** | 1.79s | yes | yes |

**28x less memory, and faster.** The streamed output reproduced
`content_sha256` `218e15dc1d09cf2813a28e88...` exactly and emitted
exactly 182,077,926 bytes. The largest transcript in the corpus is
244,117,661 bytes, which `export_transcript` would peak at roughly 2.9 GB
(PREDICTION, by the measured 12.1x ratio); three concurrent exports would
exhaust most machines. Streaming is not an optimization here, it is what
makes the endpoint safe to expose.

```python
def iter_export_lines(
    conn: sqlite3.Connection, transcript_id: int,
) -> Iterator[LineExport]:
    """Yield one verified LineExport per line, in line order.

    Description: the SINGLE rendering path. ``export_transcript`` is
      refactored to consume this, so a streamed export and a verified
      export can never disagree about a line's bytes - there is one
      implementation, not two that happen to match today. Uses
      fetchmany() so peak memory is a batch, not a transcript: measured
      78 MB vs 2,205 MB on the 182 MB transcript.
    Inputs: conn (sqlite3.Connection), transcript_id (int).
    Output: Iterator[LineExport], ascending line_no.
    Raises: nothing. A line that cannot be rendered is yielded with
      outcome VERIFY_CANNOT_RENDER; the CALLER decides whether that is
      fatal, which is what lets strict and non-strict share one path.
    Example: sum(1 for _ in iter_export_lines(conn, 4)) -> 980
    """
```

`export_transcript` keeps its exact current signature and semantics and
becomes a consumer of it. `test_message_model_export.py` must assert the
joined iterator output equals `export_transcript(...).text` byte for
byte on a real transcript.

**Streaming and the three-outcome rule.** A line whose outcome is
`VERIFY_CANNOT_RENDER` cannot be silently skipped: a shorter file would
fail the whole-file hash for a reason nobody could read off the failure.
The stream ABORTS at that line and the partial body is already on the
wire. There is no trailer to report it in (see above), so the consumer's
only signal is that the received byte count does not match
`X-Archive-Expected-Bytes` - which is another reason the client-side
comparison is mandatory rather than advisory. A consumer that does not
compare will keep a truncated file and have nothing telling it so. That is
precisely why 6.10 exists, and why `verified_available` is advertised on
the transcript header. Measured: 3,125,122 of 3,125,122 appearance rows
are `fidelity_verified`, so this path has never fired on real data.

---

### 6.10 GET /archive/transcripts/{transcript_id}/export/verified

Same bytes, buffered, verified BEFORE the first byte is sent.

**Params:** `transcript_id` (int, path).

**Behaviour:** reconstruct the whole transcript in memory, compare the
computed sha256 against the stored `content_sha256`, and only then
respond. On mismatch, respond `200` with a `cannot_determine` ENVELOPE
and no file body at all.

**The refusal.** If `raw_byte_length > VERIFY_BEFORE_SEND_MAX_BYTES`
(8,388,608), this route refuses with `413` and a `cannot_determine`
envelope pointing at 6.9. It does not silently fall back to streaming,
because the caller asked for a guarantee this route cannot provide at
that size.

```json
{
  "result": null,
  "result_status": "cannot_determine",
  "scope_status": "resolved",
  "unevaluated": [
    {
      "subject": "transcript:10902",
      "reason": "raw_byte_length 182077926 exceeds VERIFY_BEFORE_SEND_MAX_BYTES 8388608; buffering it would peak near 2.2 GB. Use /api/v1/archive/transcripts/10902/export and check the sha256 trailer."
    }
  ],
  "meta": {"stream_href": "/api/v1/archive/transcripts/10902/export"}
}
```

**When each export is correct:**

| | 6.9 streaming | 6.10 verify-before-send |
|---|---|---|
| Memory | ~78 MB regardless of size (measured) | ~12x the transcript (measured) |
| Size limit | none | 8 MiB, refuses above |
| Applies to | all 21,039 transcripts | 20,816 of 21,039 (98.9 percent) |
| Bad bytes reach the client | YES, then the trailer says so | NO |
| Client must | read and check the trailer | check the status code |
| Use it for | anything large, anything piped to a file, any bulk export | a browser download, an automated restore, any consumer that cannot check a trailer |

**The tradeoff in one sentence.** Streaming cannot tell you the file is
good until after it has handed you the file; verify-before-send cannot
handle a file that does not fit in memory. There is no third option that
is both, so the API offers both and states which is which rather than
picking a default that is wrong half the time.

`Content-Type`, `Content-Disposition` and the `X-Archive-Expected-*`
headers match 6.9. `X-Archive-Verification` is `before_send`, and
`X-Archive-Actual-Sha256` is a real HEADER here, not a trailer, because
it is known before the response starts.

---

### 6.11 GET /archive/search

**Scoped. Always. There is no unscoped form and adding one is a
regression.**

**Params:**

| Param | Type | Default | Bounds |
|---|---|---|---|
| `q` | str, query | required | 2 to 200 chars, non-blank |
| `project_id` | int, query | one of | mutually exclusive with `transcript_id` |
| `transcript_id` | int, query | one of | mutually exclusive with `project_id` |
| `limit` | int, query | 50 | 1 to 200 |
| `cursor` | str, query | none | opaque, carries scan position |
| `scan_budget` | int, query | 2000 | 1 to `MAX_SCAN_BUDGET` (2000), transcripts |
| `scan_bytes` | int, query | 536870912 | 1048576 to `MAX_SCAN_BYTES`, bytes |
| `case_sensitive` | bool, query | `false` | |
| `snippets` | bool, query | `true` | `false` withholds EVERY preview - the only hard no-disclosure guarantee (section 1 rule 5) |

Exactly one of `project_id` / `transcript_id` is required. Neither, or
both, is a `400 cannot_determine`. A global search is not offered, is not
a missing feature, and must not be added: see section 7.

**SQL, per transcript in the scan:**

```sql
SELECT a.line_no, a.body_id, b.secret_finding_count,
       LENGTH(b.body_json) AS body_bytes,
       INSTR(LOWER(b.body_json), LOWER(:q)) - 1 AS match_offset
  FROM message_appearances a
  JOIN message_bodies b ON b.id = a.body_id
 WHERE a.transcript_id = :transcript_id
   AND INSTR(LOWER(b.body_json), LOWER(:q)) > 0
 ORDER BY a.line_no
 LIMIT :remaining_plus_one
```

`INSTR` rather than `LIKE '%x%'` because it yields the offset in the same
pass, and the offset is required for the snippet and for a client-side
highlight. With `case_sensitive=true` the `LOWER()` calls are dropped,
which is measurably faster and is the reason the parameter exists.

**Measured plan:**

```
SEARCH a USING INDEX sqlite_autoindex_message_appearances_1 (transcript_id=?)
SEARCH b USING INTEGER PRIMARY KEY (rowid=?)
```

The scope is an indexed range; the text test is a post-filter inside it.
That is the whole design: the index bounds the work, the scan does the
matching.

**The scan loop.** Transcripts in the scope are visited in
`(ingested_at DESC, id DESC)` order, the same order as 6.4, so a search
result and a transcript list agree about what "next" means. For each
transcript the loop adds `raw_byte_length` to a byte counter and 1 to a
transcript counter, and STOPS when either budget is spent or `limit` hits
are collected.

**Measured cost, 2026-08-31.** Scanning is linear in bytes at a strikingly
stable rate. Three projects not touched by any earlier query in the
session, 400 transcripts each, searching for a term that appears nowhere
(the worst case, since no early exit is possible):

| Project | Transcripts | Bytes | Time | Rate |
|---|---|---|---|---|
| 30 | 400 | 215,068,454 | 0.50s | 0.432 GB/s |
| 9 | 400 | 281,231,037 | 0.63s | 0.448 GB/s |
| 38 | 400 | 168,549,095 | 0.38s | 0.445 GB/s |

Per-transcript cost tracks size, not count:

| Transcript | Lines | Bytes | Time |
|---|---|---|---|
| 17956 | 20,931 | 32,224,301 | 68.8 ms |
| 5767 | 30,805 | 91,950,363 | 210.8 ms |
| 10902 | 29,322 | 182,077,926 | 405.6 ms |

**This is why `MAX_SCAN_BUDGET=2000` alone is the wrong governor**, and
it is the most important correction in this document. 2,000 transcripts
of project 1 measured 0.7s (PREDICTION from a 143-transcript sample at
0.4 ms each). 2,000 transcripts the size of 10902 would be about 811
seconds (PREDICTION, 2000 x 405.6 ms). A count budget admits a
three-orders-of-magnitude spread. `MAX_SCAN_BYTES` is the primary
governor; the transcript count is kept as a secondary cap so a scope of
many tiny transcripts cannot spend forever on per-query overhead.

Measured whole-scope worst case: project 12, the largest in the corpus at
3,416 transcripts and 2,211,811,751 bytes, absent term, first 2,000
transcripts: **2.65s**, and 4.5s for all 3,416 (PREDICTION by the
measured per-transcript rate). At 512 MiB the byte budget stops that scan
at about 1.2s.

**Two zero-hit responses, structurally distinguishable.** This is the
requirement that shapes the whole `meta.scan` block.

Complete and found nothing:

```json
{
  "result": [],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {
    "query": {"q": "restic", "case_sensitive": false},
    "scope": {"kind": "project", "project_id": 1, "transcripts_in_scope": 143},
    "scan": {
      "status": "complete",
      "transcripts_scanned": 143,
      "transcripts_not_scanned": 0,
      "bytes_scanned": 41229887,
      "budget_transcripts": 2000,
      "budget_bytes": 536870912,
      "elapsed_seconds": 0.054,
      "resume_cursor": null
    },
    "paging": {"limit": 50, "returned": 0, "has_more": false, "next_cursor": null},
    "snippet_gate": {
      "guarantee": "best_effort",
      "layers": [
        "body_secret_finding_count",
        "detectors_over_window",
        "known_credential_value_hash"
      ],
      "known_values_indexed": 739,
      "limitation": "A credential never detected anywhere in this corpus is not in the known-value index and is invisible to the detectors for the same reason it was never detected. Pass snippets=false for a response that carries no preview text at all.",
      "withholding_never_suppresses_a_hit": true
    }
  }
}
```

**`scan.bytes_scanned` IS A CHARGE, NOT BYTES EXAMINED. It must never
drive a progress bar or a throughput display.** A transcript's FULL
`raw_byte_length` is charged against the budget the moment the scan
ENTERS it, before a single line is read, because the budget exists to
bound worst-case work and a bound you only apply after doing the work is
not a bound. Two consequences fall out, and both look like bugs if you
have not been told:

* **It can exceed the budget.** MEASURED 2026-08-31, scoped to
  transcript 4 with `scan_bytes=1048576`: `bytes_scanned` came back
  `3,181,330` against `budget_bytes` `1,048,576`. The transcript is
  3.18 MB, entering it charged all of it, and the overshoot is by
  construction. The budget is a stopping rule, not a ceiling on the
  reported number.
* **It says nothing about elapsed time.** The same request reported
  `status: limit_reached` in `elapsed_seconds: 0.002637`, having stopped
  as soon as the page was full. Dividing one by the other implies
  1.2 GB/s, which is not a measurement of anything - the scan did not
  examine 3.18 MB, it was CHARGED for 3.18 MB and then stopped early.
  On a `limit_reached` result the figure can be arbitrarily large in an
  arbitrarily small time.

So: use `bytes_scanned` to reason about BUDGET CONSUMPTION and to decide
whether to resume from `resume_cursor`. Do not render it as progress, do
not divide it by `elapsed_seconds`, and do not show a user a byte count
implying work that was not done. `SCAN_BYTES_PER_SECOND` exists to render
a PREDICTED cost before a scan and is never used to decide anything.

Note the interaction with `status`. On `complete` and `budget_exhausted`
the charge is a fair proxy for work, because every entered transcript was
read to the end or the budget genuinely ran out. On `limit_reached` it is
not, because the scan stopped mid-transcript with the page full. Read
`status` before you read the number.

Ran out of budget and found nothing SO FAR:

```json
{
  "result": [],
  "result_status": "partial",
  "scope_status": "resolved",
  "unevaluated": [
    {
      "subject": "project:12",
      "reason": "1416 of 3416 transcripts were not scanned: byte budget 536870912 was spent after 2000 transcripts"
    }
  ],
  "meta": {
    "query": {"q": "restic", "case_sensitive": false},
    "scope": {"kind": "project", "project_id": 12, "transcripts_in_scope": 3416},
    "scan": {
      "status": "budget_exhausted",
      "transcripts_scanned": 2000,
      "transcripts_not_scanned": 1416,
      "bytes_scanned": 536870912,
      "budget_transcripts": 2000,
      "budget_bytes": 536870912,
      "elapsed_seconds": 1.21,
      "resume_cursor": "eyJieXRlcyI6NTM2ODcwOTEyLCJsaW5lX25vIjotMSwic2Nhbm5lZCI6MjAwMCwidF9pZCI6OTAzLCJ0X2luZ2VzdGVkX2F0IjoiMjAyNi0wOC0yOVQyMjoxNzoxNS44MjU4MzFaIiwidiI6MX0"
    },
    "paging": {"limit": 50, "returned": 0, "has_more": null, "next_cursor": null}
  }
}
```

`result_status` differs (`ok` vs `partial`), `scan.status` differs
(`complete` vs `budget_exhausted`), `transcripts_not_scanned` differs (0
vs 1416), `unevaluated` differs (empty vs populated), and
`resume_cursor` differs (null vs a cursor). **Five independent
discriminators.** A client cannot accidentally render "no results" for
the second case, and `test_archive_search_zero_hits.py` asserts all five.

**A hit, including the withheld-snippet case:**

```json
{
  "result": [
    {
      "transcript_id": 5767,
      "session_ref": "0bd09502-f4be-48f2-ac56-dce81b92d20b",
      "line_no": 1695,
      "body_id": 375769,
      "match_offset": 1348,
      "match_length": 6,
      "body_bytes": 4211,
      "secret_finding_count": 0,
      "snippet": "...checked against the restic REST server at 10.0.10.80...",
      "snippet_state": "included",
      "body_href": "/api/v1/archive/bodies/375769",
      "lines_href": "/api/v1/archive/transcripts/5767/lines?cursor=eyJsaW5lX25vIjoxNjk0LCJ2IjoxfQ"
    },
    {
      "transcript_id": 5767,
      "session_ref": "0bd09502-f4be-48f2-ac56-dce81b92d20b",
      "line_no": 2240,
      "body_id": 376269,
      "match_offset": 3556,
      "match_length": 6,
      "body_bytes": 9902,
      "secret_finding_count": 2,
      "snippet": null,
      "snippet_state": "withheld_secret_bearing",
      "body_href": "/api/v1/archive/bodies/376269",
      "lines_href": "/api/v1/archive/transcripts/5767/lines?cursor=eyJsaW5lX25vIjoyMjM5LCJ2IjoxfQ"
    }
  ],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": { "...": "as above" }
}
```

**The second hit is still reported.** Its transcript, line, offset and
length are all present; only the snippet is withheld. An operator
looking for where a credential was mentioned gets the location, follows
`body_href`, and masks client-side using the offsets from 6.8. Dropping
the hit would make secret-bearing material the hardest thing in the
corpus to find, which is backwards.

**Snippet construction.** A window of `match_offset - 60` to
`match_offset + match_length + 60`, clamped to the body, taken from
`body_json` and prefixed/suffixed with `...` when clamped. **A snippet is
NOT a body and is not covered by section 1 rule 3**, which is about
returning a body: a snippet is a separate, explicitly-labelled field, it
is never placed in `body_json`, and `body_state` is never `included` on a
search hit. `test_archive_no_body_prefix.py` asserts that no `body_json`
field anywhere in any response is a prefix of a real body, by EQUALITY
against the stored value, never by `startswith`.

---

### 6.12 GET /archive/transcripts/{transcript_id}/subagents

Subagent lineage, scoped. `subagent_edges()` today is unscoped and
returns 1,627,995 rows, which is not a response, it is an outage.

**Params:** `transcript_id` (int, path), `limit` (1 to 200, default 100),
`cursor` (opaque).

**SQL:**

```sql
SELECT a.id, a.line_no, a.agent_id, a.is_sidechain,
       t.session_ref AS transcript_session_ref,
       b.origin_session_ref, b.message_uuid
  FROM message_appearances a
  JOIN message_transcripts t ON t.id = a.transcript_id
  LEFT JOIN message_bodies b ON b.id = a.body_id
 WHERE a.transcript_id = :transcript_id
   AND (a.agent_id IS NOT NULL OR a.is_sidechain = 1)
   AND (:cur_id IS NULL OR a.id > :cur_id)
 ORDER BY a.id
 LIMIT :limit_plus_one
```

**Measured plan:**

```
SEARCH t USING INTEGER PRIMARY KEY (rowid=?)
SEARCH a USING INDEX sqlite_autoindex_message_appearances_1 (transcript_id=?)
SEARCH b USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
USE TEMP B-TREE FOR ORDER BY
```

**Measured 0.058s** on transcript 17956, which has 20,931 subagent
appearances, the corpus maximum. The temp b-tree is over that transcript's
rows only.

**The signature change:**

```python
def subagent_edges(
    conn: sqlite3.Connection, transcript_id: Optional[int] = None,
) -> List[Dict[str, object]]:
```

Default `None` preserves the existing unscoped behaviour for
`verify_all`-style callers, so this is additive. **No API route ever
calls it with `None`.** `test_archive_subagents.py` asserts that the
route rejects an absent `transcript_id` rather than falling through to
the 1.6 million row form.

**Response:**

```json
{
  "result": [
    {
      "appearance_id": 2764357,
      "line_no": 0,
      "agent_id": "a78b89f",
      "is_sidechain": true,
      "transcript_session_ref": "agent-a78b89f",
      "origin_session_ref": "942e13bc-e96c-46a7-b323-d6c18bef02b3",
      "message_uuid": "eeaff413-d0ce-492e-82a1-505e2186a823",
      "body_href": "/api/v1/archive/bodies/1174433"
    }
  ],
  "result_status": "ok",
  "scope_status": "resolved",
  "unevaluated": [],
  "meta": {
    "paging": {"limit": 100, "returned": 1, "has_more": true,
               "next_cursor": "eyJhcHBlYXJhbmNlX2lkIjoyNzY0MzU3LCJ2IjoxfQ"},
    "scope": {"kind": "transcript", "transcript_id": 17956},
    "lineage": {
      "transcript_session_ref": "agent-a78b89f",
      "session_ref_scheme": "agent",
      "distinct_agent_ids": 1,
      "distinct_origin_session_refs": 1,
      "parent_transcripts": [
        {"transcript_id": 17950, "session_ref": "942e13bc-e96c-46a7-b323-d6c18bef02b3",
         "href": "/api/v1/archive/transcripts/17950"}
      ]
    }
  }
}
```

`meta.lineage.parent_transcripts` resolves `origin_session_ref` back to
real transcripts:

```sql
SELECT id, session_ref, host_id FROM message_transcripts
 WHERE session_ref = :origin_session_ref
```

Plan: `SEARCH message_transcripts USING INDEX
ix_message_transcripts_session (session_ref=?)`. Indexed and cheap.

Note this can legitimately return MORE than one transcript for one
session_ref: the same session copied between the owner's two machines is
two transcript rows with one `session_ref`, which is what
`message_session_hosts` exists to count. That is not a collision and must
not be reported as one. It is returned as a LIST for exactly that reason.

---

## 7. Excluded from v1, with reasons

Each of these is a deliberate refusal, not a backlog item someone forgot.
Shipping any of them without re-doing the measurement is a regression.

### 7.1 Global full-text search (FTS5 over `body_json`)

Two independent reasons, either sufficient.

**Size.** Measured 2026-08-31: `AVG(LENGTH(body_json))` is 3,172 bytes
across 2,447,028 rows, so the stored body text is about 7.76 GB. An FTS5
index over raw `body_json` roughly doubles that. The prior pass's
estimate of about 16.4 GB total and about 31 minutes to build is
CONSISTENT with my measured average but I did not build it, so the build
time is CANNOT DETERMINE and the size is a PREDICTION.

**Secrets.** An FTS index over raw `body_json` becomes a SECOND
uncontrolled copy of every secret-bearing body, in a table with no
`secret_finding_count` column, no offsets, and no relationship to
`message_secret_findings`. Every control in section 2 is defined on
`message_bodies`; none of them reach an FTS shadow table. That alone
rules it out.

**If it ever ships, it indexes an extracted TEXT PROJECTION, not raw
`body_json`.** The projection is built from the message content fields
only, with secret-bearing bodies excluded or their matched ranges
removed BEFORE indexing, so the index cannot become a bypass around
`withheld_secret_bearing`.

### 7.2 Unscoped search

`body_json LIKE '%x%'` across the corpus is a full scan of 7.76 GB. At
the measured 0.44 GB/s that is about 17.6 seconds (PREDICTION from a
measured rate), on a shared event loop, per request, with no upper bound
on concurrency. A single impatient user with a reload key is a
self-inflicted denial of service. Scoped search with a byte budget gives
the same answers in bounded time.

### 7.3 A global message feed (`ORDER BY ts DESC`)

**Measured 2026-08-31: 4.402s**, plan `SCAN message_bodies` plus `USE
TEMP B-TREE FOR ORDER BY`. (The prior pass recorded 3.4s; mine is the
re-measurement.) This is the one feature the sanctioned
`message_bodies(ts)` index would fix.

It ships as an explicit `cannot_determine`, never as a 4.4s scan:

```json
{
  "result": null,
  "result_status": "cannot_determine",
  "scope_status": "cannot_determine",
  "unevaluated": [
    {
      "subject": "global_message_feed",
      "reason": "requires an index on message_bodies(ts), which is not present. Ordering 2,447,028 bodies by ts without it is a measured 4.402s full scan and will not be served. Scope the query to a project or transcript instead."
    }
  ],
  "meta": {"required_index": "CREATE INDEX ix_message_bodies_ts ON message_bodies (ts)"}
}
```

A route that returns a named refusal is honest. A route that quietly
takes 4.4 seconds teaches users the app is slow and teaches operators
nothing.

### 7.4 Role / record_type / model filters OUTSIDE a scope

Measured: `COUNT(*) WHERE role_id = 1` is a 1.6s full scan (prior pass;
my re-measurements of comparable full scans on this table ranged 1.9s to
4.9s, so treat 1.6s as a floor). There is no index on `role_id`,
`record_type_id` or `model_id` and none is sanctioned.

INSIDE a scope they are free, because the scope is already an indexed
range and the filter is a post-filter on rows already being read. They
are offered on 6.7 for exactly that reason, and their result counts are
labelled `counts_are: "scanned_within_this_transcript_only"`. **A count
from a scoped post-filter is not a corpus total and must never be
rendered as one.**

### 7.5 Aggregate and analytics endpoints

"Messages per model", "busiest project by volume", "secrets by detector
over time" are all full scans. Measured examples: `MAX(LENGTH(body_json))`
27.4s, `COUNT(*) WHERE secret_finding_count > 0` 2.21s,
`GROUP BY line_status` 1.92s.

These belong in a materialized summary table written at INGEST, when the
rows are already in hand, and read back in microseconds. They do not
belong in a request handler. Building that summary is a separate piece of
work with its own three-outcome story about staleness.

### 7.6 Any write, delete, or re-ingest path

The archive is append-only and this API is read-only. `POST
/corpus/ingest` (`corpus_routes.py`) remains the ONE write path in the
subsystem, and it is not extended here. Every connection this API opens
carries `PRAGMA query_only=ON`, verified in section 8.

### 7.7 `raw_line` exposure

Measured: 1 non-null row in 3,125,122. It exists only where re-rendering
FAILED at ingest, so it is diagnostic evidence about a defect, not
content. It is used internally by the export path (section 1 rule 2) and
is not returned by any route. `/lines` reports
`lines_with_raw_line` as a COUNT so its presence is visible without its
bytes being served.

### 7.8 A secret-browse endpoint

"Show me every secret in the corpus" is a 2.21s scan (measured) and,
more importantly, it is an index OF the credential material, sorted for
convenience. The per-body findings on 6.8 give an operator everything
needed to act on a body already in hand. A corpus-wide enumeration is a
different tool with a different risk profile and it is not this one.

---

## 8. File plan

### 8.1 New files

The four-file plan below was the ESTIMATE. What shipped is thirteen
modules, because the 500-line cap bit repeatedly and each split was taken
along a real seam rather than by moving whichever function was nearest
the bottom. Actual line counts MEASURED 2026-08-31:

| File | Purpose | Lines |
|---|---|---|
| `src/api/archive_routes.py` | The hierarchy, line and body routes. Thin: parse, delegate, wrap. No SQL. | 380 |
| `src/api/archive_search_routes.py` | The search route. | 185 |
| `src/api/archive_export_routes.py` | The two export routes, the stream response and the concurrency limiter. | 497 |
| `src/api/archive_support.py` | `respond()`, `is_client_error()`, `state_dir()`. ONE status rule across all three route modules. | 101 |
| `src/core/archive_read.py` | `open_read_only()`, `run_read()`, tuning constants, query helpers. | 275 |
| `src/core/archive_envelope.py` | `envelope()` and the status vocabularies. The three-outcome contract. | 266 |
| `src/core/archive_hierarchy.py` | Hosts, corpora, projects, transcript pages. | 496 |
| `src/core/archive_lines.py` | Transcript header and the line page query. | 369 |
| `src/core/archive_line_rows.py` | Shaping one line row, attaching bodies and their secret offsets. | 187 |
| `src/core/archive_body.py` | Whole-body reads and the secret-finding offsets. | 393 |
| `src/core/archive_search.py` | The budgeted scan loop. | 450 |
| `src/core/archive_snippet_gate.py` | Snippet withholding decisions. | 291 |
| `src/core/archive_subagents.py` | Subagent lineage. | 213 |
| `src/core/archive_cursor.py` | `encode_cursor`, `decode_cursor`, `CursorError`. | 284 |
| `src/core/archive_units.py` | ONE definition of `offset_units_meta()`. | 84 |

**The cap is load-bearing in this change and it is a REVIEW rule, not an
enforced one.** There is no linter and no CI step that measures file
length anywhere in this repo, so nothing catches a regression
automatically. `CLAUDE.md` states it as a "500-line guideline" and names
seven PRODUCTION files already past it that "should not grow"; MEASURED
2026-08-31, 26 of 172 files under `src/` exceed it, up to 5,918 lines.
The rule that is actually followed is the sentence next to that list:
NEW logic goes in NEW focused modules.

**Does it apply to tests?** Not in practice, and no convention in this
repo says it does. MEASURED 2026-08-31: 15 of 223 test files exceed 500
lines, the largest at 2,125, and no test file appears in `CLAUDE.md`'s
do-not-grow list. `tests/test_archive_read_api.py` is 677 lines and sits
mid-pack among them. It is left alone deliberately: splitting a cohesive
test file to satisfy a cap nothing enforces, on files nobody has held to
it, is churn that makes the suite harder to navigate for no measured
benefit. New archive test files are kept focused instead
(`test_archive_lines_secrets.py`, `test_archive_result_shape.py`).

```python
def open_read_only(state_dir: Path) -> sqlite3.Connection:
    """Open cloude.db for reading and REFUSE writes on this connection.

    Description: connect(create=False) so a typo'd state directory
      raises instead of manufacturing an empty database that renders as
      a healthy install with no data, then PRAGMA query_only=ON so a bug
      in a read path cannot mutate an 11 GB archive. VERIFIED on the
      live corpus 2026-08-31: with query_only=ON a SELECT returns rows
      and an INSERT raises OperationalError "attempt to write a readonly
      database" on the SAME connection.
    Inputs: state_dir (Path).
    Output: sqlite3.Connection, row_factory sqlite3.Row.
    Raises: DatastoreUnreadableError - missing or unopenable.
    Example: with closing(open_read_only(sd)) as c: c.execute("SELECT 1")
    """
```

**`mode=ro` in the URI is NOT used, and that is deliberate.** These are
WAL databases. A `mode=ro` open fails when no `-shm` sidecar exists,
because a read-only connection cannot create the shared-memory index a
WAL reader needs. `PRAGMA query_only=ON` on a normal read-write
connection forbids every content write while still permitting the `-shm`
a legitimate reader requires. This is the same trap recorded as fleet
hazard 63 and refined by hazard 65(b).

### 8.2 Changed files

| File | Change | Risk |
|---|---|---|
| `src/core/message_model_export.py` | ADD `iter_export_lines(conn, transcript_id) -> Iterator[LineExport]`. REFACTOR `export_transcript` to consume it. ADD `transcript_id: Optional[int] = None` to `subagent_edges`. | Behaviour-preserving. `export_transcript` keeps its signature, its dataclasses and its `strict=True` raise. Guarded by a byte-for-byte equality test. |
| `src/main.py` | ONE import beside line 51, ONE `include_router` beside line 700, matching the existing `corpus_router` lines exactly. | Trivial. |
| `src/core/db.py` | OPTIONAL `read_only: bool = False` kwarg on `connect()`, appending `PRAGMA query_only=ON`. | Additive, default off. If it is not added, `open_read_only` applies the pragma itself, which is what the measured verification above actually did. Either is acceptable; do not do both. |

No other file changes. Nothing lands in `src/api/routes.py`, which is
already 134 KB.

---

## 9. Test plan

Python: `python3 -m pytest -q`. JS: standalone `node tests/*.node.mjs`.
Existing conventions in `tests/` are followed (`test_corpus_api.py`,
`test_message_model_export.py` are the nearest neighbours).

### 9.1 Required Python test files

| File | What it asserts |
|---|---|
| `tests/test_archive_read_only.py` | **The read-only connection with a POSITIVE CONTROL.** On ONE connection from `open_read_only()`: a `SELECT` returns real rows (positive control, proving the connection works at all), and an `INSERT` raises `sqlite3.OperationalError`. A test that only asserts the refusal cannot tell a read-only connection from a broken one. Also asserts the connection survives on a WAL database with no `-shm` present, which is what `mode=ro` would fail. |
| `tests/test_archive_envelope.py` | **Empty vs unopenable is the same test file's point.** A route against a real but empty scope returns `("ok", [])`; the SAME route against an unopenable state dir returns `("cannot_determine", null)` with a populated `unevaluated`. The two responses are asserted to be structurally different, so a client can never conflate them. Also asserts `envelope()` raises on an unknown status string, and that every key in section 3 is present on every outcome. |
| `tests/test_archive_cursor.py` | Round-trip encode/decode. **A malformed cursor is a 400 `cannot_determine`, NEVER a reset to page 1** - asserted for bad base64, bad JSON, wrong `kind`, wrong `v`, missing key, wrong type, and empty string, each as a separate case. Also asserts the ISO-8601 uniformity invariant against the live schema (fixed width, `Z` suffix) so a future ingest writing a local-time value fails here rather than corrupting paging silently. |
| `tests/test_archive_keyset.py` | **Walks every row exactly once across a tie.** Builds a fixture where several rows share one `ingested_at`, pages the whole list at `limit=3`, and asserts the concatenated ids equal the full ordered id list with no duplicate and no gap. Repeated at `limit=1` and at `limit=n` and `limit=n+1` around the exact boundary, because `has_more` off-by-one is the classic bug. Asserts `has_more` is `null` and not `false` on `not_found`. |
| `tests/test_archive_search_zero_hits.py` | **Two zero-hit searches must differ structurally.** Asserts all five discriminators from 6.11: `result_status` (`ok` vs `partial`), `scan.status` (`complete` vs `budget_exhausted`), `transcripts_not_scanned` (0 vs >0), `unevaluated` (empty vs populated), `resume_cursor` (null vs a cursor). Also asserts resuming from the cursor visits the remaining transcripts and no earlier one. |
| `tests/test_archive_search_secrets.py` | A hit on a body with `secret_finding_count > 0` has `snippet: null` and `snippet_state: "withheld_secret_bearing"` AND IS STILL PRESENT in `result` with its transcript, line, offset and length. Asserts no matched value appears anywhere in the serialized response, and that nothing was written to the captured log records. |
| `tests/test_archive_search_snippet_gate.py` | **A body that CONTAINS a credential but carries ZERO findings must not have its snippet served.** The fixture puts one credential in two bodies - one in an assignment (detected, flagged) and one bare in prose (not detected, not flagged), the exact split measured on the live corpus - so a gate reading only `secret_finding_count` serves the second. Proven to fail against the old gate on 2026-08-31: reverting layer 3 turns 3 of the 10 tests red. Asserts the fixture still reproduces the detector split against the real detectors, so it cannot rot into a trivial pass; asserts the withheld hit keeps its transcript, line, offset and length; asserts an unbuildable index WITHHOLDS rather than serves; asserts an ordinary hit still gets a snippet, so a gate that withheld everything would fail. |
| `tests/test_archive_no_body_prefix.py` | **No response anywhere contains a prefix of a body, asserted by EQUALITY.** For every route that can carry `body_json`, the returned value is either `None` or `== ` the stored `body_json` read independently from the database. `startswith` is explicitly NOT used, because `startswith` passes for a full string and therefore cannot detect the defect it is meant to catch. Includes a synthetic body larger than `MAX_BODY_BYTES` to exercise `withheld_too_large`, which is unreachable with real data (section 10). |
| `tests/test_archive_export_stream.py` | **Joined `iter_export_lines` output equals `export_transcript` byte for byte** on a real multi-line transcript, and the joined stream's sha256 equals the stored `content_sha256` and its byte length equals `raw_byte_length`. Asserts `/export/verified` refuses above `VERIFY_BEFORE_SEND_MAX_BYTES` with a 413 and a `cannot_determine` naming the stream href, rather than falling back. Asserts a `VERIFY_CANNOT_RENDER` line aborts the stream and sets the trailer to `cannot_determine`. |
| `tests/test_archive_hierarchy.py` | Hosts, corpora and projects round-trip. **Asserts a transcript with `project_id IS NULL` is reachable via `/unattributed` and is counted in `unattributed_transcript_count`**, so it can never be silently dropped. Asserts a transcript with `host_attribution = 'cannot_determine'` still appears under its host and carries `attribution_state: "cannot_determine"`, since it has a `host_id`. |
| `tests/test_archive_subagents.py` | Scoped edges match `subagent_edges(conn, transcript_id)` for the same transcript. **Asserts the route cannot be invoked without a `transcript_id`** and never reaches the unscoped 1,627,995-row form. Asserts `parent_transcripts` is a LIST and correctly returns two entries for a session_ref present on two hosts. |
| `tests/test_archive_routes_contract.py` | Introspects the FastAPI app: **every route on the archive router has `response_model is None`** and carries `require_auth` in its dependencies. This is a structural assertion, so a route added later without them fails without anyone remembering the rule. |
| `tests/test_archive_filters.py` | A `role`/`record_type`/`model` value that does not exist in the lookup table is `cannot_determine` with a reason, NOT an empty `ok`. Asserts `counts_are` is present on every filtered response. |

### 9.2 Required JS test files

| File | What it asserts |
|---|---|
| `tests/test_archive_envelope_client.node.mjs` | The client helper branches on `result_status` BEFORE rendering an empty state, and renders three visibly different states for `ok`/`partial`/`cannot_determine` with an empty `result`. Asserts the "no results" string is never produced for `partial` or `cannot_determine`. |
| `tests/test_archive_secret_masking.node.mjs` | Client-side masking uses `match_offset`/`match_length` against the WHOLE `body_json` and produces a string of the same length as the original. Asserts the masker never calls `slice` in a way that drops the tail, and that a body with two findings has both masked. |

### 9.3 What is deliberately NOT tested by a mock

The query plans in section 6 are asserted against a REAL database, not a
fixture, in `tests/test_archive_query_plans.py`: each documented SQL
statement is run through `EXPLAIN QUERY PLAN` and asserted to contain
`SEARCH ... USING INDEX <name>` and, where this document says so, to
contain NO `SCAN` of a large table. A fixture database with 10 rows will
happily use a different plan than an 11 GB one, so this test is skipped
with a NAMED skip reason when the real corpus is absent, never passed
silently.

---

## 10. Where the schema fights the design

Every compromise, named, with the measurement that decides whether it
matters.

### 10.1 No index carries an ORDER BY for transcripts

`ix_message_transcripts_project` is `(project_id)` only, so every
transcript page sorts in a temp b-tree. **Measured cost: 1.8ms on the
largest project (3,416 rows).** Accepted. The fix is a composite
`(project_id, ingested_at, id)`; it is NOT sanctioned because it costs
write amplification and about 700 KB to save 1.8ms. Re-measure if any
project reaches roughly 100,000 transcripts.

### 10.2 `project_id IS NULL` is an unindexed post-filter

`/unattributed` scans the corpus range and filters. **Measured 0.0079s on
the 19,548-transcript corpus for 0 matches.** Its cost is proportional to
the corpus and unrelated to the answer, which is the one route in this
API with that property. Accepted at this corpus size. A partial index
(`WHERE project_id IS NULL`) would fix it and is not worth 8ms.

### 10.3 `message_bodies.ts` is NULL on 33,480 rows, and unindexed

Two separate problems.

**Unindexed** is why the global feed is excluded (7.3), measured at
4.402s.

**NULL** is the subtler one and it affects the sanctioned index itself.
33,480 of 2,447,028 bodies have no `ts`. In SQLite NULL sorts BEFORE
every value ascending and after descending, so a keyset cursor on `ts`
cannot express a position among the NULLs: `ts < :cur` is NULL for a NULL
row, which is not true, so **every NULL-ts row is silently invisible to a
keyset page.** Any future ts-ordered endpoint must either add
`WHERE ts IS NOT NULL` and REPORT the 33,480 excluded rows in
`unevaluated`, or order by `(ts IS NULL, ts, id)` and index that
expression. It must not simply page and hope.

**The sanctioned index.** `CREATE INDEX ix_message_bodies_ts ON
message_bodies (ts)`. The prior pass measured 26.8s to build and +77 MB;
**I did not build it and did not verify those figures, so they are
CANNOT DETERMINE from this pass.** What I did measure is the 4.402s scan
it would replace. One useful property, offered as a PREDICTION requiring
`EXPLAIN QUERY PLAN` confirmation before anyone relies on it: SQLite
appends the rowid to every index entry on a rowid table, so an index on
`(ts)` is physically `(ts, rowid)` and, since `id` IS the rowid here, it
should serve `ORDER BY ts DESC, id DESC` with no separate tie-break
column. Verify that before designing a cursor around it.

### 10.4 Role, record_type and model are unindexed

No index on `role_id`, `record_type_id`, `model_id`. Forces filters to be
scope-local post-filters (7.4) and forces every count they produce to be
labelled `scanned`, not total. This is a real capability loss: "how many
assistant messages used opus across the corpus" is not answerable in a
request, and it is not faked.

### 10.5 `secret_finding_count` is unindexed

A 2.21s scan (measured) to find secret-bearing bodies. Forces the
secret-browse exclusion (7.8). The per-body path is unaffected because
`ix_message_secret_findings_body` covers it.

### 10.6 `LENGTH(body_json)` cannot be filtered without a scan

There is no stored body-size column, so "which bodies exceed
`MAX_BODY_BYTES`" is a 27.4s scan (measured). Consequences: the
`withheld_too_large` decision is made per row as rows are read, which is
correct and cheap, but the API can never report a corpus-wide count of
oversized bodies. It does not try.

### 10.7 `MAX_BODY_BYTES` is currently unreachable

**Measured: 0 bodies exceed 67,108,864 bytes. The maximum is 54,376,859.**
So `body_state: "withheld_too_large"` cannot fire on today's corpus. The
path must still exist, because the cap is what stops one future row from
pinning the process, and it must be tested against a SYNTHETIC oversized
body. A code path that has never executed and has no test is a path that
does not work; this one has a test precisely because reality cannot
provide the input.

### 10.8 `raw_line` and null `body_id` are one-row cases

Exactly 1 appearance row of 3,125,122 has `raw_line`, and exactly 1 has
`body_id IS NULL`. Both are live branches in `_render_row` and in
`/lines` (`body_state: "absent"`). A corpus this lopsided will not
exercise them by accident, so both need explicit fixtures. The
temptation to treat a 1-in-3,125,122 case as impossible is exactly how
the export path would silently break on the next one.

### 10.9 `ingested_at` is not unique and the whole corpus shares a batch

All 21,039 transcripts were ingested in a small number of batch runs, so
`ingested_at` values repeat at microsecond resolution. The `id DESC`
tie-break is therefore not a theoretical nicety, it is load-bearing on
real data, and `test_archive_keyset.py` must walk a real tie rather than
a synthetic one.

### 10.10 There is no schema-level guarantee that timestamps are ISO-8601

Stated fully in 5.2. All three columns are TEXT with no CHECK. The entire
pagination design rests on a property of the data that nothing enforces.
The mitigation is a test, not a constraint, because adding a CHECK to an
11 GB table means rebuilding it.

---

## 11. Corrections to the prior design pass

Places where I found the carried-forward decisions to be wrong or
unimplementable against the real schema. Recorded rather than silently
fixed, per this project's convention.

### 11.1 The export memory figure is wrong by about 6x, and it changes the priority

The brief states `iter_export_lines` is needed because
`export_transcript` peaks at "~360 MB on the 181 MB transcript", a ratio
of about 2x. **Measured 2026-08-31 on that exact transcript (id 10902,
182,077,926 bytes): 2,205 MB peak RSS, a ratio of 12.1x.** Confirmed
independently on transcript 5767 (91,950,363 bytes): 1,139 MB, 12.4x.

The cause is visible in the code: `export_transcript` retains a
`LineExport` per line each holding its own full `text`, PLUS the `texts`
list, PLUS the joined string, so roughly three retained copies before
Python's per-string overhead.

Consequence: the largest transcript in the corpus (244,117,661 bytes)
would peak near 2.9 GB (PREDICTION, by the measured ratio), and the
streaming refactor is a SAFETY REQUIREMENT rather than an optimization.
The prototype measured 78 MB for the same work. The brief's conclusion
was right; its reasoning understated the problem by an order of
magnitude.

### 11.2 `MAX_SCAN_BUDGET = 2000` is the wrong UNIT

A transcript-count budget assumes transcripts are comparable. Measured,
they are not: per-transcript search cost ranges from 0.4 ms to 405.6 ms,
a factor of about 1,000. A 2,000-transcript budget therefore admits
anything from 0.7s to about 811s (both PREDICTIONS extrapolated from
measured per-transcript rates).

Scan cost is, however, strikingly linear in BYTES: measured 0.432, 0.448
and 0.445 GB/s across three projects not previously touched in the
session. **This document therefore adds `MAX_SCAN_BYTES = 536870912` as
the primary governor and demotes `MAX_SCAN_BUDGET` to a secondary cap.**
Keeping only the count would have shipped an endpoint whose worst case is
13 minutes.

### 11.3 "Unattributed" conflates two different conditions

The brief asks for transcripts "whose host/project attribution is
unknown" to appear as `unattributed`. Measured, the real schema has:

- **0 transcripts with `host_id IS NULL`.**
- **0 transcripts with `corpus_id IS NULL`.**
- **5 transcripts with `project_id IS NULL`** (`project_attribution =
  'none_declared'`).
- **3 transcripts with `host_attribution = 'cannot_determine'` that
  nonetheless HAVE a `host_id`.**

So there is no "unattributed host" bucket to build, and the 3
cannot-determine rows must NOT be moved into an unattributed bucket:
they belong under their host, with their attribution state named. This
document splits the concept into a NAVIGATION concern
(`/corpora/{id}/unattributed`, for the 5 project-less rows) and a
QUALITY concern (`attribution_state` on every transcript row, for the 3
unevidenced ones). Implementing the brief literally would have hidden
three transcripts under a heading that does not describe them.

### 11.4 The global feed scan is 4.4s, not 3.4s

Re-measured: 4.402s, `SCAN message_bodies` plus a temp b-tree. Does not
change the decision to exclude it. Reported because the document should
carry the number I measured, not one I inherited.

### 11.5 `MAX_BODY_BYTES` never fires today

Detailed in 10.7. The constant is correct and the path is unreachable on
real data. This is not an argument to remove it; it is an argument that
its test must be synthetic, which the brief did not say.

### 11.6 Everything else in the brief held up

Byte-exactness, the no-redaction position, the secrets flag-never-redact
model, the no-prefix rule, always-scoped search, keyset over offset,
malformed-cursor-is-400, `response_model=None`, the exclusion list and
the file plan were all checked against the real code and the real data
and are carried forward unchanged. The `PRAGMA query_only=ON` approach
was verified live with both a positive and a negative control, and
`db.py` was confirmed to have no `query_only` pragma today.

---

## 12. Measurement log

Environment: MacBook, dev copy of the corpus at
`/Users/jsugamele/Scratch/llmScratch/cc-dev-state/cloude.db`, 11 GB,
schema v17, WAL. All reads on a same-uid connection with
`PRAGMA query_only=ON`. The pristine
`/Users/jsugamele/Scratch/llmScratch/hostdim/multihost.db` was not
opened. Nothing was written to either database. Date: 2026-08-31.

### 12.1 Controls run before trusting anything

| Control | Result |
|---|---|
| `PRAGMA query_only` reads back | `1` |
| `SELECT COUNT(*) FROM message_transcripts` on that connection | 21,039 rows (POSITIVE control: the connection works) |
| `INSERT INTO message_roles` on the SAME connection | `OperationalError: attempt to write a readonly database` (NEGATIVE control) |
| Unauthenticated `GET /api/v1/corpus/status` | HTTP 401 |
| Authenticated `GET /api/v1/corpus/status` | HTTP 200, real JSON, `archive.schema_version: 17` |

The write refusal alone would not have proved anything: a broken
connection refuses writes too. The pair does.

### 12.2 Corpus shape

| Table | Rows |
|---|---|
| `message_transcripts` | 21,039 |
| `message_bodies` | 2,447,028 |
| `message_appearances` | 3,125,122 |
| `message_projects` | 80 |
| `message_hosts` | 2 |
| `message_corpora` | 3 |
| `message_secret_findings` | 12,390 (6,240 distinct bodies) |
| `message_ingest_findings` | 142,888 |
| `message_record_types` / `message_models` / `message_roles` | 26 / 13 / 2 |

Largest project: id 12, 3,416 transcripts, 2,211,811,751 bytes.
Largest transcript by lines: id 5767, 30,805.
Largest transcript by bytes: 244,117,661.
Largest body: 54,376,859 bytes. Average body: 3,172 bytes.

### 12.3 Timings

| Query | Time | Plan |
|---|---|---|
| Hosts rollup | 0.0006s | covering index searches |
| Corpora for host | 0.0016s | index search + 3-row temp sort |
| Projects for corpus (51 rows) | 0.0001s | `(corpus_id, slug)` covers both predicates |
| Transcript counts per project, corpus 1 | 0.0102s | index range + temp group-by |
| Transcripts for project 1 (143) | 0.0004s | index + temp sort |
| Transcripts for project 12 (3,416) | 0.0018s | index + temp sort |
| Unattributed, corpus 1 | 0.0079s | index range + null post-filter |
| Lines, 201 rows | 0.0003s | `(transcript_id, line_no)` covers both, NO temp sort |
| Lines, 501 rows from line 15,000 | 0.0016s | same |
| All 30,805 lines, metadata | 0.132s | same |
| Body by id | <0.001s | integer primary key |
| Body by `message_uuid` | <0.001s | covering index |
| Secrets for body | 0.0002s | index on `body_id` |
| Subagent edges, transcript 17956 (20,931 rows) | 0.058s | index + temp sort |
| Search, one 30,805-line transcript | 0.028s | index range + INSTR post-filter |
| Search, project 12, 2,000 transcripts, absent term | 2.65s | as above |
| `ORDER BY ts DESC LIMIT 50` | 4.402s | **full scan + temp sort** |
| `COUNT(*) WHERE secret_finding_count > 0` | 2.21s | **full scan** |
| `MAX(LENGTH(body_json))` | 27.4s | **full scan** |
| `GROUP BY line_status` | 1.92s | **full scan** |

### 12.4 Scan rate, three untouched projects

| Project | Transcripts | Bytes | Time | Rate |
|---|---|---|---|---|
| 30 | 400 | 215,068,454 | 0.50s | 0.432 GB/s |
| 9 | 400 | 281,231,037 | 0.63s | 0.448 GB/s |
| 38 | 400 | 168,549,095 | 0.38s | 0.445 GB/s |

### 12.5 Export, measured end to end

| Transcript | Bytes | Method | Time | Peak RSS | sha256 | Bytes out |
|---|---|---|---|---|---|---|
| 1 | 58,968 | `export_transcript` | 0.001s | 23 MB | match | exact |
| 5767 | 91,950,363 | `export_transcript` | 1.43s | 1,139 MB | match | exact |
| 10902 | 182,077,926 | `export_transcript` | 2.38s | 2,205 MB | match | exact |
| 10902 | 182,077,926 | **streaming prototype** | **1.79s** | **78 MB** | **match** | **exact** |

The streaming prototype reproduced `content_sha256`
`218e15dc1d09cf2813a28e88...` and emitted exactly 182,077,926 bytes.

### 12.6 Labelled predictions (NOT measured here)

| Claim | Basis |
|---|---|
| Full-project scan of project 12 is 4.5s | measured per-transcript rate x 3,416 |
| Unscoped corpus search is about 17.6s | 7.76 GB at the measured 0.44 GB/s |
| 2,000 large transcripts would take about 811s | 2000 x measured 405.6 ms |
| Largest transcript peaks near 2.9 GB under `export_transcript` | measured 12.1x ratio x 244,117,661 |
| FTS5 over raw `body_json` adds roughly 8 GB | measured 7.76 GB of body text, doubled |
| An index on `(ts)` serves `ORDER BY ts DESC, id DESC` via the appended rowid | SQLite structure; **confirm with EXPLAIN QUERY PLAN before relying on it** |

### 12.7 CANNOT DETERMINE

| Question | Why |
|---|---|
| Build time and size of `ix_message_bodies_ts` | I did not build it. The prior pass's 26.8s / +77 MB is unverified by this pass. Building it requires a write to the corpus, which was out of scope. |
| FTS5 build time on this corpus | Not built. The prior pass's ~31 min is carried forward unverified. |
| Whether these timings hold on a cold page cache | Every measurement here ran against a warm-ish cache on a machine also running the dev server. Cold-cache figures would be worse; the ratios between queries should hold, the absolute numbers may not. |
| Concurrent-request behaviour | Every measurement is single-threaded. The `asyncio.to_thread` design means N concurrent searches are N threads against one SQLite file, which was not load-tested. |
