# CloudeCode - Task Tracker

> **APPEND ONLY.** Nothing in this file is ever deleted or rewritten. Mark items
> complete. When a past entry turns out to have been WRONG, add a new
> `SUPERSEDED` or `CORRECTION` entry rather than editing the old one - the wrong
> entry plus its correction is more useful than a clean lie.
>
> **Heading convention:** `### YYYY-MM-DD - short title - OPEN|DONE|SUPERSEDED`
>
> **Split out 2026-09-07** from
> `/Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs/Sync/Development/Assistants/Infrastructure/.claude/TODO.md`.
> The original entries were LEFT IN PLACE there, because that file is
> append-only history and deleting from it is not permitted. Infrastructure now
> covers the fleet (24 hosts, monitoring, backups); this file covers the app.
> Where an item still has a live Infrastructure index number it is tagged
> `INFRA-nn` so it can be traced back.
>
> **Path spelling.** Always write the long iCloud spelling:
> `/Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs/Sync/Development/CloudeCode`.
> `~/Development` is a symlink into iCloud and Claude Code derives its transcript
> directory from the LITERAL cwd string, so two spellings of one directory
> produce two transcript dirs. That is exactly how the `Mac (old path)` and
> `Hirschfeld (old path)` project rows came to exist.
>
> **Voice:** no em-dashes, no en-dashes, no emojis, anywhere, including commit
> messages. Date every factual claim. Write CANNOT DETERMINE rather than guess.

---

## AGREED PRIORITY ORDER

Set 2026-09-06, and nothing since has changed it:

1. **Unblock the event loop.** Run `py-spy dump --pid <server>` DURING a stall
   FIRST, to name the query. Do not write a fix before that; the query is
   currently CANNOT DETERMINE. Item 2 / the LAG entry.
2. **The project-tree render guard.** `renderProjectList()` has no signature
   guard and rebuilds 794 DOM nodes every 5 seconds. Item 13 / the render entry.
3. **Push updates over the existing websocket instead of polling.** Last,
   deliberately: `src/api/websocket.py` carries no project or session-list
   message type at all, so this is real work, and the poll may not be the real
   cost once (1) is done. Re-measure before designing it.

---

## OPEN - index

One line each, newest measurement wins. Detail follows below in full.

- **2. FIXED AND DEPLOYED 2026-09-07.** `PRAGMA integrity_check` is off the
  request path. Measured on live: p99 **14,505.7 ms -> 13.3 ms** (1,090x), p50
  30.5 -> 5.2 ms, the 20.0s / 14.5s stall cadence GONE, 86.5% of the window
  stalled -> 0.0%. Corpus ingester verified still ingesting with a positive
  control (row counts rose). Commit `3c23309`. See the DONE entry for the full
- **22. HALF DONE 2026-09-07.** The DRY-RUN RUNG PREVIEW is built and the shell
  landmine is no longer silent: `GET /sessions/restart/preview` reports the rung
  a session would land on without acting, twice - `unchanged` (what a restart
  does now) and `projected` (what it would come back AS, liveness ignored) -
  plus `pane_state` as its own fact. A projected rung is a PREDICTION, NEVER A
  PERMISSION. Also landed: an optional `agent_type` on `POST /sessions/respawn`
  so a session can be moved onto another launch wrapper without hand-editing
  `sessions.agent_type`, and a return-to-session step after a successful
  restart. STILL OPEN: close-and-recreate for a LIVE session, which needs a new
  row re-carrying attribution, theme, unread, filing and position. A live pane
  still answers `not_dead` and the picker refuses it.
- **23.** The deploy copies with `ditto`, which MERGES. The first commit that
  DELETES a file will leave the stale copy on BOTH targets and still report
  success, because verification only hashes files that should be present.
  Latent until something is removed. LOW effort, do it before a deletion ships.
- **2b. MEASUREMENT NOTE 2026-09-07.** A later 30s poll after the `cddc823`
  deploy read p99 **64-72 ms** across two runs, not the 13.3 ms recorded right
  after the fix. Stable, no stall pattern, still 3 orders of magnitude better
  than the 14,505 ms broken state, but ~5x the earlier figure and NOT
  attributable. The corpus ingester was active during both later runs, which is
  a hypothesis and was not tested. Re-measure on a quiet box before treating
  13.3 ms as the number.
  measurement and how the deploy was verified.
- **21. NEW, HIGH.** `scripts/deploy-mini.sh` is BROKEN on current macOS: both
  machines ship Apple openrsync, which rejects `--files-from=- --relative`
  with `server receiver mode requires two argument`. It fails safe (copies
  nothing) but every future deploy needs a hand tar-over-ssh until fixed.
### The lag and the render path
- **2.** Tab switching and keyboard extremely laggy. Root cause found and
  measured 2026-09-05: synchronous SQLite on the asyncio event loop, 71.4
  percent of main-thread samples inside `sqlite3_step`, a 12.05s stall every
  20.0s, loop unavailable ~60 percent of wall clock on an IDLE box. WHICH query
- **2a. ANSWERED 2026-09-07.** The query is `PRAGMA integrity_check` at
  `src/core/db.py:177`, reached from the async `GET /api/v1/version` handler
  (`version_routes.py:207` -> `:178` -> `db_health.py:94`), run synchronously on
  the loop against 4.5 GB, polled every 20s by the ELECTRON TRAY
  (`macOS/main.js:1588` `INTERVAL_MS = 20000`). Confirmed twice independently:
  py-spy 17/17 stalled dumps byte-identical, plus static analysis. It is a
  REQUEST HANDLER on a timer, not a background task. Stall has grown 12.05s ->
  14.3s and scales with the file. FIX NOT BUILT: threadpool alone is
  insufficient, the pragma does not belong on a status endpoint.
  is CANNOT DETERMINE. `py-spy dump` mid-stall is the next step. PRIORITY 1.
- **13.** Home screen polls every 5s and `renderProjectList()` repaints from it
  with NO guard: 385 elements + 409 text nodes destroyed and recreated per tick
  at 15 projects / 17 sessions, ~45 listeners re-attached, including while the
  launchpad is hidden behind a terminal. No `clearInterval` anywhere. PRIORITY 2
  (the guard) and PRIORITY 3 (the push channel).

### Project creation, all three reproduced live 2026-09-06
- **14.** The typed project name is never used for the directory. Owner
  requirement: he must be able to CHOOSE where a project lives, including the
  subfolder.
- **15.** `POST /api/v1/sessions` cannot set a title. `CreateSessionRequest` has
  no `label` field. Every UI-created session lands `title = NULL`. Fix is one
  field and three lines.
- **16.** Attribution races project creation by 15 milliseconds, so the session
  lands `project_id NULL / attribution 'none'` and nothing ever re-probes.
- **17.** A server restart unbinds every session but one; they render
  "adoptable" rather than "running" until opened. Do NOT fix by making
  `/sessions/list` read the database.

### From owner screenshots 2026-09-07
- **18.** The folder path overruns its box in the project modals and cannot be
  read to the end. `div.folder-picker-path` has `width: 100%` and no
  `word-break` / `overflow-wrap` / `overflow` (`styles.css:2641`); the sibling
  `.folder-picker-status` already carries the fix. Its `:focus` and
  `::placeholder` rules are DEAD on a div. VERIFIED in code.
- **18a.** Same screenshot shows the SHORT cwd spelling
  `/Users/jsugamele/Development/...`, the exact symlink trap that manufactured
  the two `(old path)` project rows. UNVERIFIED whether the modal resolves it on
  submit; it definitely displays the short form. More serious than 18.
- **19.** A session parked on an unanswered folder-trust prompt over stale
  diagnostic scrollback (`CLAUDE-NOT-FOUND` / `PATHIS`), and the sidebar showed
  it as `Connected` with a PID and no signal. Neither probe string exists in
  this repo, so the scrollback is almost certainly replayed. UNVERIFIED, owner
  says it may be obsolete. Reproduce before spending time.
- **20.** A pinned theme bleeds across a session switch: session A pinned to
  Darcula left Darcula applied to session B, which pins nothing. Likely a
  missing else-branch (theme applied on open, never reset on leave). The home
  screen case is UNTESTED and the owner flagged it. UNVERIFIED, reported from
  live use 2026-09-07. Verify on the pixel, not the API value.

### Session and agent identity
- **1.** Sleep and resume: EXPOSE full-vs-summary as a deliberate option on wake.
  Do not ship the suppression as the fix.
- **3.** Sessions display as "maybe claude" or "unknown". `sessions.agent_type`
  is NULL on rows 11-14 while the API reports `claude-skip-permissions`. Fix the
  persistence, not the display.
- **The respawn ladder gates on tmux `#{pane_start_command}`.** A pane born as a
  bare shell lands on `RESPAWN_SHELL`, so the restart button hands you a login
  shell instead of claude. Needs BOTH a non-empty start command AND a stored
  `agent_type`. OPEN as a product defect, not just a per-session fact.
- **The SessionEnd hook is cancelled on every clean exit, and
  `reconcile_lifecycle` cannot cover for it.** A row reads `running` over a pane
  whose agent has exited. Measured 2026-09-06 and seen again 2026-09-07.

### Attention, toasts and sidebar
- **4.** Alert lights need looking into. Related and narrower than item 4 now
  reads: `activity_state` reads `working` for about four minutes after a resume,
  then self-corrects.
- **6.** Sidebar group ellipsis popup renders BEHIND the sidebar.
- **7.** Toasts must be visible ACROSS sessions. Raising should go global;
  dismissal must STAY per-session. Two different axes.
- **8.** A toast HISTORY page. Server already holds the record; this is a read
  view, not new persistence.
- **11.** Read / unread state per session. Shares one state model with items 4
  and 7, or the three will disagree.

### Sidebar, groups and projects
- **5.** Merge the `(old path)` and new-path projects. API CANNOT do it. Owner
  authorised a direct database edit. Take a VERIFIED backup first.
- **9.** Group everything, including pinned sessions. Question outstanding for
  the owner about the ungrouped Infrastructure sessions.
- **10.** Durable ORDER WITHIN a group. Needs a `position` column on
  `session_group_members`, done in the SAME change as re-keying that table from
  `tmux_name` to `session_uuid`.
- **12.** Archive a PROJECT when finished. Sessions have `archived_at`; projects
  now do too as of `a89c919`, so this is largely DONE - see the DONE section.
- Register the ~26 dormant projects as archived. Unblocked 2026-09-06.
- There is no sessions UNARCHIVE endpoint. Projects have one; sessions do not.
- **Startup race, cosmetic.** Cold load with show-archived already on paints the
  button pressed before the notice lands.

### Connection state and deploy
- **The UI gives NO signal when the server dies.** The supervisor gives up after
  3 unexpected stops, so a dead server can STAY dead while the page looks normal.
- **`deploy-mini.sh` deploys CHANGED files, so a clean tree deploys nothing** and
  prints `nothing to deploy`, which reads like success. Fix it rather than work
  around it.
- Push the 8 unpushed `v1.1` commits.

### Archive and message browser (opened 2026-08-31 / 2026-09-02)
- **INFRA-99.** Export download cannot authenticate from a browser. Needs a
  short-lived single-use download ticket. Owner's call: it puts a credential in
  a URL.
- **INFRA-101.** Corpus 1 has `manifest_sha` NULL yet 19,545 of 19,548
  transcripts claim `host_attribution manifest_verified`.
- **INFRA-102.** Widen ingest-side secret detection.
- **INFRA-103 / 114 / 121.** Where the corpus permanently lives, and making the
  moved copy readable. The 21 GB database is on the mini at
  `/Users/jsugamele/ClaudeArchive/` and is NOT readable by the production app.
- **INFRA-105.** 46 subagent transcripts (12.2 MB) are in NO archive; the
  ingester does not walk `workflows/`.
- **INFRA-109.** Reclaim the 3.78 GB of duplicate ingest. Prevention shipped at
  schema v22; reclaim not attempted.
- **INFRA-115.** tmux resurrect/continuum. A restored session gets a NEW tmux
  creation epoch and session identity is the triple, so a naive restore orphans
  rows. Needs a design pass.
- **INFRA-116.** `src/core/db_steps.py` (1,124+ lines) and
  `src/core/message_gate_contract.py` (906 lines) exceed the 500-line cap.
- **INFRA-117.** `tests/test_secret_gate_parity.py` failed once across three
  full runs and passed 10 of 10 in isolation. Cause CANNOT DETERMINE.
- **INFRA-100 / 110.** The archived-secret inventory is undercounted, and it
  feeds the deferred credential rotation. OWNER-GATED: he asked to be consulted
  on secrets separately. Kept in BOTH files on purpose.
- **INFRA-112.** The mini leaks a CloudeCode hook token in plaintext `ps`
  arguments.

### Test-harness safety, all HIGH
- **INFRA-49.** The pytest suite still creates real tmux sessions on the live
  shared `cloude` socket, roughly 15 hardcoded socket-name literals, currently
  deselected rather than refactored.
- **INFRA-61.** ~28 standalone `scripts/verify_*.py` harnesses write into the
  owner's REAL `~/.claude/settings.json`. Not one sets
  `CLOUDE_CLAUDE_SETTINGS_PATH`, and at least twenty boot a real server.

### Older app items carried over
- **INFRA-53.** The copy chip reports a FALSE SUCCESS on real iOS, and the only
  candidate fix is UNVERIFIED (`0ed0b91`, branch `fix/copy-ios-false-success`,
  written by an agent that died before reporting).
- **INFRA-54.** TLS on the Tailscale hostname blocks four things; the `cld`
  wrapper; an unpushed PR to Adam; merging `integration/mobile-rc` to main.
- **INFRA-56.** `tests/manual_audio_check.md` documents removed UI elements.
- **INFRA-59.** `setup_auth.py` still hardcodes `PORT=8000` in its `.env`
  fallback template.
- **INFRA-60.** The effects-`inactive` terminal state is untested BY
  CONSTRUCTION - a check that can never fail.
- **INFRA-52.** claude-history residual gaps: the `audit.jsonl` adapter, the
  `(session_id, seq_in_file)` uniqueness blocker, and the history VIEWER.
- **INFRA-57 / 58.** Stale tmux sockets on mac-mini-m4 (~1500 `cc-locale-*` and
  four of unknown provenance). Host-side symptom of this app's test harness;
  the line stays in Infrastructure too.
- **The browser half of `--chrome` is NOT ready and it is the owner's to do.**
  Chrome is not installed on the mini (only Brave); the extension IS installed
  under Brave `Profile 1`.

### Added and updated 2026-09-08 - newest measurement wins

Nothing above is removed. These lines are the current reading where they
disagree with an older line.

- **DEPLOY STATE.** Live on mac-mini-m4 runs **`0b12edf`**. Committed AND
  PUSHED but **NOT DEPLOYED**: `0793eb1` (picker layout) and `8dd54a8` (lineage
  recovery plus backfill). **Nothing is unpushed**, which supersedes the "8
  commits unpushed" lines below and in `HANDOFF.md` section 8.
- **25. NEW, HIGH. The conversation id is missing on 16 of 39 session rows.**
  Two independent defects, both traced: `SessionStart` is the ONLY writer on
  the create path and never retries (25 failures across 20 sessions in the live
  log), and `slugify_project_dir` mapped only `/` and `.` when the real rule
  maps everything outside `[A-Za-z0-9-]`, so the fallback ladder had NEVER ONCE
  fired on this machine. Fix committed `8dd54a8`, NOT deployed. `agent_type`
  does NOT share this cause, so item 3 stays separate.
- **26. NEW. Five rows carry a uuid whose transcript does not exist, and all
  five COLLIDE** with a sibling row split off by the cwd spelling trap. Pairs
  7 to 4, 9 to 11, 10 to 12, 38 to 39. Over 39 rows: 16 absent, 18 sound, 5
  phantom. Merging them is the already-authorised item 5 family and still needs
  a VERIFIED backup first.
- **27. NEW. Deploy `0793eb1` and `8dd54a8`**, and authorise the 8 confident
  backfill FILLs (rows 14, 15, 16, 17, 19, 23, 24, 25). Dry run only so far,
  nothing written.
- **2. DONE AND DEPLOYED.** The lag is fixed. p99 14,505.7 ms to 13.3 ms.
  Caveat 2b stands and has grown: later polls read 64-72 ms and most recently
  66-73 ms, so **13.3 ms should not be treated as settled.**
- **20. DONE 2026-09-07, deployed.** The theme bleed. `a6b6b91`.
- **21. DONE 2026-09-07, deployed.** `c779afb`. **The openrsync diagnosis in
  the line above was WRONG**: the failure was a remote destination containing
  SPACES, not `--files-from`. See the 2026-09-08 corrections entry.
- **22. DONE 2026-09-07, deployed.** Part 2 shipped: `f95a9ed` restarts a live
  pane in place, `0b12edf` makes every rung that can resume the same
  conversation. The picker layout fix `0793eb1` is committed and NOT deployed.
- **CORRECTION to the respawn-ladder line above.** 18 of 22 live sessions have
  an empty `pane_start_command` (was 15 of 19 on 2026-09-07), BUT an explicit
  wrapper choice OVERRIDES that gate (`session_respawn.py:542`). Only an
  UNPICKED restart lands on the shell rung.
- **CORRECTION.** The tmux identity premise was wrong: `respawn-pane -k` does
  NOT move the instance triple. Measured on tmux 3.7c, `session_created` held
  at 1788821572 and `pane_id` at `%0`; only `pane_pid` changed.
- **CORRECTION.** "Media Compression's conversation is gone" was wrong. The
  transcript is 73,190,422 bytes and present.
- **CORRECTION.** The `CLAUDE.md` pytest baseline was stale by an order of
  magnitude because the `venv` symlink pointed at a deleted `venv.nosync`. Real
  numbers: **3 failed / 4874 passed / 12 skipped.**
- **24. UPDATED.** `'ProjectsView' object has no attribute 'read_only'` is now
  56 occurrences since 2026-08-29 and fires twice per boot. Pre-existing.
- **`--chrome` browser control: the route is the wrapper picker.** The
  extension is paired and installed but `claudeInChromeDefaultEnabled = false`,
  and tools bind at session start, so the only way in is to restart a session
  and choose the `claude-chrome` wrapper.
- **HAZARD.** A second Claude Code session was editing branch `v1.1`
  concurrently on 2026-09-07 and committed `32052d1`. It has exited. Two agents
  on one branch is how work gets clobbered.

---

## 2026-09-04 - CloudeCode backlog, after the session migration completes - OPEN

Recorded from the owner during the MacBook-to-mini migration. All of these are
POST-migration. He is testing the new daily-briefing agent now and wants
sessions moved a couple at a time so he can retire the local app as he goes.

The wording of items 1 to 5 is the OWNER'S OWN, preserved from the
Infrastructure TODO. Do not paraphrase it.

1. **Sleep and resume: he wants the OPTION, not the suppression.** Earlier work
   found `CLAUDE_CODE_RESUME_TOKEN_THRESHOLD` (default 100000) and
   `CLAUDE_CODE_RESUME_THRESHOLD_MINUTES` (default 70) gate the
   "resume from summary vs full" dialog, plus a persisted `resumeReturnDismissed`
   in `~/.claude.json`. Both env vars are UNVERIFIED - found in the binary, never
   exercised. He has since said he does NOT mind the warning; he wants the choice
   surfaced. So the work is to EXPOSE full-vs-summary as a deliberate option on
   wake, not to suppress the prompt. Do not ship the suppression as the fix.
   Note the mini runs claude 2.1.260/261 with native background-session
   primitives (`attach`, `logs`, `respawn`, `stop`, `rm`) that may replace the
   hand-built sleep entirely - evaluate before building.

2. **Tab switching and keyboard still extremely laggy.** The earlier diagnosis
   blamed machine load (UrBackup at 94 percent CPU, disk 95 percent full, 18
   pinned snapshots). All three are now resolved and he reports it is STILL bad,
   so that explanation is insufficient. The live structural finding stands and
   was never disproven: the server runs SQLite SYNCHRONOUSLY on its asyncio event
   loop, so any query blocks every other request. Sampling showed a stall every
   ~20s lasting ~9.5s. `corpus_ingest_task.py:223` already does it correctly with
   `asyncio.to_thread`; the blocking path does not. Re-measure on the now-quiet
   box before fixing - a clean baseline is what was missing.

   MEASURED 2026-09-05, see the LAG entry below. The re-measure was done and the
   stall is 12.05s every 20.0s, not ~9.5s.

3. **Sessions display as "maybe claude" or "unknown".** The `sessions.agent_type`
   DB column reads NULL on rows 11, 12, 13 and 14 (Mac, Hirschfeld, Personal
   Assistant, daily-briefing) while the API reports `claude-skip-permissions`
   with `agent_family_source: wrapper`. Every session created via
   `auto_start_claude:false` + a hand-sent claude command lands this way, because
   the agent_type wrapper only applies on auto-start. The flag IS on the running
   processes. So the stored value and the displayed value disagree with reality
   in different directions. Fix the persistence, not the display.

