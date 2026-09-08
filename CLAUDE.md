# Cloude Code

Drive a Mac's live Claude Code sessions from a phone. A FastAPI server owns real
tmux sessions on the Mac, streams the PTY over a WebSocket, and paints it into
xterm.js in a browser. The session runs whether anyone is watching or not, which
is the whole point and the source of most of the interesting design.

Read this before writing code here. It is orientation first, conventions second.

## Stack

| Layer | What | Where |
|---|---|---|
| Backend | Python 3.12, FastAPI, uvicorn, pydantic models | `src/` |
| Logging | structlog, event-name first arg, kwargs for context | everywhere |
| Terminal | tmux (live backend) driving a PTY, xterm.js client-side | `src/core/tmux_backend.py`, `client/js/terminal.js` |
| Frontend | vanilla JS, no framework, NO build step for `client/` | `client/` |
| Desktop shell | Electron wrapper (has its own `package.json`) | `macOS/` |
| State | JSON on disk (`config.json`), plus one SQLite file for refresh tokens | `src/config.py`, `src/core/refresh_store.py` |

`client/` is served straight off disk under `/static`. There is no bundler, no
transpile, no `client/package.json`. A file you add there is live on reload, so
it must be valid in the browser as written. Run `node --check` on every JS file
you touch.

## Architecture, the parts that shape everything else

**tmux is the live backend. `PTYBackend` is legacy.** The `SessionBackend` ABC is
at `src/core/session_backend.py:32` and has two implementations, each in its own
file: `TmuxBackend` (`src/core/tmux_backend.py:163`) is the one that runs, and
`PTYBackend` (`src/utils/pty_session.py:293`) is the legacy path. Only the ABC
lives in `session_backend.py`; do not go looking for the subclasses there. Write
against tmux; do not build new behavior on the PTY path.

**Sessions live on a dedicated tmux socket, `tmux -L cloude`.** That socket is
separate from the user's own tmux server, which is why our sessions survive a
server restart and why we can never accidentally kill a user session. Sessions we
create are named `cloude_*`. Constants: `DEFAULT_SOCKET_NAME` (`src/core/tmux_backend.py:84`) and
`SESSION_PREFIX` (`:87`). Address sessions by socket + name, never by prefix
guessing.

**Created vs adopted is a real distinction, not a detail.** Cloude Code can
attach to a tmux session it did not create. A TRULY external one - no row for
its instance triple - gets an id of `adopted:<tmux-name>` and is absent from
`owned_tmux_sessions`. Anything that parses, matches, displays or routes on a
session id has to handle both shapes. Strip the prefix to recover the tmux name;
do not assume the id is a clean display string.

**AN ADOPTION RESOLVES THE ID, IT DOES NOT MINT ONE, and the difference is the
hook path.** `adopt_external_session` opened with the literal
`adopted_id = f"adopted:{name}"` until 2026-09-08. That is wrong for a session
the app already has a row for. `get_env_for_spawn` puts `CLOUDECODE_SESSION_ID`
into the pane at `new-session` time, so the agent inside presents its
create-time id on every hook POST for life, with a token bound to THAT id;
registering the same live pane under an invented id makes `validate_hook_token`
answer False and `routes.py` return **403 - not 410**, so grepping for the
stale-session code finds nothing and the hook path looks healthy. Measured:
94 refusals in four minutes for one session. `src/core/session_adopt_identity.py`
now resolves the id through the boot re-adopt's OWN ladder
(`session_boot_readopt_plan.resolve_session_id`, imported rather than rebuilt,
so the two paths cannot name one pane two things), keyed on the instance triple
via `session_store.get_instance`.

Note it is NOT "a row exists, so reuse its id": `persist_adoption` records the
sighting BEFORE the id is resolved, so a row exists for every adoption by then.
The derived rung is reached because a fresh `observed` row carries no
`legacy_session_id` and no hook-token mapping.

**ONE PANE IS ONE REGISTRATION, so the adopt teardown keys on the tmux NAME.**
While the id was always `adopted:<name>`, "the registration for this id" and
"the registration for this pane" were the same question. Resolving the id made
them different, and keying the teardown on the id alone leaves the other one
behind. Measured on live minutes after the re-key shipped: the rehydrated
`session_metadata.json` entry held `cloude_Agent_-_Cloude_Code` as
`adopted:cloude_Agent_-_Cloude_Code`, the browser's adopt correctly re-keyed to
`ses_fb8dd410`, and `GET /sessions/list` returned **22 rows for 21 live tmux
sessions** - one pane, two backends, two tailers on one FIFO.
`_registered_ids_for_tmux_name` is what enforces the rule. Note this was caught
only because the deploy was verified against `/sessions/list` rather than
against the `boot_readopt_complete` log line, which was perfect.

**AND A RECOVERED ID MUST NOT BE RE-MINTED A TOKEN.** `_mint_hook_token`
REPLACES the token it holds for an id. Called on a re-keyed id it revokes the
credential the running agent is holding and cannot be handed a replacement for,
so the 403 storm returns wearing the correct session id and every log line looks
right. A re-keyed adoption calls `_keep_hook_token` instead, which re-binds the
tmux name and leaves the secret alone; a derived id still mints exactly as
before.

**AND WHEN A MINT DOES LAND ON A RUNNING AGENT, IT IS NOW RECOVERABLE
ONCE.** The rule above is the prevention; this is the net under it,
because the failure is invisible from inside the pane and cost 4h24m of
dead hooks on 2026-09-08. Traced to the millisecond: a derived-id adopt
minted at 16:16:40.633984Z, the first `hook_post_rejected_invalid_token`
for that id landed 130 ms later at 16:16:40.763005Z, and 4,325 followed
until the owner restarted the pane by hand at 20:40:23Z. The same id was
being ACCEPTED minutes before (`toast_recorded` 16:11:28Z), so it was a
rotation, not a misconfiguration. `src/core/hook_token_recovery.py` keeps
a bounded IN-MEMORY ring of tokens this process minted and then replaced;
on a rejection, `SessionManager.recover_hook_token` accepts a value ONLY
if this server minted it for THAT id on THAT pane and superseded it, then
re-binds the store to what the running process holds, logs
`hook_token_rebound_from_superseded` once, and NEVER MINTS - minting is
the defect, and a recovery that minted would revoke the credential again
while every log line read correctly. `RECOVERY_NO_MATCH` (searched, not
found) is kept apart from `RECOVERY_UNAVAILABLE` (nothing to search, or
the pane binding is unknown); both refuse, but only the first says
anything about the token. THE NEGATIVE CONTROL IS THE LOAD-BEARING TEST:
a recovery that accepted broadly would pass the positive test perfectly
and be a credential bypass. In memory only is deliberate - a mint plus a
restart is not recoverable this way, and the restart already has its own
answer.

**AND A RESTART IS THE ONE MOMENT A LIVE PANE'S ENV CAN BE CORRECTED.**
tmux copies the SESSION environment into a pane's process at spawn, so
`CLOUDECODE_SESSION_ID` and `CLOUDECODE_HOOK_TOKEN` cannot be pushed into
a process already running - which is why a hand restart was what ended
the storm above. `TmuxBackend.respawn` takes `spawn_env` and issues
`set-environment` BEFORE `respawn-pane`, and the boot re-adopt does the
same for the next process in each pane. Ordering is the whole claim, so
`tests/test_respawn_refreshes_pane_env.py` proves it against REAL tmux by
having the respawned process write its own inherited value: a mock
asserting two calls happened in order would only be testing its own
arrangement.

**BOOT HOLDS EVERY SURVIVING SESSION, not just the last one.** It used to
rehydrate the ONE session in `session_metadata.json`; measured 2026-09-08, 21 live
sessions and zero held. `src/core/session_boot_readopt{,_plan}.py` now re-adopts
every instance whose row says `origin` is `created` or `adopted`, keyed on the
triple. The ID IS RECOVERED, NOT MINTED: the hook-token store's `tmux_names` map is
the only durable record of the `CLOUDECODE_SESSION_ID` injected into the pane, so
reversing it is what stops the hook route answering 410. A name with no row stays
adoptable, an unreadable table yields `cannot_determine` and holds nothing, and the
pass is SCHEDULED, never awaited - uvicorn binds at the lifespan `yield`, so an
awaited pass is dead port (measured: 1.2 ms to bind, versus 49 ms awaited and 945 ms
serial). It takes its own listing because `discover_existing` carries no epoch.

