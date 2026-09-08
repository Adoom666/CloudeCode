# Session status: one state model, two rings

What the lights mean, what moves them, and where each fact is stored. This
is the single source of truth for the status model; if code and this file
disagree, one of them is a bug.

## The vocabulary lives in one file

Every status string this app can show is spelled once, in
`src/core/session_status.py`. `src/core/session_activity.py` imports them
and owns the state MACHINE; it defines no string literals of its own. The
client mirrors the same set in `client/js/session-status-ui.js`.

## The states

| state | what it claims | what moves it there |
|---|---|---|
| `working` | the agent is doing tool work | `UserPromptSubmit`, `PreToolUse`, `PostToolUse` |
| `working_subagent` | the same, inside a spawned subagent | `SubagentStart` with no matching `SubagentStop` |
| `question` | BLOCKED on the user: a yes/no nobody has answered | `PermissionRequest` |
| `notice` | claude wants attention and is NOT blocked | `Notification` |
| `finished_unread` | a turn ended and nobody has looked | `Stop`, plus the unread flag |
| `idle` | alive, at rest, already seen | `Stop`, or a bare-shell pane |
| `dead` | the pane's process exited | tmux `#{pane_dead}` = 1 |
| `unknown` | NOT MEASURED | anything else, including a live pane with no hook signal |

`unknown` is a first-class answer, not a failure mode. "I did not look"
and "I looked and found rest" are different claims and must never render
the same way.

### Why `question` and `notice` are two states, not one

They were one state until 2026-09-08, and that state was named `question`
because it carried both `PermissionRequest` and `Notification`.

**A permission prompt stops the agent. A notification does not.** A
`PermissionRequest` means claude has halted mid-turn and will make no
further progress until a human answers a yes/no. A `Notification` means
claude is asking to be looked at - it is idle at a prompt, or it has
something to say - and nothing is blocked by the user not looking. Those
are different amounts of urgency and different actions, and the single
`question` state asserted the louder of the two for both.

The cost of collapsing them is a light that cries wolf: a chatty session
firing notifications painted exactly like one genuinely parked on a
permission dialog, so the state that most needs acting on stopped being
the state that stands out. That is the false-urgency twin of this
project's recurring false-green problem, and the fix is the same shape -
say only what was measured.

**Both are cleared by the same three events** (`UserPromptSubmit`,
`PreToolUse`, `Stop`), because what resolves either one is the user
showing up, and those are the events that measure it.

**They are two independent booleans**, `permission_open` and
`notice_open`, not one field with three values. Hooks arrive unordered
and duplicated: a `Notification` landing after the `PermissionRequest` it
accompanies must not be able to downgrade the blocking claim, and one
landing before it must not be resurrected when the permission clears.
Two flags read in a fixed order converge on the same answer whatever
order the events arrive in, and applying either event twice is a no-op.

**Precedence:** `question` outranks `notice` in the same window;
`notice` outranks `working`. A stopped session is the most actionable
thing on the screen, and work that proceeds without the user is the least
of the three.

**The toasts say it too.** `_hook_event_presentation` in
`src/api/routes.py` titles a `PermissionRequest` **needs your permission**
and a `Notification` **wants your attention**. The split is only worth
having if it reaches the surface the user actually reads, and the toast is
that surface on a phone.

## The rules that keep it honest

**tmux is the only thing that can see a pane die.** The dead check runs
first and outranks every hook signal. Hooks cannot observe a dead process,
so nothing else may report `dead`.

**A DEAD SESSION KEEPS ITS ROW, and until 2026-09-08 it did not.** `dead`
is only worth having if the user can see it, and they could not: the
listing pass resolved one verdict, `gone`, from two different facts - "the
backend says there is no such tmux session" and "the session is there and
its pane is a corpse" - and dropped the row for both.
`GET /sessions/attachable` cannot catch either, because the route filters
out every tmux name bound to a live backend so the UI never offers
self-adopt. So a session whose process died VANISHED off the sidebar and
the running list, while `dead`/`off` sat in the LED table below and
`actionsFor('dead')` sat ready with restart and remove. Measured against a
real agent by `tests/test_led_real_hooks.py`, which pinned the vanishing
as the behaviour that existed.