4. **Alert lights need looking into.** Owner-reported, not yet diagnosed. Note
   the related earlier finding: `activity_status` read `working` on Hirschfeld
   while its pane was idle at a prompt - stale state left from the resume render.
   May be the same defect or may be separate; do not assume.

   NARROWED 2026-09-06: `activity_state` read `working` for roughly four minutes
   after a resume while the pane sat idle at an empty prompt, then corrected
   itself to `question` at 21:49:38. On that evidence the stale `working` is real
   and SELF-CLEARING, not stuck - a narrower defect than item 4 as written.

5. **Merge the "(old path)" and new-path projects.** Projects 3 and 4 are
   `Mac (old path)` and `Hirschfeld (old path)`, retired 2026-09-03 because their
   root used the non-iCloud symlink spelling which maps to a different Claude
   transcript dir. Projects 5/6 superseded them. The API CANNOT do this: `PATCH
   /api/v1/projects/{name}` explicitly never rewrites `projects.root`, and DELETE
   returns HTTP 500 on a FOREIGN KEY constraint because archived session rows
   still reference them. Owner has explicitly authorised doing it directly in the
   database ("this is a me thing it can be done via database"). Take a verified
   backup first - note every pre-existing backup in that directory is 65-115 KB
   against a 4.5 GB database and would be useless as a rollback.

   ALSO, from `handoff-2026-09-06`: `launchpad.js:5599` launches new sessions
   with `project.path` (= `raw_path`), NOT `root`. A project-row correction that
   fixes only `root` is worse than nothing. Spot-checked 2026-09-07: that call
   site now reads `working_dir: project.path` at launchpad.js:5871, so the line
   number has drifted; the fact has not.

Also outstanding from earlier, unrelated to the above:
- 25 of the 31 sidebar sessions still to migrate, a couple at a time.
- The connection-state indicator: the UI gives NO signal when the server dies.
  Demonstrated during the 2026-09-04 live deploy - the supervisor gives up after
  3 unexpected stops, so a dead server can STAY dead while the page looks normal.
- Codex OAuth sign-out is the owner's to do; `auth.json` and the three sqlite
  files holding live bearer tokens were deliberately left untouched.
- Monthly rules review and a pitfalls/lessons file for the daily-briefing repo,
  agreed but not yet written. Trigger on ELAPSED time since last review, never on
  a calendar day - he misses days and a date-triggered review would skip silently.
  (That last bullet belongs to the daily-briefing repo, not this one; kept here
  because it was recorded in the same block.)

### 2026-09-05 - two more CloudeCode UI items for the post-migration backlog - OPEN

6. **Sidebar group ellipsis popup renders BEHIND the sidebar.** Clicking the
   ellipsis on a group (to close/collapse it) opens a menu that is obscured by
   the sidebar itself. Stacking-context or z-index problem, likely the menu being
   a child of a container that establishes its own stacking context. Note the
   sidebar gained `inert` + `visibility:hidden` on its CLOSED state (commit
   `7bf95e5`) - check that fix did not contribute before assuming it is
   pre-existing.

7. **Toasts must be visible ACROSS sessions.** The owner wants to see that
   another session needs attention while he is looking at a different one.
   The toast work in commit `a945771` made AUTO-DISMISS per-session, which is
   correct and should stay - typing into session A must not clear session B's
   toast. But he reports the toasts are not SHOWING across sessions in the first
   place. Determine whether visibility is currently scoped to the active session
   and, if so, make raising global while keeping dismissal per-session. These are
   two different axes and it would be easy to fix one by breaking the other.
   Toasts are server-backed with no dwell timer and require an ack, so a
   cross-session toast will persist until answered - which is the desired
   behaviour here, but confirm it does not stack unboundedly across 20+ sessions.

8. **A toast HISTORY page.** Owner: "we should keep a history log that i can look
   at in case i miss something, maybe a history page." Cross-session toasts
   (item 7) mean more of them land while he is elsewhere, so a scrollback of what
   was raised and what he did about it. Good news on feasibility: toasts are
   already SERVER-BACKED and ack-required (no dwell timer, they survive a reload
   and are re-delivered by the attach backfill until acked via
   `POST /api/v1/toasts/<id>/ack`), so the server already holds the record - this
   is a read view over existing state, not new persistence. Show: raised-at,
   session, type (`Stop` / `Notification` / `PermissionRequest`), the text, and
   how it was resolved (acked by the user, auto-dismissed by session input, or
   cleared by Dismiss all). Distinguishing those three matters - "I answered it"
   and "it got swept by Dismiss all" are different facts and collapsing them
   makes the history useless for the exact case he wants it for.

9. **Group everything, including pinned sessions.** Established 2026-09-05:
   group membership is DB-backed and travels between devices; pinning is
   per-device `localStorage` (`cloude.session.sidebar.arrangement`) and does not.
   A session that is BOTH pinned and grouped shows under Pinned where it was
   pinned and falls back to its group elsewhere - which is the desired behaviour.
   So the migration should assign a group even to pinned sessions, making the pin
   purely additive. In the owner's own sidebar, `Agent - Developer` and
   `Agent - N8N` are already both pinned AND in `Agents`; the rest of the pinned
   block is ungrouped. Ask him whether the ungrouped Infrastructure sessions
   (HA, HA Smart Git, Corporate, Athena, Claude Dev Move, Claude Move Mac Mini)
   want a group of their own rather than sitting loose.

10. **Durable ORDER WITHIN a group.** Measured 2026-09-05 against the live
    schema: `session_groups` HAS a `position` column and a reorder endpoint, so
    GROUP order is DB-backed and travels between devices. `session_group_members`
    has only `tmux_name` (pk), `group_id`, `added_at` - NO position. The store's
    own docstring says it: "members: tmux names filed in this group, in no
    meaningful order - the sidebar's own manual order decides how they are
    drawn", and that manual order is per-device `localStorage`. Owner wants his
    own durable order inside a group. Needs a `position` column on
    `session_group_members`, an endpoint to set it, and the client sorting on it
    instead of the local arrangement. **Do it in the same change as re-keying
    that table from `tmux_name` to `session_uuid`** - the current key is a latent
    defect (two sessions sharing a tmux name would share one membership row); it
    measured clean for his 31 titles but the table is being altered anyway.

11. **Read / unread state per session.** Owner: entering a session marks it read
    and it greys out if nothing is happening in it; he wants to be able to mark
    it unread again from the sidebar. Note this interacts with item 7
    (cross-session toasts) and item 4 (alert lights) - all three are "does this
    session want my attention", and they should share one state model rather than
    three that can disagree. Note also `activity_status` has already been seen
    reading `working` on an idle pane (stale state after a resume render), so the
    existing signal is not trustworthy as-is.

12. **Archive a PROJECT when finished.** Owner wants to retire a whole project,
    not just a session. Sessions already have `archived_at`; projects do not.
    Relevant constraint found 2026-09-05: a project that has ever hosted a session
    CANNOT be deleted through the API - `DELETE /projects/{name}` returns HTTP
    500 on a FOREIGN KEY constraint because `sessions.project_id` still
    references it, and `PATCH /projects/{name}` explicitly never rewrites
    `projects.root`. So project archival has to be a soft-delete column plus
    filtering, the same shape sessions already use, and it pairs naturally with
    the "show archived" toggle.

    **LARGELY DONE 2026-09-06** in `a89c919` plus `5c88fdd`. See the DONE
    section. No migration was needed: `projects.archived_at` has existed since
    the original DDL and the list query already filtered on it; only the write
    path, API and UI were missing. What remains is registering the ~26 dormant
    projects as archived.

13. **Home screen polls every 5s; owner noticed it visibly refreshing and asked
    whether it is generating traffic.** It is: `launchpad.js` ~387
    `setInterval(loadRunningSessions, 5000)` hitting `/sessions/attachable` and
    `/sessions/list` on every tick, and `renderProjectList()` repaints from that.
    With 17+ sessions this is measurable. He raised moving to PUSH updates
    (Svelte-style) rather than polling. Worth noting `src/api/websocket.py`
    carries NO project or session-list message type at all - the websocket is
    per-session terminal I/O only - so there is no push channel for state today
    and adding one is real work, not a config change. See also item 2: the server
    runs SQLite synchronously on the asyncio event loop, so every poll competes
    with terminal websocket frames on the same loop. Fix the blocking first and
    re-measure before designing a push layer; the poll may not be the real cost.

### 2026-09-05 - THE LAG: root cause found and measured - OPEN, PRIORITY 1

**Synchronous SQLite running as a coroutine on the asyncio event loop. The
server freezes for 12 seconds out of every 20, on an IDLE machine.**

Measured on the quiet box (all earlier load explanations - UrBackup, 95 percent
disk, 18 pinned snapshots - are resolved and were insufficient):

    sample, 30s:   main-thread samples 19,484
                   inside sqlite3_step 13,913  =  71.4 percent
    stack:         uvloop Loop__run -> uv__run_idle -> Handle__run -> task_step
                   -> pysqlite_connection_execute -> sqlite3_step
                   (a coroutine, running a synchronous query, on the loop)

    /health at 10Hz, 60s:  n=155  min=9ms  p50=30ms  p90=70ms
                           p99=11,966ms  max=12,767ms
    STALLS at 11:08:45, 11:09:05, 11:09:25, 11:09:45
    - exactly every 20.0s, each 12.05s long.

The loop is unavailable ~60 percent of wall-clock. Context: `cloude.db` is
4.68 GB with a 195 MB WAL and 5,598,436 rows in `transcript_records`.

**Why this IS the typing symptom.** There is no local echo -
`terminal.js:312 term.onData -> ws.send`, and characters only appear via
`term.write()` fed by PTY output returning over the same socket. So every
keystroke round-trips through the loop in BOTH directions. 8 seconds on,
12 seconds off, then a catch-up flush. That is the owner's description exactly.

**RULED OUT, with method:**
- Simultaneous multi-session streaming (the owner's own hypothesis). Only ONE
  terminal is attached at a time - `terminal.js` ends in a SINGLETON
  `new Terminal()` holding one `this.ws`, force-closed on every switch path
  (`connectToSession` :811, `reconnectToExistingSession` :948, `pauseForHome`
  :2375). Measured server-side: 2 established TCP connections from the browser,
  not 17. Does not scale with session count.
- Network latency: stalls measured from 127.0.0.1, p50 30ms.
- Machine load: box quiet, min 9ms proves the server is fast when the loop is free.
- Poll traffic volume: ~5 req/s of small JSON; `/sessions/list`,
  `/sessions/attachable` and `/sessions/records` all correctly use
  `run_in_threadpool` - they are VICTIMS of the stall, not the cause. The "home
  screen keeps refreshing" is four polls' worth of results landing in a clump
  when the block releases, not a repaint storm.

**CANNOT DETERMINE: which query.** `sample` renders CPython frames as
`_PyEval_EvalFrameDefault` so the Python call site is invisible. One 30-second
read-only step closes it: `py-spy dump --pid <server>` during a stall. Two
unproven candidates: `session_manager.record_hook_event()` called synchronously
un-awaited at `routes.py:1976` inside an `async def` handler (a real structural
defect either way), or something scanning `transcript_records`. No 20s-cadence
timer exists in the code, so the periodicity is itself unexplained.

Also unexplained and worth noting: the `-wal` at the canonical path is deleted
and recreated at 0 bytes at each stall while the server holds an fd on a 195 MB
unlinked WAL inode - the signature of connection open/close per call.

**The correct shape already exists in this repo:** `corpus_ingest_task.py:223`
does it with `asyncio.to_thread`. The blocking path does not. Do the `py-spy
dump` FIRST; do not write a fix against a query nobody has named.

### 2026-09-05 - The project tree rebuilds 794 DOM nodes every 5 seconds, unguarded - OPEN, PRIORITY 2

Separate, secondary, and real. `renderRunningSessions()` HAS a working signature
guard that correctly excludes ticking values. `renderProjectList()`
(`launchpad.js:3976`, wholesale `innerHTML =` at :4120) has NO guard at all -
measured at 15 projects / 17 sessions: 385 elements + 409 text nodes destroyed
and recreated per tick, plus ~45 event listeners re-attached, unconditionally,
including while the launchpad is hidden behind a terminal. Called twice with
byte-identical data it rebuilds both times.

Consequences measured: keyboard focus in the tree is destroyed every 5s (a
SECOND focus-destruction mechanism, separate from the sidebar one fixed in
`7bf95e5`); an in-progress rename input is destroyed when any unrelated session
changes state; `:hover` is dropped and fades back in over a 300ms transition,
which is the visible pulse under the cursor. Scroll reset RULED OUT (not scroll
containers). Terminal container never touched.

Fix: give it the same signature guard. Nothing in that tree is time-relative, so
there is no ticking value to build the guard around. Verify by asserting the
MUTATION COUNT, not that the signature stopped changing - a signature that never
changes and a renderer that ignores it look identical from outside.

Also: there is no `clearInterval` anywhere in `launchpad.js`. The poller is armed
once at :152 and runs forever, explicitly not pausing on tab hide.

**LINE NUMBERS HAVE DRIFTED - spot-checked 2026-09-07 on the `v1.1` tip.**
`renderProjectList()` is at launchpad.js:4155, the wholesale `innerHTML =` at
:4318, and the 5-second `setInterval` at :454, in a 6,069-line file. The other
figure recorded for the poller, `~387` in item 13 above, does not match either.
The FACTS all still hold - `grep -c clearInterval` over launchpad.js returns
**0** - but re-derive any line number before quoting it. Both recorded values
are kept here rather than corrected away, per the append-only convention.

### 2026-09-05 - CORRECTION: the three "missing hook scripts" were a false alarm

`archive-context.sh`, `notify-done.sh`, `enforce-slimRules.sh` do not exist on
EITHER machine and are referenced by NOTHING - 84,217 files scanned on the mini,
zero hits; the mini's `settings.json` Stop array holds exactly one hook (the
cloudecode curl). The error text visible in panes is a STORED TRANSCRIPT
ATTACHMENT from 2026-04-23, repainted by `--resume` every time those sessions
are resumed. The only file that ever referenced them is
`settings.json.backup-20260513-170340`. Copying them would have created dead
files and changed nothing on screen.

The related claim that `archive-context.sh` feeds `claude_session_uuid` was ALSO
wrong. That is a whole subsystem (`hook_contract.py`, `claude_hooks.py`,
`claude_session_correlate_ladder.py`, `session_claude_correlate_bind.py`,
`db_steps.py`) fed by the managed curl hooks POSTing to
`/api/v1/hooks/claude-event`, which is live and firing. Why specific rows are
NULL is CANNOT DETERMINE and needs the correlation ladder's own outcomes.

### 2026-09-06 - project creation is broken three ways, all reproduced live - OPEN

The owner created "Fantasy Football 2026" through the UI. Three separate defects,
each with a file:line, all hit in one action. Repaired by hand (project 17 and
sessions 38/39); the CODE is still broken and will do it again on the next project.

14. **The typed project name is never used for the directory.** `launchpad.js:4781-4786`
    sends `{auto_start_claude, copy_templates, project_name, cols, rows}` with **no
    `working_dir`**. `routes.py:600` mints `session_id = f"ses_{uuid4().hex[:8]}"`,
    and `session_manager.py:2758-2763` falls back to
    `settings.get_working_dir() / session_id` when no working_dir is supplied,
    creating an EMPTY dir named `ses_121ceb6f`. `launchpad.js:4820-4824` then
    registers the project as `{name: typed_name, path: session.working_dir}` -
    the typed name bolted onto a machine-minted path.
    There is NO name-to-slug code anywhere; `FantasyHockey2026` came from the
    FOLDER-PICKER route (`launchpad.js:5481 saveProjectWithUniqueName` with a
    `selectedPath`), i.e. the owner chose it himself. Known-recurring: a
    `ses_ec5bf2a3` path is fossilised as test data in
    `tests/test_project_authority.py:71` and `project_writes.py:14`.
    **OWNER REQUIREMENT 2026-09-06: he must be able to CHOOSE where a project
    lives, including the subfolder.** The picker already exists on the other
    route - the new-project flow should use it, or accept an explicit
    `working_dir`. Three outcomes required on the derived path: if the target
    directory already exists and is non-empty, that is an explicit refusal or a
    declared reuse, never a silent adopt.

15. **`POST /api/v1/sessions` cannot set a title.** `CreateSessionRequest`
    (`src/models.py`) has no `label` field; `routes.py:619-630` calls
    `create_session(...)` with nine kwargs and no `label=`, while the fork route
    (`:314`) and restart route (`:506`) both pass it. `create_session` itself
    ACCEPTS `label` (`session_manager.py:2662`) - only this one route declines to
    pass it. Every session created through the UI or the API lands with
    `title = NULL`. Every one of the ~29 sessions migrated that day needed a
    follow-up `PATCH /sessions/{id}/name` to work around it. Fix is one field and
    three lines.

16. **Attribution races project creation by 15 milliseconds.** Session row 38 was
    written at `20:21:56.978851Z`, its project row at `20:21:56.993415Z` - so the
    session attributed against a projects table that did not yet contain its own
    project, landed `project_id NULL / attribution 'none'`, and nothing ever
    re-probes. `project_writes.create_project` (`project_writes.py:277`) does no
    backfill of existing sessions. Either register the project BEFORE spawning the
    session, or have `create_project` attribute pre-existing sessions whose
    `working_dir` falls under the new root.

Related, same session: the project was registered under the `~/Development`
SYMLINK spelling, which is exactly why projects 3 and 4 were retired on
2026-09-03 (it maps to a different Claude transcript dir than the resolved iCloud
path the pane actually runs in). Any auto-derived path must use the resolved
iCloud spelling.

17. **A server restart unbinds every session but one, and they render as
    "adoptable" rather than "running" until opened.** `session_manager.py:889`
    rehydrates AT MOST ONE session on startup; the rest keep their live tmux pane
    but lose their in-process backend. `/sessions/list` (`routes.py:766-785` ->
    `list_session_infos` -> `_session_info_for`, `session_manager.py:3693-3698`)
    returns only sessions WITH a live backend, so an unbound one is absent from it
    and present in `/sessions/attachable` instead. Nothing is lost - the sidebar
    merges both - but after every restart, any session the owner does not open sits
    in that state. Observed 2026-09-06: `cloude_Fantasy_Hockey_2026`, unbound since
    the 2026-09-04 12:05 restart, 17 of 18 sessions having been opened since.
    **Do NOT fix by making `/sessions/list` read the database** - its contract is
    "a websocket attach will succeed here", and widening it would make it promise
    something false. Fix in the client merge: an `/attachable` row carrying a
    non-null `session_row_id` whose DB `lifecycle='running'` is a KNOWN session
    with a live pane, not an unknown external tmux, and should render as running.

### 2026-09-06 - the SessionEnd hook is cancelled on exit, and lifecycle cannot catch it - OPEN

Seen during the football bounce: `SessionEnd hook [(cat | curl ...)] failed:
Hook cancelled`. Not a misconfiguration and not harmful in that instance, but
it exposes a real false green.

**The hook channel itself is fine, proven positively.** The SAME hook plumbing
was working minutes later from the new process: `sessions.activity_state_at` on
row 39 advanced at 21:45:18, 21:49:18 and 21:49:38 with the state moving
`working -> working -> question`. So this is a SHUTDOWN RACE, not a broken
hook - SessionEnd by definition fires while claude is exiting, its command is a
`curl -sS -m 2`, and claude cancels pending hooks as it tears down.

**Why it still matters.** If the SessionEnd POST never lands, CloudeCode is
never told the agent ended. `reconcile_lifecycle` cannot cover for it, because
that reconciles against the TMUX LISTING - whether the tmux session exists - and
a pane whose claude exited still has a live tmux session. Measured exactly that:
between `/exit` and the relaunch, tmux was alive, no claude process existed, and
row 39 still read `lifecycle = running`. Nobody noticed because a relaunch
followed within a minute. On an ordinary exit the row would sit at `running` over
an empty pane indefinitely - a session the UI calls alive with nothing in it.
Same family as the connection indicator that gives no signal when the server
dies.

**SEEN AGAIN 2026-09-07.** Row 40 read `lifecycle = running` the whole time its
pane was DEAD (pane exited status 0 at 07:51), for the same reason: the tmux
session existed, only the pane died. Two independent ways to reach the same
false green, so a fix has to reconcile against the PANE and the agent, not the
tmux listing.

### 2026-09-06 - project archiving verified live, one false green found and fixed - DONE + OPEN

**DONE - verified in the owner's own browser against live.**
Toggle discoverability PASS (rendered unconditionally, never gated on there
BEING archived rows - knowing whether any archived rows exist would need a
second fetch of the very rows the toggle excludes, so the control announces
itself instead). Round trip PASS end to end: archived `Mac (old path)` live,
default `GET /projects` returned 16 without it, `include_archived=true` returned
17 with `archived_at: 2026-09-06T21:12:30Z`, UI read `showing archived: 1`, then
unarchived and the state was restored exactly (17 projects, 0 archived,
preference key removed). The test mutation was recorded before it was made and
reverted after.

**DONE - a false green INSIDE the feature built to prevent false greens.**
`loadProjects()` latched `_archivedFetchOk = false` on the error path and then
returned WITHOUT re-rendering, so the screen kept whatever the last successful
fetch had painted. Measured live with `API.getProjects` stubbed to throw:
internal state went to `false` and the notice on screen still read
**"showing archived: 1"**. A confident count left standing after the request
that would have told you had failed. Fixed by one `this.renderProjectList()` in
the catch (`5c88fdd`), deployed to BOTH live locations, verified by fetching
`/static/js/launchpad.js` over HTTP and hashing against the local file rather
than by `git rev-parse`. All three states then proven live:

| toggle | fetch | rendered |
|---|---|---|
| off | not asked | nothing at all |
| on | ok | `showing archived: 0` (or N) - a measured fact |
| on | failed | `CANNOT DETERMINE - archived projects could not be loaded. This is NOT a claim that there are none; the list below may be stale or incomplete.` |

The generalisable point: the three-outcome state existed in the MODEL and never
reached the SCREEN on the one path it was built for. Latching a third state is
not the same as rendering it - assert on the DOM, not on the variable.

**OPEN - startup race, cosmetic, low priority.** On a cold load with the
show-archived preference already on, the first paint uses a non-archived fetch
and the archived-aware one lands a second or two later, so the button reads
pressed while the notice is briefly absent. Does not produce a false claim (an
absent notice is the "not asked" state, not a false zero), but it is a real
ordering bug between the preference read and the first `loadProjects()`.

**NEW TRAP - Chrome MCP clicks in a background tab, and re-running an init.**
Physical clicks did not reach the button at all while `document.hidden` was
true; `element.click()` fired the handler fine. Then calling the wiring function
again to "re-wire" it attached a SECOND listener, so one click fired both,
flipped the state twice and landed back where it started - which reads exactly
like a dead control. The wiring was correct the whole time
(`initArchivedVisibleToggle()` is called from `initSectionDisclosures()`,
launchpad.js:3439). Assert `!document.hidden` before believing any click-driven
measurement through that MCP, and never re-run an init function to test whether
it ran - that mutates the thing you are measuring.

**OPEN - now unblocked.** Registering the ~26 dormant projects as archived was
blocked on archiving being live. It is live and round-trip proven, so it can
proceed. `Mac (old path)` and `Hirschfeld (old path)` are the obvious first two -
their own descriptions say "Retired 2026-09-03". They were NOT archived that day;
the test archive was reverted.

### 2026-09-06 - claude-chrome wrapper staged on the mini for Fantasy Football - DONE (staged), OPEN (browser half)

Owner asked for `--chrome` either added to the existing wrapper or put in a new
one with the football chat pointed at it. Took the SECOND option: a new wrapper
`claude-chrome`, leaving the default `claude-skip-permissions` untouched, so
only the one project changes rather than every session on the box.

**What is in place**
- **Wrapper `claude-chrome`** added through the running server's own API
  (`POST /api/v1/agents/wrappers`), so config.json and the in-memory config
  cache agree - a hand edit to config.json would NOT have been seen, because
  `Settings.load_auth_config` caches in `_auth_config_cache`.
  Script: `command claude --dangerously-skip-permissions --chrome "$@"`,
  rendered to `~/Library/Application Support/CloudeCode/agent_wrapper_scripts/claude-chrome.zsh`.
- **DB**: `projects.default_agent_type = 'claude-chrome'` on project 17
  (Fantasy Football 2026), and `sessions.agent_type = 'claude-chrome'` on rows
  38 and 39. Prior values were captured live first - all NULL except session 38,
  which was `claude-skip-permissions`. Revert SQL is on the mini at
  `~/football_chrome_revert.sql`.
- Verified in-process against the live config: the project resolves to
  `claude-chrome`, and the CONTROL (`Infrastructure`, no default) still resolves
  to `claude-skip-permissions` - nothing else moved.

**`--chrome` is a real flag, proven with a negative control.**
`claude --chrome mcp list` runs; `claude --chrom mcp list` returns
`error: unknown option '--chrom' (Did you mean --chrome?)`. Worth recording HOW
that was established, because the obvious test is worthless: `claude --chrome
--version` AND `claude --chrom --version` both exit 0 and print the version,
because `--version` short-circuits before option validation. A flag test that
passes for a flag that does not exist is not a flag test.

**THE RESTART BUTTON WILL NOT DO THIS, and that is a finding in its own right.**
`respawn_session` gates on tmux's `#{pane_start_command}`, and for
`cloude_Fantasy Football 2026` that field is **EMPTY** - the pane was born as a
bare shell and claude was typed into it (which is why the row reads
`agent_family_source: not_launched` with no `agent_type`, backlog item 3). The
ladder therefore lands on `RESPAWN_SHELL` and would put a LOGIN SHELL in that
pane, not claude, with or without the wrapper. A session needs BOTH a non-empty
`pane_start_command` AND a stored `agent_type` to reach `RESPAWN_AGENT`.

**OPEN - the browser half is NOT ready, and it is the owner's to do.**
`--chrome` will launch, but there is nothing on the mini for it to connect to.
Measured: **Google Chrome is not installed** (only Brave). The native messaging
host IS registered for both Chrome and Brave and its binary exists at
`~/.claude/chrome/chrome-native-host`.

**CORRECTION 2026-09-06 to the Brave finding above.** The claim that the
Anthropic extension `fcoeoabgfenejglbffodgkkbkcdhcgfn` was "installed in none of
Brave's three profiles" was WRONG, and the owner said so. It read Brave's
`Preferences`; Chromium keeps extension settings in `Secure Preferences`.
Re-measured there: the extension IS installed under Brave `Profile 1`. Left as a
correction rather than an edit. Lesson: an extension absent from `Preferences` is
not evidence of absence - check `Secure Preferences`, which is the protected
store Chromium actually writes.

### 2026-09-07 - a guard that could not see, and two writers on one transcript - DONE (recovered), LESSON

Recorded here because the mechanism is a product-adjacent hazard, not a fleet one.

**`pgrep -P` RETURNS NOTHING IN THE CLAUDE CODE SANDBOXED SHELL.** A session
recovery script's first guard - "refuse if a claude is still running under the
default socket" - was built on `pgrep -P <pane_pid> -f claude`. Measured on
mac-mini-m4, in that shell: **`pgrep -P 29423` returns NOTHING while
`ps -eo pid,ppid` plainly shows 29725 with ppid 29423**, and `pgrep -P 29422`
returns nothing for a tmux server whose child 29423 `ps` lists. `pgrep -x zsh`
works fine, so pgrep itself runs - only `-P` is blind. The same `pgrep -P` calls
issued through `ssh mac-mini-m4 '...'` DID return children, so this is a property
of the LOCAL sandboxed shell, not of the machine.

**Consequence, measured, not hypothetical:** the guard found nothing, reported
the coast clear, and the script respawned a SECOND claude onto the transcript a
still-running ssh copy was writing. Two writers on one `.jsonl` is the one thing
the whole staging design existed to prevent. Caught within about a minute; a byte
copy of the transcript was taken first (99 MB), then the second pid was killed.
**No damage: 36,196 lines, 0 invalid JSON, last record a normal attachment
carrying the right sessionId.**

**A guard that cannot fail is worse than no guard** - it reports a verdict it
never measured, and everyone downstream trusts it. Rewritten on
`ps -eo pid,ppid,command`, which is authoritative here.

Second bug found while fixing the first: matching `*claude*` against the whole
argv matched TRANSIENT SHELLS, because every Bash tool call in this environment
runs a zsh whose argv names `~/.claude/shell-snapshots/...`. That would have made
the guard refuse forever after the real claude had gone - the opposite failure,
equally silent. The predicate now tests column 3, the executable path, against
`claude` or `*/claude`.

Both directions proven before trusting it:
- POSITIVE control - with the ssh claude alive it REFUSES and names exactly one
  pid, the real one.
- NEGATIVE control - the same logic run against a snapshot with that pid removed
  PASSES, so it is not simply stuck on refuse.

Also relevant to this app: **CloudeCode only manages sockets it owns**, so a
session resumed by hand on the `default` tmux socket is invisible to it, and
**tmux cannot move a pane between servers**. The only route back is to let the
foreign copy exit and resume the same transcript inside the CloudeCode pane.

### 2026-09-06 - every shell CloudeCode spawns can run a Homebrew update - OPEN (workstation-side, affects session launch)

`~/.zshrc` sources `.profile_interactive`, which chains to
`~/.dotfiles/platforms/mac/.profile`, which calls **`my-update-mac`
unconditionally on every shell load** - and CloudeCode's own wrapper is
`zsh -c 'source ~/.zshrc ...; source <wrapper>.zsh "$@"'`, so a session launch
runs it too. Staging a session triggered it: it listed 8 outdated formulae and
reached `Need sudo to fix Homebrew Cellar permissions (may prompt for password)`
inside a non-interactive shell that can never answer.

It is not a per-launch tax - the script claims a 1-day timestamp reservation
first, so only the FIRST shell each day actually runs it and the rest skip
silently. But that first one is whichever session you happen to open first, and
it does system updates and blocks on a sudo prompt during that session's own
startup. **Worth suspecting as a contributor to a slow or hung first session
launch of the day.**

### 2026-09-07 - two items from owner screenshots - OPEN

Both raised by the owner from screenshots on 2026-09-07. What follows separates
what is VISIBLE in the images from what was VERIFIED in the code, because only
the first item was reproducible from the tree.

**18. The folder path overruns its box in the project modals. VERIFIED in code.**

Screenshot: the add-project modal with folder
`/Users/jsugamele/Development/Assistants/FantasyHockey2026`, the tail of the
string running past the right edge of the box with no wrap, no scroll and no
ellipsis. The user cannot read the end of their own path.

Mechanism, read 2026-09-07 on the `v1.1` tip. The path is NOT an input. It is
a `div.folder-picker-path`, rendered in two places in `client/js/launchpad.js`:
the edit-project modal at :4562 and the shared `pathHintHtml` at :5167. Its CSS
is `client/css/styles.css:2641` and it sets `width: 100%` with NO
`word-break`, NO `overflow-wrap` and NO `overflow`, so a long single-token path
simply spills.

The fix already exists eleven lines below it: `.folder-picker-status`
(`styles.css:2666`) carries `word-break: break-all` for exactly this reason.

Two things to fix in the same pass, because they are the same defect:
- The div is styled to impersonate an input (`cursor: text`, plus
  `.folder-picker-path:focus` at :2655 and `.folder-picker-path::placeholder`
  at :2661). Both of those rules are DEAD on a div and can never match. Either
  make it a real readonly input or drop the two rules; do not leave CSS that
  claims a behaviour the element cannot have.
- There is no `title` attribute on it, so hover reveals nothing either. A
  wrapped path is the fix; a tooltip is not a substitute on a phone.

Verify on the PIXEL, not the DOM. `textContent` will read the full path whether
or not it rendered inside the box. Assert the element's `scrollWidth` is not
greater than its `clientWidth`, or measure the bounding rect against the modal,
or take a screenshot someone looks at.

**18a. The same screenshot carries a more serious finding the owner did not
ask about: the SHORT cwd spelling.** The folder reads
`/Users/jsugamele/Development/Assistants/FantasyHockey2026`. `~/Development` is
a symlink into iCloud, and Claude Code derives its transcript directory from
the LITERAL cwd string, so that spelling produces a SECOND transcript directory
for a directory that already has one. This is the exact mechanism that
manufactured projects 3 and 4, `Mac (old path)` and `Hirschfeld (old path)`,
retired 2026-09-03. Any path the modal accepts or auto-derives must be resolved
to the long iCloud spelling
(`/Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs/Sync/...`)
before it is stored. UNVERIFIED whether the modal resolves it on submit; the
screenshot only proves it DISPLAYS the short form. Check
`launchpad.js` around the create path (`working_dir: project.path`, spot-checked
at :5871) before assuming either way.

**19. Session `ses_2b41d99f` parked on an unanswered trust prompt, over stale
diagnostic scrollback. UNVERIFIED, owner says it may be obsolete.**

Screenshot: session `ses_2b41d99f`, header `ses 2b41d99f`, sidebar group OTHER,
status `Connected`, PID 47672. The pane shows two commands as plain text,

    command -v claude || echo CLAUDE-NOT-FOUND
    echo PATHIS:$PATH

and then Claude Code's own first-run folder-trust prompt for
`/Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs/Sync/Development/Assistants/Media`,
sitting on `No, exit` and never answered.

**Neither probe string exists anywhere in this repo.** Grepped 2026-09-07
across `*.py`, `*.js`, `*.sh` and `*.zsh` excluding `node_modules`: zero hits
for `CLAUDE-NOT-FOUND` and zero for `PATHIS`. So they were typed by a human or
an agent during a past debugging session, which makes this most likely REPLAYED
SCROLLBACK rather than anything the app emits today. That is the same shape as
the "three missing hook scripts" false alarm: on-screen text from an old
transcript being repainted by `--resume`, mistaken for a live error.

What is NOT resolved by that explanation, and is the part worth reproducing:
- The `CLAUDE-NOT-FOUND` probe means somebody was chasing `claude` missing from
  PATH in a spawned tmux shell. Related to the `agent_type` NULL / respawn
  ladder family (backlog item 3, and the `RESPAWN_SHELL` gate on an empty
  `#{pane_start_command}`), but not the same bug and not shown to be current.
- A session that opens onto an unanswered trust prompt looks IDENTICAL in the
  sidebar to one that is working: `Connected`, a PID, no signal. Same family as
  the other false greens in this file, an absence of bad news rendered as good
  news.
- The session is titled `ses 2b41d99f` and sits under OTHER, not under a project
  name. Consistent with item 15 (`POST /api/v1/sessions` cannot set a title,
  every UI-created session lands `title = NULL`) and item 16 (attribution races
  project creation). Not new evidence, but a live instance of both.

Before spending time here: reproduce it. Open a NEW session against a folder
Claude Code has not trusted yet and see whether the app surfaces the prompt or
hides it. If it cannot be reproduced, close this as scrollback and keep only the
"a session parked on a prompt is indistinguishable from a working one"
observation, which stands on its own.

---
### 2026-09-07 - a pinned theme bleeds across a session switch - OPEN, UNVERIFIED

**20.** Reported by the owner 2026-09-07 from live use, not yet reproduced in
code. One session is pinned to Darcula. Switching to a DIFFERENT session in the
browser left Darcula applied to that other session, which does not have it
pinned. The owner also flagged, without testing it, that he does not know what
happens on returning to the HOME SCREEN.

Shape of the likely defect: `pinned_theme` rides on the `SessionInfo` WRAPPER,
not on `.session` (see the two-levels section of the root `CLAUDE.md`). If the
theme is APPLIED on session open but never RESET when the session is left, then
a session with no pinned theme of its own simply inherits whatever the previous
one set. That is a missing else-branch, not a wrong value: the code has a path
for "this session pins a theme" and no path for "this one does not, put it
back".

Reproduction to run before touching code, three steps and the third is the one
people skip:
1. Pin Darcula on session A. Open session B, which pins nothing. Does B render
   Darcula?
2. From B, go to the HOME SCREEN / launchpad. Does the launchpad render Darcula?
   The owner explicitly does not know this one.
3. Hard-reload on B. If B renders correctly after a reload but wrongly after a
   switch, the bug is in the switch path and not in what the server sends.

Verify on the PIXEL. `getComputedStyle` on a themed element, or a screenshot a
human looks at. Reading the `pinned_theme` VALUE off the API proves the server
is right and proves nothing about what rendered - the archived-notice defect
(`5c88fdd`) was exactly this: the state existed in the MODEL and never reached
the SCREEN.

Note the device-scope rule while testing so a wrong result is not manufactured:
group membership is DB-backed and travels between devices, but PINNING and
within-group order are `localStorage` and are per-device.

**Do not cycle themes to test this on the live app without recording the owner's
current theme first and restoring it after.** An earlier session cycled all 23
themes against live and left him on whichever one it stopped at, with no prior
value on record.

---
### 2026-09-07 - THE LAG: the query is NAMED. Confirmed twice, independently - OPEN (fix), CLOSED (diagnosis)

Item 2 / PRIORITY 1 was blocked on "WHICH query is CANNOT DETERMINE". It is no
longer cannot-determine. Two independent methods, run without knowledge of each
other's result, returned the same call site.

**THE CALL SITE**

    src/core/db.py:177        integrity_check
      conn.execute("PRAGMA integrity_check").fetchall()
    <- src/core/db_health.py:94      live_state
    <- src/api/version_routes.py:178 _datastore_block
    <- src/api/version_routes.py:207 get_version   (async GET /api/v1/version)

`GET /api/v1/version` runs `PRAGMA integrity_check` SYNCHRONOUSLY, inside the
coroutine, against the 4.5 GB `cloude.db`, on EVERY request. No
`run_in_threadpool`, no cache, no TTL, no guard.

**WHAT POLLS IT.** `macOS/main.js:1588` `const INTERVAL_MS = 20000`, fired by
`setInterval(tick, INTERVAL_MS)` at `:1604` -> `pollTraySignals()`
(`macOS/main.js:668`) -> `trayApi.fetchUpdateStatus()`
(`macOS/tray-api.js:267`), which calls `getAuthed('/api/v1/version')` with a
Bearer token, so it DOES reach the handler.

**It is a REQUEST HANDLER on a timer, not a background task.** Everything in
the earlier analysis that went looking for a periodic server-side loop was
looking on the wrong side of the wire. The timer is in the ELECTRON SHELL.

**EVIDENCE, method 1 - py-spy against the live process.** PID 48024, Python
3.14.7, `-m src.main`, ppid 48016 (the Electron shell under launchd
`com.cloudecode.menubar`). 25 dumps taken. **17 of 17 dumps that landed inside a
stall showed this stack byte-identical, zero variation.** 22 of 25 total were
`integrity_check`; the other 3 landed in the quiet window and showed an idle
`selectors.select`, which is what a healthy loop looks like. Timestamp
correlation is exact: a `/health` request starting `1788789574.36` took 14.52s,
and dumps 17-21 all fall inside that window and all show `integrity_check`;
dumps 14, 15, 22, 23 fall in fast windows (`/health` 0.02-0.04s) and all show
the idle loop. NO dump contradicts the pattern.

**EVIDENCE, method 2 - static analysis of the checkout.** Independently walked
`/api/v1/version` -> `_datastore_block` -> `live_state` -> `integrity_check` and
independently found the 20000 ms `setInterval` in `macOS/main.js`. Reasoned that
`PRAGMA integrity_check` re-verifies every page of every B-tree, which on 4.5 GB
is seconds of pure `sqlite3_step` - matching both the 12.05s duration and the
71.4% main-thread share.

**Why the period is EXACTLY 20.0s and not 20 + 12.05.** `setInterval` fires on a
fixed wall-clock cadence regardless of how long the fetch takes. A chained
`setTimeout` would have drifted. The fixed period was itself a clue and it
pointed at a timer, correctly.

**IT IS GETTING WORSE ON ITS OWN.** The stall measured 12.05s on 2026-09-05 and
14.1-14.4s on 2026-09-07, re-confirmed live with slow `/health` at `...835.34`,
`...855.52`, `...875.82` (deltas 20.18s and 20.30s). `integrity_check` scales
with file size and the database grows. This does not plateau.

**WHY NOBODY SAW IT.** `/tmp/cloudecode-menubar-error.log` contains ZERO
`Executing <Handle` lines: asyncio debug mode is off, so the loop never warned
about a 14-second callback. `/tmp/cloudecode-menubar.log` is meanwhile full of
`GET /api/v1/version HTTP/1.1 200 OK` from `127.0.0.1` - the evidence was
sitting in the access log the whole time, looking like normal traffic.

**A 401 DOES NOT TEST THIS.** An unauthenticated `curl` to `/api/v1/version`
returns 401 WITHOUT reaching the handler, so it cannot measure the pragma. One
such 401 did take 4.76s, which independently proves the loop was blocked at that
moment but says nothing about the cause. If you test this endpoint, use a real
token or you are measuring the auth rejection.

**THE FIX - NOT YET BUILT. Three parts, and part 1 alone is not enough.**

1. `run_in_threadpool` / `asyncio.to_thread` ALONE IS INSUFFICIENT. It unblocks
   the loop but still burns 14 seconds of disk and CPU every 20 seconds,
   forever, growing. The loop would be free and the machine would still be
   grinding.
2. **`PRAGMA integrity_check` does not belong on a status endpoint at all.** It
   is a maintenance operation, not a liveness probe. A version/status handler
   wants "can I open and query this database", not a full-file page walk.
3. If a periodic integrity check is genuinely wanted, it belongs on a SLOW
   background schedule (daily, not 3x/minute), in a thread, with the result
   CACHED and the endpoint reading the cache.

**Verify the fix on the SYMPTOM, not the code.** Re-run the `/health` poll and
show p99 dropping from ~12,000ms to double digits, and confirm the 20s stall
cadence is gone. A code-reading that says "it is in a thread now" is not a
measurement. Keep a before/after latency log.

**Cleanup owed:** py-spy 0.4.2 was installed into a throwaway venv at
`/tmp/pyspy-diag/venv` ON THE MINI to take these dumps. Nothing in the server
tree, database or config was touched, and nothing was restarted or deployed.
Remove that venv when convenient. Also recorded: py-spy CANNOT attach on macOS
without root (`This program requires root on OSX`), and passwordless sudo IS
available on the mini (`sudo -n true` succeeds), which is what made the live
dump possible at all.

