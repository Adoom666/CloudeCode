# Upgrading CloudeCode with Claude

You are running Claude Code. So is everyone else who uses this. That makes
the upgrade something you can hand to an agent rather than perform by hand,
and this file is what the agent follows.

    /upgrade            in a Claude Code session opened on this checkout

or, in plain words to Claude: "upgrade CloudeCode and confirm the data
migrated." The slash command lives at `.claude/commands/upgrade.md` and does
nothing more than point at this document.

This covers the **from-source install** (README "Path B"): a git checkout with
its own `venv/`, `.env` and `config.json`. The packaged `.app` has no in-place
upgrader; a new version there means a new DMG dragged over `/Applications`.

---

## The rule this whole document exists to enforce

**An upgrade that reports success is not the same as an upgrade that worked,
and a database that opens is not the same as a database that kept your data.**

Every check below has THREE outcomes, never two: it passed, it failed, or it
could not be evaluated. The third is not a flavour of the first. If a step
cannot be measured, say so and stop; do not write "looks fine".

This is not abstract caution. `src/core/db.py::get_schema_version` carries a
warning in its own docstring about a real incident in this codebase: it
collapses "no version recorded" and "a version that will not parse" onto the
same `0`, and that collapse let a populated nine-project database migrate with
**zero backups taken**, recorded in the trail as a clean bootstrap from an
empty file. The number looked like a measurement. It was two different facts
wearing one hat.

---

## Step 1: capture the baseline BEFORE touching anything

You cannot verify a migration without a before. This is the step people skip
and the reason "did it work?" is usually unanswerable afterwards.

```bash
./scripts/upgrade-baseline.sh
```

That writes a timestamped JSON snapshot under `.upgrade-baselines/` holding the
current version, the schema version as a three-outcome read, and a row count
for every table in `cloude.db`. Read it back; do not just trust that it ran.

If the script reports it could not read the database, **stop**. An upgrade you
cannot verify is one you should not start.

## Step 2: upgrade

```bash
./scripts/upgrade.sh              # newest published release tag
./scripts/upgrade.sh 1.0.7        # a specific tag
```

`upgrade.sh` refuses any tag that is not a real published release tag on the
remote, so you cannot typo your way onto an untagged commit. It takes its own
backup into `.upgrade-backups/<timestamp>_from-X_to-Y/` before it changes
anything, with a `.manifest` recording, per file, whether it was backed up or
legitimately not present yet.

**Read the manifest.** A backup that quietly skipped a file is the thing that
turns a bad upgrade into a lost one.

## Step 3: verify the data actually migrated

```bash
./scripts/upgrade-verify.sh
```

It compares the live database against the newest baseline and reports each of
these separately, with its own verdict:

| Check | Passes when | Cannot determine when |
|---|---|---|
| schema version | reads as an int and is >= the baseline's | absent, or present and unparseable |
| code schema version | matches `CURRENT_SCHEMA_VERSION` | the module will not import |
| row counts | no table lost rows | a table is missing from either side |
| migration trail | this run's entry exists and is `ok` | no entry for this run |
| served version | the running server reports the new version | the server is not answering |

**A schema version that did not change is not a failure.** Most upgrades ship
no migration. Between v1.0.5 and v1.0.7 the schema stayed at 9 and that was
correct. What would be a failure is the version going BACKWARDS, or row counts
dropping, or a trail entry that says anything other than `ok`.

### Verify the running server by CONTENT, not by claim

`git rev-parse` on either side only reads back the claim the deploy already
believes. It is not an independent measurement. Fetch an asset over HTTP from
the running server and hash it against the git blob that exists only in the new
commit:

```bash
curl -fsS http://127.0.0.1:8000/static/js/app.js | shasum -a 256
git show <new-tag>:client/js/app.js | shasum -a 256
```

Two identical hashes prove the server is serving the new code. Note that
`index.html` will NOT match its blob, because the server substitutes
`{{VERSION}}` into it; diff it and confirm the only delta is that substitution
rather than abandoning the check.

### If nothing looks different in your browser

A tab left open across a restart does not re-fetch static assets on its own. It
keeps polling its API endpoints and keeps its WebSocket alive while running
arbitrarily old JavaScript. Before looking for a server-side cause, hard-reload
the page. If you want to be sure, look in the access log for a static-asset
request from your own address AFTER the restart timestamp; if there is none,
the fix is the reload, not the server.

## Step 4: if it went wrong

```bash
./scripts/rollback.sh
```

