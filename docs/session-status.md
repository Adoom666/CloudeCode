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

**ONE TOAST CARD PER SESSION, and it shows the same thing the LED would.**
Until 2026-09-09 the toast stack coalesced on (kind, session), so one
session produced one card per kind: a "wants your attention" card AND a
"Your turn" card, side by side, about the same session. Measured on the
owner's screen that day, four cards for two sessions plus a "Dismiss all
(9)" row. `client/js/toast.js` now keys the group on the SESSION alone and
`client/js/toast-session-group.js` picks which pending event that card
shows, by READING `SUMMARY_PRIORITY` out of
`client/js/session-status-summary.js` - the same fold, in the same order,
that the sidebar group headers and the launchpad top bar already use.
There is no second ranking; the join is only from a hook event name to
one of that fold's buckets:

| toast kind | bucket | why |
|---|---|---|
| `PermissionRequest` | `permission` | Claude is stopped on a yes/no |
| `StartupPrompt` | `input` | parked on a startup prompt; the same bucket its `waiting-input` LED folds into |
| `Notification` | `input` | wants a look, is not blocked; matches where the `notice` LED buckets |
| `Stop` | `unread` | "your turn": a finished turn nobody has looked at |
| anything else | `input` | an unknown kind is treated exactly as a Notification, never as the least interesting thing |

Severity breaks a tie INSIDE a bucket, so a `StartupPrompt` keeps the card
from a chatty `Notification` and with it the severity-3 cap exemption and
`role="alert"`.

**THE CARD UPGRADES AND CANNOT DOWNGRADE, because the pick is a fold and
not a variable.** A permission prompt landing on a session already showing
"your turn" re-answers the fold on the SAME group key, so the same card
element becomes the permission card; a `Stop` landing on a session showing
a permission prompt changes nothing. Hook events arrive unordered,
duplicated and droppable, and a fold over what is currently held is
idempotent against all three - a running "current worst" variable would
not be.

**THE BADGE COUNTS THE KIND ON THE CARD, NOT THE PILE.** `×5` sits against
the winner's title, so it is read as "this sentence, five times"; a
session holding one permission prompt and six finished turns must not
paint "needs your permission ×7". How many records the x will actually
clear is a different number and the dismiss control states it in words
("Dismiss 7 notifications for this session"). For the same reason the
"Dismiss all" disclosure and the overflow row's worst-severity label count
RECORDS at that severity, never whole groups.

**The attachment receipt is deliberately NOT in this grouping.** It is a
browser-raised card describing what is staged in the prompt buffer, with
no server record, retired by the prompt being SENT rather than by the user
showing up. It keeps its own card, still coalesced per session, so a
session can show one status card and one receipt.

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

**There is no longer a client control for the `manual` flag.** The unread
envelope was removed from the sidebar and the launchpad on 2026-09-08 -
see "The envelope is gone" below. `PATCH /sessions/{name}/unread` still
exists and still works; nothing in the UI calls it.

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
`working`, `waiting-permission`, `waiting-input`, `notice`, `done`,
`dead`, `disconnected`, `unknown`.

**Outer halo** (`data-outer`), activity and attention:
`active` (breathing), `steady` (lit, still), `unread` (a crisp, still
green ring), `off` (dead, no halo at all), `dim` (not measured).

They are set separately and every combination renders. No rule in the
stylesheet reads one to decide the other.

### Five colours, eight states

Asked for on 2026-09-08, in the owner's words: "if the session is fully
stopped waiting for a response, then yellow. if it's still working but
needs something from me, make it light blue", over "red if the connection
is disconnected, grey if the session is idle, green if there is activity",
plus "finished turn waiting on me to look at should be a green outline and
grey filled dot". The grey fill was withdrawn on 2026-09-09 - see the
cleared centre below - and the quote is left whole because the ask it
records is still the ask.

The eight inner state NAMES stay eight. Only the paint collapses onto
five hues, and the accessible label still says which state it is, because
colour was never allowed to be the only signal here.