**Raw dumps and the correlation logs** are in this session's scratchpad under
`pyspy/` (`dump_01.txt` .. `dump_24.txt`, `dump_stall_final.txt`, `health.log`,
`dumptimes.log`). Scratchpad is session-scoped and will not survive; copy them
into the repo if they are wanted as a record.

**TWO CORRECTIONS TO THE RECORD, both found on the way.**

- **`/sessions/list` does NOT use `run_in_threadpool`.** This file and
  `HANDOFF.md` both say it does, and both are WRONG. `src/api/routes.py:772`
  awaits a plain `async def` (`session_manager.py:2496 list_session_infos`),
  which calls sync `_session_info_for` -> `_owned_instances_from_db` ->
  `_datastore_connection` (`:2573`), opening cloude.db ON THE LOOP. The queries
  are small so it is not the 14s stall, but it is real blocking and the
  "correctly uses run_in_threadpool" claim must not be repeated.
- **`GET /corpus/status` is a dormant second bomb.** `src/api/corpus_routes.py:75`
  -> `corpus_status.py:99` runs a full-scan aggregate over `transcript_archives`
  plus `COUNT(*) FROM message_transcripts` at `:157`, on the loop. And
  `transcript_archives` (`src/core/db_models.py:1083`) has six indexes but NONE
  on `source_path` (one was deliberately dropped, see the comment at `:1340`),
  so `COUNT(DISTINCT source_path)` at `corpus_status.py:100` is a guaranteed
  full scan of the biggest table. Nothing polls it today. The day something does,
  this reproduces the same outage.

---
### 2026-09-07 - THE LAG: fix BUILT and validated locally, NOT YET DEPLOYED - OPEN (deploy + confirm)

The diagnosis above is closed. The fix is written, tested and independently
validated on this checkout. **It is NOT on the mini.** Until it is deployed and
`/health` is re-polled, the real-world improvement is CANNOT DETERMINE.

**What was built**

| Piece | File |
|---|---|
| Run one check off-loop, own the verdict artifact | `src/core/db_integrity.py` (384 lines) |
| Read the verdict, decide what may honestly be said | `src/core/db_integrity_status.py` (254) |
| The daily background loop | `src/core/db_integrity_task.py` (237) |
| Atomic write / tolerant read, shared with the ingester | `src/core/json_artifact.py` (113) |
| The cheap per-request probe | `src/core/db_health.py` (modified) |

The request path is now one connect plus one small SELECT plus one small JSON
read. `PRAGMA integrity_check` moved to a daily loop via `asyncio.to_thread`.

**The verdict has TWO fields on purpose.** `verdict` is `ok` / `failed` /
`cannot_determine`; `freshness` is `current` / `stale` / `never_ran` /
`cannot_determine`, reusing the vocabulary already in
`src/core/corpus_ingest_state.py` rather than inventing a second one. `verdict`
reads `ok` ONLY when a check actually ran AND its record is fresh, so "never
checked" can never render as "checked and sound". A recorded `failed` still
degrades `data.status` the way the live pragma did; `cannot_determine`
deliberately does NOT, because not having looked is not a fault.

`CLOUDE_DB_INTEGRITY_CHECK=0` disables the loop; defaults OFF under
`CLOUDE_TEST_MODE` so pytest never walks the real database.
`CLOUDE_DB_INTEGRITY_CHECK_INTERVAL` overrides the daily interval and the
staleness window is derived from it (2x) rather than hardcoded.

**VALIDATION - what was actually proven, and how**

Independent validator, read-only, not the agent that wrote the code.

- **Pragma is gone from the request path.** Proven WITHOUT grep and WITHOUT the
  shipped test: `sqlite3.connect` was patched to install a `set_trace_callback`
  recording every statement a real `GET /api/v1/version` executes. Four artifact
  states (missing, corrupt, fresh-ok, failed) each executed exactly 5
  statements: three connection pragmas, `SELECT 1 FROM sqlite_master ...
  name='meta'`, and `SELECT value FROM meta WHERE key='schema_version'`. No
  integrity check on any path including exception and fallback paths.
  **NEGATIVE CONTROL: calling `integrity_check()` directly DID make the tracer
  print `PRAGMA integrity_check`**, so the instrument was live. Request cost
  1.4 ms.
- **The three-outcome guard was attacked in both directions.** 11 adversarial
  states (no artifact, empty file, truncated JSON, JSON array, `status:ok` with
  missing `finished_at`, unparseable stamp, future-dated stamp giving a NEGATIVE
  age, stale stamp, null status, garbage status, empty object). All 11 returned
  `cannot_determine`, none leaked `ok`. **POSITIVE CONTROL: a fresh `status:ok`
  record yields `verdict: ok, freshness: current`. FAILURE CONTROL: a
  `status:failed` record yields `verdict: failed` AND degrades `data.status` to
  `degraded_db_unreadable`.** The guard can fail and does.
- **The riskiest edit was the `corpus_ingest_state.py` extraction**, which
  deleted ~40 lines from a subsystem that was not the target. Verified by
  AST-unparsing the pre-change function bodies from `git show HEAD` and diffing
  against the new shared ones: IDENTICAL modulo the log-event call, for both
  writer and reader. Every primitive survived (`mkdir(parents, exist_ok)`,
  `NamedTemporaryFile` in the same dir, `flush`, `os.fsync(fileno)`,
  `os.replace`, `os.unlink` on failure).
- **Tray contract intact.** Top-level keys still exactly `data`, `update`,
  `version`; `update` unchanged with all seven keys, which is what
  `macOS/tray-api.js:267` reads. `integrity` is nested at `data.integrity`,
  purely additive. Grep found NO consumer of the `data` block anywhere in
  `client/js` or `macOS`.
- **Boot safety.** Scheduler started last in `lifespan()` (`src/main.py:673`),
  wrapped so boot cannot fail on it, `aclose()`d on shutdown.

**Tests: 4615 passed -> 4647 passed. 3 failed before, the SAME 3 after, 0
errors.** The three are `test_home_write_guard`, `test_state_dir_resolution`,
`test_version_probe`, all environmental and pre-existing. No new failures.

**CANNOT DETERMINE, stated plainly: the real speedup.** Measured 0.46 ms
(new probe) vs 112 ms (old pragma) against a 310 MB SYNTHETIC database built
from the real schema. That is 215x and it widens with file size, but it is NOT
the 4.5 GB production file and nobody has watched p99 drop on the mini.

**WHAT REMAINS - the deploy, and how to prove it worked**

1. Deploy with `scripts/deploy-mini.sh --target live`. `--target live` is
   REQUIRED and BOTH targets must be written (server dir AND app bundle
   Resources) or the next app launch silently reverts it.
2. Verify the deploy by fetching the asset over HTTP and hashing it against the
   local file. NOT by `git rev-parse` on either side.
3. **Prove the fix on the SYMPTOM.** Re-run the `/health` poll at 10Hz and show
   p99 dropping from ~12,000 ms to double digits, and the 20-second stall
   cadence gone. Keep the before/after log. A code reading that says "it is in a
   thread now" is not a measurement.
4. Note for whoever tests the endpoint: an unauthenticated `curl` to
   `/api/v1/version` returns 401 WITHOUT reaching the handler. Use a real token
   or you are timing the auth rejection.

**Deliberately NOT changed:** `src/main.py:755` carries an emoji in the FastAPI
app title (`title="<emoji> Cloud Code"`), which violates the no-emoji rule. It
is PRE-EXISTING, not introduced by this change, and it is a user-visible string.
Left for a deliberate decision rather than changed silently.

**Also fixed in this change, found by the validator:** `CLAUDE.md` claimed the
corpus ingest loop is "started last in `lifespan()`", which this change made
false; and the pytest baseline in `CLAUDE.md` was stale by an order of magnitude
(it said ~614 passed / 16 failed / 6 errors against a real 4647 / 3 / 0). The
reason the old number was so wrong is worth knowing: **the repo's `venv` symlink
pointed at a `venv.nosync` directory that no longer existed, so the suite could
not run properly at all and manufactured a fake low baseline.** The environment
was rebuilt on Python 3.14 from `requirements.txt`. A warning to that effect is
now in `CLAUDE.md` beside the existing one about a fresh worktree having no
`config.json`.

---
### 2026-09-07 - THE LAG IS FIXED ON LIVE. Measured, not inferred - DONE

Commit `3c23309` deployed to live on both targets 2026-09-07. No rollback
needed. **This closes item 2 / PRIORITY 1, open since 2026-09-05.**

**THE MEASUREMENT**

| metric | BEFORE | AFTER | during a 29s background ingest |
|---|---|---|---|
| p50 | 30.5 ms | 5.2 ms | 5.1 ms |
| p99 | 14,505.7 ms | **13.3 ms** | 8.9 ms |
| max | 14,774.7 ms | 78.3 ms | 40.4 ms |
| stalls over 1s | 4 | **0** | 0 |
| stall cadence | every 20.0s, ~14.5s each | **GONE** | none |
| share of window stalled | 86.5% | **0.0%** | 0.0% |
| samples in window | 120 | 539 | 996 |

**p99 fell 1,090x.** The sample count TRIPLED in the same 60 seconds at the same
poll rate, which is its own independent evidence: the loop is genuinely free now
and can answer three times as many requests in the same wall clock.

Both runs used the same script, same host, same 60s, same ~10 Hz, and a 30s
timeout so a stall is recorded at its TRUE length rather than truncated. Zero
non-200 responses in either run.

**HOW THE DEPLOY WAS VERIFIED.** sha256 of all 434 deployable files on BOTH
targets against the local repo. Not `git rev-parse` on either side. Pre-deploy
drift against HEAD~1 was 0, which PROVES the 8-file deploy was equivalent to
`--all` rather than assuming it. Post-deploy: 0 mismatches on both targets, all
four new modules present. Then **re-hashed AGAIN after the kickstart**, because
the packaged app copies its bundle Resources over the server dir on start, and
all 8 files survived that copy. That is the trap in section 2 of `HANDOFF.md`
being actively checked rather than trusted.

**BOOT SURVIVED, which was the highest risk in this change.** Electron
48016 -> 50860, server 48024 -> 50872, `runs=5`, last exit 0, HTTP 200 in 4 ms.
ZERO error-level lines and zero tracebacks since restart. Both schedulers logged
themselves started: `corpus_ingest_scheduler_started` (900s) and
`db_integrity_scheduler_started` (86400s, stale window 172800s). The
`ProjectsView has no attribute read_only` warning in the log is PRE-EXISTING
since 2026-08-29 (38 occurrences before this deploy) and is not a regression.

**THE CORPUS INGESTER SURVIVED THE REFACTOR.** This mattered because the change
pulled the atomic-write helpers out of `corpus_ingest_state.py`. The liveness
artifact refreshed at 14:49:03Z, 15:00:25Z and 15:15:54Z. Two full passes
observed after the restart, both `status: ok` with `rooting.status: ran`.
**Verified with a POSITIVE CONTROL, not a null result:** `transcript_archives`
21,991 -> 21,997 (+6, matching the reported `ingested: 6`),
`transcript_records` 6,682,469 -> 6,685,974 (+3,505),
`transcript_root_decisions` +6, while `projects` and `sessions` were unchanged.
Corpus discovered 19,186 against 19,184 `.jsonl` on disk, consistent. Row counts
GOING UP is what proves ingestion, where an unchanged count would have been a
CANNOT DETERMINE.

**THE NEW INTEGRITY LOOP RAN AND PUBLISHED.** `db-integrity/latest.json`
appeared, `status: ok`, `duration_seconds: 26.395`, finished 15:00:11Z. The file
was ABSENT before the deploy, so its appearance is attributable to this change
(negative control held).

**CANNOT DETERMINE, stated rather than softened: the daily integrity check was
never observed while the lag was being measured.** It ran 14:59:45-15:00:11Z,
entirely BEFORE the 15:00:27-15:01:27Z measurement window, so the after-numbers
do not evidence its off-loop behaviour either way. The gap was closed
INDIRECTLY instead, and this is the more interesting result: a **29 second
background ingest pass ran INSIDE a 110 second measurement window and produced
ZERO stalls with p99 8.9 ms**, which proves the `asyncio.to_thread` mechanism
holds under real background database load. Confirming the daily check
specifically would need a forced run, which means mutating config, and that was
correctly not done on a live box.

**Rollback is still staged on the mini** at
`/tmp/cc-rollback-20260907T145443Z/{server-src.tgz,bundle-src.tgz}`, verified
listable and containing `src/main.py`. Delete when satisfied.

**Raw before/after health logs** are on the mini at `/tmp/cc-health-*.log` and
in this session's scratchpad (`cc-health-before.log`, `cc-health-after.log`,
`cc-health-during-ingest.log`, plus `upgrade-baseline-pre.json` and
`upgrade-baseline-post.json`). Copy them into the repo if they are wanted as a
permanent record; the scratchpad is session-scoped.

**Note for future read-only DB access:** `mode=ro` now FAILS once the ingester
holds a WAL, because it cannot create the `-shm` file. Use the project's own
`PRAGMA query_only=ON` path instead.

---

### 2026-09-07 - `scripts/deploy-mini.sh` is BROKEN on current macOS - OPEN, HIGH

**21.** Found during the `3c23309` deploy. Both machines now ship Apple's
**openrsync** ("protocol version 29, rsync 2.6.9 compatible") rather than
GNU rsync 3.x. openrsync REJECTS the combination the script relies on,
`--files-from=- --relative`, with:

    server receiver mode requires two argument

The deploy **aborted cleanly having copied nothing**, so it fails safe rather
than half-deploying, which is the one good thing about it. The `3c23309` deploy
was completed by hand via tar-over-ssh to both targets instead.

**This affects EVERY future deploy** and should be fixed before the next one.
Options: install GNU rsync on both ends and pin the path, or rewrite the file
transfer to use tar-over-ssh the way the manual recovery did. Whichever is
chosen, the script must still write BOTH targets and must keep failing safe.

Related and still open from `HANDOFF.md` section 2: a clean tree makes the
script print `nothing to deploy`, which reads like a pass. Worth fixing in the
same pass.

---
### 2026-09-07 - restarting a LIVE session: what the owner asked for, and why it is not built - OPEN

**22.** Owner request, verbatim: "we will also need a restart menu item added. like
there is an update available, the session is quiet, i hit restart and it closes
and then runs a new session to update."

Deliberately NOT built alongside the kebab menu (`cddc823`). The reasons are in
the SOURCE, not just inherited from `HANDOFF.md` section 5, and they are the
whole point of this entry.

**THE EXISTING RESTART CANNOT DO THIS.** The path is
`SessionRowActions.ACTION_RESTART` -> `API.respawnSession` ->
`POST /sessions/respawn` (`src/api/routes.py:1000`) ->
`SessionManager.respawn_session` (`src/core/session_manager.py:5966`) ->
`resolve_respawn_plan` (`src/core/session_respawn.py`, called at
`src/core/tmux_backend.py:1419`).

`resolve_respawn_plan` returns `RESPAWN_NOT_DEAD` the moment `pane_dead != "1"`,
and tmux itself refuses `respawn-pane` on a live pane without `-k`, which that
module never passes. **Respawn revives a corpse. It does not replace a running
session.** The owner described a session that is quiet but ALIVE, so the
existing control is the wrong mechanism, not a mechanism that needs a new
button. It remains in the kebab, unchanged and tested, for a DEAD row.

**THE LANDMINE, confirmed in source.** An empty `#{pane_start_command}` lands on
`RESPAWN_SHELL`, which silently hands back a LOGIN SHELL. A non-empty command
with no stored `agent_type` lands on `RESPAWN_REPLAY`. Only both together reach
`RESPAWN_AGENT`. Since `sessions.agent_type` lands NULL for every session
created via `auto_start_claude:false` plus a hand-sent claude command (backlog
item 3), a naive restart would destroy a working claude session and return a
bare zsh prompt for a REAL SUBSET of the sessions on this box. As of 2026-09-07
there are 18 live tmux sessions, most idle for days, which is exactly that
population.

**TWO THINGS MUST BE BUILT, neither small.**

1. **A dry-run rung preview.** There is no way to ask which rung a session would
   land on without acting: `POST /sessions/respawn` is the only entry point and
   it MUTATES. Worse, the ladder short-circuits on not-dead BEFORE it ever reads
   `#{pane_start_command}`, so predicting the rung for a LIVE session needs a new
   pure-function branch plus a new read-only route. Without this the UI cannot
   honestly warn the user, and per the three-outcome rule an unknown rung must
   render as cannot-determine, never as a yes.
2. **Close-and-recreate.** Respawn deliberately PRESERVES the instance triple
   `(tmux_socket, tmux_name, tmux_created_epoch)` and writes NO database row.
   Destroy-and-create mints a NEW row and must re-carry project attribution,
   pinned theme, unread state, group filing and sidebar position, all of which
   are keyed on the tmux name. Note the latent defect already recorded: the
   primary key of `session_group_members` is `tmux_name` rather than
   `session_uuid`, so two sessions sharing a tmux name share one membership row.
   A recreate that reuses the name walks straight into it.

**Design questions to settle BEFORE writing code:**
- Does restart keep the same tmux name (simpler, collides with the
  `session_group_members` key defect) or take a new one (clean, orphans every
  localStorage per-device key that is name-scoped, including pinning and
  within-group order)?
- Does the transcript continue via `--resume`, or is a fresh transcript correct
  when the point is to pick up a new agent version?
- What is shown when the rung preview says `RESPAWN_SHELL`? The answer must be a
  choice offered to the user, not a silent proceed and not a silent refusal.

**Guard requirement.** Restart destroys a running session and is irreversible
from the user's side, so it must confirm. Do NOT build a hard block on
`activity_status`: `HANDOFF.md` section 6 records `activity_state` reading
`working` for about four minutes after a resume before self-correcting. Use it
to inform the confirmation, never to silently refuse.

---

### 2026-09-07 - pick a wrapper at restart, and land back in the session - DONE (item 22's first half)

**24.** Owner, verbatim: "when resuming/restarting a session can we pick a new
wrapper" then "yes, and then fix so i can go right back into it." The workflow
behind it: he wanted a session on the `claude-chrome` wrapper and had to have
the database edited by hand to do it.

**WHAT LANDED.**

1. **A read-only rung preview.** `GET /sessions/restart/preview`
   (`src/api/restart_routes.py`) probes the pane once and runs the SAME ladder
   the action runs, so it cannot drift from it. It answers TWO questions rather
   than one, because they are different questions:
   - `unchanged` - what a restart does RIGHT NOW. On a live pane, `not_dead`.
     This is the safety answer and the one a button obeys.
   - `projected` - the rung it would come back AS, liveness ignored
     (`project_restart_rung` in `src/core/session_respawn.py`). Never
     `not_dead`. **A PREDICTION, NEVER A PERMISSION.**
   - `pane_state` - `dead` / `alive` / `unknown`, liveness as its own fact.
   Plus one predicted outcome per configured wrapper, each carrying `resolvable`
   (a fact about the wrapper) and `actionable_now` (a fact about the pane), kept
   apart so a client can say WHY a row is greyed out.
   `wrappers_status` is `ok` / `unavailable`, so an unreadable wrapper list can
   never render as "you have none configured".

2. **An `agent_type` override on `POST /sessions/respawn`.** An ID, never a
   command. Validated against `agents.wrappers` in
   `src/core/session_agent_choice.py`, which REFUSES an unknown id rather than
   letting `Settings.get_agent_command` fall back to the default wrapper - that
   forgiveness is right for a launch and catastrophic for a picker. Three
   verdicts: `accepted`, `unknown`, `cannot_determine`.

3. **The explicit choice outranks the `pane_start_command` gate.** The judgment
   call worth reviewing. The gate exists because a STORED `agent_type` is
   written on every create and so is not evidence of intent; a wrapper the user
   picked in a panel, having been shown what it would do, is different evidence,
   and the failure the gate prevents (an agent in a pane the user believes is
   his shell) cannot occur. It does NOT outrank `not_dead` or an unanswered
   probe. Verdict stays `RESPAWN_AGENT`; `RespawnPlan.chosen` and a different
   sentence carry the distinction, so the kind vocabulary did not fork.

4. **Persistence.** On a restart VERIFIED alive with a picked wrapper,
   `sessions.agent_type` is updated on the row keyed by the instance triple.
   Only one column; no lineage, no origin, no identity. A restart that FAILED
   does not record the choice - recording it would make the next restart
   re-derive a command already observed to fail. `agent_type_persisted` is
   reported so a restart that worked and a choice that did not stick are told
   apart.

5. **Return to session.** `POST /sessions/respawn` now reads the row back AFTER
   the restart and returns `session_id` and `session_uuid`.
   `client/js/session-restart-return.js` reopens by that id and refuses to
   navigate if a different session comes back. Identity is the SERVER's
   measurement, not a client name match.

6. **The picker.** `client/js/session-restart-picker.js`, opened from the
   kebab's existing restart item and from the launchpad row. It shows the
   baseline first (the row that exposes the shell landmine), marks the current
   wrapper, shows the predicted outcome for every choice, and its own restart
   button is the confirmation. `activity_status` is shown and never used to
   refuse - HANDOFF section 6 records `working` lagging four minutes after a
   resume.