**EVERY SESSION BELONGS TO A PROJECT, AND THE ROW IS WHERE THAT LIVES.**
The owner's rule, verbatim: "all sessions belong to projects, the root folder
... its impossible to not have a project." Nothing in memory carries it - the
`Session` model (`src/models.py:138`) has no project field and `SessionInfo`
ships none, so `/sessions/list` cannot lose a project and cannot restore one.
The launchpad tree reads `project_id` / `project_attribution` off
`GET /sessions/records`, joined to the live session by tmux name plus epoch in
`_buildProjectSessionGroups` (`client/js/launchpad.js:3878`), and it tests
`attribution === 'none'` BEFORE it looks at `project_id`. So a row carrying
both an id and `none` renders under "no project" while holding a perfectly good
one, which is what the owner saw. The invariant is enforced in
`src/core/session_project_binding.py` and nowhere else: `resolve_project_binding`
is the ladder (as-written match, then the same path canonicalised, then a minted
project, then `none` for a scratch dir, then `unknown`), and `columns_to_write`
is the rule that **THE PAIR MOVES TOGETHER OR NEITHER MOVES**. It is called from
`persist_adoption` (`session_adopt_persist.py`, `allow_create=False` - re-entering
a session is not a moment to invent a project) and from `persist_creation`
(`session_create_persist.py`, `allow_create=True`), while `create_project`
(`project_writes.py`) adopts the project-less live rows already under its new
root, which is punchlist 16 closed from both directions.

Two things caused this and both are worth keeping. First, a HALF-WRITE:
`claim_instance` applies each column "only when not None", so a derived
`(None, 'none')` skipped the id and wrote the attribution ALONE - the row kept
its project and acquired a value contradicting it. "Only when not None" reads
as conservative and is not, when the columns are a pair. Second, a SPELLING the
lexical matcher cannot cross: `project_attribution.attribute` refuses to resolve
symlinks, correctly, so a session probed at `/Users/jsugamele/Development/...`
could never match a project declared at the iCloud path that same directory
resolves to. Canonicalising is a FALLBACK RUNG, tried only after the as-written
match fails, so a project declared at a symlink still collects the sessions
declared there. Measured 2026-09-08: rows 7 and 8 held projects 1 and 2 beside
`none`, row 45 held NULL from the create race, and 3 of 22 running sessions
rendered project-less.

**IT HELD ZERO ON ITS FIRST REAL BOOT, and the cause is worth keeping.** The
pass builds an OWNED backend (`build_backend`, not `TmuxBackend.for_external`)
and calls `attach_existing(needs_pipe_setup=True)`. That reached
`ensure_pipe_pane`, whose guard was `not self._running and not self._is_external`
- and `_running` is not set until the BOTTOM of `attach_existing`. So both halves
were true for every owned session: 20 failures, all `"backend not running"`, all
inside one millisecond, before a single tmux command was issued.

**A guard whose only exercised caller sets the flag it checks has never been
tested.** Until the boot re-adopt, the sole caller reaching that branch was the
external adopt path, where `for_external` sets `_is_external=True` and the guard
CANNOT fail. 4874 green tests had never observed it raise, because
`tests/test_boot_readopt.py` is hermetic and its `FakeBackend.attach_existing`
has no guard to fail. The fix threads intent explicitly -
`ensure_pipe_pane(attaching=True)`, passed only from inside `attach_existing`,
after `is_alive()` and the `#{pane_dead}` probe have both answered. It does not
set `_running` early: that would make the backend claim it was streamable across
four tmux round trips and leave the claim standing on an object whose setup
raised. `tests/test_boot_readopt_real_tmux.py` covers it against a REAL backend
on a real throwaway socket, because a double cannot reproduce a guard.

**Claude Code lifecycle hooks feed the status machine.** `src/core/claude_hooks.py`
merges a managed hook block into `~/.claude/settings.json` (marked
`# cloudecode-managed`, idempotent, atomic write, bails rather than clobbering an
unparseable file). Hooks POST to a loopback-only endpoint authenticated by an
env-injected shared token. Events: `Stop`, `Notification`, `PermissionRequest`
(these three also raise a toast), plus `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `SubagentStart`, `SubagentStop` (activity state only).

Hook events arrive **unordered, and may be duplicated or dropped.** Every
consumer in `src/core/session_activity.py` is therefore idempotent: last-write-
wins booleans and floored counters (`subagent_depth = max(0, depth - 1)`). If you
add a hook consumer, it has to survive the same event twice and a missing pair
half. Never increment without a floor, never assume a `Stop` follows the
`UserPromptSubmit` you saw.

**Security posture.** CSP is stamped on every response by the middleware in
`src/main.py`. As of 2026-08-16 there is **no third-party origin in any
directive**: the baseline is `default-src 'self'` with `frame-ancestors 'none'`,
and `https://cdn.jsdelivr.net` has been removed from `script-src`, `style-src`
and `font-src`. It used to sit in all three because xterm.js 5.3.0, its CSS and
its fit / webgl / unicode11 addons loaded straight from the CDN in
`client/index.html`. They are now vendored under `client/vendor/xterm/`.
`style-src` still carries `'unsafe-inline'` and must keep it: xterm addons set
inline style attributes on nodes they manage, and the terminal renders blank
without it. Inline **styles** are therefore permitted; inline **script** and
`eval` are not.

**Why the CDN removal was a correctness fix, not only hardening.** A remote
xterm asset is one content blocker away from breaking the terminal for
everybody. Brave Shields on a phone dropped enough of it that xterm ran but
`xterm.css` did not apply, so the character cell was measured wrong, `FitAddon`
derived a bogus cols/rows from it, and `sendResize` reflowed the real tmux pane
to a grid matching nothing on screen. The symptom was a terminal that rendered
garbage on the device while the desktop and the desktop mobile emulator both
looked perfect, which is exactly what you would least suspect a CDN of. Nothing
the terminal needs may be loaded off-origin again. `tests/test_no_remote_assets.py`
fails the build if a third-party URL reappears in `client/index.html`.

New third-party libraries get **vendored** under `client/vendor/<lib>/` and
served from `/static`, with a `VERSION.md` beside the files recording version,
upstream source URL and sha256, plus a refresh script under
`scripts/<lib>-vendor/`. CodeMirror 6 and xterm are the worked examples
(`scripts/xterm-vendor/fetch.sh` re-downloads the pinned versions and hard-fails
on a hash mismatch). Do not add a host to the CSP, do not weaken
`frame-ancestors 'none'`, do not introduce inline script or `eval`, and do not
read `style-src 'unsafe-inline'` as license to widen anything further.

**Config writes are atomic and backed up.** Copy the pattern in
`Settings.update_settings_config()` (`src/config.py`): write the `.bak` of the
pre-write bytes first, then temp file, `fsync`, `os.replace`. A half-written
`config.json` costs the user their whole setup, so there is no "just dump the
JSON" shortcut anywhere in this codebase.

## The `/sessions/list` shape

`GET /sessions/list` returns `SessionInfo` objects (`src/models.py`), and the
fields sit on **two different levels**:

- On the wrapper: `activity_status`, `unread`, `startup_gate`, `tmux_session`,
  `agent_type`, `agent_family`, `agent_family_source`, `agent_wrapper_label`,
  `pinned_theme`, `session_backend`, `recent_logs`, `local_servers`, `stats`
- On the nested `.session`: `id`, `pty_pid`, `working_dir`, and the rest of the
  `Session` model

Reading `info.id` or `info.session.unread` gives you `undefined` silently, and it
looks exactly like "the backend didn't send it". This is the single most
repeated bug in the project. Check the level before you debug the endpoint.

