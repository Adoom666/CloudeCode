# Release notes

## v1.0.9

Measured against `v1.0.8`. Two changes, both found or motivated by
testing v1.0.8 end to end against a real LM Studio.

### The cldl base URL must not carry /v1

The Anthropic SDK appends `/v1/messages` to `ANTHROPIC_BASE_URL` itself,
so the v1.0.8 example wrapper's `http://host/v1` produced
`/v1/v1/messages`. LM Studio answers that 200 with a 65-byte
`{"error":"Unexpected endpoint or method"}` - JSON that is not a Message.

Worth knowing because of how it PRESENTS. Claude Code reports it as
"API returned an empty or malformed response (HTTP 200) - check for a
proxy or gateway intercepting the request ... 0 stream events received",
which reads like a transport fault. Every layer underneath was verified
working first: the model list, a non-streaming Messages call returning
real content, a streaming call emitting correct Anthropic SSE events, and
the same again with tools and a system prompt.

**If you imported the `cldl` example from v1.0.8, re-import it** - the
stored copy in your config.json is not rewritten by an upgrade.

### Session id on the home screen

Each session box now shows its durable `sessions.id` bottom-right, and
`#7 <- #3` when it is a fork. That id is what `parent_session_id` points
at, so it is the number that lets a fork tree be followed by eye.

An EXTERNAL tmux session the app never created has no row and shows
nothing there, rather than an invented `#?`.

## v1.0.8

Measured against `v1.0.7`. Six branches: one listing fix, one launch-path
fix, and three features - a reserved-family picker, a Claude-executable
upgrade runbook, GUI fork, and LM Studio as a local model provider.

Test suite at the release sha: **2964 passed, 0 failed**, plus 114
standalone `tests/*.node.mjs` suites and 132 files through the JS syntax
check, all passing. Baseline at `v1.0.7` was 2872.

**Schema is UNCHANGED at v9.** This release adds no migration, so first
start does not migrate the database. `requirements.txt` is unchanged from
v1.0.7, so the launchd wrapper will not rebuild the venv. Your
`config.json` and `.env` are not touched by the installer.

### Fork a session from the interface

Every owned running session row carries a `fork` control. It spawns a NEW
tmux session that resumes that Claude conversation and branches it
(`--resume <uuid> --fork-session`), labelled with `(fork)` appended.

**The parent is not touched.** Not archived, not stopped, not marked. It
stays running, listed, resumable and forkable again. There is deliberately
no "was forked from" state, because in neither fork shape does the parent
die - recording one would be a verdict about a session that is alive. The
relationship is a reverse lookup on `parent_session_id`, so it cannot go
stale. A test compares the parent row byte for byte across a fork.

A session with no recorded Claude conversation is **refused** with a 409
rather than forked. Forking it would start a brand new conversation
wearing a fork label, and you would believe you had branched your work.

The label appends forever - `name(fork)(fork)`. Renaming is your job.

### Local models via LM Studio

A new `local` agent family runs a session against an LM Studio server
instead of the hosted API. Set the address in `config.json`:

```json
"providers": { "local_host": "127.0.0.1:1234" }
```

The launch picker gains a **local (lm studio)** row that fetches the live
model list. There is no default address, deliberately: a guessed one would
make "unreachable" the normal state for everyone who does not run LM
Studio, and would point the app at some other network's machine.

The model list reports **three** states, not two - `reachable`,
`unreachable`, and `not-configured`. The last two get conflated constantly
and mean opposite things: one says go check the machine, the other says go
set the address.

A local launch with no model is **refused**, not downgraded to some
default. `cldl` addresses one specific model, so a bare launch would open
a pane that errors or quietly runs something you did not choose.

The server address is injected as `CLDL_HOST` into the tmux environment
rather than interpolated into the command, so it never enters shell text.
There is no endpoint that sets it - it is an outbound fetch target, so a
setter would be an SSRF surface reachable with one authenticated POST.

### The launch picker offers every reserved family