| colour | states | token |
|---|---|---|
| green | `working`, `working_subagent` | `--led-color-working` -> `--color-success` |
| yellow | `question`, `awaiting_startup_prompt` | `--led-color-permission` / `--led-color-waiting` -> `--color-warning` |
| light blue | `notice` | `--led-color-notice` -> `--color-info` |
| grey | `idle`, `unknown` | `--led-color-idle` / `--led-color-unknown` -> `--color-fg-muted` |
| red | `dead`, transport disconnected | `--led-color-dead` / `--led-color-disconnected` -> `--color-danger` |
| green ring, cleared centre | `finished_unread` | `--led-color-unread` ring, `--led-fill: transparent` |

Four things about that table are load-bearing.

**Yellow is STOPPED, light blue is NOT.** `question` is a
`PermissionRequest` that halted the agent mid-turn; the startup gate is a
pane parked on the folder-trust dialog. Both are fully stopped and the
user's answer to both is the same, so they paint the same yellow and keep
separate names and separate labels. `notice` is a `Notification`: the
agent is still working and merely wants a look. That is the
`question` / `notice` split of earlier the same day, now visible on the
light rather than only in the data.

**Light blue has to survive red-green colourblindness.** Under both
protanopia and deuteranopia the green (`#4ade80`) desaturates toward a
pale yellow-khaki while a blue at this wavelength (`#4fc1ff`) stays
plainly blue. A third warm hue would have failed that.

**Grey at rest and grey unmeasured are told apart by SHAPE.** `idle` and
`unknown` take the same hue on purpose - neither is interesting to look at
and neither may be dressed up as a measured healthy state - and `unknown`
is drawn hollow. Colour would have ranked them; shape does not.

**THE TWO HOLLOW LIGHTS SHARE ONE RECIPE.** `unknown` and the
finished-turn ring both clear the centre of the dot so the row background
shows through, and they do it in a single rule naming both states
(`--led-fill: transparent` in `status-led.css`). That is the owner's
2026-09-09 correction: the ring shipped with a mid-grey FILLED centre and
read as two lights stacked, and the ask was "it should look like the
'status not measured' dot, but the outline should be green instead of
light grey with the dark grey center". Two copies of "clear the middle"
would be free to drift into one state showing the real background and the
other showing a grey somebody picked, so the count of that declaration is
asserted in `tests/test_status_led.node.mjs`. The two are told apart by
HUE and by WHERE THE BAND SITS - a grey 2px rim on the 9px dot for
`unknown`, a green 2.5px band on the 15.3px halo box for the ring - never
by the centre.

The permission orange this replaced (`--color-status-pending`, `#ffa500`)
sat too close to the red the dead light takes. At nine pixels an orange
and a red in the same list read as one colour.

### The envelope is gone

`finished_unread` used to be carried by an unread ENVELOPE ICON beside the
row name on the sidebar and the launchpad, which doubled as the manual
mark-unread control. Both were removed on 2026-09-08 and the green ring is
what says it now. Unread TRACKING is untouched: `src/core/unread_store.py`
still keys on the instance, `Stop` still sets it, binding a WS terminal
still clears it, and `PATCH /sessions/{name}/unread` still exists. Only
the client control went, along with its click and keyboard handlers in
`launchpad.js`, `session-sidebar-clicks.js` and `session-sidebar.js`, and
its CSS. Nothing carries `data-row-unread` any more, because nothing
reads it.

### The mapping

`StatusLed.ledStateFor({activity_status, unread, startup_gate, transport})`
is the ONE place the server vocabulary becomes a pair of rings.