**Three fields describe the agent, and they answer three different questions.**
`agent_type` is the raw stored value: a wrapper id (`claude-chrome`) or a bare
family name (`shell`). `agent_family` plus `agent_family_source` are the
DISPLAY-time resolution of it (`src/core/agent_family_display.py`), and the
source is what keeps a fingerprinted guess from rendering like a launch fact.
`agent_wrapper_label` is the wrapper's configured label
(`src/core/agent_wrapper_display.py`), `null` whenever no configured wrapper can
be named - a fingerprinted value, a bare family name, or an id config no longer
carries. `AttachableSession` carries the same three, resolved by the same
functions, so a launchpad row merged from either endpoint agrees about itself.

**A GUESS MUST NEVER OUTRANK A RECORD, and the ordering lives in one place.** A
live session has two sources for `agent_type`: the in-memory `Session` and
`sessions.agent_type` on its row. `_session_info_for` used to read the row only
when the in-memory value was EMPTY, and that is wrong for an ADOPTED session -
every live session is re-adopted after a server restart, the adopt path
fingerprints the pane and stores the bare family token, so a session launched as
`claude-chrome` came back holding `claude` and painted `~claude` (the dashed
"guessed" pill) beside a row that recorded the exact wrapper. Measured on the
owner's box 2026-09-08: row 43, `agent_type='claude-chrome'`,
`agent_family_source='launched'`. `session_agent_evidence.choose_agent_evidence`
is now the only thing that picks between the two, as a four-rung ladder:
in-memory launch, then the row, then the in-memory fingerprint, then nothing.
The row beats the fingerprint because nothing writes an inference into
`sessions.agent_type` - `persist_fingerprint_family` writes `agent_family` and
deliberately leaves that column alone.

**`startup_gate` answers a DIFFERENT question from `activity_status`: has this
session started at all?** A freshly launched claude parked on its folder-trust
dialog is a live pane, running a real process, with a pid tmux reports happily -
and it has fired NO hook, so every field beside it reads healthy and the row
painted a green `Connected` dot over a session waiting for a keypress. That is
punchlist 19. `activity_status` describes what a RUNNING agent is doing; this
says whether it is running. Three values, `ready` / `awaiting_startup_prompt` /
`unknown`, resolved by `src/core/session_startup_gate.py` (the PURE ladder) with
its state and its one tmux read next door in
`src/core/session_startup_gate_ledger.py`, and rendered by
`client/js/session-startup-gate.js` as `needs a keypress` on the sidebar row and
the launchpad card, plus a `StartupPrompt` toast claimed ONCE per instance.

**The signal is the ABSENCE of a hook, and it was measured.** Controlled
experiment, 2026-09-08, claude 2.1.263, throwaway `tmux -L cloude-test` socket
with the hook POSTs pointed at a local listener: launched in an UNTRUSTED
directory the pane sat alive on the trust dialog for 35.8s and the listener got
ZERO POSTs; the dialog was answered at +34s and the first hook, `SessionStart`,
landed 1.76s later. Launched in the same directory once trusted, three runs,
`SessionStart` landed 0.49s / 0.50s / 0.41s after pane birth. So a hook is
positive proof startup finished, and `STARTUP_HOOK_GRACE_SECONDS = 20` is forty
times the normal cost of getting one.

**A HOOK OUTRANKS THE SCROLLBACK, and the rung order is the whole design.** tmux
does not erase the trust dialog when it is answered, so a session that answered
it an hour ago still has the marker text in its tail. Reading the text first
would pin that session at `awaiting_startup_prompt` forever, which is why
`resolve_startup_gate` tests the hook before it ever looks at the text. Old
scrollback is STALE EVIDENCE. Rung 5 (no tail captured) refuses with `unknown`
while rung 7 (tail read, nothing matched) answers `ready` - the same asymmetry
the transcript-presence guard uses, for the same reason. `ready` claims only
"not blocked on a startup prompt", NOT "healthy": a pane measured dead answers
`ready` here, and `activity_status` is what says it died.

**Steady state costs nothing, because the tail is only read when the cheap
signals already point at a stuck session.** `should_capture_tail` gates the one
`capture-pane` on alive + past the grace window + no hook, which on a working
box is the empty set. Do not move that capture up into the unconditional path.

**The ledger is keyed by the tmux INSTANCE, not by `session_id`, and that is not
interchangeable with `SessionActivityTracker.hooks_seen`.** A confirmed live
restart (`respawn-pane -k`) keeps the session_id AND the epoch and moves only the
pane pid, so `StartupGateLedger` keys on (epoch, pane_pid) and drops the whole
record - first-hook time and toast claim together - when either is MEASURED to
move. A null never resets, or the ledger would reset every poll and re-toast
forever. And every hook kind feeds it, not just `SessionStart`: hooks are
droppable, and a session whose `SessionStart` was lost but whose `PreToolUse`
landed is plainly past its prompt. Feeding it one event kind would rebuild the
one-shot-channel-with-no-retry defect that cost this project sixteen conversation
ids.

Note for anyone touching `src/core/agent_fingerprint.py`: its
`^\s*❯\s*1\.\s*Yes, I trust this folder` pattern CANNOT fire on claude 2.1.263.
The live capture shows no option numbers and the cursor on "No, exit", so the
options read `❯ No, exit` then `  Yes, I trust this folder`. The startup gate
matches the sentence text instead, for exactly that reason.

**`unknown family` on a row whose record says `not_launched` is DATA, not a bug.**
27 of 40 rows on the owner's box carry `agent_type` NULL with
`agent_family_source='not_launched'`, which is the `auto_start_claude:false` plus
a hand-sent claude command case named under "Restarting a session" above. The
pane may well be running claude; the record says the app started no agent. Fixing
that means filling `agent_type` from evidence, not defaulting the resolver to
claude - a resolver that always finds something is worse than useless.

## How we work here

- **Every function documented**: one-line description, typed inputs, typed
  output, and an example when the usage is not obvious. Types belong in the
  Python signature, not only in the docstring.
- **DRY, single source of truth, named constants.** No magic strings. A literal
  like the hook marker or the socket name lives in exactly one module and gets
  imported.
- **New logic goes in new focused modules.** These files are already past the
  500-line guideline and should not grow: `client/js/terminal.js`,
  `client/js/launchpad.js`, `client/js/app.js`, `client/css/styles.css`,
  `src/config.py`, `src/api/routes.py`, `src/core/session_manager.py`. Edit them
  when the change belongs there; do not use them as the default landing spot.
- **No bare `except:` and no blanket `except Exception:`** that swallows. Catch
  the specific error, log it with structlog context, or re-raise. If you
  deliberately swallow, a comment says why (see the History-API guard in
  `client/js/router.js` for the shape).
- **Production ready.** No mocks, no placeholders, no test endpoints left behind.
- **`python3`, never `python`.** Tests: `venv/bin/python3 -m pytest -q` from the
  repo root. System python3 has no fastapi. Current baseline, re-measured
  2026-09-08 after the status-split and hook-token-recovery round
  (`117823d..6934965`), is 5274 passed / 3 failed / 21 skipped (the extra
  nine are `tests/test_led_real_hooks.py`, skipping because
  `CLOUDE_REAL_HOOK_TESTS=1` is not set); the three failures are the same
  ones as before, environmental and pre-existing:
  `test_home_write_guard.py::test_guard_refuses_the_real_claude_settings_path_by_name`,
  `test_state_dir_resolution.py::test_get_state_dir_default_is_never_under_the_system_temp_dir`,
  and `test_version_probe.py::test_current_version_empty_when_unresolvable`.
  Your job is to add no NEW ones. Watch for a broken environment
  manufacturing a fake baseline: a `venv` symlink pointing at a
  `venv.nosync` directory that no longer exists lets the suite limp
  along and undercount silently, rather than failing outright. If the
  numbers you see look nothing like these, check the symlink and rebuild
  the venv from `requirements.txt` before trusting the count. **That is not
  hypothetical: it happened.** The baseline this file carried before
  2026-09-08 read 4647 passed / 13 skipped and was stale by an order of
  magnitude for exactly that reason. **One test in this suite is measured
  FLAKY under a full run** (`test_respawn_refreshes_pane_env.py::test_the_session_environment_itself_is_updated`
  failed once alongside 3 passes in isolation, both runs on the same tree
  minutes apart) - it drives the real `cloude` tmux socket, which is the
  same class of flakiness INFRA-49 already names. A lone failure there
  without a code change behind it is not a new regression; re-run before
  chasing it. Node: 185 tracked files (re-counted 2026-09-08; the earlier
  "169" figure undercounted and predates this round), only the pre-existing
  `test_archive_full_page_mode.node.mjs` fails. `tests/led_state_for.node.mjs`
  is a piped-stdin CLI helper for that harness, not a standalone test, and
  exits non-zero when run with no input; that is expected, not a failure.