`codex`, `hermes` and `openclaw` were launchable over the API and
unreachable from the interface unless you first authored a wrapper for
them. A pickable family with no wrappers now gets one pinned row, in
registry order, which puts codex directly under claude. `shell` stays out:
it already has the "new console" button, and two controls for one action
is worse than none.

### A fork is no longer born invisible

`list_sessions` hid any row carrying a `parent_session_id`, from a time
when that meant "a past Claude conversation". A GUI fork is a real tmux
session that also knows its origin, and knowing your origin is not a
reason to be hidden. Visibility now keys on the `tmux_created_epoch` that
already distinguishes the two - and it is a conjunction, not a swap,
because an imported stopped session has no epoch either.

### Every family that runs a user-installed CLI sources your rc

`codex`, `hermes` and `openclaw` passed their command to tmux raw. The
tmux pane shell is non-interactive and reads no rc, so a version-manager
install (nvm, asdf, mise) was not on PATH and the pane died "command not
found". The behaviour was inverted: the fallback for an EMPTY command was
already rc-sourced, so configuring nothing worked and shipping the default
did not. `shell` stays raw - `$SHELL -i` sources the rc itself.

### Upgrading with Claude

`/upgrade` ships in the repo and follows `docs/upgrade-with-claude.md`.
Two new scripts make an upgrade verifiable instead of assumed:
`scripts/upgrade-baseline.sh` records version, schema version and
per-table row counts BEFORE you start, and `scripts/upgrade-verify.sh`
compares afterwards. Verify exits 0 when everything passed, 1 when
something failed, and **2 when something could not be evaluated** - which
is deliberately not 0, because "I could not look" is not "nothing is
wrong".

## v1.0.4

Measured against `v1.0.3`. Nine branches: two schema changes, one behaviour
removal, three lifecycle/isolation fixes and three UI fixes.

Test suite at the release sha: **2706 passed, 1 skipped, 0 failed** (2707
collected), plus 102 standalone `tests/*.node.mjs` suites, all passing.
Baseline at `v1.0.3` was 2561 collected.

**READ THIS BEFORE UPGRADING.** This release changes the database schema
(v6 to v8) and it changes `src/`, so it is not a restart-free drop-in. Quit
from the tray first, install, then start it again. On the first start the
app backs up `cloude.db` and then migrates it. `requirements.txt` is
unchanged from v1.0.3, so the launchd wrapper will not rebuild the venv.
Your `config.json` and `.env` are not touched by the installer.

### Schema v6 to v8

Two additive steps run on first start, inside one transaction, after the
app has taken its own backup of `cloude.db`.

- **v6 to v7** creates `ix_sessions_claude_uuid`. No column is added -
  `claude_session_uuid`, `parent_session_id` and `fork_kind` have been in
  the sessions DDL since v2 and were never written. The index is what makes
  the new lineage lookup cheap on every Claude SessionStart.
- **v7 to v8** creates `session_groups` and `session_group_members`. Nothing
  is backfilled and that is the correct empty state: before this version no
  group existed, so every session is ungrouped, and ungrouped is the ABSENCE
  of a membership row rather than a row pointing at a default group.

Both steps carry their own `IF NOT EXISTS`, so a retry after an interrupted
run is safe. A v6 or v7 reader still opens the same file: an index changes
no query result and the new tables are simply never queried.

### Session lineage

Sessions now record which Claude session they are (`claude_session_uuid`),
what they were forked from (`parent_session_id`) and how (`fork_kind`),
driven by SessionStart and SessionEnd hooks. The hooks fail open: a hook
that cannot write does not take a session down with it.

### Sidebar groups

You can create named groups in the session sidebar and drag sessions into
them. Drag already existed for reordering; it is extended, not replaced.

### Restart a dead session in place

A dead session row now offers a Restart control next to Delete instead of
only offering you the chance to throw the row away. The respawn writes
nothing to the database - it reuses the row it already has. This overrides
a prohibition previously documented in `client/js/session-sidebar-rows.js`,
which is marked superseded rather than deleted.