| transport | activity_status | startup_gate | unread | inner | outer |
|---|---|---|---|---|---|
| `disconnected` | any | any | any | `disconnected` | `off` |
| other | `dead` / `stopped` | any | any | `dead` | `off` |
| other | any | `awaiting_startup_prompt` | any | `waiting-input` | `active` |
| other | `question` | any | any | `waiting-permission` | `active` |
| other | `notice` | any | any | `notice` | `active` |
| other | `working` / `working_subagent` / `running` | any | any | `working` | `active` |
| other | `finished_unread` | any | any | `done` | `unread` |
| other | `idle` | any | no | `done` | `steady` |
| other | `idle` | any | yes | `done` | `unread` |
| other | `unknown` / absent / unrecognised | any | any | `unknown` | `dim` |

Order matters. A dead TRANSPORT outranks everything: nothing we are
showing is fresh once the socket is down, so the light may not keep
asserting the last status it happened to see. Then `dead` (an unread flag
must not paint a corpse as something to go and read), then anything
blocking on the user, then activity.

Two rows changed with the five-colour pass and both are deliberate.
**A working session is solid green whatever its unread flag says** - the
unread halo is now the green finished-turn ring, and a ring claiming a
turn ended, around a session that is mid-turn, is two contradictory claims
on one light. **`unknown` never takes the ring either**, for the same
reason in its stronger form: the ring asserts that a turn FINISHED here,
and nothing was measured.

### Transport: the one signal the server cannot report

`client/js/session-transport.js`. Every other signal here is a fact about
the session, measured on the Mac and shipped down `/sessions/list`.
"Disconnected" is a fact about the BROWSER: the WebSocket in
`client/js/terminal.js` closed. It is written from `ws.onopen` and
`ws.onclose` and read by `session-sidebar-rows.js` and `launchpad.js` on
their way into `dotHtml`.

This browser holds a socket to at most ONE session, so **every other
session answers `unknown`, never `connected` and never `disconnected`**. A
sidebar full of red because one socket dropped would be exactly the
fabricated-measurement mistake this whole model exists to avoid. A
DELIBERATE close clears the record rather than marking it disconnected:
"you left it" and "we lost it" are different facts and only one is worth
painting red.

`dead` and `disconnected` share the red, so the LABEL is the only thing
separating them, and the two must never be paraphrases: "dead - the
process exited" against "disconnected - no live connection to this
session".

### Motion

`active` breathes on a 2s ease-in-out cycle, opacity and scale together,
on the HALO only - the dot itself never animates, so the state colour
stays at full strength at every point in the cycle. `steady` is lit and
still. `off` has no halo. Under `prefers-reduced-motion: reduce` the glow
stays and the pulse stops; the active/resting distinction moves entirely
into opacity.

`unread` DOES NOT BREATHE since the five-colour pass. It is the
finished-turn ring, and an outline that pulses stops reading as an outline
at nine pixels. Motion is therefore a signal in its own right now: a light
that moves is a session that is moving.

**It is DRAWN AS A RING, NOT AS A DISC, and that is not a style
preference.** The halo pseudo-element carries `z-index: -1`, which inside
the element's own stacking context paints it ABOVE the element's
background - and the element's background IS the dot. Every other halo
gets away with that because it is a wash at 0.18 to 0.55 opacity, so the
dot reads straight through it. An OPAQUE disc at the same z-index hides
the dot completely: measured in a 6x render, `finished_unread` came out a
solid green blob with no grey in it at all. So the `::after` drops its
fill and draws the band with an inset shadow instead, leaving the middle
clear.

**AND THE DOT UNDER IT IS CLEARED TOO, since 2026-09-09.** The first
version left the grey `done` dot filled inside the band, which the owner
rejected. `--led-fill: transparent` now removes it, so what shows in the
middle is the row background rather than a second light - the `unknown`
dot's construction in a different hue. The inner state is still `done`
and still resolves to the grey ink; only the paint of the centre changed,
so nothing in the state machine or the summary fold moved.