**NOT BUILT, deliberately: close-and-recreate for a LIVE session.** A running
pane still answers `not_dead` and the picker renders that refusal. The wrapper
switch is complete for a dead or exited session, which is the case the hand edit
was for. Recreating a live one needs a new row that re-carries attribution,
theme, unread, group filing and position, and walks straight into the
`session_group_members` primary-key defect. That stays item 22.

**Tests.** `tests/test_session_restart_wrapper_choice.py` (pure ladder + real
tmux: the picked wrapper is what ends up in a pane born as a bare shell; an
unknown id refuses and spawns nothing; the choice persists and `session_uuid`
does not move; a failed restart records nothing; the preview spawns nothing).
`tests/test_restart_picker_renders.py` (Playwright, phone width: the shell
warning is a different colour from the agent one, a live session is told what it
would come back as and every radio stays disabled, a failed preview paints no
panel). `tests/test_restart_picker.node.mjs` + its
`tests/test_restart_picker_runs.py` wrapper pin the prediction/permission split.

---

### 2026-09-07 - the deploy copies with `ditto`, which MERGES - OPEN, latent

**23.** Found during the `cddc823` deploy. `dl_commit` in
`scripts/deploy-lib.sh` copies the staged tree into each destination with
`ditto`, which MERGES into the target rather than replacing it.

Consequence: **the first commit that DELETES a client or server file will leave
the stale copy on BOTH targets, and the deploy will still report success.**
Verification cannot catch it, because verification hashes the files that SHOULD
be present and a leftover file is not in that list. An orphaned `.js` still
referenced by a cached `index.html`, or a removed module that something still
imports, would keep working on the mini and nowhere else.

Not urgent: every deploy so far has only added or modified. It becomes a live
defect the moment a file is removed, and the group-chip removal in progress may
be exactly that moment. **Check whether that change deletes any file before
deploying it.**

Fix options: have the deploy compute the deletion set from git and remove those
paths on both targets explicitly, or replace-not-merge into a fresh directory
and swap. Whichever is chosen, the verification must then also assert that
files which should be ABSENT are absent, otherwise it still cannot fail.

---
### 2026-09-07 - restart preview and wrapper picker DEPLOYED, and the owner's two design calls - DONE (deploy), OPEN (part 2)

`83b6377` deployed to live on both targets 2026-09-07, second clean run of the
rewritten deploy script. No rollback needed.

**What is live now.** The restart rung preview
(`GET /api/v1/sessions/restart/preview`), the wrapper picker, the transcript
presence guard, the close-lifecycle fix from `c9271da`, the group chip removal
and the dvh fix. Nine new files, all confirmed present on BOTH targets.

**Verified, with controls that were proven able to fail.** sha256 of 14 files
matched on both destinations before AND after the restart re-hash (the app
copies bundle Resources over the server dir on start, which is how a deploy
silently reverts). The MISSING branch of the check was fired deliberately with a
bogus filename. All five new static assets served 200 with body hashes matching
local, and a bogus asset URL returned 404 so the check can fail. The new route
returned **401, not 404** - and a bogus sibling route under the same prefix
returned 404, which is what makes the 401 evidence of registration rather than a
blanket auth wall.

**No regression.** /health at 10 Hz on-box, 30s each side. Before p50 29.7 /
p99 74.6 / max 84.4 ms. After p50 29.9 / p99 66.0 / max 73.0 ms. Zero errors
either side. The lag fix holds. Corpus ingest artifact mtime MOVED during the
window (18:39:42 -> 18:44:48, age 213s -> 44s), so it is live rather than merely
present. DB integrity `status: ok`.

Rollback tars on the mini, both listable and content-checked:
`/tmp/rollback-server-20260907-184229.tgz`,
`/tmp/rollback-bundle-20260907-184229.tgz`.

**CANNOT DETERMINE: whether the picker behaves correctly in the browser.** The
bytes are on disk, the assets are served with matching hashes, the tags are in
the served HTML and the route is registered. Nobody has clicked it. A tab open
across the deploy keeps serving cached assets until a hard reload.

---

**OWNER DECISIONS on item 22 part 2, given 2026-09-07. Do not relitigate.**

- **Same tmux name.** His words: "same tmux should be fine."
- **Resume the same conversation.** His words: "yes resume the same session."

**These two answers collapse most of part 2.** It is no longer close-and-recreate.
It becomes kill the pane and respawn it IN PLACE with the same start command,
which is `respawn-pane -k` against a live pane - a small delta on the existing
ladder, not a new subsystem. No new database row is minted, so NONE of the
re-carrying work previously scoped is needed: project attribution, pinned theme,
unread state, group filing and sidebar position all stay put because the row
never moves.

**It also sidesteps the `session_group_members` landmine** rather than fixing it.
That table's primary key is `tmux_name` rather than `session_uuid`, so a recreate
that CHANGED the name would have collided. Keeping the name means the membership
row keeps pointing at the right thing. The defect is still real and still open;
this feature simply no longer walks into it.

**The one consequence to keep in mind.** Killing and respawning gives the pane a
NEW `tmux_created_epoch`, so the instance triple
`(tmux_socket, tmux_name, tmux_created_epoch)` changes and anything keyed on the
epoch sees a different instance. That is the same mechanism that makes tmux
resurrect/continuum a design problem (INFRA-115), so it is a known shape here,
not a surprise.

**And this is exactly why the transcript-presence guard matters.** Resuming the
same conversation routes every restart through `refuse_if_transcript_missing`,
which turns a deleted transcript into a named `RESPAWN_TRANSCRIPT_MISSING` state
instead of the dead-pane-reported-running failure that cost the owner a session
on 2026-09-07.

---

### 2026-09-07 - `ProjectsView` has no attribute `read_only` - OPEN, low

**24.** `src/core/upload_sweeper.py:155` logs `project_list_unreadable` with
`'ProjectsView' object has no attribute 'read_only'`. **52 occurrences going back
to 2026-08-29.** Confirmed PRE-EXISTING and not introduced by any of the
2026-09-07 work: neither `read_only` nor `ProjectsView` is touched anywhere in
`cddc823..83b6377`. Noise today, but it means the upload sweeper's project list
read is failing every pass and nobody has looked at what it was supposed to do.

---

## DONE - kept because they are the evidence for how the open ones should be approached

All on branch `v1.1`. **8 commits unpushed as of 2026-09-07**, verified with
`git log --oneline @{u}..HEAD`.

- `5c88fdd` **the archived notice renders when the fetch FAILS.** The
  three-outcome state existed in the model and never reached the screen. Fixed
  by one `renderProjectList()` in the catch. Verified by fetching the asset over
  HTTP and hashing it against the local file, NOT by `git rev-parse`. **This is
  the verification pattern to copy.**
- `a89c919` **project archiving.** `POST /api/v1/projects/{name}/archive` and
  `/unarchive`, `include_archived` on `GET /projects` and `GET /sessions/recent`,
  new `src/core/project_archive.py` (159 lines), and the show-archived toggle.
  **No migration was needed** - `projects.archived_at` has existed since the
  original DDL and the list query already filtered on it. Only the write path,
  API and UI were missing. Schema stays v23. **Read the DDL before writing a
  migration.**
- `63c8439` **the archive is a full-page mode.** It used to sit on top of the
  current session and keep holding it. It now releases via `pauseForHome()`.
- `7bf95e5` **sidebar focus.** A closed sidebar kept keyboard focus off screen,
  so keystrokes went nowhere. `close()` now calls `_releaseFocus()` and
  `_setInteractive(false)`, and the closed state carries `inert` plus
  `visibility:hidden`. `App.focusTerminal()` is called from `showTerminal()` and
  `returnToExistingTerminal()`. **Note this is only ONE of two focus-destruction
  mechanisms** - the 5-second `renderProjectList()` rebuild is the other, and it
  is still open.
- `a945771` **toasts.** `dismissForSessionActivity(sessionId)` (which ACKS, it
  does not merely hide), `dismissAll()`, `_noteUserInputToSession()`.
  Auto-dismiss is deliberately PER-SESSION and must stay that way - see open
  item 7, where the missing half is cross-session VISIBILITY, a different axis.
- `dc91524` **test(ordering): pin which hook events reach the work stamp.**
- `e57a7f2` **ordering by work done, never by clicking.** The `is_this_tab` and
  `is_active` sort terms were removed. The sidebar is a timeline of work, which
  is what the owner asked for and is the point of the whole ordering rule.
- `f7bea60` **refresh the project list after a session creates a project.**
  Guarded `await this.loadProjects()` added to `_handleAttachRunningSession`,
  `createConsoleSession` and `_createNewSessionInner` - the "main page updates
  sessions but not projects" fix.

Also done, outside this repo but part of the same work:
- `~/.claude/_scripts/statusline-enhanced.sh` (135 -> 190 lines).
  `build_icon_path()` handles both the `$HOME/Development/...` and the long
  iCloud spelling; the `[NN%]` was replaced with a ten-cell bar; **unknown
  renders `[??????????]`, not an empty bar**, which is the three-outcome rule
  applied to a status line. Backup at `statusline-enhanced.sh.bak-20260903`.
- Migration state as of 2026-09-06: 29 sessions on the mini, groups Joe /
  Agents / Waiting / Clients, 12 in Recent. 17 projects, 0 archived. Disk on the
  mini went 26 GB -> 178 GB free.

---

## CARRIED OVER FROM THE INFRASTRUCTURE TODO - older app items, all still OPEN

These predate the 2026-09-04 backlog and were filed in the Infrastructure OPEN
index. They are app defects, so they live here now; the Infrastructure lines are
left in place because that file is append-only.

### Test-harness safety - HIGH, both touch the owner's live state

**INFRA-49. The pytest suite still creates real tmux sessions on the live shared
`cloude` socket.** `tests/test_session_backend.py::test_adopt_external_session_*`,
roughly 15 hardcoded socket-name literals, currently DESELECTED rather than
refactored onto a throwaway socket. One leaked as `adopt_itest_d15ad052`. It is
the remaining way an unattended test run can touch the owner's live tmux
sessions on mac-mini-m4. Effort: 2-3h. Risk: HIGH. Opened 2026-08-17.

**INFRA-61. ~28 standalone `scripts/verify_*.py` harnesses write into the
owner's REAL `~/.claude/settings.json`.** Verified 2026-08-26: `src/main.py`'s
FastAPI lifespan calls `ensure_hook_settings(default_settings_path())`, and
`default_settings_path()` (`src/core/claude_hooks.py:291`) falls back to
`Path.home()/".claude"/"settings.json"` whenever `CLOUDE_CLAUDE_SETTINGS_PATH`
is unset - and NOT ONE harness sets it, while at least twenty boot a real
server. v1.0.4's fix was narrower than it reads: it made the
`ensure_hook_settings` argument required and added a conftest canary, but the
`verify_*.py` harnesses are not pytest and the canary does not cover them.
Effort: 1-2h (set the override in every harness's server-spawn env, plus a guard
that refuses to boot a harness server without it). Risk: HIGH. Guardrail: the
file must stay at sha256
`c950b30887ca3bfe63e7ba51ca1ca4b49f453ca333d0c162b2e2a66db4f6ae09` (confirmed
unchanged 2026-08-26); hash before and after any harness run. Opened 2026-08-26.

### iOS and mobile

**INFRA-53. The copy chip reports a FALSE SUCCESS on real iOS, and the only fix
for it is UNVERIFIED.** Measured twice on an iPhone 16e simulator against an
insecure origin: the UI says "copied url to clipboard" while
`xcrun simctl pbpaste` returns EMPTY, and it DESTROYS a sentinel already on the
clipboard. A candidate fix is committed as `0ed0b91` on branch
`fix/copy-ios-false-success`, but **the agent that wrote it DIED before
reporting**, so nobody has read the diff, nobody has run its tests, and it has
never been deployed. Treat it as untrusted. It is the false-green class, on the
owner's primary device, in the control he uses to move OAuth urls off the phone.
Effort: 1-2h. Risk: HIGH. Opened 2026-08-18.

**INFRA-54. Carried-forward group, unchanged since 2026-08-18.**
(a) **TLS on the Tailscale hostname blocks FOUR things**: web push, clipboard
WRITE, clipboard READ (`navigator.clipboard` is undefined on http, so
paste-from-clipboard is impossible by construction - no code fix exists) and PWA
install. One `tailscale cert` on the `.ts.net` hostname clears all four; it is
the highest-leverage unblocked item.
(b) Adam's `cld` wrapper is broken on the mini - Keychain generic-password
service `claude-cld-oauth` does not exist; fix is `claude setup-token` run
inside a GUI session, not over ssh.
(c) The PR to Adam on branch `fix/portability-and-personal-paths` @ `3bfcee0`
has NEVER been pushed.
(d) Merging `integration/mobile-rc` to main is still owed.

### Small and mechanical

**INFRA-56.** `tests/manual_audio_check.md` lines 12 and 14 still tell a manual
tester to look for `.header-audio-toggle` and `<button id="audioToggleBtn">`,
both removed by the 2026-08-18 kebab consolidation. Effort: 15min. Risk: LOW.

**INFRA-59.** `setup_auth.py` still writes a literal `PORT=8000` into its
minimal `.env` fallback template, even after `bb29afd` single-rooted port
resolution on `Settings.port` and added `scripts/resolve-port.sh`. Effort:
30min. Risk: LOW-MEDIUM (only a first-run fallback path, but it defeats the
point of the single-rooting).

**INFRA-60. The effects-`inactive` terminal state is untested BY
CONSTRUCTION.** All 23 themes now declare an `effects` key, so there is no
effects-free theme left to prove the harness settles to `inactive` with zero
canvases on switch; teardown was verified another way instead (previous canvas
`isConnected: false` after a switch), which is not the same claim. Same shape as
a check that can never fail. Effort: 1-2h (a synthetic no-effects theme fixture,
or a harness-level unit test that forces the branch directly). Risk: LOW-MEDIUM.

**INFRA-52. claude-history residual gaps, three of them.** (a) Desktop
`local-agent-mode-sessions/*/audit.jsonl` (5 files, 3,078 lines) is deliberately
NOT ingested and needs its own adapter: no `parentUuid`, a different timestamp
field, and TWO session ids per file, which breaks the one-file-one-Session
assumption the ingester rests on. (b) `(session_id, seq_in_file)` cannot be made
UNIQUE until subagent sessions are re-keyed by `(parent uuid, agent id)`. (c)
The CloudeCode-side history VIEWER is not started.
**Not a gap, recorded so nobody hunts for it:** roughly six months of pre-2026
transcripts were PRUNED by the `cleanupPeriodDays` default of 30 before the
setting was raised to 360 on 2026-01-22, and are unrecoverable. The owner has
about EIGHT months of history, not the year he believes.

**INFRA-57 / 58. tmux socket cruft on mac-mini-m4.** Roughly 1500 stale
`cc-locale-*` and other socket files accumulating in `/private/tmp/tmux-501/`,
plus four of unknown provenance (`cloude-refit-test`,
`cloude-test-jsstatusbar`, `cloudeverify`, `cloudewt`) not explained by any
tracked test branch. Filed here because this app's test harness creates them;
the Infrastructure lines stay because it is that host's disk. Opened 2026-08-18.

### The message browser and the archive (opened 2026-08-31 / 2026-09-02)

**INFRA-99. THE EXPORT DOWNLOAD CANNOT AUTHENTICATE FROM A BROWSER.** Auth is
HTTPBearer only; measured, `?token=`, `?access_token=` and a cookie ALL return
401, and a plain navigation sends no Authorization header. Needs a short-lived
single-use download ticket. It is the OWNER'S CALL because it puts a credential
in a URL.

**INFRA-101.** Corpus 1 has `manifest_sha` NULL yet 19,545 of its 19,548
transcripts claim `host_attribution manifest_verified` - an evidenced state with
no evidence behind it. Ingest-side, flagged not fixed.

**INFRA-102.** Widen ingest-side secret detection. The current detector requires
a nearby name like `key` or `token`; the undercount in INFRA-100 traces to this.
Unscheduled.

**INFRA-103 / 114 / 121. Where the archive corpus lives, and whether it is
readable.** The 21 GB database was migrated to the mini at
`/Users/jsugamele/ClaudeArchive/` on 2026-09-02 and hash-verified WITH A
NEGATIVE CONTROL (a deliberately altered copy was confirmed to FAIL the same
check, proving the check can discriminate). **It is not readable there yet:**
the production CloudeCode app on the mini was version 1.0.33, has no
message-archive feature, is on schema 11, and deliberately drops to read-only
against a newer schema. Reading it needs a SEPARATE instance from the checkout
run with `CLOUDE_MESSAGE_ARCHIVE=1`, not the production app.

**INFRA-105.** 46 subagent transcripts (12.2 MB) exist in NO archive because the
ingester does not walk `workflows/`. A further 554 MB of tool-result artifacts
are uncovered BY DESIGN, not by omission.

**INFRA-109.** Reclaim the 3.78 GB of duplicate ingest. Cause: the ingest
idempotency key was `source_path`, and once the mini's real path changed on the
2026-09-01/02 symlink migration, 19,294 files whose `content_sha256` was already
stored got archived a second time under the new path. Prevention shipped at
schema v22 by making the key content-addressed. **Path canonicalisation would
NOT have fixed this** - the two encodings are genuinely different directory
names on disk, not two paths resolving to one file. Reclaim of the existing
duplicates was never attempted.

**INFRA-115. tmux resurrect/continuum: wanted by the owner**, but a restored
session gets a NEW tmux creation epoch and session identity is the triple
(socket, name, epoch), so a naive restore would orphan rows. Needs a design pass
before implementation.

**INFRA-116.** `src/core/db_steps.py` (1,124+ lines) and
`src/core/message_gate_contract.py` (906 lines) both exceed this project's
500-line file cap.

**INFRA-117.** `tests/test_secret_gate_parity.py` failed once across three full
test runs and passed 10 of 10 when run in isolation; cause is CANNOT DETERMINE
as of 2026-09-02.

**INFRA-112.** The mini leaks a CloudeCode hook token in plaintext `ps`
arguments. Opened 2026-09-02.

**INFRA-100 / 110. OWNER-GATED, kept in BOTH files deliberately.** The archived
secret inventory is undercounted: measured lower bound **1,800** missed
body-occurrences on one value, recall **1.5 percent**, extrapolated to roughly
**7,000** against **12,390** recorded findings. Treat 1,800 as the FLOOR, not
the estimate - the 7,000 is a heavy-tailed extrapolation. This feeds the
deferred rotation of 731+ credentials found in the archive. The owner asked to
be consulted on secrets work separately, so nothing here is actionable without
him. It stays in the Infrastructure TODO too because the credentials are fleet
credentials.

### Findings from the message-browser build that are worth not re-learning

Recorded 2026-08-31 and 2026-09-02, measured not reasoned about. These are
closed as work but live as constraints.

1. A snippet-withhold gate keyed on a DETECTOR FLAG rather than on the VALUE
   leaked credentials. One credential appeared in 762 bodies, 415 of them
   unflagged, and 21 of 43 search hits returned it in cleartext. 587 of 587
   missed occurrences lacked the nearby `key`/`token` context the detector
   needs, so re-scanning would NOT have helped - proven by re-running the
   current detectors over all 415 and getting zero findings. The fix RECOGNISES
   rather than detects: hash candidate substrings against every credential
   already detected anywhere in the corpus. Result 0 of 43, stated as
   best-effort, not a guarantee.
2. **Secret offsets are Unicode CODE POINTS** (12,390 of 12,390, with a positive
   control proving a byte answer was reachable) **but JavaScript strings are
   UTF-16**, and 1,100 findings sit behind an astral character. Masking with
   code-point offsets in JS measured 0 of 1,100 correct and LEFT THE LAST 4
   CHARACTERS OF A 40-CHARACTER CREDENTIAL VISIBLE. Fixed with
   `match_offset_utf16` companions.
4. `export_transcript` peaked at 2,205 MB serializing a 181 MB transcript
   (12.1x). Streaming replaced it, but the "78 MB regardless of size" claim is
   FALSE: the largest transcript contains a SINGLE 37,404,061-byte line, and one
   line's render is the floor, measured 566 MB. **Size export concurrency
   against roughly 600 MB, not 78 MB.**
5. **Streaming export cannot verify itself** - uvicorn implements no HTTP
   trailers, so there is no hash of what was sent. Reported as an explicit
   could-not-evaluate with the `shasum` command a human can run, never as
   success.
6. **Subagent linkage does not work through the obvious foreign keys.**
   `agent_id` matches zero of any `tool_use_id`, and `origin_session_ref` names
   the ROOT session in one hop even five subagents deep. The real link is a
   string printed in the tool RESULT's prose, `agentId: <id>`, which resolves
   96.04 percent of 19,629 spawns. Not schema-enforced, and the printed format
   has already changed once.
7. **`json_extract` raises on malformed JSON even when `json_valid` is selected
   in the same query**, because SQLite evaluates every selected expression
   regardless. One unparseable body would abort a whole page of valid rows.
8. **`PRAGMA foreign_keys` is a no-op inside a transaction**, which is exactly
   where migrations run. A standard table-rebuild migration would have
   cascade-deleted 3.1M appearance rows with the guard unarmable at the moment
   it mattered.