`src/core/session_liveness.py` splits it into four named outcomes, and the
pane words are borrowed from `session_respawn.py` rather than spelled a
second time:

| verdict | what was measured | what happens to the row |
|---|---|---|
| `alive` | the session exists and its pane is live | listed, normal status |
| `pane_dead` | the session exists, `#{pane_dead}` = 1 | **listed, says `dead`** |
| `session_gone` | the backend says there is no such session | dropped; the reaper files the stored row as ended and it appears in the recent list |
| `unknown` | could not ask | listed, says `unknown` |

`pane_dead` keeps the row because `remain-on-exit` holding the corpse open
is the same fact that lets `respawn-pane` revive it - restart and remove
are both real actions on that row, and neither is reachable on a row that
is not drawn. `session_gone` has no pane to paint and nothing a respawn
could land in, so it moves to the recent list, where a restart is a
resume. Existence is read BEFORE the pane, so a stale `dead` in the bulk
status map can never keep a row alive for a session tmux no longer has.

The startup gate reads a `pane_dead` session as `ready` - the narrow claim
"not blocked on a startup prompt", which is true of a corpse - raises no
toast for it, and captures no scrollback, so a dead row costs nothing per
poll.

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

**Hook events are unordered, duplicated and droppable.** Every consumer in
`session_activity.py` is idempotent: last-write-wins booleans, counters
floored at zero (`subagent_depth = max(0, depth - 1)`), and an unknown
event kind is a documented no-op rather than an error. Applying the same
event twice, or two events in the wrong order, converges on the same state
a correctly-ordered stream would reach.

**A CLOSING EVENT IS NOT A HEARTBEAT ON ITS OWN**, and that was punchlist
item 4. Measured on claude 2.1.265 by `tests/test_led_real_hooks.py`,
twice: on a turn with **no subagent anywhere in it**, `SubagentStop`
arrives about 1.5s AFTER `Stop` (Stop+38.96s, SubagentStop+40.46s).
`Stop` had just cleared `last_tool_event_ts` to say the turn was over and
`SubagentStop` stamped it again, so the heartbeat re-armed and a finished
session painted `working` for the full 120s. `finished_unread` was visible
for about a second and a half and **`idle` was unreachable in between** -
the light claiming work nothing can see, this time through the hook
stream rather than through the tmux fallback that was fixed for the same
lie. The punchlist recorded it as "activity reads working for minutes
after a resume".

The rule: an event that CLOSES something stamps the heartbeat only when
something was open for it to close. `SubagentStop` needs
`subagent_depth > 0`, which is already the exact record of an unmatched
`SubagentStart`; at zero it decrements nothing, stamps nothing, moves no
state and logs `subagent_stop_without_start` at debug. `PostToolUse` has
no counter (parallel tool calls and a droppable `PreToolUse` would
desynchronise one), so it keys on a `turn_open` boolean that every
OPENING event (`UserPromptSubmit`, `PreToolUse`, `SubagentStart`) sets and
`Stop` clears. Opening events still stamp unconditionally - there is
nothing they could be late for.

**The refusal is narrow, which is what makes it a measurement.**
`PostToolUse` is refused ONLY when a `Stop` has POSITIVELY been seen for
this session and no opening event has landed since. Never having seen a
`Stop` - a fresh session, a server restarted mid-turn - is not evidence
the turn ended, so that case still stamps. The remaining hole is a turn
whose `UserPromptSubmit` AND `PreToolUse` were both dropped, leaving only
a `PostToolUse`: it costs one under-claimed `working`, corrected by the
next opening event. Under-claiming is the safe direction.