### Server lifecycle

- Adoption of an already-running server is gated on its version, read from
  `/api/v1/health`, which now reports the running version. A server whose
  version cannot be established is not adopted.
- Quit is deterministic instead of best-effort.
- Restart on an adopted server refuses and offers, rather than silently
  doing something to a process the app did not start.
- The supervisor's restart backoff is bounded.

### Test runs can no longer write to your real home directory

`ensure_hook_settings()` no longer has an implicit default path - the
argument is required, which is the enforcement. A new
`src/core/test_write_guard.py` plus a conftest canary make a test that
reaches outside its temp root fail rather than quietly editing
`~/.claude/settings.json`.

### Upload sweeper no longer follows symlinks out of its bucket

A real production bug: the sweeper's delete could follow a symlink out of
`.cloude_uploads` and remove what it pointed at. The delete is now gated by
a pure `sweep_verdict()` query, and the second delete site in
`destroy_session` is gated the same way.

The sweeper's project list is now read from `cloude.db` rather than
`config.json`, keeping its three-outcome contract: a list (possibly empty)
when the datastore was read, and "undetermined" when it could not be.
Undetermined means the sweeper deletes NOTHING, rather than falling back to
sweeping paths it could not verify.

### Toasts stack instead of pile up

Client-side coalescing, a cap, and priority ordering, with an overflow row
when the cap is hit. `client/js/toast.js` and `client/css/toast.css` only.

### Tray glyph paints at full strength

The menu bar glyph was drawn at 71 of 255 ink and read as washed out
against a dark menu bar. It is now 255. The icon generator and all
generated assets are regenerated, and `dot_signature` is derived from
colour rather than from alpha.


### Projects live in cloude.db and nowhere else

`config.json` no longer carries a `projects` key. The database has been
authoritative for projects since `feat/db-is-authoritative`; the config copy
was kept as a mirror so an older build could still read it. That mirror is
now gone, and with it the divergence reporter that compared the two.

**What this fixes.** The launchpad could render two provenance banners at
once that contradicted each other - one saying "Showing config.json's
projects", the other saying "cloude.db is authoritative and is what you are
seeing". They came from two independently-formed opinions in the same
render method and nothing made them agree. Measured on the reported case:
the four projects on screen all carried `id: null`, which is the config
shape, so the second banner was the false one.

Underneath, the comparison read the database LIVE and `config.json` from
`Settings._auth_config_cache`, which is invalidated only when the app itself
writes the file. A hand edit therefore left the server comparing a fresh
measurement against a stale one. Reproduced: with both stores emptied on
disk, the running server still reported "4 only in config.json". A
divergence report built on a cache it cannot invalidate is not a
measurement, and the fix is to stop having two things to compare.

**WHAT THIS COSTS YOU, stated plainly, because it is a real loss.** The
config copy existed so that a user could fall back to an older build by
deleting `cloude.db` and coming back up on the file. That path is gone. An
older build reading `config.json` after this upgrade finds NO projects at
all, and will start with an empty launchpad. Your projects are not lost -
they are in `cloude.db`, which the older build cannot read - but you will
have to re-add them by hand if you downgrade and stay downgraded. This is a
deliberate trade: one source of truth, no contradiction possible, in
exchange for the file-level rollback. It sits alongside the existing
downgrade exposure recorded under v1.0.1 and v0.8.2, and it is the more
predictable of the two, because it fails the same way every time rather
than depending on what the old build happens to overwrite.

**The upgrade cannot lose a project.** On the first start after upgrading,
`projects_config_migration` reads the `projects` key, imports every entry
the table has never seen, RE-READS the table to prove every config root is
now either a row or a deliberate deletion, and only then removes the key. A
pass that cannot prove that leaves `config.json` byte for byte as it found
it and retries on the next start. A project you DELETED is not resurrected -
tombstones are the evidence for that. A project whose absence cannot be
explained at all IS imported and reported separately as
`imported_undetermined`, which reverses the old reconcile's rule on purpose:
while the file still held the entry, leaving it alone cost nothing; once the
key is being removed, leaving it alone means deleting it permanently.