9. `result_status "ok"` does NOT mean the scope was fully read - a live search
   returned `ok` having scanned 1 of 3,416 transcripts.
10. `scan.bytes_scanned` is a CHARGE, not work done - 1,501,528 against a
    1,048,576 budget, and 91,950,363 bytes reported in 0.0039 seconds. It must
    never drive a progress bar.
11. **The in-app Chrome MCP browser pane reports `document.hidden === true` EVEN
    WHEN FRONTED**, which freezes rAF and makes every measurement through it
    false. Verification moved to a headed Playwright chromium.
12. **Mutation testing found three things ordinary green tests did not:** a
    production promise that never settled on a rejected fetch (the suite HUNG
    rather than going red), a binary-search mutant that survived 10,000 random
    scroll samples because random floats never land exactly on a row boundary
    though scroll-into-view always does, and a Node test harness whose `test()`
    did not await its argument so six async tests recorded a pass before their
    assertions ran and were incapable of failing.
13. **Six client defects passed their own tests because the test MOCK and the
    real server disagreed about response shape** - most seriously, every message
    body in the reader rendered COULD NOT EVALUATE because the body cache never
    unwrapped the API envelope. **Capture fixtures from the live server; do not
    hand-write mocks.**
14. gitleaks and `scripts/scan_secrets.py` disagreed and only the weaker one ran
    pre-commit, so CI was ALREADY red before that session's first commit. They
    are complementary - gitleaks has no rule for the 1Password `ops_` token
    class. A gitleaks `paths` allowlist suppresses the ENTIRE file, so allowlists
    here are value-scoped. Also measured: a positive control planting AWS's own
    published documentation example key CANNOT FIRE, because gitleaks allowlists
    that exact value by design - a positive control that itself needs a positive
    control.
17. **Counting assertions across the Node test suites is unreliable** - five
    different summary formats were observed and a regex written for one silently
    zeroed out eight files. Judge suite health by EXIT STATUS PER FILE, not by a
    parsed assertion count.

Two schema/registry facts that read like bugs and are not: the `projects` table
is EMPTY and `message_projects` is the real registry; the `sessions` table held
8 rows, all pytest fixtures. And project slugs are NOT invertible (a hyphen is
both separator and literal), so display names are derived from `observed_cwd`.

**Checked, not landed, verified 2026-08-31 against the `v1.1` checkout:** a
`start_line` parameter on `/lines` and a `session_ref_scheme` filter on the
transcript list were REPORTED as added and do not exist. `archive_routes.py` has
no `start_line` query param on `get_transcript_lines`, and no route takes
`session_ref_scheme` as a filter (the field is only ever returned in output
rows). Left open, not struck.


---

### 2026-09-07 - restart a session whose pane is ALIVE - DONE (item 22 part 2)

Built to the owner's two calls verbatim: "same tmux should be fine" and "yes
resume the same session". So it is NOT close-and-recreate. It is
`tmux respawn-pane -k` against a live pane: kill the process, put a new one in
the same pane, same tmux session name, same row. No row is minted, so project
attribution, pinned theme, unread state, group filing and sidebar position all
stay put because nothing moves, and the `session_group_members` primary-key
defect is sidestepped rather than walked into.

**ONE LADDER, NOT TWO.** `resolve_respawn_plan` took one new keyword,
`live_restart_confirmed`. It changes exactly which side of the liveness gate a
live pane falls on; both sides still end at `_rung_from_start_command`, which is
what makes the projection structurally incapable of drifting from the action. A
parametrised test pins the two to identical `kind`, `command` and `detail`
across every rung.

**THE PERMISSION IS ONE FIELD AND A PREDICTION CANNOT SET IT.**
`RespawnPlan.kills_live_pane` is the only thing `TmuxBackend.respawn` reads to
decide whether `-k` is passed, and it is True only when the pane was MEASURED
alive AND the caller confirmed AND the rung is actionable.
`project_restart_rung` is never told whether the pane is alive, so every plan it
returns carries False - asserted over all 16 input shapes rather than by
reading the code. `refuse_if_transcript_missing` BUILDS its refusal instead of
copying, so `RESPAWN_TRANSCRIPT_MISSING` arrives with the flag False and a
missing transcript cannot kill a working session.

**FOUR GATES IN THE UI AND ON THE WIRE.** `actionsFor` now offers restart on a
status positively known live (`unknown` still gets close alone). The picker's
arm checkbox (`client/js/session-restart-live.js`, its own file because it is
the half that destroys something) is emitted unchecked, takes no argument, and
is the only thing that relaxes `disabled`; `optionsHtml` still derives the initial state from
`actionable_now` alone. The confirm modal names the bare-shell outcome. The
request must carry `confirm_restart_live`.

**THE CONFIRMATION COPY**, verbatim, title `replace what is running`, primary
`kill and restart`:

> this cannot be undone. the process running in this pane is killed and a new
> one is started in the same pane. the session keeps its tmux name, its row and
> its place in the list, and the transcript is not deleted. this session has no
> recorded start command, so it does not come back as an agent. it comes back
> as a plain login shell. what will be started: <the server's own sentence>.

The shell sentence appears only when the projected rung is `shell`, which on the
owner's box is 15 of 19 live sessions. A busy row adds the `activity_status`
lag sentence; it informs and never refuses.

**IDENTITY WAS MEASURED, AND THE PREMISE THAT IT MOVES IS WRONG.** tmux 3.7c on
a scratch socket, before and after `respawn-pane -k`: `session_created`
1788821572 both times, `pane_id` `%0` both times, `pane_pid` 34420 -> 34426. The
instance triple does NOT change, because `#{session_created}` belongs to the
SESSION and `-k` replaces the pane's PROCESS. Fourteen queries in `src/core` key
on the exact triple and all read the same column, so they hold together.
`src/core/session_instance_rekey.py` takes the epoch either side of the kill
anyway and answers `unchanged` / `rekeyed` / `cannot_determine`, re-keying on the
OLD triple if a future tmux ever does move it. A measurement that can stop being
true is not a thing to assume.

**Tests.** `tests/test_session_restart_live.py` (32: the ladder, the shared
tail, the exhaustive projection guard, the transcript guard, real tmux proving
the process is replaced in place and the triple survives, and the HTTP gate).
`tests/test_session_instance_rekey.py` (15: all four identity outcomes plus the
old-triple match). `tests/test_restart_live_gate.node.mjs` + its
`tests/test_restart_live_gate_runs.py` wrapper (15: the row gate, the arm gate,
the confirmation copy). Four new browser tests in
`tests/test_restart_picker_renders.py`.

**Still open, and it is an owner decision.** On the `agent` rung the command is
re-derived through `Settings.get_agent_command`, which carries no
`--resume <uuid>`, so that restart starts a FRESH conversation rather than
resuming the old one. Only the `replay` rung (tmux replaying its own recorded
command) resumes the same conversation, which is 3 of the 19 live sessions. The
confirmation is honest about it - it shows the server's own sentence for the
rung - but "resume the same session" is only literally true on the replay path.
Making the agent rung resume would mean injecting `--resume` from
`sessions.claude_session_uuid`, which is a new behaviour and was not built.

**Re-measured read-only against the live `cloude` socket, 2026-09-07.** 22 live
tmux sessions now, 18 with an EMPTY `#{pane_start_command}`, 3 carrying
`--resume`. The ladder gates on that tmux field alone, so 18 of 22 (82 percent)
land on `RESPAWN_SHELL` and come back a login shell - higher than the 15 of 19
the part-2 brief quoted, and the reason the bare-shell warning is the load
bearing half of the confirmation. No write was made to the socket or the
database.

**Known deviation.** `client/js/session-restart-picker.js` is 578 lines, past
the project's 500-line rule. It was 476 before this change, so the destructive
half was extracted to `session-restart-live.js` (157 lines) rather than left
inline. Getting under 500 would mean also splitting the option-list rendering,
which is deployed code this change does not otherwise touch. Flagged rather
than done.

### 2026-09-07 - a restart resumes the same conversation - DONE (item 22, the ditto defect)

**THE OWNER'S DEFINITION, verbatim.** "restart on recent is really just
resume. restart on open is close and resume session so it loads a new
wrapper or new claude binary." ONE semantic, two mechanics. A dead row has
no process to kill so its restart IS a resume; a live row has its pane
killed first, and the kill exists only so the pane picks up a new wrapper
or a new claude binary. Both come back on the SAME conversation.

**THE DEFECT.** `f95a9ed` made that true only on the REPLAY rung, where
tmux replays a recorded `--resume` it wrote down itself (3 of the owner's
22 live sessions). The AGENT rung re-derives its command through
`Settings.get_agent_command`, which carries no `--resume`, so a restart
there started a FRESH conversation wearing the old session's name and
dropped the user's working context.

**WHAT SHIPPED.**

- `src/core/session_resume_target.py` (new). Classifies
  `sessions.claude_session_uuid` into `resumed` / `none_recorded` /
  `unknown` - the same three words `RestartSessionResponse.conversation`
  already used, reused rather than re-invented - and builds the argv
  fragment through the existing `session_restart.resume_arguments`.
- The uuid reaches the command as
  `get_agent_command(extra_args=['--resume', <uuid>])`, which shlex-quotes
  at every boundary and routes THROUGH the user's wrapper. Never
  concatenated: the returned string is `zsh -c '...; cld'` and an appended
  flag would land outside that quoting.
- ONE lookup per request feeds all FOUR command resolutions (stored agent
  and every wrapper offer, on the action side and the preview side), so
  the preview's predicted command and the action's actual command are the
  same string by construction. Test asserts byte equality.
- `RespawnPlan.conversation` / `RespawnResult.conversation` /
  `RestartPlanPreview.conversation` / `RestartPreviewOption.conversation`.
  DERIVED FROM THE ARGV: a command carrying a `--resume` reads `resumed`
  whatever the caller believed, so the claim cannot outrun the command.
- The transcript guard now covers the AGENT rung on the dead path as well
  as the live one. It always did structurally (`TmuxBackend.respawn` keys
  on `plan.resume_uuid`, not on liveness); it was vacuous there until the
  rung began carrying a uuid. `unchecked` still never refuses.
- The preview passes presence verdicts as `presence_by_uuid` because TWO
  conversations can be in play: the replay rung resumes what tmux
  recorded, the agent rung resumes what the row says. One verdict applied
  to both would refuse a restart nobody measured.
- `client/js/session-restart-continuity.js` (new, 115 lines). The picker
  was NOT grown; it already renders the server's `detail` verbatim and
  that sentence now carries the clause.
- `liveConfirmCopy` keeps the bare-shell warning and adds the continuity
  line. Anything unrecognised, a missing field included, reads `unknown`.

**A DRIFT FOUND AND FIXED WHILE PROVING THE PREVIEW.**
`_agent_command_for_tmux_name` read `agent_type` from `self.sessions`
while `restart_preview` read it from the ROW. An ADOPTED session carries
`agent_type` None in memory and the wrapper id on its row, so it PREVIEWED
as AGENT and RESTARTED as REPLAY. Both now go through
`_stored_agent_type_for_tmux_name`. This is a behaviour change beyond the
brief and is called out deliberately: the alternative was to align the
preview DOWN to the action, which would have removed the whole point of
the picker.

**A GUARD REWRITTEN, NOT DELETED.**
`test_the_wrapper_chooser_never_resumes_a_transcript` asserted the chooser
never resumes anything, which the owner's definition makes wrong. Its own
docstring said what to do instead, and that is what was done: it is now
`test_no_module_on_the_restart_path_spells_its_own_resume`, an AST-based
check that `session_restart.resume_arguments` stays the single builder, so
every restart resume still reaches the presence check.

**The four gates from `f95a9ed` all still hold**, re-pinned by tests:
`kills_live_pane` needs measured-alive AND confirmed AND actionable;
`project_restart_rung` takes no liveness input (swept over every value of
the new `resume_outcome` argument); `armHtml()` takes zero arguments;
`optionsHtml` derives `disabled` from `actionable_now` alone, proven
against a preview whose every projection is actionable and whose every
continuity reads `resumed`.

**Baseline held.** pytest 3 failed / 4823 passed / 12 skipped (was 3 /
4803 / 12; the same three environmental failures). Node: 169 files, only
the pre-existing `test_archive_full_page_mode.node.mjs` fails.

**NOT committed. Nothing on the mini was touched** - no writes to the
production database, no deploy, no restart, no tmux pane created or killed
outside a scratch socket.

---

### 2026-09-08 - DEPLOYMENT STATE, read this before believing anything about live

**Live on mac-mini-m4 runs `0b12edf`.** Committed, pushed, and NOT DEPLOYED:

| commit | what it is | on live? |
|---|---|---|
| `0b12edf` | restart means resume | YES, this is what live runs |
| `0793eb1` | restart picker layout fix | NO |
| `8dd54a8` | lineage recovery plus the backfill tool | NO |

**Nothing is unpushed.** `git log --oneline @{u}..HEAD` returns empty as of
2026-09-08, and `origin/v1.1` is at `8dd54a8`. This SUPERSEDES the "8 commits
unpushed" line in the DONE section and in `HANDOFF.md` section 8, both of which
were true on 2026-09-07 and are not true now.

Anything you observe on live is `0b12edf` behaviour. The picker layout defect
and the lineage recovery are both still live-visible as defects because the
fixes are sitting in git.

---

### 2026-09-07 - the session that vanished, and the two rows repaired by hand - DONE (fix deployed), DONE (recovery)

The owner closed a session, hit restart, and it disappeared from every view.
Two independent root causes, both fixed in `c9271da`, which IS deployed.

1. **Restart resumed a `claude_session_uuid` whose transcript did not exist.**
   The pane died instantly with **status 127** while the row still read
   `lifecycle=running`. This is the incident that motivated
   `RESPAWN_TRANSCRIPT_MISSING` and `refuse_if_transcript_missing`.
2. **Close set `archived_at`.** The Recent group is
   `lifecycle='stopped' AND archived_at IS NULL`, so a closed session missed
   Recents entirely and was in no list at all.

**The hand recovery, recorded because it changed live data.** The pane was
respawned onto the correct transcript, and two rows were repaired in the live
database:

- **Row 42** was emptied and retired: `claude_session_uuid = NULL`, archived.
- **Row 43** took the live tmux linkage: `archived_at = NULL`,
  `lifecycle = running`, `tmux_name = cloude_Agent_-_Cloude_Code`,
  `tmux_created_epoch = 1788813811`.

The repair had to CONSOLIDATE rather than copy, because `claude_session_uuid`
is UNIQUE and both rows cannot hold the same value. Rollback SQL is on the mini
at `/tmp/cc_row_rollback.sql`. `/tmp` is not durable across a reboot; treat
that file as expiring, not as an archive.

---

### 2026-09-08 - the conversation id: two defects, a third group nobody had counted, and a backfill nobody has run - DONE (code, `8dd54a8`), OPEN (deploy and authorisation)

**25.** A session row with no `claude_session_uuid` comes back from a restart
with no history. **16 of the owner's 39 rows were in that state.** Both causes
traced to file and line, not guessed.

**DEFECT ONE: the create path has exactly one writer and it never retries.**
`sessions.claude_session_uuid` is written on create only by Claude Code's
`SessionStart` hook. An empty POST body becomes `{}` at `routes.py:2108`,
`session_manager.py:4341` then returns `LINEAGE_UNRESOLVED` and writes nothing.
The live server log holds **25 such failures across 20 distinct sessions**, and
their ids map onto the missing rows. The structural problem is the shape of the
channel: `SessionStart` fires ONCE per conversation with no retry, while every
other hook event repeats and self-heals. That is exactly why `last_work_at`
recovers on the next event and the uuid never does.
`src/core/session_lineage_recovery.py` (171 lines) now runs the existing
correlation ladder at that failure point, giving the create path the second
chance the adopt path already had.

**DEFECT TWO, worse in practice: the fallback ladder had never once fired on
this machine.** `slugify_project_dir` (`claude_transcript_correlate.py:191`)
mapped only `/` and `.`. The real rule maps EVERYTHING outside `[A-Za-z0-9-]`
to a single `-`, verified against **908 of 919** live transcripts. Every one of
the owner's paths contains a space and two tildes, so every slug the ladder
built was wrong and no fallback had ever succeeded here. **A ladder that cannot
fire is not a fallback**, and the failure was invisible because a fallback that
never matches looks exactly like a fallback that was never needed.
`src/core/claude_project_dirs.py` (260 lines) is the corrected rule.

**`agent_type` does NOT share this cause.** A crosstab over all 39 rows finds
**14 with `agent_type` NULL and a hook-written uuid**, so the two failures are
independent and **backlog item 3 stays its own job.**

**26. A THIRD GROUP NOBODY HAD COUNTED: rows whose recorded uuid has NO
transcript on disk.** Over 39 rows: **16 absent, 18 sound, 5 phantom.** All 5
phantoms COLLIDE, because the correct uuid is already held by a sibling row
split off by the cwd spelling trap. The pairs are **7 to 4, 9 to 11, 10 to 12,
38 to 39.**

Worked example, because the mechanism is the point. Row 7 is LIVE, its
`working_dir` is the SHORT symlink spelling, and its uuid
`db81f6bf-85f9-448b-a7f6-bc83f62659d9` exists nowhere on disk. Row 4 is
ARCHIVED, carries the long iCloud spelling, and its uuid `82854c0e-...` has a
transcript that very much exists. The live pane is literally running
`--resume 82854c0e-... --fork-session`. **`--fork-session` MINTS A NEW uuid**,
the hook recorded that new one, and the forked transcript never materialised.
So the picker truthfully said "no transcript for db81f6bf" about a uuid that
has nothing to do with the conversation the user is in, while the real
conversation sat on the archived twin. The message was correct and the
conclusion a reader draws from it is wrong.

**THE BACKFILL, DRY RUN ONLY, NOTHING WRITTEN.**
`scripts/backfill_claude_session_uuid.py` (392 lines) proposes a uuid for a row
that lacks one. It is dry run by default. Results over the live corpus:

| outcome | rows |
|---|---|
| confident FILL | 8: rows 14, 15, 16, 17, 19, 23, 24, 25 |
| confident REPLACE | 0 |
| ambiguous | 8: rows 26, 27, 30, 31, 33, 35, 40, 42, at 45 to 78 candidates each |
| no candidate | 1: row 41 |
| collision pair | 4 pairs, listed above |

Row 41's `working_dir` is literally `/Users/jsugamele/Development/ses_ee8d919b`,
which is not a project directory, so no candidate is the right answer there.

Both cwd spellings are resolved FORWARD, by slugifying each spelling found in a
HOME symlink scan, AND BACKWARD, from the transcript's own recorded cwd
canonicalised. The backward direction is not optional: **754 of 919 transcripts
sit in a directory that disagrees with their own recorded cwd.**

**TWO MATCHER DEFECTS WERE CAUGHT BY CONTROLS, NOT BY READING.** Worth keeping
because both would have shipped a matcher that looked right.

- Directory agreement was clearing a two-signal bar as ONE FACT CORROBORATING
  ITSELF. Evidence is now counted in independent FAMILIES.
- The negative control, a row pointed at a project that does not exist while
  real transcripts are present, initially returned `ambiguous` rather than
  `no_candidate`, because timing alone was counting as evidence. An ANCHOR GATE
  now lets timing and title CORROBORATE a candidate and never CREATE one.
  **A matcher that always finds something is worse than useless.**

New modules from `8dd54a8`: `src/core/session_uuid_backfill.py` (358),
`src/core/session_uuid_backfill_rules.py` (371),
`src/core/session_uuid_backfill_report.py` (312),
`src/core/claude_project_dirs.py` (260),
`src/core/session_lineage_recovery.py` (171), plus the script. Tests:
`tests/test_claude_project_dirs.py`, `tests/test_session_lineage_recovery.py`,
`tests/test_session_uuid_backfill.py`. 33 tests added, each fix proven by
REVERTING it. pytest went 4823 to 4874 passed with the same 3 pre-existing
environmental failures.

**STILL OPEN on this item:** the code is not deployed, and no write has been
authorised. See the decisions entry below.

---

### 2026-09-07 - the restart picker painted its option text over the next row - DONE (code, `0793eb1`), OPEN (deploy)

Option rows inherited `flex-shrink: 1` inside a `max-height: 46dvh` flex
column, so the flex algorithm SQUASHED THE ROWS instead of scrolling the list.
Measured: **113px of content in a 62px box on desktop, 145px in a 58px box on a
phone.** The fix is `flex-shrink: 0`.

**Verified by GEOMETRY, not by DOM text.** `tests/test_restart_picker_geometry.py`
(649 lines) asserts that no row's text intersects another row's title, and it
was PROVEN TO FAIL on the old code with **19px of text sitting on text**. A
test that has not been shown failing on the broken version is not evidence.
Screenshots are committed alongside it.

`client/js/session-restart-options.js` (350 lines) was extracted from
`session-restart-picker.js` in the same change, which takes the picker from 578
lines back down under the 500-line rule.

---

### 2026-09-08 - CORRECTIONS: four earlier conclusions were WRONG

