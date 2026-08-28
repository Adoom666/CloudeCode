# Why the project list reconciles on every start

Written 2026-08-20, after an executed upgrade / downgrade / re-upgrade
round trip found the loss. Not a design note written ahead of the code.

## What was measured

On a re-upgraded install:

```
config: 6 projects   db: 5 rows   served_mode: "db"   degraded: false
in_config_but_not_in_db: ["roundtrip-probe-after-downgrade"]
```

A project the OLD version created while the user was downgraded never
reached the projects table, because the config-projects import sat behind
`meta.imported_from_json_at`, a latch stamped exactly once per install,
ever. Nothing warned. Every three-outcome check in `project_authority`
reported healthy and was RIGHT to: the database opened, answered, and was
authoritative. It simply answered with the wrong row set.

That is the one failure shape a three-outcome check cannot see. Its three
states are pass, fail and could-not-evaluate, and a successful query
returning incomplete data is none of them.

One project write later, `snapshot_projects` rebuilt config.json's
`projects` key wholesale from the table and the entry left the file too.
From that point it was unrecoverable.

## Why the latch was right for sessions and wrong for projects

The difference is the INPUT, not the caller.

The sessions import reads a live tmux process table, an input that is
gone by tomorrow. Re-running it against a failed probe would destroy
history, which is why `session_import` guards it as carefully as it does.
That latch is untouched.

The projects import reads config.json, a durable file that says the same
thing every time it is read. Re-reading it costs nothing and can only add
rows the table has never seen. So the projects stage moved out from
behind the sessions gate and became `project_reconcile.reconcile_projects`,
which runs on every start.

## The question the reconcile has to answer honestly

A root is in config.json and not in `projects`. Three causes, not two:

| Cause | Response |
|---|---|
| never imported (the round-trip case) | IMPORT it |
| the user deleted it through the new version | LEAVE IT DELETED |
| deleted before deletion tracking existed | CANNOT EVALUATE |

Nothing in the v4 schema could tell the first two apart. `delete_project`
is a hard DELETE, deliberately, and it left no `deleted_at`, no archive
flag and no trail entry, so to a set comparison a never-imported root and
a deleted one are byte-identical. A reconcile built on the sets alone
would resurrect every deleted project on the next start, trading one
silent data defect for another and a worse one: the user made a decision
and the app would quietly reverse it.

Schema v5 adds `project_tombstones`, one row per deleted root, written in
the SAME transaction as the DELETE and cleared when a project is created
at that root again. A tombstone records a past decision; it never
overrides a present one.

A separate table rather than a soft delete on `projects`, because the row
carries `UNIQUE(root)`. Left in place under an `archived_at` it would
keep occupying the root, and a user who deleted a project and then added
the same folder back would hit `ProjectRootConflict` from a row nothing
renders. This way `projects` keeps meaning what it always meant - every
row is a live project - and `project_snapshot` keeps building config.json
from that table without learning a second exclusion rule.

## The third state, and why it is bounded

Deletions made before the tombstone table existed left no evidence in
either direction. Those roots are UNDETERMINED. They are not imported,
because that would undo a deletion on a guess, and not skipped quietly,
because that would hide the very loss this exists to catch. They are
named, counted, and reported in the reconcile block on
`GET /projects/authority`.

The set cannot grow. It is captured ONCE, on the first reconcile after
the v4 to v5 migration, into `meta.project_reconcile_undetermined_roots`.
Every root that appears after that instant is covered by tombstones and
classifies cleanly. A fresh install's set is empty forever.

## Legacy-gap detection is exact, not a heuristic

Whether a database has a legacy gap is decided by HOW IT REACHED v5, not
by inspecting its contents:

- created by this code, so it ran the whole chain 0 to 5 in one
  transaction. `_step_v0_to_v1` stamps the marker on the
  genuinely-new-file path. Tracking existed before any project row could
  be created, let alone deleted. No gap.
- created by an earlier version and migrated in. That version never heard
  of the marker, so `_step_v4_to_v5` finds it absent and records a gap.

A row count cannot do this job. A database whose projects were ALL
deleted is indistinguishable from a fresh one by counting, and an earlier
draft of this step got exactly that wrong.

## What is still not covered

- A database below schema v5 cannot classify anything, so
  `reconcile_projects` imports NOTHING there and reports every
  unexplained root as undetermined. It also refuses to write the capture
  record, because freezing a verdict taken from a schema that cannot hold
  the evidence would stop the real assessment ever running.
- An undetermined root has no resolve-in-the-UI path yet. It is reported
  with its reason; acting on it is manual.