A forwarding note is written where the array was, so anyone who remembers
the key learns that re-adding it by hand does nothing.

**Also changed.** `resolve_projects()` no longer accepts a config project
list - the signature is the enforcement. The `config_fallback` mode is
replaced by `db_unreadable`, which serves an EMPTY list and says in words
that empty means "could not read", not "you have none"; writes are still
refused. `GET /projects/authority` no longer returns `diff`, `diff_state` or
`config_path`. `src/core/project_diff.py` and `src/core/project_snapshot.py`
are deleted, along with the five config-writing project methods on
`Settings` that were already dead in production.

### Known and unchanged

A hand edit to `config.json` still does not take effect until the server
restarts, for every key the file still holds. That is unchanged and it is
disclosed where it matters - the tray's **Edit Config** item says so before
opening the file. What changed is that the stale cache can no longer feed a
REPORT: it used to produce a confident, specific, wrong claim about your
projects, and now at worst an edit does not apply yet. Making the cache
mtime-aware was considered and not done here; it touches every config
consumer and is a separate change with its own risks, and it is no longer
load-bearing for correctness.

## v1.0.3

Measured against `v1.0.2`. Three fix branches plus the open PR that finally
brought the v1.0.x line onto `ship/round5`.

Test suite at the release sha: **2561 passed, 0 failed** (2561 collected),
run with `--ignore=tests/test_nuke_sandbox.py`, which executes the real
`nuke.sh`. That reconciles exactly against the branch baselines, measured
rather than reported: v1.0.2 collects 2529, and the three branches collect
2537, 2542 and 2540, so +8 +13 +11 = 2561. Node suite: 94 files, all green,
run per file (1665 assertions). JS syntax check: 97 files parsed cleanly
(95 at v1.0.2, plus the two new pure modules `macOS/setup-verdict.js` and
`macOS/published-url.js`).

### What shipped in this release

**The tray stops showing an address the server is not listening on.** The
bind row renders the MEASURED bind, never the configured aspiration, and
`src/config.py` no longer reports `effective_bind_host` as whatever was
configured - it reports what was actually bound, via the new
`record_bound_host` / `bind_report` pair.

**One answer to "is setup finished", and the server owns it.** The tray had
its own private notion of configured that required three `CLOUDFLARE_*`
environment variables belonging to a tunnel feature removed in plan v3.2.
Nothing writes them, so the condition was unsatisfiable by construction and
the menu offered "Run Setup Script" to a user who was already set up. The
verdict now comes from the server through the new `macOS/setup-verdict.js`,
with a local evaluation used only when there is no server to ask, reading
the same facts. A stopped server is not an unconfigured one and no longer
renders as one. The same dead gate is gone from `setup.sh` and from
`macOS/server-manager.js`.

**Tray status glyphs say more.** Undimmed; `stopped` is a grey filled dot,
`starting` an amber hollow ring, and `attention` moved from red to amber.

**Running setup again no longer destroys a paired TOTP secret.**
`setup_auth.py` preserves an existing secret by default. Rotation is now an
explicit, typed-confirmation act: `--rotate-totp` and `--rotate-jwt`. The
`.totp_paired` sentinel is cleared only when the secret actually changed.

**A fresh install opens onto a usable screen.** The create control moved out
of `#running-sessions-section` - whose `display:none` made a global control
disappear whenever there were no running sessions - into an always-present
`.launchpad-actions` row. `config.example.json` ships `projects: []` instead
of four invented demo projects pointing at paths that existed on nobody's
machine. Clicking a project whose folder is gone now explains itself by name
and path instead of doing nothing. `getPublishedUrl()` no longer falls back
to a LAN address when the bind is unmeasured, which during the setup lockdown
sent the browser to a port nothing was listening on. New
`src/core/tmux_discovery.py` stops tmux discovery depending on a single PATH
prepend in the launcher.

