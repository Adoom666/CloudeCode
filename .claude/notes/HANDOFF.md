# HANDOFF - CloudeCode app development

Written 2026-09-07, UPDATED 2026-09-08. Re-scoped from
`Infrastructure/.claude/notes/handoff-2026-09-06-cloudecode-migration.md`, which
was written for someone continuing the MacBook-to-mini MIGRATION. This one is
written for someone continuing APP DEVELOPMENT.

Everything below was measured on the dates given, not inferred. Where something
is unproven it says so in those words.

Read `CLAUDE.md` in the repo root first - it is the architecture and the
conventions, and this file deliberately does not repeat it. Read
`.claude/TODO.md` second, starting with its `## OPEN - index`. This file is the
operational half: where things run, how to deploy, and what will lie to you.

---

## 1. WHERE THINGS PHYSICALLY ARE

| Thing | Location |
|---|---|
| Source checkout (authoritative) | `/Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs/Sync/Development/CloudeCode`, branch `v1.1` |
| Live production server dir | mini: `~/Library/Application Support/cloude-code-menubar/server` |
| Live production app bundle | mini: `/Applications/Cloude Code.app/Contents/Resources` |
| Live URL | `http://10.0.1.150:8000` |
| Staging (v1.1) | port 8001, state `~/Library/.../CloudeCode-v1.1`, tmux socket `cloude-v11`. Disposable. |
| Deploy script | `scripts/deploy-mini.sh`; `--target live` is REQUIRED, the default target is v11 |
| Live tmux socket | `cloude` |
| Database | `~/Library/Application Support/CloudeCode/cloude.db`, schema v23, ~4.5 GB (4.68 GB with a 195 MB WAL as measured 2026-09-05) |
| Archive corpus database | mini: `/Users/jsugamele/ClaudeArchive/`, 21 GB, NOT readable by the production app |
| Wrapper scripts (rendered) | `~/Library/Application Support/CloudeCode/agent_wrapper_scripts/<id>.zsh` |

**BOTH deploy targets must be written.** The packaged app copies its bundle
Resources over the server dir on every start, so writing only the server dir is
silently reverted at next launch. `deploy-mini.sh --target live` does both; a
hand `scp` must do both explicitly.

**Everything here runs on mac-mini-m4 (10.0.1.150).** There is no other host in
this project.

**WHAT LIVE ACTUALLY RUNS, updated 2026-09-08 night: repo HEAD is
`6934965`, three commits past the `8ee40d1` this file previously named as
live (`117823d..6934965`: `e7a212e` halo recalibration, `cc885d6` the
question/notice split, `6934965` the hook-token recovery self-heal).
**A deploy of this HEAD was in progress by another agent at the time this
line was written - treat it as deploy in progress at time of writing,
not confirmed.** The last CONFIRMED live state is still what `8ee40d1`
left running: boot held 19 sessions (18 plus 1 benign skip), every
running session carries a project, session lists show interactive
conversations only (645 archived visible, 270 automated excluded by
default, 305 unknown kept). See section 8 for the full commit-by-commit
state of both rounds and the branch's push status.

---

## 2. THE DEPLOY TRAPS

Each of these has cost real time, and each fails by looking like success.

**FIXED 2026-09-07: a clean tree now deploys the committed state.**
`deploy-mini.sh` used to deploy only CHANGED files, so against a clean tree it
printed `nothing to deploy` and exited, which was repeatedly read as a
successful deploy. It now falls back to the committed file set and says
`working tree is CLEAN, so deploying the committed state`. The three outcomes
are separate banners and separate exit codes: `== DEPLOYED ==` (0),
`== NOTHING DEPLOYED ==` (2, and it spells out that the target was NOT
updated), `== DEPLOY FAILED ==` (1, or 3 for could-not-evaluate, 4 for a hash
mismatch). `--all` still forces the full set explicitly.

**The transfer is tar over ssh, NOT rsync, and this is deliberate.** Both this
Mac and the mini ship Apple openrsync (protocol 29, "rsync 2.6.9 compatible"),
not GNU rsync 3.x. openrsync handles `--files-from=- --relative` fine; what
breaks it is a remote destination containing SPACES, which the remote shell
word-splits, giving `server receiver mode requires two argument`. BOTH live
destinations contain spaces, so `--target live` could never work while
`--target v11` always did. That is the real cause of the 2026-09-07 failure,
not `--files-from`. tar over ssh has no such edge because the remote command is
one quoted string the script controls.

**Verification is now automatic and nobody has to remember it.**
`deploy-mini.sh` stages the files in one dir on the mini, sha256s the staging
area against this Mac BEFORE production is touched, copies into each
destination, then sha256s EVERY file on EVERY destination against this Mac, and
after a live restart re-hashes the server dir to catch the bundle revert. A
mismatch names the offending file and exits non-zero. A file the target cannot
hash becomes an explicit MISSING line rather than dropping out of the
comparison, because absent on both sides compares equal and that is how a check
silently verifies nothing. `deploy-mini.sh --verify-only` re-runs exactly that
check against a target without copying anything.

Never verify with `git rev-parse` on either side - that only reads back the same
claim the deploy already believes, it is not an independent measurement. For a
by-hand check outside the script, fetch the asset over HTTP and hash it against
the local file; the 2026-09-06 `5c88fdd` deploy was verified by
`GET /static/js/launchpad.js` and hashing, and that is the pattern to copy.

**A browser tab open across a deploy does not re-fetch static assets.** It can
run arbitrarily many releases behind while its API calls and its WebSocket keep
working perfectly. Hard-reload before believing ANY UI observation. One-line
check before chasing a server-side explanation for "my changes are not showing":
look for a STATIC-ASSET request from that client's IP after the restart
timestamp. If there is none, the fix is a hard reload, not the server.

**The supervisor gives up after 3 unexpected stops.** A dead server can STAY
dead while the page looks completely normal, because the UI gives no signal when
the server dies. Recover with:

    launchctl kickstart -k gui/501/com.cloudecode.menubar

That took about 15 seconds on the last deploy. The missing down-indicator is
itself an open item.

**`com.cloudecode.menubar` on port 8000 is the live agent.**
`com.cloudecode.v11` on 8001 was disabled 2026-08-31 (`launchctl bootout` plus
`disable`, plist left in place). `com.imc.cloude-code` does NOT serve this app;
Infrastructure `CLAUDE.md` hazard 40 names it wrongly and carries the correction.

**Staging cannot be click-tested.** It is TOTP-gated and credentials are not to
be entered. Verify on live, or by API.

---

## 3. THE LAG - FIXED 2026-09-07, and what is still open behind it

