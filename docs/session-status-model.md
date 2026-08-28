# The session status model

Derived from source on branch `fix/ended-sessions-visibility` (base tag
`v1.0.5`, `6240396`). Every state and every transition below cites the file and
symbol it was read out of. If a citation and this prose disagree, the citation
wins - and `tests/test_status_model_chart_drift.py` fails the build when the
state names here and the constants in the code stop matching.

## There are FOUR state machines, not one

They are independent. A session has a value in all four at once, and no value
in any one of them determines a value in another.

| # | Axis | Where it lives | Who writes it | Survives a restart |
|---|------|----------------|---------------|--------------------|
| 1 | **Activity** | in-memory, recomputed per request | `src/core/session_activity.py::SessionActivityTracker.resolve` | no |
| 2 | **Lifecycle** | `sessions.lifecycle` column | `src/core/session_lifecycle.py::reconcile_from_listing` and the create/adopt/import writers | yes |
| 3 | **Origin** | `sessions.origin` column | `src/core/session_identity.py::claim_instance`, the create path, the import path | yes |
| 4 | **Deleted** | `sessions.archived_at` column | `src/core/session_store.py::archive_session` - the ONLY writer | yes |

A fifth thing, the **tray**, is not a session state at all. It is a derived
view over a *set* of sessions plus server health. Chart 6 shows that
relationship rather than listing its states beside the others.

The single most useful sentence in this document: **`dead` and `stopped` are
not the same thing and are not even on the same axis.** `dead` (activity) is a
tmux session that still EXISTS, holding a pane whose process exited - you can
attach to it, respawn into it, kill it. `stopped` (lifecycle) is a tmux
instance that is GONE - there is nothing to attach to and nothing to kill, only
a stored row. See `client/js/session-status-ui.js` `STATUS_LABELS.stopped`,
which says so in the label text itself.

---

## Chart 1 - Activity, the tmux-only classifier

The pure function. No hooks, no I/O, no persistence.
Source: `src/core/session_status.py::resolve_pane_status`.

```mermaid
flowchart TD
    Q["tmux pane query<br/>pane_dead / pane_current_command<br/>(session_status.resolve_pane_status)"]

    Q -->|"either value is None"| unknown
    Q -->|"pane_dead == '1'"| dead
    Q -->|"alive, command empty"| unknown
    Q -->|"alive, command in KNOWN_SHELL_COMMANDS"| idle
    Q -->|"alive, any other command"| running

    dead["dead<br/>STATUS_DEAD"]
    running["running<br/>STATUS_RUNNING"]
    idle["idle<br/>STATUS_IDLE"]
    unknown["unknown<br/>STATUS_UNKNOWN"]
```

These four are `ALL_STATUSES` (`session_status.py:58`).
`KNOWN_SHELL_COMMANDS` is `session_status.py:134` - `zsh bash sh dash ksh fish
tcsh csh`.

**What the code admits it cannot do.** The module docstring
(`session_status.py:24-33`) refuses to derive "waiting for user input" from
tmux, because a pane query cannot tell thinking from blocked. That refusal is
why chart 2 exists.

---

## Chart 2 - Activity, the unified hook + tmux vocabulary

Source: `src/core/session_activity.py::SessionActivityTracker.resolve`.
Every state string is still defined in `session_status.py` - `session_activity`
owns the machine and no literals (`session_status.py:63-67`).

```mermaid
flowchart TD
    R["resolve(session_id, tmux_status, unread, now)"]

    R -->|"tmux_status == STATUS_DEAD"| dead
    R -->|"no signal, or hook_seen False"| FB["map_tmux_fallback<br/>(chart 3)"]
    R -->|"hook_seen, question_open"| question
    R -->|"hook_seen, heartbeat fresh, subagent_depth > 0"| wsub
    R -->|"hook_seen, heartbeat fresh, depth 0"| working
    R -->|"heartbeat stale, unread"| fin
    R -->|"heartbeat stale, not unread, tmux unknown"| unknown
    R -->|"heartbeat stale, not unread, otherwise"| idle

    dead["dead"]
    question["question"]
    wsub["working_subagent"]
    working["working"]
    fin["finished_unread"]
    idle["idle"]
    unknown["unknown"]
```