**A missing `Stop` is handled by a timeout, not by detection.** A
tool-use heartbeat is trusted for `WORKING_HEARTBEAT_TIMEOUT_SECONDS`
(120s). Past that, no `working` is claimed. 120s is longer than a
realistic single tool call (a slow fetch, a long test run) so the light
does not flicker mid-turn, and short enough that a dead process does not
read as busy for the rest of the day.

**A persisted state is judged by its age.** `src/core/activity_persist.py`
stamps every write and refuses a perishable state (`working`,
`working_subagent`, `question`) older than the live timeout plus a small
grace. A stale `working` is a lie about right now, so it returns
not-measured rather than the stored value and rather than `idle`.

## The tmux fallback, and the claim it is not allowed to make

A session with NO hook signal at all falls back to tmux's pane
classification. That tier maps as follows, and the first row is the one
that changed:

| tmux says | means | maps to |
|---|---|---|
| `running` | the foreground command is not a bare shell | `unknown` |
| `idle` | the foreground command is a bare shell | `idle`, or `finished_unread` when unread |
| `dead` | the pane's process exited | `dead` |
| `unknown` | the query failed | `unknown` |

`running` used to map to `working`, and that was a claim tmux cannot
support. It means only "some non-shell process is in the foreground",
which is equally true of an agent mid-tool-call and one parked at an empty
prompt. The fallback carries no timestamp either, so unlike the hook tier
nothing could ever expire the claim.

**Measured 2026-09-08 on the reference box:** of 19 live sessions, 15
report a claude VERSION STRING as `pane_current_command` (`2.1.259`,
`2.1.261`, `2.1.263` - the binary renames its own process) and only 4
report `zsh`. So this branch is the common case, not the exotic one an
older note in `session_status.py` assumed, and every one of those 15
sessions was reporting a permanent, unexpiring `working` on no evidence.
That is the stale `working` recorded on the punchlist as lasting minutes
after a resume: it did not last minutes, it lasted until a hook arrived to
overrule it.

## Unread

**Set** on `Stop` (the `auto` flag), and by the user's explicit control
(the `manual` flag). A session is unread if either is set.

**Cleared** when a WS terminal actually binds to the session
(`SessionManager.mark_session_viewed`) - the strongest "the user is
looking at this" signal the server has, deliberately stronger than merely
appearing in a poll response. That clears `auto` only. A conversation the
user pinned unread for followup stays flagged after they open it, until
they clear it themselves.

**Stored** server-side, not in `localStorage`, because the user drives
this from a phone and a desktop and the flag has to follow them.

### The key is the INSTANCE

`src/core/unread_store.py` keys on `<tmux_name>@<#{session_created}>`.

Keyed on the tmux NAME alone, a flag set on a killed session reappeared on
the next session to take that name, pointing the user at a `Stop` that
happened in a conversation that no longer exists. Keyed on `session_id` it
would not survive a detach. The creation epoch is what separates two
instances of one name, and it is the same identity rule
`sessions.tmux_created_epoch` uses throughout `src/core` - reused, not
re-invented.

A restart in place keeps the flag: `respawn-pane -k` replaces the pane's
PROCESS, while `#{session_created}` belongs to the SESSION and does not
move (measured on tmux 3.7c). So the flag survives exactly the operation
the user thinks of as "restart" and is dropped exactly when the session is
genuinely a new one.

An epoch that could not be measured is an UNKNOWN instance, not a new one.
It composes to the bare name, which is also the legacy key shape, so an
unmeasurable epoch degrades to the old behaviour instead of minting a
second entry. A store written before the re-key still answers, and
migrates to the composite key on the next write rather than being
duplicated.

## The LED: two independent dimensions

`client/js/status-led.js` and `client/css/status-led.css`.

One flat dot had to answer two questions at once - what is the chat doing,
and does it want my attention - and could not. `finished_unread` exists as
a whole extra state only to say "done, and also unread", and there was no
way at all to say "working, and also unread". Two rings say both.

**Inner dot** (`data-inner`), the chat's own status:
`working`, `waiting-permission`, `waiting-input`, `done`, `dead`,
`unknown`.