**The root cause was not where anyone was looking.** `PRAGMA integrity_check`
at `src/core/db.py:177` ran SYNCHRONOUSLY on the asyncio event loop inside the
`GET /api/v1/version` handler (`version_routes.py:207` -> `:178` ->
`db_health.py:94`), against a 4.5 GB `cloude.db`. The Electron tray polls that
endpoint every 20 seconds (`macOS/main.js`, `INTERVAL_MS = 20000`), and the
pragma re-verifies every page of every B-tree, so the loop was unavailable for
roughly 14 of every 20 seconds on an IDLE box, and the stall grew with the file.

**It was a REQUEST HANDLER ON A TIMER, not a background task.** That is exactly
why it survived several hunts: everyone was grepping for a loop. When you are
chasing a periodic stall, read what POLLS as well as what loops.

Confirmed twice, independently, BEFORE a line of fix was written: `py-spy dump`
mid-stall returned **17 of 17 stalled dumps byte-identical**, and the static
call chain says the same thing.

**Measured on live, before and after the deploy:**

| measurement | before | after |
|---|---|---|
| p99 | 14,505.7 ms | 13.3 ms |
| p50 | 30.5 ms | 5.2 ms |
| stall cadence | every 20.0s, lasting 14.5s | GONE |
| share of the window stalled | 86.5 percent | 0.0 percent |
| samples at the same poll rate | baseline | TRIPLED |

The tripled sample count is the measurement to trust, because it is
independent of the timing numbers: at an unchanged poll rate, more samples come
back only if the loop is free to answer them.

**13.3 ms IS NOT SETTLED, do not quote it as the number.** Later polls read p99
**64-72 ms** across two runs, and most recently **66-73 ms**. That is still
three orders of magnitude better than the 14,505 ms broken state and carries no
stall pattern, but it is about five times the figure recorded immediately after
the fix and it is NOT attributable. The corpus ingester was active during the
later runs; that is a hypothesis and it was NOT tested. Re-measure on a quiet
box before treating any single figure as the baseline.

The replacement design is in `CLAUDE.md` under "The daily database integrity
check": the pragma moved to a daily background loop that publishes a verdict
artifact, and the request path became one connect plus one small SELECT plus
one small JSON read. `tests/test_db_integrity_verdict.py` booby-traps every
binding of the helper, so a pragma cannot come back onto the request path
without failing the build.

**Still open behind it: the render path.** `renderProjectList()` does a
wholesale `innerHTML =` with NO signature guard, rebuilding 794 DOM nodes (385
elements + 409 text nodes) plus about 45 event listeners on every 5-second
tick, including while the launchpad is hidden behind a terminal. There is no
`clearInterval` anywhere in `launchpad.js` and the poller never pauses on tab
hide. Verify a fix by asserting the MUTATION COUNT, not that the signature
stopped changing - a signature that never changes and a renderer that ignores
it look identical from outside.

**Line numbers recorded for these have DRIFTED.** The TODO records
`renderProjectList()` at launchpad.js:3976 with the `innerHTML =` at :4120, and
the poller variously at :152 and ~387. Spot-checked 2026-09-07 on the `v1.1`
tip: `renderProjectList()` is at :4155, the wholesale `innerHTML =` at :4318,
the `setInterval` at :454, in a 6,069-line file, and `grep -c clearInterval`
returns 0. The facts hold; re-derive any line number before quoting it.

**Priority order now:** (1) the event loop is DONE. (2) the project-tree render
guard. (3) push updates over the existing websocket instead of polling. Note
for (3) that `src/api/websocket.py` carries NO project or session-list message
type at all - the websocket is per-session terminal I/O only, so there is no
push channel for state today and adding one is real work, not a config change.
Re-measure before designing it: the poll may not be the real cost now that the
loop is free.
---

## 4. IDENTITY - the model, and the identifiers that are not the same

**Three different identifiers, do not conflate them:** pin IDs, `cliSessionId`,
and transcript UUIDs. A pin ID can COINCIDENTALLY also exist as a real
transcript UUID. That happened with `8ae03490` and produced a confident wrong
conclusion that the Daily Briefing "was never a daily-briefing session". It maps
to `ce8c2dca`, a genuine briefing transcript, Aug 4 to Sep 1, 1,604 turns.

**Session identity is the triple `(tmux_socket, tmux_name, tmux_created_epoch)`
versus the durable `session_uuid`.** Not interchangeable. This is why tmux
resurrect/continuum needs a design pass rather than an implementation: a
restored session gets a NEW creation epoch and a naive restore orphans rows.

**`lifecycle='stopped' AND archived_at IS NULL`** is the Recent group.

**Group membership is DB-backed and travels between devices. Pinning and
within-group order are `localStorage` and are per-device.** A session that is
both pinned and grouped shows under Pinned where it was pinned and falls back to
its group elsewhere, which is the desired behaviour. `session_groups` HAS a
`position` column and a reorder endpoint; `session_group_members` does NOT, and
its primary key is `tmux_name` rather than `session_uuid`, which is a latent
defect (two sessions sharing a tmux name would share one membership row).

**`launchpad.js` launches new sessions with `project.path` (= `raw_path`), not
`root`.** A project-row correction that fixes only `root` is worse than nothing.
Recorded at :5599; spot-checked 2026-09-07 as `working_dir: project.path` at
:5871.

**cwd spelling matters, and it is the reason two junk project rows exist.**
`~/Development` is a symlink into iCloud and Claude Code derives its transcript
directory from the LITERAL cwd string, so two spellings of the same directory
produce two separate transcript dirs. Projects 3 and 4, `Mac (old path)` and
`Hirschfeld (old path)`, were retired 2026-09-03 for exactly this. Any
auto-derived path must use the resolved iCloud spelling. Always write the long
spelling in documents and in code.

**`sessions.claude_session_uuid` IS THE ROW'S COPY OF THE TRANSCRIPT UUID, AND
IT IS THE ONE THAT GOES MISSING.** It is what a restart resumes. Diagnosed
2026-09-08; the fix is committed as `8dd54a8` and NOT DEPLOYED, so on live all
of this is still true.

- **16 of 39 rows carry no uuid at all.** On the create path it has exactly ONE
  writer, Claude Code's `SessionStart` hook. An empty POST body becomes `{}` at
  `routes.py:2108`, `session_manager.py:4341` returns `LINEAGE_UNRESOLVED` and
  writes nothing. The live server log holds **25 such failures across 20
  distinct sessions**. The structural reason it never heals: `SessionStart`
  fires ONCE per conversation with no retry, while every other hook event
  repeats. That is why `last_work_at` recovers on the next event and the uuid
  never does.