Geometry: `--led-lit-scale` 1.7 with a `--led-ring-width` of 2.5px. At
the 9px default that is a 15.3px lit object and an unmistakable 2.5px of
green. **This ring is what sets the size for every other state** - see
Sizing below. It used to override the halo scale in its own block; it
must not do that again. Measured on a real render at 8x device scale,
before and after the cleared centre, the painted extent was IDENTICAL to
the hundredth of a pixel in all nine states: 15.75px for the four
breathing ones, 16.00 for the ring, 15.62 for `steady`, 15.38 for `dim`,
and 9.00 for the two `off` states, which carry no halo at all by design.
Clearing a fill moves paint, not geometry.

Every state colour is a named token declared exactly once, at the top of
`status-led.css`, and every one of them defers to a palette token that all
of `client/css/themes` already declares. A theme that wants a different
palette redefines `--led-color-*`, never these rules.

### Sizing

**ONE LIT DIAMETER FOR EVERY STATE.** `--led-size` (9px) is the dot and
`--led-lit-scale` (1.7) multiplies it into the halo box, so everything
the component paints in any state fits inside one 15.3px circle. States
differ in colour, opacity and fill. They never differ in size. Both
tokens are declared once, on `.status-led`, and **no `[data-inner]` or
`[data-outer]` rule may override either**.

Every call site renders at the 9px default: the sidebar row
(`session-sidebar-rows.js`) and the launchpad card (`launchpad.js`) both
call `dotHtml()` with no `size`. A surface that needs a different size
passes `size` to `ledHtml()`, which scales `--led-size` and the halo with
it.

**Why that had to be written down.** Until 2026-09-09 the halo was sized
per state AND drawn partly outside its own box, so the LIT object came
out at three different diameters while the ELEMENT box measured 9px in
every one of them - which is exactly why no test caught it:

| state | halo box | painted outside it | what a reader sees |
|---|---|---|---|
| `active` | 11.69px | a 1.5px spread glow | about 14.7px, saturated |
| `unread` | 15.30px (own override) | nothing | 15.3px ring |
| `steady` | 11.69px | a glow at 0.30 opacity | 9px - grey on a grey dot |
| `dim` | 11.69px | a glow at 0.18 opacity | 9px |
| `off` | 11.69px | nothing, opacity 0 | 9px |

The bottom three are invisible on a real row, so in a sidebar where one
session is working and the rest are at rest, that one dot read about 60
percent wider than its neighbours. That is what the owner reported.

**The glow is a radial gradient, not a spread box-shadow, and that is the
load-bearing half of the fix.** A spread shadow paints beyond the element
it sits on by definition, so it can never be held to a declared diameter.
A gradient fades out AT the box edge, so the halo's painted extent IS its
box and is measurable. `--led-halo-core` (55 percent) is how far out the
halo stays fully opaque before it fades: at 55 percent of 15.3px that is
an 8.4px core, just inside the 9px dot, so the only thing outside the dot
is falloff. That is the owner's 2026-09-08 calibration ("glowing is still
to big. like 1 or 2 px larger than the front circle") expressed as a
shape rather than as a smaller number.

Earlier configurations, if you are tracing a regression: 1.3x halo plus a
1.5px spread glow put the lit object at about 14.7px with a hard-edged
11.7px core, so a working session read as a wider dot rather than a lit
one; 1.7x/0.3x put it at about 21px; the original 2.6x/0.62x put it at
about 35px, larger than the row text itself. The breathing keyframes
scale the halo between 0.92 and 1, never past its resting size, so the
tokens are the true maximum rather than a floor the animation overshoots.

`scripts/verify_status_led_geometry.py` measures all forty (inner, outer)
pairs in a real Chromium, across three themes and two viewports, and
fails if two of them differ or if anything paints outside its box. A CSS
read cannot do that job: the divergence was in what the box RESOLVES to
once a per-state override and a pseudo-element's own shadow are composed.

### Rolling a group up

`client/js/session-status-summary.js` folds a set of sessions into one
LED. Priority: **permission > input > working > unread > done > dead >
unknown**.