### Known and unchanged

`scripts/verify_home_mechanics.py` ITEM 48 fails, as it did at v1.0.2.
`scripts/verify_selection_apps.py`, `verify_selection_regressions.py` and
`verify_selection_scrolled.py` cannot be evaluated without a live-server
harness artifact; they behave identically at v1.0.2, so this is a
CANNOT DETERMINE, not a pass and not a regression.
`setup.sh` still PROMPTS for Cloudflare values further down the script. Only
the completeness GATE was removed. The prompts are dead code and were not in
scope here.

## v1.0.1

**This is the v1 release. `v1.0.0` was tagged and immediately superseded** -
its CI skip audit was red, and the tag is left in place rather than moved so
the record stays honest. Do not install `v1.0.0`.

What was wrong with it: removing the server reset endpoint emptied the
`ACCEPTED_UNDELIVERED` register in `tests/test_runtime_script_delivery.py`,
and a `parametrize` over an empty collection collects one placeholder case
that pytest SKIPS on every platform. The CI skip audit exists to refuse
exactly that - a test that cannot go red - and it did its job. The test now
iterates the register inside its body, so it RUNS against an empty register
and still fails the moment a thin exemption reason is added.

Measured against `v0.8.1`. This is the first 1.0 and it is a version-string
decision, not a claim that everything below is finished - the known issues
are real and listed.

Test suite at the release sha: **2528 passed, 0 skipped** (2528 collected),
run with `--ignore=tests/test_nuke_sandbox.py`, which executes the real
`nuke.sh`. Node suite: 91 files, all green, run per file. JS syntax check:
95 files parsed cleanly.

### What shipped in this release

**Workspace settings, in the app.** A settings screen with a development
root, a default shell, a default editor and an environment map, plus bind
and TLS preferences. Two additive blocks in `config.json` - `workspace` and
`server_prefs`. No config version bump, so an older build reads the file and
ignores what it does not know. The environment map actually reaches a newly
spawned terminal; that is measured, not assumed. The menu bar opens the
settings screen, and the Edit Config item no longer claims to do something
it did not.

**Downgrade got safer, in one specific way.** The state-file location is now
resolved once per filename and pinned, and `rollback.sh` restores each file
to where it found it rather than to wherever the current version would put
it. That closes the file-LOCATION half of the metadata-loss defect. It does
not close the other half - see the known issues.

**The in-app "reset server" control was withdrawn.** It called
`POST /api/v1/server/reset`, which ran a `reset.sh` that has never been in
the app bundle, so it returned a 500 on every packaged install. Shipping the
script would have been worse than the 500: in a packaged install its
fallback branch would have killed the python child the Electron app owns and
left a detached uvicorn holding the port after the app quit - a quiet wrong
state in place of a loud correct error. The endpoint and its UI control are
both gone. Restart the app from the tray menu, or `launchctl kickstart -k`
under launchd. Nothing that worked was removed: the mini never had the
script.

### Resolved earlier in this line, and you may still be exposed

These were fixed, but a machine that ran the broken version still carries
the damage. Read them even though they say RESOLVED.

**The uninstaller left your data on disk while reporting a full reset.**
"Nuke it from Orbit" reported a complete wipe and left `cloude.db`, the
migration trail and your stored refresh tokens in place. If you ever ran the
old uninstaller, **those files are still there** - this release removes them
when you run the current one, but it cannot retroactively undo the old
run's false report. Check the state directory yourself if that matters to
you.

**Rollback on Linux restored no state files while printing a success
count.** The count came from what it intended to restore, not from what it
wrote. A Linux rollback that told you it restored N files may have restored
none of them. If you rolled back on Linux and your app came back not
recognising its own sessions, this is why.

### Known issues

Each of these is real and shipping anyway. Read the consequence, not the
title.