- **The fallback ladder had never once fired on this machine.**
  `slugify_project_dir` (`claude_transcript_correlate.py:191`) mapped only `/`
  and `.`; the real rule maps EVERYTHING outside `[A-Za-z0-9-]` to a single
  `-`, verified against **908 of 919** live transcripts. Every one of the
  owner's paths contains a space and two tildes, so every slug it built was
  wrong. **A ladder that cannot fire is not a fallback**, and it is invisible,
  because a fallback that never matches looks exactly like one that was never
  needed.
- **`agent_type` does NOT share this cause.** 14 of 39 rows have `agent_type`
  NULL and a hook-written uuid, so backlog item 3 is a separate job.

**A uuid ON THE ROW IS NOT EVIDENCE A TRANSCRIPT EXISTS, and this is the trap
that will lie to you.** Over 39 rows: 16 absent, 18 sound, and **5 PHANTOM** -
a recorded uuid with no transcript on disk. All 5 collide, because the correct
uuid is already held by a sibling row split off by the cwd spelling trap above.
The pairs are **7 to 4, 9 to 11, 10 to 12, 38 to 39**.

Worked example, because the mechanism is the whole point. Row 7 is LIVE, its
`working_dir` is the SHORT symlink spelling, and its uuid
`db81f6bf-85f9-448b-a7f6-bc83f62659d9` exists nowhere. Row 4 is ARCHIVED,
carries the long iCloud spelling, and its uuid `82854c0e-...` has a transcript
that is **73,190,422 bytes**. The live pane is literally running
`--resume 82854c0e-... --fork-session`. **`--fork-session` MINTS A NEW uuid**,
the hook recorded that new one, and the forked transcript never materialised.
So the picker truthfully said "no transcript for db81f6bf" about a uuid with
nothing to do with the conversation the user was in, while the real
conversation sat on the archived twin. **The message was locally correct and
the conclusion a reader draws from it is wrong.** "Media Compression's
conversation is gone" was that wrong conclusion, and it was wrong.

**The backfill tool exists and has NEVER WRITTEN ANYTHING.**
`scripts/backfill_claude_session_uuid.py` is dry run by default. Its run over
the live corpus: **8 confident FILLs** (rows 14, 15, 16, 17, 19, 23, 24, 25),
**0 confident REPLACEs**, **8 ambiguous** (rows 26, 27, 30, 31, 33, 35, 40, 42,
at 45 to 78 candidates each), **1 no-candidate** (row 41, whose `working_dir` is
literally `/Users/jsugamele/Development/ses_ee8d919b`), and the 4 collision
pairs. It resolves both cwd spellings FORWARD, by slugifying every spelling
found in a HOME symlink scan, AND BACKWARD, from each transcript's own recorded
cwd canonicalised. The backward direction is not optional: **754 of 919
transcripts sit in a directory that disagrees with their own recorded cwd.**

Two matcher defects were caught by CONTROLS rather than by reading, and both
would otherwise have shipped looking correct. Directory agreement was clearing
a two-signal bar as ONE FACT CORROBORATING ITSELF, so evidence is now counted
in independent FAMILIES. And the negative control (a row pointed at a project
that does not exist, with real transcripts present) initially returned
`ambiguous` instead of `no_candidate`, because timing alone was counting as
evidence; an ANCHOR GATE now lets timing and title CORROBORATE a candidate and
never CREATE one. **A matcher that always finds something is worse than
useless.**

**CloudeCode only manages tmux sockets it owns.** A session resumed by hand on
the `default` socket is invisible to it, and tmux cannot move a pane between
servers. The only route back is to let the foreign copy exit and resume the same
transcript inside the CloudeCode pane.

---

## 5. WRAPPERS AND THE RESPAWN LADDER

Wrappers live under `agents.wrappers` in `config.json` and are rendered to
`~/Library/Application Support/CloudeCode/agent_wrapper_scripts/<id>.zsh`.

**A hand edit to `config.json` is NOT seen.** `Settings.load_auth_config` caches
in `_auth_config_cache`, so the running server keeps the old view. Add or change
a wrapper through `POST /api/v1/agents/wrappers`, which keeps the file and the
in-memory cache in agreement.

`claude-chrome` exists as of 2026-09-06:
`command claude --dangerously-skip-permissions --chrome "$@"`. Project
"Fantasy Football 2026" (project 17) has `projects.default_agent_type =
'claude-chrome'`; the default `claude-skip-permissions` was deliberately left
untouched so only that one project changed. Revert SQL is on the mini at
`~/football_chrome_revert.sql`.

**The respawn ladder gates on tmux's `#{pane_start_command}`.** A pane born as a
bare shell has that field EMPTY, so the ladder lands on `RESPAWN_SHELL` and an
UNPICKED restart hands back a LOGIN SHELL instead of claude. A session needs
BOTH a non-empty `pane_start_command` AND a stored `agent_type` to reach
`RESPAWN_AGENT` on its own. Row 40 (`Agent - Infrastructure`) was staged
deliberately with both, and was the first session on the box that would restart
correctly unaided.

**RESTATED 2026-09-08, because this was misstated once already.** Two facts,
and quoting only the first is what caused the confusion:

- **18 of 22 live sessions have an empty `#{pane_start_command}`** (measured
  2026-09-08; it read 15 of 19 on 2026-09-07, the population moved).
- **An explicit wrapper choice OVERRIDES that gate**
  (`src/core/session_respawn.py:542`). Picking a wrapper in the picker starts
  the agent properly.

So only an UNPICKED restart lands on the shell rung. Do not write "the restart
button gives you a shell" without the second half.

**UPDATE: the gate is no longer silent, and an explicit choice now beats it.**
Two things changed together.

`GET /sessions/restart/preview` (`src/api/restart_routes.py`) answers which rung
a session would land on WITHOUT acting. It reports the rung twice, because they
are two different questions: `unchanged` is what a restart does right now (for a
live pane, `not_dead`), and `projected` is what the session would come back AS
with liveness ignored (`project_restart_rung`, never `not_dead`). `pane_state`
carries liveness as its own fact. **A projected rung is a PREDICTION, NEVER A
PERMISSION** - it exists so the picker can say "this one would come back a plain
shell" about a session that is still running, which on this box is the whole
interesting population.

`POST /sessions/respawn` now takes an optional `agent_type`. It is an ID, never
a command: it is checked against `agents.wrappers` and an unconfigured id is a
400 rather than a silent fall back to the default wrapper
(`src/core/session_agent_choice.py`). An explicit choice OUTRANKS the
`pane_start_command` gate - the gate exists because a STORED `agent_type` is not
evidence of intent, and a wrapper the user just picked in a panel is. It does
NOT outrank `not_dead` or an unanswered probe. On a verified-alive restart the
choice is written to `sessions.agent_type`, which is the hand edit this replaces.

