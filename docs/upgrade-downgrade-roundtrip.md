# Can the old version be dropped back in?

Measured 2026-08-20 by executing the round trip, not by reading the
migration's own promises. Re-run it with:

```
./scripts/ci/roundtrip-upgrade-downgrade.sh
```

Everything happens in a throwaway work directory. The script clones the
repo rather than checking anything out in place, points `CLOUDE_STATE_DIR`
at its own directory, binds a port before using it, and never touches a
real install.

## Which "old"

`v0.8.1` (2026-08-04) - the newest published release tag, and the last one
that predates BOTH `src/core/config_migration.py` and the datastore. It is
an ancestor of `integration/ui-only`, so the trip is a real fast-forward
and back. It is also what a user dropping back to "the last release"
actually gets.

## Verdict

The plain round trip works. The old version starts, reads the migrated
`config.json`, and sees every project. Three qualifications, all measured,
all reproducible by the harness:

### 1. The migration is additive, as documented

`config.json` across the upgrade: `config_version` and `terminal_commands`
ADDED, `common_slash_commands` gained `/login` appended as a bare string.
Nothing removed, nothing rewritten. The old version ignores the two new
top-level keys (`AuthConfig` inherits pydantic's default `extra="ignore"`)
and every old write path is a raw-dict round trip
(`save_project`, `delete_project`, `update_project`, `move_project_to_top`,
`add_provider_model`, `remove_provider_model`), so a downgraded install
writing the config back out PRESERVES the new version's keys. Measured:
after the old version added a project to the migrated config, the only key
that changed was `projects`.

### 2. An object-form slash entry is a hard downgrade break

`AuthConfig.common_slash_commands` is `List[str]` at v0.8.1 and
`List[Union[str, Dict[str, Any]]]` on the new tip. One object-form entry -
a shape the new version accepts - makes the old version's
`load_auth_config` raise a `ValidationError` and the server exit with
`Application startup failed`. Not a degraded mode: it does not start.

The config migration does NOT introduce this shape (it appends bare
strings), so a pure upgrade leaves the config downgrade-safe. Anything
that later writes an object entry does not.

Guarded by step 08 of the harness, which is DECLARED to fail. If that step
ever starts passing, the guard has stopped measuring anything.

### 3. Re-upgrade silently drops a project created during the downgrade

The one real data loss, and it is silent. The projects table is
authoritative on the new tip (`src/core/project_authority.py`), and
`run_first_run_import` is gated on `meta.imported_from_json_at`, which is
stamped once. So:

1. downgrade, old version writes a new project into `config.json`;
2. re-upgrade - no re-import, the table still has the old row set;
3. `projects_service.current_view` reports `mode: db`, `degraded: False`,
   and serves the table. The project is invisible with no banner;
4. the first project write in the new version calls `snapshot_projects`,
   which replaces `config.json`'s `projects` key wholesale from the table.
   The project is now gone from the file too.

Steps 1 through 4 were executed; step 4 was run directly against the
harness's own state dir and observed to delete the entry.

### 4. There is no "this config is newer than I understand" check

`migrate_config_dict` compares `existing_version >= CURRENT_CONFIG_VERSION`
and returns unchanged. Handed `config_version: 99` it returns
`changed=False` and leaves the 99 in place - no refusal, no warning. In
practice that means a config written by a FUTURE version is treated as
already-current by this one, and whatever migration that future version
would have needed never runs. Contrast `src/core/db_version_gate.py`,
which does exactly this check for the database.

### 5. Session metadata DOES survive the round trip (fixed 2026-08-24)

Measured ABSENT on 2026-08-20. Fixed and re-measured INTACT on
2026-08-24; the harness step now DECLARES `INTACT`, so a regression goes
UNEXPECTED. What follows is the defect and the fix, kept rather than
deleted because the shape recurs.

**What was wrong.** `v0.8.1` reads `session_metadata.json` from
`LOG_DIRECTORY` and nowhere else. The current version resolved it through
`Settings._resolve_state_file()`, which RE-DERIVED the answer from disk on
every single call. That made the resolution a function of whatever had
just happened to the files rather than of the install.

The upgrade alone was always safe. But `SessionManager.detach_session`
unlinks the RESOLVED metadata path and then, when another session is still
live, calls `_save_session_metadata()`. That save re-resolved; by then the
old location no longer existed, so the resolver returned the NEW path and
the file MOVED to the state directory. Nothing copied it back.
`_clear_stale_metadata` had the same shape. One ordinary user action,
permanent relocation, no error anywhere.

Lost on the downgrade: the most-recently-active session's id, its working
directory, its agent type, its pinned theme, and `owned_tmux_sessions` -
the set that tells the app which tmux sessions are ITS OWN. The tmux
sessions kept running and presented as strangers to re-adopt.

**And the project's own rollback tool did it too**, which is the sharper
half. `take_backup()` located each state file individually, found
`session_metadata.json` at `LOG_DIRECTORY`, and said so.
`restore_backup()` then placed every state file at `resolve_state_dir()`
regardless of origin. So `scripts/rollback.sh`, the thing a user runs
specifically to go back, was itself the step that made going back fail.

**The fix, in two halves.**

*The resolver decides once.* `_resolve_state_file()` now pins its answer on
the FIRST resolution of a given filename and keeps returning it. An install
that started at `LOG_DIRECTORY` keeps writing there; one that started in the
state dir stays there; nothing relocates as a side effect of anything. The
pin is keyed on `(filename, state_dir_override, log_directory)`, so
repointing either configured directory legitimately re-asks the question
while a file appearing or disappearing does not. `get_state_file_location()`
publishes the decision, so no caller re-derives it - a resolution
recomputed independently at several call sites is what produced this bug,
and patching the two call sites that were noticed would have left the rest.

Precedence on that first resolution is unchanged, including the
both-present case: the NEW path wins and the old copy is left on disk,
never deleted. It wins because it is the location this version WRITES to;
preferring the old copy would leave the app reading a file it is not
updating, which is a guaranteed divergence rather than a possible one. The
`state_file_present_in_both_locations` warning still names both paths.

*The rollback restores to where it found it.* The backup manifest gained a
fourth field, the ORIGIN DIRECTORY, recorded by `take_backup()` from the
path `resolve_state_file` actually read. `restore_backup()` honours it. The
field is APPENDED, so an older manifest parses unchanged and carries an
empty origin - reported as an explicit COULD NOT DETERMINE line and
restored to the current state directory (the behaviour that manifest was
written under), never silently guessed at. `NOT_PRESENT` rows still print
three fields, so no existing anchored matcher of that format broke.

**Measured, not reasoned about.** `tests/test_session_meta_continuity.py`
runs the two real bash functions and reads the DISK, and covers both
origins so a one-line inversion of the bug cannot pass. The end-to-end
answer is the `meta-*` steps of the harness: seeded by `v0.8.1`'s own
code, put through the NEW version's real detach sequence, read back with
`v0.8.1`'s own resolver. Verdict INTACT, `old_sees_session_id`
`roundtrip-b` - the session the new version last persisted.

**Stale is still possible, and still worse than absent.** With the file in
BOTH places the resolver prefers the new one and leaves the old untouched,
so a downgrade rehydrates a session that is no longer live - a wrong
answer rather than a missing one, with nothing on screen saying so.
Covered by
`test_metadata_present_in_both_locations_leaves_the_old_copy_stale`.

**One harness ordering bug found while confirming the fix.** The
post-downgrade probe used to run AFTER `capture_step "downgraded-old"`,
i.e. after the old server had already started and reconciled. With the
location bug fixed, `v0.8.1` loaded the file correctly
(`session_metadata_loaded`, `session_id` `roundtrip-b`) and then pruned
`cloude_roundtrip-b` from the owned set and deleted the metadata as stale
- on a socket where that tmux session was demonstrably alive. So the probe
reported ABSENT no matter what the resolver did: a verdict the measurement
could not tell apart from the bug it exists to catch. The probe now runs
BEFORE the old server starts, because the question this step asks is about
the file's LOCATION. The owned-set prune is a separate, real
name-form difference between the two versions and is NOT measured here -
CANNOT DETERMINE, and it deserves its own step.

### Two traps that manufactured this finding three times before it was real

Both produced a clean, plausible ABSENT out of the fixture rather than
the product, and both are guarded now.

`TmuxBackend.discover_existing()` lists only `cloude_`-prefixed names,
and the reconciler prunes the owned set against exactly that list. Bare
session names read as dead, so the owned set was pruned empty and the
metadata deleted.

The reconciler then builds its backend from `persisted.id` and matches
`cloude_<id>`; it never reads the `tmux_session` field. A persisted id
that is not the bare tmux name gets its metadata deleted as stale before
the relocation path is reached. The harness now reads the upgrade's own
server log and reports CANNOT DETERMINE when it sees
`stale_session_metadata_deleted` without a matching rehydrate, instead of
scoring the rejection as a finding.

### A safety defect in the harness itself

The seed fixture carried `tmux_socket_name: "cloude"` - the socket a real
install runs on, with the user's live work on it. Every server the
harness started reconciled against that socket while the script's header
claimed it touched nothing real. It is now pinned to a per-run throwaway
socket and re-asserted before EVERY server start, not once, because a
later step can rewrite `config.json`.

### Still not measured

Whether the DOWNGRADED old version, once running, writes metadata back to
`LOG_DIRECTORY` in a way the next upgrade then treats as the both-present
ambiguous case. The steps above stop at the read.