- **`CLOUDE_REAL_HOOK_TESTS=1` opts in to `tests/test_led_real_hooks.py`**, which
  launches a REAL `claude` in a throwaway tmux socket and asserts the status LED
  against hooks it actually fired. It is off by default because it spends real
  Claude turns and about 50 seconds; without the variable (or without tmux /
  claude / node) every test in it skips with a reason naming what went
  unmeasured.
- **`node --check`** every JS file you touch, before you claim it works.
- **Stage files by name** when committing. No `git add -A`.
- **Voice**: no em-dashes, no en-dashes, no emojis, anywhere, including commit
  messages. UI copy is lowercase and plain.

## Restarting a session, and picking what it comes back as

`POST /sessions/respawn` revives a pane whose PROCESS exited. It can ALSO
replace a running one, but only when the request says so:
`resolve_respawn_plan` answers `RESPAWN_NOT_DEAD` for a live pane unless
`live_restart_confirmed=True`, and tmux itself refuses `respawn-pane`
without `-k`. See "Replacing what is running" below.

**The ladder gates on tmux's `#{pane_start_command}`, and the empty case is
the trap.** Empty means the pane was born a bare shell, so a restart lands on
`RESPAWN_SHELL` and hands back a LOGIN SHELL rather than the agent - silently,
for a real subset of the sessions on a working box, because
`sessions.agent_type` lands NULL for every session created with
`auto_start_claude:false` plus a hand-sent claude command.

| Piece | File |
|---|---|
| The ladder, the projection, and pane liveness | `src/core/session_respawn.py` |
| Which conversation it comes back on | `src/core/session_resume_target.py` |
| Shape the preview the picker reads | `src/core/session_restart_preview.py` |
| Validate an `agent_type` choice, and persist it | `src/core/session_agent_choice.py` |
| `GET /sessions/restart/preview` | `src/api/restart_routes.py` |
| The panel, and the reopen afterwards | `client/js/session-restart-picker.js`, `client/js/session-restart-return.js` |
| The option list, extracted so the picker stays under 500 lines | `client/js/session-restart-options.js` |
| Keep the row keyed on its instance after a kill | `src/core/session_instance_rekey.py` |
| The arm control and the kill confirmation | `client/js/session-restart-live.js` |
| What the user is told about the conversation | `client/js/session-restart-continuity.js` |

**A PREDICTION IS NEVER A PERMISSION, and that is why the preview reports the
rung twice.** `resolve_respawn_plan` short-circuits on `not_dead` BEFORE it
reads the start command, so on its own it can only tell you a running session
is running - not what it would come back AS, which is the only interesting
question about the idle-but-alive sessions a user actually wants to restart.
So the tail of the ladder is factored into `_rung_from_start_command` and
reached two ways: `resolve_respawn_plan` through the probe gate AND the
liveness gate, `project_restart_rung` through the probe gate only. There is
still ONE ladder. `unchanged` is what a restart does now, `projected` is what
it would come back as, `pane_state` (`dead` / `alive` / `unknown`) is liveness
on its own. A UI badge may read `projected`; only `unchanged` / `actionable_now`
may enable a button. Wire the badge to the button and every live session
becomes restartable.

**`agent_type` on the respawn request is an ID, never a command.** It is
validated against `agents.wrappers` and an unconfigured id is a 400.
`Settings.get_agent_command` deliberately falls back to the default wrapper for
an unknown type, which is right for a launch and wrong for a picker - a user
who asks for `claude-chrome` and silently gets `claude-skip-permissions` has
been lied to. Validate through `session_agent_choice.validate_agent_choice`
first; never call `get_agent_command` with a user-supplied id directly.

**An explicit choice outranks the `pane_start_command` gate; nothing else
does.** The gate exists because a STORED `agent_type` is not evidence of
intent. A wrapper picked in this request, after the user was shown what it
would do, is different evidence. It does NOT outrank `not_dead` or a probe that
did not answer. The verdict stays `RESPAWN_AGENT`; `RespawnPlan.chosen` and a
different sentence carry the distinction rather than a sixth kind.

**A RESTART MEANS A RESUME, ON EVERY RUNG THAT CAN.** The owner's
definition, 2026-09-07, verbatim: "restart on recent is really just
resume. restart on open is close and resume session so it loads a new
wrapper or new claude binary." ONE semantic, two mechanics: a dead row has
no process to kill so its restart IS a resume, and a live row has its pane
killed first, the kill existing only so the pane picks up a new wrapper or
a new claude binary. Both come back on the SAME conversation.

`f95a9ed` made that true only on the REPLAY rung, by accident of what tmux
had written down. The AGENT rung re-derives its command through
`Settings.get_agent_command`, which carries no `--resume`, so a restart
there started a FRESH conversation wearing the old session's name.
`sessions.claude_session_uuid` now reaches that command as
`extra_args=['--resume', <uuid>]`, built once by
`session_resume_target.resume_extra_args` and passed to all FOUR command
resolutions in a restart request - the stored agent's and every wrapper
offer's, on the action side and on the preview side. Building it in one
place is what makes the preview's predicted command and the action's
actual command the same string by construction. NEVER concatenate the flag
onto a resolved command: `get_agent_command` returns
`zsh -c 'source ~/.zshrc ...; cld'` and an appended argument lands outside
that quoting, handed to zsh instead of to claude.

**The resume drops claude's own `--name`, and that is a known gap, not a
bug fix waiting to be noticed.** `resume_extra_args` carries `--resume
<uuid>` only, so a restarted session comes back without whatever name
claude itself had been given (`--name`, `/rename`). The app's own row title
survives regardless, because it lives in `sessions.title`, not in claude's
argv. Open item: reuse `claude_title_sync`'s read of the transcript's last
`custom-title` to reapply the name on a resume the same way it already
detects one.

**A MISSING uuid IS A NAMED OUTCOME, NOT A SILENT FRESH START.**
`RespawnPlan.conversation` and the preview's `conversation` field carry
`resumed` / `none_recorded` / `unknown`, the SAME three words
`RestartSessionResponse.conversation` has used since the restart route
shipped, reused rather than re-invented. `none_recorded` means the row was
READ and holds no conversation, so the session comes back WITHOUT its
history - a legitimate restart, said out loud rather than performed
quietly. `unknown` means the row could not be read: no `--resume` is
injected and nothing claims a resume, because an unknown is never a yes.
The value is DERIVED FROM THE ARGV - a command carrying a `--resume` reads
`resumed` whatever the caller believed - so the claim can never outrun the
command. The rung sentence the picker renders verbatim carries the clause,
and `SessionRestartLive.liveConfirmCopy` names it before anything dies.

Note the asymmetry, it is deliberate: an unreadable ROW degrades the claim
and never refuses, while a MEASURED missing TRANSCRIPT refuses outright.
Not having looked is not evidence of absence; having looked and found
nothing is.

**A REPLAY CAN RESUME A CONVERSATION, and that is guarded.** `RESPAWN_REPLAY`
hands tmux back its own `#{pane_start_command}`, and measured on the owner's
box 2026-09-07, 3 of 19 live sessions carry an explicit `--resume <uuid>` in
theirs. So a replay can re-run a resume, and a resume against a deleted
transcript exits instantly, leaving a dead pane the row still calls running -
the incident this project already paid for. `resume_uuid_in` extracts the uuid,
`refuse_if_transcript_missing` turns a DEFINITE absence into
`RESPAWN_TRANSCRIPT_MISSING`, and the filesystem lookup lives in
`src/core/session_transcript_presence.py` so the ladder stays pure. THE
GUARD COVERS THE AGENT RUNG TOO now that it resumes, on the dead path as
well as the live one: the check in `TmuxBackend.respawn` keys on
`plan.resume_uuid` and is not gated on liveness. TWO CONVERSATIONS CAN BE
IN PLAY AT ONCE - the replay rung resumes what tmux recorded, the agent
rung resumes what the ROW says - so the preview passes presence verdicts
as `presence_by_uuid`, keyed by the uuid each was measured for. One
verdict applied to both would refuse a restart nobody measured.
`unchecked` NEVER refuses - not having been able to look is not evidence a file is gone, and
refusing on it would break restart on every machine whose corpus lives somewhere
the checker was not told about. The PREVIEW applies the same guard; a preview
that skipped it would promise a replay the restart then declines.