A sixth rung exists now: `RESPAWN_TRANSCRIPT_MISSING`. A REPLAY hands tmux back
its own start command, and 3 of the 19 live sessions on the box carry an
explicit `--resume <uuid>` in theirs, so a replay CAN re-run a resume against a
transcript that has since been deleted. `resume_uuid_in` extracts it and the
caller checks it through `session_transcript_presence`; a DEFINITE absence
refuses, `unchecked` never does. The preview applies the same guard, so the
picker cannot promise a replay the restart then declines.

The picker is `client/js/session-restart-picker.js`; the reopen is
`client/js/session-restart-return.js`; the option list was extracted to
`client/js/session-restart-options.js` (350 lines) in `0793eb1`, which also
takes the picker back under the 500-line rule.

**The picker's option text painted over the next row, and that fix is NOT
deployed (`0793eb1`).** Option rows inherited `flex-shrink: 1` inside a
`max-height: 46dvh` flex column, so the flex algorithm SQUASHED THE ROWS
instead of scrolling the list. Measured: 113px of content in a 62px box on
desktop, 145px in a 58px box on a phone. The fix is `flex-shrink: 0`. It is
verified by GEOMETRY, not by DOM text: `tests/test_restart_picker_geometry.py`
asserts no row's text intersects another row's title, and it was PROVEN TO FAIL
on the old code with 19px of text sitting on text. This matters more than a
cosmetic fix looks, because the picker is the only route to `--chrome` browser
control (section 9).

**UPDATE 2026-09-07: restarting a LIVE session IS built (item 22 part 2).**
The owner's two calls collapsed it - "same tmux should be fine" and "yes
resume the same session" - so it is `respawn-pane -k` in place, not
close-and-recreate. No row is minted, so nothing is re-carried and the
`session_group_members` primary-key defect is sidestepped rather than hit.

Four gates, and none of them is derivable from the projection: the row only
offers restart on a status positively known live (`unknown` gets close alone);
the picker's arm checkbox arrives unchecked and takes no argument; the confirm
modal names the bare-shell outcome (measured: 15 of 19 live sessions come back
a login shell); and the request must carry `confirm_restart_live`.
`RespawnPlan.kills_live_pane` is the single field that makes anything pass
`-k`, and `project_restart_rung` has no liveness input so it can never set it.

**UPDATE 2026-09-07 (later): A RESTART NOW RESUMES, ON EVERY RUNG THAT
CAN.** The owner defined it: "restart on recent is really just resume.
restart on open is close and resume session so it loads a new wrapper or
new claude binary." `f95a9ed` delivered that only on the REPLAY rung, by
accident of what tmux had recorded; the AGENT rung re-derives through
`get_agent_command` and carried no `--resume`, so it started a FRESH
conversation wearing the old name. `sessions.claude_session_uuid` now
reaches the command via `extra_args`, built once in
`src/core/session_resume_target.py` and threaded into all four command
resolutions (stored agent and every wrapper offer, action side and
preview side) so the two cannot render different strings.

`RespawnPlan.conversation` and the preview's `conversation` field carry
`resumed` / `none_recorded` / `unknown` - the same vocabulary
`RestartSessionResponse.conversation` already used. A NULL uuid is
`none_recorded` and is SAID, never a quiet fresh start; an unreadable row
is `unknown`, injects nothing and claims nothing. The value is derived
from the argv, so the claim cannot outrun the command. The transcript
guard now covers the agent rung on both paths, and the preview keys
presence verdicts by uuid because two rungs can resume two different
conversations.

ONE DRIFT FIXED ALONG THE WAY: `_agent_command_for_tmux_name` read
`agent_type` from `self.sessions` while `restart_preview` read it from the
ROW, so an ADOPTED session (in-memory `agent_type` None, row records the
wrapper) previewed as AGENT and restarted as REPLAY. Both now go through
`_stored_agent_type_for_tmux_name`.

**Identity across the kill is MEASURED.** tmux 3.7c, scratch socket: same
`#{session_created}`, same `#{pane_id}`, new `#{pane_pid}`. The triple does not
move, so the fourteen triple-keyed queries keep matching.
`src/core/session_instance_rekey.py` takes the epoch either side anyway and
answers `unchanged` / `rekeyed` / `cannot_determine` - a measurement that can
stop being true is not a thing to assume.

Related, and open as backlog item 3: every session created via
`auto_start_claude:false` plus a hand-sent claude command lands with
`sessions.agent_type` NULL, because the agent_type wrapper only applies on
auto-start. The flag IS on the running process. Fix the persistence, not the
display.

---

## 6. KNOWN FALSE GREENS CURRENTLY LIVE IN THE PRODUCT

Each with its mechanism, because the mechanism is what tells you whether your
fix covers it.

**A row reads `running` over a pane whose agent has exited.** The SessionEnd
hook is cancelled on every clean exit - it fires while claude is tearing down,
its command is a `curl -sS -m 2`, and claude cancels pending hooks as it exits.
`reconcile_lifecycle` CANNOT cover for it, because that reconciles against the
TMUX LISTING, and a pane whose claude exited still has a live tmux session.
Measured 2026-09-06 between an `/exit` and a relaunch: tmux alive, no claude
process, row 39 still `lifecycle = running`. Seen again 2026-09-07 with row 40
reading `running` while its pane was DEAD (exited status 0). Two independent
routes to the same wrong answer, so a fix must reconcile against the PANE and
the agent, not the listing.

**`activity_state` reads `working` for about four minutes after a resume.** Then
it self-corrects (measured moving to `question` at 21:49:38 on 2026-09-06). Real
and self-clearing on that evidence, not stuck - a narrower defect than backlog
item 4 as written. Do not treat the existing signal as trustworthy while
building read/unread state or alert lights on top of it.

**The UI gives NO signal when the server dies.** Covered in section 2. It is the
same family as the two above: an absence of bad news rendered as good news.

**THE 2026-09-07 INCIDENT: a session the owner closed and restarted vanished
from every view.** Fixed in `c9271da`, which IS deployed. Two independent
causes, and both are the shape this section is about:

1. **The restart resumed a `claude_session_uuid` whose transcript did not
   exist**, so the pane died instantly with **status 127** while the row still
   read `lifecycle=running`. This is the incident that bought
   `RESPAWN_TRANSCRIPT_MISSING` and `refuse_if_transcript_missing`.