"Heartbeat fresh" is `now - last_tool_event_ts <=
WORKING_HEARTBEAT_TIMEOUT_SECONDS` (120s, `session_activity.py:100`).

These seven are `ALL_ACTIVITY_STATUSES` (`session_status.py:103`).
`ACTIVITY_STATUS_PRIORITY` (`session_status.py:120`) lists the same seven in
urgency order and is documented as consulted by no resolver in this codebase.

**`running` is in `ALL_STATUSES` and NOT in `ALL_ACTIVITY_STATUSES`.** That is
deliberate and documented at `session_status.py:98-102`: a raw tmux `running`
is mapped onto `working` before it ever reaches a client.

### Chart 2b - what moves the hook signal

Source: `src/core/session_activity.py::SessionActivityTracker.record_event`,
event constants `session_activity.py:60-67`, membership `KNOWN_EVENTS:73`.

```mermaid
flowchart LR
    N["Notification /<br/>PermissionRequest"] -->|"question_open = True"| S(("signal"))
    U["UserPromptSubmit"] -->|"question_open = False"| S
    PRE["PreToolUse"] -->|"question_open = False<br/>last_tool_event_ts = now"| S
    POST["PostToolUse"] -->|"last_tool_event_ts = now"| S
    SS["SubagentStart"] -->|"depth += 1, ts = now"| S
    SE["SubagentStop"] -->|"depth = max(0, depth-1), ts = now"| S
    ST["Stop"] -->|"question False, depth 0,<br/>ts = None, last_stop_ts = now"| S
    X["any unknown kind"] -->|"ignored, no-op"| S
```

Every update is idempotent last-write-wins on a boolean or a floored counter,
so duplicate or out-of-order hook delivery converges
(`session_activity.py:17-32`). A *missing* `Stop` is handled by the 120s
heartbeat timeout, not by the event stream.

---

## Chart 3 - the graceful-degradation map

Source: `src/core/session_activity.py::map_tmux_fallback`. Taken when a session
has never fired a hook.

```mermaid
flowchart LR
    d["tmux dead"] --> D["dead"]
    r["tmux running"] --> W["working"]
    i1["tmux idle + unread"] --> F["finished_unread"]
    i2["tmux idle, not unread"] --> I["idle"]
    u["tmux unknown"] --> U["unknown"]
```

This function never fabricates `question` or `working_subagent` - there is no
signal to base them on (`session_activity.py:110-113`).

---

## Chart 4 - Lifecycle, the durable column

Source: `sessions.lifecycle`, values `src/core/db_models.py:168-175`
(`SESSION_LIFECYCLES`). Default in the DDL is `'unknown'`
(`db_models.py::DDL_SESSIONS`).

```mermaid
stateDiagram-v2
    [*] --> running : create<br/>session_create_persist (source 'create')
    [*] --> running : adopt<br/>session_adopt_persist (source 'adopt')
    [*] --> running : import, live tier<br/>session_import (source 'import[:reason]')
    [*] --> stopped : import, stopped tier<br/>session_import (source 'import')
    [*] --> unknown : row written with no lifecycle<br/>DDL default

    running --> stopped : instance absent from a COMPLETE listing<br/>session_lifecycle.reconcile_from_listing<br/>source 'tmux_missing'
    running --> running : renamed in place<br/>session_lifecycle rename pass<br/>(name moves, lifecycle does not)

    unknown --> unknown : never promoted - see docstring<br/>session_lifecycle.py:71-79
    stopped --> stopped : already correct, not rewritten
```

**The reaper refuses in three named ways rather than guessing.** Outcome tokens
`src/core/session_lifecycle.py:112-129`:

```mermaid
flowchart TD
    P["reconcile_from_listing(listing)"]
    P -->|"sessions table missing (pre-v2)"| A["no_sessions_table<br/>a real 'nothing to do'"]
    P -->|"listing.ok is False"| B["probe_unavailable<br/>NOT a statement about any session"]
    P -->|"ok but rows refused, or a row<br/>had no readable (name, epoch)"| C["listing_incomplete<br/>a partial list cannot support an absence argument"]
    P -->|"answered AND complete"| D["evaluated<br/>stopped_uuids says what was reaped"]
```

`evaluated=False` carries no uuids and `examined=0` on every refusal branch.

`ReconcileOutcome.evaluated=False` carries no uuids and `examined=0`
(`_not_evaluated`, `session_lifecycle.py:179`). "I looked and nothing died" and
"I could not look" are different facts.

`SESSION_LIFECYCLE_SOURCE_PROBE_FAILED` (`db_models.py:179`) exists and is
**never written on purpose** - pinned by
`test_probe_failed_source_is_never_written` and explained at
`session_lifecycle.py:60-72`. Writing `unknown` on a transient tmux hiccup
would destroy a `running` value the app does believe.

### When does the reaper run

Not on a timer. `SessionManager.reconcile_lifecycle` has exactly one caller:
`SessionManager.list_attachable_sessions` (`src/core/session_manager.py:4160`),
which is the home-screen probe. The comment there says so explicitly - it runs
where the probe is already paid for.

---

## Chart 5 - Origin and Deleted

Origin: `src/core/db_models.py:145-161`. Deleted: `sessions.archived_at`,
sole writer `src/core/session_store.py::archive_session`.

```mermaid
flowchart TD
    O["observed<br/>seen on our socket, never claimed<br/>the ONLY value that badges EXTERNAL"]
    C["created<br/>the app ran tmux new-session<br/>in SESSION_OWNED_ORIGINS"]
    A["adopted<br/>the user claimed a session the app did not start<br/>in SESSION_OWNED_ORIGINS"]
    O -->|"claim_instance, session_identity.py:545<br/>origin='adopted', adopted_at=COALESCE, written ONCE"| A
    C -->|"no transition - a created session is never re-badged"| C
```

`observed` is the only value that renders as external (`db_models.py:144`).
`claim_instance` refuses a row whose `lifecycle = 'stopped'` (the SQL's
`AND lifecycle != ?` guard) - you cannot adopt a corpse.

```mermaid
flowchart LR
    live["archived_at IS NULL<br/>on the user's screens"] -->|"user presses delete - archive_session,<br/>the only writer, keyed on session_uuid<br/>never on tmux_name"| gone["archived_at set<br/>hidden from listings<br/>ROW RETAINED"]
    gone -->|"NOTHING un-archives - no code path clears the column"| gone
```

`archive_session` is idempotent and the first stamp wins
(`session_store.py:360`, `WHERE ... AND archived_at IS NULL`). The reaper
reconciles archived rows but never touches the column
(`session_lifecycle.py:81-91`), so a deleted row can never come back through
RECENT.

### The one inclusion rule

`src/core/session_store.py::listable_sessions` - "the ONE spelling of what
belongs on a screen", added because RECENT and the project tree each carried
their own and disagreed.

```mermaid
flowchart TD
    row["a sessions row"]
    row -->|"archived_at IS NOT NULL"| hide["EXCLUDED - the user said take it off my screen"]
    row -->|"parent_session_id IS NOT NULL"| hide2["EXCLUDED - a lineage row is a past conversation, not a session"]
    row -->|"lifecycle = stopped"| show["INCLUDED and rendered ENDED"]
    row -->|"lifecycle = unknown"| show2["INCLUDED - routed to NEEDS ATTENTION,<br/>never dropped"]
    row -->|"lifecycle = running"| show3["INCLUDED - the live probe owns it"]
```

`needs_attention` (`session_store.py:398`) selects
`lifecycle='unknown' OR project_attribution='unknown'`, with `archived_at IS
NULL` deliberately OUTSIDE the parenthesised OR - inside it, the attribution
arm would have kept the hole the lifecycle arm just lost.