**Replacing what is running: `respawn-pane -k`, and the four gates in
front of it.** The owner's two calls (2026-09-07) were "same tmux should
be fine" and "yes resume the same session", so this is not
close-and-recreate. It kills the pane's process and respawns it in the
same pane, same tmux name, same row - and because no row is minted,
project attribution, pinned theme, unread state, group filing and
sidebar position all stay put without anything re-carrying them. It also
sidesteps rather than fixes the `session_group_members` primary-key
defect, which keys on `tmux_name`.

It is DESTRUCTIVE and irreversible, so it is gated four times and no
gate is derivable from a prediction:

1. `actionsFor` offers restart on a row whose status we POSITIVELY know
   is live. `unknown` still gets close alone.
2. The picker's arm checkbox (`SessionRestartLive.armHtml`, always
   emitted unchecked, takes no argument) is what unlocks the choices. `optionsHtml` derives
   `disabled` from `actionable_now` ALONE, so a live pane paints every
   radio locked whatever it projects.
3. `App.showConfirmModal` with `SessionRestartLive.liveConfirmCopy`,
   which names the
   bare-shell outcome AND what happens to the conversation. Measured
   2026-09-08: 18 of 22 live sessions have an empty `pane_start_command`,
   so an UNPICKED restart comes back a login shell for 82 percent of them
   (it read 15 of 19 on 2026-09-07; the population moves, re-measure
   rather than quoting either). That warning is what makes this safe to
   ship. Note the whole sentence: an explicit wrapper choice OVERRIDES
   the gate (`session_respawn.py:542`), so only an unpicked restart lands
   on the shell rung.
4. `confirm_restart_live` on the request. `RespawnPlan.kills_live_pane`
   is the ONLY thing that makes anything pass `-k`, and it is set only
   when the pane was measured alive AND the caller confirmed AND the
   rung is actionable. `project_restart_rung` has no liveness input, so
   a projection cannot set it. `refuse_if_transcript_missing` BUILDS its
   refusal rather than copying, so a missing transcript cannot kill.

`activity_status` informs all of this and refuses none of it - it reads
`working` for about four minutes after a resume.

**Identity is MEASURED across the kill, not assumed.** `#{session_created}`
belongs to the SESSION and `-k` replaces the pane's PROCESS, so on tmux
3.7c the instance triple does not move (measured 2026-09-07: same epoch,
same `pane_id`, new `pane_pid`). Fourteen queries in `src/core` key on
that triple exactly and all read the same column, so they break or hold
together. `TmuxBackend.respawn` therefore reads the epoch either side and
`session_instance_rekey.reconcile_instance_epoch` answers `unchanged` /
`rekeyed` / `cannot_determine`, re-keying the row on the OLD triple if it
ever does move. `cannot_determine` is not `unchanged`; a reading that did
not answer is not evidence nothing moved.

**Respawn writes exactly one column, and only when asked.** On a restart
verified alive with a picked wrapper, `sessions.agent_type` is updated on the
row keyed by the instance triple. Nothing else - a respawn is still not a fork
and never touches a lineage column. With no `agent_type` it issues no write at
all. A restart that FAILED records nothing, and `agent_type_persisted` says so
rather than letting a stuck choice look saved.

## The conversation id, and how a row loses it

`sessions.claude_session_uuid` is the id a restart resumes: the row's copy of a
transcript uuid. Do not conflate it with the instance triple, a pin id or a
project name. Everything in this section was traced to file and line on 2026-09-08 and
shipped in `8dd54a8`. **Note that `8dd54a8` is committed but not deployed at
the time of writing**, so a live install may still be showing the old
behaviour.

| Piece | File |
|---|---|
| Second chance at correlation when the hook writes nothing | `src/core/session_lineage_recovery.py` |
| How Claude Code actually slugifies a working directory | `src/core/claude_project_dirs.py` |
| The correlation ladder itself | `src/core/claude_transcript_correlate.py` |
| Propose a uuid for a row that lacks one | `src/core/session_uuid_backfill.py` |
| What counts as evidence, and what only corroborates | `src/core/session_uuid_backfill_rules.py` |
| Render the proposal for a human | `src/core/session_uuid_backfill_report.py` |
| The operator entry point, DRY RUN BY DEFAULT | `scripts/backfill_claude_session_uuid.py` |

**A ONE-SHOT CHANNEL WITH NO RETRY IS THE WHOLE PROBLEM.** On the create path
the uuid had exactly ONE writer, Claude Code's `SessionStart` hook. An empty
POST body becomes `{}` at `routes.py:2108`, `session_manager.py:4341` returns
`LINEAGE_UNRESOLVED`, and nothing is written. The live log holds 25 such
failures across 20 distinct sessions, and 16 of 39 rows carried no uuid at all.
Contrast the other hook events, which repeat: that is precisely why
`last_work_at` self-heals on the next event and the uuid never did. If you add
a field fed by a hook, ask which of the two kinds of event feeds it, and give
the one-shot kind a recovery path rather than a log line.

**A FALLBACK THAT CANNOT FIRE IS NOT A FALLBACK, AND IT IS INVISIBLE.**
`slugify_project_dir` mapped only `/` and `.`. The real rule replaces
EVERYTHING outside `[A-Za-z0-9-]` with a single `-`, verified against 908 of
919 live transcripts. Every path on the developer's machine contains a space
and two tildes, so every slug the ladder built was wrong and the fallback had
NEVER ONCE SUCCEEDED there. It looked exactly like a fallback that was never
needed. A ladder rung that has never been observed to fire is unmeasured, not
proven.

**A RECORDED uuid IS NOT EVIDENCE A TRANSCRIPT EXISTS.** Rows fall into three
groups, not two: over 39 rows, 16 absent, 18 sound, and 5 PHANTOM, holding a
uuid with no file behind it. All 5 phantoms collide with a sibling row, because
the correct uuid is already held by the twin that the cwd spelling trap split
off. The mechanism is `--fork-session`: it MINTS A NEW uuid, `SessionStart`
records the new one, and the forked transcript may never materialise. So the
picker can say "no transcript for <uuid>" perfectly truthfully about a uuid
that has nothing to do with the conversation the user is in, while the real
73 MB transcript sits on the archived twin. A message can be locally correct
and lead every reader to the wrong conclusion.

**Both cwd spellings must be resolved in BOTH directions.** Forward, by
slugifying every spelling found in a HOME symlink scan; and backward, from each
transcript's own recorded cwd, canonicalised. Backward is not optional: 754 of
919 transcripts sit in a directory that disagrees with their own recorded cwd.

**Evidence is counted in independent FAMILIES, and weak signals corroborate
rather than create.** Two matcher defects were caught by CONTROLS, not by
reading the code, and both would have shipped looking right. Directory
agreement was clearing a two-signal bar as one fact corroborating itself. And
the negative control, a row pointed at a project that does not exist while real
transcripts are present, returned `ambiguous` instead of `no_candidate` because
timing alone counted as evidence; an anchor gate now lets timing and title
corroborate a candidate and never create one. **A matcher that always finds
something is worse than useless**, so a negative control is mandatory for
anything in this family.

**`agent_type` is a SEPARATE failure, do not fold them together.** A crosstab
over all 39 rows finds 14 with `agent_type` NULL and a hook-written uuid. The
two are independent, and the `agent_type` persistence gap is still its own open
item.

## Naming a session, and the one name rule