2. **Close set `archived_at`.** Recents is
   `lifecycle='stopped' AND archived_at IS NULL`, so a closed session missed
   Recents and was then in no list at all.

**The hand recovery is recorded because it changed live data.** The pane was
respawned onto the correct transcript, and two rows were repaired in the live
database: **row 42** was emptied and retired (`claude_session_uuid = NULL`,
archived), and **row 43** took the live tmux linkage (`archived_at = NULL`,
`lifecycle = running`, `tmux_name = cloude_Agent_-_Cloude_Code`,
`tmux_created_epoch = 1788813811`). The repair had to CONSOLIDATE rather than
copy, because `claude_session_uuid` is UNIQUE and two rows cannot hold the same
value. Rollback SQL is on the mini at `/tmp/cc_row_rollback.sql`; `/tmp` is not
durable across a reboot, so treat that file as expiring rather than as an
archive.

**The archived-projects notice used to be one of these and no longer is.**
`loadProjects()` latched `_archivedFetchOk = false` on the error path and
returned WITHOUT re-rendering, so the screen kept the last successful paint and
read `showing archived: 1` after a failed fetch. Fixed in `5c88fdd`. The lesson
is the reusable part: **the three-outcome state existed in the MODEL and never
reached the SCREEN.** Latching a third state is not the same as rendering it.
Assert on the DOM, not on the variable.

---

## 7. HOW TO VERIFY ANYTHING HERE

**THE THREE-OUTCOME RULE.** Every check has three outcomes: pass, fail, and
could-not-evaluate. The third is not a flavour of the other two. Collapsing it
into pass invents a verdict nobody measured; collapsing it into fail burns the
alert's credibility until people stop reading it. Name the third state out loud
(`CANNOT DETERMINE`, `unreachable`, `unknown`) and make it reach the roll-up.
Ask of anything you write: what does this return when the thing it measures is
unreachable, absent, or was never created? If the answer is the same as when
everything is healthy, it is not finished.

**Run a POSITIVE and a NEGATIVE control before trusting a measurement.** A count
of zero from a query that has never been shown capable of returning non-zero is
a CANNOT DETERMINE, not a pass. Two real instances from this work:

- **The `--chrome` flag test.** `claude --chrome --version` AND
  `claude --chrom --version` both exit 0 and print the version, because
  `--version` short-circuits before option validation. A flag test that passes
  for a flag that does not exist is not a flag test. The real test is
  `claude --chrom mcp list` returning
  `error: unknown option '--chrom' (Did you mean --chrome?)` while
  `claude --chrome mcp list` runs.
- **The `pgrep -P` safety guard.** A recovery script's guard - "refuse if a
  claude is still running under the default socket" - was built on
  `pgrep -P <pane_pid> -f claude`. In the Claude Code sandboxed shell,
  `pgrep -P` returns NOTHING even when `ps -eo pid,ppid` plainly shows the
  child. `pgrep -x zsh` works, so pgrep itself runs; only `-P` is blind. The
  same call over `ssh` DID return children, so it is a property of the local
  shell, not the machine. The guard reported the coast clear and the script
  started a SECOND claude on a transcript another process was still writing.
  Rewritten on `ps -eo pid,ppid,command`, and proven in both directions: with
  the other claude alive it REFUSES and names exactly one pid; against a
  snapshot with that pid removed it PASSES. **A guard that cannot fail is worse
  than no guard.**

Two more environment-specific traps, both of which manufacture false results
inside the verification step itself:

- **A Chrome MCP tab reports `document.hidden === true` even when fronted.**
  That freezes rAF, so a settle loop never resolves, a CSS transition stays at
  `currentTime: 0` and `getComputedStyle` reads the pre-transition value
  forever, and physical clicks do not reach the element at all (while
  `element.click()` does fire the handler). Assert `!document.hidden` before
  trusting any measurement through it, or measure in a headed Playwright
  chromium instead.
- **Never re-run an init function to test whether it ran.** Calling
  `initArchivedVisibleToggle()` again attached a SECOND listener, so one click
  fired both handlers, flipped the state twice and landed back where it started,
  which reads exactly like a dead control. The wiring had been correct the whole
  time. Re-running an init mutates the thing you are measuring.

**A DOM-text assertion cannot see whether a glyph actually rendered.** Where a
defect is visible to a human, the test has to fail on the pixel: a computed
style check, a measured bounding rect, or a screenshot someone looks at.
`textContent` and element-presence assertions prove the DOM is right and prove
nothing about what rendered.

**Verification against a live system the owner is using is READ-ONLY by
default.** If a check genuinely requires mutating state to observe an effect,
record the value before you touch it, restore it after, and SAY in your report
that you did both. The 2026-09-06 archiving verification did exactly that: the
mutation was recorded before it was made and reverted after, and the report says
so. An earlier session cycled all 23 themes against the live app and left the
owner's theme on whichever one it stopped at, with no prior value on record to
restore.

**Tests and syntax.** `venv/bin/python3 -m pytest -q` from the repo root; system
python3 has no fastapi. **Baseline as of 2026-09-08: 3 failed / 4874 passed /
12 skipped**, the three being `test_home_write_guard`,
`test_state_dir_resolution` and `test_version_probe`, all environmental and all
pre-existing. Node: 169 files, only `test_archive_full_page_mode.node.mjs`
fails. **The number in `CLAUDE.md` was stale by an order of magnitude** because
the repo `venv` symlink pointed at a deleted `venv.nosync`, which is the exact
failure `CLAUDE.md` warns about: the suite limps along undercounting instead of
failing outright. Check the symlink before trusting any count that looks
nothing like the above. `node --check` every JS file you touch. A fresh git
worktree has NO `config.json` (it is gitignored), and its absence manufactures
collection errors that look exactly like pre-existing code bugs - seed it from
`config.example.json` before trusting any baseline measured in a worktree.

---

## 8. CURRENT GIT STATE

Branch `v1.1`, pushed to `origin/v1.1` as of this session (git-workflow
protocol followed: `git pull --rebase` then push, no force). HEAD is
`6934965`, three commits past `8ee40d1` (`117823d..6934965`). **Another
agent was deploying this HEAD to the mini as this section was written -
say "deploy in progress at time of writing" rather than confirmed, and
re-check the live commit directly before trusting either claim.** The
last state actually CONFIRMED live is still what `8ee40d1` left running:
boot held 19 sessions (18 plus 1 benign skip), every running session
carries a project, session lists show interactive conversations only.

**What rides the next deploy** (i.e. is committed but was not yet
confirmed live as this was written): the LED halo's second, smaller
recalibration (`e7a212e`), the `question`/`notice` split
(`cc885d6`), and the hook-token self-heal (`6934965`). None of the three
needed a database change, so a deploy of this HEAD is a straight code
push - no migration, no backfill, nothing to run against `cloude.db`.