---

## Chart 6 - the tray is a DERIVED view, not a fifth axis

Source: `macOS/tray-status.js` - `TRAY_STATES` (line 78), `deriveTrayState`
(132), `countSessionSignals` (96), `ATTENTION_STATUSES` (64).

```mermaid
flowchart TD
    SRV["server state<br/>stopped / starting / running"] --> DTS
    SESS["the session LIST<br/>each row activity_status"] --> CNT["countSessionSignals<br/>attention = status in ATTENTION_STATUSES<br/>unknown = status 'unknown' or missing"] --> DTS
    SETUP["setupStatus<br/>complete / incomplete / undetermined / null"] --> DTS
    UPD["updateStatus"] --> DTS

    DTS["deriveTrayState - precedence, most urgent first"]

    DTS --> crashed["crashed - stopped AND lastExitUnexpected"]
    DTS --> stoppedT["stopped"]
    DTS --> starting["starting"]
    DTS --> unknownT["unknown - session list unreadable, OR unknown rows > 0"]
    DTS --> attention["attention - setup not complete, OR attention rows > 0"]
    DTS --> update["update - update_available"]
    DTS --> okT["ok"]
```

`ATTENTION_STATUSES` is `['question', 'finished_unread', 'dead']` - three of
the seven activity states. `unknown` is deliberately excluded from it: a
measurement failure is not an alarm, and it routes to the `unknown` tray state
instead so it cannot masquerade as a definite one
(`tray-status.js:58-60`).

Only ONE unknown escalates the icon: an undeterminable session list. The
update check routinely cannot reach GitHub and is not allowed to pin the icon
forever - "a warning that never clears is not a monitor"
(`tray-status.js:29-38`). Nothing is dropped; `describeSignals` reports every
signal's own three-state verdict into the tooltip.

---

## Chart 7 - what tmux does to a session, and who observes it

The transitions that cross axes.

```mermaid
flowchart TD
    new["tmux new-session<br/>TmuxBackend.start"] --> doa{"dead-on-arrival probe<br/>tmux_backend.py:620-680<br/>pane_dead=='1' or pane_dead_status"}
    doa -->|"yes"| kill["capture-pane for diagnostics,<br/>kill-session, raise RuntimeError.<br/>NO row survives"]
    doa -->|"no"| liveS["live session<br/>lifecycle running, origin created"]

    liveS -->|"agent process exits.<br/>remain-on-exit ON<br/>(TmuxBackend._apply_remain_on_exit)"| corpse["tmux session EXISTS,<br/>pane_dead=1<br/>activity = dead<br/>lifecycle still running"]

    corpse -->|"tmux respawn-pane<br/>session_respawn ladder"| liveS
    corpse -->|"kill-session, or tmux server dies"| gone["instance absent from tmux"]
    liveS -->|"kill-session"| gone

    gone -->|"next COMPLETE listing<br/>reconcile_from_listing"| stoppedRow["lifecycle stopped<br/>source tmux_missing<br/>row retained"]
    gone -->|"listing not ok / incomplete"| nothing["NOTHING WRITTEN<br/>probe_unavailable / listing_incomplete"]

    liveS -->|"tmux rename-session"| renamed["rename pass matches on<br/>(epoch, tmux session_id)<br/>moves tmux_name only"]
    renamed --> liveS
```

**Why the corpse state is important.** `remain-on-exit on` is set globally
before any window exists (`tmux_backend.py:304-332`) and re-asserted per
session (`:588`) and on adopt (`:875`). It is the reason `dead` is reachable at
all: without it the session would simply vanish and the user would only ever
see `stopped`.

**Respawn's five outcomes** (`src/core/session_respawn.py:78-92`), gated on
`pane_start_command` rather than on `sessions.agent_type` - that column is
written on every create whether an agent was started or not, so trusting it
would launch an agent into a console the user believes is his own shell:

```mermaid
flowchart TD
    RP["resolve_respawn_plan(probe, agent_type)"]
    RP -->|"pane is alive"| ND["not_dead<br/>tmux itself refuses respawn-pane without -k,<br/>and this module never passes -k"]
    RP -->|"probe did not answer"| CD["cannot_determine<br/>refuses rather than guessing"]
    RP -->|"start command recorded AND agent_type known"| AG["agent<br/>re-derive via Settings.get_agent_command"]
    RP -->|"start command recorded, no agent_type"| RL["replay<br/>no argument, tmux replays its own record"]
    RP -->|"probe SUCCEEDED and start command empty"| SH["shell<br/>positive evidence of a bare login shell"]
```

`shell` and `cannot_determine` are kept apart on purpose. A respawn matches the
SAME row and never writes a new one - the tmux `session_created` value is a property of the
session, not the pane's process, so the identity triple `(tmux_socket,
tmux_name, tmux_created_epoch)` is unchanged (`session_respawn.py:12-21`).

**The rename discriminator** is `(creation epoch, tmux session_id)` -
`session_lifecycle.py::rename_map`. Neither half is sufficient: the epoch is
one-second resolution, and the tmux `session_id` resets to `$0` on every tmux server
restart. A discriminator seen twice in one listing is DROPPED, not resolved -
picking either would be a verdict nobody measured.

---

## STATE INVENTORY - the machine-readable half

`tests/test_status_model_chart_drift.py` reads the block below and asserts it
BOTH ways against the code, then asserts every name in it actually appears
inside a mermaid chart on this page. Adding a state to the code without adding
it here fails the build; naming a state here that no longer exists in the code
fails the build too.

Format: `state | axis | defining symbol`.

```state-inventory
running | activity | src/core/session_status.py::STATUS_RUNNING
idle | activity | src/core/session_status.py::STATUS_IDLE
dead | activity | src/core/session_status.py::STATUS_DEAD
unknown | activity | src/core/session_status.py::STATUS_UNKNOWN
question | activity | src/core/session_status.py::STATUS_QUESTION
working | activity | src/core/session_status.py::STATUS_WORKING
working_subagent | activity | src/core/session_status.py::STATUS_WORKING_SUBAGENT
finished_unread | activity | src/core/session_status.py::STATUS_FINISHED_UNREAD
running | lifecycle | src/core/db_models.py::SESSION_LIFECYCLE_RUNNING
stopped | lifecycle | src/core/db_models.py::SESSION_LIFECYCLE_STOPPED
unknown | lifecycle | src/core/db_models.py::SESSION_LIFECYCLE_UNKNOWN
created | origin | src/core/db_models.py::SESSION_ORIGIN_CREATED
adopted | origin | src/core/db_models.py::SESSION_ORIGIN_ADOPTED
observed | origin | src/core/db_models.py::SESSION_ORIGIN_OBSERVED
evaluated | reconcile | src/core/session_lifecycle.py::RECONCILE_EVALUATED
probe_unavailable | reconcile | src/core/session_lifecycle.py::RECONCILE_PROBE_UNAVAILABLE
listing_incomplete | reconcile | src/core/session_lifecycle.py::RECONCILE_LISTING_INCOMPLETE
no_sessions_table | reconcile | src/core/session_lifecycle.py::RECONCILE_NO_TABLE
agent | respawn | src/core/session_respawn.py::RESPAWN_AGENT
replay | respawn | src/core/session_respawn.py::RESPAWN_REPLAY
shell | respawn | src/core/session_respawn.py::RESPAWN_SHELL
not_dead | respawn | src/core/session_respawn.py::RESPAWN_NOT_DEAD
cannot_determine | respawn | src/core/session_respawn.py::RESPAWN_CANNOT_DETERMINE
crashed | tray | macOS/tray-status.js::TRAY_STATES
attention | tray | macOS/tray-status.js::TRAY_STATES
unknown | tray | macOS/tray-status.js::TRAY_STATES
starting | tray | macOS/tray-status.js::TRAY_STATES
stopped | tray | macOS/tray-status.js::TRAY_STATES
update | tray | macOS/tray-status.js::TRAY_STATES
ok | tray | macOS/tray-status.js::TRAY_STATES
```

The `archived` axis has no state vocabulary of its own - `archived_at` is a
timestamp or NULL, so it is asserted by the test as a two-valued column rather
than by name.

---

## Where the code disagrees with itself

Found while deriving this. Each is a real inconsistency in the tree at
`c8865c0`, not a stylistic preference.

1. **`ALL_STATUSES` and `ALL_ACTIVITY_STATUSES` are not nested sets.**
   `running` is in the first and not the second; the three hook states are in
   the second and not the first. This is documented
   (`session_status.py:98-102`) and correct, but it means "a status" has no
   single membership test in this codebase and every validator has to say
   which vocabulary it means.

2. **The client's status vocabulary is a third set that matches neither.**
   `client/js/session-status-ui.js` `STATUS_LABELS` has NINE keys: the seven
   `ALL_ACTIVITY_STATUSES` plus `stopped` (a *lifecycle* value, not an
   activity one) plus `running` (an activity value the unified vocabulary
   deliberately excludes, kept as a back-compat alias mapping onto the
   `working` dot class). The dot the user looks at is therefore rendering two
   axes through one switch.

3. **`launchpad.js` tests for a lifecycle value the schema cannot hold.**
   `_endedSessionsForTree` (`client/js/launchpad.js:2888`) accepts
   `rec.lifecycle === 'stopped' || rec.lifecycle === 'dead'`. `SESSION_LIFECYCLES`
   is `running / stopped / unknown` (`db_models.py:171`) and nothing anywhere
   writes `lifecycle='dead'` - grep for a writer returns nothing. The `dead`
   arm is unreachable. It is harmless today and it is exactly the conflation
   that item 2 makes easy.

4. **`lifecycle_source` has no canonical set, and the set that looks canonical
   is incomplete.** `db_models.py:178-181` names four tokens (`tmux_list`,
   `probe_failed`, `tmux_missing`, `import`) and there is no
   `SESSION_LIFECYCLE_SOURCES` tuple to validate against. Two more values are
   minted elsewhere and never added: `"adopt"`
   (`session_adopt_persist.py:71`) and `"create"`
   (`session_create_persist.py:78`). A seventh shape exists as a composite,
   `f"import:{verdict.reason}"` and `f"import:rerun:{verdict.reason}"`
   (`session_import.py:535, 694`). Of the four "canonical" ones,
   `probe_failed` is deliberately never written and `tmux_list` is written
   only by `session_lineage.py:467`.

5. **`ACTIVITY_STATUS_PRIORITY` documents an ordering nothing consults.**
   `session_status.py:115-119` says so itself. It is a comment with a type
   annotation. Harmless, but a reader can reasonably assume it arbitrates
   something.

6. **`unknown` rendered identically to `idle` until this branch.**
   `client/css/status-dot.css:113-147` records the repair: the two were "two
   colour rules" that came out pixel-identical, and `unknown` is now a
   genuinely transparent-centred **hollow** ring - shape, not hue, so it
   survives the three themes that zero every radius token. `stopped` is
   deliberately FILLED and not hollow, because a stopped session is a
   measured fact, not a could-not-measure.

7. **The reaper is not on a timer, though it is often described as one.**
   Its single caller is the home-screen probe
   (`session_manager.py:4160`). There is no scheduled reconcile anywhere in
   `src/`. A machine whose home screen is never opened never reaps, and
   nothing in the model says otherwise.

## What I could not evaluate

- **Whether the `dead` arm in `_endedSessionsForTree` was ever reachable.** It
  is not reachable at `c8865c0`. Whether an older schema or an import path
  once wrote `lifecycle='dead'` is not answerable from this tree.
- **Runtime behaviour.** Everything above is read from source. Nothing was run
  against the live instance on 10.0.1.150, which is read-only for this work.