**Outer halo** (`data-outer`), activity and attention:
`active` (breathing), `steady` (lit, still), `unread` (its own hue,
breathing), `off` (dead, no halo at all), `dim` (not measured).

They are set separately and every combination renders. No rule in the
stylesheet reads one to decide the other.

### The mapping

`StatusLed.ledStateFor({activity_status, unread, startup_gate})` is the
ONE place the server vocabulary becomes a pair of rings.

| activity_status | startup_gate | unread | inner | outer |
|---|---|---|---|---|
| `dead` / `stopped` | any | any | `dead` | `off` |
| any | `awaiting_startup_prompt` | any | `waiting-input` | `active` |
| `question` | any | any | `waiting-permission` | `active` |
| `notice` | any | any | `waiting-input` | `active` |
| `working` / `working_subagent` / `running` | any | no | `working` | `active` |
| `working` / `working_subagent` / `running` | any | yes | `working` | `unread` |
| `finished_unread` | any | any | `done` | `unread` |
| `idle` | any | no | `done` | `steady` |
| `idle` | any | yes | `done` | `unread` |
| `unknown` / absent / unrecognised | any | no | `unknown` | `dim` |
| `unknown` / absent / unrecognised | any | yes | `unknown` | `unread` |

Order matters: `dead` outranks everything (an unread flag must not paint a
corpse as something to go and read), then anything blocking on the user,
then activity. `unread` rides the halo independently of all of it.

Both inner waiting states are reachable from live data as of 2026-09-08.
`waiting-permission` is `question` and nothing else - the agent is
stopped. `waiting-input` is `notice` OR the startup gate, which is the
right pairing: both mean "come and look", neither means "approve this".

`waiting-permission` has its own hue, `--led-color-permission`, resolving
to the existing `--color-status-pending` (`#ffa500`). It sits between the
terracotta `--color-accent` that `waiting-input` takes (`#d77757`) and
the red `--color-danger` that `dead` takes (`#ff4444`): hotter than "come
and look", and deliberately NOT a red, because a blocked session is not a
dead one and the two lights must never be confusable at a glance. An
existing palette token was chosen over a new value so no theme has to
learn one.

### Motion

`active` and `unread` breathe on a 2s ease-in-out cycle, opacity and scale
together, on the HALO only - the dot itself never animates, so the state
colour stays at full strength at every point in the cycle. `steady` is lit
and still. `off` has no halo. Under
`prefers-reduced-motion: reduce` the glow stays and the pulse stops; the
active/resting distinction moves entirely into opacity.

The six state colours plus the unread hue are named tokens declared
exactly once, at the top of `status-led.css`. A theme that wants a
different palette redefines `--led-color-*`, never these rules.

### Sizing

`--led-size` is 9px by default, and every call site in this app actually
renders at that default: the sidebar row (`session-sidebar-rows.js`) and
the launchpad card (`launchpad.js`) both call `dotHtml()` with no `size`,
so neither passes a per-instance override. `--led-halo-scale` (1.3) and
`--led-glow-spread` (a fixed `1.5px`, not a fraction of the dot - a flat
pixel value reads truer than a proportional one at this size) size the
halo off that one dot size; at the 9px default the whole lit object -
halo ring plus glow, at the breathing peak - is about 14.7px across:
9 * 1.3 = 11.7px halo, plus 2 * 1.5 = 3px of glow.

That is the owner's own calibration (2026-09-08): "glowing is still to
big. like 1 or 2 px larger than the front circle" - the halo ring itself
reads as only a couple of px bigger than the dot, with the glow adding a
further 1-2px on top. Two earlier configs are worth knowing if you are
tracing a regression: 1.7x scale / 0.3x spread (shipped earlier the same
day) put the lit object at about 21px across, still visibly larger than
"1 or 2px more"; before that, 2.6x scale / 0.62x spread put it at about
35px across at the peak - larger than the row text itself and overlapping
neighbours on the compact sidebar density and on the launchpad cards,
which is what the owner meant by "the breathing is way too big" the first
time. The breathing keyframes scale the halo between 0.92 and 1 - never
past its own resting size - so the geometry tokens above are the true
maximum rather than a floor the animation overshoots.