**A config with object-form slash commands will not load on v0.8.1.** If any
slash command in your `config.json` carries a description, the older server
exits at startup rather than starting with a degraded config. New writes
normalize to bare strings when there is no description, which shrinks the
exposure without eliminating it. Check your slash commands before you
downgrade.

**A second downgrade defect is UNMEASURED.** Separate from the file-location
fix above, `v0.8.1` was observed to prune a live session from the owned set
and delete its metadata as stale. Consequence: after a downgrade the tmux
sessions keep running and the app presents them as strangers you must
re-adopt. The location fix does not address this and nobody has measured how
far it reaches. Recorded in `docs/upgrade-downgrade-roundtrip.md`.

**The login screen applies no theme variables.** The theme manifest is
fetched after authentication, so the login screen renders in default colours
no matter which theme you picked. Cosmetic, long-standing, unchanged here.

**Logging out with an active session may stack two confirmation dialogs.**
Found by reading the code path, not measured against the running app. If you
see two dialogs, that is this.

**The TLS preference is recorded but not enforced.** The settings screen
lets you express a TLS preference and stores it. There is no TLS terminator
in this build, so nothing acts on it. Setting it does not make anything
encrypted.

**`verify_home_mechanics` fails on ITEM 48.** The help panel does not move -
it stays the first child of its container. Pre-existing, confirmed identical
at the base commit, and this release took that harness from 6 failures to 3.
Not introduced here and not fixed here.

**Three verification harnesses could not be evaluated for this release.**
`verify_selection_apps`, `verify_selection_regressions` and
`verify_selection_scrolled` each need a live authenticated server and a TOTP
secret file that was not present. They are CANNOT DETERMINE, not passes.
`verify_lifecycle_reconcile` is a parameterised tool requiring `--db` and
`--listing` and was likewise not exercised.

**The type-NUKE confirmation window has been verified as rendered markup,
not as a live Electron window.** Its HTML was rendered at the window's real
dimensions: the confirm button is disabled on an empty field, still disabled
on the wrong word in the wrong case, and enables only on exactly `NUKE`.
Focus lands on the text field, not on the destructive control. The Electron
window that hosts it, and the wiring from its verdict to `nuke.sh`, are
covered by source-level tests only. Nobody has watched the real window open.


## v0.8.2

Measured against `v0.8.1`. 390 commits. Test suite at the release candidate:
2334 passed, 0 skipped. JS syntax check: 94 files parsed cleanly.

### The headline

**The uninstaller used to lie.** "Nuke it from Orbit" reported a complete
reset while leaving the session database and your stored refresh tokens on
disk. That is fixed. If you ever ran the old uninstaller, those files are
still there: `cloude.db`, the migration trail and the state directory's
token store. This release removes them; the old one did not, whatever it
printed.

This reaches upgraded installs, not just fresh ones. The release candidate
shipped the fixed script without shipping it to the place the app runs it
from; that delivery gap is closed here.

**The uninstaller fix now actually reaches an upgraded install.** This was
listed as a known issue in the release candidate and is fixed. `nuke.sh` was
excluded from the per-launch asset resync so that a customized copy would
survive, and the tray menu executes the copy in the derived server directory
- so every upgrade paired the NEW confirmation window with the OLD
destructive script, and looked fixed while behaving exactly as before.
`nuke.sh` is now resynced from the bundle on every launch like every other
build artifact. A test derives the set of scripts the app executes by path
straight from the source and fails if any of them is not covered by the
resync allowlist, so a script added later is caught without anyone
remembering to update a list.

**The uninstaller now makes you type the word NUKE.** The tray menu used to
show a message box with a "NUKE IT" button, one mis-aimed click away from an
irreversible wipe, on a menu that also holds ordinary items. It now opens a
confirmation window where the confirm control stays disabled until the field
contains exactly `NUKE`. A window closed any other way is a no, never a yes.
The dialog also names what it destroys, including the refresh tokens, which
the old one never mentioned. It no longer claims to delete a Cloudflare
tunnel and DNS records, which it stopped doing several versions ago.