ONE NAME PER SESSION, LAST RENAME WINS FROM EITHER SIDE. It lives in three places
- `sessions.title` (the browser), claude's own name (`--name`, `/rename`) and the
jsonl's `custom-title` record, where the two writers MEET. See
`src/core/claude_title_sync.py` (tail reader + rules), `claude_title_sync_apply.py`
(the seam, run from the hook route on every event), `claude_rename.py` (push out).
**NO HOOK EVENT CARRIES A `/rename`** - claude intercepts slash commands before
they become prompts - so the pane's name is only readable by READING THE
TRANSCRIPT. Last 64 KB only, 0.274 ms median against a 244 MB file, because this
runs on `PreToolUse`; an older rename reads `no_record` and changes nothing.
`sessions.claude_title` stops being dead weight and becomes the marker that makes
the sync idempotent under duplicated events.
**FIRST SIGHT OF A TITLE IS A BASELINE, NOT AN INSTRUCTION**: `custom-title` has
no timestamp, so it cannot be ordered against the label already on the row.
**A BOUND uuid IS NOT EVIDENCE A TRANSCRIPT EXISTS** and the push paid for it -
14:47:41Z 2026-09-08, a rename logged `claude_rename_pushed` and delivered
nothing because the file appeared 2m33s later, `--resume` exited 1, stderr went to
DEVNULL. `decide_push` now defers on a MEASURED absence only (`unchecked` still
sends) and `spawn_oob_rename` reaps and logs. STILL OPEN: a deferred push is
never retried; `title != claude_title` is the marker a retry would key on.
**The plain create endpoint was the one creator passing no label**, so only
launchpad sessions launched claude with no `--name`; `CreateSessionRequest.label`
closes it, and an absent label leaves the command line byte-identical.

## Where a new project's folder comes from

A project's directory is `sessions.working_dir`, and it is permanent: the
row carries it, the launcher lists it, and the archive derives a
transcript directory from it. So the create path is the one place that can
poison every downstream reader, and until 2026-09-08 it did.

| Piece | File |
|---|---|
| Compose, validate and create the directory | `src/core/project_directory.py` |
| The folder step, and the pure rules behind it | `client/js/project-create-folder.js` |
| Where it is wired in | `src/api/routes.py` (`create_session`), `client/js/launchpad.js` (`_createNewSessionInner`) |

**"START EMPTY" HAD NO FOLDER STEP AT ALL.** The chain was "+" > new
claude project > start empty > provider > name this project > create
session, and nothing in it ever asked where the project should live. The
client posted a name and no `working_dir`, so `SessionManager.create_session`
fell through to `work_path = settings.get_working_dir() / session_id` and a
project the user named `Punchlist Test` was created at
`.../ses_5a756046`. That is not a folder anyone chose, recognises, or can
find later, and it was written into the row as the project's home. The
fallback is still there for an old client, but it now logs at warning
level: it went unnoticed for as long as it did precisely because it was
silent.

**THE SECOND DEFECT WAS IN THE SAME LINE, AND IT IS GOTCHA 6.** That path
was built with `Path.expanduser()`, which expands `~` and stops. It does
NOT resolve symlinks, so `~/Development` stayed the SHORT spelling of a
directory that really lives in iCloud, and a short spelling is how one
directory becomes two projects. `project_directory` canonicalises with
`os.path.realpath` instead, so the LONG spelling is what reaches the row.
`tests/test_project_directory.py` asserts that against a real symlink
rather than trusting the reading.

**THE NEW FIELD IS `project_parent_dir`, NEVER `working_dir`.** Three
shipped flows already post `working_dir` with a folder from anywhere on
disk: "open an existing folder", the new-console FAB (it posts `~`), and
clone. Attaching a root restriction to that field would start refusing
folders they have always accepted, which is a worse bug than the one being
fixed. A restriction on a field nothing used to send cannot regress
anything. The server joins the parent to `project_name` itself, so the
client cannot compose a path the server did not check.

**Traversal and symlink escape are ONE check.** The parent goes through
`realpath` BEFORE any comparison, so `..` cannot survive and a symlink
bridge resolves to where it really points. Containment is component-wise,
never `str.startswith`, or `/Users/jsugamelevil` reads as living under
`/Users/jsugamele`. The allowed roots are the projects root plus HOME, and
home is deliberately generous: the owner's projects live under the iCloud
Sync path rather than under `DEFAULT_WORKING_DIR`, so a root set of only
the projects root would refuse the exact folders the folder step exists to
offer. That is a silent way of not shipping the feature.