There is one set of geometry tokens, not one per surface, because every
surface that renders a LED today renders it at the same 9px size. A
surface that needs a different size passes `size` to `ledHtml()` (see
`client/js/status-led.js`), which scales `--led-size` and, through it,
the halo and glow with it - a second geometry override is only warranted
if a surface ships at a different base size.

### Rolling a group up

`client/js/session-status-summary.js` folds a set of sessions into one LED
plus an unread count. Priority: **permission > input > working > unread >
done > dead > unknown**.

`permission` is a session stopped on a yes/no; `input` is one that wants
the user's eyes (a `notice`, or a startup prompt nobody has answered)
without being stopped. Permission leads because it is the only bucket
guaranteed to make no progress at all until a human acts - a header that
hoisted a chatty notification over a parked session would point the user
at the wrong row. Both outrank working because they are about the user
and will stay that way; working resolves on its own. Dead sits BELOW done deliberately -
a group with one corpse and nine busy sessions must not read as dead. An
EMPTY group is `unknown`, not `done`: nothing to measure is not the same
as measured-and-quiet.

Each child is bucketed from the LED state it already resolved to, not from
its raw `activity_status`, so a header cannot disagree with the rows under
it.

## A silent degradation worth knowing about

A hook POST with a stale token is rejected at
`src/api/routes.py` and logged as `hook_post_rejected_invalid_token`.
Nothing surfaces it to the user. The session then has no hook signal at
all, falls into the tmux tier above, and its light goes quiet - which
looks exactly like a session that is genuinely idle.

Measured 2026-09-08 in the live server log: 4,325 rejections from
`adopted:cloude_Agent_-_Cloude_Code` and 364 from `ses_68c185ce`, whose
run was on 2026-08-28 and whose pane is long gone.

**The cause was a MINT LANDING ON A RUNNING AGENT, and it is now
recoverable.** `_mint_hook_token` REPLACES the token held for an id. The
same value was baked into the pane's environment at `new-session` time
and is read from there at hook-fire time, so it is fixed for the life of
that process and cannot be re-issued to it: the agent keeps presenting a
credential the store has moved on from, with no retry and no error
surface. Traced to the millisecond - an adopt registered the live pane
under a derived id and minted at 16:16:40.633984Z, and the first
rejection for that id was logged at 16:16:40.763005Z, 130 ms later. The
same id had been ACCEPTED minutes earlier (`toast_recorded` 16:11:28Z,
16:12:56Z), which is what makes this a rotation rather than a
misconfiguration. It ran for 4h24m and ended only when the owner
restarted the pane by hand at 20:40:23Z, so a new process inherited the
current environment.

Two changes close it. `src/core/hook_token_recovery.py` keeps a bounded,
in-memory ring of tokens this process minted and then superseded; when
the route's ordinary validation rejects, `recover_hook_token` accepts a
token ONLY if this server minted it for THAT id on THAT pane and
replaced it, re-binds the store to the value the running process holds,
logs `hook_token_rebound_from_superseded` once, and NEVER mints. A token
matching nothing still rejects, and so does one superseded on a
different pane. Separately, the respawn path and the boot re-adopt now
push the current control variables onto the pane's session environment
BEFORE a new process starts, because tmux copies that environment at
spawn - a write afterwards reaches the next restart instead of this one.

Two bounds worth stating. The ring is in memory only, so a mint followed
by a server restart is not recoverable this way (the restart has its own
answer: the store is reloaded and the boot re-adopt re-keys the pane to
the id its agent presents). And the SURFACING gap is still open: nothing
tells the user that a session's hooks are being rejected, so one that
stays broken still falls to the tmux tier and looks calm. The honest
treatment there is `unknown` with a reason.