### Also in this release

- **App state moved to `~/Library/Application Support/CloudeCode`**, with
  `CLOUDE_STATE_DIR` to override it.
- **A real datastore.** `cloude.db` plus a migration trail. Sessions,
  projects and adoption now persist instead of being re-guessed at launch.
  `cloude.db` is authoritative; `config.json` is the rollback artifact.
- **Home screen** is a two-level project-to-session tree with a RECENT
  group, session pinning, a user-defined order and a density control.
- **Setup wizard**, plus a bind lockdown so the server refuses to listen
  broadly while it is unconfigured.
- **Upgrade and rollback scripts** for from-source installs, tag-based, and
  rollback moves DATA with CODE or refuses and says why.
- **Themes** gained canvas background effects across the whole set, and
  session rows paint in their own session's theme.
- **Terminal**: translucent background over the theme effect, altscreen and
  transcript scrolling routed through one primitive, refit on resize.
- **File browser** is a dockable right-side drawer; the file editor is a
  full-screen modal.
- **Tray icon** carries a status light for server, sessions and updates, and
  the secure/insecure binding indicator now reports what is actually true
  rather than an address nothing is listening on.
- **CI now parses `macOS/`.** The Electron main process, the code that
  spawns the uninstaller, was the one JavaScript tree in this repo that no
  syntax check had ever looked at.

### Known issues

Each of these is real, reproduced, and shipping anyway. Read the
consequence, not just the title.

**Session metadata does not survive a downgrade. Confirmed unsafe.**
`detach_session` relocates the metadata file into the state directory, and
`rollback.sh` restores state files to the new location regardless of where
it found them. If you roll back to an earlier version, you lose the record
of which tmux sessions this app owns. The sessions keep running. The app
does not recognise them and presents them as strangers you have to re-adopt.
Not fixed in this release.

**A config with object-form slash commands will not load on v0.8.1.** If any
slash command in your `config.json` carries a description, the older server
exits at startup rather than starting with a degraded config. New writes
normalize to bare strings when there is no description, which shrinks the
exposure but does not eliminate it. If you plan to downgrade, check your
slash commands first.

**The login screen applies no theme variables.** The theme manifest is
fetched after authentication, so `document.documentElement.style.length` is
0 on the login screen and it renders in default colors no matter which theme
you have chosen. Cosmetic, long-standing, unchanged here.

**Logging out with an active session may stack two confirmation dialogs.**
Reported by inspection of the code path, not measured against the running
app. If you see two dialogs, that is this.

**RESOLVED: the in-app "reset server" control was removed.** It called
`POST /api/v1/server/reset`, which ran `reset.sh` from the server's own
directory, and `reset.sh` has never been in the app bundle - so the control
returned a 500 naming the missing file on every packaged install. Shipping
the script would not have fixed it. Restarting a process belongs to whatever
supervises that process, and this server never supervises itself: packaged,
`macOS/server-manager.js` owns the python child and already has `restart()`;
under launchd, `launchctl kickstart -k` is launchd's own job; from source,
`./reset.sh` is still in the tree. `reset.sh`'s fallback branch is
`stop.sh` + `start.sh`, which in a packaged install would kill the child the
app owns (the tray would report a crash that did not happen) and leave a
detached uvicorn holding the port after the app quits - a quiet wrong state
in place of a loud correct error. **Restart the app from the tray menu.**

**The type-NUKE confirmation window has been verified as rendered markup,
not as a live Electron window.** Its HTML was rendered in a browser at the
window's real dimensions and checked: the confirm button is disabled on an
empty field, still disabled on the wrong word in the wrong case, and enables
only on exactly `NUKE`. Focus lands on the text field, not on the
destructive control. The Electron window that hosts it, and the wiring from
its verdict to `nuke.sh`, are covered by source-level tests only. Nobody has
watched the real window open. Open it once and press Cancel if you want that
last bit of certainty.