Recorded per the convention at the top of this file. The wrong entry plus its
correction beats a clean lie.

**CORRECTION to item 21, the openrsync diagnosis.** `deploy-mini.sh` did NOT
fail because openrsync rejects `--files-from=- --relative`. It handles that
combination fine, proven by a successful copy. It fails on **a remote
destination containing SPACES**, which the remote shell word-splits. Both live
destinations contain spaces and the v11 staging path does not, so **`--target
live` could never work while `--target v11` always did.** That asymmetry is the
whole reason the bug hid for as long as it did: every test anyone ran by habit
went to v11. Fixed in `c779afb` by moving the transfer to tar over ssh, where
the remote command is one quoted string the script controls.

**CORRECTION to the tmux identity premise.** Killing and respawning does NOT
move the instance triple. Measured on tmux 3.7c: `session_created` held at
**1788821572** and `pane_id` at **`%0`** across `respawn-pane -k`, and only
`pane_pid` changed. The reason is structural, not luck: `session_created`
belongs to the SESSION and `-k` replaces the PANE'S PROCESS.
`src/core/session_instance_rekey.py` still measures the epoch either side and
answers `unchanged` / `rekeyed` / `cannot_determine`, because a measurement that
can stop being true is not a thing to assume.

**CORRECTION: "Media Compression's conversation is gone" was WRONG, and the app
said it too.** The transcript `82854c0e-a423-4591-a34f-a14cb92fbf41.jsonl`
exists and is **73,190,422 bytes**. See the phantom-uuid entry above for why
the app's message was locally truthful and globally misleading.

**CORRECTION: the `CLAUDE.md` pytest baseline was stale by an order of
magnitude.** The repo `venv` symlink pointed at a deleted `venv.nosync`
directory, exactly the failure mode `CLAUDE.md` warns about, and the suite
limped along undercounting rather than failing outright. Real numbers after
rebuilding: **3 failed / 4874 passed / 12 skipped.** The three are the same
environmental ones: `test_home_write_guard`, `test_state_dir_resolution`,
`test_version_probe`. Node: 169 files, only `test_archive_full_page_mode.node.mjs`
fails, also pre-existing.

---

### 2026-09-08 - the bare-shell exposure, restated correctly

This was MISSTATED ONCE ALREADY, so the correct form in full:

- **18 of 22 live sessions have an empty `#{pane_start_command}`.** Measured
  2026-09-08. Earlier entries say 15 of 19; that was 2026-09-07 and the
  population moved.
- An UNPICKED restart on such a session lands on the `RESPAWN_SHELL` rung and
  hands back a login shell.
- **BUT an explicit wrapper choice OVERRIDES that gate**
  (`src/core/session_respawn.py:542`). Picking a wrapper in the picker starts
  the agent properly.

So the exposure is real and it is NOT unavoidable: only an unpicked restart
lands on the shell rung. Do not record this as "the restart button gives you a
shell" without the second half.

---

### 2026-09-08 - item 24 recount

**24.** `'ProjectsView' object has no attribute 'read_only'` at
`src/core/upload_sweeper.py:155`. Now **56 occurrences since 2026-08-29**, and
it fires TWICE PER BOOT. Still pre-existing, still not introduced by any
2026-09-07 or 2026-09-08 work. Unchanged in substance from the entry above,
recounted so the number is current.

---

### 2026-09-08 - item 23 is still latent

**23.** The deploy copies with `ditto`, which MERGES. The first commit that
DELETES a file will leave the stale copy on BOTH targets and still report
success, because verification only hashes files that SHOULD be present. **No
deletions have shipped yet**, so this has not bitten. Both undeployed commits
(`0793eb1`, `8dd54a8`) are additions and edits, no removals, so deploying them
does not trip it either. Do it before a deletion ships.

---

### 2026-09-08 - HAZARD: two Claude Code sessions were editing branch `v1.1` at once

A SECOND Claude Code session was working this same branch concurrently for part
of 2026-09-07. It committed `32052d1`. It has since exited. Nothing was
clobbered that anyone found, but **two agents on one branch is how work gets
clobbered**, and the second agent's commits are indistinguishable from the
first's in the log. If a commit in this range does something you did not expect,
that is the likely explanation. Recorded as a process hazard, not as a defect.

---

### 2026-09-08 - browser control, and the only route to it

Claude in Chrome is PAIRED and INSTALLED: `pairedDeviceName = Browser 2`,
`hasCompletedClaudeInChromeOnboarding = true`. But
`claudeInChromeDefaultEnabled = false`, so **browser tools exist ONLY in a
session launched with `--chrome`**, which is the `claude-chrome` wrapper. Tools
BIND AT SESSION START, so browser control cannot be added to a conversation
already running.

**The wrapper picker is the intended route**: restart a session and choose
`claude-chrome`. That is what the picker was built for, and it is now the
supported answer to "can this session drive the browser". Note this is also why
the picker layout fix (`0793eb1`) matters more than it looks: it is the control
surface for this.

---

### 2026-09-08 - OPEN DECISIONS AWAITING THE OWNER

Three, none of them started, all of them blocked on a human answer.

1. **Authorise writing the 8 confident FILLs** from the backfill dry run (rows
   14, 15, 16, 17, 19, 23, 24, 25). Nothing has been written. The tool is dry
   run by default and stays that way until told otherwise.
2. **Merge the 4 duplicate row pairs** (7 to 4, 9 to 11, 10 to 12, 38 to 39).
   This is the ALREADY-AUTHORISED `(old path)` merge family, item 5, and it
   still carries its condition: **take a VERIFIED backup first. Every
   pre-existing backup in that directory is 65 to 115 KB against a 4.5 GB
   database and would be useless as a rollback.** A backup that cannot restore
   is not a backup.
3. **Deploy `0793eb1` and `8dd54a8`.** Live is on `0b12edf`.

### 2026-09-08 owner note: clean up database backups when the row repairs are done

- [ ] When the Media Compression row repair and the duplicate-row merges are finished and verified, delete the extraneous cloude.db backups: every pre-existing 65-115 KB backup in the backup directory (useless as rollbacks against a 4.5 GB file) and any full-size dated backups taken for the repairs, once a restart has proven the repaired rows work. Keep exactly one verified full backup until then. Owner request, 2026-09-08.

### 2026-09-08 security note from the Desktop inventory

- [ ] `~/.config/restic/mini-m4.pw` is a plaintext restic repository password on disk. It was surfaced into a subagent transcript while reading backup config, and the file also sits inside the Time Machine backup. Owner decision: rotate the restic repo password and move it to `op://Claude/`, per rules/secrets-protocol.md. Deferred to the owner (credential rotation is his call; see the earlier decision to defer rotations until this project is done).
- [ ] restic on the mini covers only `ai-setup` and `docker-management`; the Desktop is covered by Time Machine to the NAS only. Decide whether the restic scope should widen.

### 2026-09-08 INFRA-49 fresh evidence

- [ ] The boot re-adopt agent measured `test_session_startup_gate.py::test_manager_never_captures_a_tail_for_a_session_with_a_hook` failing because of leaked `keeper` tmux sessions from the respawn/restart suites, and `test_hook_driven_status.py::test_list_attachable_sessions_maps_tmux_status_with_unread` hitting the production socket guard. Order-dependent, both pass in isolation. Live socket checked at 15:40 local: 21 sessions, all real, no leak present now. INFRA-49 (tests on the live `cloude` socket) is now causing flaky failures, not only risk. Raise priority.

### 2026-09-08 evening - punchlist items closed today, twenty commits `018f2a7..1a28b23`