**A name is REFUSED, never rewritten.** Spaces are legal and stay verbatim
(the owner's own projects have them). `/`, `\`, NUL, control characters, a
leading dot, `.` and `..`, and anything over 255 bytes come back as an
inline sentence. A sanitiser that turned `a/b` into `a-b` would make a
folder the user did not ask for and cannot find. An existing EMPTY target
directory is fine; a non-empty one refuses.

"clone from github" does NOT have this defect: it has collected a parent
directory since it shipped (`launchpad.js`, `modal-clone-parent`).

## The status lights, and what they are allowed to claim

Full model in `docs/session-status.md`. The eight states are `working`,
`working_subagent`, `question`, `notice`, `finished_unread`, `idle`,
`dead` and `unknown`. `UserPromptSubmit`/`PreToolUse`/`PostToolUse` move to
`working`; `SubagentStart` with no matching `SubagentStop` to
`working_subagent`; `PermissionRequest` to `question` and `Notification`
to `notice`; `Stop` to `finished_unread` while unread, `idle` once seen;
tmux's `#{pane_dead}` to `dead`; everything else is `unknown`, which is a
real answer and never `idle`.

**`question` AND `notice` ARE TWO STATES BECAUSE A PERMISSION PROMPT
STOPS THE AGENT AND A NOTIFICATION DOES NOT.** They were one state named
`question` until 2026-09-08. A `PermissionRequest` halts claude mid-turn
until a human answers a yes/no; a `Notification` is claude asking to be
looked at while nothing is blocked. Collapsed, a chatty session painted
exactly like a parked one, so the state that most needed acting on
stopped standing out - the false-urgency twin of this project's
false-green problem. They are TWO INDEPENDENT BOOLEANS,
`permission_open` and `notice_open`, not one field with three values:
hooks arrive unordered and duplicated, so a `Notification` landing either
side of the `PermissionRequest` it accompanies must not be able to move
the blocking claim. `permission_open` is read first, so a session holding
both answers `question`. Both are cleared by the same three events
(`UserPromptSubmit`, `PreToolUse`, `Stop`) because what resolves either
is the user showing up. On the LED, `question` is inner
`waiting-permission` (its own hue, `--led-color-permission`) and `notice`
is `waiting-input`, shared with the startup gate - both mean "come and
look", neither means "approve this". Summary priority is
**permission > input > working > unread > done > dead > unknown**.

**A CLOSING HOOK EVENT IS NOT A HEARTBEAT ON ITS OWN, and that was
punchlist 4.** Measured twice by `tests/test_led_real_hooks.py` on claude
2.1.265: on a turn with NO SUBAGENT IN IT, `SubagentStop` arrives about
1.5s AFTER `Stop`. `Stop` had just cleared `last_tool_event_ts` to say the
turn was over, `record_event` stamped it again, and a finished session
painted `working` for the full 120s - `finished_unread` lasted a second
and a half and `idle` was UNREACHABLE. The rule now: an event that CLOSES
something stamps only when something was open for it to close.
`SubagentStop` needs `subagent_depth > 0` (at zero it moves nothing and
logs `subagent_stop_without_start` at debug); `PostToolUse` keys on a
`turn_open` boolean that every OPENING event sets and `Stop` clears, and
is refused ONLY when a `Stop` was POSITIVELY seen and nothing has opened
since - never having seen a `Stop` is not evidence the turn ended.

**A DEAD SESSION KEEPS ITS ROW.** `dead` was unreachable from live data
until 2026-09-08: `resolve_listing_liveness` answered ONE verdict, `gone`,
for two different facts - "the backend says there is no such tmux session"
and "the session is there and its pane is a corpse" - and
`_session_info_for` dropped the row for both, while `/sessions/attachable`
filters out every name bound to a live backend. So a killed pane VANISHED
off the sidebar and the running list, with `dead`/`off` sitting in
`status-led.js` and restart + remove sitting in `actionsFor('dead')`,
never delivered a row to paint. `src/core/session_liveness.py` splits it
four ways, borrowing the pane words from `session_respawn.py`:
`alive` / `pane_dead` / `session_gone` / `unknown`. `pane_dead` KEEPS the
row and says `dead`, because `remain-on-exit` holding the corpse open is
the same fact that lets `respawn-pane` revive it; `session_gone` drops it
exactly as before and the reaper files the stored row into the recent
list, where a restart is a resume. Existence is read BEFORE the pane, so a
stale `dead` row cannot keep a session tmux no longer has on screen.
`keeps_row` is an ALLOW-LIST of what survives, so a verdict added later
cannot silently inherit "make the row vanish".

THE BOOT RE-ADOPT STILL REFUSES A DEAD PANE, and that is correct rather
than a hole this left. `attach_existing(needs_pipe_setup=True)` cannot
pipe-pane a corpse, so it raises and the pass (which gathers with
`return_exceptions=True`) simply does not hold that session. The row does
not disappear: with no live backend bound to the name,
`/sessions/attachable` lists it and decorates it with
`map_tmux_fallback(STATUS_DEAD)`, which is the path that has ALWAYS
surfaced a husk. The two are complementary - bound to a backend, the
session says `dead` on `/sessions/list`; unbound, it says `dead` on
`/sessions/attachable` - and after this change they finally agree.

**A tmux `running` pane maps to `unknown`, NOT `working`.** It means only
"the foreground command is not a bare shell", which is equally true of an
agent mid-tool-call and one at an empty prompt, and the fallback carries no
timestamp so nothing could expire the claim. Measured 2026-09-08: 15 of 19
live sessions report a claude VERSION STRING as `pane_current_command`, so
that branch is the common case and all 15 were reporting a permanent
`working` on no evidence. Hook-fed `working` still expires after 120s.

**Unread is keyed on the INSTANCE**, `<tmux_name>@<#{session_created}>`,
because a name is reused and a flag from a killed session reappeared on its
successor. Set on `Stop` and by the user's control, cleared when a WS
terminal binds. An unmeasurable epoch degrades to the legacy name key.

**The LED is two independent rings** (`client/js/status-led.js`): an inner
dot for the chat's status and an outer halo for activity and attention, so
"working, and also unread" is sayable. `dotHtml` delegates to it, so every
surface renders the same component.

## The transcript archive the app maintains

The app keeps a byte-exact archive of this machine's Claude Code
transcript corpus (`~/.claude/projects`) inside its own `cloude.db`. It
is a background loop, started second-to-last in `lifespan()` (only the
database integrity scheduler starts after it) and stopped first on
shutdown, and it is fail-soft in exactly the way `ensure_db_migrated`
and `claude_hooks.ensure_hook_settings` are: boot never waits on it and
never fails because of it.

| Piece | File |
|---|---|
| One incremental pass, start to finish | `src/core/corpus_ingest_service.py` |
| The scan plan and the two DB fingerprints it rests on | `src/core/corpus_ingest_scan.py` |
| Scan cache + liveness artifact on disk | `src/core/corpus_ingest_state.py` |
| The background loop | `src/core/corpus_ingest_task.py` |
| The read-only status object | `src/core/corpus_status.py` |
| `GET /corpus/status`, `POST /corpus/ingest` | `src/api/corpus_routes.py` |

Four things worth knowing before you touch it.

**A steady-state pass must stay invisible.** It is measured at about 40 ms
over a 400-file archive and about one second of `stat` calls over the
real 19,065-file corpus. Two shortcuts buy that, and BOTH refuse
themselves rather than guess: the scan cache skips a file only when its
size, its mtime and the hash the database holds all agree, and the
incremental hash query is only used while the `install_id` matches and
`max_archive_id` has not gone backwards. If you add work to the pass,
measure it against those numbers.

**A skipped rooting pass is a named state, not zeros.** `report.rooting`
carries `status: ran` or `status: skipped_unchanged`. Do not "simplify"
it back to a bare count dict; a reader would then be unable to tell
"rooted nothing" from "did not look".

**Liveness is published on every terminating path, including failures**,
and its AGE is the signal. An ingester that dies looks exactly like one
finding nothing new, so `var`-style artifacts live under
`<state_dir>/corpus-ingest/` and `GET /corpus/status` reports the age of
`latest.json` with four outcomes: `current`, `stale`, `never_ran`,
`cannot_determine`.

**It maintains the ARCHIVE, not the v16 message model.** The message
model refuses a `source_ref` it has already ingested, on purpose, which
makes it the wrong layer for transcripts that grow while the app watches
them. The status endpoint still reports that model's gate findings
read-only, and says `model_not_populated` rather than "0 findings" when
it holds nothing.

`CLOUDE_CORPUS_INGEST=0` switches the loop off; it defaults OFF under
`CLOUDE_TEST_MODE` so a pytest run never reads the developer's real
corpus. `CLOUDE_CORPUS_ROOT` relocates the corpus,
`CLOUDE_CORPUS_INGEST_INTERVAL` the sleep between passes.

## The daily database integrity check

`PRAGMA integrity_check` is a MAINTENANCE OPERATION, NOT A LIVENESS PROBE, and
that distinction cost the project real time. `GET /api/v1/version` used to run
it synchronously inside its own coroutine on every request. The Electron tray
polls that endpoint every 20 seconds (`macOS/main.js`, `INTERVAL_MS = 20000`)
and the pragma re-verifies every page of every B-tree, so on a 4.5 GB
`cloude.db` the event loop was blocked for roughly 14 of every 20 seconds on an
idle machine, and the stall grew with the file. Wrapping it in a thread would
have freed the loop and still burned those seconds of disk three times a
minute, forever, which is why it was rejected.

| Piece | File |
|---|---|
| Run one check and publish the verdict | `src/core/db_integrity.py` |
| Read the verdict and decide what may be said | `src/core/db_integrity_status.py` |
| The background loop | `src/core/db_integrity_task.py` |
| The cheap per-request probe | `src/core/db_health.py` |
| Atomic write / tolerant read, shared with the ingester | `src/core/json_artifact.py` |

**The request path is now one connect plus one small SELECT**, plus one read of
a small JSON file. Measured at 0.46 ms median against a 310 MB database where
the pragma took 112 ms, a 215x difference that widens with the file. Do not put
a pragma back on it; `tests/test_db_integrity_verdict.py` booby-traps every
binding of the helper and fails the build if one appears.

**Measured on live after the deploy, 2026-09-07**, against the real 4.5 GB
file: p99 **14,505.7 ms to 13.3 ms**, p50 30.5 to 5.2 ms, the 20.0s cadence of
14.5s stalls GONE, and the share of the window stalled 86.5 percent to 0.0
percent. The measurement to trust is that the SAMPLE COUNT TRIPLED at an
unchanged poll rate, because that is independent of the timings: more samples
come back only if the loop is free to answer them. **13.3 ms is NOT settled.**
Later polls read p99 64-72 ms and most recently 66-73 ms. Still three orders of
magnitude better than the broken state and with no stall pattern, but about
five times that figure and NOT attributable; the corpus ingester was active
during the later runs, which is a hypothesis that was not tested. Re-measure on
a quiet box before quoting a number.

**The diagnosis is worth keeping for its shape: it was a REQUEST HANDLER ON A
TIMER, not a background task.** Several passes looking for a periodic
server-side loop found nothing, because nothing looped; the Electron tray
polled. When you are chasing a periodic stall, read what POLLS as well as what
loops. It was confirmed twice independently before any fix was written: py-spy
returned 17 of 17 stalled dumps byte-identical, and the static call chain
agreed.

**The cached verdict carries two facts, so it has two fields.** `verdict` is
`ok` / `failed` / `cannot_determine`; `freshness` is `current` / `stale` /
`never_ran` / `cannot_determine`, the same four-value vocabulary
`src/core/corpus_ingest_state.py` uses, reused rather than re-invented.
`verdict` reads `ok` ONLY when a check actually ran AND its record is inside
the freshness window, so "never checked" can never render as "checked and
sound". A recorded `failed` still degrades `data.status` exactly as the live
pragma did; `cannot_determine` deliberately does not, because not having looked
is not a fault.

`CLOUDE_DB_INTEGRITY_CHECK=0` switches the loop off; it defaults OFF under
`CLOUDE_TEST_MODE` so a pytest run never walks the developer's real database.
`CLOUDE_DB_INTEGRITY_CHECK_INTERVAL` overrides the daily interval, and the
staleness window is derived from it (two intervals) rather than hardcoded. The
loop also asks the artifact whether a check is due before its first run, so
restarting this menubar app all day does not re-walk the file each time.

## Secret scanning

`./scripts/install-secret-hook.sh` installs a pre-commit hook that refuses a
commit staging credential material; `./scripts/uninstall-secret-hook.sh`
removes it. `.git/hooks` is not version controlled, so the installer is the
distribution mechanism and has to be run once per clone.

`src/core/message_model_secrets.py` is the single source of truth for what
counts as a secret, shared with the transcript message model. Add a detector
there and a case to `tests/test_secret_detectors.py`; never write a second set
of patterns. No matched value is ever printed, logged or stored, by any path.

Audit the tree with `./venv/bin/python3 scripts/scan_secrets.py`. Exit 2 means
could-not-scan and is not a pass. Full detail in `docs/secret-scanning.md`.

## Upgrading an install

`docs/upgrade-with-claude.md` is the runbook, and `/upgrade`
(`.claude/commands/upgrade.md`) is the entry point. The one rule that matters:
take `./scripts/upgrade-baseline.sh` BEFORE touching anything, because you
cannot verify a migration without a record of what the data was, and that is
the step everyone skips. `./scripts/upgrade-verify.sh` exits 2 when a check
could not be evaluated; 2 is not 0.

## Imported conversations

`scripts/import_transcript_sessions.py` gives every real Claude Code
conversation a `sessions` row. DRY RUN BY DEFAULT. Measured 2026-09-08:
1,486 transcripts against 43 rows; it wrote 895 ARCHIVED rows into 12
existing and 59 CREATED archived projects, so nothing lands on a screen
until "show archived" is on. Four things stay out, each a NAMED outcome:
a cwd under `/private/tmp`, `/tmp` or `/var/folders` (the ONLY exclusion,
and it is about the path - `fstest` and `llmScratch` work is real and
imports); 319 files with no `cwd` anywhere (`file-history-snapshot`
bookkeeping); 224 `agent-<id>.jsonl` SUBAGENT runs, whose records report
the PARENT's `sessionId` - which is why identity is the FILE STEM; and
the older of two transcripts for one uuid split by a cwd spelling.

**NOT ALL OF IT IS THE OWNER'S TYPING, AND THE LISTS NOW SAY SO.** The
owner's rule, verbatim: "lists should always just be mine. the rest can
be found in the archive explorer." `sessions.kind` (schema v25) carries
three words and never a fourth: `interactive` / `automated` / `unknown`.
Measured over the 895 imported rows 2026-09-08 and applied on live: 320
interactive, 270 automated, 305 unknown. `GET /sessions/records` and
`GET /sessions/recent` exclude `kind='automated'` by default;
`include_automated=true` returns it and no UI sets that. `/archive` reads
the transcript archive and never `sessions`, so nothing here can hide a
transcript from the explorer.

**ONLY A FACT THE MACHINERY WROTE MAY SAY `automated`.** Two rungs, both
emitted by the thing that did the automating: a `<scheduled-task ...>`
tag around the prompt (259 rows), and `entrypoint: sdk-cli`, the headless
SDK path (11). Interactive is reached by `entrypoint` `cli` or
`claude-desktop` (266), a `planContent` record (30), or a `custom-title`
/ `mode` record (24). Title shape, turn count and prompt wording are NOT
in the ladder: 28 rows titled "Implement the following plan: ..." read as
delegated work and every one is a HUMAN approving a plan in the TUI,
while 3 scheduler runs are titled "Urbackup completion proof" and a title
rule misses all three. `src/core/session_kind.py` owns the ladder;
`scripts/classify_session_kind.py` is the operator entry point, DRY RUN
BY DEFAULT, and it prints those negative controls on every run.

**THE 305 UNKNOWNS ARE AN ERA, NOT A GAP, AND THEY STAY IN THE LISTS.**
Every one was written by claude <= 2.1.77, which predates the
`entrypoint` field; the earliest confirmed scheduler run is 2.1.121 and
the earliest confirmed headless run is 2.1.198. The corpus holds NO
confirmed automated run from that era to derive a marker from. NULL and
`unknown` both list, because every reader excludes on `kind='automated'`
ALONE - not having looked is not evidence of automation.

An imported row has no `tmux_name`, epoch, `agent_type` or `model`, none
of it invented, so `GET /sessions/restart/preview` cannot ADDRESS it.
`session_imported_restart.py` + `imported_restart_routes.py` are the path
that can: keyed on `session_uuid`, a restart CREATES a session with
`--resume <uuid>` and `reuse_session_id` on the imported row. The launch
directory is MEASURED across every spelling, because `--resume` finds the
file only under the slug of the LITERAL cwd; a measured absence refuses,
`unchecked` never does.

## Gotchas that have cost real time

1. **Wrapper vs `.session`.** Described above. When a field reads as missing,
   check which level you are on before you go looking in the backend.
2. **Hook events are unordered, duplicated and droppable.** A state machine that
   assumes ordering works on your machine and drifts in the field. Floor the
   counters, make every transition idempotent.
3. **An adopted session is not a launcher "project".** Deep-link resolution walks
   launcher projects first, then live/adopted sessions
   (`client/js/router.js`, `Launchpad.openProjectByName()`). Resolving only
   against projects made a deep link spawn a duplicate session next to the one
   the user was already in. Unresolvable targets go through `rejectTarget()`, one
   banner, one `replaceState` back to `/`, never a silent bounce.
4. **The tmux socket is load-bearing.** Anything that shells out to `tmux`
   without `-L cloude` is talking to the user's personal tmux server. That is how
   you kill someone else's work.
4b. **A session id is not a tmux name, and deriving one from the other loses
   sessions.** `build_backend` with no `session_name` rebuilds
   `cloude_<slug(session_id)>`, which for an adopted id yields
   `cloude_adopted_cloude_Foo` - a name no socket has ever carried. It then fails
   the liveness test and `_clear_stale_metadata` throws the pointer away. Pass the
   STORED `tmux_session`, and keep the derivation as the fallback for pre-field
   metadata.
5. **A uuid on the row is not evidence a transcript exists, and a missing
   transcript is not evidence the conversation is gone.** Five rows on the
   developer's box hold a phantom uuid minted by `--fork-session` while the
   real conversation lives on an archived twin row. Check the twin before
   telling anyone their history is lost.
6. **cwd spelling splits a session in two.** `~/Development` is a symlink into
   iCloud and Claude Code derives its transcript directory from the LITERAL cwd
   string, so two spellings of one directory make two transcript directories,
   two project rows and, as above, two session rows. Always write the long
   iCloud spelling, in code and in documents. **Qualified 2026-09-08:** the
   historic split in the data is real, and claude 2.1.263 resolves symlinks
   before slugging its transcript path, so `--resume` now finds a transcript
   from either spelling of the cwd and a NEW split cannot originate from
   claude itself. It is not fully fixed, though - this app's own project
   creation kept WRITING the short symlinked spelling into `working_dir`
   until `a4eeef1` closed that path today.
7. **`if (pinned) apply()` with no else leaves the last session's theme on
   screen.** A pinned theme bled across session switches because three
   copy-pasted restores in `app.js` and two session-entry paths each applied a
   theme and never reset one. A missing else is not a missing feature, it is
   state left over from the previous thing. There is now ONE total function,
   `applyForTarget()` in `client/js/theme-navigation.js`, and every navigation
   goes through it. Fixed in `a6b6b91`.
8. **A stale doc is worse than no doc.** A missing doc sends the next agent to
   read the code; a confidently wrong one sends it to write a bug. If you change
   behavior this file describes, update this file in the same change. If you find
   a claim here that reality contradicts, fix it and say so in the commit.