**The roll-up IS the row component.** `summaryHtml` picks an (inner,
outer) pair and hands it to `StatusLed.ledHtml`, the same builder every
row uses, so a header takes every treatment a row takes - including the
green ring around a cleared centre for a finished turn nobody has read. It
is not a header-shaped dot, and building one would be how the two come to
disagree.

**There is no numeric unread badge beside it.** A yellow `(n)` pill used
to carry the unread count on every group header; it was removed on
2026-09-09 at the owner's request ("to be clear remove the yello (1)")
and nothing replaced it. The ring already says there is something here
for the user, and two indicators for one fact is how two indicators end
up disagreeing. `summarizeStates` still RETURNS `unreadCount`, which is a
measured property of the fold; nothing renders it. The plain count pill
on the header is a different control - it says how many conversations a
folded section is hiding - and stays.

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

**The `input` bucket holds two hues and its RANK did not move.** Since the
five-colour pass, `waiting-input` is yellow (stopped on a startup prompt)
and `notice` is light blue (still working). They stay in one bucket -
that priority is the product decision - but the header has to paint one of
them, so it paints yellow if any member is stopped and light blue when
every member is a notice. A header that painted the stopped yellow over a
group holding nothing stopped would be claiming something nobody measured,
and a header that disagreed with its only child is a bug the suite guards.

`disconnected` buckets with `dead`: they paint the same red and rank the
same way. No group feeds one in today - children come from a REST listing,
which has no socket.

### The key, and why the lights finally have words

`client/js/session-status-key.js` renders a foldable legend at the foot
of the session sidebar. Collapsed by default; the fold rides
`cloude.statusKey.open` in localStorage, the same `cloude.*` convention
the pin and the density preference use, and an unreadable or absent value
means collapsed rather than an error.

It exists because the LED is now the ONLY thing on a row saying what a
session is doing - the envelope is gone and there is no text badge beside
it - and its meaning lives in a `title` nobody hovers on a phone.

**Every swatch is a real LED.** `itemHtml` calls `StatusLed.ledHtml` with
the same (inner, outer) pair the rows resolve to, so the key cannot show
a colour, a size or a shape the app does not paint. A hand-drawn legend
would be a second implementation of the component, and this project has
already paid for two stylesheets drawing one dot.

**SEVEN ROWS, ONE PER LIGHT.** It carried nine until 2026-09-09, one per
inner state, and the owner asked for "one entry per colour": two rows
showed the same yellow and two showed the same red, which sends a reader
looking up a dot on their screen hunting for a difference the light
cannot show them. The rows, top to bottom, in the same urgency order the
group-header fold uses:

| light | row |
|---|---|
| yellow | stopped, waiting on you |
| light blue | still working, but needs your attention |
| green | working |
| green ring | done, unread |
| grey | idle |
| red | dead / disconnected session |
| grey outline | not measured - nothing reported in, so this is not idle |

Green and grey each appear twice and that is not a breach of the rule:
a solid dot and an outline are two different things on screen, which is
exactly what the last row exists to explain. **THE STATE MACHINE DID NOT
CHANGE.** There are still eight inner states, `waiting-permission` and
`waiting-input` still paint one yellow, `dead` and `disconnected` still
paint one red, and the component's own `title` and `aria-label` still say
WHICH of each pair a given dot is. Collapsing the rows made those labels
load-bearing rather than decorative, so
`tests/test_status_key.node.mjs` now pins that the collapsed pairs really
do resolve to one colour in the stylesheet AND that their words still
differ. It also asserts the count, that every hue the component can paint
has a row, and that no two rows draw the same light.

It sits where the sidebar's "N remembered positions are held for sessions
not currently listed" note used to, removed the same day: it named
bookkeeping no reader could act on. **The remembered positions themselves
are untouched** - `session-sidebar-arrangement.js` still keeps those
slots, they still reach the repaint signature, and
`session-sidebar.js` still stamps the count on the list element as
`data-order-missing`. Only the sentence went.

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