DEPLOY STATE: HEAD is `1a28b23`, 20 commits past this morning's `018f2a7`, NOT
pushed (deliberate hold, `git status` reads "ahead of origin/v1.1 by 20
commits"). Live on mac-mini-m4 was `0b12edf` this morning and has since moved
to `c9cd9ab` (picks up `0793eb1` and `8dd54a8`). A deploy of HEAD is IN
PROGRESS as a separate concurrent task at the time of this entry - it will run
the v24 migration and the app's first real boot re-adopt. Treat this as
"in progress," not "done," until checked directly on the mini. Full
commit-by-commit table in `HANDOFF.md` section 8.

- [x] **Item 6.** Sidebar group ellipsis popup rendered behind the sidebar
  panel. Fixed by raising its z-index. Commit `43e8fc2`.
- [x] **Item 13.** Home screen repainted the whole project tree from a 5s
  poll with no guard (385 elements + 409 text nodes destroyed and recreated
  per tick). Fixed by gating the repaint on whether anything actually
  changed. Commit `ee5d547`.
- [x] **Item 14.** A new project could not choose its own folder; the typed
  name was never used for the directory. Fixed. Commit `a4eeef1`.
- [x] **Item 15.** `POST /api/v1/sessions` could not set a title; every
  UI-created session landed `title = NULL`. Server-side `label` field
  shipped in `06bacd6`; both claude-launching create paths (new project,
  open-existing-project) now send it in `e7ca5ec`.
- [x] **Item 17.** A server restart unbound every session but one, so they
  rendered "adoptable" rather than "running" until opened. Fixed by holding
  every surviving session through boot re-adopt, not just the last one.
  Commit `bca7069`. A related bug in the same area, a reused tmux name
  hiding a session the user had deleted from the RECENT list, was fixed
  alongside it in `986c50e`.
- [x] **Item 18.** The folder path overran its box in the project modals.
  Fixed with `word-break` / `overflow-wrap`. Commit `9009588`.
- [x] **Item 18a.** The project modal displayed and WROTE the short,
  symlinked cwd spelling (`/Users/jsugamele/Development/...`), the same trap
  that manufactured the `(old path)` project rows. Fixed as part of the
  project-folder-picker work. Commit `a4eeef1`. See also the qualification
  added to `CLAUDE.md` gotcha 6 today: claude 2.1.263 itself now resolves
  symlinks before slugging, so new splits cannot originate from claude, but
  this app was still writing the short spelling until this commit.
- [x] **Item 19.** A session parked on an unanswered folder-trust prompt
  painted `Connected` with a PID and no signal. Fixed: a hook-absence signal
  plus a scrollback probe now distinguish `ready` / `awaiting_startup_prompt`
  / `unknown`, never guessing. Commit `9cdcb90`.
- [x] **Item 23.** The deploy copied with `ditto`, which merges rather than
  mirrors, so a deleted file would survive on both targets and still verify
  as a success. Fixed: deploy now mirrors `src/client` instead of merging.
  Commit `9adaac9`.
- [x] **Item 24.** `'ProjectsView' object has no attribute 'read_only'` at
  `src/core/upload_sweeper.py:155`, firing twice per boot since 2026-08-29.
  Fixed: reads the real `ProjectsView.writable` attribute. Commit `bc65ef4`.
- [x] **Items 25/27 (database operation, no commit).** `agent_type` filled on
  15 running rows from measured argv plus environment disambiguation
  (`cld` / `cldor` / `claude-skip-permissions` share an identical flag set;
  separated by presence of `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_BASE_URL`).
  8 of the confident backfill FILLs (rows 14, 15, 16, 17, 19, 23, 24, 25)
  authorised and written. `lsof` confirmed claude does NOT hold its
  transcript file open, a hypothesis that would have blocked safe editing if
  true (measured negative).
- [x] **Items 5/26 (database operation, no commit).** The 3 "phantom pair"
  entries (9/11, 10/12, 38/39) were INVERTED in the earlier note: the live
  rows already held the real conversation ids, and it was their dead twins
  holding phantom ones. The dead twins were retired (archived), not merged
  over the live rows. Ghost rows 44 and 47 (no session or transcript behind
  them) were retired too. Remaining phantom-uuid rows: 41 (kept
  deliberately) and 46 (a newborn session, transcript not yet landed -
  re-check, do not treat as settled).
- [x] **Item 10.** Durable order within a group needed a `position` column on
  `session_group_members`, done in the same change as re-keying that table
  from `tmux_name` to `session_uuid`. Commit `24d25b9`.
- [x] **The "show archived" toggle rename.** The RECENT list's control used
  to say "deleted," which told the user their history was gone when it was
  one checkbox away; it now says "show archived," matching what
  `archive_session` actually does (stamps `archived_at`, keeps every
  column). Commit `2b93428`.
- [x] **Wrapper pills.** A running-session row now shows which launch
  wrapper it is running, read from the record rather than guessed. Commit
  `c89ef55`.
- [x] **Naming.** One name per session, last rename wins from either side;
  the pane's typed name is read from the transcript tail since no hook
  event carries a `/rename`. Commit `06bacd6`. A new session's label is now
  sent from both claude-launching create paths. Commit `e7ca5ec`.
- [x] **Import.** `~/.claude/projects` held 1,486 top-level transcripts
  against 43 session rows; everything else was invisible to the app. Script
  landed in `b18f018`; run today (database operation, no further commit):
  895 conversations imported as archived sessions, 59 archived projects
  created, 13 scratch conversations excluded, 224 `agent-*.jsonl` subagent
  files correctly identified as non-sessions and skipped, 319 files with no
  recoverable cwd. Dry-run report: `.claude/notes/import-dry-run-2026-09-08.md`.
- [x] **Lineage guard.** A second claude process started under one pane
  (for example by `claude` invoked from inside another session's shell) was
  being treated as a fork of this app's own session. Fixed: no longer
  misattributed. Commit `2071963`.

### 2026-09-08 evening - additional verification done, no code change

- [x] Restart proven live on three real sessions - Media Compression, Agent
  Cloude Code, Fantasy Football 2026 - same tmux session, same conversation
  resumed, wrapper switched to `claude-chrome`, `agent_type` persisted to the
  row. This is the end-to-end proof behind items 22/27's "restart means
  resume" claim.
- [x] Ingester proven live with a positive control: this transcript itself
  grew during the check and was re-archived, file count moved 19,223 to
  19,255, 0 missing. INFRA-105 ("`workflows/` is never walked") is REFUTED:
  457/457 files there are archived.
- [x] Desktop `~/Desktop/Backups`: 13 transcripts existed ONLY there (not in
  the corpus). Copied in and archived 13/13; verified against the TrueNAS
  archive bundle (`05-desktop-backups.tar.zst`,
  `/mnt/ARCHIVE/vault/85_cloud-exports/claude/claude-archive-20260830/` on
  10.0.1.237) by manifest hash, 13/13; Time Machine on 10.0.1.202 separately
  holds all 10,431 files. The folder itself can now be deleted (owner's
  call, see the new open item below).
- [x] Media project's `.claude/settings.json` had five `Write(...)`
  permission rules where `Edit(...)` was meant, the source of its yellow
  startup warnings. Rewritten to `Edit(...)`.

### 2026-09-08 evening - new open items

- [ ] **Restart drops claude's own `--name`.** `resume_extra_args` carries
  only `--resume <uuid>`, so a resumed session comes back without whatever
  name claude itself had (`--name`, `/rename`). The app's row title
  (`sessions.title`) is unaffected because it lives outside claude's argv.
  Fix would reuse `claude_title_sync`'s transcript read to reapply the name
  on resume. Documented in `CLAUDE.md`'s restart section today.
- [ ] **A deferred browser-rename push is never retried.** When `/rename`'s
  push defers because the transcript is not there yet (measured absence),
  nothing re-attempts it once the file lands. `title != claude_title` is the
  marker a retry would key on.
- [ ] **`FALLBACK_PROJECTS_ROOT` hardcodes `/Users/jsugamele`**
  (`src/core/project_directory.py:85`). Fallback-only path, but a hardcoded
  home directory in shared code is wrong on any other machine.
- [ ] **The "four oversized test files" premise was WRONG - there are far
  more.** `wc -l tests/*.py tests/*.mjs | awk '$1>500'` finds 48 files over
  500 lines, not four. Worst offenders: `test_session_backend.py` (2125),
  `test_session_row_menu_renders.py` (1036), `test_sidebar_sessions.node.mjs`
  (998), `test_theme_audio.node.mjs` (796), `test_session_restart_wrapper_choice.py`
  (793), `test_session_restart_resumes_the_conversation.py` (770),
  `test_restart_picker_renders.py` (768), `test_boot_readopt.py` (763),
  `test_archive_overlay.py` (754). CLAUDE.md's 500-line guideline names five
  production files as already past it; it says nothing about tests, and this
  count suggests the guideline has never been enforced there. Re-scope or
  explicitly exempt tests before quoting a number again.
- [ ] **gitleaks is not installed on the mini.** The pre-commit hook's
  second gate (`scripts/install-secret-hook.sh`) silently never runs its
  gitleaks stage; only the first gate (`message_model_secrets.py`-based
  scan) is active. Install it or stop documenting a two-gate hook as if both
  gates run.
- [ ] Delete `~/Desktop/Backups` on this Mac now that its 13 transcripts are
  archived, manifest-verified against the TrueNAS bundle, and also covered
  by Time Machine on 10.0.1.202. Owner's call; nothing depends on the folder
  surviving.

### 2026-09-08 browser re-test on the redeployed build (01ebb85 + 41a89f1)

- [x] Boot re-adopt: 21 of 21 held under stored ids after restart, 0 `adopted:` re-mints (verified on live by the deploy agent).
- [x] Cards: family pill `claude` on all 21, wrapper pill `claude (chrome)` on the three chrome sessions.
- [x] Archived toggle: 13 recent -> 912 with the box checked (895 imported visible).
- [x] New project flow: name -> folder step (parent defaulted to the long-spelling Development root, full path shown) -> session in `.../Development/Punchlist Two`, row working_dir long spelling, claude launched with `--name`, wrapper and conversation id recorded (row 943).
- [x] Rename TUI -> browser: `/rename` in the pane reached the header on the next hook event; toast carries the new name.
- [x] Rename browser -> TUI: header rename appended a `custom-title` record to the transcript (`claude_rename_push_landed`); the running TUI status line stays stale until that process restarts (documented limitation).
- [ ] Row `title` NULL at create despite `--name` landing (row 943): fix in flight.
- [ ] Test artifacts to archive when the owner says so: sessions `Punchlist Test` (45), `Punchlist Browser Rename(fork)` (46), `Punchlist Two` (943); folders `.../Development/ses_5a756046` and `.../Development/Punchlist Two`.
- [ ] This orchestrator session (`Agent - Cloude Code`) carries a rotated hook token in its running process (hooks 403 until restarted); a restart through the picker resumes the conversation and fixes it.
- [ ] Wrapper pill reads `claude` for `claude-skip-permissions`; confirm that is the configured label, not a fallback.

### 2026-09-08 owner report: "we lost some of our conversation"

- [ ] Display-side loss, not data loss. After the app restarts and re-attaches to a running pane, the client fetches `include_scrollback=1` and repaints the terminal from tmux capture-pane. Claude Code TUI draws in place, so the pane has `history_size=0` and the capture is one screen; the browser replaces the buffer it had built over the websocket with that screen. Transcript verified intact (7.6 MB, one compaction marker from the morning /compact, last write 13:29). Fix direction: keep the existing client buffer when the incoming capture is shorter than what the client holds, or replay the tail of the server pipe stream on attach. Explorer report pending; then build.

### 2026-09-08 restart procedure finding

- [x] Restarting the menubar app with osascript quit plus `open -a` relaunches it under an ad hoc launchd job (`application.com.cloudecode.menubar.*`) with stdout to /dev/null, so `/tmp/cloudecode-menubar.log` stops growing and the app runs outside its LaunchAgent. Correct restart: `launchctl kickstart -k gui/$(id -u)/com.cloudecode.menubar`. The server also writes its own log to `~/Library/Application Support/cloude-code-menubar/logs/server.log` regardless. Being added to docs/deploy-mini.md by the reconnect agent.
- [ ] Reconnect buffer keep (d70e4ab): keep path verified firing in the browser console; the resize nudge that follows made the TUI repaint the viewport, so on-screen rows were lost while scrollback is expected to survive. Measurement of the exact resize escape sequences and the nudge decision in progress.

### 2026-09-08 the eraser is a geometry flap, not the reconnect

- [ ] Console evidence on the Punchlist Two tab: terminal rows flap 41 -> 45 -> 41 around websocket connect/reconnect (`source=ResizeObserver`), and claude answers any geometry change with `ESC[2J` (measured: a same-size resize emits zero bytes, a different size emits a viewport clear plus redraw). The reconnect keep path (f5156aa) works and is not the eraser; an in-flow page element changing the terminal container height by four rows is. Culprit hunt and overlay fix in progress; also a transient-resize guard in the ResizeObserver path.

### 2026-09-08 eraser hunt, instrumented

- [x] Ruled out by measurement: the reconnect (keep path fires, no reset), the resize nudge (removed in f5156aa), the local-servers panel (overlaid in 3ef5624), the renderer and the font (cell 8x16 webgl on every resize line, 2361b33 instrumentation).
- [ ] Arithmetic on the instrumented lines: 45 rows x 16 px = 720 px at reconnect, 41 x 16 = 656 px ten seconds later; `#terminal` measures 668 px after the flap; the only in-flow sibling is the 43 px `.info` status bar under the terminal. It is absent or collapsed at reconnect and appears once session info lands, stealing four rows and triggering claude ESC[2J. Fix in progress: reserve its height permanently or take it out of flow.

### 2026-09-08 eraser hunt, closed

- [x] Real in-app causes, both fixed and deployed: the local-servers panel toggling in flow under the terminal (3ef5624, now an overlay) and the bottom status bar growing when its three late writers fill it (afbe005, height reserved via a named token). Reconnect keeps the browser buffer (d70e4ab, f5156aa). A settle guard drops transient unannounced resizes (3ef5624). `[TERM-RESIZE]` prints cell, renderer and box heights; `[TERM-BAR]` logs status-bar height changes.
- [x] The residual flap seen during testing was the Brave automation infobar: the extension attaches a debugger on each action, the viewport shrinks 56 px, and detaches when idle (`screen=797` idle vs `screen=741` attached, info=43 both). Not an app defect.
- [ ] Inherent limit, documented: claude 2.1.263 answers any real geometry change with ESC[2J, so a phone rotation, the on-screen keyboard, or a sidebar toggle still clears the visible screen. The transcript is never affected. Only spurious resizes are avoidable, and those are now avoided.

### 2026-09-08 late findings

- [x] Fork verified end to end in the browser: the forked session answered from the parent conversation history and took a new prompt, hooks and toast working (row 46).
- [ ] Deep link rejects a session whose name contains parentheses: `/session/Punchlist%20Browser%20Rename(fork)` bounces home with "Invalid project name in URL"; the card click works and lands on `/session/Punchlist_Browser_Rename_fork`. The router validation and the slug the card uses disagree. Fix: accept the same set of names the app itself creates (fork labels carry `(fork)`), or link cards and deep links by the same slug.
- [ ] Rename status-line staleness: after a browser rename the running TUI keeps its old name in the status line until that claude process restarts; the transcript carries the new name. Documented limitation, revisit only if claude gains an external rename signal.

### 2026-09-08 after the import, what the archived list looks like

- [ ] Owner decision: of the 895 imported conversations, 512 look interactive, 256 are `scheduled task: ...` runs (media-refetch-wave-advance, ptimc-email-reply-poll and the like) and 127 are headless or delegated prompts ("You are a code splitting subagent...", "Implement the following plan...", "Reply with exactly OK"). All are real transcripts; as launcher rows they bury the interactive ones. Options: a second filter on the archived list (hide automated runs), or tag them at import (`origin` stays imported, add a `kind`) and default the list to interactive only.
- [ ] `SessionInfo` carries no `created_at_epoch`, so the instance-exact join in `launchpad.js` (`_buildProjectSessionGroups`, the `:3900` join) can never fire for an open session and always degrades to the name match. Cosmetic today; a fallback that cannot fire. Found by the attribution agent, not fixed.
- [x] Attribution invariant enforced (7bd55fd, e73c1a7): ladder as-written -> canonical spelling -> mint a project -> none only for scratch; the pair (project_id, project_attribution) moves together or not at all; archived roots take no part in matching; create_project adopts project-less live rows under a new root (item 16 closed). Live: 19 running, 0 without a project.

### 2026-09-08 status light work, late findings

- [x] LED status light (0fc23a5, 4215ad0): two independent rings, inner = chat status (working, waiting-permission, waiting-input, done, dead, unknown), outer = attention (active breathing, steady, unread, off, dim); one component behind the sidebar row, launchpad card, project tree and terminal header; group headers summarise children (waiting > working > unread > done > dead > unknown). Full model in docs/session-status.md.
- [x] The tmux fallback turned any non-shell foreground process into `working` with no expiry; 15 of 19 panes hit it because claude renames its own process. Now `unknown` until a hook answers.
- [x] Unread persisted by `<tmux_name>@<session_created>` so a killed session cannot hand its flag to its successor.
- [ ] A second session, `ses_68c185ce`, has 2,757 hook POSTs rejected for a stale token (same class as this orchestrator session). Find how its token went stale and add a self-heal: a rejected hook for a pane that has a row should re-issue the token into the pane env and log once.
- [ ] `waiting-permission` is in the LED vocabulary but unreachable: the server folds `PermissionRequest` and `Notification` into one `question` state. Split them server-side so a permission prompt can read differently from a plain notification.
- [x] Concurrency incident: the recent roll-up agent committed from a stale tree (3732bdf) and reverted 15 files of the LED work; restored byte for byte in 4215ad0. Rule for future agents: build commits from HEAD plus own hunks only.

### 2026-09-08 herdr teardown (github.com/herdrdev/herdr), candidates only

- [ ] Automation primitives: `agent wait --until blocked`, `agent prompt --wait`, JSON socket API. Fits our hook state; needs a wait endpoint and a control API.
- [ ] Status "explain": a verdict that reports its source, matched rule, evidence and a named fallback reason. Add on top of the LED state machine; today `unknown` cannot say why.
- [ ] Sequence-numbered seen/done per source (`state_change_seq`), stale reports accepted and ignored. Stricter than our last-write-wins unread; consider for the unordered-hook problem.
- [ ] OS-level notification when an agent blocks (web push or APNs); today only an open tab toasts.
- [ ] Multi-machine in one client (saved SSH machines); our model assumes one tmux socket on one box.
- [ ] Git worktree workspaces grouped with the parent repo; one more project kind.
- [ ] Other agents via screen-scrape manifests; only Claude gives us hooks.
- [ ] Real-hook LED integration test: launch a real claude on a throwaway tmux socket with the hook URL pointed at the app in test mode, drive prompts, assert the LED after each real event; skip when the binary is absent. Owner asked whether possible: yes, not yet built.

---

### 2026-09-08 late round closed out, 455d692..8ee40d1 (12 commits), deployed live

DEPLOY STATE: live on mac-mini-m4 runs HEAD as of the `8ee40d1` deploy. Boot
held 19 sessions (18 plus 1 benign skip). Every running session carries a
project. Session lists show interactive conversations only: 645 archived
rows visible, 270 automated excluded by default, 305 unknown kept (kind is
unknown, not evidence of automation).

- [x] **Deep link fork-label rejection.** A session named `<title>(fork)`
  bounced the deep link home with "Invalid project name in URL" while its
  card click worked. Fixed with a permissive validator that rejects only
  what is actually hostile in a URL segment, and a title-fallback resolver
  so a link built from a display title resolves the same session a
  tmux-slug link does. Commit `ee3133c`.
- [x] **Attribution invariant, half-write.** `claim_instance`'s "only when
  not None" column policy let a derived `(None, 'none')` write the
  attribution alone, leaving a row with a real `project_id` rendering as
  "no project" because the tree checks `attribution === 'none'` first. The
  id and the attribution now move together or neither moves. Also closed:
  the lexical matcher couldn't cross a symlink spelling, so a project
  declared at an iCloud path never matched sessions probed at the `~`
  symlink; canonicalising is now a fallback rung. Commit `7bd55fd`.
- [x] **Attribution invariant, archived catch-all.** An archived project
  rooted at the owner's home directory contained every session on the
  machine and won the as-written match rung before the canonical rung
  (the one that finds the real project) ever ran, so two live rows were
  attributed to a project the user cannot see. Archived roots now take no
  part in matching. Commit `e73c1a7`. Live, verified: 19 running sessions,
  0 without a project.
- [x] **Wording purge: archive, not delete.** Owner's instruction verbatim:
  "remove the deleted wording, its archived." The project row's hard-delete
  trash button is gone (archive/unarchive was already correct and stays);
  every other user-facing delete/deleted/deletion string in `client/js` and
  `client/index.html` is renamed to archive/archived (recoverable actions)
  or remove/removed (genuinely irrecoverable ones, matching this app's
  existing verb for that class). New node test tokenizes every JS string
  literal and HTML text node and asserts none read delete/deleted/deletion.
  Commit `026c7ac`.
- [x] **Session kind: interactive / automated / unknown.** Owner's rule
  verbatim: "lists should always just be mine. the rest can be found in the
  archive explorer." `sessions.kind` (schema v25) is nullable with no SQL
  default, stamped only by evidence the machinery itself wrote (a
  `<scheduled-task>` tag, or `entrypoint='sdk-cli'`) so a title can never
  create an `automated` verdict. `/sessions/records` and `/sessions/recent`
  exclude `kind='automated'` by default; `/archive` is untouched and still
  shows everything, asserted structurally. Measured over the 895 imported
  rows: 320 interactive, 270 automated, 305 unknown (an era before the
  `entrypoint` field existed, not a gap). Commit `2b0ed7d`.
- [x] **LED: two independent rings, and its accidental revert.** `0fc23a5`
  shipped the two-ring LED (inner = chat status, outer = activity/attention)
  replacing the old single-dot model that could not say "working, and also
  unread" at the same time; stopped a tmux `running` pane from reporting a
  permanent unmeasured `working` (measured: 15 of 19 live sessions report a
  claude version string as `pane_current_command`, so this was the common
  case, not the rare one). `3732bdf`, landed seconds later from a stale
  tree, reverted all fifteen of those files (2,132 deletions) as a side
  effect of an unrelated launchpad fix; nothing in its message flagged it.
  `4215ad0` restored all fifteen byte-for-byte and touched none of the
  three files `3732bdf` legitimately owned. Commits `0fc23a5`, `3732bdf`,
  `4215ad0`. **Rule for every future agent on a shared branch: build your
  commit from HEAD plus your own hunks via a private index, verify
  `git diff origin/v1.1 --stat` shows only your paths, and never run
  `git reset --hard`.**
- [x] **LED halo, sized down.** The sidebar row and the launchpad card both
  render the LED at the 9px default with no per-call override anywhere in
  the codebase, so the old `--led-halo-scale` (2.6) / `--led-glow-spread`
  (0.62) put the whole lit object at ~35px across at the breathing peak -
  bigger than the row text, overlapping neighbours. Dropped to 1.7 / 0.3
  (~21px peak) and the breathing keyframe's peak scale from 1.06 to 1, so
  the animation no longer grows the halo past its resting size. Commit
  `8ee40d1`. Reference gallery (all LED states enumerated, pending the
  owner's sign-off on the resized version):
  https://claude.ai/code/artifact/aac4e1df-56aa-44e7-a444-6d1e1fc48627
- [x] **Recent section collapse toggle.** `#recent-sessions-toggle` has
  rendered as a real disclosure button since `b1365a2` but was never wired
  into `initSectionDisclosures()`, so clicking it did nothing - no click
  handler existed to clobber. Wired up with the same persistence and
  re-apply-on-load as its two siblings; the recent section's header now
  matches the projects header's layout (show-archived control right-aligned
  in the same row). Commit `3732bdf`.
- [x] **Project gutter alignment.** Styling fix bringing the project tree's
  left gutter into alignment across rows, landed alongside the day's LED
  TODO notes. Commit `d6e4883`; test `test_project_gutter_alignment.node.mjs`.
- [x] **Documentation only, no code.** `4ae9965` (import noise counts, the
  dead epoch fallback, attribution invariant closed), `e8cbc79` (herdr
  teardown candidates, the real-hook LED test idea).

**Verified in the browser today, end to end:** fork and rename; new project
flow (name to folder step, long-spelling root, session launched with
`--name`, wrapper and conversation id recorded); the archived toggle;
recent-section collapse and its toggle placement; project gutter alignment.

**Test artifacts.** All prior test sessions and scratch folders created
during this work were archived through the app. The two empty test folders
and `~/Desktop/Backups` are gone - the owner deleted them himself.

**Carried forward, still open (unchanged by today's round, listed here so
this closing section doesn't bury them):**
- `waiting-permission` LED state is in the vocabulary and unreachable from
  live data until the server splits `PermissionRequest` from `Notification`
  (both fold into `question` today). See `docs/session-status.md`.
- Stale hook tokens: this orchestrator session and `ses_68c185ce`
  (2,757 rejected POSTs) both need a self-heal - a rejected hook for a pane
  that still has a row should re-issue the token into the pane env and log
  once, rather than degrading silently into the tmux fallback tier.
- The real-hook LED integration test (launch a real claude on a throwaway
  socket, drive prompts, assert the LED after each event) is offered, not
  built.
- `SessionInfo` carries no `created_at_epoch`; the instance-exact join in
  `launchpad.js` degrades to a name match for every open session. Cosmetic
  today, a fallback that cannot fire.
- The herdr teardown candidates (see the 2026-09-08 herdr section above).
- **Owner action needed:** confirm the LED states rendered in the gallery
  artifact above (particularly the resized halo from `8ee40d1`) before
  treating the visual design as settled.

### 2026-09-08 final round closed out, 117823d..6934965 (3 commits)

Picks up right where the late round above left off. Deploy state for this
round: another agent was deploying HEAD as this section was written, so
treat it as **deploy in progress at time of writing** - do not read
"live" anywhere below as confirmed for these three commits until the next
session checks the running commit directly.

- [x] **LED halo, sized down again, per owner calibration.** The owner
  looked at the gallery artifact from the late round and called it: the
  halo should be 1 to 2px larger than the dot, not a wide bloom.
  `--led-halo-scale` 1.7 -> 1.3, `--led-glow-spread` from a scale-relative
  0.3x to a fixed 1.5px. At the 9px default the lit object goes from ~21px
  to ~14.7px across. Breathing amplitude tightened 0.9 -> 0.92 so the
  animation stays inside that new size. Pinned numbers updated in
  `tests/test_status_led.node.mjs` and `docs/session-status.md`. Commit
  `e7a212e`.
- [x] **`question` split into `question` (blocked) and `notice` (not
  blocked).** A `PermissionRequest` halts claude mid-turn until a human
  answers yes or no; a `Notification` is claude asking to be looked at
  while nothing is blocked. One state named `question` carried both, so a
  chatty session painted identically to a parked one - the false-urgency
  twin of this project's recurring false-green problem. Two independent
  booleans, `permission_open` and `notice_open`, not one field with three
  values, because hook events arrive unordered and duplicated and a
  `Notification` on either side of its `PermissionRequest` must not move
  the blocking claim. `permission_open` reads first, so holding both
  answers `question`. Both clear on `UserPromptSubmit`, `PreToolUse`, and
  `Stop` - the events that mean a human showed up. LED: `question` ->
  inner `waiting-permission` (new hue, `--led-color-permission`, resolves
  to the existing `--color-status-pending`), `notice` -> inner
  `waiting-input` (shared with the startup gate). Summary priority is now
  permission > input > working > unread > done > dead > unknown. Toast
  copy: "needs your permission" vs "wants your attention". Commit
  `cc885d6`.
- [x] **A superseded hook token is recovered once, never re-minted.**
  Traced to the millisecond on live 2026-09-08: a derived-id adopt minted
  a new token for `cloude_Agent_-_Cloude_Code` at 16:16:40.633984Z while
  the pane's own process still held the old one baked into its env at
  spawn time, and the first `hook_post_rejected_invalid_token` landed
  130ms later. 4,325 rejections followed over 4h24m, ending only when the
  owner restarted the pane by hand at 20:40:23Z. `hook_token_recovery.py`
  keeps a bounded in-memory ring of tokens this process minted and then
  superseded; a rejected hook is checked against that ring and, on a
  match for that id and that pane, the store is re-bound to the value the
  running process actually holds - logged once as
  `hook_token_rebound_from_superseded`, and nothing is minted. The
  negative control is the point of the test: a recovery that accepted
  broadly would be a credential bypass wearing a passing test. The
  respawn path and boot re-adopt now also push the current session env
  onto the pane before the next process starts, since tmux only copies
  session env at spawn time and a live process can never receive a
  post-hoc push. Live state needed no repair - the owner's 16:40 restart
  had already left the process, the pane env and the store agreeing,
  confirmed by 26 hook 200s in 45 seconds with no new 403s afterward.
  `ses_68c185ce` (2,757 rejected POSTs, carried forward from the late
  round above as needing a self-heal) last fired 2026-08-28 and its pane
  no longer exists - nothing to recover there, it is simply dead. Commit
  `6934965`.

**Test baseline, re-measured directly this round, not copied from a
commit message:** full `venv/bin/python3 -m pytest -q` from repo root,
**5274 passed / 3 failed / 12 skipped**, the three failures the same
pre-existing environmental ones CLAUDE.md already names
(`test_home_write_guard`, `test_state_dir_resolution`,
`test_version_probe`). This matches what `6934965`'s own commit message
claims, so that number is confirmed rather than merely quoted. **One test
is flaky under a full run, not from this round's code:**
`test_respawn_refreshes_pane_env.py::test_the_session_environment_itself_is_updated`
failed once in a full-suite run and passed 3-for-3 seconds later in
isolation on the same tree - it drives the real `cloude` tmux socket,
same class as INFRA-49. A lone failure there without a matching code
change is not a new regression. **Node: `git ls-files` counts 185
tracked `*.node.mjs` files, not 190 as `cc885d6`'s own commit message
claimed** - running all 185 directly gives 184 pass / 1 fail, the
pre-existing `test_archive_full_page_mode.node.mjs`. The "190" figure was
wrong when it was written; CLAUDE.md is corrected to 185 rather than
carrying the inflated count forward. Two untracked files sat alongside
this work throughout - `tests/led_state_for.node.mjs` and
`tests/test_led_real_hooks.py`, a real-hook LED integration harness
another agent was building in parallel (see the next bullet). Neither is
committed as of `6934965`, so neither is counted in the 185/1 line above.

- [ ] **The real-hook LED integration test is IN PROGRESS, not built,
  as of this section.** `tests/test_led_real_hooks.py`,
  `tests/real_hook_app.py`, `tests/real_hook_harness.py` and
  `tests/led_state_for.node.mjs` exist on disk (confirmed present) but are
  UNTRACKED - `git status` shows no commit behind them. Running the
  python file directly gives 8 skipped (opt-in via
  `CLOUDE_REAL_HOOK_TESTS=1`, correctly gated off by default), so the
  gating logic works; whether the tests pass WITH the env var set against
  a real claude binary has not been checked here, out of scope for a
  docs-only pass. `led_state_for.node.mjs` is not itself a test - it is a
  piped-stdin CLI helper the python harness shells out to
  (`echo '{"activity_status":...}' | node tests/led_state_for.node.mjs`)
  so that the python test can assert against the SHIPPED
  `client/js/status-led.js` mapping instead of re-implementing it; running
  it standalone with no stdin fails on a JSON parse error, which is
  expected, not a defect. Next session: find out who owns this work
  before committing it yourself, then verify token 4 of HANDOFF.md's
  first-session list.

- [ ] **Item 4 (alert lights / activity-after-resume) - BUILT, verify
  then close, not yet verified this round.** The narrower defect on
  record (`activity_state` reads `working` for about four minutes after a
  resume, then self-corrects) is now addressed two ways already shipped
  in this branch's history: the 120-second expiry on hook-fed `working`
  (`CLAUDE.md`, "The status lights, and what they are allowed to claim")
  and the fix stopping a raw tmux `running` pane from mapping to
  `working` on no evidence (it maps to `unknown` instead, landed in
  `0fc23a5`/`4215ad0` from the late round). Nobody has re-run the
  original four-minute measurement against current code to confirm the
  combination actually closes the gap rather than narrowing it further.
  Next session: reproduce a resume, time how long `activity_state` claims
  `working` past it, then close this item or write down what is still
  wrong.
- [ ] **Item 11 (read/unread state per session) - BUILT, verify then
  close, not yet verified this round.** CLAUDE.md's "Unread is keyed on
  the INSTANCE" section and the LED's outer-ring `unread` state (marked
  DONE earlier in this file at the `0fc23a5`/`4215ad0` entry) describe a
  finished mechanism: unread is keyed on `<tmux_name>@<#{session_created}>`
  so a reused name cannot inherit a dead session's flag, set on `Stop`,
  cleared when a WS terminal binds, with a legacy-name fallback when the
  epoch cannot be measured. What is NOT confirmed this round is the
  owner-facing half from the original ask (entering a session marks it
  read; a control lets the owner mark it unread again from the sidebar).
  Next session: click through that flow by hand against a real session
  before marking this closed.

**Backups to delete, filenames confirmed from this repo's own written
record (`.claude/notes/troubleshooting.md`) - do NOT delete from here,
this is a list for whoever next has mini access and database
permission:**
- `cloude.db.bak-uuidrepair-20260908T141621Z`
- `cloude.db.bak-agenttype-20260908T144337Z` (4,966,510,592 bytes,
  confirmed `ok` integrity, 40/40 tables at time of writing)
- `cloude.db.bak-uuidfill-20260908T144757Z` (4,966,510,592 bytes,
  confirmed `ok` integrity, 41/41 tables at time of writing)
- `cloude.db.bak-v23-20260908T161615Z` (written by the v24 migration)

Troubleshooting.md separately records "six 4.6GB `cloude.db.bak-*`
copies (~24GB)" in the state dir as of that pass, so **two more exist
beyond the four named above.** The owner's next-session intent names them
as "the sessionkind and projectbind ones" (from the `sessions.kind`
schema-v25 migration, `2b0ed7d`, and the project-attribution invariant
fixes, `7bd55fd`/`e73c1a7`) - but **no filename for either one appears
anywhere in this repo's tracked docs**, so do not invent one. List the
actual state dir on the mini before deleting anything; go by what is on
disk, not by this guess at what its name should be. Per the standing
owner note at "2026-09-08 owner note: clean up database backups when the
row repairs are done" earlier in this file: keep exactly one verified
full backup until a restart has proven the repaired rows work, then
delete the rest.
