# Removing a test project or session

Written 2026-08-28 after a cleanup that took five passes and still missed
things. Each pass genuinely believed it was finished, because each one
cleared a different LAYER and the layers are not visible from each other.

A CloudeCode test session leaves traces in **seven** places. Killing the
tmux session clears exactly one of them, and the UI keeps rendering the
rest, which is why "it wasn't a full clear" was the correct verdict three
times in a row.

## The seven layers

Work top to bottom. Later steps depend on earlier ones being done.

| # | Layer | Where | Check |
|---|---|---|---|
| 1 | tmux session | socket `cloude` (live) or `cloude-v11` | `tmux -L <socket> list-sessions` |
| 2 | Background agents | Claude's own registry | `claude agents --json` -> `kind: background` |
| 3 | Session rows | `cloude.db` `sessions` | one row per INSTANCE **and** per conversation |
| 4 | Project row | `cloude.db` `projects` | survives its sessions |
| 5 | Transcripts | `~/.claude/projects/<slug>/*.jsonl` | one file per conversation, not per session |
| 6 | Project folder | the working dir itself | `~/Development/...` |
| 7 | Config | `config.json` `projects` | re-imports on next start if listed |

## The traps, each of which cost a pass

**A session has MORE THAN ONE ROW.** An instance row (epoch SET) plus one
conversation row per `/clear`, `/branch` or `/fork` (epoch NULL). Deleting
by tmux name catches them all; deleting the one you can see does not.

**The UI delete is a SOFT delete.** It sets `archived_at` and the row
stays. Fine for hiding, useless for cleanup - archived rows still appeared
in every subsequent count.

**Killing tmux does not end the background agents.** A `/fork` spawns an
agent with no terminal; it survives its parent's tmux session and keeps a
pid. `claude agents --json` is the only place it shows.

**Transcripts outlive everything.** Killing the session, deleting the row
and removing the project all leave the `.jsonl` files untouched.

**The project row outlives its sessions**, and the project FOLDER outlives
the row. Three separate deletions.

**A browser tab does not re-fetch.** After cleanup the page keeps
rendering the old list until a hard reload - which read as "you deleted my
session" when the session was fine. Reload before believing the UI.

## The order that works

1. `tmux -L <socket> kill-session -t <name>` for each session
2. `claude agents --json` -> kill any `kind: background` pids you created
3. Delete `sessions` rows `WHERE tmux_name LIKE '<prefix>%'` - clear
   `parent_session_id` references FIRST so nothing is left dangling
4. Delete the `projects` row (and NULL out `project_id` on any survivors)
5. Remove `~/.claude/projects/<slug>/*.jsonl` - **archive to /tmp first**
6. Remove the project folder - **archive to /tmp first**
7. Confirm the project is not in `config.json`, or it returns on restart
8. `PRAGMA integrity_check` + a dangling-parent count, then HARD RELOAD
   the browser

## What "done" looks like

Not "the list looks empty". These four, measured:

```
sessions rows for the prefix      0
projects row                      gone
transcripts for the slug          0
background agents you created     0
integrity_check                   ok
dangling parent_session_id        0
```

## Never delete what you cannot identify

Every deletion above should be preceded by a listing of exactly what will
go and what will be kept, and followed by a re-read. The one time this was
skipped, a keep-list was computed from memory rather than from live state
- cross-reference `claude agents --json` and unarchived DB rows instead,
and archive to `/tmp` before removing anything with content in it.