Restores from the newest `.upgrade-backups/` entry. Nothing in that directory
is ever deleted automatically.

Downgrading is supported by design: the config model is declared
`extra="ignore"`, so a config written by a newer version loads cleanly in an
older one, with the fields it does not know about carried through untouched
rather than rejected. The database is a different question. A schema that moved
forward will not move back on its own, which is exactly why step 1 exists.

---

## What Claude should report back

Not "upgraded successfully". Report the measurements:

```
version        0.9.0 -> 1.0.7
schema         9 -> 9  (no migration on this path, expected)
rows           projects 9 -> 9, sessions 14 -> 14, migration_trail 3 -> 4
trail entry    kind=code from=0.9.0 to=1.0.7 status=ok
served asset   sha256 match against 1.0.7 blob
backup         .upgrade-backups/20260826T191455Z_from-0.9.0_to-1.0.7 (manifest complete)
```

and, if anything could not be evaluated, say which check and why, in those
words. "Could not determine whether the served asset matches: the server was
not answering on 8000" is a useful sentence. A blank line is not.

---

## Step 4: did the user's EXISTING SESSIONS come across?

This is the question the person upgrading actually asks, and the schema
checks above do not answer it. Read this section before telling anyone the
upgrade succeeded.

### Row counts cannot answer it, and will look like they did

`upgrade-verify.sh` fails loudly if any table SHRINKS, so a lost session is
caught. What it cannot catch is a session that was never imported. A user
coming from a version with no datastore has zero `sessions` rows in the
baseline, so the count goes `0 -> 0` and the check reports "no table lost
rows" - true, and useless. **Zero because there was nothing to import and
zero because the import never ran are different facts.**

That is why there is a separate `session import` check. Read it.

### What the import actually does

On the first start after upgrading, the app takes ONE tmux listing and
imports the live sessions it finds into the `sessions` table. It is guarded
by a one-way latch, `meta.imported_from_json_at`.

The guard is the important part:

| listing outcome | imported | latch | what happens next |
|---|---|---|---|
| `ok=True`, sessions found | those sessions | SET | done |
| `ok=True`, `reason=no_server` | 0 | SET | correct - tmux was not running, so there was genuinely nothing to import |
| `ok=False` (probe failed) | 0 | **UNSET** | retries on the next start, and the home screen says so |

A failed probe deliberately does NOT stamp the latch. Stamping it would
permanently skip the import and silently cost the user their history, so
`session_import.py` has exactly one latch write site, placed after the
`if not listing.ok` gate, and a test walks the module's AST to assert both.

**`no_server` is `ok=True` on purpose** - it is a real, complete answer of
zero, not a failure. Do not read "imported 0" as a problem without reading
the reason beside it.

### What to tell the user

- `session import PASS ... imported N` - their sessions are in.
- `session import PASS ... imported 0 because no tmux server was running` -
  nothing to import; start a session and it will be tracked normally.
- `session import CANNOT-DETERMINE ... has not run yet: <reason>` - NOT a
  failure and NOT a success. It retries next start. Their existing sessions
  are not imported until it does. Have them start the app with tmux
  reachable and re-run the verify.

## Step 5: downgrade, if they want to go back

**The upgrade backs up before it does anything destructive.** `upgrade.sh`
copies `.env`, `config.json`, `refresh_tokens.db`, `session_metadata.json`,
`pinned_themes.json`, `unread_state.json`, `cloude.db`, `hook_tokens.json`
and `migration_trail.jsonl` into a dated directory it prints prominently,
SQLite files via `VACUUM INTO` + `integrity_check` rather than `cp`, since
several are live WAL databases. `scripts/rollback.sh` restores that.

**If they downgrade WITHOUT restoring the data, nothing is corrupted.** The
older app reads `meta.schema_version`, finds a number it does not
understand, and refuses to touch anything:

> this install's data is at schema vN, this app version only knows schema
> vM - restore the newer app version, or restore data to vM via the trail.
> Running read-only until then; **no data has been changed.**

That is a named degraded state, not a crash. Migrating backward is never
attempted, because that code was never written to understand a newer
schema. `rollback.sh` is equally careful: with a database present but no
migration trail beside it, it REFUSES rather than guessing which schema
belongs to the target version.

Schema changes so far have been ADDITIVE (v10 added `claude_title`, v11
added `activity_state` and `activity_state_at`), so an older reader that
selects the columns it knows still reads a newer file correctly. That is
what makes a data-restoring rollback clean rather than lossy - but it is a
property of these particular migrations, not a guarantee about future ones.