**WHAT TO DO FIRST NEXT SESSION, in this order:**

1. **Confirm the deploy landed.** Check the live commit directly (not a
   log line that only reports what the deploy script believed) before
   trusting anything in this file that says "deployed" for `e7a212e`,
   `cc885d6`, or `6934965`.
2. **The real-hook LED integration test is DONE, landed and run.**
   Commit `3af3a3d` ("test(status): assert the led against hooks a real
   claude actually fired") adds `tests/test_led_real_hooks.py`,
   `tests/real_hook_harness.py`, `tests/real_hook_app.py`,
   `tests/real_hook_assertions.py`, and `tests/led_state_for.node.mjs`,
   pushed to `origin/v1.1`. Run with
   `CLOUDE_REAL_HOOK_TESTS=1 venv/bin/python3 -m pytest -q
   tests/test_led_real_hooks.py` (needs claude, tmux, and node on PATH;
   without the env var every test in the file skips, naming what went
   unmeasured). Real run: 9 passed in 49.5s, driving a real
   `SessionManager` plus real routes plus a real `/ws/terminal` under
   uvicorn, a real claude fed the production `_build_hook_block()` via a
   temp settings file, and every assertion piping `GET /sessions/list`
   through the shipped `client/js/status-led.js` under node. It measured
   two new open defects, added to the punchlist in step 5 below and
   spelled out in `TODO.md`'s "real-hook led integration test closed
   out, 3af3a3d" section: (a) `SubagentStop` re-arms `working` after
   `Stop` on a turn with no subagent, stealing the `finished_unread`
   window down to about 1.5s; (b) a dead pane reaches no live endpoint,
   so the led's `dead` state is unreachable from live data and a killed
   session just vanishes from the sidebar instead of showing dead.
3. **Get the owner's sign-off on the LED gallery states**, now against
   the SECOND recalibration. Reference artifact:
   https://claude.ai/code/artifact/aac4e1df-56aa-44e7-a444-6d1e1fc48627 -
   it enumerates every inner/outer LED combination. The owner already
   confirmed the shape after two rounds of shrinking (final tokens: 1.3x
   halo, 1.5px glow, lit object under 15px at the 9px default,
   `e7a212e`); what remains is his confirmation of the individual STATES
   rendered in the gallery, which he said he would do when using it.
4. **Verify and close items 4 and 11, or write down what's still
   wrong.** Both are BUILT, neither is verified this round. Item 4
   (alert lights / activity-after-resume): the narrower defect on record
   - `activity_state` staying `working` for about four minutes after a
   resume - is now addressed by the 120-second hook-fed expiry plus the
   fix that stops a raw tmux `running` pane from ever mapping to
   `working` on no evidence. Reproduce a resume and time it before
   closing. Item 11 (read/unread per session): the mechanism is built and
   documented (`CLAUDE.md`, "Unread is keyed on the INSTANCE") - keyed on
   the tmux instance so a reused name can't inherit a dead session's
   flag, set on `Stop`, cleared when a WS terminal binds. What's
   unconfirmed is the owner-facing half - entering a session marks it
   read, and there is a control to mark it unread again from the sidebar.
   Click through that by hand before closing.
5. **Then the remaining punchlist**, oldest-numbered first since they are
   independent of each other: item 1, item 2b, item 3
   (`sessions.agent_type` persistence for a session started via
   `auto_start_claude:false` plus a hand-sent claude command), item 7
   (toasts must be visible ACROSS sessions - raise global, dismiss
   per-session), item 8 (a toast history page), item 9 (group everything
   including pinned - owner decision still outstanding), item 22
   (close-and-recreate), and the websocket push (last, deliberately -
   re-measure whether polling is still the real cost once the event loop
   and render-guard work is further along). Full definitions in
   `TODO.md`'s "Session and agent identity" / "Attention, toasts and
   sidebar" sections and this final round's closing entry. **Also add**
   the two defects the real-hook led test measured (item 2 above):
   `SubagentStop` re-arming `working` after `Stop` on a subagent-less
   turn, and a dead pane being unreachable from any live endpoint so the
   led's `dead` state has nothing to render against - full detail in
   `TODO.md`'s "real-hook led integration test closed out, 3af3a3d"
   section.
6. **Then the infra debt**, lowest urgency: INFRA-49 (tests against the
   live `cloude` socket are measured FLAKY, not just risky - one test in
   this final round's own baseline run flaked this way, see `TODO.md`'s
   closing entry), gitleaks not installed on the mini, the
   `~/.config/restic/mini-m4.pw` plaintext password awaiting the owner's
   rotation decision, `FALLBACK_PROJECTS_ROOT` hardcoding
   `/Users/jsugamele` (`src/core/project_directory.py:85`), the deferred
   rename-push retry (a push deferred on a measured-missing transcript is
   never retried), 48 test files over the 500-line guideline, `--name`
   dropped on a restart's resume, `SessionInfo` carrying no
   `created_at_epoch`, and the database backups needing deletion (six
   4.6GB `cloude.db.bak-*` copies as of the last count - four are named
   in `TODO.md`'s closing entry for this round, two more ("the
   sessionkind and projectbind ones") are known to exist but have no
   filename recorded anywhere in this repo's docs; list the actual state
   dir before deleting anything).

Full commit-by-commit list and item mapping for THIS final round (3
commits, `117823d..6934965`) is in `TODO.md`'s dated 2026-09-08 "final
round closed out" section, including the re-measured test baseline
(5274 passed / 3 failed / 12 skipped, matching `6934965`'s own commit
message) and a correction to the node test file count (185 tracked
files, not the 190 `cc885d6`'s commit message claimed). **That 12
figure is now stale**: `3af3a3d` (the real-hook led integration test,
step 2 above) landed 9 opt-in tests that skip without
`CLOUDE_REAL_HOOK_TESTS=1`, so the current baseline is 5274 passed / 3
failed / 21 skipped.

Full commit-by-commit list and item mapping for the late round (12 commits,
`455d692..8ee40d1`) is in `TODO.md`'s dated 2026-09-08 "late round closed out"
section. The consequential ones, newest first:

| commit | what it did | deployed? |
|---|---|---|
| `6934965` | a superseded hook token is recovered once, never re-minted (self-heal for the 4,325-rejection storm) | in progress at time of writing |
| `cc885d6` | `question` split into `question` (blocked, PermissionRequest) and `notice` (not blocked, Notification) | in progress at time of writing |
| `e7a212e` | LED halo recalibrated a second time per owner feedback: 1.7/0.3 down to 1.3 scale / 1.5px fixed glow | in progress at time of writing |
| `8ee40d1` | LED halo shrunk to match its actual 9px render size (~35px peak down to ~21px) | yes |
| `e8cbc79` | docs: herdr teardown candidates, the real-hook LED test idea | n/a, docs |
| `d6e4883` | docs: status light findings, plus a project-tree gutter alignment fix | yes |
| `4215ad0` | restored the two-ring LED, byte-for-byte, after `3732bdf` reverted it in passing | yes |
| `3732bdf` | wired the recent section's collapse toggle; ALSO reverted 15 LED files as a side effect (see the hazard below) | yes |
| `0fc23a5` | a two-ring LED (inner = chat status, outer = activity/attention), and the tmux `running` fallback stops claiming unmeasured `working` | yes (via `4215ad0`'s restore) |
| `2b0ed7d` | `sessions.kind` (interactive/automated/unknown, schema v25); lists show the owner's work, not the machine's | yes |
| `026c7ac` | wording purge: archive/remove replaces delete/deleted everywhere a person reads it | yes |
| `4ae9965` | docs: import noise counts, the dead epoch fallback, attribution invariant closed | n/a, docs |
| `e73c1a7` | an archived catch-all project must not swallow every session (attribution invariant, part 2) | yes |
| `7bd55fd` | a session may never hold a project id and say it has none (attribution invariant, part 1) | yes |
| `ee3133c` | deep links accept fork labels and other free-form names | yes |

Older, from earlier in the day, kept for the record:

| commit | what it did | deployed? |
|---|---|---|
| `1a28b23` | docs: document the startup-gate ledger constructor | n/a, docs |
| `fadeb66` | boot: a session entered during re-adopt keeps being current | NO |
| `2b93428` | restart: an imported row with no tmux session can now be restarted (one gets created) | NO |
| `dcf8b02` | rename: stop reporting a push as landed when the resumed transcript was not there yet | NO |
| `9cdcb90` | status: detect a session parked on an unanswered startup prompt (punchlist 19) | NO |
| `b18f018` | import: bring in every real conversation this app had never accounted for (punchlist "import") | NO, script; the 895-row import it ran is a database change, not a deploy |
| `bca7069` | boot: hold every surviving session, not just the last one (punchlist 17) | NO |
| `ee5d547` | perf: stop repainting the whole project tree every 5s (punchlist 13) | NO |
| `24d25b9` | groups: key group membership on the session, not a recycled tmux name (punchlist 10) | NO |
| `06bacd6` | naming: one name per session, read the one typed into the pane | NO |
| `9adaac9` | deploy: mirror src/client instead of merging with ditto (punchlist 23) | NO |
| `a4eeef1` | projects: a new project can pick its own folder (punchlist 14, 18a) | NO |
| `2071963` | lineage: a second claude under one pane is not a fork of ours | NO |
| `bc65ef4` | upload-sweeper: read the real `ProjectsView.writable` attribute (punchlist 24) | NO |
| `43e8fc2` | sidebar: group menu no longer paints behind the sidebar panel (punchlist 6) | NO |
| `9009588` | folder-picker: wrap a long path instead of overrunning its box (punchlist 18) | NO |
| `8dd54a8` | lineage recovery for the conversation id the `SessionStart` hook failed to record, plus the dry-run backfill tool | YES, deployed earlier today |
| `0793eb1` | the restart picker's option text no longer paints over the next row | YES, deployed earlier today |
| `0b12edf` | a restart RESUMES the same conversation, on every rung that can | yes |
| `f95a9ed` | restart a session whose pane is still ALIVE, `respawn-pane -k` in place | yes |
| `83b6377` | the picker's `max-height` got its `dvh` twin | yes |
| `32052d1` | pick the wrapper at restart, and land back in the session. **Committed by a SECOND agent, see the hazard below** | yes |
| `c9271da` | restart resumed a transcript that does not exist, and close left the row nowhere (section 6) | yes |
| `8212e30` | the group chip left the sidebar row, its action moved into the kebab | yes |
| `cddc823` | the row icons folded into one borderless kebab, three ways to open it | yes |
| `c779afb` | the deploy script repaired: tar over ssh, a clean tree that deploys, self-verifying bytes (section 2) | yes |
| `a6b6b91` | the theme bleed: every navigation now owns the theme | yes |

Also today, with no commit behind them because they are database operations,
not code: 895 conversations imported as archived sessions (59 archived
projects created, 13 scratch conversations excluded, 224 `agent-*.jsonl`
subagent files correctly identified as non-sessions and skipped, 319 files
with no recoverable cwd) - dry-run report at
`.claude/notes/import-dry-run-2026-09-08.md`; `agent_type` filled on 15
running rows; 8 conversation-id fills applied; the three "phantom pair" rows
corrected (see section 10's new correction below); two ghost rows retired.
None of this needed a deploy - it is already true of the live database.

**HAZARD: two Claude Code sessions were editing this branch at once.** A second
session worked `v1.1` concurrently for part of 2026-09-07 and committed
`32052d1`. It has since exited. Nothing was found clobbered, but two agents on
one branch is how work gets clobbered, and the second agent's commits are
indistinguishable from the first's in the log. If something in this range does
what you did not expect, that is the likely explanation. Before starting work
here, check that no other session is live on the branch.

**HAZARD, confirmed again 2026-09-08 late round: eight to ten agents committed
on this one branch in the same day.** Two real incidents, both recovered: a
stale-tree commit (`3732bdf`) that reverted 15 files of unrelated LED work
(fixed in `4215ad0`, see the commit table above), and separately a
`git reset --hard` that dropped a commit outright. **The rule going forward:
every agent builds its commit from HEAD plus its own hunks via a private git
index, verifies `git diff origin/v1.1 --stat` shows only the paths it actually
touched before committing, and never runs `git reset --hard` on this branch.**

**`a6b6b91`, the theme bleed, is worth one line of mechanism** because the
shape recurs: three copy-pasted theme restores in `app.js`, plus two
session-entry paths written as `if (pinned) apply()` with NO else. A missing
else is not a missing feature, it is state left over from the last thing. There
is now ONE total function, `applyForTarget()`, in
`client/js/theme-navigation.js`, and every navigation goes through it.
---

## 9. OWNER DECISIONS - the settled ones, then the ones still open

The settled ones are not to be relitigated.

- Sync stays iCloud. His words: "i want icloud" / "stay with icloud".
- He will not work the same session on two computers.
- `OP_SERVICE_ACCOUNT_TOKEN` rotation is deferred by his explicit decision:
  "not until we finish this project. because it will happen again." Rotation is
  a 1Password WRITE and needs him.
- Codex OpenAI sign-out is his: "ill take care of that part when ready."
  `auth.json` and the three sqlite files holding live bearer tokens were
  deliberately left untouched.
- The RSA private key and `MESH_PASS`: "put it in the backlog."
- Merging the `(old path)` project rows directly in the database is authorised:
  "this is a me thing it can be done via database". **Take a verified backup
  first - every pre-existing backup in that directory is 65-115 KB against a
  4.5 GB database and would be useless as a rollback.**
- Installing the browser extension for `--chrome` is his call.
- CloudeCode DB copies in the NAS archive: "no i dont need them."

**Browser control, and the only route to it.** Claude in Chrome is PAIRED and
INSTALLED (`pairedDeviceName = Browser 2`,
`hasCompletedClaudeInChromeOnboarding = true`), but
`claudeInChromeDefaultEnabled = false`, so browser tools exist ONLY in a session
launched with `--chrome`, which is the `claude-chrome` wrapper. Tools BIND AT
SESSION START, so browser control cannot be added to a conversation already
running. **The wrapper picker is the intended route: restart a session and
choose `claude-chrome`.** That is what the picker was built for.

### DECISIONS STILL WAITING ON HIM, updated 2026-09-08 evening

Two of the three that were open this morning are now CLOSED, one not as
originally planned.

1. **CLOSED.** The 8 confident backfill FILLs (rows 14, 15, 16, 17, 19, 23,
   24, 25) were authorised and written.
2. **CLOSED, but not as a merge.** Re-examination of the 4 "duplicate" pairs
   (7/4, 9/11, 10/12, 38/39) found the earlier note had it backwards: the
   LIVE rows in 9/11, 10/12 and 38/39 already held the real conversation ids,
   and it was their dead twins holding phantom ones. The dead twins were
   retired (archived), not merged over the live rows. Ghost rows 44 and 47
   (no session or transcript behind them) were retired too. Remaining
   phantom-uuid rows: 41 (kept deliberately) and 46 (a newborn session whose
   transcript has not landed yet - re-check it, do not treat it as settled).
3. **CLOSED.** Deploying today's commits is done, not in progress. Live runs
   `8ee40d1` (the late round, `455d692..8ee40d1`); confirmed by boot holding
   19 sessions and every running session carrying a project. See section 8.

**Opened earlier in the day:**

4. Rotate `~/.config/restic/mini-m4.pw` (a plaintext restic repository
   password, surfaced during the Desktop backup inventory) into 1Password,
   and decide whether restic's scope should widen beyond `ai-setup` and
   `docker-management` to cover the Desktop. Owner's call on both, per the
   standing decision to defer credential rotation until this project is
   finished. Still open.
5. **CLOSED.** Delete `~/Desktop/Backups` on this Mac - the owner deleted it
   himself, along with the two empty test folders left over from the day's
   verification work.

**NEW, opened in the late round, none started:**

6. Confirm the LED states rendered in the reference gallery
   (https://claude.ai/code/artifact/aac4e1df-56aa-44e7-a444-6d1e1fc48627),
   in particular the resized halo shipped in `8ee40d1`. This is the top item
   on next session's list in section 8.

---

## 10. CORRECTIONS CARRIED FORWARD

Recorded rather than quietly dropped, per the convention that a wrong entry plus
its correction beats a clean lie.

- **The Brave extension finding was WRONG.** The claim that the Anthropic
  extension was "installed in none of Brave's three profiles" read Brave's
  `Preferences`; Chromium keeps extension settings in `Secure Preferences`.
  Re-measured there, `fcoeoabgfenejglbffodgkkbkcdhcgfn` IS installed under Brave
  `Profile 1`. An extension absent from `Preferences` is not evidence of
  absence.
- **The three "missing hook scripts" were a FALSE ALARM.** 84,217 files scanned
  on the mini, zero references. The on-screen errors are transcript attachments
  from 2026-04-23 being replayed by `--resume`. The related claim that
  `archive-context.sh` feeds `claude_session_uuid` was also wrong; that is a
  whole subsystem fed by the managed curl hooks POSTing to
  `/api/v1/hooks/claude-event`, which is live and firing.
- **The re-measured lag figure superseded the first one.** An early sample said
  a stall every ~20s lasting ~9.5s. The clean-baseline measurement on the quiet
  box gives 12.05s every 20.0s, and by the time it was fixed the stall had
  grown to 14.5s. It scaled with the file, which is itself the evidence it was
  the pragma.
- **The openrsync diagnosis was WRONG**, and the corrected version is in
  section 2. `deploy-mini.sh` did not fail because openrsync rejects
  `--files-from=- --relative`; it handles that fine, proven by a successful
  copy. It failed on a remote destination containing SPACES. Both live
  destinations contain spaces and the v11 staging path does not, so
  **`--target live` could never work while `--target v11` always did**, which
  is the asymmetry that let it hide.
- **The tmux identity premise was WRONG.** Killing and respawning does NOT move
  the instance triple. Measured on tmux 3.7c: `session_created` held at
  **1788821572** and `pane_id` at **`%0`** across `respawn-pane -k`, and only
  `pane_pid` changed. The reason is structural rather than lucky:
  `session_created` belongs to the SESSION and `-k` replaces the PANE'S
  PROCESS. `session_instance_rekey.py` measures it either side anyway, because
  a measurement that can stop being true is not a thing to assume.
- **"Media Compression's conversation is gone" was WRONG, and the app said it
  too.** The transcript `82854c0e-a423-4591-a34f-a14cb92fbf41.jsonl` exists and
  is 73,190,422 bytes. The mechanism is the phantom-uuid trap in section 4: the
  app's message was locally truthful about a uuid that had nothing to do with
  the session.
- **The `CLAUDE.md` pytest baseline was stale by an order of magnitude**, and a
  broken `venv` symlink is why. Corrected numbers in section 7.
- **`com.imc.cloude-code` is the wrong agent name** for this app, in
  Infrastructure `CLAUDE.md` hazard 40. The live one is
  `com.cloudecode.menubar`.
- Earlier in the migration: UTC timestamps were read as local and led to a wrong
  conclusion about which sessions post-dated the row-reuse fix (they PREDATE it
  by 1h38m); a proposal to delete all 6 archived DB rows would have orphaned
  lineage, because rows 4 and 5 are parents of live sessions and roots of
  archives holding 24,790 records; and a token-minting shell quoting bug made
  both endpoints return 401 while the parser read the error body as an empty
  list, producing a false zero.
